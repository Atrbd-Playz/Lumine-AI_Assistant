// Do the three focus-statistics implementations actually agree?
//
// There are three of them, and there have to be: Rust serves the desktop app's
// derived reads, Python serves Lumine's agent, and TypeScript draws the charts —
// because a chart redrawn on every timer tick cannot afford an IPC round trip. Two
// implementations of "focus minutes per day" is a documentation problem; three is
// only survivable because something checks them.
//
// This is that something. It runs the *same* fixtures through all three and fails
// on any difference. It also compares the field sets, so a field added to one
// implementation and not another is a red check rather than a chart that quietly
// renders `undefined` for a month.
//
// What it deliberately does NOT do is judge the numbers in isolation. A rule all
// three share can be wrong in all three — filing a session under the day it ended
// would pass this script. Those cases are pinned in the Rust and Python unit suites,
// which have defensible right answers. This script answers the narrower and more
// mechanical question: do the three say the same thing?
//
// It runs as a script rather than under a test runner because this repository has no
// frontend test runner, and the five checks that do exist already use this shape.
// See `verify-command-surface.mts` for the pattern, including why type imports need
// their own `import` statement.
import { register } from "node:module";
import { execFileSync } from "node:child_process";
import { existsSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
// A separate `import type` statement, not an inline `type` modifier: Node's type
// stripping erases the statement but does not rewrite inline modifiers, so
// `import { x, type Y } from "..."` is a syntax error under
// `--experimental-strip-types`.
import type { FocusStats } from "../src/pages/focus/focusStatsContract.ts";

register("./ts-extension-resolver.mjs", import.meta.url);

const { FOCUS_CONTRACT, emptyStats, localDateString, CONTRACT, STATS_VERSION } = await import(
  "../src/pages/focus/focusStatsContract.ts"
);
const { statsFor, dailySeries, subjectTotals, streakOf, rollingAverage } = await import(
  "../src/pages/focus/selectors.ts"
);

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..");

let failures = 0;
function check(what: string, ok: boolean, detail?: unknown) {
  if (ok) {
    console.log(`  ok    ${what}`);
  } else {
    failures += 1;
    console.log(`  FAIL  ${what}${detail === undefined ? "" : `\n        ${JSON.stringify(detail)}`}`);
  }
}

/* ---------------------------------------------------------------------------
 * Fixtures
 *
 * The same instant the Rust and Python suites use: 2026-02-02T09:13:20Z. Shared on
 * purpose — a fixture only compares across implementations if it means the same
 * day to all three, and "today" is relative.
 * ------------------------------------------------------------------------ */

const T0 = 1_770_000_000_000;
const MS_PER_DAY = 86_400_000;

type Row = Record<string, unknown>;
const session = (daysAgo: number, minutes: number, completed: boolean, subject: string | null): Row => ({
  id: `s${daysAgo}-${minutes}`,
  startedAt: T0 - daysAgo * MS_PER_DAY,
  endedAt: T0 - daysAgo * MS_PER_DAY + minutes * 60_000,
  plannedMin: 25,
  actualMin: minutes,
  phase: "work",
  subject,
  todoId: null,
  noteId: null,
  completed,
  interruptions: 0,
});

const todos = [
  { id: "t1", done: true },
  { id: "t2", done: false },
  { id: "t3", done: false },
  { id: "t4", done: true },
];

type Fixture = { sessions: Row[]; days: number; offset: number; cliRejected?: string };
const fixtures: Record<string, Fixture> = {
  "no sessions at all": { sessions: [], days: 7, offset: 0 },
  "one completed session": { sessions: [session(0, 25, true, "Maths")], days: 7, offset: 0 },
  "several sessions on one day": {
    sessions: [session(0, 25, true, "Maths"), session(0, 10, false, "Maths")],
    days: 7,
    offset: 0,
  },
  "a run of consecutive days": {
    sessions: [session(0, 25, true, "A"), session(1, 25, true, "A"), session(2, 25, true, "B")],
    days: 7,
    offset: 0,
  },
  "a streak broken by a missed day": {
    sessions: [session(3, 25, true, "A"), session(4, 25, true, "A")],
    days: 7,
    offset: 0,
  },
  "sessions with no subject": {
    sessions: [session(0, 25, true, null), session(0, 25, true, "   ")],
    days: 7,
    offset: 0,
  },
  "every session interrupted": {
    sessions: [session(0, 5, false, "A"), session(1, 5, false, "A")],
    days: 7,
    offset: 0,
  },
  "a session outside the window": {
    sessions: [session(0, 25, true, "A"), session(200, 50, true, "A")],
    days: 7,
    offset: 0,
  },
  "a 14-day window": {
    sessions: [session(0, 25, true, "A"), session(13, 45, true, "B")],
    days: 14,
    offset: 0,
  },
  "a session before the epoch and one with no timestamp": {
    sessions: [
      { id: "neg", startedAt: -1, actualMin: 999, completed: true },
      { id: "text", startedAt: "yesterday", actualMin: 500, completed: true },
      session(0, 25, true, "A"),
    ],
    days: 7,
    offset: 0,
  },
  // Two cases the CLI refuses on purpose, so they cannot be compared through it and
  // are asserted directly below instead. `--days 400` is rejected with exit code 2
  // by the argument check, which is the *right* behaviour: a person typing it gets
  // told rather than being handed a 400-row chart. The clamp underneath it is what
  // matters, and `summarise` applies it regardless of who asked.
  "an absurd window is refused by the CLI": {
    sessions: [],
    days: 400,
    offset: 0,
    cliRejected: "an out-of-range window is a usage error, not a chart",
  },
};

function documentFor(name: string): Row {
  const fixture = fixtures[name];
  return {
    version: 1,
    settings: { dailyGoalMin: 120 },
    todos,
    notes: [{ id: "n1" }, { id: "n2" }],
    sessions: fixture.sessions,
  };
}

/** The TypeScript implementation, via the store's own selector. */
function typescriptStats(name: string): FocusStats {
  const fixture = fixtures[name];
  return statsFor(documentFor(name) as never, fixture.days, T0, fixture.offset);
}

/* ---------------------------------------------------------------------------
 * 1. The TypeScript report satisfies the contract it declares
 * ------------------------------------------------------------------------ */

console.log("\nthe declared contract, against a report the selectors produce");

const sample = typescriptStats("several sessions on one day");
for (const path of Object.keys(FOCUS_CONTRACT)) {
  const head = path.split(".")[0];
  const cursor = sample as unknown as Record<string, unknown>;
  const value = path.includes(".") ? (cursor[head] as Record<string, unknown>)[path.split(".")[1]] : cursor[head];
  const declared = FOCUS_CONTRACT[path];
  // `typeof []` is `"object"`, so an array contract has to be checked with
  // Array.isArray. Getting this wrong would make every series field look present but
  // mistyped -- and the mistyped one is the field a chart reads.
  const matches =
    declared.type === "array" ? Array.isArray(value) : typeof value === declared.type;
  const ok = value !== undefined && (declared.nullable === true || matches);
  check(`contract field ${path} is present and a ${declared.type}`, ok, value);
}

check("the contract version matches", sample.version === STATS_VERSION);
check("the contract name matches", sample.contract === CONTRACT);

/* ---------------------------------------------------------------------------
 * 2. The Python implementation, run as a subprocess
 *
 * A subprocess rather than an import because `focus_stats.py` is a CLI and importing
 * it here would put the repository's Python on Node's import path — which works
 * right up until a path separator or a venv difference makes it not. If the
 * interpreter is missing, the check is *skipped with a message*, never silently
 * passed: a contract check that reports success when it ran nothing is the worst
 * possible failure mode for a file whose whole job is to catch silent divergence.
 * ------------------------------------------------------------------------ */

console.log("\nPython, against the same fixtures");

function pythonAvailable(): { ok: boolean; python: string } {
  for (const candidate of [join(repoRoot, ".venv", "Scripts", "python.exe"), "python"]) {
    try {
      execFileSync(candidate, ["--version"], { stdio: "ignore" });
      return { ok: true, python: candidate };
    } catch {
      // Try the next candidate.
    }
  }
  return { ok: false, python: "" };
}

const python = pythonAvailable();
if (!python.ok) {
  console.log("  skip  no Python interpreter found; the Rust suite still pins Python's rules");
} else {
  const focusStats = join(repoRoot, "agent", "focus_stats.py");
  check("agent/focus_stats.py exists", existsSync(focusStats));

  /** Run one fixture through the CLI with an injected clock. */
  function pythonStats(name: string): Record<string, unknown> | null {
    const fixture = fixtures[name];
    // `--path` is pointed at a temp file rather than the real document, so this check
    // can never read -- or be affected by -- somebody's actual study history.
    const temp = join(repoRoot, ".focus-contract-fixture.json");
    writeFileSync(temp, JSON.stringify(documentFor(name)), "utf8");
    try {
      const stdout = execFileSync(
        python.python,
        [
          focusStats,
          "--json",
          "--days",
          String(fixture.days),
          "--path",
          temp,
          // The CLI reads the machine's real offset; the fixture pins its own, and
          // the offset is asserted separately below rather than through this path.
        ],
        { encoding: "utf8", env: { ...process.env, LUMINE_FOCUS_PATH: temp } },
      );
      return JSON.parse(stdout) as Record<string, unknown>;
    } catch {
      return null;
    } finally {
      try {
        unlinkSync(temp);
      } catch {
        // A leftover temp file is not worth failing a check over.
      }
    }
  }

  /** The exit code the CLI gives a fixture, for the refusal assertions. */
  function pythonExitCode(name: string): number | null {
    const temp = join(repoRoot, ".focus-contract-fixture.json");
    writeFileSync(temp, JSON.stringify(documentFor(name)), "utf8");
    try {
      execFileSync(
        python.python,
        [focusStats, "--json", "--days", String(fixtures[name].days), "--path", temp],
        { encoding: "utf8", env: { ...process.env, LUMINE_FOCUS_PATH: temp }, stdio: "pipe" },
      );
      return 0;
    } catch (error) {
      const status = (error as { status?: number }).status;
      return typeof status === "number" ? status : null;
    } finally {
      try {
        unlinkSync(temp);
      } catch {
        // A leftover temp file is not worth failing a check over.
      }
    }
  }

  // Every field except `generatedAt`, which is an *input* to the comparison rather
  // than a result: the CLI cannot be told what "now" is, so it uses the real clock.
  //
  // That first mismatch was not a bug in either implementation — it was this script
  // comparing a report computed for February against one computed for October, and
  // then reading the 24,000 differing fields as a contract violation. The fix is
  // below, where both sides are anchored to one clock instead of the differing fields
  // being excused.
  const IGNORED: Record<string, true> = { generatedAt: true };

  // Anchor both implementations to the same instant.
  //
  // Python's CLI reads the system clock and its own timezone; TypeScript's selectors
  // take both as parameters. Rather than adding a `--now` flag to a shipped script
  // purely so a test can drive it, this asks Python what clock it used -- it reports
  // `generatedAt` and `utcOffsetMinutes` -- and then runs the TypeScript side with
  // exactly those. Both compute "today" from the same pair, so the comparison is
  // about the *rules* rather than about which day it happened to be.
  const anchor = pythonStats("no sessions at all");
  if (!anchor) {
    check("python produced an anchor report", false);
  } else {
    check(
      "python reports the clock it used, so both sides can be anchored to it",
      typeof anchor.generatedAt === "number" && typeof anchor.utcOffsetMinutes === "number",
      { generatedAt: anchor.generatedAt, utcOffsetMinutes: anchor.utcOffsetMinutes },
    );
  }

  const anchored = anchor
    ? { now: Number(anchor.generatedAt), offset: Number(anchor.utcOffsetMinutes) }
    : null;

  for (const name of Object.keys(fixtures)) {
    const fixture = fixtures[name];
    const rust = fixture.cliRejected ? null : pythonStats(name);
    if (fixture.cliRejected) {
      // Asserted as a refusal rather than compared, because a refusal is the
      // behaviour. The CLI returns 2 for an out-of-range window, so a chart can never
      // be asked for a year of days by a mistyped number.
      const refused = pythonExitCode(name);
      check(
        `${name}: the CLI refuses it with exit code 2 (${fixture.cliRejected})`,
        refused === 2,
        refused,
      );
      check(
        `${name}: the aggregate clamps it anyway when called directly`,
        statsFor(documentFor(name) as never, fixture.days, T0, 0).windowDays === 365,
        statsFor(documentFor(name) as never, fixture.days, T0, 0).windowDays,
      );
      continue;
    }
    if (!rust || !anchored) {
      check(`${name}: python produced a report`, false);
      continue;
    }
    // The fixture's own offset is not reachable from the CLI, so the fixture is
    // always evaluated at *this machine's* offset. The two offset-sensitive rules are
    // asserted directly against the pure functions above and in the Rust/Python unit
    // suites, where the offset can be pinned.
    const ts = statsFor(
      documentFor(name) as never,
      fixtures[name].days,
      anchored.now,
      anchored.offset,
    );
    const differences = diffReports(ts as unknown as Record<string, unknown>, rust, IGNORED);
    check(
      `${name}: python and typescript agree`,
      differences.length === 0,
      differences.length > 0 ? differences.slice(0, 6) : undefined,
    );
  }

  // The one property the offset *does* have to agree on: which day a session lands
  // in. A CLI that cannot be told the offset still has to produce the same shape, so
  // the day-bucketing rule is asserted through the pure functions the CLI and the
  // chart both call.
  console.log("\nthe day-bucketing rule, which is where a timezone sign error would hide");

  // Jan 31 at 22:00 UTC. **January**, deliberately: `Date.UTC(2026, 1, 31, ...)` is
  // the 31st of February, which does not exist, so the Date constructor rolls it to
  // March 3 -- and the fixture silently became a check about March rather than about
  // a day boundary. A calendar bug hiding inside a calendar test is the worst place
  // to find one.
  const evening = Date.UTC(2026, 0, 31, 22, 0, 0);
  const row = {
    id: "evening",
    startedAt: evening,
    actualMin: 25,
    completed: true,
    subject: "Maths",
    interruptions: 0,
  };
  const utcView = dailySeries([row as never], 7, 0, evening + 3_600_000);
  const shiftedView = dailySeries([row as never], 7, 360, evening + 3_600_000);
  check(
    "at UTC+0 a 22:00 session is filed on the 31st",
    utcView.find((entry) => entry.focusMinutes > 0)?.date === "2026-01-31",
    utcView.find((entry) => entry.focusMinutes > 0)?.date,
  );
  check(
    "at UTC+6 the same session is filed on the 1st",
    shiftedView.find((entry) => entry.focusMinutes > 0)?.date === "2026-02-01",
    shiftedView.find((entry) => entry.focusMinutes > 0)?.date,
  );
}

/* ---------------------------------------------------------------------------
 * 3. The internal consistency rules the charts depend on
 *
 * These are the properties a chart silently gets wrong rather than the numbers a
 * person reads. A series with a hole in it renders one day's minutes as the next
 * day's, so contiguity is checked directly rather than inferred from a sum.
 * ------------------------------------------------------------------------ */

console.log("\nthe properties the charts depend on");

for (const [name, fixture] of Object.entries(fixtures)) {
  const ts = typescriptStats(name);
  const expected = Math.max(1, Math.min(fixture.days, 365));
  check(`${name}: the series has exactly ${expected} rows`, ts.byDate.length === expected, ts.byDate.length);

  const indices = ts.byDate.map((entry) => Date.parse(`${entry.date}T00:00:00Z`) / MS_PER_DAY);
  const contiguous = indices.every((value, index) => index === 0 || value - indices[index - 1] === 1);
  check(`${name}: the series has no holes`, contiguous, ts.byDate.map((entry) => entry.date));

  check(
    `${name}: the series ends on today`,
    ts.byDate[ts.byDate.length - 1]?.date === ts.today.date,
    ts.byDate[ts.byDate.length - 1]?.date,
  );
  check(
    `${name}: today's minutes match the today row`,
    ts.byDate.find((entry) => entry.date === ts.today.date)?.focusMinutes === ts.totals.todayMinutes,
  );
  check(
    `${name}: the bySubject total plus unassigned equals focus minutes`,
    round2(
      ts.bySubject.reduce((sum, entry) => sum + entry.focusMinutes, 0) + ts.totals.unassignedMinutes,
    ) === ts.totals.focusMinutes,
    {
      subjects: ts.totals.focusMinutes - ts.totals.unassignedMinutes,
      unassigned: ts.totals.unassignedMinutes,
      total: ts.totals.focusMinutes,
    },
  );
  check(
    `${name}: goal progress is a fraction and never a percentage`,
    ts.totals.goalProgress >= 0 && ts.totals.goalProgress <= 1,
    ts.totals.goalProgress,
  );
}

check(
  "a zero daily goal reports zero progress rather than infinity",
  statsFor({ settings: { dailyGoalMin: 0 } } as never, 7, T0, 0).totals.goalProgress === 0,
);

/* ---------------------------------------------------------------------------
 * 4. Rules with no fixture, because they are about a shape rather than a number
 * ------------------------------------------------------------------------ */

console.log("\nshapes the charts depend on");

const empty = typescriptStats("no sessions at all");
check("an empty report still has a today row", empty.today.date === "2026-02-02");
check("an empty report has a null lastActiveDate", empty.streak.lastActiveDate === null);
check("an empty report's average is 0, not NaN", Number.isFinite(empty.totals.averageSessionMinutes));

const filled = statsFor(
  {
    settings: { dailyGoalMin: 120 },
    sessions: [session(0, 25, true, "A"), session(0, 25, true, "A"), session(0, 25, true, "B")],
  } as never,
  7,
  T0,
  0,
);
check(
  "a rolling average is never NaN, even at the start of a series",
  rollingAverage([0, 0, 25], 7).every((value) => Number.isFinite(value)),
  rollingAverage([0, 0, 25], 7),
);
check(
  "subjects are ordered longest first so a donut's slices are stable",
  filled.bySubject[0].focusMinutes >= filled.bySubject[1].focusMinutes,
  filled.bySubject,
);

const unstaggered = subjectTotals(
  [session(0, 25, true, "  "), session(0, 25, true, "Physics")] as never,
);
check(
  "a blank subject is counted as unassigned, never as a subject called ''",
  unstaggered.subjects.length === 1 && unstaggered.unassignedMinutes === 25,
  unstaggered,
);

const streakOne = streakOf([session(1, 25, true, "A"), session(2, 25, true, "A")] as never, 0, T0);
const streakLost = streakOf([session(3, 25, true, "A"), session(4, 25, true, "A")] as never, 0, T0);
check("a streak survives an unstarted today", streakOne.current === 2, streakOne);
check("a streak ends after a full missed day", streakLost.current === 0, streakLost);
check("but the record survives losing the run", streakLost.longest === 2, streakLost);

check(
  "localDateString matches the offset's sign convention, east-positive",
  localDateString(Date.UTC(2026, 0, 31, 23, 59, 59), 0) === "2026-01-31" &&
    localDateString(Date.UTC(2026, 0, 31, 23, 59, 59), 360) === "2026-02-01",
  `${localDateString(Date.UTC(2026, 0, 31, 23, 59, 59), 0)} / ${localDateString(
    Date.UTC(2026, 0, 31, 23, 59, 59),
    360,
  )}`,
);

check("emptyStats declares the shipped goal, not zero", emptyStats().totals.dailyGoalMinutes === 120);

/* ---------------------------------------------------------------------------
 * helpers
 * ------------------------------------------------------------------------ */

function round2(value: number): number {
  return Number.isFinite(value) ? Math.round(value * 100) / 100 : 0;
}

/**
 * Every leaf where two reports disagree.
 *
 * Recursive rather than a flat key list, because `totals.sessions` and
 * `bySubject[2].focusMinutes` both need reporting and a flat comparison would only
 * ever find the first kind.
 */
function diffReports(
  left: unknown,
  right: unknown,
  ignored: Record<string, true>,
  path = "",
): string[] {
  if (ignored[path]) return [];
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right)) return [`${path}: array vs not`];
    if (left.length !== right.length) {
      return [`${path}: length ${left.length} vs ${right.length}`];
    }
    return left.flatMap((value, index) => diffReports(value, right[index], ignored, `${path}[${index}]`));
  }
  if (left && right && typeof left === "object" && typeof right === "object") {
    const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
    return [...keys].flatMap((key) =>
      diffReports(
        (left as Record<string, unknown>)[key],
        (right as Record<string, unknown>)[key],
        ignored,
        path ? `${path}.${key}` : key,
      ),
    );
  }
  // Numbers are compared with a tolerance: Rust rounds to 2 dp and JavaScript does
  // not, so `24.999999` and `25.0` are the same statistic expressed twice.
  if (typeof left === "number" && typeof right === "number") {
    return Math.abs(left - right) < 0.005 ? [] : [`${path}: ${left} vs ${right}`];
  }
  return left === right ? [] : [`${path}: ${JSON.stringify(left)} vs ${JSON.stringify(right)}`];
}

// Node's `fs` is imported at the top rather than through a lazy `require`, because
// this file has a top-level `await` and Node refuses to guess the module format
// when `require()` and `await` are both present. That refusal is a runtime error
// halfway through the check, which is the worst place to meet it.
console.log(
  failures === 0
    ? "\nAll focus contract checks passed."
    : `\n${failures} focus contract check(s) FAILED.`,
);
process.exit(failures === 0 ? 0 : 1);