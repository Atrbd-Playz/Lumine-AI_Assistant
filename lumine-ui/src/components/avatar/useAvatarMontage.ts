import { useEffect, useRef } from "react";
import { getMontageSequence, randomIdleAnimation } from "./idleMontage";
import type { LumineAnimation } from "./avatarTypes";
import type { LumineAvatarEngine } from "./emotionEngine";

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

type MontageKind = "idle" | "listening" | "thinking" | "speaking";

/**
 * Animations that move the eyes, and so conflict with cursor gaze.
 *
 * Declared here rather than inferred from the engine's tween targets: the engine
 * has no table of which animations touch which channel, and adding one for this
 * would be a second source of truth about the same animations.
 */
const MOVES_EYES: ReadonlySet<LumineAnimation> = new Set<LumineAnimation>([
  "lookLeft",
  "lookRight",
  "tinySway",
  "pauseDrift",
  "peek",
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

/** How long to sit still between gestures, in ms, per montage. */
const GAP: Record<MontageKind, [number, number]> = {
  // Idle is a long wait. A face that fidgets is a face you notice, and the point
  // of this screen is that it is there without asking for anything.
  idle: [2600, 6200],
  // Listening has to read as attentive, so it moves more often but less far.
  listening: [1200, 2600],
  // Thinking is the one state where a still face would be a bug: the pause is
  // the signal, and a long one is indistinguishable from a dropped connection.
  thinking: [1800, 3400],
  // Speaking is busy already. The activity overlay carries it.
  speaking: [2400, 4800],
};

function gapFor(kind: MontageKind): number {
  const [low, high] = GAP[kind];
  return low + Math.random() * (high - low);
}

export function useAvatarMontage({ engine, kind, paused, cursorGaze }: MontageOptions): void {
  // A ref rather than state: this must not cause a render, and the effect that
  // starts the loop should not re-run because a timer fired.
  const kindRef = useRef(kind);
  kindRef.current = kind;

  useEffect(() => {
    if (!engine || paused) return;

    let cancelled = false;
    let timer = 0;
    let running = false;

    const tick = async () => {
      if (cancelled || running) return;
      running = true;
      try {
        const sequence = getMontageSequence(kindRef.current);
        // Weighted random, and the group is the sequence, not a shuffle of the
        // whole catalog. The group is what keeps a thinking face from doing a
        // `wiggle`; the weighting is what stops it doing the same gesture twice
        // in a row, which is the one thing that makes an avatar look automated.
        const allowed = sequence.filter((animation) => !cursorGaze || !MOVES_EYES.has(animation));
        // No group is entirely gaze-driven today, but a future one could be, and
        // an empty pool would hand `play` an undefined animation and land in its
        // `else` branch — the surprise reaction. Falling back to the unfiltered
        // group is visibly wrong for a moment; being startled is worse.
        const pool = allowed.length > 0 ? allowed : sequence;
        // Draw from the weighted list, then keep it only if the filter allowed
        // it. Sampling the pool uniformly instead would make every allowed
        // gesture equally likely, and the weights are the whole reason a blink
        // outnumbers a `wiggle` eight to one.
        const drawn = randomIdleAnimation();
        const next = pool.includes(drawn) ? drawn : pool[Math.floor(Math.random() * pool.length)];
        // A state change mid-gesture does not cut this one short — `play` is
        // atomic and re-entry is guarded above — but the *next* one comes from
        // the new state, because `kindRef` is read when the queue is drawn.
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
