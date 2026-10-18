import { useSyncExternalStore } from "react";

import {
  stageActivity,
  type StageActivity,
  type StageTask,
  type StageTool,
} from "../../../features/stage/stageActivity";
import { Icon } from "./Icon";

/**
 * The left of the avatar is where "Lumine is working" belongs to live.
 *
 * Tool activity used to arrive as toasts, in the same stack as errors and
 * permission warnings. That stack is bottom-left and *persistent*: a toast sits
 * until it is dismissed or its timeout runs, so a tool call that finished a
 * second ago sits beside "Check the Python interpreter" for thirty seconds,
 * and six calls in one turn bury the only message a person needed to read.
 * The design reference here is a *step ledger* rather than a notification
 * feed: rows that update in place, a running row that keeps its place, and a
 * quiet list that disappears when nothing is happening.
 *
 * One block per task, then the loose tool calls. Both come from the same
 * `tool_status` records on `lumine.tool`; the store decides which is which by
 * whether the record carries a task id.
 */

const STATUS_LABEL: Record<StageTask["status"], string> = {
  running: "Running",
  done: "Complete",
  failed: "Failed",
};

/** `840ms`, `1.2s`, `47s` — whichever the number actually reads as. */
function formatDuration(ms?: number): string | null {
  if (ms === undefined || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function toolDuration(tool: StageTool): string | null {
  if (tool.endedAt !== undefined && tool.startedAt !== undefined) {
    return formatDuration(tool.endedAt - tool.startedAt);
  }
  return formatDuration(tool.durationMs);
}

/**
 * A task row's marker, by state.
 *
 * Hollow ring = not reached yet, spinner = running, `check`/`close` = the two
 * ways it ended. The pending row is synthesised in the store from the run's
 * declared step count — the engine only reports the step it is on — and a row
 * with no status at all is pending by definition, so a future status the store
 * does not know renders as "not reached" rather than as "running".
 */
function StepMark({ status }: { status: StageTask["steps"][number]["status"] }) {
  if (status === "done") return <Icon name="check" size={11} weight="bold" />;
  if (status === "failed") return <Icon name="close" size={11} weight="bold" />;
  if (status === "running") return <i className="ledger-mark ledger-mark--spin" aria-hidden="true" />;
  if (status === "skipped") return <i className="ledger-mark ledger-mark--skip" aria-hidden="true" />;
  return <i className="ledger-mark" aria-hidden="true" />;
}

function TaskBlock({ task }: { task: StageTask }) {
  return (
    <section className="ledger-task" aria-label={`Plan: ${STATUS_LABEL[task.status]}`}>
      <header className="ledger-task-head">
        <span className="ledger-task-title">Plan</span>
        <span className={`ledger-chip ledger-chip--${task.status}`}>
          <StepMark status={task.status === "running" ? "running" : task.status} />
          {STATUS_LABEL[task.status]}
        </span>
        {(() => {
          const duration = formatDuration(
            task.endedAt !== undefined ? task.endedAt - task.startedAt : task.durationMs,
          );
          return duration ? <span className="ledger-dur">{duration}</span> : null;
        })()}
      </header>

      {task.line ? <p className="ledger-line">{task.line}</p> : null}

      <ol className="ledger-steps">
        {task.steps.map((step) => {
          const key = step.index ?? step.id;
          const label =
            step.label ??
            (step.index !== undefined ? `Step ${step.index}` : step.id);
          const status = step.status ?? "pending";
          return (
            <li key={key} className={`ledger-step ledger-step--${status}`} title={label}>
              <StepMark status={status} />
              <span className="ledger-step-label">{label}</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function ToolRow({ tool }: { tool: StageTool }) {
  const duration = toolDuration(tool);
  return (
    <li className={`ledger-tool ledger-tool--${tool.status}`} title={tool.summary}>
      <span className="ledger-tool-glyph" aria-hidden="true">
        {tool.status === "done" ? (
          <Icon name="check" size={11} weight="bold" />
        ) : tool.status === "failed" ? (
          <Icon name="close" size={11} weight="bold" />
        ) : (
          <i className="ledger-mark ledger-mark--spin" aria-hidden="true" />
        )}
      </span>
      <span className="ledger-tool-body">
        <span className="ledger-tool-name">{tool.name}</span>
        <span className="ledger-tool-summary">{tool.summary}</span>
      </span>
      {duration ? <span className="ledger-dur">{duration}</span> : null}
    </li>
  );
}

/** Renders nothing when there is nothing to show — the stage is the avatar the rest of the time. */
export function StageLedger() {
  const activity: StageActivity = useSyncExternalStore(
    stageActivity.subscribe,
    stageActivity.getSnapshot,
    stageActivity.getSnapshot,
  );

  if (!activity.task && activity.tools.length === 0) return null;

  return (
    <aside className="stage-ledger" aria-label="What Lumine is doing">
      {activity.task ? <TaskBlock task={activity.task} /> : null}
      {activity.tools.length > 0 ? (
        <ul className="ledger-tools">
          {activity.tools.map((tool) => (
            <ToolRow key={tool.id} tool={tool} />
          ))}
        </ul>
      ) : null}
    </aside>
  );
}
