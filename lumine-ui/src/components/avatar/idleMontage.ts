import type { LumineAnimation } from "./avatarTypes";

const grouped: Record<"idle" | "listening" | "thinking" | "speaking", LumineAnimation[]> = {
  idle: ["blink", "softBreeze", "microTilt", "tinySway", "slowBlink", "lookLeft", "lookRight", "softBounce", "pauseDrift"],
  listening: ["softBreeze", "microTilt", "lookLeft", "lookRight", "blink", "tinySway"],
  thinking: ["microTilt", "tinySway", "lookLeft", "lookRight", "softBounce", "pauseDrift"],
  speaking: ["softBounce", "tinySway", "microTilt", "blink", "wiggle", "pauseDrift"],
};

const weighted: Array<[LumineAnimation, number]> = [
  ["blink", 26], ["doubleBlink", 6], ["slowBlink", 10], ["lookLeft", 10], ["lookRight", 10],
  ["curiousTilt", 8], ["wiggle", 6], ["softBreeze", 12], ["microTilt", 10], ["tinySway", 12], ["softBounce", 8], ["pauseDrift", 8], ["sleepyBlink", 2], ["peek", 2],
];

export function randomIdleAnimation() {
  const pick = Math.random() * weighted.reduce((total, [, weight]) => total + weight, 0);
  let cursor = 0;
  for (const [animation, weight] of weighted) {
    cursor += weight;
    if (pick <= cursor) return animation;
  }
  return "softBreeze" as const;
}

export function getMontageSequence(kind: keyof typeof grouped = "idle") {
  return grouped[kind].slice();
}

