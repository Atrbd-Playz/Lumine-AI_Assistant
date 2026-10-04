/**
 * The `focus-stats/1` contract, in one place.
 *
 * ## What this file is for
 *
 * Three runtimes compute focus statistics: Rust (for the desktop app), Python (for
 * Lumine's agent), and TypeScript (for the charts, in `selectors.ts`). A statistic
 * that reads "3h 15m" in one and "3h 25m" in another is not a rounding difference —
 * it is a chart a person acts on being wrong, with no way for them to tell.
 *
 * So the *field names, types and units* live here, are mirrored in
 * `focus_stats.rs` (`FOCUS_CONTRACT`'s Rust sibling) and `focus_stats.py`
 * (`FOCUS_CONTRACT`), and are **checked** by `scripts/verify-focus-contract.mts`.
 * A contract nobody checks is a comment; this one fails the build.
 *
 * ## The four rules that make the numbers agree
 *
 * 1. **`utcOffsetMinutes` is east-positive.** JavaScript's `getTimezoneOffset()`
 *    returns the *negation*, so the webview passes `-getTimezoneOffset()`. Getting
 *    this backwards shifts every day boundary, which produces a chart that is wrong
 *    by a few hours rather than visibly broken — the worst kind of wrong.
 * 2. **A local day is computed by shifting, not by asking a timezone database.**
 *    `localMs = utcMs + offset * 60_000`, then plain UTC calendar arithmetic on the
 *    shifted value. A local day is a UTC day plus a whole number of hours, so this
 *    is exact, DST included, because the offset is the one in force right now.
 * 3. **A session is filed under the day it *started*.** A block running past
 *    midnight started on the day the person sat down.
 * 4. **Minutes are minutes.** Never seconds, never milliseconds. A field ending in
 *    `Minutes` is a float rounded to 2 dp; a ratio or progress field is `0.0..1.0`
 *    rounded to 4 dp and is **never** a percentage.
 */

/** Bumped when a field changes meaning. Mirrors `focus_stats::STATS_VERSION`. */
export const STATS_VERSION = 1;

/** The contract identifier. Mirrors `focus_stats::CONTRACT`. */
export const CONTRACT = "focus-stats/1";

/** 86_400_000. Named so the calendar arithmetic below reads as arithmetic. */
export const MS_PER_DAY = 86_400_000;

/**
 * One row of the daily series.
 *
 * `focusMinutes` is `0` for a day with no sessions, and the row is **present**.
 * That is not laziness: a bar chart with no axis position for a zero renders
 * Tuesday's 45 minutes as Wednesday's, so the empty days have to be emitted.
 */
export type StatsDay = {
  date: string;
  focusMinutes: number;
  sessions: number;
};

/** One row of the subject breakdown. Deliberately *lifetime*, not windowed. */
export type StatsSubject = {
  subject: string;
  focusMinutes: number;
  sessions: number;
};

/**
 * The report, as every implementation emits it.
 *
 * `subjectFilter` is present only when the caller asked for one subject. It is
 * declared here as optional rather than nullable because "no filter was applied" and
 * "a filter was applied and matched nothing" are different facts, and a report that
 * cannot tell them apart is a report that shows zero and looks like a bad day.
 */
export type FocusStats = {
  version: number;
  contract: string;
  /** Epoch millis, `f64` on the wire. The clock that produced this report. */
  generatedAt: number;
  utcOffsetMinutes: number;
  /** 1..365. Clamped inside every implementation, not only by the caller. */
  windowDays: number;
  totals: {
    /** Every session with a usable `startedAt`. */
    sessions: number;
    completedSessions: number;
    interruptedSessions: number;
    /** Time *sat*. Includes interrupted sessions. */
    focusMinutes: number;
    /** Time *finished*. Completed sessions only. */
    completedFocusMinutes: number;
    interruptions: number;
    /** `focusMinutes / sessions`, and `0` when there are none — never `null`. */
    averageSessionMinutes: number;
    dailyGoalMinutes: number;
    todayMinutes: number;
    /** `todayMinutes / dailyGoalMinutes` as `0.0..1.0`. `0` when the goal is `0`. */
    goalProgress: number;
    /** Minutes from sessions with no subject. Counted, never dropped. */
    unassignedMinutes: number;
  };
  today: { date: string; focusMinutes: number; sessions: number };
  streak: {
    /** Consecutive active days ending today, with one day of grace for today. */
    current: number;
    /** Over the whole file. Losing a run must not lose the record. */
    longest: number;
    lastActiveDate: string | null;
  };
  byDate: StatsDay[];
  bySubject: StatsSubject[];
  todoCompletion: { total: number; done: number; ratio: number };
  noteCount: number;
  subjectFilter?: string;
};

/**
 * Every contracted path and its declared type.
 *
 * This table is the executable half of the contract. `verify-focus-contract.mts`
 * asserts three things with it: that a real report carries every path, that the
 * Python `FOCUS_CONTRACT` dictionary has the same key set, and that the fixture
 * values the three implementations produce for the same input are identical.
 *
 * `nullable: true` means the field may legitimately be `null` — currently only
 * `streak.lastActiveDate`, which is null precisely when nothing has ever been
 * studied, and coercing that to the epoch would file it in 1970.
 */
export const FOCUS_CONTRACT: Record<string, { type: string; nullable?: boolean }> = {
  version: { type: "number" },
  contract: { type: "string" },
  generatedAt: { type: "number" },
  utcOffsetMinutes: { type: "number" },
  windowDays: { type: "number" },
  "totals.sessions": { type: "number" },
  "totals.completedSessions": { type: "number" },
  "totals.interruptedSessions": { type: "number" },
  "totals.focusMinutes": { type: "number" },
  "totals.completedFocusMinutes": { type: "number" },
  "totals.interruptions": { type: "number" },
  "totals.averageSessionMinutes": { type: "number" },
  "totals.dailyGoalMinutes": { type: "number" },
  "totals.todayMinutes": { type: "number" },
  "totals.goalProgress": { type: "number" },
  "totals.unassignedMinutes": { type: "number" },
  "today.date": { type: "string" },
  "today.focusMinutes": { type: "number" },
  "today.sessions": { type: "number" },
  "streak.current": { type: "number" },
  "streak.longest": { type: "number" },
  "streak.lastActiveDate": { type: "string", nullable: true },
  byDate: { type: "array" },
  bySubject: { type: "array" },
  "todoCompletion.total": { type: "number" },
  "todoCompletion.done": { type: "number" },
  "todoCompletion.ratio": { type: "number" },
  noteCount: { type: "number" },
};

/** Read a dotted path out of a report. Returns undefined rather than throwing. */
export function readPath(source: unknown, path: string): unknown {
  let cursor: unknown = source;
  for (const key of path.split(".")) {
    if (cursor === null || typeof cursor !== "object") return undefined;
    cursor = (cursor as Record<string, unknown>)[key];
  }
  return cursor;
}

/**
 * This machine's offset east of UTC, in minutes.
 *
 * The negation is the trap and it is why this function exists rather than the call
 * sites each writing `-new Date().getTimezoneOffset()`. A caller that reads
 * `-new Date().getTimezoneOffset()` is doing arithmetic on a sign convention every
 * time; a caller that reads `eastPositiveOffsetMinutes()` is not.
 */
export function eastPositiveOffsetMinutes(at: Date = new Date()): number {
  // `getTimezoneOffset` is minutes to *add* to local time to reach UTC, so UTC+6
  // returns -360. Negating gives the east-positive value the contract uses.
  return -at.getTimezoneOffset();
}

/**
 * Shift a UTC timestamp into local milliseconds.
 *
 * After this, `localMs / MS_PER_DAY` is the local day index, so calendar arithmetic
 * on the shifted value needs no timezone database. Exported because
 * `verify-focus-contract.mts` checks the TypeScript implementation against the Rust
 * and Python ones, and three copies of "shift then divide" is exactly the arithmetic
 * that drifts.
 */
export function localMs(utcMs: number, utcOffsetMinutes: number): number {
  return utcMs + utcOffsetMinutes * 60_000;
}

/**
 * The local `YYYY-MM-DD` for a timestamp, given an explicit offset.
 *
 * The offset is a parameter rather than read from the environment so a test can
 * place a session either side of a day boundary and check both answers.
 */
export function localDateString(utcMs: number, utcOffsetMinutes: number): string {
  const shifted = localMs(utcMs, utcOffsetMinutes);
  const date = new Date(shifted);
  // `getUTC*` on the shifted value is UTC calendar arithmetic on local time, which is
  // the whole trick: no `Intl`, no locale, no timezone database, and identical on
  // every machine.
  const year = date.getUTCFullYear();
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  const day = String(date.getUTCDate()).padStart(2, "0");
  return `${String(year).padStart(4, "0")}-${month}-${day}`;
}

/** Today's local date, as `YYYY-MM-DD`. */
export function todayString(utcOffsetMinutes: number = eastPositiveOffsetMinutes()): string {
  return localDateString(Date.now(), utcOffsetMinutes);
}

/**
 * A report with every field present and every number zero.
 *
 * What a chart renders before the document has loaded. Returning a *shaped* empty
 * report rather than `null` means a chart never has to handle "no data yet" as a
 * separate case from "no sessions", and the empty state it draws is the same one it
 * draws for a person who genuinely has not studied — which is the honest answer in
 * both cases.
 */
export function emptyStats(utcOffsetMinutes = 0): FocusStats {
  return {
    version: STATS_VERSION,
    contract: CONTRACT,
    generatedAt: 0,
    utcOffsetMinutes,
    windowDays: 7,
    totals: {
      sessions: 0,
      completedSessions: 0,
      interruptedSessions: 0,
      focusMinutes: 0,
      completedFocusMinutes: 0,
      interruptions: 0,
      averageSessionMinutes: 0,
      // 120, not 0: matching the shipped default means an empty report's goal ring
      // is empty rather than full, which is the difference between "you have not
      // started" and "you have exceeded your goal by an infinite amount".
      dailyGoalMinutes: 120,
      todayMinutes: 0,
      goalProgress: 0,
      unassignedMinutes: 0,
    },
    today: { date: todayString(utcOffsetMinutes), focusMinutes: 0, sessions: 0 },
    streak: { current: 0, longest: 0, lastActiveDate: null },
    byDate: [],
    bySubject: [],
    todoCompletion: { total: 0, done: 0, ratio: 0 },
    noteCount: 0,
  };
}