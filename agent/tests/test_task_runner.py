"""The orchestrator end to end: routing, planning, execution, cancellation.

Where `test_task_orchestration.py` tests the engine with fixture steps, this file
tests the *steps themselves* -- the ones that resolve an app, focus a window, look
something up, open a page, find a control and act on it -- plus the tool surface
around them. Everything runs against fakes: no window is opened, no page loaded,
no request made.

The fake engine is the important fixture. It mimics the real one's contract (a
gate that stamps generations, an `act` that can refuse a stale generation) rather
than being a stub that always succeeds, because the refusal paths are exactly what
is worth testing.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.runtime import router, task_model, verification
from agent.tools import app_index, app_resolver, task_runner
from agent.tools.task_runner import STEP_IMPLEMENTATIONS, STEP_ACTION_CLASS, LiveTask, plan_for
from agent.tools.tools_compat import ToolError

CONTEXT = SimpleNamespace(session=None)


# -- fakes ----------------------------------------------------------------------


class FakeGate:
    def __init__(self):
        self.generation = 0
        self.current = None
        self.invalidated = 0

    def next_generation(self) -> int:
        self.generation += 1
        return self.generation

    def stamp(self, payload):
        from agent.tools.browser import state

        self.current = state.snapshot_from_script(self.next_generation(), payload)
        return self.current

    def check(self, generation: int, expected_url: str = "") -> str:
        from agent.tools.browser.engine import GenerationGate

        return GenerationGate.check(self, generation, expected_url)

    def invalidate(self) -> None:
        self.invalidated += 1
        self.current = None


def page(
    candidates: list[dict],
    *,
    url: str = "https://www.youtube.com/results",
    title: str = "YouTube",
    items: list[dict] | None = None,
) -> dict:
    return {
        "url": url,
        "title": title,
        "text": "page text",
        "ready": "complete",
        "candidates": candidates,
        "items": list(items or []),
        "tabs": [],
        "forms": [],
        "viewport": {},
    }


def control(index: int, name: str, role: str = "link", **detail) -> dict:
    return {
        "index": index,
        "role": role,
        "name": name,
        "tag": "a" if role == "link" else "input",
        "visible": True,
        "enabled": True,
        "checked": None,
        "detail": detail,
        "rowText": "",
    }


class FakeEngine:
    """The engine contract, with the refusals the real one makes."""

    def __init__(self, initial: dict | None = None) -> None:
        self.gate = FakeGate()
        self.pages: list[dict] = list(initial or [])
        self.acted: list = []
        self.after_url = "https://www.youtube.com/watch?v=abc"
        self.act_error: str = ""
        self.closed = False
        self.screenshot_bytes = b"\x89PNG fake"

    async def navigate(self, url: str) -> dict:
        self.pages.append(page([], url=url, title="Fixture"))
        self.gate.stamp(self.pages[-1])
        return {"url": url, "title": "Fixture"}

    async def observe(self) -> dict:
        if not self.pages:
            self.pages.append(page([]))
        return self.pages[-1]

    async def act(self, step):
        if self.act_error:
            from agent.tools.browser.engine import EngineError

            # The real engine raises `EngineError`; a bare string would surface as
            # a TypeError and hide the refusal this test is about.
            raise EngineError(self.act_error)
        self.acted.append(step)
        return {
            "ok": True,
            "before": {"url": self.pages[-1]["url"], "title": "YouTube"},
            "after": {"url": self.after_url, "title": "Baby Shark"},
            "element": {"name": step.expected.get("name", "")},
            "host": "youtube.com",
        }

    async def history(self, direction: str) -> dict:
        return {"moved": True, "url": "https://www.youtube.com/", "title": "YouTube"}

    async def reload(self) -> dict:
        return {"url": self.pages[-1]["url"], "title": "YouTube"}

    async def tabs(self) -> dict:
        return {"tabs": [{"name": "Results", "selected": True}], "url": self.pages[-1]["url"]}

    async def screenshot(self) -> bytes:
        return self.screenshot_bytes

    async def close(self) -> None:
        self.closed = True


async def approve(**kwargs) -> None:
    """A recording no-op approval, so the gate is not the subject here."""
    return None


# -- routing integration ----------------------------------------------------------


class RoutingIntegrationTests(unittest.TestCase):
    TOOLS = ["search_web", "open_app", "browse", "web_control", "send_whatsapp", "run_task"]

    def _route(self, request: str, **kwargs):
        return router.classify(request, router.MachineFacts.build(**kwargs), tools=self.TOOLS)

    def test_a_lookalike_program_does_not_win_over_a_site(self):
        # A machine with an app called "YouTube Downloader" must not turn "open
        # YouTube" into a desktop request.
        facts = router.MachineFacts.build(installed={"youtube downloader", "youtube"})
        route = router.classify("Open YouTube in your browser", facts, tools=self.TOOLS)
        self.assertEqual(route.environment, router.BROWSER)

    def test_a_title_word_that_sounds_like_a_window_command_stays_on_the_open_page(self):
        request = "Open the second one. The title is Breathe Lo-Fi Deep Focus Study."
        facts = router.MachineFacts.build(browser_open=True)
        initially = router.classify(request, facts, tools=self.TOOLS)
        self.assertEqual(initially.environment, router.DESKTOP)
        self.assertEqual(initially.reason, "asked to arrange or close a window")

        route = task_runner.prefer_open_browser_for_control(initially, facts, request)
        self.assertEqual(route.environment, router.BROWSER)
        self.assertEqual(route.tool, "browse")
        plan = plan_for(route, request, browser_open=facts.browser_open)
        self.assertEqual([step.tool for step in plan.steps], ["find_element", "browser_act"])
        self.assertEqual(
            plan.steps[0].args,
            {"strategy": "text", "name": "Breathe Lo-Fi Deep Focus Study"},
        )

    def test_an_explicit_desktop_request_is_not_moved_to_the_browser(self):
        request = "Open the second one on my desktop. The title is Breathe Lo-Fi Deep Focus Study."
        facts = router.MachineFacts.build(browser_open=True)
        initially = router.classify(request, facts, tools=self.TOOLS)
        route = task_runner.prefer_open_browser_for_control(initially, facts, request)
        self.assertEqual(route.environment, router.DESKTOP)


# -- planning ---------------------------------------------------------------------


class PlanningTests(unittest.TestCase):
    def test_every_planned_verb_has_an_implementation_and_a_class(self):
        # The registry is checked rather than the planner's own table: a verb the
        # planner can emit but the engine cannot run would fail at the third step
        # of a conversation instead of here.
        for verb, step_class in STEP_ACTION_CLASS.items():
            with self.subTest(verb=verb):
                self.assertIn(verb, STEP_IMPLEMENTATIONS)

    def test_every_implementation_has_a_class(self):
        for verb in STEP_IMPLEMENTATIONS:
            with self.subTest(verb=verb):
                self.assertIn(verb, STEP_ACTION_CLASS)

    def test_a_lookup_plans_one_search(self):
        plan = plan_for(router.classify("find the price of a drawing tablet"), "find the price of a drawing tablet")
        self.assertEqual([step.tool for step in plan.steps], ["search_web"])

    def test_a_named_program_plans_a_launch_then_a_focus(self):
        route = router.classify("open Krita", router.MachineFacts.build(installed={"krita"}))
        plan = plan_for(route, "open Krita")
        self.assertEqual([step.tool for step in plan.steps], ["open_app", "focus_window"])
        # The focus depends on the launch: a focus aimed at a window that does not
        # exist yet would fail on a cold machine.
        self.assertEqual(plan.steps[1].depends_on, (plan.steps[0].id,))

    def test_the_browser_plan_reaches_a_control_only_when_one_is_named(self):
        route = router.classify("open youtube in your browser", router.MachineFacts.build())
        plan = plan_for(route, "open youtube in your browser")
        self.assertEqual([step.tool for step in plan.steps], ["open_page"])

    def test_a_search_on_a_site_plans_reach_then_search(self):
        # One search step, not fill-then-submit. Splitting them is how a search
        # used to end with the text typed and nothing run -- the reported
        # "she cannot search on YouTube".
        route = router.classify(
            "open youtube in your browser and search for a song", router.MachineFacts.build()
        )
        plan = plan_for(route, "open youtube in your browser and search for a song")
        self.assertEqual([step.tool for step in plan.steps], ["open_page", "browser_search"])
        self.assertEqual(plan.steps[1].args["query"], "a song")
        self.assertEqual(plan.steps[1].depends_on, (plan.steps[0].id,))

    def test_a_search_that_names_the_site_does_not_search_for_the_site(self):
        route = router.classify("Search for a cafe on maps", router.MachineFacts.build())
        plan = plan_for(route, "Search for a cafe on maps")
        self.assertEqual(plan.steps[-1].args["query"], "a cafe")

    def test_a_reference_to_search_results_is_not_planned_as_a_new_search(self):
        request = "Open the search results, titled Breathe Lo-Fi Deep Focus Study"
        self.assertEqual(task_runner._search_query(request), "")

    def test_finding_an_existing_result_or_control_is_not_a_search(self):
        self.assertEqual(task_runner._search_query("Find the second video"), "")
        self.assertEqual(task_runner._search_query("Find the search box"), "")
        plan = plan_for(
            router.Route(router.BROWSER),
            "Find the search box",
            browser_open=True,
        )
        self.assertEqual([step.tool for step in plan.steps], ["find_element"])

    def test_a_title_and_ordinal_request_targets_the_existing_page_item(self):
        request = "Open the second one. The title is Breathe Lo-Fi Deep Focus Study."
        self.assertEqual(
            task_runner._control_target(request),
            {"strategy": "text", "name": "Breathe Lo-Fi Deep Focus Study"},
        )
        plan = plan_for(
            router.Route(router.BROWSER),
            request,
            browser_open=True,
        )
        self.assertEqual([step.tool for step in plan.steps], ["find_element", "browser_act"])
        self.assertEqual(
            plan.steps[0].args,
            {"strategy": "text", "name": "Breathe Lo-Fi Deep Focus Study"},
        )
        self.assertEqual(
            task_runner._control_target("Open the second one"),
            {"role": "link", "position": "second"},
        )

    def test_acting_on_the_page_already_open_needs_no_address(self):
        # "Click the second video" names no address. Answering that with "no
        # address was named" is why the reported clicking failure happened.
        route = router.classify(
            "Click the second video", router.MachineFacts.build(browser_open=True)
        )
        plan = plan_for(route, "Click the second video", browser_open=True)
        self.assertEqual([step.tool for step in plan.steps], ["find_element", "browser_act"])
        self.assertEqual(plan.steps[0].args, {"role": "link", "position": "second"})
        self.assertEqual(plan.steps[1].depends_on, (plan.steps[0].id,))

    def test_acting_on_a_page_that_is_not_open_plans_nothing(self):
        route = router.classify("Click the second video", router.MachineFacts.build())
        plan = plan_for(route, "Click the second video")
        self.assertEqual(plan.steps, [])
        self.assertIn("no page is open", plan.note)

    def test_an_ordinal_request_names_the_thing_it_is_picking_out_of(self):
        self.assertEqual(
            task_runner._control_target("click the second video"),
            {"role": "link", "position": "second"},
        )
        self.assertEqual(
            task_runner._control_target("open the third result"),
            {"role": "link", "position": "third"},
        )

    def test_a_quoted_name_becomes_a_text_query(self):
        self.assertEqual(
            task_runner._control_target("click 'Baby Shark'"),
            {"strategy": "text", "name": "Baby Shark"},
        )

    def test_a_named_control_becomes_a_role_and_a_name(self):
        self.assertEqual(
            task_runner._control_target("click the Subscribe button"),
            {"role": "button", "name": "Subscribe"},
        )

    def test_an_unsayable_request_plans_no_control(self):
        # No target is a question, not a guess. Every guess here decides what
        # happens on somebody's screen.
        self.assertEqual(task_runner._control_target("do the thing"), {})

    def test_messaging_defers_to_its_own_tool(self):
        # Two places deciding whether a message goes out would mean one of them is
        # the less careful one.
        route = router.classify("message Afia")
        plan = plan_for(route, "message Afia")
        self.assertEqual(plan.steps, [])
        self.assertIn("send_whatsapp", plan.note)

    def test_a_desktop_route_with_no_resolvable_name_plans_nothing(self):
        # A desktop route the router could not pin to a program. Planning a launch
        # from the request's first two words would be a guess, and a wrong launch
        # is a window the user has to close.
        plan = plan_for(router.Route(router.DESKTOP), "open it")
        self.assertEqual(plan.steps, [])
        self.assertIn("which", plan.note)

    def test_an_unresolvable_open_reaches_the_caller_as_a_question(self):
        # Which is what actually happens: the router marks it ambiguous, and the
        # tool answers with the question instead of a plan.
        route = router.classify("open it", router.MachineFacts.build())
        self.assertTrue(route.ambiguous)
        self.assertTrue(route.question)

    def test_the_query_is_stripped_of_the_search_verb(self):
        self.assertEqual(task_runner._query_from_request("find the price of white t-shirts"), "the price of white t-shirts")
        self.assertEqual(task_runner._query_from_request("search for a drawing tablet"), "a drawing tablet")

    def test_only_safe_reads_are_retryable(self):
        route = router.classify("find the price of a tablet")
        plan = plan_for(route, "find the price of a tablet")
        self.assertEqual(plan.steps[0].retry, task_model.READ_RETRY)
        desktop = plan_for(router.classify("open Krita", router.MachineFacts.build(installed={"krita"})), "open Krita")
        self.assertEqual(desktop.steps[0].retry, task_model.NO_RETRY)


# -- the steps ---------------------------------------------------------------------


class OpenAppStepTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, step):
        ctx = task_runner.StepContext(
            step=step,
            route=router.Route(router.DESKTOP, target="krita"),
            request="open Krita",
            outputs={},
            approve=approve,
        )
        return await STEP_IMPLEMENTATIONS[step.tool](ctx)

    def _step(self, **args):
        return task_model.build_step("Open Krita", "open_app", args)

    async def test_a_resolved_app_is_opened_and_reported_observed(self):
        target = app_resolver.AppTarget(
            name="Krita", kind="program", entry=app_index.entry("Krita", app_index.KIND_APP_PATH, r"C:\k.exe")
        )
        with mock.patch.object(app_resolver, "resolve", new=mock.AsyncMock(return_value=target)), \
             mock.patch.object(app_resolver, "open_target", new=mock.AsyncMock(return_value=verification.observed("open", "Opened Krita"))):
            result = await self._run(self._step(app_name="krita"))
        self.assertEqual(result.outcome.status, verification.OBSERVED)
        self.assertEqual(result.outputs["app"], "Krita")

    async def test_an_unresolvable_name_fails_and_starts_nothing(self):
        target = app_resolver.AppTarget(name="photoshop")
        with mock.patch.object(app_resolver, "resolve", new=mock.AsyncMock(return_value=target)), \
             mock.patch.object(app_resolver, "open_target", new=mock.AsyncMock()) as opened:
            result = await self._run(self._step(app_name="photoshop"))
        opened.assert_not_called()
        self.assertEqual(result.outcome.status, verification.FAILED)

    async def test_a_duplicate_launch_is_reported_as_suppressed_not_as_a_new_window(self):
        # The reported "two Krita windows" bug, at the step that fixes it: a second
        # launch inside one task is refused by the launch dedup, and the refusal
        # is reported as such rather than as a successful open.
        target = app_resolver.AppTarget(name="Krita", kind="program", entry=app_index.entry("Krita", app_index.KIND_APP_PATH, r"C:\k.exe"))
        with mock.patch.object(app_resolver, "resolve", new=mock.AsyncMock(return_value=target)), \
             mock.patch.object(app_resolver, "open_target", new=mock.AsyncMock(return_value=verification.observed("open", "Opened Krita"))):
            with mock.patch("agent.tools.apps.dedup_window", return_value=10.0), \
                 mock.patch("agent.tools.apps.reset_launch_history"):
                from agent.tools import apps

                apps._claim(apps.launch_key("Krita"))
                result = await self._run(self._step(app_name="Krita"))
        self.assertIn("already", result.outcome.detail.casefold())


class SearchStepTests(unittest.IsolatedAsyncioTestCase):
    def _step(self, query: str = "drawing tablet"):
        return task_model.build_step("look it up", "search_web", {"query": query})

    async def _run(self, step):
        ctx = task_runner.StepContext(step=step, route=router.Route(router.BACKGROUND), request="x", outputs={}, approve=approve)
        return await STEP_IMPLEMENTATIONS[step.tool](ctx)

    async def test_results_become_a_verified_observation(self):
        with mock.patch.object(task_runner, "_search", new=mock.AsyncMock(return_value=[("A", "https://a.test")])):
            result = await self._run(self._step())
        self.assertEqual(result.outcome.status, verification.OBSERVED)
        self.assertEqual(result.outcome.data["titles"], ["A"])

    async def test_a_failed_lookup_is_a_failure_not_an_observation(self):
        with mock.patch.object(task_runner, "_search", new=mock.AsyncMock(side_effect=RuntimeError("offline"))):
            result = await self._run(self._step())
        self.assertEqual(result.outcome.status, verification.FAILED)
        self.assertIn("offline", result.outcome.detail)

    async def test_no_results_is_a_failure_rather_than_an_empty_success(self):
        with mock.patch.object(task_runner, "_search", new=mock.AsyncMock(return_value=[])):
            result = await self._run(self._step())
        self.assertEqual(result.outcome.status, verification.FAILED)


class BrowserStepTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, step, outputs=None, engine=None, route=None):
        engine = engine or FakeEngine()
        with mock.patch.object(task_runner.control, "engine", return_value=engine):
            ctx = task_runner.StepContext(
                step=step,
                route=route or router.Route(router.BROWSER, url="https://www.youtube.com"),
                request="open youtube in your browser",
                outputs=dict(outputs or {}),
                approve=approve,
            )
            return await STEP_IMPLEMENTATIONS[step.tool](ctx), engine

    async def test_opening_a_page_reports_what_the_page_said(self):
        step = task_model.build_step("open", "open_page", {"url": "https://www.youtube.com"})
        result, engine = await self._run(step)
        self.assertEqual(result.outcome.status, verification.OBSERVED)
        self.assertTrue(engine.pages)

    async def test_opening_without_an_address_fails_before_the_browser(self):
        # The route carries no address and the step names none, so nothing is
        # navigated. A browser opened here would be a window the user did not ask
        # for and cannot explain.
        step = task_model.build_step("open", "open_page", {})
        result, engine = await self._run(step, route=router.Route(router.BROWSER))
        self.assertEqual(result.outcome.status, verification.FAILED)
        self.assertEqual(engine.pages, [])

    async def test_finding_a_control_returns_it_and_its_generation(self):
        # The generation is carried forward, because the next step's action is
        # checked against it: without it, "find then click" could click against a
        # page the caller never saw.
        engine = FakeEngine(
            initial=[page([control(1, "Baby Shark"), control(2, "Bath Song")])]
        )
        step = task_model.build_step("find", "find_element", {"role": "link", "name": "Bath Song"})
        result, _ = await self._run(step, engine=engine)
        self.assertEqual(result.outcome.status, verification.OBSERVED)
        self.assertEqual(result.outputs["element"]["name"], "Bath Song")
        self.assertTrue(result.outputs["generation"])

    async def test_browser_search_returns_visible_result_titles(self):
        from agent.tools.browser import search as browser_search

        engine = FakeEngine(
            initial=[
                page(
                    [],
                    items=[
                        {
                            "index": 1,
                            "kind": "video",
                            "title": "Breathe Lo-Fi Deep Focus Study",
                            "text": "Breathe Lo-Fi Deep Focus Study video",
                            "href": "https://www.youtube.com/watch?v=fixture",
                        }
                    ],
                )
            ]
        )
        outcome = verification.observed(
            "search", "Searched for lo-fi", evidence="the results page moved", url="https://www.youtube.com/results?search_query=lo-fi"
        )
        step = task_model.build_step(
            "search", "browser_search", {"query": "lo-fi"}
        )
        ctx = task_runner.StepContext(
            step=step,
            route=router.Route(router.BROWSER),
            request="search for lo-fi",
            outputs={},
            approve=approve,
        )
        with mock.patch.object(task_runner.control, "engine", return_value=engine), \
                mock.patch.object(browser_search, "run_search", new=mock.AsyncMock(return_value=outcome)):
            result = await task_runner.STEP_IMPLEMENTATIONS["browser_search"](ctx)
        self.assertEqual(
            result.outcome.data["results"],
            [
                {
                    "index": 1,
                    "kind": "video",
                    "title": "Breathe Lo-Fi Deep Focus Study",
                    "url": "https://www.youtube.com/watch?v=fixture",
                }
            ],
        )

    async def test_finding_something_absent_fails_and_lists_what_is_there(self):
        engine = FakeEngine(initial=[page([control(1, "Baby Shark")])])
        step = task_model.build_step("find", "find_element", {"role": "button", "name": "Subscribe"})
        result, _ = await self._run(step, engine=engine)
        self.assertEqual(result.outcome.status, verification.FAILED)
        self.assertEqual(result.outcome.data["candidates"], ["Baby Shark"])

    async def test_an_ambiguous_find_reports_candidates_rather_than_choosing(self):
        engine = FakeEngine(initial=[page([control(1, "Search"), control(2, "Search")])])
        step = task_model.build_step("find", "find_element", {"role": "button", "name": "Search"})
        result, _ = await self._run(step, engine=engine)
        self.assertEqual(result.outcome.status, verification.FAILED)

    async def test_acting_without_a_find_first_refuses(self):
        # The model cannot skip the observation and act on a name it invented.
        step = task_model.build_step("click", "browser_act", {"verb": "click"})
        result, _ = await self._run(step)
        self.assertEqual(result.outcome.status, verification.FAILED)
        self.assertIn("haven't found", result.outcome.detail)

    async def test_acting_passes_the_element_and_generation_through(self):
        engine = FakeEngine(initial=[page([control(1, "Baby Shark")])])
        find = task_model.build_step("find", "find_element", {"role": "link", "name": "Baby Shark"})
        with mock.patch.object(task_runner.control, "engine", return_value=engine):
            ctx = task_runner.StepContext(
                step=find,
                route=router.Route(router.BROWSER),
                request="x",
                outputs={},
                approve=approve,
            )
            found = await STEP_IMPLEMENTATIONS["find_element"](ctx)
            act = task_model.build_step("click", "browser_act", {"verb": "click"})
            ctx2 = task_runner.StepContext(
                step=act,
                route=router.Route(router.BROWSER),
                request="x",
                outputs={**ctx.outputs, **found.outputs},
                approve=approve,
            )
            result = await STEP_IMPLEMENTATIONS["browser_act"](ctx2)
        self.assertEqual(result.outcome.status, verification.OBSERVED)
        self.assertEqual(engine.acted[0].expected["name"], "Baby Shark")
        self.assertEqual(engine.acted[0].generation, found.outputs["generation"])

    async def test_an_engine_refusal_becomes_a_step_failure(self):
        engine = FakeEngine(initial=[page([control(1, "Baby Shark")])])
        engine.act_error = "the page changed since I looked at it"
        find = task_model.build_step("find", "find_element", {"role": "link", "name": "Baby Shark"})
        with mock.patch.object(task_runner.control, "engine", return_value=engine):
            ctx = task_runner.StepContext(step=find, route=router.Route(router.BROWSER), request="x", outputs={}, approve=approve)
            found = await STEP_IMPLEMENTATIONS["find_element"](ctx)
            act = task_model.build_step("click", "browser_act", {"verb": "click"})
            ctx2 = task_runner.StepContext(
                step=act,
                route=router.Route(router.BROWSER),
                request="x",
                outputs={**ctx.outputs, **found.outputs},
                approve=approve,
            )
            result = await STEP_IMPLEMENTATIONS["browser_act"](ctx2)
        self.assertEqual(result.outcome.status, verification.FAILED)
        self.assertIn("changed", result.outcome.detail)


# -- the executor ----------------------------------------------------------------


class ExecuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_task_runs_end_to_end_and_reports_its_route(self):
        route = router.classify("find the price of a drawing tablet", router.MachineFacts.build())
        plan = plan_for(route, "find the price of a drawing tablet")
        task = task_model.make_task("find the price of a drawing tablet", plan.steps)
        with mock.patch.object(task_runner, "_search", new=mock.AsyncMock(return_value=[("A", "https://a.test")])):
            await task_runner.execute(
                task, approve=approve, request=task.request, route=route, narration=[]
            )
        payload = task_runner.report(task, route)
        self.assertIn(task_model.TASK_COMPLETED, payload)
        self.assertIn("search_web", payload)

    async def test_a_cancelled_task_runs_nothing(self):
        route = router.classify("find the price of a drawing tablet", router.MachineFacts.build())
        plan = plan_for(route, "find the price of a drawing tablet")
        task = task_model.make_task("x", plan.steps)
        task.cancel("stopped")
        with mock.patch.object(task_runner, "_search", new=mock.AsyncMock()) as search:
            await task_runner.execute(task, approve=approve, request="x", route=route)
        search.assert_not_called()
        self.assertEqual(task.status, task_model.TASK_CANCELLED)

    async def test_an_uncertain_step_is_surfaced_in_the_report(self):
        route = router.classify("open Krita", router.MachineFacts.build(installed={"krita"}))
        plan = plan_for(route, "open Krita")
        task = task_model.make_task("open Krita", plan.steps)
        target = app_resolver.AppTarget(name="Krita", entry=app_index.entry("Krita", app_index.KIND_APP_PATH, r"C:\k.exe"))
        with mock.patch.object(app_resolver, "resolve", new=mock.AsyncMock(return_value=target)), \
             mock.patch.object(app_resolver, "open_target", new=mock.AsyncMock(return_value=verification.uncertain("open", "started it but cannot tell"))):
            await task_runner.execute(task, approve=approve, request="open Krita", route=route)
        payload = task_runner.report(task, route)
        self.assertIn("uncertain", payload)

    async def test_the_session_registry_is_cleared_after_a_run(self):
        route = router.classify("find a tablet price", router.MachineFacts.build())
        plan = plan_for(route, "find a tablet price")
        task = task_model.make_task("x", plan.steps)
        session = object()
        with mock.patch.object(task_runner, "_search", new=mock.AsyncMock(return_value=[("A", "u")])):
            await task_runner.execute(task, approve=approve, request="x", route=route, session=session)
        self.assertIsNone(task_runner.for_session(session))


# -- the stage's step ledger --------------------------------------------------


class FakeSession:
    """A session the weak-keyed publisher registry can hold.

    `SimpleNamespace` cannot be weakly referenced, so registering one is silently
    dropped and every publish test would fail for the wrong reason -- a test that
    passes because its own fixture was ignored is the worst kind of passing.
    """

    def __init__(self, name: str = "fake") -> None:
        self.name = name


class _Recorder:
    """A publisher that keeps the progress records instead of sending them."""

    def __init__(self, sink: list[dict]) -> None:
        self.sink = sink

    def emit_and_publish(self, topic: str, record: dict) -> dict:
        self.sink.append({"topic": topic, **record})
        return record


class TaskStepLedgerTests(unittest.IsolatedAsyncioTestCase):
    """A task publishes its steps, so the stage can draw a ledger while it runs.

    The narration in `_observer` is deliberately filtered -- a voice turn does not
    want a commentary on every verification read -- and the ledger cannot be,
    because a list showing three of its six rows is a list the reader cannot
    trust. These tests exist so the publish side cannot quietly inherit the
    narration side's filter, which is the shape of the mistake that would be made
    here: one `if` in the wrong place and the ledger shows only the steps that
    acted.
    """

    def setUp(self):
        from agent.runtime import progress

        self.progress = progress
        self.session = FakeSession()
        self.records: list[dict] = []
        progress.register_publisher(self.session, _Recorder(self.records))
        self.addCleanup(progress.unregister_publisher, self.session)

    def _steps(self) -> list[dict]:
        return [r for r in self.records if r.get("action", "").startswith("step")]

    async def test_every_step_of_a_run_is_published_with_its_position(self):
        route = router.classify("find the price of a drawing tablet", router.MachineFacts.build())
        plan = plan_for(route, "find the price of a drawing tablet")
        task = task_model.make_task("find the price of a drawing tablet", plan.steps)
        with mock.patch.object(task_runner, "_search", new=mock.AsyncMock(return_value=[("A", "https://a.test")])):
            await task_runner.execute(
                task, approve=approve, request=task.request, route=route, session=self.session
            )

        steps = self._steps()
        total = len(task.steps)
        self.assertTrue(steps, "a completed task published no steps at all")
        # `total` is what lets the app draw the rows that have not started yet as
        # pending; without it the list grows under the reader one row at a time.
        self.assertTrue(all(r["total"] == total for r in steps))
        self.assertTrue(all(r["tool"] == task_runner.TASK_TOOL for r in steps))
        self.assertTrue(all(r["task"] == task.id for r in steps))
        self.assertTrue(all(r["status"] in ("started", "done", "failed") for r in steps))
        # 1-based positions against the *plan*, not a counter kept while
        # publishing: read steps run concurrently, so arrival order is not
        # plan order and a maintained counter would credit the wrong row.
        for record in steps:
            index = int(record["action"][len("step"):])
            self.assertGreaterEqual(index, 1)
            self.assertLessEqual(index, total)
            self.assertEqual(record["index"], index)
        started = {r["action"] for r in steps if r["status"] == "started"}
        done = {r["action"] for r in steps if r["status"] == "done"}
        self.assertTrue(done, "a finished task left every row open")
        self.assertTrue(started >= done, "a step finished without ever starting")

    async def test_a_step_that_never_started_publishes_nothing_for_it(self):
        route = router.classify("find a tablet price", router.MachineFacts.build())
        plan = plan_for(route, "find a tablet price")
        task = task_model.make_task("x", plan.steps)
        # Cancelled before it runs: the engine settles the rest as skipped and
        # never notifies per step, so the ledger's pending rows are the honest
        # reading and a record for them would claim a step that never moved.
        task.cancel("stopped")
        with mock.patch.object(task_runner, "_search", new=mock.AsyncMock()):
            await task_runner.execute(
                task, approve=approve, request="x", route=route, session=self.session
            )
        self.assertEqual(self._steps(), [])

    def test_a_retry_is_published_as_the_step_running_again(self):
        task = task_model.make_task("x", [task_model.build_step("look up", "search_web", ident="a")])
        step = task.steps[0]
        task_runner._publish_task_step(self.session, task, step, "step_retry")
        self.assertEqual(self._steps()[0]["status"], "started")
        self.assertIn("retrying", self._steps()[0]["summary"])

    def test_a_failure_carries_the_reason_and_no_duration(self):
        task = task_model.make_task("x", [task_model.build_step("look up", "search_web", ident="a")])
        step = task.steps[0]
        step.status = task_model.FAILED
        step.error = "the page never loaded"
        task_runner._publish_task_step(self.session, task, step, "step_failed")
        record = self._steps()[0]
        self.assertEqual(record["status"], "failed")
        self.assertIn("the page never loaded", record["summary"])
        # `started_at`/`ended_at` are both zero, so `duration_ms` is falsy and
        # dropped: a failed step that claims 0ms reads as "instant" rather than
        # as "never measured".
        self.assertNotIn("duration_ms", record)

    def test_a_step_from_another_plan_is_dropped_rather_than_numbered(self):
        task = task_model.make_task("x", [task_model.build_step("look up", "search_web", ident="a")])
        stray = task_model.build_step("elsewhere", "search_web", ident="z")
        task_runner._publish_task_step(self.session, task, stray, "step_started")
        self.assertEqual(self._steps(), [])

    def test_an_event_with_no_step_publishes_nothing(self):
        task = task_model.make_task("x", [task_model.build_step("look up", "search_web", ident="a")])
        task_runner._publish_task_step(self.session, task, task.steps[0], "task_started")
        self.assertEqual(self._steps(), [])


class CancellationToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_with_nothing_running_says_so(self):
        result = await task_runner.cancel_task.__wrapped__(CONTEXT)
        self.assertIn("nothing running", result)

    async def test_cancelling_a_running_task_stops_it(self):
        session = object()
        task = task_model.make_task("open Krita", [task_model.build_step("open", "open_app")])
        task_runner.register(session, LiveTask(task=task))
        result = await task_runner.cancel_task.__wrapped__(SimpleNamespace(session=session))
        self.assertIn("Stopped", result)
        self.assertEqual(task.status, task_model.TASK_CANCELLED)
        task_runner._TASKS.pop(id(session), None)

    def test_releasing_a_session_cancels_its_task(self):
        session = object()
        task = task_model.make_task("x", [task_model.build_step("open", "open_app")])
        task_runner.register(session, LiveTask(task=task))
        task_runner.release(session)
        self.assertTrue(task.cancelled)
        self.assertIsNone(task_runner.for_session(session))


class LatencyTests(unittest.IsolatedAsyncioTestCase):
    """The phases a user feels and cannot otherwise see."""

    def setUp(self):
        self._sessions: list = []

    def _tracker(self):
        from agent.runtime.latency import LatencyTracker

        rows: list[tuple] = []
        # The tracker's emit is `emit(type, **payload)`, and `stage` arrives
        # *inside* the payload -- so the recorded stage name is read from the
        # fields, not from the first positional argument.
        return LatencyTracker(lambda _type, **fields: rows.append((_type, fields))), rows

    def _session(self):
        from agent.runtime.latency import register_tracker

        class Session:
            """A holdable object.

            A *weak* registry is what `latency` uses, so the test keeps this
            alive for its own duration; a session that were collected between
            registration and the call would make the measurement vanish and the
            test would pass for the wrong reason.
            """

        session = Session()
        tracker, rows = self._tracker()
        register_tracker(session, tracker)
        self._sessions.append(session)
        return session, rows

    async def test_routing_publishes_its_own_duration_and_the_facts_split(self):
        session, rows = self._session()
        with mock.patch.object(task_runner, "machine_facts", new=mock.AsyncMock(return_value=router.MachineFacts.build())):
            await task_runner.measure_route(session, "find the price of a tablet")
        stage = next((row[1] for row in rows if row[1].get("stage") == "route_classify"), None)
        self.assertIsNotNone(stage)
        self.assertIn("environment", stage)
        # Cold and warm mean different things, so the facts read is separated out.
        self.assertIn("facts_ms", stage)

    async def test_app_resolution_and_launch_are_measured_separately(self):
        session, rows = self._session()
        target = app_resolver.AppTarget(
            name="Krita", kind="program", entry=app_index.entry("Krita", app_index.KIND_APP_PATH, r"C:\k.exe")
        )
        step = task_model.build_step("open", "open_app", {"app_name": "krita"})
        with mock.patch.object(app_resolver, "resolve", new=mock.AsyncMock(return_value=target)), \
             mock.patch.object(app_resolver, "open_target", new=mock.AsyncMock(return_value=verification.observed("open", "Opened"))), \
             mock.patch("agent.tools.apps.dedup_window", return_value=0.0):
            ctx = task_runner.StepContext(
                step=step, route=router.Route(router.DESKTOP), request="open krita",
                outputs={}, approve=approve, session=session,
            )
            await task_runner.STEP_IMPLEMENTATIONS["open_app"](ctx)
        stages = {row[1].get("stage") for row in rows}
        self.assertIn("app_resolve", stages)
        self.assertIn("app_launch", stages)

    async def test_a_measurement_failure_never_breaks_a_step(self):
        # Telemetry that can fail is a session that can die; see the same rule in
        # `runtime_events.write_event_line`.
        session, _rows = self._session()
        with mock.patch.object(task_runner.latency_module, "emit_duration", side_effect=OSError("pipe")):
            with mock.patch.object(task_runner, "machine_facts", new=mock.AsyncMock(return_value=router.MachineFacts.build())):
                route, _facts, _ms = await task_runner.measure_route(session, "find a price")
        self.assertEqual(route.environment, router.BACKGROUND)


class ReportTests(unittest.TestCase):
    def test_the_report_names_the_route_and_why(self):
        route = router.classify("find the price of a drawing tablet", router.MachineFacts.build())
        task = task_model.make_task("x", [task_model.build_step("look up", "search_web")])
        payload = task_runner.report(task, route)
        self.assertIn("route", payload)
        self.assertIn("why", payload)

    def test_a_skipped_step_is_reported_as_skipped_not_failed(self):
        steps = [
            task_model.build_step("launch", "open_app", ident="a"),
            task_model.build_step("use", "open_page", depends_on=("a",), ident="b"),
        ]
        task = task_model.make_task("x", steps)
        task.steps[0].status = task_model.FAILED
        task.steps[0].error = "no"
        task.steps[1].status = task_model.SKIPPED
        payload = task_runner.report(task, router.Route(router.DESKTOP))
        self.assertIn("skipped", payload)

    def test_browser_search_results_are_available_to_the_next_decision(self):
        step = task_model.build_step("search", "browser_search", {"query": "lo-fi"})
        step.outcome = verification.observed(
            "browser_search",
            "Searched",
            results=[{"index": 1, "kind": "video", "title": "Breathe Lo-Fi"}],
        )
        payload = task_runner.report(
            task_model.make_task("search", [step]), router.Route(router.BROWSER)
        )
        self.assertIn('"results":[{"index":1,"kind":"video","title":"Breathe Lo-Fi"}]', payload)


if __name__ == "__main__":
    unittest.main()