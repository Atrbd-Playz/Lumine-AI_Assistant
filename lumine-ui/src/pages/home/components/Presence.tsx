import { useEffect, useRef, useState, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import lumineAvatar from "../../../assets/lumine_face.svg";
import { LumineAvatarEngine } from "../../../components/avatar/emotionEngine";
import { randomIdleAnimation } from "../../../components/avatar/idleMontage";
import type { LumineActivity, LumineAnimation, LumineEmotion } from "../../../components/avatar/avatarTypes";
import type { LumineState } from "../types";
import { clamp } from "../utils";

const emotions: LumineEmotion[] = ["idle", "happy", "excited", "curious", "thinking", "confused", "sad", "sleepy", "surprised", "embarrassed", "angry", "focused", "shy", "proud", "worried", "playful"];
const animations: LumineAnimation[] = ["blink", "slowBlink", "doubleBlink", "sleepyBlink", "surprisedBlink", "happyBlink", "lookLeft", "lookRight", "curiousTilt", "wiggle", "bounce", "sleepy", "surprise", "peek"];
const stateEmotion: Record<LumineState, LumineEmotion> = { idle: "idle", listening: "curious", thinking: "thinking", speaking: "excited" };
const stateActivity: Record<LumineState, LumineActivity> = { idle: "none", listening: "listening", thinking: "none", speaking: "speaking" };

export function Presence({ state, cursorGaze, showAvatarColor }: { state: LumineState; cursorGaze: boolean; showAvatarColor: boolean }) {
  const avatarRef = useRef<HTMLObjectElement>(null);
  const engineRef = useRef<LumineAvatarEngine | null>(null);
  const montageRef = useRef(false);
  const [labOpen, setLabOpen] = useState(false);
  const [montageOn, setMontageOn] = useState(true);
  const [debugAnimation, setDebugAnimation] = useState<"idle" | LumineAnimation>("idle");
  const [emotionOverride, setEmotionOverride] = useState<LumineEmotion | null>(null);
  const [activityOverride, setActivityOverride] = useState<LumineActivity | null>(null);
  const emotion = emotionOverride ?? stateEmotion[state];
  const activity = activityOverride ?? stateActivity[state];

  useEffect(() => {
    const avatar = avatarRef.current;
    if (!avatar) return;
    const engine = new LumineAvatarEngine(avatar);
    engineRef.current = engine;
    const setup = () => engine.connect();
    avatar.addEventListener("load", setup);
    if (avatar.contentDocument) setup();
    const follow = (event: MouseEvent) => {
      if (!cursorGaze) { engine.setGaze(0, 0); return; }
      const rect = avatar.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      engine.setGaze(clamp((event.clientX - (rect.left + rect.width / 2)) / (rect.width / 2), -1, 1) * 5, clamp((event.clientY - (rect.top + rect.height / 2)) / (rect.height / 2), -1, 1) * 4);
    };
    window.addEventListener("mousemove", follow, { passive: true });
    return () => { avatar.removeEventListener("load", setup); window.removeEventListener("mousemove", follow); engine.destroy(); engineRef.current = null; };
  }, [cursorGaze]);

  useEffect(() => { engineRef.current?.setEmotion(emotion); engineRef.current?.setActivity(activity); }, [emotion, activity]);

  useEffect(() => {
    if (!montageOn) return;
    let timeout = 0;
    const next = () => {
      if (!montageRef.current) return;
      const animation = randomIdleAnimation();
      setDebugAnimation(animation);
      void engineRef.current?.play(animation).finally(() => { timeout = window.setTimeout(next, 1600 + Math.random() * 2800); });
    };
    montageRef.current = true;
    timeout = window.setTimeout(next, 1500);
    return () => { montageRef.current = false; window.clearTimeout(timeout); };
  }, [montageOn]);

  const play = (animation: LumineAnimation) => { setMontageOn(false); setDebugAnimation(animation); void engineRef.current?.play(animation, 2).finally(() => setDebugAnimation("idle")); };
  const replayMontage = () => { setMontageOn(false); window.setTimeout(() => setMontageOn(true), 20); };
  const randomize = () => play(randomIdleAnimation());

  return <div className={`presence presence--${state} ${showAvatarColor ? "" : "presence--no-color"}`} aria-label={`Lumine is ${state}`}>
    <div className="presence-halo halo-one" /><div className="presence-halo halo-two" />
    <div className="presence-orbit orbit-one" /><div className="presence-orbit orbit-two" />
    <div className="presence-body"><object ref={avatarRef} data={lumineAvatar} type="image/svg+xml" aria-label="Lumine's animated avatar" /><div className="presence-glass" /></div>
    <button className="avatar-lab-trigger" onClick={() => setLabOpen((open) => !open)} aria-expanded={labOpen}>✦ Avatar Lab</button>
    {labOpen && createPortal(<AvatarLab emotion={emotion} activity={activity} montageOn={montageOn} debugAnimation={debugAnimation} onEmotion={(value) => { setMontageOn(false); setEmotionOverride(value); engineRef.current?.setEmotion(value); }} onActivity={(value) => { setMontageOn(false); setActivityOverride(value); engineRef.current?.setActivity(value); }} onMontage={setMontageOn} onPlay={play} onReplay={replayMontage} onRandomize={randomize} onReset={() => { setEmotionOverride(null); setActivityOverride(null); engineRef.current?.setEmotion(stateEmotion[state]); engineRef.current?.setActivity(stateActivity[state]); setDebugAnimation("idle"); }} />, document.body)}
  </div>;
}

function AvatarLab({ emotion, activity, montageOn, debugAnimation, onEmotion, onActivity, onMontage, onPlay, onReplay, onRandomize, onReset }: { emotion: LumineEmotion; activity: LumineActivity; montageOn: boolean; debugAnimation: "idle" | LumineAnimation; onEmotion: (value: LumineEmotion) => void; onActivity: (value: LumineActivity) => void; onMontage: (value: boolean) => void; onPlay: (value: LumineAnimation) => void; onReplay: () => void; onRandomize: () => void; onReset: () => void }) {
  const [position, setPosition] = useState({ x: 0, y: 0 });
  const dragRef = useRef<{ x: number; y: number; startX: number; startY: number } | null>(null);
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    dragRef.current = { x: position.x, y: position.y, startX: event.clientX, startY: event.clientY };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    setPosition({ x: dragRef.current.x + event.clientX - dragRef.current.startX, y: dragRef.current.y + event.clientY - dragRef.current.startY });
  };
  const stopDragging = () => { dragRef.current = null; };
  return <aside className="avatar-lab" style={{ transform: `translate(${position.x}px, ${position.y}px)` }} aria-label="Lumine avatar animation lab">
    <div className="avatar-lab-head" onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={stopDragging}><strong>Lumine Avatar Lab</strong><span>drag to move · live SVG</span></div>
    <p className="avatar-lab-state">Emotion: <b>{emotion}</b> · Activity: <b>{activity}</b> · Animation: <b>{debugAnimation}</b></p>
    <LabSection title="Emotion" values={emotions} selected={emotion} onSelect={(value) => onEmotion(value as LumineEmotion)} />
    <LabSection title="Activity" values={["none", "listening", "speaking"]} selected={activity} onSelect={(value) => onActivity(value as LumineActivity)} />
    <div className="avatar-lab-row"><span>Idle montage</span><button className={montageOn ? "is-selected" : ""} onClick={() => onMontage(!montageOn)}>{montageOn ? "ON" : "OFF"}</button></div>
    <LabSection title="Animation" values={animations} onSelect={(value) => onPlay(value as LumineAnimation)} />
    <div className="avatar-lab-actions"><button onClick={onReplay}>▶ Play Montage</button><button onClick={() => onMontage(false)}>Stop</button><button onClick={onRandomize}>Randomize</button><button onClick={onReset}>Reset</button></div>
  </aside>;
}

function LabSection({ title, values, selected, onSelect }: { title: string; values: string[]; selected?: string; onSelect: (value: string) => void }) {
  return <section className="avatar-lab-section"><span>{title}</span><div>{values.map((value) => <button key={value} className={selected === value ? "is-selected" : ""} onClick={() => onSelect(value)}>{value}</button>)}</div></section>;
}
