import { useEffect, useRef } from "react";
import { gapFor, pickMontageAnimation } from "./idleMontage";
import type { LumineAnimation } from "./avatarTypes";
import type { LumineAvatarEngine } from "./emotionEngine";
import type { LumineState } from "../../pages/home/types";

/**
 * The idle montage: Lumine moving on her own, with nobody asking.
 *
 * ## Why this is a driver and not a `play()` call
 *
 * The engine already had a montage — `idleMontage.ts` weighted fourteen
 * animations so the face was never quite the same twice — and nothing called it
 * outside the lab. `connect()` played one `blink` and stopped. So the home
 * screen's avatar was a still image with a slow fade in, which is what "the
 * avatar is not animated" means when somebody says it.
 *
 * A montage is a *loop*, so it has to be a loop. `play()` is a one-shot with a
 * priority, and a driver that awaited it would restart on every state change and
 * lose its place; one that fired it on a timer would stomp on whatever was
 * playing. So the driver runs a queue and steps to the next item when the current
 * one finishes, which is also what makes the timing self-adjusting: a blink takes
 * 300ms and a breeze takes 1.5s, and a fixed interval between them would either
 * cut a breeze off or blink twice in a breath.
 *
 * ## What it yields to, and why each one is a real conflict
 *
 * * **A held reaction.** An emotion event is a thing the agent said, or a thing
 *   that was detected, and it is held for up to three seconds. A montage gesture
 *   starting on top of it reads as the face disagreeing with itself. Worse, a
 *   montage animation at priority 0 would be cancelled by the reaction's own
 *   priority-2 play mid-tween, leaving the motion half-applied.
 * * **Cursor gaze.** Six of the montage's animations move the eyes as part of
 *   their body language. With the pointer driving the gaze, a `tinySway` that
 *   swings the eyes two degrees is a competing claim on the same two pixels, and
 *   the result is a face that jitters between two intentions. Rather than turn
 *   the whole montage off — which would leave the face completely static for
 *   anyone who left gaze on, the default — the driver *filters out* the
 *   gaze-touching animations and keeps the ones that move the body. The face
 *   still lives; it just stops arguing with the pointer.
 * * **Speaking and listening.** The activity overlay is already the most
 *   informative thing on screen during a conversation, and a drift that undoes
 *   the speaking tilt makes the state harder to read. The speaking and listening
 *   montages are shorter and exclude the slow idle drifts for the same reason.
 */

/**
 * The montage's kind is the room's state, verbatim.
 *
 * It used to be a narrower union of its own — four states, with `connecting`,
 * `online` and `error` folded into `idle` and `thinking` by whatever the caller
 * passed. That is the same collapsing the presence state model was fixed for, one
 * layer down, and a driver that silently accepted a state it had no gap for is
 * how the second collapse came back after the first was fixed. Aliasing
 * `LumineState` means a new state fails to compile here too.
 */
type MontageKind = LumineState;

/**
 * Animations that move the eyes, and so conflict with cursor gaze.
 *
 * Declared here rather than inferred from the engine's tween targets: the engine
 * has no table of which animations touch which channel, and adding one for this
 * would be a second source of truth about the same animations.
 *
 * `wonder` and `reaching` are here and were not, because both were authored after
 * this list was written. That is the honest reason this set is hand-maintained
 * and not derived: a derived list would be correct, and it would also be a second
 * place to forget.
 */
const MOVES_EYES: ReadonlySet<LumineAnimation> = new Set<LumineAnimation>([
  "lookLeft",
  "lookRight",
  "tinySway",
  "pauseDrift",
  "peek",
  "wonder",
  "reaching",
]);

export type MontageOptions = {
  engine: LumineAvatarEngine | null;
  /** Which montage to play. The conversation's state picks it. */
  kind: MontageKind;
  /**
   * Whether to play at all. False while a reaction is held, and false when the
   * engine is not connected yet.
   */
  paused: boolean;
  /** Whether the pointer is driving the gaze, which changes what may play. */
  cursorGaze: boolean;
};

export function useAvatarMontage({ engine, kind, paused, cursorGaze }: MontageOptions): void {
  // A ref rather than state: this must not cause a render, and the effect that
  // starts the loop should not re-run because a timer fired.
  const kindRef = useRef(kind);
  kindRef.current = kind;
  // A ref for the same reason, and because the scheduler needs to know what it
  // just played to avoid playing it again. The picker owns the weighting; this
  // owns only the one fact it cannot know.
  const lastRef = useRef<LumineAnimation | null>(null);

  useEffect(() => {
    if (!engine || paused) return;

    let cancelled = false;
    let timer = 0;
    let running = false;

    const tick = async () => {
      if (cancelled || running) return;
      running = true;
      try {
        const current = kindRef.current;
        const drawn = pickMontageAnimation(current, lastRef.current);
        // No group is entirely gaze-driven today, but a future one could be, and
        // an empty pool would hand `play` an undefined animation and land in its
        // `else` branch — the surprise reaction. So the drawn behaviour is checked
        // and, if the pointer owns the eyes, the state is downgraded to `idle`,
        // whose pool is body-led. Being visibly wrong for a moment beats being
        // startled.
        const next = cursorGaze && MOVES_EYES.has(drawn) ? pickMontageAnimation("idle", lastRef.current) : drawn;
        lastRef.current = next;
        // A state change mid-gesture does not cut this one short — `play` is
        // atomic and re-entry is guarded above — but the *next* one comes from the
        // new state, because `kindRef` is read when the behaviour is drawn.
        await engine.play(next, 0);
      } catch {
        // A montage that throws would stop the loop and leave the face frozen
        // for the rest of the session, which is the exact symptom this was meant
        // to fix. The gap is the price of carrying on.
      } finally {
        running = false;
        if (!cancelled) timer = window.setTimeout(tick, gapFor(kindRef.current));
      }
    };

    timer = window.setTimeout(tick, gapFor(kind));
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [engine, paused, cursorGaze]);
}
