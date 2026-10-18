import { motion } from "framer-motion";
import type { ExpressionEffect } from "./expressions";
import singleExclamationSvg from "../../assets/emotion_artifacts/single exclamation.svg";
import singleExclamationFilledSvg from "../../assets/emotion_artifacts/single exclamation filled.svg";
import doubleExclamationSvg from "../../assets/emotion_artifacts/double exclamation.svg";
import tripleExclamationSvg from "../../assets/emotion_artifacts/triple_exclamation.svg";
import interrobangSvg from "../../assets/emotion_artifacts/Interrobang.svg";
import sweatDropsSvg from "../../assets/emotion_artifacts/sweat_drops.svg";
import veinPopSvg from "../../assets/emotion_artifacts/Vein Pop.svg";
import fireworkBurstSvg from "../../assets/emotion_artifacts/firework burst.svg";
import actionBurstSvg from "../../assets/emotion_artifacts/Action burst.svg";
import shadowLinesSvg from "../../assets/emotion_artifacts/Vertical Shadow Lines.svg";
import thoughtsSvg from "../../assets/emotion_artifacts/Thoughts.svg";
import questionMarkSvg from "../../assets/emotion_artifacts/Double Question mark.svg";
import sparkleSvg from "../../assets/emotion_artifacts/Sparkles.svg";
import loveSvg from "../../assets/emotion_artifacts/Love.svg";
import blushSvg from "../../assets/emotion_artifacts/Blush.svg";

const artifactMotion = (effect: ExpressionEffect) => {
  if (effect === "sparkle") return { opacity: [0, 1, 1, 0.86], scale: [0.6, 1.08, 1, 1], rotate: [-14, 2, 0, 0], x: [10, 0, 0, -2], y: [8, -3, -3, -2] };
  if (effect === "love") return { opacity: [0, 1, 1, 0.88], scale: [0.6, 1.1, 1, 1], rotate: [-18, 3, 0, 0], x: [10, 0, 0, -1], y: [10, -4, -4, -3] };
  if (effect === "firework") return { opacity: [0, 1, 1, 0.86], scale: [0.6, 1.12, 1, 1], rotate: [-10, 3, 0, 0], x: [8, 0, 0, -1], y: [6, -2, -2, -1] };
  if (effect === "blush") return { opacity: [0, 1, 1, 0.94], scale: [0.82, 1.03, 1, 1], rotate: [-4, 0, 0, 0], x: [0, 0, 0, 0], y: [5, 0, 0, 0] };
  if (effect === "vein-pop" || effect === "anger" || effect === "tension") return { opacity: [0, 1, 1, 0.88], scale: [0.7, 1.14, 1, 1], rotate: [-14, 6, 4, 2], x: [6, 0, 0, 1], y: [2, 0, 0, 0] };
  if (effect === "sweat" || effect === "shadow-lines") return { opacity: [0, 1, 1, 0.86], scale: [0.72, 1.08, 1, 1], rotate: [-8, 0, 0, 2], x: [4, 0, 0, 1], y: [2, 0, 0, 1] };
  if (effect === "thoughts") return { opacity: [0, 1, 1, 0.84], scale: [0.6, 1.08, 1, 1], rotate: [-10, 2, 0, 0], x: [6, 0, 0, -2], y: [4, 0, 0, -1] };
  return { opacity: [0, 1, 1, 0.86], scale: [0.65, 1.1, 1, 1], rotate: [-10, 3, 0, 0], x: [7, 0, 0, -1], y: [5, 0, 0, -1] };
};

/**
 * The size of each manpu, in CSS pixels.
 *
 * These were 38–48px, which put a sparkle at about a fifth of the disc it floats
 * over — the size of a stray dot rather than a symbol. The face is a 118px head
 * in a 174px disc, and manga draws these marks *larger* than the feature they
 * annotate: a sweat drop bigger than an eye, a vein cross that sits off the
 * corner of the head. They are sized against that, and `blush` is the widest of
 * the set because two cheeks have to span a face rather than sit in one corner.
 */
const EFFECT_SIZE: Record<Exclude<ExpressionEffect, "none">, number> = {
  question: 58,
  surprise: 56,
  anger: 52,
  sweat: 54,
  blush: 96,
  sparkle: 60,
  love: 58,
  tension: 58,
  interrobang: 56,
  "single-exclamation": 52,
  "double-exclamation": 56,
  "triple-exclamation": 60,
  "vein-pop": 52,
  "shadow-lines": 60,
  firework: 56,
  "action-burst": 58,
  thoughts: 62,
};

/**
 * Which file draws each effect.
 *
 * Two of these were pointing at the wrong drawing: `blush` showed a *sweat
 * drop* — the manpu for worry — on a shy face, which is the one emotion that
 * must not look worried — and `triple-exclamation` showed a single one, so the
 * three-symbol set had only two members in it. `single exclamation filled.svg`
 * was in the asset folder and reached by nothing.
 */
const EFFECT_ASSETS: Record<Exclude<ExpressionEffect, "none">, string> = {
  question: questionMarkSvg,
  surprise: singleExclamationFilledSvg,
  anger: veinPopSvg,
  sweat: sweatDropsSvg,
  blush: blushSvg,
  sparkle: sparkleSvg,
  love: loveSvg,
  tension: actionBurstSvg,
  interrobang: interrobangSvg,
  "single-exclamation": singleExclamationSvg,
  "double-exclamation": doubleExclamationSvg,
  "triple-exclamation": tripleExclamationSvg,
  "vein-pop": veinPopSvg,
  "shadow-lines": shadowLinesSvg,
  firework: fireworkBurstSvg,
  "action-burst": actionBurstSvg,
  thoughts: thoughtsSvg,
};

/**
 * The width a *non-square* artifact is drawn at, and the height that goes with
 * `EFFECT_SIZE`'s width. Only `blush` needs it: two cheeks are twice as wide as
 * they are tall, and forcing it into the square would squash the pair into one
 * mark.
 */
const ARTIFACT_DIMENSIONS: Record<string, { width: number; height: number }> = {
  blush: { width: 112, height: 52 },
};

export function ExpressionEffects({ effect, intensity = 0.6 }: { effect: ExpressionEffect; intensity?: number }) {
  if (effect === "none") return null;
  const opacity = 0.4 + intensity * 0.62;
  const asset = EFFECT_ASSETS[effect as Exclude<ExpressionEffect, "none">];
  const motionState = artifactMotion(effect);
  const size = ARTIFACT_DIMENSIONS[effect] ?? { width: EFFECT_SIZE[effect as Exclude<ExpressionEffect, "none">], height: EFFECT_SIZE[effect as Exclude<ExpressionEffect, "none">] };

  return <div className={`expression-effects expression-effects--${effect}`} aria-hidden="true">
    {/* `key={effect}` so a *change* of symbol re-runs the entrance. Without it
        React reuses the node and framer tweens one manpu's keyframes into
        another's — a sparkle that morphs into a sweat drop, which reads as the
        same symbol changing its mind rather than as a new one arriving. */}
    <motion.img
      key={effect}
      src={asset}
      alt=""
      className="expression-artifact"
      initial={{ opacity: 0, scale: 0.55, rotate: -16, x: 10, y: 8 }}
      animate={{ ...motionState, opacity: motionState.opacity.map((value) => value * opacity) }}
      transition={{ duration: 1, ease: "easeOut" as const, times: [0, 0.16, 0.74, 1] }}
      style={{
        width: size.width,
        height: size.height,
        // Blush gets its own filter. The shared one is `brightness(1.14)`, but
        // `.expression-artifact` in index.css declares `brightness(1.22)
        // saturate(0.9) contrast(1.1)` and the stylesheet loses to inline — so
        // every artifact was being desaturated and contrast-boosted, which is
        // right for a white line drawing and wrong for a pink one. The cheeks
        // want saturation kept and the highlights lifted.
        filter: effect === "blush"
          ? "drop-shadow(0 2px 10px rgba(247,154,169,0.45)) brightness(1.18) saturate(1.15)"
          : "drop-shadow(0 2px 8px rgba(255,255,255,0.12)) brightness(1.14)",
      }}
    />
  </div>;
}
