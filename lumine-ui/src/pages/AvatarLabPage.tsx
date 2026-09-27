import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import lumineAvatar from "../assets/lumine_face.svg";
import { type ExpressiveEyeMode } from "../components/avatar/expressiveEyePaths";
import { LumineAvatarEngine } from "../components/avatar/emotionEngine";
import { expressionSpec } from "../components/avatar/expressions";
import { ExpressionEffects } from "../components/avatar/ExpressionEffects";
import { randomIdleAnimation } from "../components/avatar/idleMontage";
import { useAvatarMontage } from "../components/avatar/useAvatarMontage";
import type { LumineActivity, LumineAnimation, LumineEmotion } from "../components/avatar/avatarTypes";
import { Dropdown } from "../components/ui/dropdown";
import { Knob } from "../components/ui/knob";
import { Hint } from "../components/ui/hint";
import { Icon } from "./home/components/Icon";

/**
 * The Avatar Lab: a way to look at the face on purpose.
 *
 * ## What is instrumented and why it matters
 *
 * This screen was a wall of forty-one-pixel chips: eighteen emotions, three eye
 * constructions, three activities and fourteen animations, all drawn as buttons,
 * with an ON/OFF toggle and a row of four actions underneath. It worked and it was
 * unreadable — the state of the lab was the state of the buttons, so a person
 * trying to answer "what does `thinking` actually look like" had to hold the
 * control panel in their head while watching the preview.
 *
 * Three changes fix that, and each one is a claim about what this page is for:
 *
 * 1. **The preview is the read-out.** The stage names what is currently loaded —
 *    the emotion, the eye construction, the overlay effect, the activity — so the
 *    answer to the question is on the face, not inferred from which chip is lit.
 * 2. **The montage is the real one.** The toggle now runs the same
 *    `useAvatarMontage` driver that the home screen runs, with the same state
 *    groups and the same gaze filtering. The lab previously had its own private
 *    copy, which meant it could demo a montage the home screen never played — the
 *    exact gap that made home look broken and the lab look fine.
 * 3. **One chooser.** Eye construction, activity and the single-shot animations
 *    are `Dropdown`s, the same control the settings use. A lab that had its own
 *    picker was a lab whose keyboard behaviour and typeahead had to be
 *    re-discovered.
 *
 * The emotion list stays a wall of chips, and that is a deliberate exception
 * rather than an oversight: there are twenty-six of them, a menu hides twenty-four
 * at a time, and the whole point of looking at a face is comparing several
 * side by side. Choosing a *setting* is a dropdown problem. Choosing what to look
 * at is not.
 */

const EMOTIONS: LumineEmotion[] = [
  "idle", "neutral", "happy", "loving", "delighted", "amused", "excited", "playful",
  "mischievous", "curious", "thinking", "focused", "determined", "confused", "worried",
  "surprised", "embarrassed", "shy", "sleepy", "jealous", "wink", "proud", "relieved",
  "calm", "alert", "concerned", "angry",
];

const ANIMATIONS: LumineAnimation[] = [
  "blink", "doubleBlink", "slowBlink", "sleepyBlink", "surprisedBlink", "happyBlink",
  "lookLeft", "lookRight", "curiousTilt", "wiggle", "bounce", "sleepy", "surprise", "peek",
  "softBreeze", "microTilt", "tinySway", "softBounce", "pauseDrift",
];

/** `softBreeze` → `Soft breeze`, for the rows nobody wants to read as camelCase. */
function readable(animation: LumineAnimation): string {
  const spaced = animation.replace(/([a-z])([A-Z])/g, "$1 $2");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

/** What each eye construction actually is, since "arc" on its own says nothing. */
const EYE_MODES: { value: string; label: string; hint: string }[] = [
  { value: "canonical", label: "Recommended for the emotion", hint: "What the home screen uses. Each emotion picks its own eyes." },
  { value: "mask", label: "Mask", hint: "The original sheet. The face's own shape does the work." },
  { value: "arc", label: "Arc", hint: "A second register: the eyes read as a drawn curve." },
];

const ACTIVITIES: { value: string; label: string; hint: string }[] = [
  { value: "none", label: "None", hint: "The face is only reacting." },
  { value: "listening", label: "Listening", hint: "What Lumine looks like while you are the one talking." },
  { value: "speaking", label: "Speaking", hint: "What Lumine looks like while she is." },
];

const DEFAULT_EMOTION: LumineEmotion = "neutral";
const DEFAULT_INTENSITY = 0.8;

export default function AvatarLabPage() {
  const avatarRef = useRef<HTMLObjectElement>(null);
  const engineRef = useRef<LumineAvatarEngine | null>(null);
  const [engine, setEngine] = useState<LumineAvatarEngine | null>(null);
  const [emotion, setEmotion] = useState<LumineEmotion>(DEFAULT_EMOTION);
  const [intensity, setIntensity] = useState(DEFAULT_INTENSITY);
  const [activity, setActivity] = useState<LumineActivity>("none");
  const [eyeMode, setEyeMode] = useState<"canonical" | ExpressiveEyeMode>("canonical");
  const [montageOn, setMontageOn] = useState(false);
  const [playing, setPlaying] = useState<"idle" | LumineAnimation>("idle");

  useEffect(() => {
    const avatar = avatarRef.current;
    if (!avatar) return;
    const created = new LumineAvatarEngine(avatar);
    engineRef.current = created;
    // Published so the montage driver can start. `connect()` is what finds the
    // eyes, and a montage that begins before that plays into a face with no eyes
    // to move.
    const setup = () => {
      created.connect();
      setEngine(created);
    };
    avatar.addEventListener("load", setup);
    if (avatar.contentDocument) setup();
    return () => {
      avatar.removeEventListener("load", setup);
      created.destroy();
      engineRef.current = null;
      setEngine(null);
    };
  }, []);

  const spec = expressionSpec(emotion);
  const activeMode = eyeMode === "canonical" ? spec.eyeMode : eyeMode;

  useEffect(() => {
    engineRef.current?.setEmotion(emotion, intensity);
    engineRef.current?.setActivity(activity);
    engineRef.current?.setEyeMode(activeMode);
  }, [emotion, intensity, activity, activeMode]);

  // The same driver the home screen uses, at the lab's own tempo. `paused` is the
  // toggle, so "stop the montage" and "stop a single animation" are one state
  // rather than two that can disagree.
  useAvatarMontage({
    engine,
    kind: activity === "none" ? "idle" : activity,
    paused: !montageOn,
    cursorGaze: false,
  });

  const play = (animation: LumineAnimation) => {
    setPlaying(animation);
    void engineRef.current?.play(animation, 2).finally(() => setPlaying("idle"));
  };

  const reset = () => {
    setEmotion(DEFAULT_EMOTION);
    setIntensity(DEFAULT_INTENSITY);
    setActivity("none");
    setEyeMode("canonical");
    setMontageOn(false);
    setPlaying("idle");
  };

  return (
    <main className="avatar-lab-page" aria-labelledby="avatar-lab-title">
      <header className="workspace-header avatar-lab-header">
        <div>
          <p className="eyebrow">Lumine workspace · visual instrument</p>
          <h1 id="avatar-lab-title">Avatar Lab</h1>
          <p>Everything the face can be, and the exact driver the home screen runs.</p>
        </div>
        <button
          className="workspace-settings"
          onClick={reset}
          aria-label="Reset the avatar lab to its starting state"
          title="Reset the avatar lab"
        >
          <Icon name="reset" size={18} />
        </button>
      </header>

      <div className="avatar-lab-layout">
        <section className="avatar-lab-stage" aria-label="Live preview">
          <div className="avatar-lab-stage-label">
            <span className="signal-dot" />
            Live preview
            {/* The read-out. The old page made the answer a deduction from which
                chip was lit, which is the one thing a preview must not require. */}
            <span className="avatar-lab-live" role="status">
              {playing === "idle" ? (montageOn ? "montage" : "holding") : readable(playing)}
            </span>
          </div>

          <div className="avatar-lab-preview">
            <div className="avatar-lab-halo" aria-hidden="true" />
            <div className="avatar-lab-face">
              <object ref={avatarRef} data={lumineAvatar} type="image/svg+xml" aria-label="Lumine animated avatar" />
              <ExpressionEffects effect={spec.effect} intensity={intensity} />
            </div>
          </div>

          <div className="avatar-lab-caption">
            <strong>{emotion}</strong>
            <span>{activeMode} · {spec.effect} · {activity} · {Math.round(intensity * 100)}%</span>
          </div>
        </section>

        <motion.aside
          className="avatar-lab-controls"
          initial={{ opacity: 0, x: 14 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.35 }}
        >
          <div className="avatar-lab-controls-head">
            <div>
              <p className="eyebrow">Director controls</p>
              <h2>Expression study</h2>
            </div>
          </div>

          <section className="avatar-lab-section">
            <span id="lab-emotion-label">Emotion</span>
            <div className="avatar-lab-emotions" role="group" aria-labelledby="lab-emotion-label">
              {EMOTIONS.map((value) => (
                <button
                  key={value}
                  type="button"
                  className={value === emotion ? "is-selected" : ""}
                  aria-pressed={value === emotion}
                  onClick={() => setEmotion(value)}
                >
                  {value}
                </button>
              ))}
            </div>
          </section>

          <div className="avatar-lab-field">
            <label id="lab-eyes-label">Eye construction</label>
            <Dropdown
              label="Eye construction"
              value={eyeMode}
              onChange={(next) => setEyeMode(next as "canonical" | ExpressiveEyeMode)}
              options={EYE_MODES}
            />
            <span className="avatar-lab-hint">{EYE_MODES.find((mode) => mode.value === eyeMode)?.hint}</span>
          </div>

          <div className="avatar-lab-field">
            <label id="lab-activity-label">Activity</label>
            <Dropdown
              label="Activity"
              value={activity}
              onChange={(next) => setActivity(next as LumineActivity)}
              options={ACTIVITIES}
            />
            <span className="avatar-lab-hint">{ACTIVITIES.find((row) => row.value === activity)?.hint}</span>
          </div>

          <div className="avatar-lab-field">
            <label htmlFor="lab-intensity">Intensity</label>
            <Knob
              name="intensity"
              value={String(intensity)}
              onChange={(next) => setIntensity(Number(next))}
              minimum={0}
              maximum={1}
              step={0.05}
              ends={["Barely", "All in"]}
              format={(value) => `${Math.round(value * 100)}%`}
              help="How far the expression travels. Below about 0.3 the morph stops partway and the face reads as neutral, which is worth seeing once."
            />
          </div>

          <div className="avatar-lab-row">
            <span id="lab-montage-label">Idle montage</span>
            <button
              type="button"
              role="switch"
              aria-checked={montageOn}
              aria-labelledby="lab-montage-label"
              className={montageOn ? "is-selected" : ""}
              onClick={() => setMontageOn((current) => !current)}
            >
              {montageOn ? "On" : "Off"}
            </button>
            <Hint label="What this is">
              The loop Lumine runs on her own. It is the same driver the home screen uses, so what plays
              here is what plays there — gestures chosen at random from the group for the current state,
              with a pause between them.
            </Hint>
          </div>

          <div className="avatar-lab-field">
            <label id="lab-animation-label">Play one gesture</label>
            <Dropdown
              label="Animation"
              value={playing === "idle" ? "" : playing}
              onChange={(next) => {
                if (next !== "") play(next as LumineAnimation);
              }}
              placeholder="Choose a gesture…"
              options={ANIMATIONS.map((animation) => ({ value: animation, label: readable(animation) }))}
            />
          </div>

          <div className="avatar-lab-actions">
            <button type="button" onClick={() => play(randomIdleAnimation())}>
              Surprise me
            </button>
            <button type="button" onClick={reset}>
              Reset
            </button>
          </div>
        </motion.aside>
      </div>
    </main>
  );
}
