/**
 * What Lumine is doing, right now, as one small model the stage can draw.
 *
 * ## Why this is a store and not a hook's state
 *
 * Three producers feed one surface. `Home.tsx` wires `onToolActivity` (every
 * raw `tool_status` record, so the ledger can show *progress*) and
 * `onToolResult` (the finished call's payload, so the row can say what came
 * back), and both land here rather than in a component — the ledger lives on
 * the home stage, which is rendered whether or not a session is running, and a
 * context would have to be threaded through `MainSpace` for a value only one
 * leaf reads. `activityFeed.ts` is the precedent: a module-level feed with a
 * subscribe/snapshot pair, rendered through `useSyncExternalStore`.
 *
 * ## What is a task block and what is a tool row
 *
 * Everything arrives on `tool_status`, and the routing is two fields:
 *
 * - `taskId` present → a step of a `run_task` plan. It belongs to the task
 *   block, keyed by that id.
 * - `name === TASK_TOOL` and no `taskId` → the `run_task` call itself, or one
 *   of its internal narration steps (`run_search` publishes `tool="run_task"`
 *   with actions like `find`/`type`/`wait` for browser sub-steps *inside* a
 *   plan). A bracket (no `action`) is the block's header, status and duration;
 *   a step updates the block's line and is never a row of its own — it is
 *   narration for work the plan already lists.
 * - anything else → a tool call. A bracket (no `action`) makes or settles a
 *   row; a step updates the *running* row of the same name with what it is
 *   doing now.
 *
 * Tool rows join to their steps by `name` and never by id: a step's wire id is
 * `tool.action` and a call's is a call id, so no shared key exists — and a
 * step with no running row of its name is correctly ignored, because the
 * internal steps of a task have no bracket of their own.
 *
 * ## Why nothing here is permanent
 *
 * The ledger is a live surface, not a log — the Activity page is the log, and
 * it is the one that persists. Rows expire on their own (see the three
 * constants below), so a screen nobody clears cannot fill with rows from
 * yesterday, and the component renders null when the model is empty: the stage
 * is the avatar and the greeting the rest of the time.
 */

import { readableToolName, summarizeToolResult, type ToolOutcome } from "../toast/toolToast";
import type { VoiceToolEvent } from "../voice/voice-manager";

/**
 * The tool id `run_task` publishes under, mirroring `agent/tools/task_runner.py`.
 *
 * A task block is told apart from a tool row by this name, so the two sides
 * naming it differently would silently split a plan's steps into loose rows.
 */
export const TASK_TOOL = "run_task";

/**
 * A running entry lives this long after its last activity.
 *
 * Ninety seconds because it is the worker's own task budget
 * (`TASK_BUDGET_SECONDS`) — a step that has been silent longer than the engine
 * allows is not still running, and a spinner that outlives the work it
 * describes is the exact lie this surface exists to avoid. Refreshed by every
 * record, so a live task never expires under itself.
 */
const LIVE_MS = 90_000;

/** A finished tool row, long enough to read and short enough to be gone after. */
const TOOL_DONE_MS = 12_000;

/** A finished plan, longer because it carries several rows a person may still be reading. */
const TASK_DONE_MS = 20_000;

/**
 * How many loose tool rows the stage holds at once.
 *
 * Four, newest first. A turn can make a dozen calls, and a column taller than
 * the avatar it sits beside stops being a ledger and starts covering the
 * stage. The Activity page has every one of them.
 */
const MAX_TOOLS = 4;

export type StageStepStatus = "pending" | "running" | "done" | "failed" | "skipped";

export type StageStep = {
  /** The wire id (`run_task.step3`), or a synthesised key for a pending row. */
  id: string;
  /** 1-based, as the worker reports it. */
  index?: number;
  /** What the step is, from the worker's own narration. */
  label?: string;
  status: StageStepStatus;
  endedAt?: number;
};

export type StageTaskStatus = "running" | "done" | "failed";

export type StageTask = {
  status: StageTaskStatus;
  startedAt: number;
  endedAt?: number;
  /** From the bracket when the worker measured it, else `endedAt - startedAt`. */
  durationMs?: number;
  /** The live narration line — what the plan is doing *now*. */
  line?: string;
  /** Every declared step, pending ones included. */
  steps: StageStep[];
};

export type StageToolStatus = "running" | "done" | "failed";

export type StageTool = {
  /** The call's wire id. */
  id: string;
  /** A name a person can read — `readableToolName`, never the tool id. */
  name: string;
  /** One line of what came back, or what it is doing right now. */
  summary: string;
  status: StageToolStatus;
  startedAt: number;
  endedAt?: number;
  durationMs?: number;
};

export type StageActivity = {
  task: StageTask | null;
  tools: StageTool[];
};

export type StageActivityStore = {
  /** Feed one raw `tool_status` record in. The ledger's whole input. */
  ingest: (event: VoiceToolEvent) => void;
  /** A finished call's payload, joined to its row by name. */
  result: (outcome: ToolOutcome) => void;
  /** Drop everything still running — the worker is gone and can never finish it. */
  drain: () => void;
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => StageActivity;
};

/** `started`/`done`/`failed` are the step statuses; brackets use `completed`. */
function stepStatus(status: string): StageStepStatus {
  if (status === "done") return "done";
  if (status === "failed") return "failed";
  return "running";
}

function isSettled(status: StageTaskStatus | StageToolStatus): boolean {
  return status !== "running";
}

function createStageActivityStore(): StageActivityStore {
  let task: StageTask | null = null;
  let tools: StageTool[] = [];
  /** When the task last heard anything, so a live plan is never swept. */
  let taskTouchedAt = 0;

  const listeners = new Set<() => void>();
  let snapshot: StageActivity = { task: null, tools: [] };
  let timer: ReturnType<typeof setTimeout> | null = null;

  const publish = () => {
    snapshot = { task, tools: [...tools] };
    for (const listener of listeners) listener();
    schedule();
  };

  /** The earliest moment something on screen stops being true, or null. */
  const nextDeadline = (): number | null => {
    let earliest: number | null = null;
    const consider = (at: number) => {
      if (earliest === null || at < earliest) earliest = at;
    };
    if (task) {
      const deadline = isSettled(task.status)
        ? (task.endedAt ?? taskTouchedAt) + TASK_DONE_MS
        : taskTouchedAt + LIVE_MS;
      consider(deadline);
    }
    for (const tool of tools) {
      const deadline = isSettled(tool.status)
        ? (tool.endedAt ?? tool.startedAt) + TOOL_DONE_MS
        : tool.startedAt + LIVE_MS;
      consider(deadline);
    }
    return earliest;
  };

  const schedule = () => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    const deadline = nextDeadline();
    if (deadline === null) return;
    // Clamped, because a sweep that fires every frame is a busy loop and one
    // that waits a minute leaves a finished row up for fifty seconds longer
    // than it was due. The window is short enough to be prompt and long
    // enough to not be a poll.
    const delay = Math.min(5_000, Math.max(250, deadline - Date.now()));
    timer = setTimeout(() => {
      timer = null;
      const now = Date.now();
      let changed = false;
      if (task && isSettled(task.status) && task.endedAt !== undefined && task.endedAt + TASK_DONE_MS <= now) {
        task = null;
        changed = true;
      }
      const kept = tools.filter(
        (tool) =>
          !isSettled(tool.status) || tool.endedAt === undefined || tool.endedAt + TOOL_DONE_MS > now,
      );
      if (kept.length !== tools.length) {
        tools = kept;
        changed = true;
      }
      if (changed) publish();
    }, delay);
  };

  /** Keep at most `total` steps, filling the not-yet-reached ones as pending. */
  const fillPending = (current: StageStep[], total: number): StageStep[] => {
    const next = [...current];
    while (next.length < total) {
      const index = next.length + 1;
      next.push({ id: `${TASK_TOOL}.step${index}`, index, status: "pending" });
    }
    return next;
  };

  const ingest = (event: VoiceToolEvent) => {
    const now = Date.now();

    // A step of a plan. `taskId` is what makes it one, and it is the only
    // thing that can: a `run_task` step *without* one is an internal narration
    // step (below), and a tool step of another name is a tool row's update.
    if (event.taskId !== undefined) {
      const total = event.total;
      let steps = task ? task.steps : [];
      if (total !== undefined) steps = fillPending(steps, total);
      const position = event.index !== undefined ? event.index - 1 : -1;
      if (position >= 0 && position < steps.length) {
        const existing = steps[position];
        steps[position] = {
          ...existing,
          id: event.id,
          index: event.index,
          label: event.message || existing.label,
          status: stepStatus(event.status),
          ...(event.status !== "started" ? { endedAt: now } : {}),
        };
      }
      // A step can arrive without a bracket having made the block (a worker
      // that restarted mid-plan), so the block is built here when it is
      // missing rather than the step being dropped.
      const previous = task;
      task = {
        status: previous?.status ?? "running",
        startedAt: previous?.startedAt ?? now,
        line: event.message || previous?.line,
        steps,
        ...(previous?.endedAt !== undefined ? { endedAt: previous.endedAt } : {}),
        ...(previous?.durationMs !== undefined ? { durationMs: previous.durationMs } : {}),
      };
      taskTouchedAt = now;
      publish();
      return;
    }

    if (event.name === TASK_TOOL) {
      // A bracket is the block's header, and it arrives before any step does.
      if (event.action === undefined) {
        if (event.status === "started") {
          task = {
            status: "running",
            startedAt: now,
            steps: task?.steps ?? [],
          };
        } else if (task) {
          task = {
            ...task,
            status: event.status === "failed" ? "failed" : "done",
            endedAt: now,
            durationMs: event.durationMs ?? now - task.startedAt,
          };
        }
        taskTouchedAt = now;
        publish();
        return;
      }
      // A narration step for work the plan already lists — `run_search`
      // publishing `find`/`type`/`wait` inside a plan. It is the plan's line,
      // never a row: making it one would show two entries for one action.
      if (task) {
        task = { ...task, line: event.message || task.line };
        taskTouchedAt = now;
        publish();
      }
      return;
    }

    // A step inside a tool call: the same name as the row it belongs to, and
    // no shared id — so the running row of that name is the join. With no row
    // to update the step is dropped, which is right: the internal steps of a
    // task have no bracket of their own and must not invent a row.
    if (event.action !== undefined) {
      const index = tools.findIndex((tool) => tool.name === readableToolName(event.name) && tool.status === "running");
      if (index >= 0 && event.message) {
        const tool = tools[index];
        tools = [...tools.slice(0, index), { ...tool, summary: event.message }, ...tools.slice(index + 1)];
        publish();
      }
      return;
    }

    // A whole call. `started` makes a row; anything else settles one.
    if (event.status === "started") {
      const row: StageTool = {
        id: event.id,
        name: readableToolName(event.name),
        summary: event.message,
        status: "running",
        startedAt: now,
      };
      tools = [row, ...tools].slice(0, MAX_TOOLS);
      publish();
      return;
    }

    const index = tools.findIndex((tool) => tool.id === event.id);
    if (index < 0) return;
    const existing = tools[index];
    const settled: StageTool = {
      ...existing,
      status: event.status === "failed" ? "failed" : "done",
      endedAt: now,
      durationMs: event.durationMs ?? now - existing.startedAt,
      summary: event.message || existing.summary,
    };
    tools = [...tools.slice(0, index), settled, ...tools.slice(index + 1)];
    publish();
  };

  const result = (outcome: ToolOutcome) => {
    // A task carries its own line, and the block already shows the plan's
    // narration — a second summary of the same call would be the doubled
    // notification this surface replaced.
    if (outcome.name === TASK_TOOL) return;
    const name = readableToolName(outcome.name);
    const index = tools.findIndex((tool) => tool.name === name);
    if (index < 0) return;
    const existing = tools[index];
    const line = summarizeToolResult(outcome);
    // The bracket already measured it when it settled; only borrow the
    // caller's number if the row is still running, and never invent one.
    const durationMs =
      existing.durationMs ?? (outcome.durationMs !== undefined ? outcome.durationMs : undefined);
    const updated: StageTool = {
      ...existing,
      status: outcome.status === "completed" ? "done" : "failed",
      endedAt: existing.endedAt ?? Date.now(),
      ...(durationMs !== undefined ? { durationMs } : {}),
      // A success with nothing to say keeps whatever the last step told us —
      // "Notepad" on its own is not a sentence, but the step before it was.
      summary: line.detail ?? existing.summary,
    };
    tools = [...tools.slice(0, index), updated, ...tools.slice(index + 1)];
    publish();
  };

  const drain = () => {
    const keptTools = tools.filter((tool) => tool.status === "running");
    const keptTask = task && task.status === "running" ? null : task;
    if (keptTools.length === tools.length && keptTask === task) return;
    tools = keptTools;
    task = keptTask;
    publish();
  };

  function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  return { ingest, result, drain, subscribe, getSnapshot: () => snapshot };
}

/**
 * The one store, module-level.
 *
 * One, because there is one stage and one session driving it; a second call
 * would be a second ledger on the same avatar. HMR-safe by construction: the
 * object holds no timers that outlive a reload, and a stale module's rows are
 * expired by the same sweep as everything else.
 */
export const stageActivity: StageActivityStore = createStageActivityStore();
