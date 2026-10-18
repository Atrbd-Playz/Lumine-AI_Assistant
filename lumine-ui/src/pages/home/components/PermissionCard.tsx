import { useEffect, useRef, useState } from "react";
import type { VoicePermissionRequest } from "../../../features/voice/voice-manager";

/**
 * The approval question, as a card with two answers and a clock.
 *
 * ## Why a card and not only a sentence
 *
 * Lumine now *speaks* the question, so this is no longer the only way to answer
 * it. The card remains because of what it can carry and a sentence cannot: the
 * exact recipient, the exact wording, and the countdown. "Send a message to Afia"
 * spoken aloud is a paraphrase the user has to trust; "to Afia Rahman / I'll be
 * there in 20 minutes" on screen is not. The two channels resolve the same
 * request id, so answering twice still runs the action once.
 *
 * The spoken text and `request.question` are the *same sentence* — the worker
 * builds both from one function. This card renders that field rather than
 * composing its own wording, because a card that paraphrased the action would be a
 * second place where "what am I approving" is decided, and the user reads it while
 * deciding.
 *
 * ## Why the clock is visible, and draining
 *
 * An approval expires (60s by default) and silence is a refusal. Hiding that
 * would show a button that stops working for no visible reason; showing the
 * seconds makes "you did not answer" legible instead of mysterious. The bar is
 * the same number without the arithmetic — it drains, it is the last thing on
 * the card, and it turns red under ten seconds because that is the point where
 * the request stops being a warning. When the clock runs out the worker has
 * already refused, and the card is swept by `Home.tsx` rather than lingering as
 * a lie.
 *
 * A request that carries no deadline at all (`expiresAt` of 0, which an older
 * worker may send) gets no clock and no bar rather than an instantly-drained
 * one, and it is never swept: "no deadline" is a fact about the worker, not a
 * request that has already been refused.
 *
 * ## Why nothing here decides anything
 *
 * The card does not evaluate the request. It renders the class the worker
 * declared in code and sends the id back verbatim; whether the id is pending,
 * in-window, and this session's is the worker's question. A UI that reasoned
 * about "is this sensitive?" would be a second, divergent copy of the policy
 * -- and this one would be the copy an attacker could reach.
 */

const CLASS_LABEL: Record<VoicePermissionRequest["actionClass"], string> = {
  read: "Reads information",
  act: "Acts on this device",
  sensitive: "Sensitive action",
  destructive: "Destructive action",
};

/** Anything the gate will ask about, in words a person can act on. */
const CONSEQUENCE: Record<VoicePermissionRequest["actionClass"], string> = {
  read: "Nothing is changed.",
  act: "Something on this computer changes.",
  sensitive: "This cannot be undone from here.",
  destructive: "This cannot be undone at all.",
};

/**
 * The seconds under which the card stops warning and starts counting down.
 *
 * Ten, and not five: the card's own copy says a sixty-second window, so a bar
 * that only turned at five would be red for a twentieth of the time it was
 * draining and nobody would ever see the change.
 */
const URGENT_SECONDS = 10;

/**
 * Tool ids are our vocabulary, and the title is the one place on this card that
 * is allowed to translate rather than quote. `send_whatsapp` is a name for the
 * worker's benefit; a person is being asked whether to send a message.
 *
 * Only the spellings worth overriding — everything else is title-cased, because
 * inventing a friendlier name per tool would put a second description of each
 * action in the UI, next to the one the worker wrote.
 */
const PROPER_NOUNS: Record<string, string> = {
  whatsapp: "WhatsApp",
  web: "Web",
  url: "URL",
};

export type PermissionCardProps = {
  /** Every request the worker is still waiting on, oldest first. */
  requests: VoicePermissionRequest[];
  /** Resolves to whether the answer was delivered; a failure keeps the card. */
  onAnswer: (id: string, decision: "approve" | "deny") => Promise<boolean>;
};

export function PermissionCard({ requests, onAnswer }: PermissionCardProps) {
  // One clock for the whole stack. A timer per card would be a timer per
  // request, and these arrive in bursts when the model calls two tools in one
  // step; one interval keeps them agreeing about what "now" is.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (requests.length === 0) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [requests.length]);

  /**
   * The longest deadline seen so far, as one number.
   *
   * The bar is a *fraction*, and a fraction needs the whole it is a fraction of.
   * The request carries only its deadline, so the length is observed here from
   * the first moment this component sees the request — and it may only ever
   * grow, because an early render must not be able to shorten a bar that a
   * later one would have shown full.
   *
   * One scalar rather than a map keyed by id: the worker's timeout is a single
   * configured number per session, so a map would be several copies of the same
   * value plus the bookkeeping to drop them when a request leaves.
   */
  const maxWindow = useRef(0);

  if (requests.length === 0) return null;

  return (
    <div className="permission-stack" role="group" aria-label="Approval requests">
      {requests.map((request) => {
        const seconds = request.expiresAt > 0 ? Math.max(0, request.expiresAt - now / 1000) : 0;
        if (seconds > maxWindow.current) maxWindow.current = seconds;
        const windowSeconds = maxWindow.current;
        const hasDeadline = request.expiresAt > 0;
        const expired = hasDeadline && seconds <= 0;
        const urgent = hasDeadline && seconds > 0 && seconds <= URGENT_SECONDS;
        const fill =
          hasDeadline && windowSeconds > 0 ? Math.min(100, (seconds / windowSeconds) * 100) : 0;

        return (
          <section
            key={request.id}
            className={`permission-card${urgent || expired ? " is-urgent" : ""}`}
            aria-live="assertive"
          >
            <header className="permission-header">
              <span className={"permission-class is-" + requestClass(request)}>
                {/* The dot is the periphery half of what the label says in text:
                    read from the corner of the eye while the word is read
                    straight on. A coloured word alone makes the colour the
                    signal, which fails for anyone who cannot see it. */}
                <span className="permission-dot" aria-hidden="true" />
                {CLASS_LABEL[requestClass(request)]}
              </span>
              {hasDeadline && (
                <span
                  className={`permission-clock${urgent || expired ? " is-urgent" : ""}`}
                  title="Time left to answer"
                >
                  {expired ? "refusing…" : `${Math.ceil(seconds)}s`}
                </span>
              )}
            </header>

            {/* Which tool is asking. Quoted nowhere else, and without it the
                card cannot say whether it came from a browser action or from a
                message being sent. */}
            <p className="permission-title m-0">{headline(request)}</p>

            {/* The question Lumine is speaking, word for word. */}
            <p className="permission-question m-0">{request.question}</p>

            {/* What it acts on, set apart from the sentence above so it reads as
                a value to check rather than as more prose. For a message this is
                the recipient; for a browser action it is the address. */}
            {request.target && (
              <div className="permission-detail">
                <span className="permission-detail-label">On</span>
                <code className="permission-target">{request.target}</code>
              </div>
            )}

            <p className="permission-consequence m-0">{CONSEQUENCE[requestClass(request)]}</p>

            <div className="permission-actions">
              <span className="permission-hint">Or say yes / no out loud.</span>
              <button
                type="button"
                className="permission-deny"
                disabled={expired}
                onClick={() => void onAnswer(request.id, "deny")}
              >
                Not now
              </button>
              <button
                type="button"
                className="permission-allow"
                disabled={expired}
                onClick={() => void onAnswer(request.id, "approve")}
              >
                Allow
              </button>
            </div>

            {/* The deadline, without the arithmetic. Absent when the request
                carries none, because an empty bar would read as "no time left"
                for a request that has all of it. Absolutely positioned so the
                card's height does not change with a value that changes every
                second. */}
            {hasDeadline && (
              <span className="permission-bar" aria-hidden="true">
                <span className="permission-bar-fill" style={{ width: `${fill}%` }} />
              </span>
            )}
          </section>
        );
      })}
    </div>
  );
}

/** `send_whatsapp` -> `Send WhatsApp`. */
function headline(request: VoicePermissionRequest): string {
  return request.tool
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((word) => PROPER_NOUNS[word.toLowerCase()] ?? word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}

/**
 * The class to label, narrowed at the last moment.
 *
 * `aria-live="assertive"` on the card means this text is *announced*, so it is the
 * one part of the UI a screen reader reads on its own. That is the right place for
 * "sensitive" rather than the tool name: a voice user who cannot see the card
 * should still hear that this is the one that needs a decision.
 */
function requestClass(request: VoicePermissionRequest): VoicePermissionRequest["actionClass"] {
  return request.actionClass in CLASS_LABEL ? request.actionClass : "sensitive";
}
