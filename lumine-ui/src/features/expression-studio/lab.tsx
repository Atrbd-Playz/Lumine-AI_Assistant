import { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import { LumineAvatarEngine } from "../../components/avatar/emotionEngine";
import { expressionSpec } from "../../components/avatar/expressions";
import { ExpressionEffects } from "../../components/avatar/ExpressionEffects";
import { CANONICAL_EMOTIONS, EmotionController, buildEmotionEvent } from "../emotion/emotion-controller";
import type { LumineEmotion, LumineEmotionIntent, LumineEmotionSource } from "../../components/avatar/avatarTypes";
import lumineAvatar from "../../assets/lumine_face.svg";

const DEFAULT_EMOTION: LumineEmotion = "neutral";

export function ExpressionLab() {
  const avatarRef = useRef<HTMLObjectElement | null>(null);
  const engineRef = useRef<LumineAvatarEngine | null>(null);
  const controllerRef = useRef(new EmotionController());
  const [primary, setPrimary] = useState<LumineEmotion>(DEFAULT_EMOTION);
  const [secondary, setSecondary] = useState<LumineEmotion | undefined>(undefined);
  const [intensity, setIntensity] = useState(0.8);
  const [source, setSource] = useState<LumineEmotionSource>("lab");
  const [current, setCurrent] = useState<LumineEmotionIntent>({ primary: DEFAULT_EMOTION, intensity: 0.5, source: "lab" });
  const [previous, setPrevious] = useState<LumineEmotionIntent | undefined>(undefined);
  const [transitionProgress, setTransitionProgress] = useState(0);
  const [state, setState] = useState("idle");
  const [modeOverride, setModeOverride] = useState<"canonical" | "mask" | "arc">("canonical");
  const spec = expressionSpec(primary);
  const activeMode = modeOverride === "canonical" ? spec.eyeMode : modeOverride;

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

  const triggerEmotion = (nextPrimary: LumineEmotion, nextSecondary?: LumineEmotion) => {
    const intent = controllerRef.current.trigger({
      primary: nextPrimary,
      secondary: nextSecondary,
      intensity,
      source,
      priority: 5,
    });
    const event = buildEmotionEvent(intent);
    setPrevious(controllerRef.current.getPrevious());
    setCurrent(intent);
    if (engineRef.current) {
      engineRef.current.setEmotion(nextPrimary, intensity);
      engineRef.current.setEyeMode(modeOverride === "canonical" ? expressionSpec(nextPrimary).eyeMode : modeOverride);
      engineRef.current.setActivity(nextPrimary === "thinking" ? "none" : "speaking");
    }
    setTransitionProgress(0.65);
    setState(event ? "triggered" : "idle");
    if (event) {
      console.info("lab emotion event", event);
    }
  };

  useEffect(() => {
    if (!engineRef.current) return;
    triggerEmotion(primary, secondary);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [primary, secondary, intensity, source, modeOverride]);

  const emotionGrid = useMemo<LumineEmotion[]>(() => CANONICAL_EMOTIONS.filter((emotion) => emotion !== "idle"), []);

  return <main style={{ padding: 24, color: "#f5f1ea", background: "#120f0d" }}>
    <div style={{ display: "grid", gridTemplateColumns: "1.3fr 0.7fr", gap: 20 }}>
      <section style={{ border: "1px solid rgba(255,255,255,0.12)", borderRadius: 20, background: "rgba(255,255,255,0.02)", padding: 20 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <div>
            <p style={{ margin: 0, fontSize: 12, letterSpacing: "0.18em", textTransform: "uppercase", opacity: 0.7 }}>Expression Playground</p>
            <h2 style={{ margin: "8px 0 0", fontSize: 28 }}>Lumine Emotion Lab</h2>
          </div>
        </div>

        <div style={{ display: "flex", justifyContent: "center", padding: 20, borderRadius: 18, background: "radial-gradient(circle at 50% 35%, rgba(255,174,103,0.25), transparent 50%)" }}>
          <div style={{ width: 280, height: 300, display: "grid", placeItems: "center" }}>
            <div style={{ position: "relative", width: 220, height: 220 }}><object ref={avatarRef} data={lumineAvatar} type="image/svg+xml" aria-label="Lumine preview" style={{ width: 220, height: 220 }} /><ExpressionEffects effect={expressionSpec(current.primary).effect} intensity={intensity} /></div>
          </div>
        </div>

        <div style={{ marginTop: 12 }}>
          <p style={{ margin: 0, opacity: 0.7 }}>Current emotion</p>
          <strong style={{ fontSize: 24 }}>{current.primary}</strong>
          <div style={{ marginTop: 8, display: "grid", gap: 4, fontSize: 13, opacity: 0.8 }}>
            <div>Eye mode: {activeMode}</div>
            <div>Effect: {spec.effect}</div>
          </div>
          <div style={{ marginTop: 10 }}>
            <label style={{ display: "block", marginBottom: 8 }}>Intensity: {intensity.toFixed(2)}</label>
            <input type="range" min={0} max={1} step={0.01} value={intensity} onChange={(event) => setIntensity(Number(event.target.value))} style={{ width: "100%" }} />
          </div>
        </div>
      </section>

      <motion.aside initial={{ opacity: 0, x: 18 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.3 }} style={{ border: "1px solid rgba(255,255,255,0.12)", borderRadius: 20, background: "rgba(255,255,255,0.02)", padding: 20 }}>
        <div style={{ marginBottom: 12 }}>
          <p style={{ margin: 0, fontSize: 12, letterSpacing: "0.18em", textTransform: "uppercase", opacity: 0.7 }}>Emotion selector</p>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 8 }}>
          {emotionGrid.map((emotion: LumineEmotion) => (
            <button key={emotion} onClick={() => setPrimary(emotion)} style={{ padding: "8px 10px", borderRadius: 10, background: primary === emotion ? "rgba(255,148,92,0.22)" : "rgba(255,255,255,0.04)", border: primary === emotion ? "1px solid rgba(255,148,92,0.8)" : "1px solid rgba(255,255,255,0.08)", color: "#f5f1ea", cursor: "pointer" }}>
              {emotion}
            </button>
          ))}
        </div>

        <div style={{ marginTop: 18 }}>
          <label style={{ display: "block", marginBottom: 8 }}>Eye construction</label>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6 }}>
            {(["canonical", "mask", "arc"] as const).map((mode) => <button key={mode} onClick={() => setModeOverride(mode)} style={{ padding: "7px 5px", borderRadius: 8, background: modeOverride === mode ? "rgba(255,148,92,0.22)" : "rgba(255,255,255,0.04)", border: modeOverride === mode ? "1px solid rgba(255,148,92,0.8)" : "1px solid rgba(255,255,255,0.08)", color: "#f5f1ea", cursor: "pointer" }}>{mode}</button>)}
          </div>
        </div>

        <div style={{ marginTop: 18 }}>
          <label style={{ display: "block", marginBottom: 8 }}>Secondary emotion</label>
          <select value={secondary ?? ""} onChange={(event) => setSecondary(event.target.value ? (event.target.value as LumineEmotion) : undefined)} style={{ width: "100%", padding: 10, borderRadius: 10, background: "#1b1713", color: "#f5f1ea", border: "1px solid rgba(255,255,255,0.12)" }}>
            <option value="">None</option>
            {emotionGrid.map((emotion: LumineEmotion) => <option key={emotion} value={emotion}>{emotion}</option>)}
          </select>
        </div>

        <div style={{ marginTop: 18 }}>
          <label style={{ display: "block", marginBottom: 8 }}>Source</label>
          <select value={source} onChange={(event) => setSource(event.target.value as LumineEmotionSource)} style={{ width: "100%", padding: 10, borderRadius: 10, background: "#1b1713", color: "#f5f1ea", border: "1px solid rgba(255,255,255,0.12)" }}>
            {(["llm", "voice", "system", "user", "lab"] as const).map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </div>

        <div style={{ marginTop: 18 }}>
          <p style={{ margin: 0, opacity: 0.7 }}>Debug panel</p>
          <div style={{ marginTop: 8, padding: 10, borderRadius: 12, background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)", fontSize: 13 }}>
            <div>Current: {current.primary}</div>
            <div>Previous: {previous?.primary ?? "none"}</div>
            <div>Target: {primary}</div>
            <div>Intensity: {intensity.toFixed(2)}</div>
            <div>Secondary: {secondary ?? "none"}</div>
            <div>Source: {source}</div>
            <div>Transition: {transitionProgress.toFixed(2)}</div>
            <div>State: {state}</div>
          </div>
        </div>
      </motion.aside>
    </div>
  </main>;
}
