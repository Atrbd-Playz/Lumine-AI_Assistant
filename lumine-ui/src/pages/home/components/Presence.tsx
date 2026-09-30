import { useEffect, useRef, useState } from "react";
import lumineAvatar from "../../../assets/lumine_face.svg";
import { LumineAvatarEngine } from "../../../components/avatar/emotionEngine";
import { useAvatarMontage } from "../../../components/avatar/useAvatarMontage";
import { recommendedEyeMode } from "../../../components/avatar/expressiveEyePaths";
import { expressionSpec } from "../../../components/avatar/expressions";
import { ExpressionEffects } from "../../../components/avatar/ExpressionEffects";
import type { LumineActivity, LumineEmotion, LumineEmotionIntent } from "../../../components/avatar/avatarTypes";
import type { LumineState } from "../types";
import { clamp } from "../utils";

const deadzone = 0.06;
const gazeSmoothing = 0.18;
const gazeRangeX = 11.5;
const gazeRangeY = 7.5;
const DEFAULT_REACTION_HOLD_MS = 3000;

/**
 * What each state looks like, and what it is doing.
 *
 * Total over `LumineState`, so a new state without a face is a compile error here
 * rather than an `undefined` that silently falls through to the neutral default.
 * These two tables were four entries long while the state model had seven, and
 * that gap *was* the bug: the three new states reached these tables and got
 * `undefined`, so connecting and connected drew the same resting face as never
 * having pressed the button.
 *
 * The states that mattered most are the two that were missing rather than wrong.
 * `connecting` is anticipation, so it is a searching curiosity — a face that looks
 * thoughtful while the room is still opening is telling the user it has already
 * begun working. `error` is a downcast rather than a neutral, because a failed
 * call that looks exactly like a resting one is indistinguishable from a call
 * nobody made.
 */
const stateEmotion: Record<LumineState, LumineEmotion> = {
  idle: "neutral",
  connecting: "curious",
  online: "happy",
  listening: "neutral",
  thinking: "thinking",
  speaking: "neutral",
  error: "worried",
};

const stateActivity: Record<LumineState, LumineActivity> = {
  idle: "none",
  // No overlay while connecting. The overlay is a speaking/listening indicator, and
  // raising it before either is true claims something the room has not confirmed.
  // The `reaching` gesture carries this state instead.
  connecting: "none",
  online: "listening",
  listening: "listening",
  thinking: "none",
  speaking: "speaking",
  error: "none",
};

export type PresenceProps = {
  state: LumineState;
  cursorGaze: boolean;
  showAvatarColor: boolean;
  emotion: LumineEmotionIntent | null;
  showEmotionDebug: boolean;
  /**
   * Subscribe to Lumine's real voice amplitude, 0..1. Returns an unsubscribe.
   *
   * Undefined disables the audio channel entirely, and the face then runs on its
   * breath alone — which is what the Avatar Lab does, since a lab preview has no
   * call to be loud in.
   */
  subscribeEnergy?: (onLevel: (level: number) => void) => () => void;
};

export function Presence({ state, cursorGaze, showAvatarColor, emotion: emotionIntent, showEmotionDebug, subscribeEnergy }: PresenceProps) {
  const avatarRef = useRef<HTMLObjectElement>(null);
  const engineRef = useRef<LumineAvatarEngine | null>(null);
  const targetGazeRef = useRef({ x: 0, y: 0 });
  const currentGazeRef = useRef({ x: 0, y: 0 });
  const rafRef = useRef<number | null>(null);
  const [reaction, setReaction] = useState<LumineEmotionIntent | null>(null);
  const emotion = reaction?.primary ?? stateEmotion[state];
  const activity = stateActivity[state];

  useEffect(() => {
    const avatar = avatarRef.current;
    if (!avatar) return;
    const engine = new LumineAvatarEngine(avatar);
    engineRef.current = engine;
    // Published so the montage can start. `connect()` is what discovers the eyes,
    // and a montage that begins before that plays into a face with no eyes to
    // move.
    setEngine(engine);
    const setup = () => {
      if (engine.connect()) setEngine(engine);
    };
    avatar.addEventListener("load", setup);
    if (avatar.contentDocument) setup();

    const follow = (event: MouseEvent) => {
      if (!cursorGaze) {
        targetGazeRef.current = { x: 0, y: 0 };
        return;
      }
      const rect = avatar.getBoundingClientRect();
      if (!rect.width || !rect.height) return;

      const pointerX = clamp((event.clientX - (rect.left + rect.width / 2)) / (rect.width / 2), -1, 1);
      const pointerY = clamp((event.clientY - (rect.top + rect.height / 2)) / (rect.height / 2), -1, 1);
      const motionBoostX = clamp((event.movementX ?? 0) / Math.max(window.innerWidth, 1) * 18, -2.5, 2.5);
      const motionBoostY = clamp((event.movementY ?? 0) / Math.max(window.innerHeight, 1) * 18, -2.5, 2.5);

      const nextX = pointerX * gazeRangeX + motionBoostX;
      const nextY = pointerY * gazeRangeY + motionBoostY;
      const dx = nextX - targetGazeRef.current.x;
      const dy = nextY - targetGazeRef.current.y;
      const distance = Math.hypot(dx, dy);

      if (distance < deadzone * 8) {
        targetGazeRef.current = { x: targetGazeRef.current.x * 0.7, y: targetGazeRef.current.y * 0.7 };
        return;
      }

      targetGazeRef.current = { x: nextX, y: nextY };
    };

    const tick = () => {
      const target = targetGazeRef.current;
      const current = currentGazeRef.current;
      const nextX = current.x + (target.x - current.x) * gazeSmoothing;
      const nextY = current.y + (target.y - current.y) * gazeSmoothing;

      currentGazeRef.current = {
        x: nextX + (target.x - nextX) * 0.16,
        y: nextY + (target.y - nextY) * 0.16,
      };
      engine.setGaze(currentGazeRef.current.x, currentGazeRef.current.y);
      rafRef.current = window.requestAnimationFrame(tick);
    };

    rafRef.current = window.requestAnimationFrame(tick);
    window.addEventListener("mousemove", follow, { passive: true });
    return () => {
      avatar.removeEventListener("load", setup);
      window.removeEventListener("mousemove", follow);
      if (rafRef.current) window.cancelAnimationFrame(rafRef.current);
      engine.destroy();
      engineRef.current = null;
      setEngine(null);
    };
  }, [cursorGaze]);

  // One reaction in, and the clock for it starts later — see the effect below.
  useEffect(() => {
    if (!emotionIntent) return;
    setReaction(emotionIntent);
  }, [emotionIntent]);

  /**
   * When a reaction is *allowed* to end, which is not when it arrives.
   *
   * It used to be: the same effect started an 1800ms timer the moment
   * `lumine.emotion` was handled, so a reaction that arrived at the start of a
   * turn expired mid-sentence — the face snapped back to the resting expression
   * while she was still visibly talking, and then flipped again a moment later
   * when the room's state changed underneath it. Two visible flips for one
   * event, and the first one landed on top of the words being spoken.
   *
   * The hold is now conditional on the thing the hold exists to accompany. While
   * `state === "speaking"` the timer does not run at all: there is no deadline,
   * because the reaction's natural end is the end of her voice. The moment she
   * stops, this effect re-runs with a non-speaking state and *then* starts the
   * clock — so the expression outlives the utterance by `holdMs` rather than
   * racing it.
   *
   * Note the second re-run it also gets: `reaction` itself is a dependency, so a
   * fresh intent arriving mid-hold restarts a timer that was not running anyway
   * and keeps the newest one. Returning early on `speaking` is what makes that
   * harmless rather than a source of flicker.
   */
  useEffect(() => {
    if (!reaction) return;
    if (state === "speaking") return;
    const holdMs = Math.max(1800, reaction.durationMs ?? DEFAULT_REACTION_HOLD_MS);
    const timer = window.setTimeout(() => setReaction(null), holdMs);
    return () => window.clearTimeout(timer);
  }, [reaction, state]);

  useEffect(() => {
    if (showEmotionDebug && emotionIntent) console.info("[Emotion] Home Presence updated", emotionIntent);
  }, [emotionIntent, showEmotionDebug]);

  /**
   * The face's three inputs, applied as one operation.
   *
   * These were three separate calls, and each one ends in `render()` — so a
   * single room-state change painted the avatar three times in the same tick,
   * each pass with a different subset of the new values applied. Nothing user
   * visible ever showed it, because all three landed before the compositor
   * committed, but it made the cost of a state change three times what it is.
   * `setFace` batches them and renders once.
   */
  useEffect(() => {
    engineRef.current?.setFace(emotion, reaction?.intensity ?? 0, activity, recommendedEyeMode(emotion));
  }, [emotion, activity, state, reaction?.intensity]);

  /**
   * The audio channel, and why it is a subscription rather than a prop.
   *
   * The obvious shape — `energy={level}` with `level` in state — is a render on
   * every analyser frame. An RMS updates around 60 times a second, so the entire
   * presence subtree would re-render 60 times a second to move a `<div>`. That
   * is the shape of the bug that makes audio-reactive UI expensive enough that
   * people do not build it.
   *
   * A subscription moves the write off React entirely: the engine's own loop
   * already calls `render()` each frame, so storing a number and letting it read
   * that number is the whole job. No state, no effect, no re-render — the cost of
   * a 60Hz channel becomes one number assignment.
   *
   * The prop is a *subscribe function*, not a level, so this component has no idea
   * the value comes from audio. A local mic meter or a scripted demo plugs into
   * the same signature.
   */
  useEffect(() => {
    if (!subscribeEnergy) return;
    return subscribeEnergy((level) => engineRef.current?.setEnergy(level));
  }, [subscribeEnergy]);

  // The montage, which is what makes the face move with nobody asking.
  //
  // `engineRef.current` is a ref and so does not re-render when the engine is
  // created, which is why `engine` is a piece of state as well: the hook needs to
  // restart when the avatar connects, and it cannot see a ref change.
  const [engine, setEngine] = useState<LumineAvatarEngine | null>(null);
  useAvatarMontage({
    engine,
    kind: state,
    // A held reaction is a thing the agent said. A gesture starting on top of it
    // reads as the face disagreeing with itself.
    paused: reaction !== null,
    cursorGaze,
  });

  /* `role="img"`, because a bare `<div>` with an `aria-label` is named on paper
     and silent in practice: the generic role does not support naming, so the
     label was announced to nobody. As an image the whole thing — avatar, halos
     and orbits — collapses to one thing called "Lumine is listening", which is
     the single piece of information the stage carries that the transcript does
     not. The subtree becoming presentational is the point; none of it was meant
     to be navigated. */
  return <div role="img" className={`presence presence--${state} ${showAvatarColor ? "" : "presence--no-color"}`} aria-label={`Lumine is ${state}`}>
    <div className="presence-halo halo-one" /><div className="presence-halo halo-two" />
    <div className="presence-orbit orbit-one" /><div className="presence-orbit orbit-two" />
    <div className="presence-body"><object ref={avatarRef} data={lumineAvatar} type="image/svg+xml" aria-label="Lumine's animated avatar" /><ExpressionEffects effect={expressionSpec(emotion).effect} intensity={reaction?.intensity ?? 0} /><div className="presence-glass absolute z-[2] inset-px hidden pointer-events-none" /></div>
    {showEmotionDebug && <div className="emotion-debug-overlay"><strong>Emotion: {(reaction?.primary ?? "neutral").toUpperCase()}</strong><span>Intensity: {(reaction?.intensity ?? 0).toFixed(2)}</span><span>Source: {reaction?.source ?? "system"}</span><span>Connection: {state}</span><span>Voice: {activity}</span><span>Last event: {emotionIntent ? "lumine.emotion" : "none"}</span></div>}
  </div>;
}
