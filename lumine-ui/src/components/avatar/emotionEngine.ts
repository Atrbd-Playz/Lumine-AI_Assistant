import { easeInOut, easeOutBack, wait } from "./animationUtils";
import { interpolate } from "flubber";
import { EXPRESSIONS, type ExpressionPose, type EyeAdjustment } from "./expressions";
import { EXPRESSIVE_EYE_POSES, type ExpressiveEyeMode } from "./expressiveEyePaths";
import type { AvatarSnapshot, LumineActivity, LumineAnimation, LumineEmotion } from "./avatarTypes";

type Eye = { element: SVGGElement; path: SVGPathElement; originalParent: SVGElement; originalNextSibling: ChildNode | null; root: SVGSVGElement; centerX: number; centerY: number; originalD: string; originalFill: string };
type Motion = { rotation: number; y: number; blink: number; gazeX: number; gazeY: number };
type Priority = 0 | 1 | 2 | 3;

const neutralMotion = (): Motion => ({ rotation: 0, y: 0, blink: 1, gazeX: 0, gazeY: 0 });
const lerp = (from: number, to: number, amount: number) => from + (to - from) * amount;
const clamp01 = (value: number) => Math.max(0, Math.min(1, value));
const EMOTION_DEBUG = import.meta.env.VITE_LUMINE_DEBUG_EMOTION === "true";

/** Lightweight director: emotion pose + activity overlay + one prioritized authored reaction. */
export class LumineAvatarEngine {
  private object: HTMLObjectElement;
  private eyes: Eye[] = [];
  private frame = 0;
  private destroyed = false;
  private emotion: LumineEmotion = "idle";
  private activity: LumineActivity = "none";
  private animation: AvatarSnapshot["animation"] = "idle";
  private gaze = { x: 0, y: 0 };
  private motion = neutralMotion();
  private commandId = 0;
  private priority: Priority = 0;
  private transitionToken = 0;
  private transitionRaf = 0;
  private eyeMode: ExpressiveEyeMode = "mask";
  private morphProgress = 0;
  private morphRaf = 0;
  private intensity = 1;

  constructor(object: HTMLObjectElement) { this.object = object; }

  connect() {
    const document = this.object.contentDocument;
    if (!document) return false;
    const groups = Array.from(document.querySelectorAll<SVGGElement>(".oeil0, .oeil1")).slice(0, 2);
    if (groups.length < 2) return false;
    this.eyes = groups.map((element) => {
      const existing = element.parentElement?.closest("[data-lumine-eye-wrapper]") as SVGGElement | null;
      const wrapper = existing ?? document.createElementNS("http://www.w3.org/2000/svg", "g");
      if (!existing) { wrapper.setAttribute("data-lumine-eye-wrapper", "true"); element.parentNode?.insertBefore(wrapper, element); wrapper.appendChild(element); }
      element.style.animationPlayState = "paused";
      const box = element.getBBox();
      return { element: wrapper, path: element as unknown as SVGPathElement, originalParent: wrapper.parentElement as unknown as SVGElement, originalNextSibling: wrapper.nextSibling, root: element.ownerSVGElement as SVGSVGElement, centerX: box.x + box.width / 2, centerY: box.y + box.height / 2, originalD: element.getAttribute("d") ?? "", originalFill: element.getAttribute("fill") ?? "#000" };
    });
    this.object.style.opacity = "0";
    this.object.style.transform = "translateY(6px)";
    this.object.style.transition = "opacity 780ms ease, transform 780ms ease";
    requestAnimationFrame(() => {
      this.object.style.opacity = "1";
      this.object.style.transform = "translateY(0)";
    });
    this.render();
    void this.play("blink", 2).catch(() => undefined);
    return true;
  }

  setEmotion(emotion: LumineEmotion, intensity = 1) {
    const nextIntensity = clamp01(intensity);
    if (emotion === this.emotion && nextIntensity === this.intensity) return;
    if (EMOTION_DEBUG) console.info("[Emotion] avatar engine applying", emotion);
    const from = EXPRESSIONS[this.emotion];
    const to = EXPRESSIONS[emotion];
    const fromArc = EXPRESSIVE_EYE_POSES[this.emotion];
    const toArc = EXPRESSIVE_EYE_POSES[emotion];
    this.emotion = emotion;
    this.intensity = nextIntensity;
    const transitionId = ++this.transitionToken;
    const start = performance.now();
    const transitionDuration = 420;
    const tick = (now: number) => {
      if (transitionId !== this.transitionToken || this.destroyed) return;
      const progress = clamp01((now - start) / transitionDuration);
      this.render(interpolatePose(from, to, easeInOut(progress)), interpolateArcPose(fromArc, toArc, easeInOut(progress)));
      if (progress < 1) this.transitionRaf = requestAnimationFrame(tick); else this.render();
    };
    cancelAnimationFrame(this.transitionRaf);
    this.transitionRaf = requestAnimationFrame(() => tick(performance.now()));
    const signature: Partial<Record<LumineEmotion, LumineAnimation>> = {
      happy: "happyBlink", excited: "wiggle", curious: "curiousTilt", confused: "peek",
      sleepy: "sleepyBlink", surprised: "surprisedBlink", embarrassed: "peek", shy: "peek",
      proud: "blink", worried: "lookLeft", playful: "wiggle", loving: "happyBlink", jealous: "peek", wink: "happyBlink",
    };
    const animation = signature[emotion];
    if (animation) window.setTimeout(() => { if (transitionId === this.transitionToken) void this.play(animation, 2); }, 360);
  }
  setActivity(activity: LumineActivity) { this.activity = activity; this.render(); }
  setEyeMode(mode: ExpressiveEyeMode) {
    if (mode === this.eyeMode) return;
    const start = this.morphProgress;
    const target = mode === "arc" ? 1 : 0;
    const started = performance.now();
    cancelAnimationFrame(this.morphRaf);
    if (mode === "arc") this.eyeMode = "arc";
    const tick = (now: number) => {
      const progress = clamp01((now - started) / 520);
      this.morphProgress = lerp(start, target, easeInOut(progress));
      this.render();
      if (progress < 1) this.morphRaf = requestAnimationFrame(tick);
      else if (mode === "mask") { this.eyeMode = "mask"; this.render(); }
    };
    this.render();
    this.morphRaf = requestAnimationFrame(tick);
  }
  setGaze(x: number, y: number) { this.gaze = { x, y }; this.render(); }
  snapshot(): AvatarSnapshot { return { emotion: this.emotion, activity: this.activity, animation: this.animation }; }

  async play(animation: LumineAnimation, requestedPriority: Priority = 1) {
    if (this.destroyed || requestedPriority < this.priority) return;
    const id = ++this.commandId;
    cancelAnimationFrame(this.frame);
    this.priority = requestedPriority;
    this.animation = animation;
    const finish = async () => {
      if (id !== this.commandId) return;
      this.motion = neutralMotion();
      this.priority = 0;
      this.animation = "idle";
      this.render();
    };

    if (animation === "blink" || animation === "happyBlink" || animation === "surprisedBlink") {
      await this.blink(animation === "surprisedBlink" ? 105 : animation === "happyBlink" ? 135 : 155, id);
    } else if (animation === "slowBlink" || animation === "sleepyBlink") {
      await this.blink(animation === "sleepyBlink" ? 560 : 430, id);
    } else if (animation === "doubleBlink") {
      await this.blink(150, id); await wait(120); await this.blink(150, id);
    } else if (animation === "lookLeft" || animation === "lookRight") {
      await this.tween({ gazeX: animation === "lookLeft" ? -5 : 5 }, 520, id, "ease"); await wait(220); await this.tween({ gazeX: 0 }, 460, id, "ease");
    } else if (animation === "curiousTilt") {
      await this.tween({ rotation: -2, y: -1 }, 100, id, "ease"); await this.tween({ rotation: -7, y: 0 }, 380, id, "back"); await wait(300); await this.tween({ rotation: -4, y: 1 }, 150, id, "ease");
    } else if (animation === "wiggle") {
      await this.tween({ rotation: -3 }, 100, id, "back"); await this.tween({ rotation: 4 }, 150, id, "back"); await this.tween({ rotation: -1.5 }, 120, id, "ease");
    } else if (animation === "bounce") {
      await this.tween({ y: 2 }, 100, id, "ease"); await this.tween({ y: -7 }, 220, id, "back"); await this.tween({ y: 0 }, 280, id, "ease");
    } else if (animation === "softBreeze") {
      await this.tween({ rotation: -2.2, y: -1 }, 480, id, "ease"); await this.tween({ rotation: 2.2, y: 1 }, 620, id, "ease"); await this.tween({ rotation: 0, y: 0 }, 460, id, "ease");
    } else if (animation === "microTilt") {
      await this.tween({ rotation: -3, y: -1 }, 380, id, "ease"); await this.tween({ rotation: 1.5, y: 0 }, 420, id, "ease");
    } else if (animation === "tinySway") {
      await this.tween({ rotation: -2.4, gazeX: -2 }, 420, id, "ease"); await this.tween({ rotation: 2.7, gazeX: 2 }, 560, id, "ease"); await this.tween({ rotation: 0, gazeX: 0 }, 420, id, "ease");
    } else if (animation === "softBounce") {
      await this.tween({ y: -2 }, 260, id, "ease"); await this.tween({ y: 1 }, 360, id, "ease"); await this.tween({ y: 0 }, 300, id, "ease");
    } else if (animation === "pauseDrift") {
      await this.tween({ rotation: -1.4, gazeY: -1 }, 420, id, "ease"); await wait(350); await this.tween({ rotation: 1.2, gazeY: 1 }, 520, id, "ease"); await this.tween({ rotation: 0, gazeY: 0 }, 440, id, "ease");
    } else if (animation === "sleepy") {
      this.applyEmotion("sleepy"); await this.tween({ y: 3, rotation: 2 }, 900, id, "ease"); await wait(300);
    } else if (animation === "peek") {
      await this.tween({ gazeX: 5, rotation: 4, y: 2 }, 420, id, "ease"); await wait(500); await this.tween({ gazeX: 1, rotation: 1, y: 0 }, 420, id, "ease");
    } else {
      this.applyEmotion("surprised"); await this.tween({ y: 2 }, 70, id, "ease"); await this.tween({ y: -4, blink: 1.08 }, 120, id, "back"); await wait(200); await this.tween({ y: 0, blink: 1 }, 420, id, "ease");
    }
    await finish();
  }

  destroy() { this.destroyed = true; this.commandId++; cancelAnimationFrame(this.frame); cancelAnimationFrame(this.morphRaf); }

  private applyEmotion(emotion: LumineEmotion) { this.emotion = emotion; this.render(); }

  private render(expression = EXPRESSIONS[this.emotion], arc = EXPRESSIVE_EYE_POSES[this.emotion]) {
    const activeExpression = blendExpression(expression, this.intensity);
    const activityTilt = this.activity === "speaking" ? -0.5 : this.activity === "listening" ? 0.5 : 0;
    const activityY = this.activity === "listening" ? -1 : this.activity === "speaking" ? -0.5 : 0;
    this.object.style.transform = `translateY(${this.motion.y}px) rotate(${this.motion.rotation}deg)`;
    this.eyes.forEach((eye, index) => {
      if (this.eyeMode === "arc" && eye.element.parentNode !== eye.root) eye.root.appendChild(eye.element);
      if (this.eyeMode === "mask" && eye.element.parentNode !== eye.originalParent) eye.originalParent.insertBefore(eye.element, eye.originalNextSibling && eye.originalNextSibling.parentNode === eye.originalParent ? eye.originalNextSibling : null);
      const side = index === 0 ? -1 : 1;
      const adjustment = index === 0 ? activeExpression.left : activeExpression.right;
      const arcActive = this.eyeMode === "arc";
      const x = this.gaze.x + this.motion.gazeX + activeExpression.eyeOffset[0] * side + (adjustment?.eyeOffset?.[0] ?? 0) * side + (adjustment?.gazeX ?? 0) + (arcActive ? side * 10 : 0);
      // Body motion is applied to the external SVG once; do not apply it again to the eye wrappers.
      const y = this.gaze.y + this.motion.gazeY + activeExpression.eyeOffset[1] + (adjustment?.eyeOffset?.[1] ?? 0) + activeExpression.y + activityY + (adjustment?.gazeY ?? 0) + (adjustment?.y ?? 0) + (arcActive ? arc.y : 0);
      const tilt = activeExpression.tilt + activityTilt + (adjustment?.tilt ?? 0) + (arcActive ? arc.rotate : 0);
      const targetD = index === 0 ? arc.left : arc.right;
      eye.path.setAttribute("d", morphPath(eye.originalD, targetD, this.morphProgress));
      eye.path.setAttribute("fill", this.eyeMode === "arc" ? "#fffaf5" : eye.originalFill);
      eye.path.setAttribute("stroke", "none");
      eye.path.setAttribute("stroke-width", "0");
      eye.path.setAttribute("opacity", "1");
      const arcScale = arcActive ? arc.scale : 1;
      const maskScale = adjustment?.maskScale ?? activeExpression.maskScale;
      eye.element.setAttribute("transform", `translate(${x} ${y}) rotate(${tilt} ${eye.centerX} ${eye.centerY}) translate(${eye.centerX} ${eye.centerY}) scale(${maskScale[0] * arcScale} ${maskScale[1] * this.motion.blink * arcScale}) translate(${-eye.centerX} ${-eye.centerY})`);
    });
  }

  private blink(duration: number, id: number) {
    // The source SVG closes its mask by compressing it vertically; values above 1 visibly open it.
    return this.tween({ blink: 0.18 }, duration * 0.42, id, "ease").then(() => this.tween({ blink: 1 }, duration * 0.58, id, "ease"));
  }

  private tween(target: Partial<Motion>, duration: number, id: number, easing: "ease" | "back") {
    return new Promise<void>((resolve) => {
      const start = performance.now();
      const initial = { ...this.motion };
      const tick = (now: number) => {
        if (id !== this.commandId) { resolve(); return; }
        const progress = clamp01((now - start) / duration);
        const eased = easing === "back" ? easeOutBack(progress) : easeInOut(progress);
        (Object.keys(target) as Array<keyof Motion>).forEach((key) => { this.motion[key] = lerp(initial[key], target[key] as number, eased); });
        this.render();
        if (progress < 1) this.frame = requestAnimationFrame(tick); else resolve();
      };
      this.frame = requestAnimationFrame(tick);
    });
  }
}

function interpolatePose(from: ExpressionPose, to: ExpressionPose, amount: number): ExpressionPose {
  return {
    maskScale: [lerp(from.maskScale[0], to.maskScale[0], amount), lerp(from.maskScale[1], to.maskScale[1], amount)],
    eyeOffset: [lerp(from.eyeOffset[0], to.eyeOffset[0], amount), lerp(from.eyeOffset[1], to.eyeOffset[1], amount)],
    tilt: lerp(from.tilt, to.tilt, amount),
    y: lerp(from.y, to.y, amount),
    left: interpolateAdjustment(from.left, to.left, amount),
    right: interpolateAdjustment(from.right, to.right, amount),
  };
}

function interpolateAdjustment(from: EyeAdjustment | undefined, to: EyeAdjustment | undefined, amount: number): EyeAdjustment | undefined {
  if (!from && !to) return undefined;
  const start = from ?? {};
  const end = to ?? {};
  return {
    maskScale: [lerp(start.maskScale?.[0] ?? 1, end.maskScale?.[0] ?? 1, amount), lerp(start.maskScale?.[1] ?? 1, end.maskScale?.[1] ?? 1, amount)],
    eyeOffset: [lerp(start.eyeOffset?.[0] ?? 0, end.eyeOffset?.[0] ?? 0, amount), lerp(start.eyeOffset?.[1] ?? 0, end.eyeOffset?.[1] ?? 0, amount)],
    tilt: lerp(start.tilt ?? 0, end.tilt ?? 0, amount),
    y: lerp(start.y ?? 0, end.y ?? 0, amount),
    gazeX: lerp(start.gazeX ?? 0, end.gazeX ?? 0, amount),
    gazeY: lerp(start.gazeY ?? 0, end.gazeY ?? 0, amount),
  };
}

function blendExpression(expression: ExpressionPose, intensity: number): ExpressionPose {
  return {
    maskScale: [lerp(1, expression.maskScale[0], intensity), lerp(1, expression.maskScale[1], intensity)],
    eyeOffset: [expression.eyeOffset[0] * intensity, expression.eyeOffset[1] * intensity],
    tilt: expression.tilt * intensity,
    y: expression.y * intensity,
    left: blendAdjustment(expression.left, intensity),
    right: blendAdjustment(expression.right, intensity),
  };
}

function blendAdjustment(adjustment: EyeAdjustment | undefined, intensity: number): EyeAdjustment | undefined {
  if (!adjustment) return undefined;
  return {
    maskScale: adjustment.maskScale ? [lerp(1, adjustment.maskScale[0], intensity), lerp(1, adjustment.maskScale[1], intensity)] : undefined,
    eyeOffset: adjustment.eyeOffset ? [adjustment.eyeOffset[0] * intensity, adjustment.eyeOffset[1] * intensity] : undefined,
    tilt: (adjustment.tilt ?? 0) * intensity,
    y: (adjustment.y ?? 0) * intensity,
    gazeX: (adjustment.gazeX ?? 0) * intensity,
    gazeY: (adjustment.gazeY ?? 0) * intensity,
  };
}

function interpolateArcPose(from: typeof EXPRESSIVE_EYE_POSES[LumineEmotion], to: typeof EXPRESSIVE_EYE_POSES[LumineEmotion], amount: number) {
  return {
    left: interpolatePath(from.left, to.left, amount),
    right: interpolatePath(from.right, to.right, amount),
    opacity: lerp(from.opacity, to.opacity, amount),
    strokeWidth: lerp(from.strokeWidth, to.strokeWidth, amount),
    scale: lerp(from.scale, to.scale, amount),
    y: lerp(from.y, to.y, amount),
    rotate: lerp(from.rotate, to.rotate, amount),
  };
}

function interpolatePath(from: string, to: string, amount: number) {
  return interpolate(from, to, { maxSegmentLength: 8 })(amount);
}

function morphPath(from: string, to: string, amount: number) {
  return interpolate(from, to, { maxSegmentLength: 8 })(amount);
}
