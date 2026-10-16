import { useCallback, useState } from "react";
import { useAgentRuntime, type AgentRuntimeEvent } from "./agentRuntime";

/**
 * Whether Lumine is actually receiving a picture, as the worker reports it.
 *
 * ## Two halves, and neither can see the other's
 *
 * The app owns the *capture*: it knows when a permission was refused, when a
 * track was published, and when the user pressed the browser's own "Stop
 * sharing" bar. The worker owns *delivery*: it is the only process that knows
 * whether a frame arrived, whether it arrived late, and whether the model it is
 * talking to can read an image at all.
 *
 * Each half is blind to the other across a process boundary, so both are
 * reported, and they are not interchangeable:
 *
 * - `isPublished` (from `Home.tsx`) is a fact about the **track**. It is the
 *   floor for every claim this module makes — see `visionShareText`.
 * - `VisualSnapshot` is a fact about the **stream**. It is the only thing that
 *   can say "frames have stopped", which no amount of local state can infer: a
 *   share that is live and a share that has quietly frozen look identical from
 *   this side of the pipe.
 *
 * The worker's own `visual_state.phase()` is the authority, and it is the same
 * value it pushes into the model's instructions, so the badge and what Lumine
 * believes cannot disagree — one field, two readers.
 *
 * ## What is deliberately absent: every number
 *
 * No frame counter, no age, no timestamp. An age in React state is true for one
 * frame and wrong for the next, and keeping it honest would mean a timer
 * re-rendering the tree to redraw a number nobody asked to watch. `phase`
 * already answers the question the badge is asked ("is she seeing this?"),
 * *without* being true for only an instant.
 *
 * It also has a performance consequence worth stating: the worker publishes a
 * heartbeat record every few seconds while sharing, and because those records
 * carry no differing field here, they collapse into the object already in state.
 * A share costs zero re-renders per heartbeat.
 *
 * ## It is not reset when a session ends
 *
 * Deliberately. Nothing in this module is *shown* unless a capture is live
 * (`SelfView` and the share strip are both rendered from the active capture),
 * and a capture cannot outlive its call — `useLocalMedia` releases the tracks
 * when the session goes away. So a stale phase has no surface to appear on, and
 * the worker writes a fresh baseline the moment the next session starts.
 */

/** Mirrors `agent/runtime/visual_state.py::PHASES`, rung for rung. */
export type VisionPhase =
  | "error"
  | "unavailable"
  | "denied"
  | "requesting_permission"
  | "not_requested"
  | "enabled"
  | "stopped"
  | "capturing"
  | "frame_available"
  | "vision_ready"
  | "vision_degraded";

/** One `visual` record, reduced to the fields a surface can render. */
export type VisualSnapshot = {
  phase: VisionPhase;
  /** Whether a track is feeding frames right now. */
  sharing: boolean;
  source: "camera" | "screen" | null;
  /** A current picture is arriving — the strongest claim on the ladder. */
  visionReady: boolean;
  /** A frame exists in this session's context, live or not. */
  frameAvailable: boolean;
  /** Whether the selected model declares image input, or `null` before it says. */
  canSee: boolean | null;
  /** What the app reported the operating system answered. */
  permission: string;
  /** The app's own sentence for a failed capture, if there was one. */
  error: string | null;
};

const PHASES: readonly string[] = [
  "error",
  "unavailable",
  "denied",
  "requesting_permission",
  "not_requested",
  "enabled",
  "stopped",
  "capturing",
  "frame_available",
  "vision_ready",
  "vision_degraded",
];

function text(event: AgentRuntimeEvent, key: string): string | null {
  const value = event[key];
  return typeof value === "string" ? value : null;
}

function flag(event: AgentRuntimeEvent, key: string): boolean {
  return event[key] === true;
}

function tri(event: AgentRuntimeEvent, key: string): boolean | null {
  const value = event[key];
  return typeof value === "boolean" ? value : null;
}

/**
 * One `visual` record into a snapshot, or `null` if it is not one worth holding.
 *
 * Exported and pure for the same reason `classifyActivity` is: the interesting
 * rules — which records are ignored, and which are refused — are decisions, and
 * a decision you cannot test is one that quietly stops holding.
 *
 * A record whose `phase` is not on the ladder is refused rather than stored.
 * The worker has the same rule about the words it will *say*; this is the
 * matching rule about the words this app will *render*, because a phase nobody
 * has a sentence for would draw a badge nobody can read.
 */
export function readVisual(event: AgentRuntimeEvent): VisualSnapshot | null {
  if (event.type !== "visual") return null;

  const phase = text(event, "phase");
  if (!phase || !PHASES.includes(phase)) return null;

  const source = text(event, "source");
  const reported = text(event, "permission");

  return {
    phase: phase as VisionPhase,
    sharing: flag(event, "sharing"),
    source: source === "camera" || source === "screen" ? source : null,
    visionReady: flag(event, "vision_ready"),
    frameAvailable: flag(event, "frame_available"),
    canSee: tri(event, "can_see"),
    permission: reported ?? "not_requested",
    error: text(event, "error"),
  };
}

function same(a: VisualSnapshot, b: VisualSnapshot): boolean {
  return (
    a.phase === b.phase &&
    a.sharing === b.sharing &&
    a.source === b.source &&
    a.visionReady === b.visionReady &&
    a.frameAvailable === b.frameAvailable &&
    a.canSee === b.canSee &&
    a.permission === b.permission &&
    a.error === b.error
  );
}

/**
 * The latest `visual` record from the worker, or `null` before one arrives.
 *
 * `null` is the honest starting answer: it means "the worker has not said",
 * which is a different statement from "nothing is being shared". Treating the
 * second as the default is how a badge ends up claiming a share nobody made.
 */
export function useVisualState(): VisualSnapshot | null {
  const [state, setState] = useState<VisualSnapshot | null>(null);

  // Stable by identity so the subscription is opened once. Returning the same
  // object when nothing moved is what makes the heartbeat records free.
  const ingest = useCallback((event: AgentRuntimeEvent) => {
    const next = readVisual(event);
    if (!next) return;
    setState((current) => (current && same(current, next) ? current : next));
  }, []);

  useAgentRuntime({ onRuntime: ingest });
  return state;
}

/**
 * The one line a share shows about whether Lumine is *actually* receiving.
 *
 * Returns `null` whenever the caller's own wording is the true one, so the
 * existing strings — "Only you can see this", "Shared with Lumine",
 * "Lumine can see this screen" — are the fallback rather than being rewritten
 * here. Only three phases say something `isPublished` cannot, and they are the
 * whole reason this exists:
 *
 * - `vision_ready` upgrades "the track is in the room" to "a picture arrived",
 *   which is a stronger and more useful claim than publication alone;
 * - `vision_degraded` reports the failure the user cannot see for themselves —
 *   a share that is live and one that has frozen look identical from here;
 * - `unavailable` names a model that cannot read frames at all, which no local
 *   state can know because it is a property of the provider catalog.
 *
 * `isPublished` is checked first and never overridden. A phase claiming more
 * than the track is doing would be the exact failure this badge was written to
 * prevent: a share that is not one. The worker can also lag by a beat after an
 * unpublish, so this direction of disagreement is resolved in favour of what
 * this process knows for certain.
 */
export function visionShareText(
  phase: VisionPhase | null | undefined,
  isPublished: boolean,
  source: "camera" | "screen",
): string | null {
  if (!isPublished) return null;
  if (phase === "vision_ready") return source === "camera" ? "Lumine can see you" : "Lumine can see this screen";
  if (phase === "vision_degraded") return "Frames have stopped arriving";
  if (phase === "unavailable") return "This model cannot read frames";
  return null;
}
