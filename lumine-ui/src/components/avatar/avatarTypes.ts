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
  | "pauseDrift";

export type AvatarSnapshot = {
  emotion: LumineEmotion;
  activity: LumineActivity;
  animation: LumineAnimation | "idle";
};
