"""The task model and the engine that runs it. No tools, no network, no browser.

Everything here is a fixture. That is the point: the failures this project started
with were *coordination* failures -- two windows, wrong order, a half-finished
task reported as done -- and coordination is only testable when the steps are
values rather than side effects.

Three properties carry the weight:

* **Dependencies gate.** A step runs only after every dependency has *succeeded*.
* **A failed step skips its dependents, and says so differently.** ``skipped`` is
  not ``failed``: "we never tried" and "we tried and it did not work" are
  different facts, and a report that merges them tells the user a search failed
  when the search never started.
* **Cancellation is sticky.** Nothing runs afterwards, and a cancelled task cannot
  be resumed by a later handle.
"""

from __future__ import annotations

import asyncio
import unittest

from agent.runtime import task_engine, task_model, verification
from agent.runtime.task_model import (
    CANCELLED,
    FAILED,
    PENDING,
    READ_RETRY,
    SKIPPED,
    SUCCEEDED,
    PlanError,
    build_step,
    make_task,
)


def observed(detail: str = "done") -> verification.ActionOutcome:
    return verification.observed("step", detail, evidence="read back afterwards")


def refused(detail: str = "no") -> verification.ActionOutcome:
    return verification.failed("step", detail)


class RecordingRunner:
    """A step runner that answers from a script and records what it was asked."""

    def __init__(self, script: dict[str, list[verification.ActionOutcome]] | None = None):
        self.script = script or {}
        self.calls: list[str] = []
        self.concurrent_max = 0
        self._active = 0

    async def __call__(self, step: task_model.Step) -> verification.ActionOutcome:
        self.calls.append(step.id)
        self._active += 1
        self.concurrent_max = max(self.concurrent_max, self._active)
        try:
            # A real step awaits something; without it, two "concurrent" reads
            # would never actually interleave and the concurrency tests would
            # pass for the wrong reason.
            await asyncio.sleep(0)
            queue = self.script.get(step.id)
            if not queue:
                return observed(f"{step.id} ran")
            return queue.pop(0) if len(queue) > 1 else queue[0]
        finally:
            self._active -= 1


def build(*specs: tuple[str, tuple[str, ...]]) -> list[task_model.Step]:
    """Steps from ``(id, depends_on)`` pairs, all the same tool.

    The tool name is a placeholder on purpose: the engine dispatches on the step's
    identity and its status, never on what the tool is, so a test that used real
    tool names would be testing the tools by accident.
    """
    return [
        build_step(f"step {ident}", "fixture", depends_on=deps, ident=ident)
        for ident, deps in specs
    ]


class SequentialExecutionTests(unittest.TestCase):
    def _run(self, task: task_model.Task, runner) -> task_model.Task:
        return asyncio.run(task_engine.TaskEngine(runner).run(task))

    def test_a_dependent_step_waits_for_its_dependency(self):
        task = make_task("t", build(("a", ()), ("b", ("a",))))
        self._run(task, RecordingRunner())
        self.assertEqual(task.status, task_model.TASK_COMPLETED)
        self.assertEqual([step.id for step in task.steps], ["a", "b"])

    def test_an_independent_read_does_not_wait_behind_a_slow_step(self):
        # Declared order is *not* execution order. A read that needs nothing runs
        # first even when it is declared second, so the user's latency is not the
        # sum of everything.
        steps = [
            build_step("launch", "open_app", ident="launch"),
            build_step("lookup", "search_web", action_class="read", ident="lookup"),
        ]
        runner = RecordingRunner()
        task = make_task("t", steps)
        self._run(task, runner)
        self.assertEqual(runner.calls[0], "lookup")
        self.assertEqual(task.status, task_model.TASK_COMPLETED)

    def test_a_failed_step_is_never_run_after_its_dependency_failed(self):
        steps = [
            build_step("launch", "open_app", ident="a"),
            build_step("use", "open_page", depends_on=("a",), ident="b"),
        ]
        runner = RecordingRunner({"a": [refused("could not start")]})
        task = make_task("t", steps)
        self._run(task, runner)
        self.assertEqual(runner.calls, ["a"])
        self.assertEqual(task.step("a").status, FAILED)
        self.assertEqual(task.step("b").status, SKIPPED)
        self.assertEqual(task.status, task_model.TASK_FAILED)


class SkipVersusFailureTests(unittest.TestCase):
    def test_a_skipped_step_says_it_never_ran(self):
        steps = [
            build_step("a", "open_app", ident="a"),
            build_step("b", "open_page", depends_on=("a",), ident="b"),
        ]
        runner = RecordingRunner({"a": [refused("nope")]})
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        skipped = task.step("b")
        self.assertEqual(skipped.attempts, 0, "a skipped step must not have been attempted")
        self.assertIn("waiting on", skipped.error)

    def test_some_work_succeeding_is_partial_not_complete(self):
        steps = [
            build_step("ok", "search_web", action_class="read", ident="ok"),
            build_step("bad", "open_app", ident="bad"),
        ]
        runner = RecordingRunner({"bad": [refused("no")]})
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertEqual(task.status, task_model.TASK_PARTIAL)

    def test_a_completion_criterion_can_demote_a_finished_task(self):
        # Every step succeeded and the task is still not done. The honest status
        # is "partial", and that is the only thing this is for.
        steps = [build_step("a", "search_web", action_class="read", ident="a")]
        task = make_task("t", steps, complete_when=lambda _t: False)
        asyncio.run(task_engine.TaskEngine(RecordingRunner()).run(task))
        self.assertEqual(task.status, task_model.TASK_PARTIAL)


class RetryTests(unittest.TestCase):
    def test_a_read_may_be_retried_within_its_policy(self):
        steps = [build_step("a", "search_web", retry=READ_RETRY, ident="a", action_class="read")]
        runner = RecordingRunner(
            {"a": [refused("timeout"), observed("second time")]}
        )
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertEqual(runner.calls, ["a", "a"])
        self.assertEqual(task.step("a").status, SUCCEEDED)
        self.assertEqual(task.step("a").attempts, 2)

    def test_a_step_without_retry_policy_runs_once(self):
        steps = [build_step("a", "open_app", ident="a")]
        runner = RecordingRunner({"a": [refused("x")]})
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertEqual(runner.calls, ["a"])

    def test_an_uncertain_outcome_is_never_retried(self):
        # The rule that protects against a duplicate message or a second window:
        # even with attempts left, an outcome we cannot interpret is not grounds
        # for repeating the act.
        steps = [build_step("a", "send", retry=READ_RETRY, ident="a")]
        runner = RecordingRunner({"a": [verification.uncertain("send", "unknown")]})
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertEqual(runner.calls, ["a"])
        self.assertEqual(task.step("a").status, FAILED)

    def test_a_retry_is_bounded(self):
        policy = task_model.RetryPolicy(max_attempts=3, retry_safe=True)
        steps = [build_step("a", "search_web", retry=policy, ident="a", action_class="read")]
        runner = RecordingRunner({"a": [refused("x")]})
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertEqual(task.step("a").attempts, 3)

    def test_the_attempt_count_is_clamped(self):
        self.assertEqual(task_model.RetryPolicy(max_attempts=99).attempts_allowed, 5)
        self.assertEqual(task_model.RetryPolicy(max_attempts=0).attempts_allowed, 1)


class VerificationTests(unittest.TestCase):
    def test_a_step_whose_own_check_disagrees_is_not_succeeded(self):
        # The step claims success; its own criterion says otherwise. The claim is
        # downgraded rather than argued with, so nothing downstream treats it as
        # done.
        steps = [
            build_step("a", "click", ident="a", verify=lambda outcome: outcome.status != "observed")
        ]
        runner = RecordingRunner({"a": [observed("clicked")]})
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertEqual(task.step("a").status, FAILED)
        self.assertEqual(task.step("a").outcome.status, verification.UNCERTAIN)

    def test_a_raising_step_becomes_a_failure_rather_than_a_crash(self):
        async def explode(_step):
            raise RuntimeError("playwright fell over")

        steps = [build_step("a", "open_page", ident="a")]
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(explode).run(task))
        self.assertEqual(task.step("a").status, FAILED)
        self.assertIn("playwright fell over", task.step("a").error)

    def test_a_dispatched_but_unobserved_step_is_recorded_as_such(self):
        steps = [build_step("a", "open_page", ident="a")]
        runner = RecordingRunner({"a": [verification.accepted("open_page", "started")]})
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertEqual(task.step("a").status, SUCCEEDED)
        self.assertEqual(task.step("a").outcome.status, verification.ACCEPTED)


class CancellationTests(unittest.TestCase):
    def test_cancelling_stops_pending_steps_and_cannot_be_resumed(self):
        steps = [
            build_step("a", "open_app", ident="a"),
            build_step("b", "open_page", depends_on=("a",), ident="b"),
        ]
        runner = RecordingRunner()
        task = make_task("t", steps)
        task.cancel("user said stop")

        asyncio.run(task_engine.TaskEngine(runner).run(task))

        self.assertEqual(runner.calls, [], "nothing may run after a cancellation")
        self.assertEqual(task.status, task_model.TASK_CANCELLED)
        self.assertEqual(task.step("b").status, CANCELLED)

    def test_a_cancellation_mid_flight_stops_the_next_step(self):
        steps = [
            build_step("a", "open_app", ident="a"),
            build_step("b", "open_page", depends_on=("a",), ident="b"),
        ]
        task = make_task("t", steps)

        async def runner(step: task_model.Step):
            if step.id == "a":
                # The user says stop while the first step is still going.
                task.cancel("stop")
            return observed(step.id)

        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertEqual(task.step("b").status, CANCELLED)
        self.assertEqual(task.status, task_model.TASK_CANCELLED)

    def test_a_cancelled_task_reports_no_cancellable_work(self):
        steps = [build_step("a", "open_app", ident="a")]
        task = make_task("t", steps)
        self.assertEqual(len(task.cancellable_steps()), 1)
        task.cancel()
        self.assertEqual(task.cancellable_steps(), [])


class ConcurrencyTests(unittest.TestCase):
    def test_two_reads_run_together(self):
        steps = [
            build_step("weather", "search_web", action_class="read", ident="weather"),
            build_step("news", "search_web", action_class="read", ident="news"),
        ]
        runner = RecordingRunner()
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertEqual(runner.concurrent_max, 2, "independent reads should overlap")

    def test_a_read_and_an_act_never_overlap(self):
        # Ordering between two side effects is the property the engine exists to
        # keep, so a batch is only ever taken when every member is a read.
        steps = [
            build_step("read", "search_web", action_class="read", ident="read"),
            build_step("act", "open_app", action_class="act", ident="act"),
        ]
        runner = RecordingRunner()
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertEqual(runner.concurrent_max, 1)

    def test_a_step_depending_on_its_batch_mate_runs_after_it(self):
        steps = [
            build_step("a", "search_web", action_class="read", ident="a"),
            build_step("b", "search_web", action_class="read", ident="b"),
            build_step("c", "observe", depends_on=("b",), ident="c"),
        ]
        runner = RecordingRunner()
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertLess(runner.calls.index("b"), runner.calls.index("c"))


class ValidationTests(unittest.TestCase):
    def test_a_duplicate_id_is_refused(self):
        steps = [
            build_step("one", "a", ident="x"),
            build_step("two", "b", ident="x"),
        ]
        with self.assertRaisesRegex(PlanError, "share the id"):
            make_task("t", steps)

    def test_a_dependency_on_nothing_is_refused(self):
        steps = [build_step("one", "a", depends_on=("ghost",), ident="x")]
        with self.assertRaisesRegex(PlanError, "not a step in this plan"):
            make_task("t", steps)

    def test_self_dependency_is_refused(self):
        steps = [build_step("one", "a", depends_on=("x",), ident="x")]
        with self.assertRaisesRegex(PlanError, "depends on itself"):
            make_task("t", steps)

    def test_a_two_step_cycle_is_refused(self):
        steps = [
            build_step("one", "a", depends_on=("y",), ident="x"),
            build_step("two", "b", depends_on=("x",), ident="y"),
        ]
        with self.assertRaisesRegex(PlanError, "cycle"):
            make_task("t", steps)

    def test_a_long_cycle_is_refused_without_recursing(self):
        # Iterative on purpose: a recursive walk over a pathological plan is a new
        # failure mode in the thing that validates plans.
        steps = [
            build_step("s", "a", depends_on=("c",), ident="a"),
            build_step("s", "a", depends_on=("a",), ident="b"),
            build_step("s", "a", depends_on=("b",), ident="c"),
        ]
        with self.assertRaisesRegex(PlanError, "cycle"):
            make_task("t", steps)

    def test_a_diamond_is_allowed(self):
        # Two steps depending on one, and one depending on both: legal, and the
        # refusal above must not have been a blanket ban on sharing a dependency.
        steps = [
            build_step("s", "a", ident="root"),
            build_step("s", "a", depends_on=("root",), ident="left"),
            build_step("s", "a", depends_on=("root",), ident="right"),
            build_step("s", "a", depends_on=("left", "right"), ident="join"),
        ]
        task = make_task("t", steps)
        self.assertEqual(len(task.steps), 4)


class ReportingTests(unittest.TestCase):
    def test_the_dict_carries_what_a_ui_would_show(self):
        steps = [build_step("open krita", "open_app", ident="a")]
        runner = RecordingRunner()
        task = make_task("open Krita", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        payload = task.to_dict()
        self.assertEqual(payload["request"], "open Krita")
        self.assertEqual(payload["steps"][0]["status"], SUCCEEDED)
        self.assertEqual(payload["steps"][0]["outcome"]["status"], verification.OBSERVED)

    def test_the_summary_counts_statuses(self):
        steps = [build_step("a", "a", ident="a"), build_step("b", "a", ident="b")]
        runner = RecordingRunner({"b": [refused("no")]})
        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(runner).run(task))
        self.assertIn("1 succeeded", task.summary())
        self.assertIn("1 failed", task.summary())


class ObserverTests(unittest.TestCase):
    def test_progress_events_are_emitted_in_order(self):
        steps = [build_step("a", "open_app", ident="a"), build_step("b", "observe", depends_on=("a",), ident="b")]
        seen: list[str] = []
        task = make_task("t", steps)

        async def observe(event: str, _task, _step) -> None:
            seen.append(event)

        asyncio.run(task_engine.TaskEngine(RecordingRunner(), observer=observe).run(task))
        self.assertEqual(seen[0], "task_started")
        self.assertEqual(seen[-1], "task_finished")
        self.assertIn("step_succeeded", seen)

    def test_a_broken_observer_never_stops_the_task(self):
        steps = [build_step("a", "open_app", ident="a")]

        async def observe(_event, _task, _step) -> None:
            raise RuntimeError("telemetry is down")

        task = make_task("t", steps)
        asyncio.run(task_engine.TaskEngine(RecordingRunner(), observer=observe).run(task))
        self.assertEqual(task.step("a").status, SUCCEEDED)


if __name__ == "__main__":
    unittest.main()