import { easeInOut, easeOutBack, wait } from "./animationUtils";
import { EXPRESSIONS, type ExpressionPose } from "./expressions";
import type { AvatarSnapshot, LumineActivity, LumineAnimation, LumineEmotion } from "./avatarTypes";

type Eye = { element: SVGGElement; centerX: number; centerY: number };
type Motion = { rotation: number; y: number; blink: number; gazeX: number; gazeY: number };
type Priority = 0 | 1 | 2 | 3;

const neutralMotion = (): Motion => ({ rotation: 0, y: 0, blink: 1, gazeX: 0, gazeY: 0 });
const lerp = (from: number, to: number, amount: number) => from + (to - from) * amount;
const clamp01 = (value: number) => Math.max(0, Math.min(1, value));

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
      return { element: wrapper, centerX: box.x + box.width / 2, centerY: box.y + box.height / 2 };
    });
    this.render();
    return true;
  }

  setEmotion(emotion: LumineEmotion) {
    if (emotion === this.emotion) return;
    const from = EXPRESSIONS[this.emotion];
    const to = EXPRESSIONS[emotion];
    this.emotion = emotion;
    const transitionId = ++this.transitionToken;
    const start = performance.now();
    const tick = (now: number) => {
      if (transitionId !== this.transitionToken || this.destroyed) return;
      const progress = clamp01((now - start) / 260);
      this.render(interpolatePose(from, to, easeInOut(progress)));
      if (progress < 1) this.transitionRaf = requestAnimationFrame(tick); else this.render();
    };
    cancelAnimationFrame(this.transitionRaf);
    this.transitionRaf = requestAnimationFrame(() => tick(performance.now()));
    const signature: Partial<Record<LumineEmotion, LumineAnimation>> = {
      happy: "bounce", excited: "wiggle", curious: "curiousTilt", confused: "peek",
      sleepy: "sleepy", surprised: "surprise", embarrassed: "peek", shy: "peek",
      proud: "bounce", worried: "lookLeft", playful: "wiggle",
    };
    const animation = signature[emotion];
    if (animation) window.setTimeout(() => { if (transitionId === this.transitionToken) void this.play(animation, 2); }, 270);
  }
  setActivity(activity: LumineActivity) { this.activity = activity; this.render(); }
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
    } else if (animation === "sleepy") {
      this.applyEmotion("sleepy"); await this.tween({ y: 3, rotation: 2 }, 900, id, "ease"); await wait(300);
    } else if (animation === "peek") {
      await this.tween({ gazeX: 5, rotation: 4, y: 2 }, 420, id, "ease"); await wait(500); await this.tween({ gazeX: 1, rotation: 1, y: 0 }, 420, id, "ease");
    } else {
      this.applyEmotion("surprised"); await this.tween({ y: 2 }, 70, id, "ease"); await this.tween({ y: -4, blink: 1.08 }, 120, id, "back"); await wait(200); await this.tween({ y: 0, blink: 1 }, 420, id, "ease");
    }
    await finish();
  }

  destroy() { this.destroyed = true; this.commandId++; cancelAnimationFrame(this.frame); }

  private applyEmotion(emotion: LumineEmotion) { this.emotion = emotion; this.render(); }

  private render(expression = EXPRESSIONS[this.emotion]) {
    const activityTilt = this.activity === "speaking" ? -0.5 : this.activity === "listening" ? 0.5 : 0;
    const activityY = this.activity === "listening" ? -1 : this.activity === "speaking" ? -0.5 : 0;
    this.object.style.transform = `translateY(${this.motion.y}px) rotate(${this.motion.rotation}deg)`;
    this.eyes.forEach((eye, index) => {
      const side = index === 0 ? -1 : 1;
      const x = this.gaze.x + this.motion.gazeX + expression.eyeOffset[0] * side;
      // Body motion is applied to the external SVG once; do not apply it again to the eye wrappers.
      const y = this.gaze.y + this.motion.gazeY + expression.eyeOffset[1] + expression.y + activityY;
      const tilt = expression.tilt + activityTilt;
      eye.element.setAttribute("transform", `translate(${x} ${y}) rotate(${tilt} ${eye.centerX} ${eye.centerY}) translate(${eye.centerX} ${eye.centerY}) scale(${expression.maskScale[0]} ${expression.maskScale[1] * this.motion.blink}) translate(${-eye.centerX} ${-eye.centerY})`);
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
  };
}
