"""The structured task Lumine executes, and the state machine that moves it.

A *task* is the user's objective plus an ordered set of *steps*, each one a tool
call with its dependencies, its retry policy and its completion criterion. The
model is deliberately a plain dataclass with no I/O, because the interesting
failures here are ordering failures and those are only testable if the structure
can be built and inspected without a browser, a window, or a network.

The lifecycle is the point:

    pending -> running -> succeeded | failed | cancelled | awaiting_approval

A step becomes ``running`` only when every dependency it names is ``succeeded``.
A step that fails does not fail its dependents -- they become ``skipped``, which
is a different statement ("we never tried") from ``failed`` ("we tried and it did
not work"). Blurring those two is how an orchestrator ends up opening a browser
window for a search that already failed.

Four rules the engine keeps, each of which exists because its absence produced a
real failure:

1. **Dependencies gate, they do not order.** Steps are declared with what they
   need, not with a position. A plan that says "open Chrome" then "navigate"
   has expressed the truth about *why* the second cannot run first, and the
   engine can then answer "what is runnable now" for a question the plan never
   had to anticipate.
2. **A cycle is refused at construction.** Not detected at run time, and not
   tolerated: a cyclic plan is a plan whose first step can never run, and
   accepting it means the task hangs rather than fails.
3. **Retry is a property of the step and defaults to off.** A read that failed
   once is worth one more attempt; a launch that may have produced a window is
   not, because the second attempt is how the user gets two.
4. **Cancellation is sticky.** A cancelled task refuses to start new work even if
   something still calls :meth:`Task.cancellable_steps` on it, so a stale handle
   from a previous turn cannot resurrect work the user stopped.
"""

from __future__ import annotations

import itertools
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from . import verification

# -- statuses -----------------------------------------------------------------

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
AWAITING_APPROVAL = "awaiting_approval"
SKIPPED = "skipped"

STEP_STATUSES: tuple[str, ...] = (
    PENDING,
    RUNNING,
    SUCCEEDED,
    FAILED,
    CANCELLED,
    AWAITING_APPROVAL,
    SKIPPED,
)

#: Task-level statuses. A task is ``completed`` only when every step succeeded
#: and the completion criterion was checked; ``partial`` is for "some steps
#: worked", which is the honest description of most real outcomes.
TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_COMPLETED = "completed"
TASK_PARTIAL = "partial"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_WAITING = "awaiting_approval"

TERMINAL_STEP_STATUSES: frozenset[str] = frozenset(
    {SUCCEEDED, FAILED, CANCELLED, SKIPPED}
)


# -- retry policy -------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """How many extra attempts a step gets, and whether that is safe at all.

    ``max_attempts`` counts the first try, so ``1`` means "no retry".
    ``retry_safe`` is the separate, more important flag: a step that can leave an
    externally visible effect is not retryable even with attempts left, because
    the retry is what creates the duplicate.
    """

    max_attempts: int = 1
    retry_safe: bool = False

    @property
    def attempts_allowed(self) -> int:
        return max(1, min(int(self.max_attempts), 5))

    def allows(self, attempt: int, outcome: verification.ActionOutcome | None) -> bool:
        """Whether attempt number ``attempt`` (1-based) may be followed by another."""
        if attempt >= self.attempts_allowed:
            return False
        if outcome is not None and not self.retry_safe:
            return False
        # An outcome we cannot interpret is never grounds for repeating the act.
        if outcome is not None and verification.may_have_happened(outcome.status):
            return False
        return True


NO_RETRY = RetryPolicy()
READ_RETRY = RetryPolicy(max_attempts=2, retry_safe=True)


# -- steps --------------------------------------------------------------------


@dataclass
class Step:
    """One operation, with everything the engine needs to run and judge it."""

    id: str
    description: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    retry: RetryPolicy = NO_RETRY
    #: How the step's effect is confirmed afterwards. ``None`` means the engine
    #: accepts the tool's own outcome, which is only safe for reads.
    verify: Callable[[verification.ActionOutcome], bool] | None = None
    #: The action class, used by the engine to decide whether this step may run
    #: concurrently with another. Defaults to ``act`` rather than ``read``
    #: because the failure direction matters: an unlabelled step that is assumed
    #: to be a read gets run alongside a launch, and a read that turns out to act
    #: is two side effects in an order nobody chose.
    #:
    #: This is *not* the gate's input. The permission decision comes from the
    #: step implementation's own `require_approval` call, which declares its class
    #: at the call site in code the model cannot reach. This field only decides
    #: scheduling.
    action_class: str = "act"
    timeout_seconds: float = 30.0
    status: str = PENDING
    attempts: int = 0
    outcome: verification.ActionOutcome | None = None
    error: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_STEP_STATUSES

    @property
    def duration_ms(self) -> float:
        if not self.started_at or not self.ended_at:
            return 0.0
        return round((self.ended_at - self.started_at) * 1000, 1)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "description": self.description,
            "tool": self.tool,
            "status": self.status,
            "attempts": self.attempts,
        }
        if self.depends_on:
            out["dependsOn"] = list(self.depends_on)
        if self.error:
            out["error"] = self.error
        if self.outcome is not None:
            out["outcome"] = self.outcome.to_dict()
        return out


# -- tasks --------------------------------------------------------------------


@dataclass
class Task:
    """The user's objective as an executable, inspectable sequence."""

    request: str
    environment: str = ""
    steps: list[Step] = field(default_factory=list)
    id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:10]}")
    #: What "done" means, as a function of the task's own state. Checked once at
    #: the end; a task that ran every step and did not satisfy this is ``partial``.
    complete_when: Callable[["Task"], bool] | None = None
    created_at: float = field(default_factory=time.time)
    status: str = TASK_PENDING
    cancelled: bool = False
    cancel_reason: str = ""
    narration: list[str] = field(default_factory=list)

    # -- structure ----------------------------------------------------------

    def step(self, step_id: str) -> Step | None:
        for item in self.steps:
            if item.id == step_id:
                return item
        return None

    def ready_steps(self) -> list[Step]:
        """Steps whose dependencies have all succeeded and that have not run.

        An empty list with pending steps remaining is a *deadlock* -- a cycle
        that construction refused to allow, or a dependency on a step that failed
        and was therefore skipped. The engine reports it rather than looping.
        """
        return [
            item
            for item in self.steps
            if item.status == PENDING and self._dependencies_met(item)
        ]

    def _dependencies_met(self, step: Step) -> bool:
        for name in step.depends_on:
            dependency = self.step(name)
            if dependency is None:
                return False
            if dependency.status != SUCCEEDED:
                return False
        return True

    def blocked_steps(self) -> list[Step]:
        """Pending steps that can never run because a dependency did not succeed."""
        return [
            item
            for item in self.steps
            if item.status == PENDING and not self._dependencies_met(item)
        ]

    def cancellable_steps(self) -> list[Step]:
        """Steps a cancellation still has to stop.

        "Still has to" is the useful part: a step that has already reached a
        terminal state is not something a cancellation can un-do, and listing it
        would suggest it could. This is what a caller asks *before* cancelling to
        find out whether anything is at stake.
        """
        return [item for item in self.steps if item.status not in TERMINAL_STEP_STATUSES]

    def outstanding_steps(self) -> list[Step]:
        """Steps not yet finished, terminal or not. Progress reporting."""
        return [item for item in self.steps if not item.done]

    # -- transitions --------------------------------------------------------

    def start(self) -> None:
        if self.cancelled:
            return
        self.status = TASK_RUNNING

    def cancel(self, reason: str = "cancelled by the user") -> None:
        """Stop the task. Sticky: nothing restarts it.

        Every pending step is moved to ``cancelled`` immediately rather than at
        the moment the engine reaches it, because "the user stopped it" has to
        be true of the whole task the instant they said so -- including the parts
        that had not started yet.
        """
        self.cancelled = True
        self.cancel_reason = reason
        self.status = TASK_CANCELLED
        for item in self.steps:
            if item.status in (PENDING, AWAITING_APPROVAL):
                item.status = CANCELLED
                item.error = reason

    def finish(self) -> str:
        """Settle the task's own status and return it."""
        if self.cancelled:
            self.status = TASK_CANCELLED
            return self.status
        if any(item.status == AWAITING_APPROVAL for item in self.steps):
            self.status = TASK_WAITING
            return self.status
        succeeded = [item for item in self.steps if item.status == SUCCEEDED]
        failed = [item for item in self.steps if item.status in (FAILED, SKIPPED)]
        if not failed and succeeded:
            if self.complete_when is not None and not self.complete_when(self):
                self.status = TASK_PARTIAL
                return self.status
            self.status = TASK_COMPLETED
        elif succeeded:
            self.status = TASK_PARTIAL
        else:
            self.status = TASK_FAILED
        return self.status

    # -- reporting ----------------------------------------------------------

    def summary(self) -> str:
        """One sentence a person can check. Never claims more than ran."""
        counts: dict[str, int] = {}
        for item in self.steps:
            counts[item.status] = counts.get(item.status, 0) + 1
        parts = [f"{count} {name}" for name, count in sorted(counts.items())]
        return ", ".join(parts) or "nothing to do"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "request": self.request,
            "status": self.status,
            "steps": [item.to_dict() for item in self.steps],
        }
        if self.environment:
            out["environment"] = self.environment
        if self.cancelled:
            out["cancelled"] = True
            out["cancelReason"] = self.cancel_reason
        return out


# -- construction -------------------------------------------------------------


class PlanError(ValueError):
    """The plan cannot be executed as written."""


_counter = itertools.count(1)


def step_id(prefix: str = "s") -> str:
    return f"{prefix}{next(_counter)}"


def build_step(
    description: str,
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    depends_on: Sequence[str] = (),
    retry: RetryPolicy = NO_RETRY,
    verify: Callable[[verification.ActionOutcome], bool] | None = None,
    action_class: str = "act",
    ident: str = "",
    timeout_seconds: float = 30.0,
) -> Step:
    """Build one step. Exists so the arguments are named, not positional.

    ``depends_on`` is normalised here rather than at execution time: a caller
    that passes ``["s1", ""]`` gets a plan that reads honestly in a log instead
    of a dependency on a step named by nothing.
    """
    name = str(tool or "").strip()
    if not name:
        raise PlanError("a step needs a tool")
    return Step(
        id=ident or step_id(),
        description=str(description or ""),
        tool=name,
        args=dict(args or {}),
        depends_on=tuple(str(item) for item in depends_on if str(item).strip()),
        retry=retry,
        verify=verify,
        action_class=str(action_class or "read"),
        timeout_seconds=float(timeout_seconds),
    )


def validate_plan(steps: Sequence[Step]) -> None:
    """Refuse a plan the engine could never finish. Raises :class:`PlanError`.

    Four checks, in this order, because each is cheaper than the next and each
    produces a more useful message:

    1. identifiers are unique -- a duplicate id makes dependency resolution
       ambiguous, and "which one did you mean" is not a question a plan should
       force at run time;
    2. every dependency names a step that exists;
    3. no step depends on itself;
    4. no cycle.
    """
    seen: dict[str, Step] = {}
    for item in steps:
        if item.id in seen:
            raise PlanError(f"two steps share the id {item.id!r}")
        seen[item.id] = item
    for item in steps:
        for name in item.depends_on:
            if name not in seen:
                raise PlanError(
                    f"step {item.id!r} depends on {name!r}, which is not a step in this plan"
                )
            if name == item.id:
                raise PlanError(f"step {item.id!r} depends on itself")
    _reject_cycle(seen)


def _reject_cycle(seen: dict[str, Step]) -> None:
    """Iterative DFS with an explicit stack; recursion would be a new failure mode."""
    UNVISITED, ACTIVE, DONE = 0, 1, 2
    state: dict[str, int] = {ident: UNVISITED for ident in seen}
    for root in seen:
        if state[root] != UNVISITED:
            continue
        stack: list[tuple[str, Iterable[str]]] = [(root, iter(seen[root].depends_on))]
        state[root] = ACTIVE
        path = [root]
        while stack:
            _node, children = stack[-1]
            advanced = False
            for child in children:
                if state.get(child, DONE) == ACTIVE:
                    cycle = " -> ".join(path[path.index(child):] + [child])
                    raise PlanError(f"the plan has a dependency cycle: {cycle}")
                if state.get(child, DONE) == UNVISITED:
                    state[child] = ACTIVE
                    path.append(child)
                    stack.append((child, iter(seen[child].depends_on)))
                    advanced = True
                    break
            if not advanced:
                state[stack[-1][0]] = DONE
                path.pop()
                stack.pop()


def make_task(
    request: str,
    steps: Sequence[Step],
    *,
    environment: str = "",
    complete_when: Callable[[Task], bool] | None = None,
) -> Task:
    """A validated task. Validation happens *here*, so an invalid plan never
    reaches the engine and cannot half-run."""
    validate_plan(steps)
    return Task(
        request=str(request or ""),
        environment=str(environment or ""),
        steps=list(steps),
        complete_when=complete_when,
    )