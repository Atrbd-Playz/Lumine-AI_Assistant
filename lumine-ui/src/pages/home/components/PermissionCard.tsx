import { useEffect, useState } from "react";
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
 * ## Why the clock is visible
 *
 * An approval expires (60s by default) and silence is a refusal. Hiding that
 * would show a button that stops working for no visible reason; showing the
 * seconds makes "you did not answer" legible instead of mysterious. When the
 * clock runs out the worker has already refused, and the card is swept by
 * `Home.tsx` rather than lingering as a lie.
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

  if (requests.length === 0) return null;

  return (
    <div className="permission-stack" role="group" aria-label="Approval requests">
      {requests.map((request) => {
        const secondsLeft = Math.max(0, Math.ceil(request.expiresAt - now / 1000));
        const expired = secondsLeft <= 0;
        return (
          <section key={request.id} className="permission-card" aria-live="assertive">
            <header className="permission-header">
              <span className={"permission-class is-" + request.actionClass}>
                {CLASS_LABEL[requestClass(request)]}
              </span>
              <span className="permission-clock" title="Time left to answer">
                {expired ? "refusing…" : `${secondsLeft}s`}
              </span>
            </header>

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
          </section>
        );
      })}
    </div>
  );
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