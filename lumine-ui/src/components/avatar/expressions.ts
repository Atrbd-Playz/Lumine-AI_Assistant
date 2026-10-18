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

/**
 * The resting face, and the distance every other face is measured from.
 *
 * ## Why these poses are roughly twice the size they were
 *
 * The first table carried a note calling itself "restrained mask poses", and
 * restrained is what it was: `happy` changed an eye's height by 18%, tilted
 * nothing, and moved the face by one unit of a 250-unit sheet. At the size the
 * badge actually renders — a 118px head inside a 174px disc — that is a pixel
 * or two, so "the expressions don't show" was a measurement rather than a
 * mood.
 *
 * The rewrite takes its vocabulary from **manpu** (漫符) — the symbol set manga
 * built to make a feeling readable at a glance — and from the psychology
 * underneath the whole style: *kindchenschema*, baby schema, the finding that
 * a round face with **big eyes** reads as cute and is why anime enlarges the
 * eye instead of detailing it. Four axes, in the order they carry:
 *
 * * **Eye height is the primary channel.** Half-closed (`0.5`) for contentment,
 *   wide (`1.3` and up) for alarm. Manga draws delight as "two thick
 *   half-circles" and excitement as *bigger* — not as a different colour.
 * * **The pair spreads.** `eyeOffset[0]` is multiplied by `side`, so it pushes
 *   the eyes *apart* or *together* symmetrically. Widening the spacing is how a
 *   stare reads as taken-aback; tightening it reads as concentration.
 * * **Vertical travel.** `y` lifts or drops both eyes together: up and open for
 *   attention, down and heavy for sad or sleepy. Four units is a third of an
 *   eye's height.
 * * **Asymmetry is one lid against the other**, through `left` and `right`.
 *   One crushed and one wide is confusion, one shut is a wink. This is the only
 *   channel that makes the two eyes differ, and it is the one to reach for when
 *   two faces would otherwise differ only by a few percent.
 * * **The effect is a manpu, not decoration.** Sparkle, hearts, a sweat drop, a
 *   popping vein, a question mark — the same shorthand the source material
 *   uses, so the face is readable before the eye shape has finished moving.
 *
 * ## `tilt` does nothing in `mask` mode, and this is worth knowing
 *
 * The engine writes each eye as `rotate(tilt, cx, cy)` followed by a scale about
 * the *same* point, where `(cx, cy)` is that eye's own bounding-box centre. A
 * circle rotated about its own centre is the same circle. So for every mask-mode
 * face — whose eyes are circular cutouts — `tilt` is a no-op, and the pose
 * values that carry it are documentation of an intent the artwork cannot show.
 * (This is why the first version of this table could describe a slant that was
 * never on screen: nothing was wrong with the values, the channel was empty.)
 *
 * It is not dead everywhere. In `arc` mode the eye is a crescent path, which is
 * *not* rotation-invariant, so `arc.rotate` genuinely tilts the ^^ — and the
 * pose's `tilt` rides along with it. Tilt stays in the table for those faces and
 * for the day a real brow shape exists; it just must not be counted on for the
 * circular ones, which is why every mask-mode face above is carried by height,
 * spread and per-eye asymmetry instead.
 *
 * `arc` is the closed cream crescent, the "^^" that means a smile reaching the
 * eyes, and it is now the default for every soft-positive face rather than for
 * four of them.
 */
const neutralPose: ExpressionPose = { maskScale: [1.02, 0.94], eyeOffset: [0, 0.5], tilt: 0, y: 0 };

const spec = (pose: ExpressionPose, eyeMode: ExpressiveEyeMode = "mask", effect: ExpressionEffect = "none"): ExpressionSpec => ({ ...pose, eyeMode, effect });

export const EXPRESSION_SPECS: Record<LumineEmotion, ExpressionSpec> = {
  // Resting. Not a blank: a half-degree of softness in the lid is what a face
  // looks like when it is at ease, and identity is what it looked like before.
  neutral: spec(neutralPose), idle: spec(neutralPose),
  happy: spec({ maskScale: [1.12, 1.1], eyeOffset: [0, 0], tilt: 0, y: -1.5 }, "arc", "sparkle"),
  loving: spec({ maskScale: [1.14, 1.08], eyeOffset: [0, 0.5], tilt: 1.5, y: -1 }, "arc", "love"),
  delighted: spec({ maskScale: [1.16, 1.14], eyeOffset: [0, -0.5], tilt: -2.5, y: -2.5 }, "arc", "sparkle"),
  amused: spec({ maskScale: [1.1, 1.06], eyeOffset: [0.5, 0.5], tilt: -3.5, y: -1 }, "arc", "blush"),
  excited: spec({ maskScale: [1.12, 1.34], eyeOffset: [0, -2], tilt: -1, y: -2.5, left: { y: -1 }, right: { y: -1 } }, "mask", "double-exclamation"),
  // One eye wide and one eye squinting is the "up to something" face; the
  // slant does most of it, so the two lids differ by half their height.
  playful: spec({ maskScale: [1.06, 1.14], eyeOffset: [1.5, -0.5], tilt: -4.5, y: -1.5, left: { maskScale: [0.9, 0.48], eyeOffset: [1.5, 1.5], tilt: -11 }, right: { maskScale: [1.12, 1.24], eyeOffset: [0, -1.5], tilt: 4 } }, "mask", "sparkle"),
  mischievous: spec({ maskScale: [1.02, 1.04], eyeOffset: [3, -0.5], tilt: -6.5, y: -0.5, left: { tilt: -4 }, right: { tilt: 3 } }, "arc", "sparkle"),
  // Sulking: the head turns away, the lids drop, and the shadow lines are the
  // manpu for a mood being carried rather than spoken.
  jealous: spec({ maskScale: [1.0, 0.68], eyeOffset: [3.5, 1], tilt: -7.5, y: 1.5, left: { tilt: -5 }, right: { tilt: -13, maskScale: [0.9, 0.6] } }, "mask", "shadow-lines"),
  wink: spec({ maskScale: [1.1, 0.92], eyeOffset: [1.5, 0.5], tilt: -2.5, y: -0.5, left: { maskScale: [0.9, 0.14], tilt: -9 }, right: { maskScale: [1.12, 1.02], tilt: 2.5 } }, "mask", "sparkle"),
  sleepy: spec({ maskScale: [1.14, 0.2], eyeOffset: [0, 4], tilt: 2.5, y: 4.5, right: { maskScale: [1.06, 0.14], y: 1.5 } }, "mask", "none"),
  sad: spec({ maskScale: [1.12, 0.58], eyeOffset: [0, 3], tilt: -2.5, y: 4.5, left: { tilt: 6, y: 1.5 }, right: { tilt: -3.5 } }, "mask", "shadow-lines"),
  surprised: spec({ maskScale: [1.16, 1.46], eyeOffset: [0, -3], tilt: 0, y: -3 }, "mask", "surprise"),
  // `> <` by way of two heavy lids and a hard slant, plus the rosy cheek.
  embarrassed: spec({ maskScale: [1.04, 0.5], eyeOffset: [2.5, 2.5], tilt: 5, y: 3, left: { tilt: 9, gazeX: 3, gazeY: 3 }, right: { tilt: -2, gazeX: -4, gazeY: 1.5 } }, "mask", "blush"),
  shy: spec({ maskScale: [1.08, 1.02], eyeOffset: [2, 2], tilt: 6, y: 2, left: { tilt: 7, gazeX: 3, gazeY: 3 }, right: { tilt: 2.5, gazeX: 1.5, gazeY: 2 } }, "arc", "blush"),
  // Mismatched by design: one lid crushed, one lid wide, both looking away.
  confused: spec({ maskScale: [1.02, 0.88], eyeOffset: [0, 1], tilt: -3.5, y: 0.5, left: { maskScale: [0.94, 0.56], tilt: -10, gazeX: -4 }, right: { maskScale: [1.12, 1.3], tilt: 6, gazeX: 5, y: -3 } }, "mask", "interrobang"),
  // Thinking is *narrowed and looking away*, which is what a person does while
  // composing an answer. The lids close to 0.84 and the eyes travel together
  // up and to one side — and the travel is the point, because with no pupil
  // there is nothing else to direct a gaze, so a shift of the whole eye is the
  // only way "looking up" can be drawn at all. The thought cloud finishes the
  // sentence for the half of the read that shape cannot carry.
  thinking: spec({ maskScale: [1.0, 1.06], eyeOffset: [0.5, -3.5], tilt: 0, y: -1.5, left: { gazeX: 10, gazeY: -8 }, right: { gazeX: 10, gazeY: -8 } }, "mask", "thoughts"),
  angry: spec({ maskScale: [1.06, 0.42], eyeOffset: [0.9, 3], tilt: 0, y: 1.5, left: { y: 1.5 }, right: { y: 1.5 } }, "mask", "vein-pop"),
  // The classic head-tilt-and-stare: the whole pair leans, and one eye grows
  // half again while the other shrinks.
  curious: spec({ maskScale: [0.96, 1.08], eyeOffset: [3.5, -1.5], tilt: -8, y: 0, left: { maskScale: [0.88, 0.8], y: 1.5 }, right: { maskScale: [1.14, 1.26], y: -3.5 } }, "mask", "question"),
  // `focused` and `determined` were both "narrow lids" and nothing else, which
  // left two names for one face. They now differ on spread, which is the other
  // axis that reads: focus *converges* — the lids close and the pair draws
  // together, the attention narrowing onto one thing — while resolve *widens*,
  // the stare set and held apart. Same lid, opposite geometry.
  focused: spec({ maskScale: [0.92, 0.66], eyeOffset: [-0.4, 0.5], tilt: 0, y: 0.5 }, "mask", "action-burst"),
  determined: spec({ maskScale: [1.14, 0.74], eyeOffset: [0.9, 0.5], tilt: 0, y: 0.5 }, "mask", "action-burst"),
  proud: spec({ maskScale: [1.1, 1.04], eyeOffset: [0, -0.5], tilt: -2.5, y: -1.5, left: { tilt: -5 }, right: { tilt: 5 } }, "arc", "sparkle"),
  worried: spec({ maskScale: [1.02, 0.54], eyeOffset: [-2, 1.5], tilt: 3, y: 2.5, left: { tilt: -10, y: 1.5 }, right: { tilt: 4, y: -1.5, maskScale: [1.1, 0.76] } }, "mask", "sweat"),
  relieved: spec({ maskScale: [1.1, 1.04], eyeOffset: [0, 0], tilt: -2.5, y: -1.5 }, "arc", "firework"),
  calm: spec({ maskScale: [1.05, 0.94], eyeOffset: [0, 0.5], tilt: 0, y: 0.5 }, "mask", "none"),
  // Wide and lifted: the attentive face, and the one `listening` spends a whole
  // session wearing, so it has to be unmistakable rather than merely correct.
  //
  // It reads through eye *shape*, because shape is the axis that bites. The
  // lids go to 1.42 tall against an unchanged width, which turns two circles
  // into two tall ovals — the "wide awake" read — and the pair lifts as one.
  // The first attempt was `[1.12, 1.26]`, a 12% widening and a 26% lift, and
  // against a neutral face that is already two circles it measured as a face
  // that looked *slightly* bigger rather than as one paying attention. The gap
  // is what carries it: neutral is 0.94 tall, this is 1.42, and half again is
  // a different shape rather than a variation of the same one.
  //
  // Its `!` is left to a *reaction* — see `Presence`'s state artifact table.
  alert: spec({ maskScale: [1.0, 1.42], eyeOffset: [0.5, -2], tilt: 0, y: -2.5 }, "mask", "single-exclamation"),
  concerned: spec({ maskScale: [1.04, 0.62], eyeOffset: [0, 1.5], tilt: 1.5, y: 3, left: { tilt: -8 }, right: { tilt: 4 } }, "mask", "shadow-lines"),
};

export const EXPRESSIONS: Record<LumineEmotion, ExpressionPose> = EXPRESSION_SPECS;

export function expressionSpec(emotion: LumineEmotion): ExpressionSpec {
  return EXPRESSION_SPECS[emotion] ?? EXPRESSION_SPECS.neutral;
}
