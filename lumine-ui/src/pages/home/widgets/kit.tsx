import { useEffect, useState, type ReactNode } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";

/**
 * The everyday rail's shared plumbing.
 *
 * Two of these cards talk to the network and four do not, but all six are the
 * same shape: a heading, a line of large type, and a caption. The card is drawn
 * once so the rail reads as one instrument rather than as six widgets that were
 * each styled on a different afternoon.
 *
 * Nothing here knows what a LiveKit room is, or that a voice agent exists. That
 * is the same boundary `ConversationPanel` holds: a clock that only works during
 * a call would be a clock.
 */
export function WidgetCard({
  title,
  meta,
  glyph,
  children,
  tone = "",
}: {
  title: string;
  /** Right-aligned caption in the heading. Sources, places, counts. */
  meta?: ReactNode;
  /** A right-hand glyph, used where the card's subject is pictorial. */
  glyph?: ReactNode;
  children: ReactNode;
  tone?: string;
}) {
  return (
    <section className={`widget-card${tone ? ` ${tone}` : ""}`}>
      <header className="widget-head flex items-center justify-between gap-2.5 min-w-0">
        <span className="widget-title text-faint text-[10.5px] font-semibold tracking-[0.09em] uppercase">{title}</span>
        {(meta || glyph) && (
          <span className="widget-aside inline-flex items-center gap-1.5 min-w-0 overflow-hidden text-faint text-[11px] text-ellipsis whitespace-nowrap">
            {meta}
            {glyph}
          </span>
        )}
      </header>
      <div className="widget-body">{children}</div>
    </section>
  );
}

/** The rail's resting state between a fetch and its answer. */
export function WidgetPending({ label }: { label: string }) {
  return (
    <p className="widget-pending flex items-center gap-2 m-0 text-faint text-[12px]" aria-live="polite">
      <span className="widget-pulse" aria-hidden="true" />
      {label}
    </p>
  );
}

/**
 * The rail's resting state when there is genuinely nothing to show.
 *
 * `children` is for the state that is *not* final — a failure the reader can
 * act on. A dead end that offers no control is a card that has decided the
 * reader should reload the whole app, and the weather card beside this one has
 * offered "Try again" since the day it shipped.
 */
export function WidgetEmpty({ label, detail, children }: { label: string; detail?: string; children?: ReactNode }) {
  return (
    <div className="widget-empty flex flex-col gap-1">
      <p>{label}</p>
      {detail && <small>{detail}</small>}
      {children}
    </div>
  );
}

/**
 * A clock, and the only reason the rail re-renders on a schedule.
 *
 * `active` exists for the timer rather than for the clock: a countdown has to
 * tick and a finished one must stop, because a `setInterval` nobody cleared is
 * a wake-up a laptop notices and a person does not. The value returned is the
 * time *read at the tick*, so two components on the same schedule cannot
 * disagree about the second they are in.
 */
export function useTicker(intervalMs: number, active = true): number {
  const [tick, setTick] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    // Refresh on mount rather than a beat later: a countdown that shows its old
    // value for the first second of a pause reads as a control that did nothing.
    setTick(Date.now());
    const timer = window.setInterval(() => setTick(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, active]);
  return tick;
}

/**
 * State that survives a restart, and never takes the rail down with it.
 *
 * localStorage throws — a full disk, a private window, a disabled profile — and
 * every one of those is a worse outcome as a blank rail than as a clock that
 * forgot your city. Every access is therefore wrapped, and a value that will not
 * parse falls back rather than throwing inside `useState`'s initializer, where
 * React has no recovery path at all.
 */
export function usePersistentState<T>(
  key: string,
  initial: T,
): [T, (next: T | ((previous: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key);
      if (raw === null) return initial;
      return JSON.parse(raw) as T;
    } catch {
      return initial;
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // Deliberately swallowed. Losing the setting for this session is the
      // cheap version of this failure.
    }
  }, [key, value]);

  return [value, setValue];
}

/**
 * A headline is read somewhere else, never here.
 *
 * In the desktop shell the OS's own browser takes it; in the dev server without
 * Tauri it opens a tab. Both are the same promise, and neither is a preview
 * rendered inside a card 300px wide.
 */
export function openExternal(url: string): void {
  if (!url) return;
  if ("__TAURI_INTERNALS__" in window) {
    void openUrl(url).catch(() => undefined);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}
