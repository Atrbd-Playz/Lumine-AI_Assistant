export type LumineEmotion =
  | "neutral"
  | "idle"
  | "happy"
  | "loving"
  | "delighted"
  | "amused"
  | "excited"
  | "playful"
  | "mischievous"
  | "jealous"
  | "wink"
  | "sleepy"
  | "sad"
  | "surprised"
  | "embarrassed"
  | "shy"
  | "confused"
  | "thinking"
  | "angry"
  | "curious"
  | "focused"
  | "proud"
  | "worried"
  | "relieved"
  | "determined"
  | "calm"
  | "alert"
  | "concerned";

export type LumineEmotionSource = "llm" | "heuristic" | "voice" | "system" | "user" | "lab";

export interface LumineEmotionIntent {
  primary: LumineEmotion;
  secondary?: LumineEmotion;
  intensity: number;
  durationMs?: number;
  source: LumineEmotionSource;
  priority?: number;
}

export interface LumineEmotionEventEnvelope {
  version: number;
  type: "lumine.emotion";
  payload: {
    primary: LumineEmotion;
    secondary?: LumineEmotion | null;
    intensity: number;
    durationMs?: number | null;
    source: LumineEmotionSource;
    priority?: number;
  };
}

export type LumineActivity = "none" | "listening" | "speaking";

/**
 * An authored gesture the engine can play.
 *
 * ## The split between the old set and the new one
 *
 * The first twenty-two are all variations on the same motion: translate, rotate,
 * scale. They are kept because they are the vocabulary the expression system and
 * the Avatar Lab are built on, and because a hand-authored blink is not made
 * better by being replaced.
 *
 * The last seven are the ones that needed a channel the old set had no way to
 * reach, and they exist because of the specific failure that a *uniform* `scale()`
 * produces on a soft body:
 *
 * * `reaching`, `settle` — the connecting and connected states. There were no
 *   faces for either: `Home.tsx` mapped `connecting` to `thinking` and `connected`
 *   to the same thing, so there was nothing here to write.
 * * `inhale`, `exhale` — the body, not the face. Both are a squash with a hold.
 * * `wonder` — a held head-turn with an upward eye, which no existing animation
 *   holds long enough to read as a state rather than a tic.
 * * `saysYes` — a nod, which needs two dips at different depths.
 * * `listensDeeply` — a lean *in*, which is `x` rather than `y`.
 *
 * `saysYes` and `listensDeeply` are also the two the new `Activity` mapping
 * reaches for, which is why they are authored gestures rather than something the
 * engine synthesises from amplitude.
 */
export type LumineAnimation =
  | "blink"
  | "doubleBlink"
  | "slowBlink"
  | "lookLeft"
  | "lookRight"
  | "curiousTilt"
  | "wiggle"
  | "bounce"
  | "sleepy"
  | "surprise"
  | "happyBlink"
  | "surprisedBlink"
  | "sleepyBlink"
  | "peek"
  | "softBreeze"
  | "microTilt"
  | "tinySway"
  | "softBounce"
  | "pauseDrift"
  | "reaching"
  | "settle"
  | "inhale"
  | "exhale"
  | "wonder"
  | "saysYes"
  | "listensDeeply";

export type AvatarSnapshot = {
  emotion: LumineEmotion;
  activity: LumineActivity;
  animation: LumineAnimation | "idle";
};
