//! Focus statistics, aggregated once so three runtimes can agree on the numbers.
//!
//! ## The problem this file exists to solve
//!
//! Focus data is read by three runtimes: the webview (which draws charts), this
//! command (which the frontend may call), and `agent/focus_stats.py` (which
//! Lumine's agent will eventually read). Three implementations of "focus minutes
//! per day" is three chances to disagree, and a disagreement here is worse than a
//! crash — a chart saying Tuesday was 45 minutes when the log says 90 is a number
//! a person trusts and acts on.
//!
//! So the *contract* is pinned (`src/pages/focus/focusStatsContract.ts` names every
//! field and unit) and the *values* are pinned by fixtures in
//! `scripts/verify-focus-contract.mts`. This file is the reference implementation
//! the other two are compared against.
//!
//! ## Why the timezone offset is an argument
//!
//! "Focus minutes per day" needs a *local* calendar day. This crate has no
//! timezone library — `chrono` and `time` are both absent from `Cargo.toml`, and
//! adding a dependency to answer one question would be the wrong trade.
//!
//! The standard answer needs no database: shift the timestamp by the offset, then
//! do plain UTC calendar arithmetic on the shifted value. A local day is a UTC day
//! plus a whole number of hours, so bucketing the shifted value by UTC date *is*
//! bucketing by local date — and it is exact, including across a DST change,
//! because the offset supplied is the one in force at the moment of the call.
//!
//! Making the offset explicit rather than asking the OS is what lets Python and
//! TypeScript reproduce these numbers exactly: they both already know their own
//! offset and neither has to be trusted to guess it.

use serde_json::{json, Value};

/// Bumped when a field changes meaning. `scripts/verify-focus-contract.mts`
/// asserts that all three implementations report the same version.
pub const STATS_VERSION: i64 = 1;

/// The contract's version, exported for the command's own documentation.
pub const CONTRACT: &str = "focus-stats/1";

/// A day with no activity is still a day.
///
/// The `byDate` series carries every day in the requested window, including the
/// empty ones, because a bar chart that omits a zero has no axis position for it —
/// Tuesday's 45 minutes would render as Wednesday's. Emitting the zeros is what
/// makes the series contiguous.
const MS_PER_DAY: f64 = 86_400_000.0;

/// Aggregate a focus document into the documented statistics shape.
///
/// `utc_offset_minutes` is **east-positive** — `new Date().getTimezoneOffset()`
/// returns the *negation*, so the caller must pass `-getTimezoneOffset()`. The
/// mismatch is easy to make and hard to see (every day is off by the offset), so it
/// is worth stating: see [`local_ms`].
pub fn summarise(document: &Value, utc_offset_minutes: i64, days: i64) -> Value {
    summarise_at(document, utc_offset_minutes, days, now_ms())
}

/// [`summarise`] with the clock supplied.
///
/// ## Why "now" is an argument
///
/// Almost every interesting number here is relative to *today* — today's minutes,
/// the daily goal, the streak, which end of the window the series starts at. A
/// function that calls `now_ms()` itself therefore cannot be tested against a
/// fixture without the fixture being written relative to whenever the test ran,
/// and "today" quietly changes underneath an assertion between this morning and
/// tonight.
///
/// This was not hypothetical. The first version read the clock internally, and the
/// tests failed in a way that looked like arithmetic bugs and was actually a
/// fixture that had drifted: the sessions were built around a fixed instant and
/// "today" was the real one.
///
/// The window is also clamped **here**, not only in the command. `summarise` is the
/// public entry point, and a clamp that lives in the caller is a clamp the next
/// caller forgets.
pub fn summarise_at(
    document: &Value,
    utc_offset_minutes: i64,
    days: i64,
    now: f64,
) -> Value {
    // 365 is `focus_store::SESSION_RETENTION_DAYS`: asking for more days than the
    // file can possibly hold is either a mistake or an attempt to make us allocate.
    let days = days.clamp(1, 365);

    let sessions = document
        .get("sessions")
        .and_then(Value::as_array)
        .map(|rows| rows.as_slice())
        .unwrap_or(&[]);
    let settings = document.get("settings");

    let daily_goal = settings
        .and_then(|s| s.get("dailyGoalMin"))
        .and_then(Value::as_f64)
        .unwrap_or(120.0);

    let today_local = local_day_string(local_ms(now, utc_offset_minutes));
    let window_start = day_start_ms(&today_local, -(days - 1));
    let window_end = day_start_ms(&today_local, 0);

    let mut by_date: Vec<(String, f64, u64)> = Vec::new();
    let mut by_subject: Vec<(String, f64, u64)> = Vec::new();
    let mut unassigned_minutes = 0.0;

    let mut counted = 0u64;
    let mut completed_count = 0u64;
    let mut interrupted_count = 0u64;
    let mut focus_minutes = 0.0;
    let mut completed_minutes = 0.0;
    let mut interruption_total = 0u64;
    let mut active_days: Vec<String> = Vec::new();

    for session in sessions {
        let Some(row) = session.as_object() else {
            // Not a record. `focus_store::validate` already refuses these on save,
            // so reaching here means the document was written by something else;
            // skipping is the safe reading and it keeps the totals honest.
            continue;
        };

        // `startedAt` is the anchor, not `endedAt`: a session that runs past
        // midnight started on the day the person sat down, which is the day they
        // meant. Bucketing by the end would move a 23:50 session into tomorrow and
        // quietly shrink today.
        let Some(started) = row.get("startedAt").and_then(Value::as_f64) else {
            continue;
        };
        if !started.is_finite() || started < 0.0 {
            continue;
        }

        let minutes = row
            .get("actualMin")
            .and_then(Value::as_f64)
            .filter(|m| m.is_finite() && *m >= 0.0)
            .unwrap_or(0.0);

        let completed = row
            .get("completed")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let interruptions = row
            .get("interruptions")
            .and_then(Value::as_f64)
            .filter(|n| n.is_finite() && *n >= 0.0)
            .unwrap_or(0.0) as u64;

        counted += 1;
        // Every session contributes to `focusMinutes`; only a completed one
        // contributes to `completedFocusMinutes`. Keeping both is what lets a chart
        // say "you sat for 4h, you finished 3h" without either number having to
        // pretend the other does not exist.
        focus_minutes += minutes;
        if completed {
            completed_count += 1;
            completed_minutes += minutes;
        } else {
            interrupted_count += 1;
        }
        interruption_total += interruptions;

        let shifted = local_ms(started, utc_offset_minutes);
        let day = local_day_string(shifted);

        if !active_days.contains(&day) {
            active_days.push(day.clone());
        }

        // Only days inside the window reach the series. A five-year-old session is
        // still in the totals — it really happened — but it must not add a bar to
        // a seven-day chart at the left edge.
        //
        // Clamped into the window rather than dropped when a session's local day is
        // *after* today, which a backwards clock jump produces. Dropping it would
        // make the series lose a session that exists; folding it into today keeps
        // the series the same length, which is the property a chart depends on.
        if shifted >= window_start {
            let bucket_date = if shifted >= window_end {
                today_local.clone()
            } else {
                day.clone()
            };
            if let Some(bucket) = bucket_for(&mut by_date, &bucket_date) {
                bucket.1 += minutes;
                bucket.2 += 1;
            }
        }

        let subject = row
            .get("subject")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty());
        match subject {
            Some(name) => {
                if let Some(bucket) = bucket_for(&mut by_subject, name) {
                    bucket.1 += minutes;
                    bucket.2 += 1;
                }
            }
            None => unassigned_minutes += minutes,
        }
    }

    // Fill the window so every day in it has a row. Emitted *after* the sessions
    // so a day nobody studied still has an axis position — a bar chart that omits
    // a zero renders Tuesday's 45 minutes as Wednesday's.
    let mut cursor = window_start;
    for _ in 0..days {
        let day = local_day_string(cursor);
        if !by_date.iter().any(|(existing, _, _)| existing == &day) {
            by_date.push((day, 0.0, 0));
        }
        cursor += MS_PER_DAY;
    }
    by_date.sort_by(|left, right| left.0.cmp(&right.0));
    by_subject.sort_by(|left, right| right.1.total_cmp(&left.1).then(left.0.cmp(&right.0)));

    let today = by_date
        .iter()
        .find(|(date, _, _)| date == &today_local)
        .cloned();
    let today_minutes = today.as_ref().map(|(_, minutes, _)| *minutes).unwrap_or(0.0);
    let today_sessions = today.as_ref().map(|(_, _, count)| *count).unwrap_or(0);

    let todos = document
        .get("todos")
        .and_then(Value::as_array)
        .map(|rows| rows.as_slice())
        .unwrap_or(&[]);
    let done_todos = todos
        .iter()
        .filter(|todo| todo.get("done").and_then(Value::as_bool).unwrap_or(false))
        .count() as u64;

    json!({
        "version": STATS_VERSION,
        "contract": CONTRACT,
        "generatedAt": now,
        "utcOffsetMinutes": utc_offset_minutes,
        "windowDays": days,
        "totals": {
            "sessions": counted,
            "completedSessions": completed_count,
            "interruptedSessions": interrupted_count,
            "focusMinutes": round2(focus_minutes),
            "completedFocusMinutes": round2(completed_minutes),
            "interruptions": interruption_total,
            // Guarded rather than plain: a divide by zero in JSON is not an error,
            // it is a `null` that every consumer then has to special-case.
            "averageSessionMinutes": if counted == 0 { 0.0 } else { round2(focus_minutes / counted as f64) },
            "dailyGoalMinutes": daily_goal,
            "todayMinutes": round2(today_minutes),
            "goalProgress": if daily_goal <= 0.0 { 0.0 } else { round4(today_minutes / daily_goal) },
            "unassignedMinutes": round2(unassigned_minutes)
        },
        "today": {
            "date": today_local,
            "focusMinutes": round2(today_minutes),
            "sessions": today_sessions
        },
        "streak": streak(&active_days, &today_local),
        "byDate": by_date.iter().map(|(date, minutes, count)| json!({
            "date": date,
            "focusMinutes": round2(*minutes),
            "sessions": count
        })).collect::<Vec<Value>>(),
        "bySubject": by_subject.iter().map(|(subject, minutes, count)| json!({
            "subject": subject,
            "focusMinutes": round2(*minutes),
            "sessions": count
        })).collect::<Vec<Value>>(),
        "todoCompletion": {
            "total": todos.len() as u64,
            "done": done_todos,
            "ratio": if todos.is_empty() { 0.0 } else { round4(done_todos as f64 / todos.len() as f64) }
        },
        "noteCount": document.get("notes").and_then(Value::as_array).map(|r| r.len()).unwrap_or(0)
    })
}

/// Shift a UTC timestamp into "local milliseconds".
///
/// After this, `local_ms / 86_400_000` is the local day index, because a local day
/// is a UTC day shifted by a whole number of hours. Doing calendar arithmetic on
/// the shifted value is therefore exact, and needs no timezone database.
///
/// The sign is the trap. JavaScript's `getTimezoneOffset()` returns minutes to
/// *add* to local time to reach UTC — UTC+6 returns `-360` — so east-positive
/// means passing `-getTimezoneOffset()`.
fn local_ms(utc_ms: f64, utc_offset_minutes: i64) -> f64 {
    utc_ms + (utc_offset_minutes as f64) * 60_000.0
}

/// `YYYY-MM-DD` for a shifted timestamp.
///
/// Hand-rolled rather than pulled from a library for the same reason as the
/// offset: the arithmetic is a division and a modulus, and it has no dependency
/// to get wrong. The negative case is handled because a timestamp before 1970 is
/// representable in a corrupt document and must not panic.
fn local_day_string(shifted_ms: f64) -> String {
    let day_index = (shifted_ms / MS_PER_DAY).floor() as i64;
    let (year, month, day) = civil_from_days(day_index);
    format!("{year:04}-{month:02}-{day:02}")
}

/// Days since the Unix epoch to a civil `(year, month, day)`.
///
/// Howard Hinnant's `civil_from_days`, which is exact for the whole proleptic
/// Gregorian range and uses no floating point. The constant is his: 719468 is the
/// number of days from 0000-03-01 to 1970-01-01 under the shifted-year leap rule.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as i64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Midnight, local, of the `offset` days from `today`'s date.
///
/// The offset is in *days*, which is what `civil_from_days` consumes, so no month
/// arithmetic is needed and a 90-day window is not a special case.
fn day_start_ms(today: &str, day_offset: i64) -> f64 {
    let index = today_index(today) + day_offset;
    index as f64 * MS_PER_DAY
}

/// The day index of a `YYYY-MM-DD` string, or today if it will not parse.
///
/// Falling back rather than erroring: a malformed date in a *statistics* call is
/// worth one wrong bucket, not a failed page. The parse is strict about shape so a
/// typo cannot silently become "epoch".
fn today_index(date: &str) -> i64 {
    let mut parts = date.split('-');
    let (Some(y), Some(m), Some(d)) = (
        parts.next().and_then(|p| p.parse::<i64>().ok()),
        parts.next().and_then(|p| p.parse::<i64>().ok()),
        parts.next().and_then(|p| p.parse::<i64>().ok()),
    ) else {
        return 0;
    };
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        return 0;
    }
    days_from_civil(y, m as u32, d as u32)
}

/// Inverse of [`civil_from_days`].
fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = if month > 2 { month - 3 } else { month + 9 } as i64;
    let doy = (153 * mp + 2) / 5 + day as i64 - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Find or create a bucket by key.
fn bucket_for<'a>(
    buckets: &'a mut Vec<(String, f64, u64)>,
    key: &str,
) -> Option<&'a mut (String, f64, u64)> {
    if let Some(index) = buckets.iter().position(|(existing, _, _)| existing == key) {
        return Some(&mut buckets[index]);
    }
    buckets.push((key.to_string(), 0.0, 0));
    buckets.last_mut()
}

/// Current and longest run of consecutive active days, measured back from today.
///
/// ## Why "measured back from today" and not "up to the last active day"
///
/// The useful question after a day off is not "what was your streak" but "have I
/// lost it yet". Counting up from the last active day would keep reporting a
/// six-day streak indefinitely, and that number is exactly the one that makes
/// somebody stop opening the app. So the run is anchored to today and is only
/// reported while it is still live.
///
/// ## The one-day grace
///
/// A streak survives a day that has *not been studied yet* — at 9am, today is
/// empty and reporting zero for a run that is still alive would be a lie. But only
/// one such day: starting the walk at yesterday when today is empty, and stopping
/// at the first day with no session, means a genuinely missed day ends the run.
fn streak(active_days: &[String], today: &str) -> Value {
    let active: std::collections::HashSet<&str> =
        active_days.iter().map(String::as_str).collect();
    let today_index = today_index(today);

    let mut current = 0u64;
    let mut cursor = if active.contains(today) {
        today_index
    } else {
        today_index - 1
    };
    while active.contains(day_string_at(cursor).as_str()) {
        current += 1;
        cursor -= 1;
    }

    json!({
        "current": current,
        "longest": longest_run(active_days),
        "lastActiveDate": active_days.iter().max().cloned()
    })
}

/// The `YYYY-MM-DD` of a local day index.
fn day_string_at(index: i64) -> String {
    local_day_string(index as f64 * MS_PER_DAY)
}

fn longest_run(active_days: &[String]) -> u64 {
    let mut indices: Vec<i64> = active_days.iter().map(|day| today_index(day)).collect();
    indices.sort_unstable();
    indices.dedup();

    let (mut longest, mut run) = (0u64, 0u64);
    let mut previous: Option<i64> = None;
    for index in indices {
        run = match previous {
            Some(last) if index == last + 1 => run + 1,
            _ => 1,
        };
        longest = longest.max(run);
        previous = Some(index);
    }
    longest
}

fn round2(value: f64) -> f64 {
    if !value.is_finite() {
        return 0.0;
    }
    (value * 100.0).round() / 100.0
}

fn round4(value: f64) -> f64 {
    if !value.is_finite() {
        return 0.0;
    }
    (value * 10_000.0).round() / 10_000.0
}

fn now_ms() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as f64)
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The fixture's "now": 2026-02-02T09:13:20Z.
    ///
    /// Every test pins the clock to this instant, which is why [`summarise_at`]
    /// exists. The date is written out because a fixture anchored to a literal
    /// millisecond count is unreadable, and because the arithmetic below is only
    /// checkable by eye if the date is known.
    const T0: f64 = 1_770_000_000_000.0;

    /// Summarise with the fixture clock, so "today" is deterministic.
    fn stats_for(document: &Value, offset: i64, days: i64) -> Value {
        summarise_at(document, offset, days, T0)
    }

    /// Epoch millis `days_from_now` days from [`T0`]. `f64` rather than `Value` so
    /// a session's `endedAt` can be its start plus a duration.
    fn at(days_from_now: f64) -> f64 {
        T0 + days_from_now * MS_PER_DAY
    }

    fn session(days_ago: f64, minutes: f64, completed: bool, subject: Option<&str>) -> Value {
        json!({
            "id": format!("s{days_ago}-{minutes}"),
            "startedAt": at(-days_ago),
            "endedAt": at(-days_ago) + minutes * 60_000.0,
            "plannedMin": 25,
            "actualMin": minutes,
            "phase": "work",
            "subject": subject,
            "todoId": Value::Null,
            "noteId": Value::Null,
            "completed": completed,
            "interruptions": 0
        })
    }

    fn document(sessions: Vec<Value>) -> Value {
        json!({
            "version": 1,
            "settings": { "dailyGoalMin": 120 },
            "todos": [
                { "id": "t1", "done": true },
                { "id": "t2", "done": false },
                { "id": "t3", "done": false },
                { "id": "t4", "done": true }
            ],
            "notes": [{ "id": "n1" }, { "id": "n2" }],
            "sessions": sessions
        })
    }

    fn total(stats: &Value, key: &str) -> f64 {
        stats["totals"][key].as_f64().unwrap_or(f64::NAN)
    }

    fn minutes_on(stats: &Value, date: &str) -> Option<f64> {
        stats["byDate"]
            .as_array()?
            .iter()
            .find(|row| row["date"] == date)
            .and_then(|row| row["focusMinutes"].as_f64())
    }

    #[test]
    fn an_empty_document_answers_every_field_rather_than_being_null() {
        let stats = stats_for(&json!({}), 0, 7);
        for key in [
            "sessions", "completedSessions", "interruptedSessions", "focusMinutes",
            "completedFocusMinutes", "interruptions", "averageSessionMinutes",
            "dailyGoalMinutes", "todayMinutes", "goalProgress", "unassignedMinutes",
        ] {
            assert!(
                stats["totals"][key].is_number(),
                "`{key}` must be a number, not null -- a divide by zero is not an error here"
            );
        }
        assert_eq!(total(&stats, "averageSessionMinutes"), 0.0);
        assert_eq!(stats["byDate"].as_array().expect("byDate").len(), 7);
        assert_eq!(stats["bySubject"].as_array().expect("bySubject").len(), 0);
    }

    #[test]
    fn completed_and_interrupted_minutes_are_reported_separately() {
        let stats = stats_for(
            &document(vec![
                session(0.0, 25.0, true, Some("Maths")),
                session(0.0, 10.0, false, Some("Maths")),
            ]),
            0,
            7,
        );
        assert_eq!(total(&stats, "sessions"), 2.0);
        assert_eq!(total(&stats, "completedSessions"), 1.0);
        assert_eq!(total(&stats, "interruptedSessions"), 1.0);
        assert_eq!(total(&stats, "focusMinutes"), 35.0, "both count as time sat");
        assert_eq!(
            total(&stats, "completedFocusMinutes"),
            25.0,
            "only the finished one counts as focus time"
        );
    }

    #[test]
    fn a_session_with_no_subject_is_counted_rather_than_dropped() {
        let stats = stats_for(
            &document(vec![
                session(0.0, 25.0, true, Some("Maths")),
                session(0.0, 25.0, true, None),
                session(0.0, 25.0, true, Some("   ")),
            ]),
            0,
            7,
        );
        assert_eq!(total(&stats, "focusMinutes"), 75.0, "nothing may vanish");
        assert_eq!(total(&stats, "unassignedMinutes"), 50.0);
        assert_eq!(
            stats["bySubject"].as_array().expect("bySubject").len(),
            1,
            "a blank subject is not a subject"
        );
    }

    #[test]
    fn goal_progress_is_todays_share_of_the_daily_goal() {
        let stats = stats_for(&document(vec![session(0.0, 60.0, true, Some("Maths"))]), 0, 7);
        assert_eq!(total(&stats, "dailyGoalMinutes"), 120.0);
        assert_eq!(total(&stats, "todayMinutes"), 60.0);
        assert_eq!(total(&stats, "goalProgress"), 0.5);

        // A zero goal must be zero progress rather than a division by zero.
        let mut doc = document(vec![session(0.0, 60.0, true, Some("Maths"))]);
        doc["settings"]["dailyGoalMin"] = json!(0);
        assert_eq!(total(&stats_for(&doc, 0, 7), "goalProgress"), 0.0);
    }

    #[test]
    fn a_streak_survives_an_unstarted_today_but_ends_after_a_missed_day() {
        // Active yesterday and the day before; today has nothing yet. At 9am that is
        // a live streak, not a broken one.
        let alive = stats_for(
            &document(vec![
                session(1.0, 25.0, true, Some("Maths")),
                session(2.0, 25.0, true, Some("Maths")),
            ]),
            0,
            7,
        );
        assert_eq!(alive["streak"]["current"], 2, "one grace day for today");

        // Active three and four days ago, nothing since. Today *and* yesterday are
        // empty, which is two missed days, so the run is over.
        let lost = stats_for(
            &document(vec![
                session(3.0, 25.0, true, Some("Maths")),
                session(4.0, 25.0, true, Some("Maths")),
            ]),
            0,
            7,
        );
        assert_eq!(lost["streak"]["current"], 0, "a full day missed ends it");
        assert_eq!(
            lost["streak"]["longest"], 2,
            "but the record is kept -- losing the run is not losing the history"
        );
        assert_eq!(lost["streak"]["lastActiveDate"], "2026-01-30");
    }

    #[test]
    fn a_streak_counts_today_when_today_is_active() {
        let stats = stats_for(
            &document(vec![
                session(0.0, 25.0, true, Some("Maths")),
                session(1.0, 25.0, true, Some("Maths")),
                session(2.0, 25.0, true, Some("Maths")),
            ]),
            0,
            7,
        );
        assert_eq!(stats["streak"]["current"], 3);
    }

    #[test]
    fn a_session_is_bucketed_by_the_day_it_started_not_the_day_it_ended() {
        // 23:50 on the 1st, running 25 minutes, ends 00:15 on the 2nd. Started-at
        // files it on the 1st, which is the day the person sat down.
        let late = days_from_civil(2026, 2, 1) as f64 * MS_PER_DAY + 23.0 * 3_600_000.0;
        let stats = stats_for(
            &document(vec![json!({
                "id": "late", "startedAt": late, "endedAt": late + 25.0 * 60_000.0,
                "actualMin": 25.0, "completed": true,
                "subject": "Maths", "interruptions": 0
            })]),
            0,
            7,
        );
        assert_eq!(minutes_on(&stats, "2026-02-01"), Some(25.0));
        assert_eq!(
            minutes_on(&stats, "2026-02-02"), Some(0.0),
            "bucketing by endedAt would have moved it to tomorrow and shown today empty"
        );
    }

    #[test]
    fn the_series_carries_every_day_in_the_window_including_the_empty_ones() {
        let stats = stats_for(&document(vec![session(0.0, 25.0, true, Some("Maths"))]), 0, 14);
        let rows = stats["byDate"].as_array().expect("byDate");
        assert_eq!(rows.len(), 14);
        assert!(
            rows.iter().any(|row| row["focusMinutes"] == 0.0),
            "empty days must be emitted as zero rows -- a bar chart with no position \
             for a zero renders one day's minutes as the next day's"
        );
        for pair in rows.windows(2) {
            let previous = today_index(pair[0]["date"].as_str().unwrap_or_default());
            let current = today_index(pair[1]["date"].as_str().unwrap_or_default());
            assert_eq!(current - previous, 1, "the series has a hole in it");
        }
        // It also ends on today, not on today-plus-one.
        assert_eq!(rows[rows.len() - 1]["date"], "2026-02-02");
    }

    #[test]
    fn a_window_longer_than_the_retention_policy_is_clamped_inside_the_aggregate() {
        // Not clamped by the command: `summarise` is the public entry point and a
        // guard that lives in one caller is a guard the next caller forgets.
        let stats = stats_for(&json!({}), 0, 100_000);
        assert_eq!(stats["windowDays"], 365);
        assert_eq!(stats["byDate"].as_array().expect("byDate").len(), 365);
        // And a nonsense window is one day, not zero.
        assert_eq!(stats_for(&json!({}), 0, 0)["windowDays"], 1);
        assert_eq!(stats_for(&json!({}), 0, -5)["windowDays"], 1);
    }

    #[test]
    fn a_session_outside_the_window_is_in_the_totals_but_not_the_series() {
        let stats = stats_for(
            &document(vec![
                session(0.0, 25.0, true, Some("Maths")),
                session(200.0, 50.0, true, Some("Maths")),
            ]),
            0,
            7,
        );
        assert_eq!(total(&stats, "focusMinutes"), 75.0, "it really happened");
        let series: f64 = stats["byDate"]
            .as_array()
            .expect("byDate")
            .iter()
            .filter_map(|row| row["focusMinutes"].as_f64())
            .sum();
        assert_eq!(series, 25.0, "but it does not belong in a seven-day chart");
        assert_eq!(
            stats["bySubject"].as_array().expect("bySubject").len(),
            1,
            "both sessions share a subject, so one bucket"
        );
        assert_eq!(
            stats["bySubject"][0]["focusMinutes"], 75.0,
            "the subject breakdown is deliberately NOT windowed: a donut that silently \
             excludes everything outside seven days answers 'where did my time go this \
             week', which is a different and much less useful question than the one \
             somebody looking at a subject split is actually asking"
        );
    }

    #[test]
    fn a_row_that_is_not_a_record_is_skipped_rather_than_fatal() {
        let mut doc = document(vec![session(0.0, 25.0, true, Some("Maths"))]);
        doc["sessions"]
            .as_array_mut()
            .expect("sessions")
            .push(json!("not a record"));
        assert_eq!(total(&stats_for(&doc, 0, 7), "focusMinutes"), 25.0);
    }

    #[test]
    fn a_corrupt_timestamp_does_not_poison_the_day_it_lands_in() {
        let doc = json!({
            "version": 1,
            "settings": { "dailyGoalMin": 120 },
            "sessions": [
                { "id": "neg", "startedAt": -1, "actualMin": 999.0, "completed": true },
                { "id": "text", "startedAt": "yesterday", "actualMin": 500.0, "completed": true },
                { "id": "ok", "startedAt": at(0.0), "actualMin": 25.0, "completed": true }
            ]
        });
        assert_eq!(
            total(&stats_for(&doc, 0, 7), "focusMinutes"),
            25.0,
            "only the usable row may contribute -- 999 minutes from a negative timestamp \
             would be a chart lying about a day it cannot place"
        );
        assert_eq!(total(&stats_for(&doc, 0, 7), "todayMinutes"), 25.0);
    }

    #[test]
    fn todo_completion_is_reported_as_counts_and_a_ratio() {
        let stats = stats_for(&document(vec![]), 0, 7);
        assert_eq!(stats["todoCompletion"]["total"], 4);
        assert_eq!(stats["todoCompletion"]["done"], 2);
        assert_eq!(stats["todoCompletion"]["ratio"], 0.5);
        assert_eq!(stats["noteCount"], 2);

        // No todos is a ratio of zero, not a null every consumer must special-case.
        let empty = stats_for(&json!({}), 0, 7);
        assert_eq!(empty["todoCompletion"]["ratio"], 0.0);
    }

    #[test]
    fn the_contract_declares_its_version_and_offset_so_a_reader_can_check_it() {
        let stats = stats_for(&document(vec![]), 330, 7);
        assert_eq!(stats["version"], STATS_VERSION);
        assert_eq!(stats["contract"], CONTRACT);
        assert_eq!(stats["utcOffsetMinutes"], 330);
        assert_eq!(stats["generatedAt"], T0, "the injected clock must be reported back");
    }

    /* -- the timezone offset, which is the whole reason this file takes one -- */

    #[test]
    fn the_offset_shifts_the_day_boundary_and_is_applied_east_positive() {
        let just_before = days_from_civil(2026, 2, 1) as f64 * MS_PER_DAY - 1.0;
        let just_after = days_from_civil(2026, 2, 1) as f64 * MS_PER_DAY;

        // At UTC+0 they are the 31st and the 1st.
        assert_eq!(local_day_string(local_ms(just_before, 0)), "2026-01-31");
        assert_eq!(local_day_string(local_ms(just_after, 0)), "2026-02-01");

        // At UTC+6, 23:59:59 UTC on the 31st is already the 1st locally -- which is
        // the entire reason a pomodoro at 11:40pm must not be filed under the 30th.
        assert_eq!(
            local_day_string(local_ms(just_before, 360)),
            "2026-02-01",
            "UTC+6 is six hours ahead, so the last second of UTC Jan 31 is already Feb 1 locally"
        );

        // And the same instant at UTC-5 is still the 31st.
        assert_eq!(local_day_string(local_ms(just_after, -300)), "2026-01-31");
    }

    #[test]
    fn one_session_can_land_on_different_days_at_different_offsets() {
        // 22:00 UTC on the 31st = 04:00 on the 1st at UTC+6.
        let start = days_from_civil(2026, 1, 31) as f64 * MS_PER_DAY + 22.0 * 3_600_000.0;
        let row = json!({
            "id": "evening", "startedAt": start, "endedAt": start + 25.0 * 60_000.0,
            "actualMin": 25.0, "completed": true, "subject": "Maths", "interruptions": 0
        });
        let utc_view = stats_for(&document(vec![row.clone()]), 0, 7);
        let shifted_view = stats_for(&document(vec![row]), 360, 7);

        assert_eq!(minutes_on(&utc_view, "2026-01-31"), Some(25.0));
        assert_eq!(
            minutes_on(&shifted_view, "2026-01-31"),
            Some(0.0),
            "at UTC+6 this block happened on the 1st, locally"
        );
        assert_eq!(minutes_on(&shifted_view, "2026-02-01"), Some(25.0));
        // Both agree on the lifetime total, which is offset-independent by
        // definition -- that is what makes the disagreement a labelling question
        // rather than a data-loss one.
        assert_eq!(total(&utc_view, "focusMinutes"), total(&shifted_view, "focusMinutes"));
    }

    #[test]
    fn the_civil_date_conversion_round_trips() {
        // Spot checks across a leap day and a century boundary, because a wrong leap
        // rule produces a chart that is off by one day every fourth year and never
        // looks obviously wrong.
        for (y, m, d) in [
            (1970, 1, 1),
            (2000, 2, 29),
            (2024, 2, 29),
            (2026, 2, 2),
            (2100, 3, 1),
            (1900, 3, 1),
        ] {
            assert_eq!(
                civil_from_days(days_from_civil(y, m, d)),
                (y, m, d),
                "{y}-{m:02}-{d:02} did not round trip"
            );
        }
    }

    #[test]
    fn a_malformed_date_string_is_refused_rather_than_read_as_the_epoch() {
        assert_eq!(today_index("2026-10-04"), days_from_civil(2026, 10, 4));
        // Silently becoming day 0 would put every chart in 1970.
        assert_eq!(today_index("not-a-date"), 0);
        assert_eq!(today_index(""), 0);
        assert_eq!(today_index("2026-13-01"), 0, "month 13 is not a month");
    }
}
