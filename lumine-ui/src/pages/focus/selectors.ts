import {
  CONTRACT,
  eastPositiveOffsetMinutes,
  emptyStats,
  localDateString,
  localMs,
  MS_PER_DAY,
  STATS_VERSION,
} from "./focusStatsContract";
import type { FocusStats, StatsDay, StatsSubject } from "./focusStatsContract";
import type { FocusDocument, FocusSession } from "./types";

/**
 * Pure derivations from session history.
 *
 * ## Every statistic here comes from `sessions`, never from todos or notes
 *
 * That is not a preference. A todo renamed in March must not change what a chart
 * said about February, and a note deleted to tidy up must not erase a week of study.
 * History is a row type of its own precisely so the charts can be trusted, and any
 * selector that reached into mutable state would break that in a way nobody would
 * notice until the numbers were wrong.
 *
 * ## This is the third implementation of these numbers, and that is deliberate
 *
 * Rust (`focus_stats.rs`) serves the desktop app's derived reads, Python
 * (`focus_stats.py`) serves the agent, and this file serves the charts — because a
 * chart redrawn on every timer tick cannot afford an IPC round trip per tick, and
 * cannot afford to lag behind the widget that just banked a session either.
 *
 * The three are kept honest by `scripts/verify-focus-contract.mts`, which runs the
 * same fixtures through this file and through the other two and fails on any
 * difference. **If you change a rule here, change it there too, or the check will
 * tell you exactly which one you missed.**
 *
 * ## Local days, without a timezone library
 *
 * The same shift-then-divide trick the other two use: add the east-positive offset,
 * then do UTC calendar arithmetic on the shifted value. A local day is a UTC day plus
 * a whole number of hours, so this is exact — DST included — and it needs no
 * `Intl`, no locale, and nothing that differs between machines.
 */

/** A session that survived being read out of the document. */
export type UsableSession = FocusSession & { startedAt: number; actualMin: number };

/**
 * Whether a session can be placed in time at all.
 *
 * Every rule in this file that touches `startedAt` goes through here first. The two
 * rejections are the interesting ones: a **future** timestamp is what a backwards
 * clock jump produces, and "older than a year" is false for it, so it must not be
 * pruned by a retention rule; and a **non-finite or negative** value is what a
 * corrupt write produces, and it would otherwise file itself under 1970 and sit in
 * every chart as a permanent outlier.
 */
export function isUsableSession(row: unknown): row is UsableSession {
  if (!row || typeof row !== "object") return false;
  const entry = row as Partial<FocusSession>;
  const started = entry.startedAt;
  if (typeof started !== "number" || !Number.isFinite(started) || started < 0) return false;
  const minutes = entry.actualMin;
  // `bool` is not a `number` in TypeScript's eyes but it *is* at runtime, so the
  // runtime guard is what matters here and `typeof` is exactly it.
  if (minutes !== undefined && (typeof minutes !== "number" || !Number.isFinite(minutes))) {
    return false;
  }
  return true;
}

/** Every session that can be placed, in document order. */
export function usableSessions(document: FocusDocument): UsableSession[] {
  return (Array.isArray(document.sessions) ? document.sessions : []).filter(isUsableSession);
}

/** The day index of a local `YYYY-MM-DD`, used for streak arithmetic. */
function dayIndex(date: string): number {
  const parts = date.split("-");
  if (parts.length !== 3) return 0;
  const year = Number(parts[0]);
  const month = Number(parts[1]);
  const day = Number(parts[2]);
  if (!Number.isInteger(year) || !Number.isInteger(month) || !Number.isInteger(day)) return 0;
  if (month < 1 || month > 12 || day < 1 || day > 31) return 0;
  // Date.UTC is the inverse of `localDateString`, so this round-trips exactly.
  return Date.UTC(year, month - 1, day) / MS_PER_DAY;
}

/** The `YYYY-MM-DD` of a local day index. The inverse of `dayIndex`. */
function dateFromIndex(index: number, offsetMinutes: number): string {
  return localDateString(index * MS_PER_DAY - offsetMinutes * 60_000, offsetMinutes);
}

export function round2(value: number): number {
  return Number.isFinite(value) ? Math.round(value * 100) / 100 : 0;
}

export function round4(value: number): number {
  return Number.isFinite(value) ? Math.round(value * 10_000) / 10_000 : 0;
}

/**
 * The daily series, one row per day in the window **including the empty ones**.
 *
 * The empties are load-bearing. A bar chart has no axis position for a day that is
 * not in the data, so omitting a zero renders Tuesday's 45 minutes as Wednesday's
 * — which is the kind of chart that is confidently wrong.
 *
 * A session whose local day is *after* today is folded into today rather than
 * dropped, so the series always has exactly `days` rows. That case is a clock change,
 * and losing a real session to one would be worse than filing it under today.
 */
export function dailySeries(
  sessions: UsableSession[],
  days: number,
  offsetMinutes: number,
  now: number,
): StatsDay[] {
  const window = Math.max(1, Math.min(Math.trunc(days), 365));
  const today = localDateString(now, offsetMinutes);
  const todayIndex = dayIndex(today);
  const windowStart = (todayIndex - (window - 1)) * MS_PER_DAY;

  const rows = new Map<string, StatsDay>();
  for (let step = 0; step < window; step += 1) {
    const date = dateFromIndex(todayIndex - (window - 1) + step, offsetMinutes);
    rows.set(date, { date, focusMinutes: 0, sessions: 0 });
  }

  for (const session of sessions) {
    const shifted = localMs(session.startedAt, offsetMinutes);
    if (shifted < windowStart) continue;
    const day = localDateString(session.startedAt, offsetMinutes);
    const date = day > today ? today : day;
    const row = rows.get(date) ?? { date, focusMinutes: 0, sessions: 0 };
    row.focusMinutes += Math.max(0, session.actualMin);
    row.sessions += 1;
    rows.set(date, row);
  }

  return [...rows.values()]
    .map((row) => ({ ...row, focusMinutes: round2(row.focusMinutes) }))
    .sort((left, right) => left.date.localeCompare(right.date));
}

/**
 * The subject breakdown. **Lifetime, not windowed.**
 *
 * Deliberate: a donut that silently excludes everything outside seven days answers
 * "where did my time go this week", which is a different and much less useful question
 * than the one somebody looking at a subject split is asking. Days with no subject
 * are counted in `unassignedMinutes` rather than dropped — they happened.
 */
export function subjectTotals(sessions: UsableSession[]): {
  subjects: StatsSubject[];
  unassignedMinutes: number;
} {
  const rows = new Map<string, StatsSubject>();
  let unassignedMinutes = 0;

  for (const session of sessions) {
    const subject = typeof session.subject === "string" ? session.subject.trim() : "";
    const minutes = Math.max(0, session.actualMin);
    if (!subject) {
      unassignedMinutes += minutes;
      continue;
    }
    const row = rows.get(subject) ?? { subject, focusMinutes: 0, sessions: 0 };
    row.focusMinutes += minutes;
    row.sessions += 1;
    rows.set(subject, row);
  }

  const subjects = [...rows.values()]
    .map((row) => ({ ...row, focusMinutes: round2(row.focusMinutes) }))
    // Longest first, then alphabetical, so the order is stable between renders --
    // a donut whose slices reshuffle on every tick is unreadable.
    .sort((left, right) => right.focusMinutes - left.focusMinutes || left.subject.localeCompare(right.subject));

  return { subjects, unassignedMinutes: round2(unassignedMinutes) };
}

/**
 * Current and longest run of consecutive active days, measured back from today.
 *
 * The one-day grace is the point: at 9am today is empty, and reporting zero for a run
 * that is still alive would be a lie. But only one such day — a genuinely missed day
 * ends the run. Counting up from the *last* active day instead would report a
 * six-day streak forever, which is the number that makes somebody stop opening the
 * app.
 */
export function streakOf(sessions: UsableSession[], offsetMinutes: number, now: number) {
  const days = new Set(sessions.map((session) => localDateString(session.startedAt, offsetMinutes)));
  const today = localDateString(now, offsetMinutes);
  const todayIndex = dayIndex(today);

  let cursor = days.has(today) ? todayIndex : todayIndex - 1;
  let current = 0;
  while (days.has(dateFromIndex(cursor, offsetMinutes))) {
    current += 1;
    cursor -= 1;
  }

  const indices = [...days].map(dayIndex).sort((left, right) => left - right);
  let longest = 0;
  let run = 0;
  let previous: number | null = null;
  for (const index of indices) {
    run = previous !== null && index === previous + 1 ? run + 1 : 1;
    longest = Math.max(longest, run);
    previous = index;
  }

  const sorted = [...days].sort();
  return {
    current,
    longest,
    lastActiveDate: sorted.length > 0 ? sorted[sorted.length - 1] : null,
  };
}

/**
 * A rolling average, for the trend chart's second line.
 *
 * A trailing window including the current day, and a shorter window returns fewer
 * points rather than dividing by zero — three days of data must not produce three
 * `NaN` values on a chart.
 */
export function rollingAverage(values: number[], window: number): number[] {
  const size = Math.max(1, Math.trunc(window));
  return values.map((_, index) => {
    const start = Math.max(0, index - size + 1);
    const slice = values.slice(start, index + 1);
    const total = slice.reduce((sum, value) => sum + value, 0);
    return round2(total / slice.length);
  });
}

/**
 * The whole `focus-stats/1` report, from a document and a clock.
 *
 * `now` and `offsetMinutes` are parameters rather than reads so a test can pin them,
 * and so one report never mixes two offsets.
 */
export function statsFor(
  document: FocusDocument,
  days = 7,
  now: number = Date.now(),
  offsetMinutes: number = eastPositiveOffsetMinutes(),
): FocusStats {
  const sessions = usableSessions(document);
  const series = dailySeries(sessions, days, offsetMinutes, now);
  const { subjects, unassignedMinutes } = subjectTotals(sessions);
  const today = localDateString(now, offsetMinutes);
  const todayRow = series.find((row) => row.date === today);

  const settings = document.settings;
  // An **absent** goal takes the shipped default; an explicit `0` does not. A goal of
  // zero means "not set", and turning it into 120 would report a person who cleared
  // their goal as having just been given two hours to do.
  const rawGoal = settings?.dailyGoalMin;
  const dailyGoalMinutes =
    typeof rawGoal === "number" && Number.isFinite(rawGoal) ? Math.max(0, rawGoal) : 120;

  const todayMinutes = todayRow?.focusMinutes ?? 0;
  const completedFocusMinutes = sessions
    .filter((session) => session.completed)
    .reduce((sum, session) => sum + Math.max(0, session.actualMin), 0);
  const focusMinutes = sessions.reduce((sum, session) => sum + Math.max(0, session.actualMin), 0);

  const todos = Array.isArray(document.todos) ? document.todos : [];
  const done = todos.filter((todo) => todo?.done === true).length;

  return {
    version: STATS_VERSION,
    contract: CONTRACT,
    generatedAt: now,
    utcOffsetMinutes: offsetMinutes,
    windowDays: Math.max(1, Math.min(Math.trunc(days), 365)),
    totals: {
      sessions: sessions.length,
      completedSessions: sessions.filter((session) => session.completed).length,
      interruptedSessions: sessions.filter((session) => !session.completed).length,
      focusMinutes: round2(focusMinutes),
      completedFocusMinutes: round2(completedFocusMinutes),
      interruptions: sessions.reduce(
        (sum, session) => sum + (Number.isFinite(session.interruptions) ? Math.max(0, session.interruptions) : 0),
        0,
      ),
      averageSessionMinutes: sessions.length === 0 ? 0 : round2(focusMinutes / sessions.length),
      dailyGoalMinutes,
      todayMinutes,
      goalProgress: dailyGoalMinutes > 0 ? round4(todayMinutes / dailyGoalMinutes) : 0,
      unassignedMinutes,
    },
    today: { date: today, focusMinutes: todayMinutes, sessions: todayRow?.sessions ?? 0 },
    streak: streakOf(sessions, offsetMinutes, now),
    byDate: series,
    bySubject: subjects,
    todoCompletion: {
      total: todos.length,
      done,
      ratio: todos.length === 0 ? 0 : round4(done / todos.length),
    },
    noteCount: Array.isArray(document.notes) ? document.notes.length : 0,
  };
}

/** The empty report, for before the document has loaded. */
export function statsForEmpty(days = 7): FocusStats {
  return { ...emptyStats(eastPositiveOffsetMinutes()), windowDays: Math.max(1, Math.min(days, 365)) };
}

/**
 * Subject suggestions for the session-context picker.
 *
 * Derived from history rather than from a separate saved list, because a list the
 * person has to maintain is a list that goes stale, and the subjects they have
 * actually studied are the useful suggestions. Most-recent-first, then most-used,
 * and capped — an unbounded picker is a wall of cards.
 */
export function subjectSuggestions(document: FocusDocument, limit = 6): string[] {
  const counts = new Map<string, { count: number; lastUsed: number }>();
  for (const session of usableSessions(document)) {
    const subject = typeof session.subject === "string" ? session.subject.trim() : "";
    if (!subject) continue;
    const entry = counts.get(subject) ?? { count: 0, lastUsed: 0 };
    entry.count += 1;
    entry.lastUsed = Math.max(entry.lastUsed, session.startedAt);
    counts.set(subject, entry);
  }
  return [...counts.entries()]
    .sort((left, right) => right[1].lastUsed - left[1].lastUsed || right[1].count - left[1].count)
    .slice(0, limit)
    .map(([subject]) => subject);
}

/** A human duration, for a stat tile. `90` becomes `1h 30m`. */
export function formatMinutes(minutes: number): string {
  if (!Number.isFinite(minutes) || minutes <= 0) return "0m";
  const whole = Math.round(minutes);
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  if (hours === 0) return `${rest}m`;
  return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`;
}