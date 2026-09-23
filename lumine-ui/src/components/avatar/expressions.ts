import type { LumineEmotion } from "./avatarTypes";

export type ExpressionEffect =
  | "none"
  | "question"
  | "surprise"
  | "anger"
  | "sweat"
  | "blush"
  | "sparkle"
  | "love"
  | "tension"
  | "interrobang"
  | "single-exclamation"
  | "double-exclamation"
  | "triple-exclamation"
  | "vein-pop"
  | "shadow-lines"
  | "firework"
  | "action-burst"
  | "thoughts";
export type ExpressiveEyeMode = "mask" | "arc";
export type EyeAdjustment = { maskScale?: [number, number]; eyeOffset?: [number, number]; tilt?: number; y?: number; gazeX?: number; gazeY?: number };
export type ExpressionPose = {
  maskScale: [number, number];
  eyeOffset: [number, number];
  tilt: number;
  y: number;
  left?: EyeAdjustment;
  right?: EyeAdjustment;
};
export type ExpressionSpec = ExpressionPose & {
  eyeMode: ExpressiveEyeMode;
  effect: ExpressionEffect;
};

// These are restrained mask poses. The source SVG remains untouched; the black eye masks are transformed.
const neutralPose: ExpressionPose = { maskScale: [1, 1], eyeOffset: [0, 0], tilt: 0, y: 0 };

const spec = (pose: ExpressionPose, eyeMode: ExpressiveEyeMode = "mask", effect: ExpressionEffect = "none"): ExpressionSpec => ({ ...pose, eyeMode, effect });

export const EXPRESSION_SPECS: Record<LumineEmotion, ExpressionSpec> = {
  neutral: spec(neutralPose), idle: spec(neutralPose),
  happy: spec({ maskScale: [1.04, 0.82], eyeOffset: [0, 1], tilt: 0, y: -1 }, "arc", "sparkle"),
  loving: spec({ maskScale: [1.02, 0.86], eyeOffset: [0, 1], tilt: 0, y: -1 }, "arc", "love"),
  delighted: spec({ maskScale: [1.05, 0.84], eyeOffset: [0, 1], tilt: -1, y: -2 }, "arc", "sparkle"),
  amused: spec({ maskScale: [1.02, 0.9], eyeOffset: [0, 1], tilt: -2, y: 0 }, "mask", "sparkle"),
  excited: spec({ maskScale: [1.04, 1.08], eyeOffset: [0, -1], tilt: 0, y: -1 }, "mask", "double-exclamation"),
  playful: spec({ maskScale: [1.02, 1.04], eyeOffset: [1, 0], tilt: -2, y: -1, left: { maskScale: [0.95, 0.62], eyeOffset: [1, 1], tilt: -7 }, right: { maskScale: [1.04, 1.08], eyeOffset: [0, -1], tilt: 1 } }, "mask", "sparkle"),
  mischievous: spec({ maskScale: [0.98, 0.92], eyeOffset: [2.2, 0], tilt: -4, y: 0, left: { tilt: -6 }, right: { tilt: 4 } }, "mask", "sparkle"),
  jealous: spec({ maskScale: [0.99, 0.72], eyeOffset: [2.5, 1], tilt: -5, y: 1, left: { tilt: -3 }, right: { tilt: -8, maskScale: [0.94, 0.68] } }, "mask", "tension"),
  wink: spec({ maskScale: [1.04, 0.86], eyeOffset: [1, 1], tilt: -1, y: 0, left: { maskScale: [0.96, 0.22], tilt: -4 }, right: { maskScale: [1.05, 0.9], tilt: 1 } }, "mask", "single-exclamation"),
  sleepy: spec({ maskScale: [1.08, 0.32], eyeOffset: [0, 3], tilt: 1, y: 3, right: { maskScale: [1.03, 0.25], y: 1 } }, "mask", "none"),
  sad: spec({ maskScale: [1.06, 0.72], eyeOffset: [0, 2], tilt: -1, y: 3, left: { tilt: 3, y: 1 }, right: { tilt: -2 } }, "mask", "shadow-lines"),
  surprised: spec({ maskScale: [1.08, 1.16], eyeOffset: [0, -2], tilt: 0, y: -2 }, "mask", "single-exclamation"),
  embarrassed: spec({ maskScale: [1.03, 0.66], eyeOffset: [1.5, 2], tilt: 2, y: 2, left: { tilt: 5, gazeX: 2, gazeY: 2 }, right: { tilt: -1, gazeX: -3, gazeY: 1 } }, "mask", "sweat"),
  shy: spec({ maskScale: [1.04, 0.7], eyeOffset: [2, 2], tilt: 3, y: 2, left: { tilt: 5, gazeX: 3, gazeY: 3 }, right: { tilt: 1, gazeX: 1, gazeY: 2 } }, "mask", "sweat"),
  confused: spec({ maskScale: [1, 0.9], eyeOffset: [0, 1], tilt: -2, y: 0, left: { maskScale: [0.98, 0.68], tilt: -6, gazeX: -3 }, right: { maskScale: [1.05, 1.08], tilt: 3, gazeX: 4, y: -2 } }, "mask", "interrobang"),
  thinking: spec({ maskScale: [1.03, 1.04], eyeOffset: [0, -3], tilt: 2, y: 0, left: { gazeX: 4, gazeY: -3 }, right: { gazeX: 4, gazeY: -3 } }, "mask", "thoughts"),
  angry: spec({ maskScale: [1, 0.58], eyeOffset: [0, 2], tilt: 0, y: 1, left: { tilt: 8, y: 1 }, right: { tilt: -8, y: 1 } }, "mask", "vein-pop"),
  curious: spec({ maskScale: [0.97, 0.97], eyeOffset: [2.5, -1], tilt: -5, y: 0, left: { maskScale: [0.94, 0.86], y: 1 }, right: { maskScale: [1.04, 1.08], y: -2 } }, "mask", "question"),
  focused: spec({ maskScale: [0.98, 0.82], eyeOffset: [0, -1], tilt: 0, y: 0 }, "mask", "action-burst"),
  proud: spec({ maskScale: [1.01, 0.74], eyeOffset: [0, -1], tilt: -1, y: -1, left: { tilt: -4 }, right: { tilt: 4 } }, "mask", "sparkle"),
  worried: spec({ maskScale: [1, 0.7], eyeOffset: [-1, 1], tilt: 1, y: 1, left: { tilt: -6, y: 1 }, right: { tilt: 2, y: -1, maskScale: [1.04, 0.82] } }, "mask", "sweat"),
  relieved: spec({ maskScale: [1.04, 0.88], eyeOffset: [0, 0], tilt: -1, y: -1 }, "arc", "firework"),
  determined: spec({ maskScale: [1.02, 0.86], eyeOffset: [0, 0], tilt: -2, y: 0, left: { tilt: 5 }, right: { tilt: -5 } }, "mask", "action-burst"),
  calm: spec({ maskScale: [1.02, 0.96], eyeOffset: [0, 0], tilt: 0, y: 0 }, "mask", "none"),
  alert: spec({ maskScale: [1.04, 1.02], eyeOffset: [0, -1], tilt: -1, y: -1 }, "mask", "single-exclamation"),
  concerned: spec({ maskScale: [1.02, 0.8], eyeOffset: [0, 1], tilt: 0, y: 2 }, "mask", "shadow-lines"),
};

export const EXPRESSIONS: Record<LumineEmotion, ExpressionPose> = EXPRESSION_SPECS;

export function expressionSpec(emotion: LumineEmotion): ExpressionSpec {
  return EXPRESSION_SPECS[emotion] ?? EXPRESSION_SPECS.neutral;
}
