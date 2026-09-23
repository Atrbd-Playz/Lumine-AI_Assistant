import type { LumineEmotion } from "./avatarTypes";
import { expressionSpec } from "./expressions";

export type ExpressiveEyeMode = "mask" | "arc";
export type ExpressiveEyePose = {
  left: string;
  right: string;
  opacity: number;
  strokeWidth: number;
  scale: number;
  y: number;
  rotate: number;
};

const pose = (left: string, right = left, options: Partial<Omit<ExpressiveEyePose, "left" | "right">> = {}): ExpressiveEyePose => ({
  left,
  right,
  opacity: 0.82,
  strokeWidth: 2.6,
  scale: 1,
  y: 0,
  rotate: 0,
  ...options,
});

export function recommendedEyeMode(emotion: LumineEmotion): ExpressiveEyeMode {
  return expressionSpec(emotion).eyeMode;
}

const neutralArc = pose("M -22 2 Q 0 -5 22 2 Q 0 6 -22 2 Z", "M -22 2 Q 0 -5 22 2 Q 0 6 -22 2 Z", { opacity: 0.35, strokeWidth: 2.1 });

export const EXPRESSIVE_EYE_POSES: Record<LumineEmotion, ExpressiveEyePose> = {
  neutral: neutralArc,
  idle: neutralArc,
  happy: pose("M -30 3 C -20 -5 -10 -10 0 -10 C 10 -10 20 -5 30 3 C 20 0 10 -2 0 -3 C -10 -2 -20 0 -30 3 Z", undefined, { strokeWidth: 2.5, scale: 1.02 }),
  loving: pose("M -31 4 C -21 -7 -11 -13 0 -14 C 11 -13 21 -7 31 4 C 20 0 10 -3 0 -4 C -10 -3 -20 0 -31 4 Z", undefined, { opacity: 0.94, strokeWidth: 2.6, scale: 1.04 }),
  delighted: pose("M -30 3 C -20 -6 -10 -10 0 -11 C 10 -10 20 -5 30 3 C 21 1 10 -1 0 -2 C -10 -1 -20 1 -30 3 Z", undefined, { opacity: 0.95, strokeWidth: 2.6, scale: 1.04, y: -1 }),
  amused: pose("M -27 4 C -17 -8 -7 -12 0 -12 C 7 -12 17 -8 27 4 C 18 1 9 -1 0 -2 C -9 -1 -18 1 -27 4 Z", undefined, { opacity: 0.9, strokeWidth: 2.5, scale: 1.01, y: 0 }),
  excited: pose("M -32 4 C -21 -7 -11 -13 0 -14 C 11 -13 21 -7 32 4 C 21 0 10 -3 0 -4 C -10 -3 -21 0 -32 4 Z", undefined, { opacity: 0.94, strokeWidth: 2.8, scale: 1.05, y: -1 }),
  playful: pose("M -26 2 Q -8 -12 8 -2 Q 14 2 20 0 Q 10 8 0 7 Q -14 7 -26 2 Z", "M -22 -4 Q 0 -15 24 -2 Q 12 4 0 4 Q -12 3 -22 -4 Z", { rotate: -2, scale: 1.02 }),
  mischievous: pose("M -24 2 Q -8 -9 0 -9 Q 8 -9 24 2 Q 12 3 0 4 Q -12 3 -24 2 Z", "M -24 -2 Q 0 -12 24 0 Q 12 2 0 2 Q -12 2 -24 -2 Z", { opacity: 0.86, rotate: -3, scale: 1.02 }),
  jealous: pose("M -22 3 Q 0 -10 22 1 Q 12 5 0 7 Q -12 5 -22 3 Z", "M -22 0 Q 0 -9 22 2 Q 11 2 0 4 Q -11 2 -22 0 Z", { opacity: 0.8, rotate: -5, scale: 1.03 }),
  wink: pose("M -30 3 C -20 -5 -10 -10 0 -10 C 10 -10 20 -5 30 3 C 20 0 10 -2 0 -3 C -10 -2 -20 0 -30 3 Z", "M -28 2 C -18 5 -8 5 2 0 Q -8 2 -28 2 Z", { opacity: 0.9, strokeWidth: 2.5, rotate: -2, scale: 1.02 }),
  sleepy: pose("M -23 1 Q 0 5 23 1 Q 12 2 0 2 Q -12 2 -23 1 Z", undefined, { opacity: 0.48, strokeWidth: 2, scale: 0.92, y: 2 }),
  sad: pose("M -25 -2 Q 0 8 25 -2 Q 13 1 0 2 Q -13 1 -25 -2 Z", undefined, { y: 2, opacity: 0.72 }),
  surprised: pose("M -23 -2 Q 0 -9 23 -2 Q 12 -3 0 -4 Q -12 -3 -23 -2 Z", undefined, { opacity: 0.65, strokeWidth: 2.1, y: -4 }),
  embarrassed: pose("M -25 4 Q -10 -3 2 4 Q -8 7 -25 4 Z", "M -20 1 Q 0 -7 21 1 Q 10 5 0 5 Q -11 5 -20 1 Z", { opacity: 0.58, y: 1, rotate: 2 }),
  shy: pose("M -24 4 Q -8 -4 5 3 Q -8 7 -24 4 Z", "M -20 2 Q 0 -5 20 2 Q 10 5 0 5 Q -10 5 -20 2 Z", { opacity: 0.55, rotate: 3 }),
  confused: pose("M -25 2 Q 0 -10 25 -2 Q 13 -2 0 -3 Q -13 -2 -25 2 Z", "M -25 4 Q 0 -4 25 -1 Q 13 0 0 -1 Q -13 0 -25 4 Z", { rotate: -3 }),
  thinking: pose("M -25 1 Q 0 -4 25 1 Q 13 -1 0 -2 Q -13 -1 -25 1 Z", "M -25 2 Q 0 -5 25 -2 Q 13 -3 0 -4 Q -13 -3 -25 2 Z", { opacity: 0.62, rotate: 1 }),
  angry: pose("M -22 5 Q 0 -4 22 -8 Q 12 -1 0 3 Q -12 6 -22 5 Z", undefined, { strokeWidth: 2.8, rotate: -1 }),
  curious: pose("M -25 2 Q 0 -8 25 2 Q 13 0 0 -1 Q -13 0 -25 2 Z", "M -25 -2 Q 0 -10 25 -1 Q 13 -3 0 -4 Q -13 -3 -25 -2 Z", { rotate: -2 }),
  focused: pose("M -22 0 Q 0 -8 22 -1 Q 12 4 0 4 Q -12 4 -22 0 Z", undefined, { opacity: 0.62, strokeWidth: 2.3 }),
  proud: pose("M -25 2 Q 0 -8 25 1 Q 13 4 0 5 Q -13 4 -25 2 Z", undefined, { opacity: 0.78, strokeWidth: 3, y: -2, rotate: -1 }),
  worried: pose("M -24 -2 Q -7 -9 8 -3 Q 14 0 22 1 Q 10 5 0 5 Q -13 4 -24 -2 Z", "M -23 1 Q 0 -5 23 2 Q 12 5 0 6 Q -12 5 -23 1 Z", { opacity: 0.72, rotate: 2 }),
  relieved: pose("M -28 3 C -20 -5 -10 -10 0 -10 C 10 -10 20 -5 28 3 C 19 1 10 -1 0 -2 C -10 -1 -19 1 -28 3 Z", undefined, { opacity: 0.9, strokeWidth: 2.5, scale: 1.02, y: -1 }),
  determined: pose("M -26 2 Q 0 -10 26 2 Q 12 2 0 2 Q -12 2 -26 2 Z", undefined, { opacity: 0.82, strokeWidth: 2.7, scale: 1.03, y: -1 }),
  calm: pose("M -24 0 Q 0 -6 24 0 Q 12 2 0 2 Q -12 2 -24 0 Z", undefined, { opacity: 0.72, strokeWidth: 2.3, scale: 0.98 }),
  alert: pose("M -26 3 C -17 -7 -8 -11 0 -11 C 8 -11 17 -7 26 3 C 19 0 10 -2 0 -3 C -10 -2 -19 0 -26 3 Z", undefined, { opacity: 0.92, strokeWidth: 2.7, scale: 1.04, y: -1 }),
  concerned: pose("M -25 1 Q 0 -7 25 1 Q 12 2 0 4 Q -12 2 -25 1 Z", undefined, { opacity: 0.68, strokeWidth: 2.4, scale: 1.02, y: 1 }),
};