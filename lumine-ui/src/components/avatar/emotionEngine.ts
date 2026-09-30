import { easeInOut, easeOutBack, wait } from "./animationUtils";
import { interpolate } from "flubber";
import { EXPRESSIONS, type ExpressionPose, type EyeAdjustment } from "./expressions";
import { EXPRESSIVE_EYE_POSES, type ExpressiveEyeMode } from "./expressiveEyePaths";
import type { AvatarSnapshot, LumineActivity, LumineAnimation, LumineEmotion } from "./avatarTypes";

type Eye = { element: SVGGElement; path: SVGPathElement; originalParent: SVGElement; originalNextSibling: ChildNode | null; root: SVGSVGElement; centerX: number; centerY: number; originalD: string; originalFill: string };
type Motion = { rotation: number; x: number; y: number; blink: number; gazeX: number; gazeY: number; squash: number };
type Priority = 0 | 1 | 2 | 3;

const neutralMotion = (): Motion => ({ rotation: 0, x: 0, y: 0, blink: 1, gazeX: 0, gazeY: 0, squash: 0 });
const lerp = (from: number, to: number, amount: number) => from + (to - from) * amount;
const clamp01 = (value: number) => Math.max(0, Math.min(1, value));
const EMOTION_DEBUG = import.meta.env.VITE_LUMINE_DEBUG_EMOTION === "true";

/**
 * How much of a channel's full value a 1.0 `squash` is worth, and how much
 * vertical give-up pays for it.
 *
 * The `0.85` is the volume-preservation factor. For a small `s` the area of
 * `scale(1+s, 1-ks)` is `1 + s(1-k) - k·s²`, so `k = 1` holds area exactly to
 * first order and `k < 1` gives a little of it back. Slightly under-correcting is
 * deliberate: exact preservation at `s = 0.5` would need `1/1.5 = 0.667`, and a
 * body that does not visibly grow when it inhales reads as broken rather than as
 * volumetric.
 *
 * **This channel is for authored gestures only.** It used to also be driven by
 * the microphone, on every frame, and that was the mistake: a volume-preserved
 * squeeze applied continuously to a rounded silhouette reads as jelly, not as a
 * character. The audio path now moves the *height* and the *eyes* and never the
 * shape — see `render`. A brief authored squash inside `settle` or `softBounce`
 * is a gesture; the same maths reacting to an RMS sixty times a second is a
 * lava lamp.
 */
const SQUASH_Y_FACTOR = 0.85;

/** The body's origin for an authored squash. Below centre, so it settles rather than inflates. */
const TRANSFORM_ORIGIN = "50% 58%";

/**
 * Motion is authored *and* rendered in raw pixels — deliberately no viewport
 * factor.
 *
 * `GESTURES` was written against the object as the shipped app laid it out:
 * 154x154 inside a 174x174 disc, where a `-7px` bounce is `-7px`. A
 * `box / 250` multiplier sat here for a while, and taking it out is the point
 * rather than a regression. It scaled the *reach* down for everyone — 188px of
 * object meant `0.75x` at desktop — so the avatar came out calmer than the
 * original at every size. Consistency was the reason it existed; being quieter
 * was a side effect nobody asked for.
 *
 * What it was genuinely buying is the shrinking disc. `.presence` clamps with
 * the window, so the ring the head travels inside narrows from 28px on a 174px
 * body to 16px on the 100px one 800x600 renders, while the translate holds
 * still. The worst frame is that translate plus the authored squash's vertical
 * growth, and it lands inside both rings — but the small end is where the slack
 * is spent, so any future increase in gesture amplitude has to be measured
 * against the 100px case first, not the desktop one.
 *
 * Rotation and squash were never scaled: a degree and a ratio are already
 * dimensionless, and scaling them would change the authored *shape* of a bounce
 * rather than its reach. The gaze channels need no factor either — they are
 * written inside the SVG, in user units, so they scale with the artwork.
 */

/**
 * The resting breath, in pixels of rise rather than in scale.
 *
 * It was `0.028` of a vertical squash, which meant the *baseline* motion of this
 * avatar — the one playing while nothing else was — was the same deformation the
 * microphone later drove. Taking the deformation out of the breath and making it
 * a lift is both the fix and an improvement: a body that rises and settles is
 * what breathing looks like, and a body that squeezes is what a stress ball does.
 *
 * Slower than a resting human breath on purpose. This is a small, still creature,
 * and the point of the motion is that it is there if you look and absent if you
 * don't — a 1.5s cycle crosses into "looping", which is the exact read the four
 * `avatar-*` CSS keyframes had.
 */
const BREATH_PERIOD_S = 4.6;
const BREATH_RISE_PX = 2.4;

/** How fast energy rises, and how fast it falls. Deliberately not the same. */
const ENERGY_ATTACK = 0.34;
const ENERGY_RELEASE = 0.09;

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

  /**
   * Lumine's real voice amplitude, 0..1, from the analyser in the voice layer.
   *
   * This is the single change that makes the avatar a readout rather than a
   * decoration. Everything else in this file runs on a clock, so the face was
   * animated during silence and during a conversation where the mouth should
   * have been moving. `energy` is what her *actual* voice is doing, so the body
   * moves when she speaks and stills when she does not.
   *
   * Held raw and smoothed separately, because the raw signal is far too fast to
   * drive a silhouette — at 60fps a peak arrives in a frame or two and the body
   * would strobe. `energySmoothed` is what `render` reads.
   */
  private energy = 0;
  private energySmoothed = 0;

  /**
   * The engine's own frame loop, running for as long as it is connected.
   *
   * It exists because two of the new motions cannot be tweens. The breath is
   * continuous by definition — a tween returns to neutral and stops — and the
   * energy response is a follower of a signal arriving 60 times a second, which
   * is not allowed to be a tween at all. Both are *additive* on top of whatever
   * gesture is playing, which is why they are applied in `render` rather than
   * written into `motion`: a gesture has to be able to finish and zero itself
   * without also cancelling the breath.
   */
  private vitalityRaf = 0;
  private breathPhase = 0;
  private lastVitalityAt = 0;

  /**
   * How many `setFace` calls are currently on the stack.
   *
   * The individual setters each end in `render()`, which is correct when one of
   * them is called on its own and wasteful when all three are called together —
   * as they are on every room-state change. This counter lets `setFace` apply
   * three inputs and paint once, without any setter needing to know it is being
   * called as part of a batch. `try/finally` rather than a matching decrement at
   * the end, because a setter that throws would otherwise leave the engine
   * permanently refusing to draw.
   */
  private batchDepth = 0;

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
    // The engine owns `transform` from here on, so the origin is set once and the
    // property is never transitioned. A `transition: transform` lagged every tween
    // by its full duration — a 420ms emotion transition smearing a 190ms nod —
    // which is part of why the motion read as mush rather than as gesture.
    this.object.style.transformOrigin = TRANSFORM_ORIGIN;
    // Only opacity is transitioned now. The entrance *was* a transform rise and it
    // never ran: the synchronous `render()` below overwrote the `translateY(6px)`
    // before a frame had painted to animate it from.
    this.object.style.opacity = "0";
    this.object.style.transition = "opacity 780ms ease";
    this.render();
    requestAnimationFrame(() => { this.object.style.opacity = "1"; });
    this.startVitality();
    void this.play("blink", 2).catch(() => undefined);
    return true;
  }

  /**
   * Lumine's real voice amplitude, 0..1. Anything outside is clamped.
   *
   * Clamped rather than trusted because the caller is an RMS measured off a
   * remote WebRTC track, and a level that reached `render` unbounded would lift
   * the silhouette off its baseline within a frame or two.
   *
   * What the value is allowed to *do* is the important part, and it changed. It
   * used to add to a `scale()` — a volume-preserved squash on every frame — which
   * made the avatar deform like a water balloon whenever she spoke. It now drives
   * three non-deforming channels: a rise in `y`, an opening of the eyelids, and a
   * few tenths of a degree of head tilt. She reacts to her own voice without
   * changing shape, which is the difference between a character and a blob.
   */
  setEnergy(level: number) {
    this.energy = clamp01(level);
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

  /**
   * The face's three inputs as one operation, applied with a single paint.
   *
   * `setEmotion`, `setActivity` and `setEyeMode` are all still usable on their
   * own — the Avatar Lab drives them independently, one control at a time, and
   * that is the right shape for a control. The home screen does not: a room-state
   * change arrives as one fact and lands as three setter calls, and each of the
   * three was painting the avatar before the next one had run. The result was
   * never wrong — all three committed before the compositor drew — but it made a
   * state change cost three full passes over the silhouette, the eyes and the
   * expressive arc.
   *
   * The batching is in `render` rather than here, so no caller has to remember
   * to pair a depth increment. This function increments, delegates to the
   * setters unchanged, decrements in a `finally`, and then paints.
   */
  setFace(
    emotion: LumineEmotion,
    intensity: number,
    activity: LumineActivity,
    eyeMode: ExpressiveEyeMode,
  ) {
    this.batchDepth++;
    try {
      this.setEmotion(emotion, intensity);
      this.setActivity(activity);
      this.setEyeMode(eyeMode);
    } finally {
      this.batchDepth--;
    }
    this.render();
  }

  setGaze(x: number, y: number) {
    this.gaze = { x, y };
    // The gaze is written ~60 times a second from the pointer loop, and the
    // vitality loop already repaints on every frame with no invitation. Painting
    // from here as well meant the steady state was two full renders per frame
    // for one transform — the difference between the avatar reading as fluid and
    // reading as merely animated, once the expressive morphs are also running.
    // The render is kept for the case where the loop is *not* running, so a
    // gaze set outside a session still lands.
    if (!this.vitalityRaf) this.render();
  }
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
      await this.tween({ y: 2, squash: 0.05 }, 100, id, "ease"); await this.tween({ y: -7, squash: -0.06 }, 220, id, "back"); await this.tween({ y: 0, squash: 0 }, 280, id, "ease");
    } else if (animation === "softBreeze") {
      await this.tween({ rotation: -2.2, y: -1 }, 480, id, "ease"); await this.tween({ rotation: 2.2, y: 1 }, 620, id, "ease"); await this.tween({ rotation: 0, y: 0 }, 460, id, "ease");
    } else if (animation === "microTilt") {
      await this.tween({ rotation: -3, y: -1 }, 380, id, "ease"); await this.tween({ rotation: 1.5, y: 0 }, 420, id, "ease");
    } else if (animation === "tinySway") {
      await this.tween({ rotation: -2.4, gazeX: -2 }, 420, id, "ease"); await this.tween({ rotation: 2.7, gazeX: 2 }, 560, id, "ease"); await this.tween({ rotation: 0, gazeX: 0 }, 420, id, "ease");
    } else if (animation === "softBounce") {
      await this.tween({ y: -2, squash: -0.04 }, 260, id, "ease"); await this.tween({ y: 1, squash: 0.045 }, 360, id, "ease"); await this.tween({ y: 0, squash: 0 }, 300, id, "ease");
    } else if (animation === "pauseDrift") {
      await this.tween({ rotation: -1.4, gazeY: -1 }, 420, id, "ease"); await wait(350); await this.tween({ rotation: 1.2, gazeY: 1 }, 520, id, "ease"); await this.tween({ rotation: 0, gazeY: 0 }, 440, id, "ease");
    } else if (animation === "sleepy") {
      this.applyEmotion("sleepy"); await this.tween({ y: 3, rotation: 2 }, 900, id, "ease"); await wait(300);
    } else if (animation === "peek") {
      await this.tween({ gazeX: 5, rotation: 4, y: 2 }, 420, id, "ease"); await wait(500); await this.tween({ gazeX: 1, rotation: 1, y: 0 }, 420, id, "ease");
    } else if (animation === "reaching") {
      // Connecting. She leans *toward* the room she is trying to get into, and
      // then checks the space either side of it — because nothing has answered yet
      // and the searching is the entire point of the gesture. The lean is in `x`
      // and the anticipation is in `squash`, so this reads as a body moving rather
      // than a picture getting bigger. This is the face `connecting` was missing.
      await this.tween({ x: 3, squash: -0.045, rotation: -2.5 }, 340, id, "ease");
      await this.tween({ gazeX: -5.5, rotation: -6, x: 1.5 }, 520, id, "ease");
      await wait(260);
      await this.tween({ gazeX: 6, rotation: 5, x: 4.5 }, 620, id, "ease");
      await wait(240);
      await this.tween({ gazeX: 0, rotation: -1, x: 0, squash: -0.02 }, 460, id, "ease");
    } else if (animation === "settle") {
      // Connected. A body arriving: it lands compressed, then springs *past*
      // neutral before resting. The overshoot is the arrival — without it this is
      // `softBounce` with a different number in it.
      await this.tween({ y: 2.5, squash: 0.075, blink: 0.82 }, 180, id, "ease");
      await this.tween({ y: -3, squash: -0.055, blink: 1.06 }, 260, id, "back");
      await this.tween({ y: 0, squash: 0.012, blink: 1 }, 420, id, "ease");
    } else if (animation === "inhale") {
      // A slow take of breath, then a hold. `blink` under 1 is a half-lidded eye,
      // and that is what makes it read as an inhale rather than as a squash.
      await this.tween({ squash: 0.05, y: 2.5, blink: 0.84 }, 900, id, "ease"); await wait(180);
    } else if (animation === "exhale") {
      await this.tween({ squash: -0.034, y: -1.6, blink: 1.05 }, 780, id, "ease"); await wait(160);
    } else if (animation === "wonder") {
      // Held long enough to be a mood rather than a tic. 700ms of eye-up and
      // head-tilt is a different read from 300ms, and the duration was the whole
      // problem — the old `curiousTilt` settled before it could mean anything.
      await this.tween({ rotation: -5.5, x: -2.5, gazeY: -3.2, squash: -0.03 }, 460, id, "ease");
      await wait(700);
      await this.tween({ rotation: 0, x: 0, gazeY: 0, squash: 0 }, 520, id, "ease");
    } else if (animation === "saysYes") {
      // Two dips, the second smaller. One dip is a bounce; two is agreement.
      await this.tween({ y: 2.6, squash: 0.055, blink: 0.88 }, 190, id, "ease");
      await this.tween({ y: -1.6, squash: -0.03 }, 200, id, "back");
      await wait(90);
      await this.tween({ y: 1.5, squash: 0.03 }, 170, id, "ease");
      await this.tween({ y: 0, squash: 0, blink: 1 }, 300, id, "ease");
    } else if (animation === "listensDeeply") {
      // Leans in on `x`, which no earlier animation could do — every one of them
      // moved in `y` or `rotation`, so a face could only ever move up and down.
      // Attention as an approach rather than as a nod.
      await this.tween({ x: 4.5, rotation: 2.5, y: 0.5, squash: -0.026, blink: 1.04 }, 560, id, "ease");
      await wait(620);
      await this.tween({ x: 0, rotation: 0, squash: 0, blink: 1 }, 480, id, "ease");
    } else {
      this.applyEmotion("surprised"); await this.tween({ y: 2 }, 70, id, "ease"); await this.tween({ y: -4, blink: 1.08 }, 120, id, "back"); await wait(200); await this.tween({ y: 0, blink: 1 }, 420, id, "ease");
    }
    await finish();
  }

  destroy() { this.destroyed = true; this.commandId++; cancelAnimationFrame(this.frame); cancelAnimationFrame(this.morphRaf); cancelAnimationFrame(this.vitalityRaf); }

  private applyEmotion(emotion: LumineEmotion) { this.emotion = emotion; this.render(); }

  /**
   * The always-on loop: breath, energy envelope, and a drift that is not a loop.
   *
   * The third channel is the least obvious and the most valuable. One sinusoid is
   * one sinusoid, and a body that only ever moves on a single period reads as a
   * machine however soft the easing is. `drift` folds in two further periods at
   * rates unrelated to the breath, so the body reaches positions the breath alone
   * would never hold and the cycle never visibly repeats.
   */
  private startVitality() {
    if (this.vitalityRaf) return;
    this.lastVitalityAt = performance.now();
    const tick = (now: number) => {
      if (this.destroyed) return;
      // Clamped delta. A backgrounded tab hands back one enormous `now`, and an
      // unclamped phase step teleports the breath on return.
      const dt = Math.min(0.1, Math.max(0, (now - this.lastVitalityAt) / 1000));
      this.lastVitalityAt = now;
      this.breathPhase = (this.breathPhase + dt / BREATH_PERIOD_S) % 1;

      // Asymmetric envelope: fast attack, slow release. A real one does this, and
      // a symmetric follower makes speech look like a train of half-spikes rather
      // than like an envelope around it.
      this.energySmoothed += (this.energy - this.energySmoothed) * (this.energy > this.energySmoothed ? ENERGY_ATTACK : ENERGY_RELEASE);

      this.render();
      this.vitalityRaf = requestAnimationFrame(tick);
    };
    this.vitalityRaf = requestAnimationFrame(tick);
  }

  private render(expression = EXPRESSIONS[this.emotion], arc = EXPRESSIVE_EYE_POSES[this.emotion]) {
    // Suppressed while `setFace` is mid-batch. See `batchDepth`: the three
    // setters it applies each end here, and three paints in one tick is three
    // times the work for a result only one of them can show. `setFace` makes the
    // final call itself, with the depth back at zero.
    if (this.batchDepth > 0) return;
    const activeExpression = blendExpression(expression, this.intensity);
    const activityTilt = this.activity === "speaking" ? -0.5 : this.activity === "listening" ? 0.5 : 0;
    const activityY = this.activity === "listening" ? -1 : this.activity === "speaking" ? -0.5 : 0;

    // The additive channels, computed here rather than folded into `motion`, so
    // that a gesture finishing and zeroing itself cannot cancel the breath.
    //
    // `quiet` fades the breath out as she starts talking: the energy term is
    // already driving the body from real audio, and a second cycle on top of that
    // reads as a tremor. It is *not* faded in silence, which is the case the
    // breath exists for.
    //
    // None of these three touch the silhouette's *shape*. The breath is a lift,
    // the energy is a lift and an eye-opening, and the drift is a rotation. The
    // `scale()` below only ever receives a value an authored gesture wrote — at
    // rest it is omitted from the string entirely rather than written as
    // `scale(1,1)`, so the resting transform is a pure translate plus a rotate.
    const quiet = 1 - this.energySmoothed * 0.65;
    const phase = this.breathPhase * Math.PI * 2;
    const breathRise = -Math.sin(phase) * BREATH_RISE_PX * quiet;
    const drift = Math.sin(phase * 0.41 + 1.7) * 0.9 * quiet;
    // Energy lifts rather than inflates. Loud is a body rising toward the
    // listener and opening its eyes; it is never a body getting wider.
    const energyRise = -this.energySmoothed * 2.4;
    const energyTilt = this.energySmoothed * 0.7;

    // Named `body*` because the eye loop below declares its own `x`/`y` for the
    // eye's position, and two different meanings of `x` in one function is a
    // bug waiting for someone to rename one of them.
    const bodyX = this.motion.x;
    const bodyY = this.motion.y + breathRise + energyRise;
    const rotation = this.motion.rotation + drift + energyTilt;
    // Authored gestures only. `Math.abs(...) > epsilon` decides whether `scale`
    // appears in the string at all, because a permanent `scale(1,1)` still puts
    // the element in its own compositing path and rounds to a hairline off 1.0
    // — which is exactly the "slightly wrong shape" this whole change exists to
    // stop.
    const authored = Math.abs(this.motion.squash) > 0.0005;

    this.object.style.transform =
      `translate(${bodyX}px, ${bodyY}px) rotate(${rotation}deg)` +
      (authored ? ` scale(${1 + this.motion.squash}, ${1 - this.motion.squash * SQUASH_Y_FACTOR})` : "");
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
      eye.path.setAttribute("d", blendPath(eye.originalD, targetD, this.morphProgress));
      eye.path.setAttribute("fill", this.eyeMode === "arc" ? "#fffaf5" : eye.originalFill);
      eye.path.setAttribute("stroke", "none");
      eye.path.setAttribute("stroke-width", "0");
      eye.path.setAttribute("opacity", "1");
      const arcScale = arcActive ? arc.scale : 1;
      const maskScale = adjustment?.maskScale ?? activeExpression.maskScale;
      // Energy opens the eyes. This is the loudest thing the audio is allowed to
      // do to the face, and it is deliberately the eyes rather than the head:
      // widening the lids is legible as attentiveness, where a head that grows
      // with the volume is a bug report.
      const lid = 1 + this.energySmoothed * 0.11;
      eye.element.setAttribute("transform", `translate(${x} ${y}) rotate(${tilt} ${eye.centerX} ${eye.centerY}) translate(${eye.centerX} ${eye.centerY}) scale(${maskScale[0] * arcScale} ${maskScale[1] * this.motion.blink * arcScale * lid}) translate(${-eye.centerX} ${-eye.centerY})`);
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
    left: blendPath(from.left, to.left, amount),
    right: blendPath(from.right, to.right, amount),
    opacity: lerp(from.opacity, to.opacity, amount),
    strokeWidth: lerp(from.strokeWidth, to.strokeWidth, amount),
    scale: lerp(from.scale, to.scale, amount),
    y: lerp(from.y, to.y, amount),
    rotate: lerp(from.rotate, to.rotate, amount),
  };
}

/**
 * One flubber interpolator per distinct pair of shapes, and the polygon it
 * produces re-emitted as a curve.
 *
 * ## The cost, which was the real bug
 *
 * This used to be `interpolate(from, to, opts)(amount)` written out inline at
 * both call sites. That constructs a **new interpolator on every call**, and
 * constructing one is not arithmetic: flubber parses each path, and for anything
 * containing a curve it creates a throwaway SVG path element and calls
 * `getPointAtLength()` once per sample — a geometry solve against the document.
 * `morphPath` sits inside `render`, which the vitality loop calls every frame,
 * so the avatar was solving two eye paths from scratch **sixty times a second,
 * forever, to produce the byte-identical string it produced last frame**.
 * Only the cost was running. That is what "the morphing isn't smooth" was: the
 * animation was fighting an unbounded per-frame allocation and a DOM geometry
 * query that had nothing to do with whether the shape had changed.
 *
 * The interpolator is now built once per pair and retained. Construction is
 * O(samples) DOM work; a call is O(points) arithmetic. During a 420ms
 * transition that is ~25 cheap calls against one build, and outside a
 * transition `morphProgress` sits at 0 or 1 where flubber short-circuits to the
 * original path and nothing is solved at all.
 *
 * ## Why the output is re-emitted as curves
 *
 * Flubber's own output is `M` + ring.join(`L`) + `Z`. It has to be a polygon:
 * the two shapes have different segment counts and different commands, and the
 * only representation guaranteed to exist for both is a list of points. So every
 * intermediate frame was a ring of straight chords — the faceting.
 *
 * `smoothClosed` re-emits that ring as a closed Catmull-Rom spline through the
 * same points. Same geometry, C1 continuity everywhere, no facets. It cannot be
 * done by asking flubber for curves (it has no such mode) and it cannot be done
 * by matching control points between poses (the 28 eye poses have two, three and
 * four segments each and share no control-point set) — which is precisely why
 * flubber is still here and only its *emission* is being replaced.
 *
 * ## When nothing is smoothed
 *
 * Near `t = 0` and `t = 1` flubber returns the *original* path string rather
 * than its own — and the originals (`eye.originalD`'s arcs, the poses' `Q`/`C`)
 * are already curves. Those pass through `parseRing` as `null` and are returned
 * untouched: re-emitting an already-smooth curve as a spline through points
 * sampled off it would only be a way of losing detail it already has.
 */
const MAX_SEGMENT_LENGTH = 4;

/**
 * How many shape pairs stay built. The ring key is `from` + NUL + `to`, and the
 * reachable set is small — the eye morphs between one original and three targets
 * per eye, and the arc transitions between poses an emotion actually reaches —
 * so a generous ceiling makes eviction an edge case rather than a per-transition
 * cost. 96 with an LRU touch rather than a plain FIFO: the pair being used *now*
 * is the one pair that must never be the one evicted mid-transition.
 */
const MORPHER_LIMIT = 96;

const morphers = new Map<string, (t: number) => string>();

function blendPath(from: string, to: string, amount: number): string {
  const key = from + "\u0000" + to;
  const cached = morphers.get(key);
  if (cached) {
    // `delete` then `set` moves the key to the end of the Map's insertion
    // order, which is what makes the eviction loop below an LRU. Two Map
    // operations rather than a timestamp the eviction loop would have to sort.
    morphers.delete(key);
    morphers.set(key, cached);
    return cached(amount);
  }

  const raw = interpolate(from, to, { maxSegmentLength: MAX_SEGMENT_LENGTH });
  const built = (t: number) => {
    const d = raw(t);
    const ring = parseRing(d);
    return ring ? smoothClosed(ring) : d;
  };

  morphers.set(key, built);
  while (morphers.size > MORPHER_LIMIT) {
    const oldest = morphers.keys().next();
    if (oldest.done) break;
    morphers.delete(oldest.value);
  }
  return built(amount);
}

/**
 * Pull the points back out of flubber's emission, or say it isn't one.
 *
 * The check is deliberately `Number.isFinite` on each half rather than a regex
 * over the numbers: flubber writes raw JS number strings, which include `1e-7`
 * and `-0`, and a pattern that had to anticipate those would be wrong the first
 * time a coordinate rounded into exponent form. Parsing rejects exactly the
 * strings that must not be re-tokenised — `A`-command arcs from `eye.originalD`
 * and the poses' `Q`/`C` — because their tokens do not parse as two numbers, and
 * it accepts `Mx,yLx,y…Z` because every token does.
 */
function parseRing(d: string): [number, number][] | null {
  if (d.length < 9 || d[0] !== "M" || d[d.length - 1] !== "Z") return null;
  const pairs = d.slice(1, -1).split("L");
  if (pairs.length < 3) return null;
  const ring: [number, number][] = [];
  for (const pair of pairs) {
    const comma = pair.indexOf(",");
    if (comma <= 0 || comma === pair.length - 1) return null;
    const x = Number(pair.slice(0, comma));
    const y = Number(pair.slice(comma + 1));
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    ring.push([x, y]);
  }
  return ring;
}

/**
 * A closed Catmull-Rom spline through every point of the ring, emitted as cubic
 * cubics.
 *
 * For points `P0..P3` the standard uniform conversion puts the first control
 * point at `P1 + (P2 - P0) / 6` and the second at `P2 - (P3 - P1) / 6`. Doing
 * that for each consecutive triple around the ring, with the neighbours taken
 * modulo the count, produces a curve that *passes through* every input point
 * with a continuous tangent at each one — which is the entire difference between
 * a smooth shape and a polygon, and it needs no correspondence between the two
 * shapes' control points.
 *
 * The `/6` is what makes it Catmull-Rom rather than an arbitrary spline: it is
 * the factor at which the tangent at `P1` equals the chord `P2 - P0`. Larger
 * would overshoot into loops at tight corners; smaller would flatten the curve
 * back toward the chords it is replacing.
 */
function smoothClosed(ring: [number, number][]): string {
  const count = ring.length;
  if (count < 4) return "M" + ring.join("L") + "Z";
  let d = "M" + ring[0][0] + "," + ring[0][1];
  for (let i = 0; i < count; i++) {
    const p0 = ring[(i - 1 + count) % count];
    const p1 = ring[i];
    const p2 = ring[(i + 1) % count];
    const p3 = ring[(i + 2) % count];
    d += "C" + (p1[0] + (p2[0] - p0[0]) / 6) + "," + (p1[1] + (p2[1] - p0[1]) / 6);
    d += " " + (p2[0] - (p3[0] - p1[0]) / 6) + "," + (p2[1] - (p3[1] - p1[1]) / 6);
    d += " " + p2[0] + "," + p2[1];
  }
  return d + "Z";
}
