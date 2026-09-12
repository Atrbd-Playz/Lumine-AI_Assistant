export type LumineEmotion = "idle" | "happy" | "excited" | "curious" | "thinking" | "confused" | "sad" | "sleepy" | "surprised" | "embarrassed" | "angry" | "focused" | "shy" | "proud" | "worried" | "playful";
export type LumineActivity = "none" | "listening" | "speaking";
export type LumineAnimation = "blink" | "doubleBlink" | "slowBlink" | "lookLeft" | "lookRight" | "curiousTilt" | "wiggle" | "bounce" | "sleepy" | "surprise" | "happyBlink" | "surprisedBlink" | "sleepyBlink" | "peek";

export type AvatarSnapshot = {
  emotion: LumineEmotion;
  activity: LumineActivity;
  animation: LumineAnimation | "idle";
};
