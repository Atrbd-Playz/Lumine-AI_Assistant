import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import lumineAvatar from "../assets/lumine_face.svg";
import { type ExpressiveEyeMode } from "../components/avatar/expressiveEyePaths";
import { LumineAvatarEngine } from "../components/avatar/emotionEngine";
import { expressionSpec } from "../components/avatar/expressions";
import { ExpressionEffects } from "../components/avatar/ExpressionEffects";
import { randomIdleAnimation } from "../components/avatar/idleMontage";
import type { LumineActivity, LumineAnimation, LumineEmotion } from "../components/avatar/avatarTypes";
import { Icon } from "./home/components/Icon";

const emotions: LumineEmotion[] = ["idle", "happy", "excited", "curious", "thinking", "confused", "sad", "sleepy", "surprised", "embarrassed", "angry", "focused", "shy", "proud", "worried", "playful", "loving", "wink"];
const animations: LumineAnimation[] = ["blink", "slowBlink", "doubleBlink", "sleepyBlink", "surprisedBlink", "happyBlink", "lookLeft", "lookRight", "curiousTilt", "wiggle", "bounce", "sleepy", "surprise", "peek"];

export default function AvatarLabPage() {
  const avatarRef = useRef<HTMLObjectElement>(null);
  const engineRef = useRef<LumineAvatarEngine | null>(null);
  const [emotion, setEmotion] = useState<LumineEmotion>("idle");
  const [activity, setActivity] = useState<LumineActivity>("none");
  const [mode, setMode] = useState<"canonical" | ExpressiveEyeMode>("canonical");
  const [montageOn, setMontageOn] = useState(false);
  const [debugAnimation, setDebugAnimation] = useState<"idle" | LumineAnimation>("idle");

  useEffect(() => {
    const avatar = avatarRef.current;
    if (!avatar) return;
    const engine = new LumineAvatarEngine(avatar);
    engineRef.current = engine;
    const setup = () => engine.connect();
    avatar.addEventListener("load", setup);
    if (avatar.contentDocument) setup();
    return () => { avatar.removeEventListener("load", setup); engine.destroy(); engineRef.current = null; };
  }, []);

  useEffect(() => { engineRef.current?.setEmotion(emotion, 0.8); engineRef.current?.setActivity(activity); engineRef.current?.setEyeMode(mode === "canonical" ? expressionSpec(emotion).eyeMode : mode); }, [emotion, activity, mode]);
  useEffect(() => {
    if (!montageOn) return;
    let timeout = 0;
    const next = () => { const animation = randomIdleAnimation(); setDebugAnimation(animation); void engineRef.current?.play(animation).finally(() => { timeout = window.setTimeout(next, 1600 + Math.random() * 2800); }); };
    timeout = window.setTimeout(next, 500);
    return () => window.clearTimeout(timeout);
  }, [montageOn]);

  const play = (animation: LumineAnimation) => { setMontageOn(false); setDebugAnimation(animation); void engineRef.current?.play(animation, 2).finally(() => setDebugAnimation("idle")); };
  const reset = () => { setEmotion("idle"); setActivity("none"); setMode("canonical"); setMontageOn(false); setDebugAnimation("idle"); };
  const spec = expressionSpec(emotion);
  const activeMode = mode === "canonical" ? spec.eyeMode : mode;

  return <main className="avatar-lab-page" aria-labelledby="avatar-lab-title">
    <header className="workspace-header avatar-lab-header"><div><p className="eyebrow">Lumine workspace · visual instrument</p><h1 id="avatar-lab-title">Avatar Lab</h1><p>Shape the quiet details of Lumine's presence. The original mask animation stays intact while the arc layer gives each emotion a second register.</p></div><button className="workspace-settings" onClick={reset} aria-label="Reset avatar lab" title="Reset avatar lab"><Icon name="reset" size={18} /></button></header>
    <section className="avatar-lab-layout">
      <div className="avatar-lab-stage"><div className="avatar-lab-stage-label"><span className="signal-dot" />Live preview</div><div className="avatar-lab-preview"><div className="avatar-lab-halo" /><div className="avatar-lab-face"><object ref={avatarRef} data={lumineAvatar} type="image/svg+xml" aria-label="Lumine animated avatar" /><ExpressionEffects effect={spec.effect} intensity={0.7} /></div></div><div className="avatar-lab-caption"><strong>{emotion}</strong><span>{activeMode} · {spec.effect} · {activity}</span></div></div>
      <motion.aside className="avatar-lab-controls" initial={{ opacity: 0, x: 14 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.35 }}>
        <div className="avatar-lab-controls-head"><div><p className="eyebrow">Director controls</p><h2>Expression study</h2></div><span className="avatar-lab-live">{debugAnimation}</span></div>
        <LabSection title="Expression" values={emotions} selected={emotion} onSelect={(value) => { setMontageOn(false); setEmotion(value as LumineEmotion); }} />
        <LabSection title="Eye construction" values={["canonical", "mask", "arc"]} selected={mode} onSelect={(value) => setMode(value as "canonical" | ExpressiveEyeMode)} />
        <LabSection title="Activity" values={["none", "listening", "speaking"]} selected={activity} onSelect={(value) => { setMontageOn(false); setActivity(value as LumineActivity); }} />
        <div className="avatar-lab-row"><span>Idle montage</span><button className={montageOn ? "is-selected" : ""} onClick={() => setMontageOn(!montageOn)}>{montageOn ? "ON" : "OFF"}</button></div>
        <LabSection title="Existing animation" values={animations} onSelect={(value) => play(value as LumineAnimation)} />
        <div className="avatar-lab-actions"><button onClick={() => setMontageOn(true)}>Play montage</button><button onClick={() => setMontageOn(false)}>Stop</button><button onClick={() => play(randomIdleAnimation())}>Randomize</button><button onClick={reset}>Reset</button></div>
      </motion.aside>
    </section>
  </main>;
}

function LabSection({ title, values, selected, onSelect }: { title: string; values: string[]; selected?: string; onSelect: (value: string) => void }) {
  return <section className="avatar-lab-section"><span>{title}</span><div>{values.map((value) => <button key={value} className={selected === value ? "is-selected" : ""} onClick={() => onSelect(value)}>{value}</button>)}</div></section>;
}