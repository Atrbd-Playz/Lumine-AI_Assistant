import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "./Icon";
import { CLOCK_FORMAT } from "../utils";
import { TabStrip, type TabStripItem } from "../../../components/ui/tabstrip";
import type { ActivityEntry, ActivityKind, ActivityTone } from "../../../features/activity/activityFeed";

/**
 * The activity timeline, as a table.
 *
 * ## What replaced what
 *
 * This draws the worker's own `LUMINE_EVENT` stream. The page it replaced drew
 * a hardcoded three-row list — a "morning standup", "3 tasks", "12 notes
 * indexed" — whose rows were invented in the component, so they were not stale,
 * they were *fiction*: they could not be wrong because they were never about
 * anything. An empty timeline is genuinely empty, on purpose.
 *
 * ## Why a table, and why newest first
 *
 * A log is read three ways — scanned for the one thing that went wrong,
 * filtered down to one kind of event, and read through when something was slow
 * and you want to know what was around it. A wall of undifferentiated rows
 * served none of the three.
 *
 * A table serves the first two structurally: fixed columns mean the eye can run
 * down **OUTCOME** looking for red without reading a single title, and
 * **DURATION** is a column of tabular figures where 40 ms is visibly a
 * different length from 400 ms. Newest first serves the third question people
 * actually arrive with — *what just happened* — without a scroll, and it is
 * what lets the old "jump to latest" machinery be deleted outright: the answer
 * is already at the top.
 *
 * The rows above the table (counters, filter chips, search, the count line)
 * exist for the scan and the filter; the table itself is the read-through.
 *
 * ## The columns are dropped by width, not by breakpoint
 *
 * `.activity-page` is a CSS container, so the table collapses against *its own*
 * width — the rail folding and the panel opening are both invisible to a media
 * query and are the two things that actually change how much room this page
 * has. KIND goes first (the title says it), then DURATION (the duration is
 * repeated inline under the title so it is never simply lost).
 */

type ActivityTimelineProps = {
  entries: readonly ActivityEntry[];
  onClear?: () => void;
};

/**
 * Tone is a *word* here rather than a Tailwind class string.
 *
 * Mapping colour by interpolating class names is how a `bg-${tone}-soft` ends up
 * emitting a class that does not exist, and it cannot be checked. Four literal
 * class strings, selected from a total `Record` over the tone union, are checked
 * by the compiler and readable in the source.
 */
const TONE_CLASS: Record<ActivityTone, string> = {
  neutral: "is-neutral",
  good: "is-good",
  warn: "is-warn",
  bad: "is-bad",
};

/** The one row of labels above the counters, so the counters have names. */
const KIND_LABEL: Record<ActivityKind, string> = {
  session: "Call",
  config: "Config",
  tool: "Tool",
  latency: "Timing",
  failure: "Problem",
  note: "Note",
};

/** How a row ended, in the one word the OUTCOME column can hold. */
const OUTCOME_LABEL: Record<ActivityTone, string> = {
  neutral: "Info",
  good: "Done",
  warn: "Attention",
  bad: "Failed",
};

/**
 * The word for a row's outcome, which is not always its tone.
 *
 * `warn` is two different things wearing one colour: a measurement that took
 * long enough to feel, and something worth a second look that has no timing at
 * all. "Slow" and "Attention" tell them apart; the colour does not, and a page
 * where the only signal is a hue is a page that fails for anyone who cannot
 * see it.
 */
function outcomeOf(entry: ActivityEntry): string {
  return entry.tone === "warn" && entry.ms !== undefined ? "Slow" : OUTCOME_LABEL[entry.tone];
}

/** Above this, agent-side work is slow enough that a person would have noticed. */
const SLOW_MS = 1000;

/**
 * The filters. Declared rather than derived, because each one is a question
 * somebody arrives with, and the set of questions does not change when a new
 * event type is added.
 *
 * `Problems` is the union of the two tones that mean something is wrong, so the
 * tab a person reaches for after a bad call is one click rather than a choice
 * between "failed" and "slow".
 */
const ALL = "__all__";
const FILTERS = [
  { id: ALL, label: "All", help: "Everything the worker reported, newest first." },
  { id: "problems", label: "Problems", help: "What failed, and what took long enough to be noticed." },
  { id: "session", label: "Calls", help: "Rooms opening and closing, and what they shared." },
  { id: "tool", label: "Tools", help: "Tool calls, one row per call and per step inside it." },
  { id: "latency", label: "Timing", help: "The worker's own stage measurements, with their measurements." },
] as const;

/** Which entries a filter id selects. */
function matchesFilter(filter: string, entry: ActivityEntry): boolean {
  switch (filter) {
    case ALL:
      return true;
    case "problems":
      return entry.tone === "bad" || entry.tone === "warn";
    default:
      return entry.kind === filter;
  }
}

/** Everything the search box can find in a row. Lowercased once per query. */
function haystack(entry: ActivityEntry): string {
  return `${entry.title}\n${entry.detail ?? ""}\n${KIND_LABEL[entry.kind]}`.toLowerCase();
}

/**
 * One `Intl.DateTimeFormat` for the whole list, built once.
 *
 * A fresh formatter per row is two hundred formatters per render of a full feed,
 * for a column of times that are all the same format by construction.
 */
const ENTRY_CLOCK = new Intl.DateTimeFormat(undefined, { ...CLOCK_FORMAT, second: "2-digit" });

/**
 * Wall-clock, to the second.
 *
 * Deliberately not a relative "3m ago". A latency timeline is read against the
 * clock — you want to know whether the 900 ms stutter was before or after the
 * thing you did — and relative times destroy exactly that. They also re-render
 * on a timer, which for a list that is already live would double its update
 * rate for no information.
 */
function formatEntryClock(at: number): string {
  const date = new Date(at);
  return Number.isNaN(date.getTime()) ? "" : ENTRY_CLOCK.format(date);
}

/** Start-of-day for a timestamp, as a comparable number. */
function dayKey(at: number): number {
  const date = new Date(at);
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/**
 * `Today`, `Yesterday`, or the date.
 *
 * Named rather than formatted, because "today" is a fact about the reader's day
 * and not something a fixed date string can say.
 */
function dayLabel(at: number): string {
  const start = dayKey(at);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const DAY = 86_400_000;
  if (start === today) return "Today";
  if (start === today - DAY) return "Yesterday";
  return new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "short" }).format(new Date(at));
}

/** `1.2 s`, `340 ms`, `2 m 04 s`. Read at a glance, never ambiguous. */
function formatMs(ms: number): string {
  if (ms >= 60_000) {
    const minutes = Math.floor(ms / 60_000);
    return `${minutes}m ${String(Math.floor((ms % 60_000) / 1000)).padStart(2, "0")}s`;
  }
  if (ms >= 1000) return `${(ms / 1000).toFixed(ms >= 10_000 ? 0 : 1)} s`;
  return `${Math.round(ms)} ms`;
}

/**
 * A mark's offset from session start: `+2h 14m`.
 *
 * The leading plus is doing real work. This column is headed DURATION, and a
 * bare `2h 14m` in it would read as "this took two hours" — the exact mistake
 * this page used to make with the same number. A milestone says when it
 * happened, and the `+` is how that is visible without a second column.
 */
function formatSince(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours > 0) return `+${hours}h ${minutes}m`;
  if (minutes > 0) return `+${minutes}m ${total % 60}s`;
  return `+${total}s`;
}

export function ActivityTimeline({ entries, onClear }: ActivityTimelineProps) {
  const listRef = useRef<HTMLDivElement>(null);
  const [filter, setFilter] = useState<string>(ALL);
  const [query, setQuery] = useState("");

  /**
   * The counters, over the *unfiltered* window.
   *
   * Over the filtered list instead would make the row a summary of the tab you
   * happen to be on, which is the one thing a summary row must not be: "3
   * problems" has to mean three problems whether or not you are looking at
   * them. `Over 1 s` counts only rows carrying a real measurement, since a
   * milestone's offset is not a cost.
   */
  const counts = useMemo(() => {
    let problems = 0;
    let calls = 0;
    let tools = 0;
    let slow = 0;
    for (const entry of entries) {
      if (entry.tone === "bad" || entry.tone === "warn") problems += 1;
      if (entry.kind === "session") calls += 1;
      if (entry.kind === "tool") tools += 1;
      if (entry.ms !== undefined && entry.ms > SLOW_MS) slow += 1;
    }
    return { problems, calls, tools, slow };
  }, [entries]);

  /**
   * Newest first, in one pass.
   *
   * The feed appends (prepending is O(n) per event and events arrive in
   * bursts), so the view is built by walking backwards. That is one allocation
   * per change of the feed rather than a reverse plus two filters, and the
   * order is decided here rather than trusted to CSS.
   */
  const searched = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const out: ActivityEntry[] = [];
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      const entry = entries[index];
      if (needle && !haystack(entry).includes(needle)) continue;
      out.push(entry);
    }
    return out;
  }, [entries, query]);

  const shown = useMemo(
    () => (filter === ALL ? searched : searched.filter((entry) => matchesFilter(filter, entry))),
    [searched, filter],
  );

  /**
   * The list as days, each day holding its own rows, newest first within it.
   *
   * One `<tbody>` per day, with the day as a `<th scope="rowgroup">` spanning the
   * table — which is what tells a screen reader that the rows below it belong to
   * that date. It was built for a sticky day label and became a *semantic*
   * decision instead, because measured in Chromium a sticky table cell is bounded
   * by the **table** and not by its row group, so every day's label piled up on
   * the same pixels under the column header. The separator survives; the
   * measurement is recorded with the rule in `index.css`.
   */
  const days = useMemo(() => {
    const groups: { day: number; entries: ActivityEntry[] }[] = [];
    for (const entry of shown) {
      const day = dayKey(entry.at);
      const current = groups.length > 0 ? groups[groups.length - 1] : undefined;
      if (current && current.day === day) current.entries.push(entry);
      else groups.push({ day, entries: [entry] });
    }
    return groups;
  }, [shown]);

  /**
   * A filter or a search replaces the whole table, so the reader is at the top
   * of a list they have just asked for and has no reason to be anywhere else.
   *
   * This is the only thing that moves the reader. Live arrivals deliberately do
   * not: rows are inserted at the top, and Chromium's scroll anchoring keeps a
   * reader who has scrolled down looking at the rows they were looking at.
   */
  useEffect(() => {
    const element = listRef.current;
    if (element) element.scrollTop = 0;
  }, [filter, query]);

  const tabs = useMemo<TabStripItem[]>(
    () =>
      FILTERS.map((candidate) => ({
        id: candidate.id,
        label: candidate.label,
        help: candidate.help,
        count:
          candidate.id === ALL
            ? searched.length
            : searched.filter((entry) => matchesFilter(candidate.id, entry)).length,
      })).filter((item) => item.count > 0 || item.id === filter),
    [searched, filter],
  );

  const trimmed = query.trim();

  return (
    <main className="workspace-view is-column" aria-labelledby="activity-title">
      <div className="activity-page">
        <header className="workspace-header">
          <div>
            <p className="eyebrow">Lumine workspace</p>
            <h1 id="activity-title">Activity</h1>
            <p>What the worker actually reported, as it reported it.</p>
          </div>
          {onClear && entries.length > 0 && (
            <button
              className="workspace-settings"
              onClick={onClear}
              aria-label="Clear the activity timeline"
              title="Clear the activity timeline"
            >
              <Icon name="trash" size={15} />
            </button>
          )}
        </header>

        <div className="activity-surface">
          {entries.length === 0 ? (
            <EmptyTimeline />
          ) : (
            <>
              <ul className="activity-stats">
                <Stat label="Reported" value={entries.length} unit={entries.length === 1 ? "entry" : "entries"} />
                <Stat
                  label="Problems"
                  value={counts.problems}
                  unit={counts.problems === 1 ? "entry" : "entries"}
                  alert={counts.problems > 0}
                />
                <Stat label="Calls" value={counts.calls} unit={counts.calls === 1 ? "session" : "sessions"} />
                <Stat label="Tool calls" value={counts.tools} unit={counts.tools === 1 ? "call" : "calls"} />
                <Stat
                  label={`Over ${SLOW_MS / 1000} s`}
                  value={counts.slow}
                  unit={counts.slow === 1 ? "measurement" : "measurements"}
                  alert={counts.slow > 0}
                />
              </ul>

              <div className="activity-controls">
                <TabStrip
                  items={tabs}
                  value={filter}
                  onChange={setFilter}
                  label="Activity categories"
                  hintSide="bottom"
                />

                <label className="activity-search">
                  <Icon name="search" size={13} aria-hidden />
                  <input
                    type="text"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Search events"
                    aria-label="Search the activity timeline"
                    spellCheck={false}
                    autoComplete="off"
                  />
                  {query !== "" && (
                    <button
                      type="button"
                      className="activity-search-clear"
                      onClick={() => setQuery("")}
                      aria-label="Clear the search"
                    >
                      <Icon name="close" size={11} />
                    </button>
                  )}
                </label>

                {/* The size of the table below, so a filter that matched nothing
                    and a table that is merely long are never confused. */}
                <p className="activity-count" aria-live="polite">
                  {shown.length.toLocaleString()} {shown.length === 1 ? "entry" : "entries"} · newest first
                  {trimmed ? ` · matching “${trimmed}”` : ""}
                </p>
              </div>

              {shown.length === 0 ? (
                <p className="activity-none">
                  {trimmed ? `Nothing matches “${trimmed}”.` : "Nothing in this group."}{" "}
                  {trimmed && (
                    <button type="button" className="activity-none-reset" onClick={() => setQuery("")}>
                      Clear the search
                    </button>
                  )}
                </p>
              ) : (
                /*
                  The scroll region, and the flex chain that makes it one.

                  This wrapper is a **column flex box**, not a plain block: it is
                  what lets the table below take the page's leftover height and
                  be smaller than its own contents. As a block it sized to its
                  content, so `overflow-y: auto` had nothing to overflow against
                  and the log ran as long as it liked — which is exactly the "the
                  page shows every row" symptom.
                */
                <div className="activity-scroll-region mt-2 flex min-h-0 flex-1 flex-col">
                  <div
                    ref={listRef}
                    className="activity-list"
                    tabIndex={0}
                    role="region"
                    aria-label="Timeline of worker events, newest first"
                  >
                    <table className="activity-table">
                      <thead>
                        <tr>
                          <th scope="col" className="activity-col-time">
                            Time
                          </th>
                          <th scope="col" className="activity-col-kind">
                            Kind
                          </th>
                          <th scope="col" className="activity-col-what">
                            What happened
                          </th>
                          <th scope="col" className="activity-col-outcome">
                            Outcome
                          </th>
                          <th scope="col" className="activity-col-ms">
                            Duration
                          </th>
                        </tr>
                      </thead>
                      {days.map((group) => (
                        <tbody key={group.day}>
                          <tr className="activity-day-row">
                            <th scope="rowgroup" colSpan={5} className="activity-day-cell">
                              <span className="activity-day">
                                <span>{dayLabel(group.day)}</span>
                              </span>
                            </th>
                          </tr>
                          {group.entries.map((entry) => (
                            <ActivityRow key={entry.id} entry={entry} />
                          ))}
                        </tbody>
                      ))}
                    </table>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </main>
  );
}

/**
 * One event.
 *
 * Its own component for a reason that has nothing to do with reuse: a log grows
 * every time the worker reports something, and a table with a thousand cells is
 * re-created wholesale on every one of those. A memoised row means a burst of
 * tool steps re-renders the rows that actually changed and leaves the rest of
 * the table's DOM — and the scroll position that depends on it — alone.
 */
const ActivityRow = memo(function ActivityRow({ entry }: { entry: ActivityEntry }) {
  const isDuration = entry.ms !== undefined;
  const isMark = entry.since !== undefined;
  const durationText = isDuration ? formatMs(entry.ms!) : isMark ? formatSince(entry.since!) : "—";
  const durationTitle = isDuration
    ? `${Math.round(entry.ms!)} milliseconds`
    : isMark
      ? "When this happened, measured from the start of the session — not a duration"
      : "";

  return (
    <tr className="activity-row">
      <td className="activity-cell-time">
        <time dateTime={new Date(entry.at).toISOString()}>{formatEntryClock(entry.at)}</time>
      </td>

      <td className="activity-cell-kind">
        <span className="activity-kind">{KIND_LABEL[entry.kind]}</span>
      </td>

      <td className="activity-cell-what">
        <span className="activity-what-line">
          {/* `title` as well as `truncate`. A truncated row that cannot be
              hovered to see the rest is a row whose detail is gone; these are
              provider strings and step labels, and both are longer than the
              column. */}
          <span className="activity-what-title" title={entry.title}>
            {entry.title}
          </span>
          {/* The duration again, inline — the only copy of it that survives the
              DURATION column collapsing on a narrow page. `display: none` by
              default, so it is never announced twice. */}
          {isDuration && <span className="activity-what-ms">{formatMs(entry.ms!)}</span>}
        </span>
        {entry.detail && (
          <span className="activity-what-detail" title={entry.detail}>
            {entry.detail}
          </span>
        )}
      </td>

      <td className="activity-cell-outcome">
        <span className={`activity-outcome ${TONE_CLASS[entry.tone]}`}>
          <span className="activity-outcome-dot" aria-hidden="true" />
          {outcomeOf(entry)}
        </span>
      </td>

      <td className="activity-cell-ms">
        {/* `is-empty` is a class rather than a `:empty` selector because the
            cell always renders *something* — the dash is the row's own statement
            that it has no measurement, and it should not carry the same weight
            as one that does. */}
        <span
          className={`activity-ms${!isDuration && !isMark ? " is-empty" : ""}`}
          title={durationTitle}
        >
          {durationText}
        </span>
      </td>
    </tr>
  );
});

/**
 * One counter. A label above, a figure below, and the unit spelled out rather
 * than abbreviated — "12 entries" is a thing you can read at a glance and "12e"
 * is not.
 */
function Stat({ label, value, unit, alert = false }: { label: string; value: number; unit: string; alert?: boolean }) {
  return (
    <li className={`activity-stat${alert ? " is-alert" : ""}`}>
      <span className="activity-stat-label">{label}</span>
      <span className="activity-stat-value">
        {value.toLocaleString()}
        <span className="activity-stat-unit">{unit}</span>
      </span>
    </li>
  );
}

/**
 * The empty state, which has to be honest about *why* it is empty.
 *
 * Three things can produce an empty timeline and they mean different things: the
 * worker is not running, it is running but has been asked nothing, or it is
 * running and the stream is not reaching the app. "No activity yet" conflates all
 * three and is the least useful of them, so the state names the first two and
 * points at Diagnostics for the third.
 */
function EmptyTimeline() {
  return (
    <div className="activity-empty flex h-full min-h-65 flex-col items-center justify-center gap-2.5 p-8 text-center">
      <span className="activity-empty-mark" aria-hidden="true">
        <Icon name="activity" size={20} />
      </span>
      <p className="text-[14px] text-foreground">Nothing reported yet</p>
      <p className="max-w-[46ch] text-[12.5px] leading-relaxed text-faint">
        Rooms opening and closing, tools running, failures, stage timings. Start a call and this
        fills in. If nothing lands, the stream is not reaching the app.
      </p>
    </div>
  );
}
