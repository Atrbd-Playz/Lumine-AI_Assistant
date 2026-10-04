import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "./Icon";
import { CLOCK_FORMAT } from "../utils";
import { TabStrip, type TabStripItem } from "../../../components/ui/tabstrip";
import type { ActivityEntry, ActivityKind, ActivityTone } from "../../../features/activity/activityFeed";

/**
 * The activity timeline.
 *
 * ## What replaced what
 *
 * This draws a hardcoded three-row list — a "morning standup", "3 tasks", "12
 * notes indexed" — with the worker's own `LUMINE_EVENT` stream. Those rows were
 * invented in the component, so they were not stale, they were *fiction*: they
 * could not be wrong because they were never about anything.
 *
 * The replacement is worse in one specific way, on purpose: an empty timeline is
 * genuinely empty, and a person looking at one learns that the panel reports
 * facts. A panel that always has three rows in it teaches the opposite.
 *
 * ## Why the page is shaped the way it is
 *
 * A log is read three ways and they want three different surfaces:
 *
 * - **scanned** for the one thing that went wrong,
 * - **filtered** down to one kind of event when a question is specific,
 * - **read through** in order when something was slow and you want to know what
 *   was around it.
 *
 * So: a counter row across the top for the scan, a category strip for the filter,
 * and dated rows in wall-clock order for the read-through. What it used to be was
 * one undifferentiated list of 200 rows with the newest at the bottom, which
 * served none of the three — and it carried the day's answer in a heading that
 * said nothing, next to a two-word empty state.
 *
 * ## Live, with a pause
 *
 * New entries appear as the worker emits them, and a call in progress
 * generates them faster than anyone reads. So the list stops moving the moment
 * the reader scrolls away from the bottom, and says so with a button — the same
 * rule the transcript uses, for the same reason. An auto-scrolling log is
 * unreadable while you are reading it.
 */

type ActivityTimelineProps = {
  entries: readonly ActivityEntry[];
  onClear?: () => void;
};

/** One glyph per kind. `fallback` is the one thing every kind shares. */
const GLYPH: Record<ActivityKind, Parameters<typeof Icon>[0]["name"]> = {
  session: "waveform",
  config: "settings",
  tool: "tools",
  latency: "clock",
  failure: "info",
  note: "chat",
};

/**
 * Tone is a *word* here rather than a Tailwind class string.
 *
 * Mapping colour by interpolating class names is how a `bg-${tone}-soft` ends up
 * emitting a class that does not exist, and it cannot be checked. Four literal
 * Tailwind class strings, selected from a total `Record` over the tone union, is
 * checked by the compiler and readable in the source.
 */
const TONE_TEXT: Record<ActivityTone, string> = {
  neutral: "text-muted-foreground",
  good: "text-ok",
  // Every tone now has a foreground of its own. `warn` was `--color-text` —
  // the same colour as the row beside it — so of the three colours two were
  // colours and one was an absence, and the amber only ever appeared as the
  // 3px edge on the left.
  warn: "text-warn",
  bad: "text-danger",
};

const TONE_EDGE: Record<ActivityTone, string> = {
  neutral: "bg-[var(--color-border)]",
  good: "bg-ok",
  warn: "bg-warn",
  bad: "bg-danger",
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

/** Above this, agent-side work is slow enough that a person would have noticed. */
const SLOW_MS = 1000;

/**
 * The filters. Declared rather than derived, because each one is a question
 * somebody arrives with, and the set of questions does not change when a new event
 * type is added.
 *
 * `Problems` is the union of the two tones that mean something is wrong, so the
 * tab a person reaches for after a bad call is one click rather than a choice
 * between "failed" and "slow" — and the strip stays at five entries, which is as
 * many as fit on the label at the width this page has. A sixth was tried and
 * arrived truncated as `Probl…`.
 */
const ALL = "__all__";
const FILTERS = [
  { id: ALL, label: "All", help: "Everything the worker reported, oldest first." },
  { id: "problems", label: "Problems", help: "What failed, and what took long enough to be noticed." },
  { id: "session", label: "Calls", help: "Rooms opening and closing." },
  { id: "tool", label: "Tools", help: "Tool calls, one row per call and per step inside it." },
  { id: "latency", label: "Timing", help: "The worker's own stage measurements, with their measurements." },
] as const;

/** Which entries a filter id selects. */
function matches(filter: string, entry: ActivityEntry): boolean {
  switch (filter) {
    case ALL:
      return true;
    case "problems":
      return entry.tone === "bad" || entry.tone === "warn";
    default:
      return entry.kind === filter;
  }
}

/**
 * One `Intl.DateTimeFormat` for the whole list, built once.
 *
 * The previous version called `formatClock` per row, and that builds a fresh
 * formatter every call — two hundred of them per render of a full feed, for a
 * column of times that are all the same format by construction.
 */
const ENTRY_CLOCK = new Intl.DateTimeFormat(undefined, { ...CLOCK_FORMAT, second: "2-digit" });

/**
 * Wall-clock, to the second.
 *
 * Deliberately not a relative "3m ago". A latency timeline is read against the
 * clock — you want to know whether the 900 ms stutter was before or after the
 * thing you did — and relative times destroy exactly that. They also re-render on
 * a timer, which for a list that is already live would double its update rate for
 * no information.
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
 * and not something a fixed date string can say. The fallback is deliberately
 * compact: this is a separator in a scrolling list, not a headline.
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

export function ActivityTimeline({ entries, onClear }: ActivityTimelineProps) {
  const scrollRef = useRef<HTMLOListElement>(null);
  const [pinned, setPinned] = useState(true);
  const pinnedRef = useRef(true);
  /**
   * How many entries have actually been on screen, so `unread` is a delta.
   *
   * It was `pinned ? 0 : entries.length` — either nothing or *everything the
   * session has ever logged*. Scroll up once during a long run and the button
   * claims fifty new rows, forty-nine of which the reader watched arrive. This
   * is the baseline, and it advances only while the tail is visible, because
   * that is the one moment "everything so far" is genuinely "everything seen".
   */
  const [seen, setSeen] = useState(0);
  const [filter, setFilter] = useState<string>(ALL);

  const setFollowing = useCallback((next: boolean) => {
    pinnedRef.current = next;
    setPinned(next);
  }, []);

  /**
   * The counters, over the *unfiltered* window.
   *
   * Over the filtered list instead would make the row a summary of the tab you
   * happen to be on, which is the one thing a summary row must not be: "3
   * problems" has to mean three problems whether or not you are looking at them.
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

  const shown = useMemo(() => (filter === ALL ? entries : entries.filter((entry) => matches(filter, entry))), [entries, filter]);

  /**
   * The list as days, each day holding all of its own rows.
   *
   * The grouping is the reason a day label can stick. `position: sticky` moves an
   * element only while its **containing block** is in the scrollport, and a
   * containing block is its parent — so with one `<li>` per row and the label
   * inside the first of them, the label held for exactly one row's height and then
   * scrolled away with it. Every day here is a single `<li>` containing its label
   * and its rows, which is the whole difference between a date you can read while
   * you scroll and a date you saw once.
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
   * Follow the tail only while the reader is already there. Re-runs on every entry
   * because that is the moment the list actually changed height.
   */
  useEffect(() => {
    if (!pinnedRef.current) return;
    const element = scrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [shown]);

  // A filter change replaces the whole list, so the reader is at the tail of a
  // list they have just asked for and has no reason to be anywhere else. This is
  // the one exception to the "never move a reader" rule above, and it is why the
  // rule needs to be a rule rather than a default.
  useEffect(() => {
    setSeen(0);
    const element = scrollRef.current;
    if (!element) return;
    const frame = requestAnimationFrame(() => {
      element.scrollTop = element.scrollHeight;
      setFollowing(true);
    });
    return () => cancelAnimationFrame(frame);
  }, [filter, setFollowing]);

  // Same condition, same trigger: pinned means the tail is rendered, so the
  // count of things the reader is behind on goes back to zero.
  useEffect(() => {
    if (pinned) setSeen(shown.length);
  }, [pinned, shown.length]);

  // A new arrival while the reader has scrolled up is a "there is something
  // below" signal, not something to yank them back down for.
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const onScroll = () => {
      const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
      setFollowing(distance < 32);
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, [setFollowing]);

  const unread = pinned ? 0 : Math.max(0, shown.length - seen);

  const tabs = useMemo<TabStripItem[]>(
    () =>
      FILTERS.map((candidate) => ({
        id: candidate.id,
        label: candidate.label,
        help: candidate.help,
        count: candidate.id === ALL ? entries.length : entries.filter((entry) => matches(candidate.id, entry)).length,
      })).filter((item) => item.count > 0 || item.id === filter),
    [entries, filter],
  );

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

              <div>
                <TabStrip items={tabs} value={filter} onChange={setFilter} label="Activity categories" hintSide="bottom" />
              </div>

              {shown.length === 0 ? (
                <p className="tools-note">Nothing in this group.</p>
              ) : (
                /*
                  The scroll region, and the flex chain that makes it one.

                  This wrapper is a **column flex box**, not a plain block: it is
                  what lets the list below take the page's leftover height and be
                  smaller than its own contents. As a block it sized to its
                  content, so `overflow-y: auto` on the list had nothing to
                  overflow against and the log simply ran as long as it liked —
                  which is exactly the "the page shows every row" symptom.

                  `relative` is load-bearing too: the "jump to latest" pill inside
                  is absolutely positioned against it.
                */
                <div className="activity-scroll-region relative mt-2 flex min-h-0 flex-1 flex-col">
                  <ol
                    ref={scrollRef}
                    className="activity-list flex min-h-0 flex-1 flex-col"
                    tabIndex={0}
                    aria-label="Timeline of worker events, oldest first"
                  >
                    {days.map((group) => (
                      <li key={group.day} className="activity-day-group">
                        <h2 className="activity-day">
                          <span>{dayLabel(group.day)}</span>
                        </h2>
                        {group.entries.map((entry) => (
                          <ActivityRow key={entry.id} entry={entry} />
                        ))}
                      </li>
                    ))}
                  </ol>

                  {!pinned && (
                    <button
                      className="activity-jump"
                      onClick={() => {
                        setFollowing(true);
                        const element = scrollRef.current;
                        if (element) element.scrollTo({ top: element.scrollHeight, behavior: "smooth" });
                      }}
                    >
                      {unread > 0 ? `${unread} new · jump to latest` : "jump to latest"}
                    </button>
                  )}
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
 * every time the worker reports something, and a single element with a hundred
 * children is re-created wholesale on every one of those. A memoised row means a
 * burst of tool steps re-renders the rows that actually changed and leaves the rest
 * of the timeline's DOM — and the scroll offsets that depend on it — alone.
 */
const ActivityRow = memo(function ActivityRow({ entry }: { entry: ActivityEntry }) {
  return (
    <div className="activity-row">
      <span
        aria-hidden="true"
        className={`absolute left-1 top-[11px] bottom-[11px] w-[2px] rounded-0.5 ${TONE_EDGE[entry.tone]}`}
      />
      <span className={`activity-row-glyph grid size-6 shrink-0 place-items-center ${TONE_TEXT[entry.tone]}`}>
        <Icon name={GLYPH[entry.kind]} size={13} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-2">
          <span className="shrink-0 font-mono text-[9px] tracking-[0.08em] uppercase text-faint">
            {KIND_LABEL[entry.kind]}
          </span>
          {/* `title` as well as `truncate`. A truncated row that cannot be hovered
              to see the rest is a row whose detail is gone; these are provider
              strings and step labels, and both are longer than the column. */}
          <span className="truncate text-[13px] leading-snug text-foreground" title={entry.title}>
            {entry.title}
          </span>
        </span>
        {entry.detail && (
          <span className="mt-0.5 block truncate text-[11.5px] leading-snug text-faint" title={entry.detail}>
            {entry.detail}
          </span>
        )}
      </span>
      {entry.ms !== undefined && (
        <span
          className={`activity-row-ms${entry.ms > SLOW_MS ? " is-slow" : ""}`}
          title={`${Math.round(entry.ms)} milliseconds`}
        >
          {formatMs(entry.ms)}
        </span>
      )}
      <time
        className="shrink-0 pt-px font-mono text-[10.5px] tabular-nums text-faint"
        dateTime={new Date(entry.at).toISOString()}
      >
        {formatEntryClock(entry.at)}
      </time>
    </div>
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
