import { motion } from "framer-motion";
import type { ExpressionEffect } from "./expressions";
import singleExclamationSvg from "../../assets/emotion_artifacts/single exclamation.svg";
import doubleExclamationSvg from "../../assets/emotion_artifacts/double exclamation.svg";
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

const artifactMotion = (effect: ExpressionEffect) => {
  if (effect === "sparkle") return { opacity: [0, 1, 1, 0.8], scale: [0.72, 1, 1, 0.96], rotate: [-12, 0, 0, 2], x: [8, 0, 0, -2], y: [6, -2, -2, -1] };
  if (effect === "love") return { opacity: [0, 1, 1, 0.82], scale: [0.7, 1, 1, 0.96], rotate: [-16, 0, 0, 3], x: [8, 0, 0, -1], y: [8, -3, -3, -2] };
  if (effect === "firework") return { opacity: [0, 1, 1, 0.8], scale: [0.7, 1, 1, 0.96], rotate: [-8, 0, 0, 3], x: [6, 0, 0, -1], y: [5, -1, -1, 0] };
  if (effect === "vein-pop" || effect === "anger" || effect === "tension") return { opacity: [0, 1, 1, 0.82], scale: [0.8, 1, 1, 0.98], rotate: [-10, 4, 4, 2], x: [4, 0, 0, 1], y: [1, 0, 0, 0] };
  if (effect === "sweat" || effect === "shadow-lines") return { opacity: [0, 1, 1, 0.8], scale: [0.82, 1, 1, 0.96], rotate: [-6, 0, 0, 2], x: [3, 0, 0, 1], y: [1, 0, 0, 1] };
  if (effect === "thoughts") return { opacity: [0, 1, 1, 0.78], scale: [0.7, 1, 1, 0.96], rotate: [-8, 0, 0, 2], x: [4, 0, 0, -2], y: [3, 0, 0, -1] };
  return { opacity: [0, 1, 1, 0.8], scale: [0.75, 1, 1, 0.96], rotate: [-8, 0, 0, 2], x: [5, 0, 0, -1], y: [4, 0, 0, -1] };
};

const EFFECT_ASSETS: Record<Exclude<ExpressionEffect, "none">, string> = {
  question: questionMarkSvg,
  surprise: singleExclamationSvg,
  anger: veinPopSvg,
  sweat: sweatDropsSvg,
  blush: sweatDropsSvg,
  sparkle: sparkleSvg,
  love: loveSvg,
  tension: actionBurstSvg,
  interrobang: interrobangSvg,
  "single-exclamation": singleExclamationSvg,
  "double-exclamation": doubleExclamationSvg,
  "triple-exclamation": singleExclamationSvg,
  "vein-pop": veinPopSvg,
  "shadow-lines": shadowLinesSvg,
  firework: fireworkBurstSvg,
  "action-burst": actionBurstSvg,
  thoughts: thoughtsSvg,
};

export function ExpressionEffects({ effect, intensity = 0.6 }: { effect: ExpressionEffect; intensity?: number }) {
  if (effect === "none") return null;
  const opacity = 0.4 + intensity * 0.62;
  const asset = EFFECT_ASSETS[effect as Exclude<ExpressionEffect, "none">];
  const motionState = artifactMotion(effect);

  return <div className={`expression-effects expression-effects--${effect}`} aria-hidden="true">
    <motion.img
      src={asset}
      alt=""
      className="expression-artifact"
      initial={{ opacity: 0, scale: 0.7, rotate: -12, x: 8, y: 6 }}
      animate={{ ...motionState, opacity: motionState.opacity.map((value) => value * opacity) }}
      transition={{ duration: 1.1, ease: "easeInOut" as const, times: [0, 0.22, 0.7, 1] }}
      style={{
        width: effect === "sparkle" ? 46 : effect === "love" ? 44 : effect === "firework" ? 42 : effect === "vein-pop" ? 38 : effect === "shadow-lines" ? 46 : effect === "thoughts" ? 48 : 38,
        height: effect === "sparkle" ? 46 : effect === "love" ? 44 : effect === "firework" ? 42 : effect === "vein-pop" ? 38 : effect === "shadow-lines" ? 46 : effect === "thoughts" ? 48 : 38,
        filter: "drop-shadow(0 2px 8px rgba(255,255,255,0.12)) brightness(1.14)",
      }}
    />
  </div>;
}
