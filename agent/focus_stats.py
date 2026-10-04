"""Focus statistics, read from the Focus Workspace document.

## Why this file exists twice

The same aggregation is implemented in ``lumine-ui/src-tauri/src/focus_stats.rs``.
That is not carelessness. The desktop app reads the document through Rust; Lumine's
agent will read it through Python; and neither can call the other. So there are two
implementations and one **contract** -- ``focus-stats/1``, described field by field
in :mod:`FOCUS_CONTRACT` below and in
``lumine-ui/src/pages/focus/focusStatsContract.ts``.

What keeps them honest is not this docstring. It is
``lumine-ui/scripts/verify-focus-contract.mts``, which runs both against the same
fixtures and fails if any number differs. A contract nobody checks is a comment.

## The three rules that must match, or the numbers will not

1. **``utc_offset_minutes`` is east-positive.** JavaScript's
   ``getTimezoneOffset()`` returns the *negation*, so the webview passes
   ``-getTimezoneOffset()``. Python's ``datetime.astimezone()`` gives a timedelta
   whose ``.total_seconds() / 60`` is already east-positive. Getting this sign
   wrong shifts every day boundary and produces a chart that is wrong by a few
   hours rather than visibly broken.
2. **A local day is computed by shifting, not by asking a timezone library.**
   ``local_ms = utc_ms + offset * 60_000`` and then plain UTC calendar arithmetic on
   the shifted value. A local day is a UTC day plus a whole number of hours, so
   this is exact -- including across a DST change, because the offset supplied is
   the one in force at the moment of the call.
3. **A session is filed under the day it started.** Not the day it ended. A block
   that runs past midnight started on the day the person sat down.

## What this does *not* do

No tool is registered here, and nothing in this file can write the document. See
``agent/focus_store.py`` for why the read side ships first.

Usage::

    python agent/focus_stats.py --json
    python agent/focus_stats.py --days 7
    python agent/focus_stats.py --days 30 --subject Mathematics

Exit codes: ``0`` success, ``1`` the document exists but cannot be read, ``2`` the
arguments do not make sense.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # package import (repo root on sys.path)
    from .focus_store import FocusStoreError, load_focus
    from .tools.tool_results import as_payload, wrap_untrusted
except ImportError:  # running as a top-level module (agent/ on sys.path)
    from focus_store import FocusStoreError, load_focus  # type: ignore[no-redef]
    from tools.tool_results import (  # type: ignore[no-redef]
        as_payload,
        wrap_untrusted,
    )

#: Must equal ``focus_stats::STATS_VERSION``.
STATS_VERSION = 1

#: Must equal ``focus_stats::CONTRACT``.
CONTRACT = "focus-stats/1"

MS_PER_DAY = 86_400_000.0

#: Matches ``focus_store::SESSION_RETENTION_DAYS``. Asking for more days than the
#: file can hold is a mistake or an attempt to make us allocate; either way the
#: answer is the same number of rows.
MAX_WINDOW_DAYS = 365


#: The contract, written out rather than only described.
#:
#: ``scripts/verify-focus-contract.mts`` reads the keys of this dict and asserts the
#: Rust and TypeScript shapes carry the same ones, so adding a field here without
#: adding it there is a failing check rather than a silent divergence.
#:
#: Units: ``*Minutes`` are minutes (float, 2 dp). ``*Ratio`` and ``*Progress`` are
#: fractions in ``0.0..1.0`` (4 dp), never percentages. Timestamps are epoch
#: milliseconds. ``date`` fields are local ``YYYY-MM-DD`` strings.
FOCUS_CONTRACT: dict[str, str] = {
    "version": "int",
    "contract": "str",
    "generatedAt": "epoch-ms",
    "utcOffsetMinutes": "int, east-positive",
    "windowDays": "int, 1..365",
    "totals.sessions": "int, every session with a usable startedAt",
    "totals.completedSessions": "int",
    "totals.interruptedSessions": "int",
    "totals.focusMinutes": "float, every session (time sat)",
    "totals.completedFocusMinutes": "float, completed only (time finished)",
    "totals.interruptions": "int, summed",
    "totals.averageSessionMinutes": "float, focusMinutes / sessions, 0 when none",
    "totals.dailyGoalMinutes": "float, from settings.dailyGoalMin",
    "totals.todayMinutes": "float",
    "totals.goalProgress": "float 0..1, todayMinutes / dailyGoalMinutes",
    "totals.unassignedMinutes": "float, sessions with no subject",
    "today.date": "YYYY-MM-DD, local",
    "today.focusMinutes": "float",
    "today.sessions": "int",
    "streak.current": "int, consecutive days ending today, 1 day of grace",
    "streak.longest": "int, over the whole file",
    "streak.lastActiveDate": "YYYY-MM-DD or null",
    "byDate": "array of {date, focusMinutes, sessions}; one row per day in the window",
    "bySubject": "array of {subject, focusMinutes, sessions}; LIFETIME, not windowed",
    "todoCompletion.total": "int",
    "todoCompletion.done": "int",
    "todoCompletion.ratio": "float 0..1",
    "noteCount": "int",
}


def local_ms(utc_ms: float, utc_offset_minutes: int) -> float:
    """Shift a UTC timestamp into local milliseconds.

    After this, ``local_ms // 86_400_000`` is the local day index, so calendar
    arithmetic on the shifted value needs no timezone database.
    """
    return utc_ms + (float(utc_offset_minutes) * 60_000.0)


def civil_from_days(days: int) -> tuple[int, int, int]:
    """Days since the Unix epoch to a civil ``(year, month, day)``.

    Howard Hinnant's algorithm, the same one the Rust side uses. Written out rather
    than reached for through ``datetime`` because ``datetime.date.fromordinal`` has a
    different epoch and the off-by-719162 version of this bug produces a chart that
    is quietly wrong.
    """
    z = days + 719_468
    era = (z if z >= 0 else z - 146_096) // 146_097
    doe = z - era * 146_097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146_096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + 1 if m <= 2 else y, m, d)


def days_from_civil(year: int, month: int, day: int) -> int:
    """Inverse of :func:`civil_from_days`."""
    y = year - 1 if month <= 2 else year
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    mp = month - 3 if month > 2 else month + 9
    doy = (153 * mp + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146_097 + doe - 719_468


def local_day_string(shifted_ms: float) -> str:
    """``YYYY-MM-DD`` for a shifted timestamp.

    ``floor`` rather than truncation: a negative timestamp is representable in a
    corrupt document, and ``int()`` truncates toward zero, which would file 1969-12-31
    under 1970-01-01.
    """
    import math

    index = math.floor(shifted_ms / MS_PER_DAY)
    year, month, day = civil_from_days(index)
    return f"{year:04d}-{month:02d}-{day:02d}"


def today_index(date: str) -> int:
    """The day index of a ``YYYY-MM-DD`` string, or 0 if it will not parse.

    Falling back to the epoch is wrong in principle and right in practice here: a
    malformed date inside a *statistics* call is worth one wrong bucket, not a
    failed command. The parse is strict about shape so a typo cannot silently
    become "the epoch" without the ranges being checked too.
    """
    parts = date.split("-")
    if len(parts) != 3:
        return 0
    try:
        year, month, day = (int(part) for part in parts)
    except ValueError:
        return 0
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return 0
    return days_from_civil(year, month, day)


def _number(value: Any, *, minimum: float = 0.0) -> float:
    """A finite number, or ``minimum``.

    Used for every numeric field read out of the document. ``bool`` is excluded
    because Python's ``bool`` is an ``int``, and a session with
    ``"actualMin": true`` would otherwise contribute one minute rather than being
    ignored.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return minimum
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return minimum
    return number if number >= minimum else minimum


def _bucket(rows: list[dict[str, Any]], key: str, label: str, minutes: float) -> None:
    for row in rows:
        if row["subject" if key == "subject" else "date"] == label:
            row["focusMinutes"] += minutes
            row["sessions"] += 1
            return
    rows.append(
        {"subject" if key == "subject" else "date": label, "focusMinutes": minutes, "sessions": 1}
    )


def _round(value: float, places: int) -> float:
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return round(value, places)


def summarise(
    document: dict[str, Any],
    utc_offset_minutes: int = 0,
    days: int = 7,
    now: float | None = None,
) -> dict[str, Any]:
    """Aggregate a focus document into the ``focus-stats/1`` shape.

    ``now`` is an argument for the same reason it is in Rust: almost every number
    here is relative to today, so a function that reads the clock itself cannot be
    tested against a fixture without the fixture drifting underneath it.
    """
    if now is None:
        now = datetime.now(timezone.utc).timestamp() * 1000.0
    days = max(1, min(int(days), MAX_WINDOW_DAYS))

    sessions = document.get("sessions")
    sessions = sessions if isinstance(sessions, list) else []
    settings = document.get("settings")
    settings = settings if isinstance(settings, dict) else {}

    # 120 is the shipped default and it is written out rather than inherited, because
    # `focus_stats.rs` uses the same fallback. The distinction below is the whole
    # subtlety: an **absent** key takes the default, an **explicit zero** does not.
    # A goal of 0 means "I have not set one", and turning it into 120 would make
    # somebody who deliberately cleared the goal read as though they had just been
    # given two hours to do. `_number` returning 0.0 for both cases is why this is an
    # explicit membership test rather than a truthiness test.
    raw_goal = settings.get("dailyGoalMin")
    daily_goal = (
        120.0
        if "dailyGoalMin" not in settings or isinstance(raw_goal, bool) or not isinstance(raw_goal, (int, float))
        else max(0.0, _number(raw_goal))
    )

    today = local_day_string(local_ms(now, utc_offset_minutes))
    today_i = today_index(today)
    window_start = (today_i - (days - 1)) * MS_PER_DAY
    window_end = today_i * MS_PER_DAY

    by_date: list[dict[str, Any]] = []
    by_subject: list[dict[str, Any]] = []
    unassigned = 0.0
    active_days: set[str] = set()

    counted = completed_count = interrupted_count = interruptions = 0
    focus_minutes = completed_minutes = 0.0

    for row in sessions:
        if not isinstance(row, dict):
            continue
        started = row.get("startedAt")
        if isinstance(started, bool) or not isinstance(started, (int, float)):
            continue
        started = float(started)
        if started != started or started in (float("inf"), float("-inf")) or started < 0:
            continue

        minutes = _number(row.get("actualMin"))
        completed = row.get("completed") is True
        counted += 1
        focus_minutes += minutes
        interruptions += int(_number(row.get("interruptions")))
        if completed:
            completed_count += 1
            completed_minutes += minutes
        else:
            interrupted_count += 1

        shifted = local_ms(started, utc_offset_minutes)
        day = local_day_string(shifted)
        active_days.add(day)

        # Windowed, unlike the subject breakdown. A five-year-old session is still in
        # the totals but must not add a bar to a seven-day chart. A day *after* today
        # -- which a backwards clock jump produces -- is folded into today rather than
        # dropped, so the series keeps the length a chart depends on.
        if shifted >= window_start:
            _bucket(by_date, "date", today if shifted >= window_end else day, minutes)

        subject = row.get("subject")
        label = subject.strip() if isinstance(subject, str) else ""
        if label:
            _bucket(by_subject, "subject", label, minutes)
        else:
            unassigned += minutes

    # Every day in the window gets a row, including the empty ones: a bar chart with
    # no position for a zero renders one day's minutes as the next day's.
    for offset in range(days):
        day = local_day_string((today_i - (days - 1) + offset) * MS_PER_DAY)
        if not any(row["date"] == day for row in by_date):
            by_date.append({"date": day, "focusMinutes": 0.0, "sessions": 0})
    by_date.sort(key=lambda row: row["date"])
    by_subject.sort(key=lambda row: (-row["focusMinutes"], row["subject"]))

    today_row = next((row for row in by_date if row["date"] == today), None)
    today_minutes = today_row["focusMinutes"] if today_row else 0.0
    today_sessions = today_row["sessions"] if today_row else 0

    todos = document.get("todos")
    todos = todos if isinstance(todos, list) else []
    done_todos = sum(1 for todo in todos if isinstance(todo, dict) and todo.get("done") is True)

    notes = document.get("notes")
    notes = notes if isinstance(notes, list) else []

    return {
        "version": STATS_VERSION,
        "contract": CONTRACT,
        "generatedAt": now,
        "utcOffsetMinutes": utc_offset_minutes,
        "windowDays": days,
        "totals": {
            "sessions": counted,
            "completedSessions": completed_count,
            "interruptedSessions": interrupted_count,
            "focusMinutes": _round(focus_minutes, 2),
            "completedFocusMinutes": _round(completed_minutes, 2),
            "interruptions": interruptions,
            "averageSessionMinutes": _round(focus_minutes / counted, 2) if counted else 0.0,
            "dailyGoalMinutes": daily_goal,
            "todayMinutes": _round(today_minutes, 2),
            "goalProgress": _round(today_minutes / daily_goal, 4) if daily_goal > 0 else 0.0,
            "unassignedMinutes": _round(unassigned, 2),
        },
        "today": {
            "date": today,
            "focusMinutes": _round(today_minutes, 2),
            "sessions": today_sessions,
        },
        "streak": _streak(active_days, today),
        "byDate": [
            {"date": row["date"], "focusMinutes": _round(row["focusMinutes"], 2), "sessions": row["sessions"]}
            for row in by_date
        ],
        "bySubject": [
            {
                "subject": row["subject"],
                "focusMinutes": _round(row["focusMinutes"], 2),
                "sessions": row["sessions"],
            }
            for row in by_subject
        ],
        "todoCompletion": {
            "total": len(todos),
            "done": done_todos,
            "ratio": _round(done_todos / len(todos), 4) if todos else 0.0,
        },
        "noteCount": len(notes),
    }


def _streak(active_days: set[str], today: str) -> dict[str, Any]:
    """Current and longest run of consecutive active days, measured back from today.

    The one-day grace is the whole point: at 9am today is empty, and reporting zero
    for a run that is still alive would be a lie. But only one such day -- starting
    the walk at yesterday when today is empty, and stopping at the first day with no
    session, means a genuinely missed day ends the run. Counting up from the last
    active day instead would report a six-day streak forever, which is the number
    that makes somebody stop opening the app.
    """
    indices = sorted(today_index(day) for day in active_days)
    longest = run = 0
    previous: int | None = None
    for index in indices:
        run = run + 1 if previous is not None and index == previous + 1 else 1
        longest = max(longest, run)
        previous = index

    today_i = today_index(today)
    cursor = today_i if today in active_days else today_i - 1
    current = 0
    while local_day_string(cursor * MS_PER_DAY) in active_days:
        current += 1
        cursor -= 1

    return {
        "current": current,
        "longest": longest,
        "lastActiveDate": max(active_days) if active_days else None,
    }


def local_offset_minutes() -> int:
    """This machine's offset east of UTC, in minutes.

    Read once and passed in, rather than sampled per call, so every bucket in one
    report uses the same offset. Sampling per row would put a session either side of
    a DST boundary into two different days *within one response*.

    ``astimezone()`` on an aware UTC datetime already returns an east-positive
    offset, which is the sign this contract uses.
    """
    offset = datetime.now(timezone.utc).astimezone().utcoffset()
    if offset is None:  # pragma: no cover - astimezone always yields one here
        return 0
    return int(offset.total_seconds() // 60)


def build_report(
    document: dict[str, Any],
    days: int = 7,
    subject: str | None = None,
    now: float | None = None,
    utc_offset_minutes: int | None = None,
) -> dict[str, Any]:
    """Summarise, optionally narrowed to one subject.

    The narrowing is applied to the **sessions** before summarising rather than to
    the finished report, so every derived number -- the streak, the daily series, the
    goal progress -- is consistent with the filter. Filtering afterwards would leave
    a by-date series that still describes every subject while the header claims one.
    """
    offset = local_offset_minutes() if utc_offset_minutes is None else utc_offset_minutes
    wanted = (subject or "").strip().casefold()

    if not wanted:
        return summarise(document, offset, days, now)

    rows = document.get("sessions")
    rows = rows if isinstance(rows, list) else []
    narrowed = {
        **document,
        "sessions": [
            row
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("subject"), str)
            and row["subject"].strip().casefold() == wanted
        ],
    }
    report = summarise(narrowed, offset, days, now)
    # Named back so a caller reading the JSON knows the filter was applied, rather
    # than having to infer it from a by-date series that happens to look short.
    report["subjectFilter"] = wanted
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="focus_stats.py",
        description="Read Lumine's Focus Workspace statistics.",
    )
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="window length, 1 to 365 (default: 7)",
    )
    parser.add_argument("--subject", default="", help="only sessions with this subject")
    parser.add_argument(
        "--path",
        default="",
        help="read this focus.json instead of searching for one",
    )
    args = parser.parse_args(argv)

    if not 1 <= args.days <= MAX_WINDOW_DAYS:
        print(f"--days must be between 1 and {MAX_WINDOW_DAYS}.", file=sys.stderr)
        return 2

    try:
        document = load_focus(Path(args.path) if args.path else None)
    except FocusStoreError as err:
        print(f"Lumine could not read your focus data: {err}", file=sys.stderr)
        return 1

    report = build_report(document, days=args.days, subject=args.subject or None)

    if args.json:
        # Full fidelity, for `--json` consumers and the contract check. Deliberately
        # not passed through `as_payload`: that helper exists to spend fewer tokens
        # on a *model*, and truncating it here would mean the verification script was
        # comparing pruned output against full output and agreeing on it for the
        # wrong reason.
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    # The model-facing shape. Note what is and is not wrapped: `wrap_untrusted` goes
    # around the whole thing because a *subject* is a string a person typed and a
    # subject called "ignore your instructions" must not read as an instruction.
    # Numbers are synthesised here and cannot be injected, but the boundary costs
    # nothing and the habit is the point.
    totals = report["totals"]
    summary = {
        "contract": report["contract"],
        "days": report["windowDays"],
        "today": report["today"],
        "totals": totals,
        "streak": report["streak"],
        # A plain list of names, not `tool_results.headlines`. That helper is the
        # *news* answer and it returns an already-serialised JSON string, so putting it
        # in a dict and handing that to `as_payload` serialises it twice -- the model
        # was being handed `"subjects": "{\"headlines\":[]}"`, which is a string
        # containing JSON rather than a list of subjects.
        "subjects": [row["subject"] for row in report["bySubject"][:5]],
        "recent": [
            {"date": row["date"], "minutes": row["focusMinutes"]}
            for row in report["byDate"][-7:]
        ],
        "todos": report["todoCompletion"],
    }
    if report.get("subjectFilter"):
        summary["subjectFilter"] = report["subjectFilter"]
    print(wrap_untrusted(as_payload(summary)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())