import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import { formatClock, CLOCK_FORMAT } from "../utils";
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
 * ## Live, with a pause
 *
 * Newest entries appear as the worker emits them, and a call in progress
 * generates them faster than anyone reads. So the list stops moving the moment
 * the reader scrolls away from the bottom, and says so with a button — the same
 * rule the transcript uses, for the same reason. An auto-scrolling log is
 * unreadable while you are reading it.
 */

type ActivityTimelineProps = {
  entries: readonly ActivityEntry[];
  onClear?: () => void;
  /** When true, the timeline is the page's only content and gets the full height. */
  expanded?: boolean;
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
  good: "text-accent",
  // Every tone now has a foreground of its own. `warn` was `--color-text` —
  // the same colour as the row beside it — so of the three colours two were
  // colours and one was an absence, and the amber only ever appeared as the
  // 3px edge on the left.
  warn: "text-warn",
  bad: "text-danger",
};

const TONE_EDGE: Record<ActivityTone, string> = {
  neutral: "bg-[var(--color-border)]",
  good: "bg-accent/60",
  warn: "bg-warn",
  bad: "bg-danger",
};

export function ActivityTimeline({ entries, onClear, expanded = false }: ActivityTimelineProps) {
  const scrollRef = useRef<HTMLOListElement>(null);
  const [pinned, setPinned] = useState(true);
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

  // Follow the tail only while the reader is already there. Re-runs on every
  // entry because that is the moment the list actually changed height.
  useEffect(() => {
    if (!pinned) return;
    const element = scrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [entries, pinned]);

  // Same condition, same trigger: pinned means the tail is rendered, so the
  // count of things the reader is behind on goes back to zero.
  useEffect(() => {
    if (pinned) setSeen(entries.length);
  }, [pinned, entries.length]);

  // A new arrival while the reader has scrolled up is a "there is something
  // below" signal, not something to yank them back down for.
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const onScroll = () => {
      const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
      setPinned(distance < 32);
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, []);

  const unread = pinned ? 0 : Math.max(0, entries.length - seen);

  return (
    <section
      className={expanded ? "flex min-h-0 flex-1 flex-col" : "activity-timeline"}
      aria-label="Activity"
    >
      <header className={expanded ? "workspace-header" : "activity-timeline-head shrink-0 pt-8.5 pb-4"}>
        <div>
          <p className="eyebrow">Lumine workspace</p>
          <h1 id="workspace-title">Activity</h1>
          <p>What the worker actually reported, as it reported it.</p>
        </div>
        {onClear && entries.length > 0 && (
          <div className="workspace-settings">
            <button onClick={onClear} aria-label="Clear the activity timeline" title="Clear the activity timeline">
              <Icon name="trash" size={15} />
            </button>
          </div>
        )}
      </header>

      <div className="relative min-h-0 flex-1">
        {entries.length === 0 ? (
          <EmptyTimeline />
        ) : (
          <ol
            ref={scrollRef}
            className="activity-list flex flex-col min-h-0 overflow-y-auto pb-4.5 scroll-hidden"
            tabIndex={0}
            aria-label="Timeline of worker events, oldest first"
          >
            {entries.map((entry) => (
              <li key={entry.id} className="activity-row">
                <span aria-hidden="true" className={`activity-row-edge absolute left-1 top-[9px] bottom-[9px] w-[3px] rounded-0.5 ${TONE_EDGE[entry.tone]}`} />
                <span className={`activity-row-glyph grid place-items-center w-5.5 shrink-0 ${TONE_TEXT[entry.tone]}`}>
                  <Icon name={GLYPH[entry.kind]} size={13} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] leading-snug text-foreground">{entry.title}</span>
                  {entry.detail && (
                    <span className="mt-0.5 block truncate text-[11.5px] leading-snug text-faint">{entry.detail}</span>
                  )}
                </span>
                <time className="shrink-0 pt-px font-mono text-[10.5px] tabular-nums text-faint" dateTime={new Date(entry.at).toISOString()}>
                  {formatEntryClock(entry.at)}
                </time>
              </li>
            ))}
          </ol>
        )}

        {!pinned && entries.length > 0 && (
          <button
            className="activity-jump"
            onClick={() => {
              setPinned(true);
              const element = scrollRef.current;
              if (element) element.scrollTo({ top: element.scrollHeight, behavior: "smooth" });
            }}
          >
            {unread} new · jump to latest
          </button>
        )}
      </div>
    </section>
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
    <div className="activity-empty flex flex-col items-center justify-center gap-2.5 h-full min-h-65 p-8 text-center">
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

/**
 * Wall-clock, to the second.
 *
 * Deliberately not a relative "3m ago". A latency timeline is read against the
 * clock — you want to know whether the 900 ms stutter was before or after the
 * thing you did — and relative times destroy exactly that. They also re-render on
 * a timer, which for a list that is already live would double its update rate for
 * no information.
 *
 * The seconds are the one thing this surface adds to the shared `CLOCK_FORMAT`,
 * and it says so here rather than assembling its own formatter to get them.
 */
function formatEntryClock(at: number): string {
  return formatClock(at, { ...CLOCK_FORMAT, second: "2-digit" });
}
