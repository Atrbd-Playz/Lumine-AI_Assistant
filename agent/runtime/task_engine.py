"""The deterministic engine that advances a :mod:`.task_model` task.

## Why the progression is not the model's job

A model asked to "keep calling tools until this is done" re-plans after every
call, and every re-plan is an opportunity to change its mind mid-sequence. The
symptom is familiar: the browser opens twice, the app launches twice, the search
runs before the app is ready. None of those are tool bugs. They are the LLM
being asked to hold a plan in prose while the plan is being executed.

So the progression lives here instead. The model may *propose* a plan, and it may
propose a revision, but a plan is validated before it runs and then executed by
this engine, which will not reconsider it because step two returned something
surprising. When a step fails, the engine decides between three fixed responses
-- retry, skip dependents, stop -- from the step's own retry policy, not from a
fresh guess.

## What "checkpoint" means

A checkpoint is three things, taken after every step: the outcome is classified
(:mod:`.verification`), the task's state is updated, and the *next* step's
preconditions are re-checked. That last part is why a plan can survive a page that
navigated on its own: if the next step's dependency is no longer in the state it
expected, the engine reports that rather than acting on a stale premise.

## Cancellation

Checked before every step and again immediately before the call, because
cancellation arrives as an event and the window between "decided to run" and
"ran" is where a cancelled task would otherwise do its one remaining side
effect. A running step is not killed -- the tools do not all have a cancellation
handle -- but the engine stops scheduling, and a cancelled task cannot be
resumed by a later call.

## Concurrency

Read-only steps with no unmet dependencies run together, because a weather
lookup and a news lookup genuinely are independent and the user's latency is the
sum of the two otherwise. Anything that acts, anything that asks, and anything
whose dependencies overlap runs sequentially: ordering between two side effects is
exactly what this module exists to preserve.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Iterable

from . import verification
from .task_model import (
    CANCELLED,
    FAILED,
    PENDING,
    RUNNING,
    SKIPPED,
    SUCCEEDED,
    Step,
    Task,
    TASK_PARTIAL,
)

logger = logging.getLogger("lumine")

#: One step's implementation: ``(args) -> ActionOutcome``.
StepRunner = Callable[[Step], Awaitable[verification.ActionOutcome]]

#: Optional hook, called with ``(event, task, step)``. The runtime wires this to
#: latency records and runtime events; tests record into a list.
Observer = Callable[[str, Task, Step | None], None]

#: How many times the engine re-reads the task state before calling a deadlock a
#: deadlock. One pass is enough for a validated plan; the bound exists so a bug in
#: a step's status assignment cannot spin the loop forever.
MAX_PASSES = 256


async def _noop(_event: str, _task: Task, _step: Step | None) -> None:
    return None


def dependencies_met(task: Task, step: Step) -> bool:
    """Whether every dependency of ``step`` has succeeded.

    Exposed as a function because the engine needs it in two places -- selection
    and immediately before acting -- and reaching into the task's private method
    from a sibling module is how a refactor later breaks one of them silently.
    """
    return task._dependencies_met(step)  # noqa: SLF001 - the one private helper


class TaskEngine:
    """Runs one task to a terminal state.

    Constructed per task rather than reused, so a cancelled task cannot inherit
    another task's runnable set. ``runner`` is injected: the engine knows about
    ordering, retries, verification and cancellation, and knows nothing about
    Playwright, UI Automation or ``subprocess`` -- which is what makes every
    branch here testable without any of them.
    """

    def __init__(
        self,
        runner: StepRunner,
        *,
        observer: Observer | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._observe = observer or _noop
        self._clock = clock

    # -- the run ------------------------------------------------------------

    async def run(self, task: Task) -> Task:
        """Advance ``task`` until nothing can advance it, then settle it."""
        if task.cancelled:
            task.finish()
            return task
        task.start()
        await self._notify("task_started", task, None)

        passes = 0
        while passes < MAX_PASSES:
            passes += 1
            if task.cancelled:
                break
            ready = task.ready_steps()
            if not ready:
                break
            reads = self._concurrent_reads(ready)
            if len(reads) > 1:
                await asyncio.gather(*(self._run_step(task, item) for item in reads))
                continue
            # One step at a time from here. Either there was nothing parallel to
            # do, or what there was has just run and the next pass re-reads the
            # task -- which is the checkpoint: the next decision is made against
            # the state the previous step actually produced.
            await self._run_step(task, reads[0] if reads else ready[0])

        if not task.cancelled:
            self._settle_blocked(task)
        task.finish()
        await self._notify("task_finished", task, None)
        return task

    @staticmethod
    def _concurrent_reads(ready: list[Step]) -> list[Step]:
        """The read-only steps that may run simultaneously.

        Every member must be a read *and* must not depend on another member.
        Anything that acts runs alone, always: ordering between two side effects is
        exactly what this module exists to preserve, so the only thing concurrency
        ever trades is latency between things that cannot affect each other.

        Reads are gathered regardless of declaration order, which is why "look
        something up" does not wait behind "open the app" when neither needs the
        other.
        """
        reads = [step for step in ready if step.action_class == "read"]
        batch: list[Step] = []
        for candidate in reads:
            if set(candidate.depends_on) & {item.id for item in batch}:
                continue
            batch.append(candidate)
        return batch

    # -- one step -----------------------------------------------------------

    async def _run_step(self, task: Task, step: Step) -> None:
        if task.cancelled or step.status != PENDING:
            return
        # Re-checked here, not only in the selection loop: `ready_steps` ran
        # before a sibling in the same batch may have changed things.
        if not dependencies_met(task, step):
            return
        if task.cancelled:
            step.status = CANCELLED
            step.error = task.cancel_reason
            await self._notify("step_cancelled", task, step)
            return

        step.status = RUNNING
        step.started_at = time.time()
        await self._notify("step_started", task, step)

        while True:
            step.attempts += 1
            if task.cancelled:
                step.status = CANCELLED
                step.error = task.cancel_reason
                step.ended_at = time.time()
                await self._notify("step_cancelled", task, step)
                return
            try:
                outcome = await self._runner(step)
            except asyncio.CancelledError:
                step.status = CANCELLED
                step.error = "cancelled"
                step.ended_at = time.time()
                await self._notify("step_cancelled", task, step)
                raise
            except Exception as exc:  # noqa: BLE001 - a step's failure is data
                logger.info("[Task] step %s raised: %s", step.id, exc, exc_info=True)
                outcome = verification.failed(step.tool, f"{exc}"[:200])
            step.outcome = outcome
            if step.verify is not None:
                accepted = bool(step.verify(outcome))
                if not accepted and outcome.status == verification.OBSERVED:
                    # The step claimed it worked and its own criterion says it did
                    # not. That is not a failure to argue with: the claim is
                    # downgraded, so nothing downstream treats it as done.
                    outcome = verification.uncertain(
                        step.tool,
                        outcome.detail or "the check after this step did not confirm it",
                    )
                    step.outcome = outcome
            # Retry *before* settling, and only for an attempt that actually
            # failed. Settling first would mark the step terminal and there would
            # be nothing left to re-run -- which is the bug this ordering exists
            # to prevent, and why `allows()` is consulted with the outcome rather
            # than with the status.
            if not verification.succeeded(outcome) and step.retry.allows(step.attempts, outcome):
                await self._notify("step_retry", task, step)
                continue
            self._settle_step(step, outcome)
            break

        step.ended_at = time.time()
        await self._notify(f"step_{step.status}", task, step)

    @staticmethod
    def _settle_step(step: Step, outcome: verification.ActionOutcome) -> None:
        """Move the step to its terminal state.

        ``awaiting_approval`` is *not* handled here, and that is deliberate. The
        approval question is raised inside the step implementation by the
        permission gate, which blocks and then either returns or raises; by the
        time an outcome exists the decision has been made. A step that was parked
        waiting for a decision is re-entered as the same step object, so an
        approved action resumes rather than restarting the task.

        ``accepted`` and ``dispatched`` count as done for a step with no check of
        its own, because a read has nothing to observe and the distinction is
        still recorded on the outcome. A step that *does* carry a ``verify`` has
        already had its chance to object, above.
        """
        status = verification.normalize(outcome.status)
        if status in (verification.OBSERVED, verification.ACCEPTED, verification.DISPATCHED):
            step.status = SUCCEEDED
            step.error = ""
            return
        step.status = FAILED
        step.error = outcome.detail or status

    # -- blocked steps ------------------------------------------------------

    def _settle_blocked(self, task: Task) -> None:
        """A step whose dependency failed is *skipped*, never failed.

        The difference is the whole point: "we never tried because the thing it
        needed did not happen" is a different fact from "we tried and it did not
        work", and a summary that merges them tells the user a search failed when
        the search never started.
        """
        for step in task.blocked_steps():
            step.status = SKIPPED
            step.error = _why_blocked(task, step)

    # -- reporting ----------------------------------------------------------

    async def _notify(self, event: str, task: Task, step: Step | None) -> None:
        try:
            result = self._observe(event, task, step)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - telemetry must never stop a task
            logger.debug("[Task] observer failed for %s", event, exc_info=True)


def _why_blocked(task: Task, step: Step) -> str:
    names = [name for name in step.depends_on if task.step(name) is not None]
    if not names:
        return "it depends on something that is not in this task"
    return "waiting on " + ", ".join(names)


#: The public outcome vocabulary, re-exported so a caller of this module has one
#: import for "did it work and how sure are we".
__all__ = [
    "Observer",
    "StepRunner",
    "TaskEngine",
    "SUCCEEDED",
    "FAILED",
    "SKIPPED",
    "CANCELLED",
    "TASK_PARTIAL",
    "verification",
]


def statuses(task: Task) -> dict[str, int]:
    """Counts by step status, for a compact report."""
    counts: dict[str, int] = {}
    for step in task.steps:
        counts[step.status] = counts.get(step.status, 0) + 1
    return counts


def iter_report(task: Task) -> Iterable[str]:
    """One line per step, for a log or a test assertion."""
    for step in task.steps:
        suffix = f" -- {step.error}" if step.error else ""
        yield f"{step.id} {step.tool} {step.status}{suffix}"