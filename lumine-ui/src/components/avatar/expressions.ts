import type { LumineEmotion } from "./avatarTypes";

export type ExpressionPose = { maskScale: [number, number]; eyeOffset: [number, number]; tilt: number; y: number };

// These are restrained mask poses. The source SVG remains untouched; the black eye masks are transformed.
export const EXPRESSIONS: Record<LumineEmotion, ExpressionPose> = {
  idle: { maskScale: [1, 1], eyeOffset: [0, 0], tilt: 0, y: 0 },
  happy: { maskScale: [1.04, 0.82], eyeOffset: [0, 1], tilt: 0, y: -1 },
  excited: { maskScale: [1.04, 1.08], eyeOffset: [0, -1], tilt: 0, y: -1 },
  curious: { maskScale: [0.97, 0.97], eyeOffset: [2.5, -1], tilt: -5, y: 0 },
  thinking: { maskScale: [1.03, 1.04], eyeOffset: [0, -3], tilt: 2, y: 0 },
  confused: { maskScale: [1.04, 0.92], eyeOffset: [-1, 1], tilt: -4, y: 0 },
  sad: { maskScale: [1.06, 0.72], eyeOffset: [0, 2], tilt: -1, y: 3 },
  sleepy: { maskScale: [1.08, 0.32], eyeOffset: [0, 3], tilt: 1, y: 3 },
  surprised: { maskScale: [1.04, 1.08], eyeOffset: [0, -1], tilt: 0, y: -1 },
  embarrassed: { maskScale: [1.1, 0.76], eyeOffset: [2, 2], tilt: 3, y: 2 },
  angry: { maskScale: [0.99, 0.62], eyeOffset: [0, 2], tilt: -2, y: 1 },
  focused: { maskScale: [0.98, 0.92], eyeOffset: [0, -1], tilt: 0, y: 0 },
  shy: { maskScale: [1.08, 0.78], eyeOffset: [3, 2], tilt: 4, y: 2 },
  proud: { maskScale: [1.02, 1.02], eyeOffset: [0, -2], tilt: -1, y: -2 },
  worried: { maskScale: [1.02, 1.04], eyeOffset: [-1, -1], tilt: 1, y: 0 },
  playful: { maskScale: [1.02, 1.06], eyeOffset: [2, 0], tilt: -4, y: -1 },
};
