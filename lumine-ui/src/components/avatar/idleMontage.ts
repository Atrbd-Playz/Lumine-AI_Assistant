import type { LumineAnimation } from "./avatarTypes";

const weighted: Array<[LumineAnimation, number]> = [
  ["blink", 35], ["doubleBlink", 8], ["slowBlink", 8], ["lookLeft", 8], ["lookRight", 8],
  ["curiousTilt", 10], ["bounce", 8], ["wiggle", 5], ["sleepyBlink", 3], ["peek", 2],
];

export function randomIdleAnimation() {
  const pick = Math.random() * weighted.reduce((total, [, weight]) => total + weight, 0);
  let cursor = 0;
  for (const [animation, weight] of weighted) {
    cursor += weight;
    if (pick <= cursor) return animation;
  }
  return "blink" as const;
}
