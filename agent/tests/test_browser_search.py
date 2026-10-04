"""Searching a page, and what happens when there is nothing to search.

The reported failure was "she cannot search": the plan typed a query and stopped,
because filling and submitting were two steps and the second could be lost. These
tests pin the properties that make that impossible to reintroduce quietly:

* **one implementation.** `run_task`'s `browser_search` step and the
  `web_control search` verb both go through `search.run_search`, so a search
  cannot be right in one place and wrong in the other.
* **typing and submitting are one call,** and the value is committed before Enter.
* **the outcome is a `verification` status,** so "I typed it and the page has not
  confirmed" is never reported as a result list.
* **a search asks before it submits** when the field is inside a form that
  submits -- classified from DOM facts, exactly as a click on that form's button
  would be.

Everything runs against a fake engine and fixture candidates: no browser, no
network, no window.
"""

from __future__ import annotations

import asyncio
import unittest

from agent.runtime import verification
from agent.tools.browser import search, state
from agent.tools.browser.locators import Candidate


def candidate(index: int, name: str, role: str = "searchbox", **detail) -> Candidate:
    return Candidate(
        index=index, role=role, name=name, tag="input", detail=detail
    )


def snapshot(url: str, candidates: list[Candidate], generation: int = 7) -> state.Snapshot:
    return state.Snapshot(
        generation=generation, url=url, title="Fixture", text="", candidates=tuple(candidates)
    )


class FakePage:
    """A page whose address the test moves by hand."""

    def __init__(self, url: str) -> None:
        self.url = url


class FakeEngine:
    """The engine contract, recording what it was asked to do."""

    def __init__(self, page: FakePage | None = None) -> None:
        self._page_value = page
        self.acted: list = []

    async def _page(self):
        if self._page_value is None:
            raise RuntimeError("no page")
        return self._page_value

    async def act(self, step):
        self.acted.append(step)
        return {"ok": True, "before": {}, "after": {}, "element": {}, "host": ""}


class Recorder:
    """An approval that records instead of deciding."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, *, action_class: str, summary: str, target: str) -> None:
        self.calls.append({"action_class": action_class, "summary": summary, "target": target})


class FakeSession:
    """A session the weak-keyed publisher registry can hold.

    `SimpleNamespace` cannot be weakly referenced, so registering one is silently
    dropped and every narration test would fail for the wrong reason.
    """

    def __init__(self, name: str = "fake") -> None:
        self.name = name


class _Recorder:
    """A publisher that keeps the progress records instead of sending them."""

    def __init__(self, sink: list[dict]) -> None:
        self.sink = sink

    def emit_and_publish(self, topic: str, record: dict) -> dict:
        self.sink.append(dict(record))
        return record


# -- finding the field --------------------------------------------------------


class FindSearchFieldTests(unittest.TestCase):
    def test_a_control_inside_a_form_is_reported_as_such(self):
        # `inForm` is what the permission class is computed from, and it has to
        # survive the collection script: a search box inside a submitting form is
        # `sensitive` because Enter submits it. It was once not collected at all,
        # which made every control read as outside a form.
        for detail, expected in (({}, False), ({"inForm": True}, True)):
            with self.subTest(detail=detail):
                found = search.find_search_field(
                    snapshot("https://example.com/", [candidate(1, "search", **detail)])
                )
                self.assertEqual(found.in_form, expected)

    def test_a_known_site_uses_its_own_selector(self):
        found = search.find_search_field(snapshot("https://www.youtube.com/", []))
        self.assertIsNotNone(found)
        # Trusted as the site's field: whoever wrote that selector had the page in
        # front of them. It is still re-validated by name once found.
        self.assertIn("search_query", found.query.css or "")

    def test_an_exact_search_label_beats_one_that_merely_mentions_search(self):
        found = search.find_search_field(
            snapshot(
                "https://example.com/",
                [
                    candidate(1, "Search filters"),
                    candidate(2, "search"),
                ],
            )
        )
        self.assertEqual(found.expected["name"], "search")

    def test_a_page_with_no_search_field_is_refused_rather_than_guessed(self):
        found = search.find_search_field(
            snapshot("https://example.com/", [candidate(1, "Subscribe", role="button")])
        )
        self.assertIsNone(found)

    def test_a_label_less_field_inside_a_lone_form_is_still_found(self):
        # Google's shape: `name="q"` with nothing in the label that says search.
        # Without the shape-only signal the most-used search engine has no field to
        # type into.
        found = search.find_search_field(
            snapshot(
                "https://example.com/",
                [candidate(1, "q", role="textbox", soleField=True)],
            )
        )
        self.assertIsNotNone(found)

    def test_a_real_label_outranks_the_shape_signal(self):
        found = search.find_search_field(
            snapshot(
                "https://example.com/",
                [
                    candidate(1, "q", role="textbox", soleField=True),
                    candidate(2, "search", role="searchbox"),
                ],
            )
        )
        self.assertEqual(found.expected["name"], "search")

    def test_a_refusal_names_the_fields_the_page_did_offer(self):
        # "There is no search box" is a dead end; naming what is there lets the
        # model try again on its own instead of reporting nothing.
        names = search.no_field_fields(
            snapshot("https://example.com/", [candidate(1, "Email"), candidate(2, "Password")])
        )
        self.assertEqual(names, ["Email", "Password"])

    def test_the_refusal_list_is_bounded(self):
        # A page with four hundred controls must not turn a refusal into a
        # paragraph the model has to pay for on every later turn.
        many = [candidate(i, f"field{i}") for i in range(1, 50)]
        self.assertLessEqual(len(search.no_field_fields(snapshot("https://e.com/", many))), 6)


# -- query normalization ------------------------------------------------------


class NormalizeQueryTests(unittest.TestCase):
    def test_whitespace_is_collapsed_rather_than_typed_in(self):
        self.assertEqual(search.normalize_query("  baby   shark \n remix "), "baby shark remix")

    def test_an_empty_query_is_empty_not_an_error(self):
        # The caller turns this into a sentence; this function stays total so the
        # same shape handles "nothing asked" and "far too long".
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                self.assertEqual(search.normalize_query(raw), "")

    def test_a_runaway_generation_cannot_fill_a_field_for_a_minute(self):
        self.assertEqual(len(search.normalize_query("x" * 5000)), search.MAX_QUERY_CHARS)


# -- the outcome --------------------------------------------------------------


class RunSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_empty_query_fails_without_touching_the_page(self):
        engine = FakeEngine(FakePage("https://example.com/"))
        outcome = await search.run_search(engine, snapshot("https://example.com/", []), "")
        self.assertEqual(outcome.status, verification.FAILED)
        self.assertEqual(engine.acted, [])

    async def test_a_page_with_no_search_box_fails_and_names_what_it_saw(self):
        engine = FakeEngine(FakePage("https://example.com/"))
        snap = snapshot("https://example.com/", [candidate(1, "Email", role="textbox")])
        outcome = await search.run_search(engine, snap, "kittens")
        self.assertEqual(outcome.status, verification.FAILED)
        self.assertIn("Email", outcome.data["fields"])
        self.assertEqual(engine.acted, [])

    async def test_typing_and_submitting_happen_in_one_call(self):
        # The reported bug. Two *tools* would let the second be lost, skipped by a
        # re-plan, or never planned -- so the search typed and stopped.
        engine = FakeEngine(FakePage("https://example.com/results"))
        snap = snapshot("https://example.com/", [candidate(1, "search")], generation=3)
        outcome = await search.run_search(engine, snap, "kittens")
        kinds = [step.kind for step in engine.acted]
        self.assertEqual(kinds, ["fill", "press"])
        # The value is committed before Enter means anything, which is why the
        # submit is a separate engine action inside the one call.
        self.assertEqual(engine.acted[1].key, "Enter")

    async def test_a_results_page_is_observed_not_assumed(self):
        engine = FakeEngine(FakePage("https://example.com/search?q=kittens"))
        snap = snapshot("https://example.com/", [candidate(1, "search")])
        outcome = await search.run_search(engine, snap, "kittens")
        self.assertEqual(outcome.status, verification.OBSERVED)
        self.assertIn("q=kittens", outcome.data["url"])
        self.assertTrue(outcome.evidence, "observed needs the evidence that earned it")

    async def test_a_search_that_typed_but_did_not_move_is_accepted_not_observed(self):
        # The distinction the whole module exists for. The page may be showing
        # results by now, or the search may have silently done nothing, and from
        # here those look identical -- so neither is called a success.
        page = FakePage("https://example.com/")
        engine = FakeEngine(page)
        snap = snapshot("https://example.com/", [candidate(1, "search")])
        outcome = await search.run_search(engine, snap, "kittens", )
        self.assertEqual(outcome.status, verification.ACCEPTED)
        self.assertTrue(outcome.data["typed"])
        self.assertFalse(verification.succeeded(outcome.status))

    async def test_the_generation_is_carried_so_a_moved_page_refuses(self):
        engine = FakeEngine(FakePage("https://example.com/"))
        snap = snapshot("https://example.com/", [candidate(1, "search")], generation=42)
        await search.run_search(engine, snap, "kittens")
        self.assertEqual(engine.acted[0].generation, 42)

    async def test_an_engine_refusal_becomes_a_sentence_not_a_crash(self):
        class Refusing(FakeEngine):
            async def act(self, step):
                raise RuntimeError("that control changed on the page")

        outcome = await search.run_search(
            Refusing(FakePage("https://example.com/")),
            snapshot("https://example.com/", [candidate(1, "search")]),
            "kittens",
        )
        self.assertEqual(outcome.status, verification.FAILED)
        self.assertIn("changed", outcome.detail)

    async def test_a_missing_page_is_a_refusal_rather_than_an_exception(self):
        outcome = await search.run_search(
            FakeEngine(None), snapshot("https://example.com/", [candidate(1, "search")]), "x"
        )
        # Typed and submitted; the *check* could not read the address, so this is
        # explicitly not a confirmed result.
        self.assertNotEqual(outcome.status, verification.OBSERVED)


# -- the permission class -----------------------------------------------------


class ProgressNarrationTests(unittest.IsolatedAsyncioTestCase):
    """A search narrates itself, because silence reads as a broken microphone.

    A search is the slowest thing these tools do and :func:`wait_for_results` will
    hold for :data:`SEARCH_SETTLE_SECONDS` before giving up -- eight seconds of a
    voice turn with no output. WhatsApp already publishes steps for exactly this
    reason; these tests exist so the browser path cannot quietly lose them.
    """

    def setUp(self):
        from agent.runtime import progress

        self.progress = progress
        self.session = FakeSession()
        self.records: list[dict] = []
        self.addCleanup(progress.unregister_publisher, self.session)
        progress.register_publisher(self.session, _Recorder(self.records))

    def _actions(self) -> list[str]:
        return [r["action"] for r in self.records if r["status"] == "started"]

    async def test_the_three_phases_are_published_in_order(self):
        engine = FakeEngine(FakePage("https://www.youtube.com/results?search_query=x"))
        outcome = await search.run_search(
            engine, snapshot("https://www.youtube.com/", [candidate(1, "search")]),
            "baby shark", context=self.session,
        )
        self.assertEqual(outcome.status, verification.OBSERVED)
        self.assertEqual(self._actions(), ["find", "type", "wait"])

    async def test_every_step_declares_its_position(self):
        # The app draws "step 2 of 3" from these rather than counting, because the
        # records arrive interleaved and a counter would be a guess.
        engine = FakeEngine(FakePage("https://www.youtube.com/results?search_query=x"))
        await search.run_search(
            engine, snapshot("https://www.youtube.com/", [candidate(1, "search")]),
            "baby shark", context=self.session,
        )
        started = [r for r in self.records if r["status"] == "started"]
        self.assertEqual([r["index"] for r in started], [1, 2, 3])
        self.assertEqual({r["total"] for r in started}, {search.SEARCH_STEPS})

    async def test_the_steps_name_the_site_so_waiting_is_not_vague(self):
        # "Waiting" with no subject is the least reassuring thing a progress list
        # can contain.
        engine = FakeEngine(FakePage("https://www.youtube.com/results?search_query=x"))
        await search.run_search(
            engine, snapshot("https://www.youtube.com/", [candidate(1, "search")]),
            "baby shark", context=self.session,
        )
        messages = " ".join(r["message"] for r in self.records)
        self.assertIn("youtube.com", messages)

    async def test_a_search_without_a_context_still_works(self):
        # Every existing caller and every unit test passes no context. A progress
        # channel that is absent must cost nothing and raise nothing.
        engine = FakeEngine(FakePage("https://example.com/results"))
        outcome = await search.run_search(
            engine, snapshot("https://example.com/", [candidate(1, "search")]), "x"
        )
        self.assertEqual(outcome.status, verification.OBSERVED)

    async def test_a_failed_search_still_closes_its_last_step(self):
        # The step list must not be left with a spinner running after the tool
        # returned. `progress.step` publishes a `failed` record on the way out, and
        # the engine refusal raises through it.
        class Refusing(FakeEngine):
            async def act(self, step):
                raise RuntimeError("that control changed on the page")

        outcome = await search.run_search(
            Refusing(FakePage("https://example.com/")),
            snapshot("https://example.com/", [candidate(1, "search")]),
            "kittens", context=self.session,
        )
        self.assertEqual(outcome.status, verification.FAILED)
        # The `type` step opened and closed; nothing after it opened, because the
        # exception propagated out of the block that would have opened it.
        self.assertIn("failed", [r["status"] for r in self.records])
        self.assertNotIn("wait", self._actions())


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_search_outside_a_form_is_an_ordinary_act(self):
        engine = FakeEngine(FakePage("https://example.com/"))
        approve = Recorder()
        snap = snapshot("https://example.com/", [candidate(1, "search")])
        await search.run_search(engine, snap, "kittens", approve=approve)
        self.assertEqual(approve.calls[0]["action_class"], "act")

    async def test_a_search_inside_a_form_that_submits_asks_first(self):
        # HTML's implicit submission: Enter in a form is a submission whichever way
        # it was activated, so it is gated exactly as clicking the button would be.
        engine = FakeEngine(FakePage("https://example.com/"))
        approve = Recorder()
        snap = snapshot("https://example.com/", [candidate(1, "search", inForm=True)])
        await search.run_search(engine, snap, "kittens", approve=approve)
        self.assertEqual(approve.calls[0]["action_class"], "sensitive")

    async def test_the_approval_names_the_host_and_the_query(self):
        # A question naming neither the site nor the words is a question nobody
        # can answer from a card.
        engine = FakeEngine(FakePage("https://example.com/"))
        approve = Recorder()
        snap = snapshot("https://www.youtube.com/", [candidate(1, "search")])
        await search.run_search(engine, snap, "baby shark", approve=approve)
        self.assertIn("youtube.com", approve.calls[0]["summary"])
        self.assertIn("baby shark", approve.calls[0]["summary"])

    async def test_the_search_is_not_typed_when_the_approval_refuses(self):
        # `approve` raising is the gate's refusal reaching us. Nothing must have
        # been typed by then.
        class Refusing:
            async def __call__(self, **kwargs):
                raise RuntimeError("the user said no")

        engine = FakeEngine(FakePage("https://example.com/"))
        snap = snapshot("https://example.com/", [candidate(1, "search")])
        with self.assertRaises(RuntimeError):
            await search.run_search(engine, snap, "kittens", approve=Refusing())
        self.assertEqual(engine.acted, [])


# -- waiting ------------------------------------------------------------------


class WaitForResultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_url_that_moved_immediately_is_not_waited_for(self):
        page = FakePage("https://example.com/results?q=x")
        got = await search.wait_for_results(
            FakeEngine(page), "https://example.com/", None, seconds=5.0
        )
        self.assertEqual(got, "https://example.com/results?q=x")

    async def test_a_page_that_never_moves_gives_up_within_its_bound(self):
        page = FakePage("https://example.com/")
        got = await search.wait_for_results(FakeEngine(page), "https://example.com/", None, seconds=0.3)
        self.assertIsNone(got)

    async def test_a_late_navigation_is_still_caught(self):
        # Polled rather than slept once, so a slow page is noticed when it lands
        # instead of after a fixed guess.
        page = FakePage("https://example.com/")

        async def move():
            await asyncio.sleep(0.15)
            page.url = "https://example.com/results?q=x"

        mover = asyncio.create_task(move())
        got = await search.wait_for_results(FakeEngine(page), "https://example.com/", None, seconds=3.0)
        await mover
        self.assertEqual(got, "https://example.com/results?q=x")

    async def test_a_single_page_app_that_swapped_content_is_not_called_a_result(self):
        # The address never moved, so from here it is indistinguishable from a
        # search that produced nothing. Only one of those is a success.
        page = FakePage("https://example.com/")
        got = await search.wait_for_results(FakeEngine(page), "https://example.com/", None, seconds=0.3)
        self.assertIsNone(got)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()