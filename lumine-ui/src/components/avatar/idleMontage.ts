import type { LumineState } from "../../pages/home/types";
import type { LumineAnimation } from "./avatarTypes";

/**
 * Which behaviours each state is allowed to reach for.
 *
 * ## Why this is a table of *intentions* and not a weighted bag
 *
 * It used to be one weighted list of fourteen interchangeable micro-tweens,
 * sampled by weight and filtered by whether the state allowed it. The weightings
 * were carefully chosen — a blink outnumbers a `wiggle` eight to one — and the
 * effect was a face that fidgets uniformly through its whole vocabulary in every
 * state, because every state drew from nearly the same pool.
 *
 * The problem is that the *pool* encoded nothing. `thinking` drawing a `wiggle`
 * was prevented by removing `wiggle` from `thinking`'s group, and then the group
 * had six entries left, all of them variations on a sway. A face that sways while
 * it thinks is not thinking; it is a screensaver.
 *
 * So each state now names what she would plausibly *do* there. `thinking` reaches
 * for `wonder` and `lookLeft` and `pauseDrift` — things that suggest attention
 * directed somewhere. `online` reaches for `settle` and `saysYes`, which are the
 * two gestures that mean "I'm here". The breath underneath is not in this table
 * at all: it is continuous and belongs to the engine, so listing `inhale` here
 * would mean a body that breathes *between* breaths.
 *
 * ## Ordering is not meaningful; the weights are
 */
const BEHAVIOURS: Record<LumineState, ReadonlyArray<readonly [LumineAnimation, number]>> = {
  // Resting. Long, small, and mostly blinks. She is not performing, and the
  // absence of motion is most of what "resting" means.
  idle: [
    ["blink", 30], ["slowBlink", 10], ["softBreeze", 14], ["microTilt", 10],
    ["tinySway", 12], ["lookLeft", 8], ["lookRight", 8], ["exhale", 6],
  ],

  // Connecting. Predominantly `reaching`, because searching for a room that has
  // not answered is the whole of what this state is.
  connecting: [["reaching", 46], ["wonder", 14], ["lookLeft", 10], ["lookRight", 10], ["peek", 8], ["inhale", 6]],

  // Joined, present, not yet addressed. `settle` is the arrival and `saysYes` is
  // acknowledgement — the two things a face does when someone arrives.
  online: [["settle", 26], ["saysYes", 18], ["blink", 20], ["wonder", 12], ["microTilt", 10], ["lookLeft", 8]],

  // Attentive. Small and frequent, and leaning *in* is the characteristic one:
  // `listensDeeply` is the gesture that says this state and no other.
  listening: [["listensDeeply", 22], ["blink", 22], ["microTilt", 14], ["saysYes", 12], ["softBreeze", 10], ["lookLeft", 8], ["lookRight", 8]],

  // Attention directed somewhere. Deliberately contains no body-movement filler
  // beyond a drift: the pause *is* the signal, so everything here either holds a
  // gaze or holds a pose.
  thinking: [["wonder", 24], ["lookLeft", 20], ["lookRight", 20], ["pauseDrift", 16], ["slowBlink", 10], ["peek", 10]],

  // Speaking. Body-led, because the audio envelope is already driving the rest
  // and a face that also blinks hard on every syllable fights it.
  speaking: [["saysYes", 20], ["softBounce", 18], ["tinySway", 16], ["blink", 18], ["curiousTilt", 12], ["pauseDrift", 10]],

  // A failed call. Downcast, and the stillness is the message: nothing here is
  // quick, nothing here fidgets, and `exhale` reads as deflation rather than as
  // breathing because it is the only fast-toward-zero gesture in the group.
  error: [["slowBlink", 26], ["exhale", 20], ["lookLeft", 16], ["lookRight", 16], ["pauseDrift", 12], ["peek", 10]],
};

/**
 * How long to sit still between gestures, in ms, per state.
 *
 * A two-part range rather than a fixed interval, because a fixed interval is what
 * makes a montage look metronomic. The driver is already a queue, so the gap
 * *after* a gesture is the only thing left to vary.
 */
const GAP: Record<LumineState, [number, number]> = {
  // Idle is a long wait. A face that fidgets is a face you notice, and the point
  // of this screen is that it is there without being asked for anything.
  idle: [3200, 7200],
  // Connecting moves more often than idle: something is supposed to be happening
  // and a frozen face during setup reads as a hang.
  connecting: [900, 1900],
  // Online is brief and busy — the arrival and the acknowledgement both want to
  // land before she has been spoken to.
  online: [1400, 3000],
  // Listening reads as attentive, so it moves more often but less far.
  listening: [1600, 3200],
  // Thinking is the one state where a long still would be a bug: the pause is the
  // signal, and a long one is indistinguishable from a dropped connection.
  thinking: [1800, 3400],
  // Speaking is busy already; the envelope carries it.
  speaking: [2600, 5200],
  // A failure should not look busy. Long gaps, and the first one is a beat of
  // nothing at all so the face can be seen to have registered what happened.
  error: [3600, 7800],
};

export function gapFor(kind: LumineState): number {
  const [low, high] = GAP[kind];
  return low + Math.random() * (high - low);
}

/**
 * Draw the next behaviour for a state, weighted, and never twice in a row.
 *
 * The no-repeat rule is what stops an avatar looking automated, and it is
 * implemented by re-rolling rather than by tracking a history: with these pool
 * sizes a single remembered value is enough, and a full history would make the
 * scheduler a second thing to reason about for no visible gain.
 *
 * `last` is passed in rather than held here because the driver owns the schedule.
 * When the pool has only ever contained one entry, the re-roll would loop
 * forever, so a single-member pool is returned as-is — a face with one gesture is
 * still better than a face that throws.
 */
export function pickMontageAnimation(kind: LumineState, last: LumineAnimation | null): LumineAnimation {
  const table = BEHAVIOURS[kind];
  if (table.length === 0) return "blink";
  if (table.length === 1) return table[0][0];

  const total = table.reduce((sum, [, weight]) => sum + weight, 0);
  for (let attempt = 0; attempt < 6; attempt += 1) {
    let cursor = Math.random() * total;
    for (const [animation, weight] of table) {
      cursor -= weight;
      if (cursor <= 0) {
        if (animation !== last) return animation;
        break;
      }
    }
  }
  // Six draws all landed on the repeat. Fall back to the highest-weighted entry
  // that is not the repeat, which is a real animation rather than a re-roll.
  const alternative = [...table].filter(([animation]) => animation !== last).sort((a, b) => b[1] - a[1])[0];
  return alternative?.[0] ?? table[0][0];
}

/** Every behaviour a state can play. Exported for the Avatar Lab's readout. */
export function montageBehaviours(kind: LumineState): LumineAnimation[] {
  return BEHAVIOURS[kind].map(([animation]) => animation);
}

/**
 * Every gesture the engine can play, for the Avatar Lab's "Surprise me".
 *
 * Held as a `Record` rather than an array specifically so it cannot fall behind
 * the union: `Record<LumineAnimation, true>` fails to compile the moment a
 * gesture is added to `LumineAnimation` and not here, which is the only reason
 * this list is trustworthy at all. An array of the same names would silently
 * become a subset, and "Surprise me" would quietly stop being able to reach half
 * the engine.
 */
const ANIMATION_CATALOGUE: Record<LumineAnimation, true> = {
  blink: true,
  doubleBlink: true,
  slowBlink: true,
  lookLeft: true,
  lookRight: true,
  curiousTilt: true,
  wiggle: true,
  bounce: true,
  sleepy: true,
  surprise: true,
  happyBlink: true,
  surprisedBlink: true,
  sleepyBlink: true,
  peek: true,
  softBreeze: true,
  microTilt: true,
  tinySway: true,
  softBounce: true,
  pauseDrift: true,
  reaching: true,
  settle: true,
  inhale: true,
  exhale: true,
  wonder: true,
  saysYes: true,
  listensDeeply: true,
};

/**
 * A gesture drawn from the whole catalog, not from a state's pool.
 *
 * Uniform, and deliberately *not* weighted: the montage's weights encode what a
 * state would plausibly do, which is meaningless here. This is a lab control for
 * looking at a gesture, and looking at one is worth as much as looking at any
 * other.
 */
export function surpriseAnimation(): LumineAnimation {
  const names = Object.keys(ANIMATION_CATALOGUE) as LumineAnimation[];
  return names[Math.floor(Math.random() * names.length)];
}
