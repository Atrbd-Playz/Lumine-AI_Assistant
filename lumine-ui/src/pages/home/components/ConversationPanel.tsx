import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { ConversationMessage as MessageRow } from "./ConversationMessage";
import { Icon, Mark } from "./Icon";
import type {
  ConversationAgentStatus,
  ConversationItem,
  ConversationMessage,
} from "../conversation/types";
import type { ConversationBubbleVariant } from "../types";

/**
 * The transcript, as a right-hand aside.
 *
 * ## Why this came back
 *
 * It existed, was replaced by `SessionDock` at the bottom of the stage, and was
 * then asked for again. The second version was a better *argument* than the dock:
 * a dock costs no width but permanently costs height and takes the avatar's stage
 * every time it opens; a panel costs width when open and nothing when shut, and
 * the stage keeps every pixel of its height.
 *
 * On the window sizes this app opens at, height is the scarcer resource. The
 * avatar is the subject of the home page, and a transcript that steals a third of
 * the stage to sit there closed was spending more than it returned.
 *
 * ## What this kept from the dock
 *
 * The look is the original panel's. The behaviour is the dock's, which was
 * stricter about three things and all three survived this move:
 *
 * - **`onSubmit` actually sends.** The original panel's composer wrote into local
 *   state and stopped — a note to yourself shaped like a message to Lumine. This
 *   one is wired in `Home.tsx` to the voice layer's `sendText`, so the text
 *   reaches the room and she answers out loud.
 * - **It says why it is disabled.** `submitHint` explains the disabled composer
 *   in the user's terms rather than leaving a dead text field.
 * - **It does not claim a history it does not have.** The transcript is not
 *   persisted, and the empty state says so.
 *
 * ## The backend-agnostic rule still holds
 *
 * This component imports nothing from `features/voice`. Live messages arrive
 * through the callbacks `Home.tsx` wires in. That constraint is load-bearing and
 * it survived both rewrites.
 *
 * A tool payload still must never come through here. Those reach the user on
 * the stage ledger via `onToolResult`, because a retrieved page rendered
 * inline in the transcript reads as though the model had been handed it by
 * the user — which is the shape a prompt injection wants.
 */

const STATUS_COPY: Record<ConversationAgentStatus, string> = {
  idle: "Ready when you are",
  connecting: "Reaching Lumine",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
  error: "Connection needs attention",
};

export type ConversationPanelProps = {
  items: readonly ConversationItem[];
  agentStatus: ConversationAgentStatus;
  bubbleVariant: ConversationBubbleVariant;
  /** The voice layer's real text channel. `false` while a room is not joined. */
  canSubmit: boolean;
  /** Why the composer is disabled, in the user's terms. */
  submitHint: string;
  onSubmit: (text: string) => boolean;
  onClear: () => void;
  onReset: () => void;
  onOpenChange: (open: boolean) => void;
  /**
   * Whether the panel is showing. Controlled, not local.
   *
   * Three things open this and none of them live here: the stage's transcript
   * switch, the command palette's "Show the transcript", and `mod+shift+enter`.
   * The switch used to be a rail item called "Conversation"; it moved into the
   * command space because the panel is a slot beside the stage, not a place you
   * navigate to, and local state would mean any of the three could arrive at a
   * closed panel and then need a second click to open it.
   *
   * The storage key and its migration still live here, in `readStoredPanel` and
   * `writeStoredPanel`, so `Home.tsx` — which decides which of the three is
   * showing — never has to know `localStorage` exists.
   */
  open: boolean;
};

/**
 * How close to the bottom still counts as *reading* the bottom.
 *
 * 48px rather than zero because a tail that has to be exact is a tail nobody can
 * stay on: one rubber-band pixel from the end and the transcript stops following,
 * so a row arrives that the reader never sees arrive. 48px is about the height of
 * the last row's own margin, which is the smallest band that cannot be left
 * behind by ordinary reading.
 */
const TAIL_THRESHOLD = 48;

/** Remaining scrollable distance from the tail, in pixels. */
function distanceFromTail(element: HTMLElement): number {
  return element.scrollHeight - element.scrollTop - element.clientHeight;
}

/** Whether the transcript should follow the tail on the next growth. */
function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

export function ConversationPanel({
  items,
  agentStatus,
  bubbleVariant,
  canSubmit,
  submitHint,
  onSubmit,
  onClear,
  onReset,
  onOpenChange,
  open,
}: ConversationPanelProps) {
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  /**
   * Whether the reader is at the tail, as one value with two readers.
   *
   * `pinned` is what renders; `pinnedRef` is what the growth path reads. They
   * have to be one fact — the growth handler runs inside a layout effect, and
   * reading the state variable there means closing over the value from the
   * commit that is still being applied, which is how a transcript ends up
   * scrolling when the reader scrolled away from the new message half a second
   * ago.
   */
  const [pinned, setPinned] = useState(true);
  const pinnedRef = useRef(true);
  /**
   * How many rows have been on screen, so `unseen` is a delta.
   *
   * It used to be `showNew ? 0 : items.length`, which is either nothing or
   * *everything the session has ever said*. Scroll up once during a long call
   * and the button claims forty new rows, thirty-nine of which the reader
   * watched arrive. This is the baseline, and it advances only while the tail is
   * visible — the one moment "everything so far" is genuinely "everything seen".
   */
  const [seen, setSeen] = useState(0);

  const setFollowing = useCallback((next: boolean) => {
    pinnedRef.current = next;
    setPinned(next);
  }, []);

  const scrollToTail = useCallback((behavior: ScrollBehavior = "auto") => {
    const element = scrollRef.current;
    // A zero-height element is the panel being shut, not an empty transcript.
    // Writing `scrollTop` to one throws it away, so a reader who opens the
    // transcript after a long call would land wherever the last visible offset
    // happened to be.
    if (!element || element.clientHeight === 0) return;
    element.scrollTo({ top: element.scrollHeight, behavior });
  }, []);

  // Opening puts the caret in the composer from every path that can open it. A
  // frame's wait is not cosmetic: with the panel shut the composer is still
  // mounted (it is only `hidden`), but the *element* has zero size, so a focus
  // written on the opening frame lands on something the layout has not measured
  // yet. Waiting one frame lets the width transition commit first.
  useEffect(() => {
    if (!open) return;
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open]);

  /**
   * Opening also means "show me the end".
   *
   * Three things open this panel and none of them is a message arriving: the
   * stage's transcript switch, the command palette, and `mod+shift+enter`. Each
   * is a request to *read* the conversation, and the readable end of a
   * conversation is its latest message — so the tail is claimed explicitly rather
   * than inferred from an offset that survived while the panel was `display:
   * none`. Without this, a panel reopened after a few turns of speech opened in
   * the middle with nothing to say so.
   */
  useEffect(() => {
    if (!open) return;
    const frame = requestAnimationFrame(() => {
      scrollToTail("auto");
      setFollowing(true);
    });
    return () => cancelAnimationFrame(frame);
  }, [open, scrollToTail, setFollowing]);

  /**
   * Follow the tail only when the reader is already at it.
   *
   * The transcript grows mid-read — a turn finishes, a tool starts, an interim
   * transcript chunk lands — and an auto-scrolling transcript is unreadable
   * while you are reading it. The "jump" affordance is what makes the
   * alternative honest.
   *
   * Three details are load-bearing and each one was a bug first:
   *
   * - **`useLayoutEffect`, not `useEffect`.** The rows are in the DOM by the time
   *   a layout effect runs, so `scrollHeight` is already the new height. A
   *   passive effect runs after the browser has been handed a frame, which means
   *   the transcript visibly jumps *after* painting the new message instead of
   *   arriving with it.
   * - **`auto`, not `"smooth"`.** Streaming transcription re-runs this on every
   *   interim chunk. Restarting a smooth scroll animation two or three times a
   *   second means the tail is always chasing a target that has just moved, so
   *   it lags behind the speaker — a lag is exactly what an unreadable
   *   auto-scroll looks like. The button keeps `smooth`, because that is a
   *   single deliberate gesture.
   * - **The zero-size guard.** While the panel is shut every measurement is
   *   zero, so the "am I at the tail" arithmetic returns 0 and concludes yes.
   *   That is why messages used to arrive into a hidden transcript, be counted
   *   as seen, and never raise the jump button.
   */
  useLayoutEffect(() => {
    if (!pinnedRef.current) return;
    scrollToTail("auto");
  }, [items, scrollToTail]);

  /**
   * Re-pin when the transcript grows for a reason that is not a new row.
   *
   * A webfont swapping in, a bubble re-wrapping as the window resizes, or an
   * interim transcript being replaced by its final text all change the height
   * of rows that are already on screen. None of them change `items` by
   * reference, so the layout effect above does not run for them — and the
   * transcript drifts away from the tail a line at a time with no explanation.
   * Observing the list's own box catches every one of them, and only acts while
   * the reader is already following, so a reader who has scrolled away is never
   * moved.
   *
   * The dependency is the *existence* of the list rather than the effect's mount,
   * because the list is not in the tree at all while the transcript is empty: an
   * observer attached once on mount found no node, and an empty list is exactly
   * the state the first message arrives into.
   */
  const hasRows = items.length > 0;
  useEffect(() => {
    const list = listRef.current;
    if (!hasRows || !list || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (pinnedRef.current) scrollToTail("auto");
    });
    observer.observe(list);
    return () => observer.disconnect();
  }, [hasRows, scrollToTail]);

  // Same condition, same trigger: at the tail means the newest row is rendered,
  // so the count of things the reader is behind on goes back to zero.
  useEffect(() => {
    if (pinned) setSeen(items.length);
  }, [pinned, items.length]);

  // A new arrival while the reader has scrolled up is a "there is something
  // below" signal, not something to yank them back down for. A listener rather
  // than a recompute inside the effect above, because the two answer different
  // questions: one reacts to the list changing, this one reacts to the reader
  // moving.
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const onScroll = () => {
      if (element.clientHeight === 0) return;
      setFollowing(distanceFromTail(element) < TAIL_THRESHOLD);
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, [setFollowing]);

  const unseen = pinned ? 0 : Math.max(0, items.length - seen);

  const submit = useCallback(() => {
    const content = draft.trim();
    if (!content || !canSubmit || pending) return;
    setPending(true);
    // Optimistic: the message appears immediately because the round trip is
    // short and a composer that waits on a promise to show its own text feels
    // broken. If the send fails the caller toasts — `sendText` returns `false`
    // rather than throwing, because a rejected promise in a submit handler is
    // an unhandled rejection.
    const accepted = onSubmit(content);
    setPending(false);
    if (accepted) {
      setDraft("");
      // Sending implies reading: you cannot have sent a message without the
      // transcript being where you are looking.
      onOpenChange(true);
    }
  }, [canSubmit, draft, onOpenChange, onSubmit, pending]);

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter is a newline. Stated in the placeholder, because
    // a composer that swallows a newline with no hint is the most common
    // complaint about text areas.
    //
    // Not while composing. On an IME, Enter is what *accepts* the candidate —
    // Japanese, Chinese and Korean all work this way — so without this guard
    // the keystroke that finishes the word also fires it, half-committed, at
    // the agent. `isComposing` is the standard signal and there is no second
    // place in the app that needs it: this is the only text entry in a room.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  /**
   * Whether two adjacent rows belong to the same speaker's turn.
   *
   * A message's `type` is `"text" | "voice"`, not `"message"` — `"tool"` and
   * `"system"` are the other two shapes, and those are what the map branches on.
   * So the guard is the negation of those two literals rather than a positive
   * check for something no variant ever equals; writing `=== "message"` compiles
   * against the union and is always false, which silently turns grouping off
   * rather than failing.
   */
  const isMessage = (item: ConversationItem | undefined): item is ConversationMessage =>
    item !== undefined && item.type !== "tool" && item.type !== "system";

  const sameGroup = (left: ConversationItem | undefined, right: ConversationItem | undefined) =>
    isMessage(left) && isMessage(right) && left.role === right.role && left.type === right.type;

  return (
    <aside
      id="conversation-panel"
      className={`conversation-panel${open ? " is-open" : ""}`}
      aria-label="Conversation"
      hidden={!open}
    >
      <header className="conversation-head">
        <div>
          <div className="conversation-brand flex items-center gap-2 text-foreground text-[15px] font-bold tracking-[-0.035em]">
            <Mark />
            <span>Conversation</span>
          </div>
          <p>
            <span className={`conversation-status-dot w-1.5 h-1.5 rounded-full bg-faint is-${agentStatus}`} aria-hidden="true" />
            {STATUS_COPY[agentStatus]}
          </p>
        </div>
        <div className="conversation-head-actions flex items-center gap-0.5">
          <button
            type="button"
            className="panel-icon"
            onClick={onReset}
            aria-label="Reset conversation"
            title="Reset conversation"
          >
            <Icon name="reset" size={16} />
          </button>
          <button
            type="button"
            className="panel-icon"
            onClick={onClear}
            aria-label="Clear conversation"
            title="Clear conversation"
          >
            <Icon name="trash" size={16} />
          </button>
          <button
            type="button"
            className="panel-icon"
            onClick={() => onOpenChange(false)}
            aria-label="Close conversation"
            title="Close conversation"
          >
            <Icon name="close" size={17} />
          </button>
        </div>
      </header>

      <div className="conversation-scroll-wrap relative min-h-0 grid grid-rows-[minmax(0,_1fr)]">
        <div ref={scrollRef} className="conversation-scroll min-h-0 overflow-y-auto p-5 scroll-hidden">
          {items.length === 0 ? (
            <PanelEmpty />
          ) : (
            <div ref={listRef} className="conversation-list">
              {items.map((item, index) => {
                const previous = items[index - 1];
                const next = items[index + 1];
                if (item.type === "tool") {
                  return (
                    <div key={item.id} className={`conversation-tool is-${item.status}`}>
                      <span aria-hidden="true">
                        {item.status === "completed" ? "✓" : item.status === "failed" ? "!" : "◇"}
                      </span>
                      <span>{item.name}</span>
                      {item.summary && <small>{item.summary}</small>}
                    </div>
                  );
                }
                if (item.type === "system") {
                  return (
                    <div key={item.id} className="conversation-system">
                      {item.text}
                    </div>
                  );
                }
                return (
                  <MessageRow
                    key={item.id}
                    message={item}
                    variant={bubbleVariant}
                    showLabel={!sameGroup(previous, item)}
                    showTimestamp={!sameGroup(item, next)}
                    isGroupStart={!sameGroup(previous, item)}
                    isGroupEnd={!sameGroup(item, next)}
                  />
                );
              })}
            </div>
          )}
        </div>

        {!pinned && items.length > 0 && (
          <button
            type="button"
            className="conversation-new-messages"
            onClick={() => {
              // Smooth, because this is one deliberate gesture rather than a
              // stream of them — and dropped when the reader has asked for less
              // motion, where an animated scroll over a screen of text is
              // exactly the movement they turned off.
              scrollToTail(prefersReducedMotion() ? "auto" : "smooth");
              setFollowing(true);
            }}
          >
            {unseen > 0 ? `↓ ${unseen} new` : "↓ New messages"}
          </button>
        )}
      </div>

      <div className="conversation-composer">
        <textarea
          ref={inputRef}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          rows={1}
          aria-label="Send a message to Lumine"
          aria-describedby="conversation-composer-hint"
          placeholder={canSubmit ? "Leave a note — Enter sends, Shift+Enter breaks the line" : submitHint}
          disabled={!canSubmit}
          spellCheck
        />
        <button
          type="button"
          onClick={submit}
          disabled={!canSubmit || !draft.trim() || pending}
          className={draft.trim() && canSubmit ? "is-ready" : ""}
          aria-label="Send to Lumine"
        >
          <Icon name="send" size={15} />
        </button>
      </div>
      <p id="conversation-composer-hint" className="sr-only">
        {canSubmit
          ? "Press Enter to send, Shift plus Enter for a new line. Your message reaches Lumine as text, and she answers out loud."
          : submitHint}
      </p>
    </aside>
  );
}

/**
 * The empty state, which states the one thing a user cannot guess.
 *
 * Not "no messages" — the reader cannot tell from that whether their text is
 * being delivered. It says what happens when they type, and that a call has to be
 * up.
 */
function PanelEmpty() {
  return (
    <div className="conversation-empty">
      <p>Nothing said yet</p>
      <small>Start a call, then talk — or type below and she answers out loud.</small>
    </div>
  );
}

/**
 * Which right-hand aside is open, as persisted.
 *
 * `localStorage` in a Tauri webview, and it can throw. Exported because the slot
 * is owned by `Home.tsx` — the two topbar switches, the command palette, the
 * keyboard shortcut and the panels all drive it — and a page shell that invented
 * its own copy of the key would silently disagree with the panels on the next
 * load.
 *
 * The two panels share one slot rather than two, so this is a *kind* and not a
 * boolean: two open asides would be two panels fighting over a single grid track,
 * and the reader would have to close one to find the other behind it.
 */
export type PanelKind = "conversation" | "notes" | null;

export function readStoredPanel(): PanelKind {
  try {
    const raw = window.localStorage.getItem("lumine.panel");
    if (raw === null) {
      // Default to the transcript. A first run that opens on nothing hides the
      // one panel the product is built around behind a button.
      const legacy = window.localStorage.getItem("lumine.panel.open");
      return legacy === "0" ? null : "conversation";
    }
    return raw === "conversation" || raw === "notes" ? raw : null;
  } catch {
    return "conversation";
  }
}

export function writeStoredPanel(panel: PanelKind): void {
  try {
    if (panel === null) window.localStorage.removeItem("lumine.panel");
    else window.localStorage.setItem("lumine.panel", panel);
  } catch {
    // A webview with storage disabled loses the preference. That is not worth
    // a message and must not break the toggle.
  }
}
