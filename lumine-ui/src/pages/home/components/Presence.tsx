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

const stateEmotion: Record<LumineState, LumineEmotion> = { idle: "neutral", listening: "neutral", thinking: "thinking", speaking: "neutral" };
const stateActivity: Record<LumineState, LumineActivity> = { idle: "none", listening: "listening", thinking: "none", speaking: "speaking" };

export function Presence({ state, cursorGaze, showAvatarColor, emotion: emotionIntent, showEmotionDebug }: { state: LumineState; cursorGaze: boolean; showAvatarColor: boolean; emotion: LumineEmotionIntent | null; showEmotionDebug: boolean }) {
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

  useEffect(() => {
    if (!emotionIntent) {
      return;
    }

    const holdMs = Math.max(1800, emotionIntent.durationMs ?? DEFAULT_REACTION_HOLD_MS);
    setReaction(emotionIntent);
    const timer = window.setTimeout(() => setReaction(null), holdMs);
    return () => window.clearTimeout(timer);
  }, [emotionIntent]);

  useEffect(() => {
    if (showEmotionDebug && emotionIntent) console.info("[Emotion] Home Presence updated", emotionIntent);
  }, [emotionIntent, showEmotionDebug]);

  useEffect(() => {
    engineRef.current?.setEmotion(emotion, reaction?.intensity ?? 0);
    engineRef.current?.setActivity(activity);
    engineRef.current?.setEyeMode(recommendedEyeMode(emotion));
  }, [emotion, activity, state, reaction?.intensity]);

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

  return <div className={`presence presence--${state} ${showAvatarColor ? "" : "presence--no-color"}`} aria-label={`Lumine is ${state}`}>
    <div className="presence-halo halo-one" /><div className="presence-halo halo-two" />
    <div className="presence-orbit orbit-one" /><div className="presence-orbit orbit-two" />
    <div className="presence-body"><object ref={avatarRef} data={lumineAvatar} type="image/svg+xml" aria-label="Lumine's animated avatar" /><ExpressionEffects effect={expressionSpec(emotion).effect} intensity={reaction?.intensity ?? 0} /><div className="presence-glass" /></div>
    {showEmotionDebug && <div className="emotion-debug-overlay"><strong>Emotion: {(reaction?.primary ?? "neutral").toUpperCase()}</strong><span>Intensity: {(reaction?.intensity ?? 0).toFixed(2)}</span><span>Source: {reaction?.source ?? "system"}</span><span>Connection: {state}</span><span>Voice: {activity}</span><span>Last event: {emotionIntent ? "lumine.emotion" : "none"}</span></div>}
  </div>;
}
