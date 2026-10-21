"""End-to-end against a real Chromium and a real page, through the real engine.

This is the check that matters for the reported failure. The unit tests prove the
planner emits the right steps; this proves the steps *work* -- that a search types
into a real field, submits, and lands somewhere real, and that a positional click
hits the element the ranking chose rather than the first match.

**One loop for the whole journey.** Playwright's objects belong to the loop that
made them, and `session` refuses to reach across loops by design, so each test
method builds its own engine on a fresh loop and releases the shared browser at
the end of that loop. That is the same arrangement `test_browser_engine.py` uses
and the reason neither file flakes.

**No network.** The fixture is a loopback HTTP server, which is why
`LUMINE_BROWSER_ALLOW_LOCAL` is set: the thing under test *is* the behaviour of a
local address.
"""

from __future__ import annotations

import asyncio
import http.server
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent.tools import task_runner  # noqa: E402
from agent.tools.browser import (  # noqa: E402
    control,
    engine,
    locators,
    search,
    session,
    sites,
)

#: A page shaped like the one that actually broke: a search box with **no**
#: aria-label and **no** placeholder (YouTube's shape), addressable only because
#: the collector falls back to `name`; and a results list whose links are the
#: only way to pick "the second one".
FIXTURE = b"""<!doctype html>
<html>
<head><title>Fixture home</title></head>
<body>
  <h1>Fixture</h1>
  <form action="/results" method="get" role="search">
    <input id="hidden-youtube-search" type="hidden" name="search_query">
    <input id="youtube-search" name="search_query">
    <input name="q" id="q">
    <button type="submit">Go</button>
  </form>
  <ul id="results">
    <li><a href="/watch?v=1" title="First result">First result</a></li>
    <li><a href="/watch?v=2" title="Second result">Second result</a></li>
    <li><a href="/watch?v=3" title="Third result">Third result</a></li>
  </ul>
  <p id="where">home</p>
</body>
</html>
"""

RESULTS = b"""<!doctype html>
<html>
<head><title>Results</title></head>
<body>
  <form action="/results" method="get"><input name="q"></form>
  <ul><li><a href="/x">First result</a></li><li><a href="/y">Second result</a></li></ul>
  <p id="where">results</p>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):  # noqa: A003 - silence the test output
        return

    def do_GET(self):  # noqa: N802 - the BaseHTTPRequestHandler name
        body = RESULTS if self.path.startswith("/results") else FIXTURE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


async def _approve(**_kwargs) -> None:
    """A no-op gate: the approval *path* is `test_permission_gate`'s subject."""
    return None


class RealBrowserJourneyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

        # Headless, a **private** profile, and the local switch on.
        #
        # A private profile is not tidiness: the product's real profile is where
        # the WhatsApp Web QR login lives, and a test that navigated it would
        # trade a logged-in account for a fixture. Headless because a test suite
        # that opens windows on somebody's desktop is not a suite anybody runs.
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        env = mock.patch.dict(
            os.environ,
            {
                session.PROFILE_VAR: str(Path(cls._tmp.name) / "profile"),
                session.HEADLESS_VAR: "true",
                session.ALLOW_LOCAL_VAR: "true",
            },
        )
        env.start()
        engine_choice = mock.patch.object(
            session, "_configured_browser_engine", return_value="chromium"
        )
        engine_choice.start()
        # Restored rather than merely unset: this file turns the local switch on
        # for its own sake, and leaving it on would quietly relax the network
        # guard for every test module imported after this one.
        cls.addClassCleanup(engine_choice.stop)
        cls.addClassCleanup(env.stop)

    def _with_engine(self, body):
        """Run ``body(engine)`` on a fresh loop, releasing the browser after."""

        async def run():
            eng = engine.PlaywrightEngine(engine.GenerationGate())
            control.set_engine(eng)
            try:
                return await body(eng)
            finally:
                control.set_engine(None)
                # Released on *this* loop, which is the only loop it may be
                # released from. Anything left running would bind the shared
                # browser to a dead loop and break the next test.
                await session.release()

        return asyncio.run(run())

    def _search_step(self, query: str):
        from agent.runtime import router

        step = task_runner._step("browser_search", "Search", {"query": query})
        return task_runner.StepContext(
            step=step,
            route=router.Route(router.BROWSER, tool="browse"),
            request=f"search for {query}",
            outputs={},
            approve=_approve,
        )

    # -- the reported failure: a search that typed and stopped ----------------

    def test_a_search_types_submits_and_lands_somewhere_real(self):
        """The exact reported bug, end to end.

        The old plan was ``open_page -> find_element -> browser_act(fill)`` and
        nothing else, so the text went in and no search ever ran. This asserts the
        search *navigated*, not merely that it typed.
        """

        async def body(eng):
            await eng.navigate(self.base)
            ctx = self._search_step("lofi")
            result = await task_runner.STEP_IMPLEMENTATIONS["browser_search"](ctx)
            self.assertEqual(
                result.outcome.status,
                "observed",
                f"search did not land: {result.outcome.detail}",
            )
            self.assertIn("/results", result.outputs["url"])
            # And the page really changed, rather than the URL being reported
            # optimistically.
            payload = await eng.observe()
            self.assertIn("results", payload["text"][:400])
            return result

        self._with_engine(body)

    def test_a_search_box_inside_a_form_is_reported_as_such(self):
        """The permission class depends on a DOM fact the collector must carry.

        ``inForm`` was missing from ``_JS_COLLECT`` entirely, so every control read
        as being outside a form and a submit button was classified ``act`` -- a
        form submission that never asked. ``browse`` was unaffected because its own
        script computed the fact separately, which is precisely how two tools end
        up disagreeing about the same button.
        """

        async def body(eng):
            from agent.tools.browser.actions import classify_key

            await eng.navigate(self.base)
            payload = await eng.observe()
            snapshot = eng.gate.stamp(payload)
            found = search.find_search_field(snapshot, payload)
            self.assertIsNotNone(found, "the fixture's search box was not found")
            # The fixture wraps its input in a real <form action="/results">.
            self.assertTrue(found.in_form, "a field inside a form was not reported as one")

            # The control facts have to survive the collector, because the
            # permission class is computed from them in Python. This is the
            # script half of the same property the assertion below pins on the
            # classifier: without `formRole` reaching Python, a search form is
            # indistinguishable from a comment box and this page asks again.
            candidate = next(
                (c for c in snapshot.candidates if c.detail.get("inForm")),
                None,
            )
            self.assertIsNotNone(candidate, "the form field was not collected at all")
            self.assertEqual(
                candidate.detail.get("formRole"),
                "search",
                "the form's own role did not survive into the candidate",
            )

            # And the end of it: a search box inside a search form is `act`, so
            # "search this page" runs without interrupting the user. It was
            # `sensitive` when Enter-in-a-form was read as a submission
            # regardless of what the form was *for*.
            self.assertEqual(
                classify_key(
                    "Enter",
                    tag=found.tag,
                    type_=candidate.detail.get("type") or "",
                    in_form=found.in_form,
                    search=True,
                ),
                "act",
                "Enter in a search form must not ask",
            )

        self._with_engine(body)

    def test_a_label_less_search_box_is_still_found(self):
        """The YouTube shape: no ``aria-label``, no ``placeholder``.

        Found only because the collector falls back to the ``name`` attribute
        after trying every label. Without that fallback the search step has
        nothing to type into on exactly the site it was written for.
        """

        async def body(eng):
            await eng.navigate(self.base)
            payload = await eng.observe()
            snapshot = eng.gate.stamp(payload)
            # `search.find_search_field`, not a private on the task runner: the
            # implementation moved so the `web_control` verb and the
            # `run_task` step share one search rather than two.
            found = search.find_search_field(snapshot, payload)
            self.assertIsNotNone(
                found, "the fixture's label-less search box was not found"
            )
            self.assertTrue(found.query.css or found.query.name)

        self._with_engine(body)

    def test_a_site_css_selector_filters_candidates_before_resolving(self):
        """A hidden duplicate selector match must not make the visible field ambiguous.

        YouTube's site mapping addresses its search input by CSS. The old engine
        ranked every page control without applying that CSS, so the search step
        failed with "more than one matches" before it could type.
        """
        async def body(eng):
            await eng.navigate(self.base)
            snapshot = eng.gate.stamp(await eng.observe())
            query = locators.build_query(css='input[name="search_query"]')
            await eng.act(
                engine.EngineAction(
                    kind="fill",
                    query=query,
                    text="your love is gone",
                    generation=snapshot.generation,
                )
            )
            page = await eng._page()
            self.assertEqual(
                await page.locator("#youtube-search").input_value(),
                "your love is gone",
            )
            self.assertEqual(
                await page.locator("#hidden-youtube-search").input_value(),
                "",
            )

        self._with_engine(body)

    def test_a_site_table_entry_is_used_when_the_page_is_a_known_site(self):
        """A known site is searched through *its own* selector.

        Asserted on the pure table rather than on a live site, because these
        selectors are documentation about somebody else's markup: they are worth
        a test that they are well-formed, not a test that pretends they cannot
        drift.
        """
        for name, url in (
            ("youtube", "https://www.youtube.com/"),
            ("google", "https://www.google.com/"),
            # Maps is not its own domain; it is a path on Google. Getting this
            # wrong means the table silently never applies to Maps.
            ("maps", "https://www.google.com/maps"),
            ("amazon", "https://www.amazon.com/"),
        ):
            with self.subTest(site=name):
                entry = sites.site_for(url)
                self.assertIsNotNone(entry, f"no table entry for {url}")
                self.assertEqual(entry.name, name)
                self.assertTrue(entry.input, f"{name} has no search field selector")
                for selector in (entry.input, entry.submit):
                    if selector:
                        self.assertTrue(
                            locators.valid_css(selector),
                            f"{name}'s selector is not one the validator accepts: {selector}",
                        )

    def test_maps_is_its_own_entry_rather_than_google(self):
        """Maps' field is ``#searchboxinput`` and Google's is ``name="q"``.

        If Maps fell through to the Google entry it would type into the wrong
        input on the single site where that would visibly do nothing.
        """
        entry = sites.site_for("https://www.google.com/maps/search?q=cafe")
        self.assertEqual(entry.name, "maps")
        self.assertIn("searchboxinput", entry.input)

    # -- the other reported failure: clicking the wrong thing ------------------

    def test_a_positional_click_hits_the_element_the_ranking_chose(self):
        """``click the second video`` must not click the first link.

        The old code rebuilt a selector and applied ``nth()`` against whatever the
        selector matched *at click time*, which is not the set the ranking saw.
        This asserts the marker approach: the element carries the reference, and
        the action is addressed by it.
        """

        async def body(eng):
            await eng.navigate(self.base)
            payload = await eng.observe()
            snapshot = eng.gate.stamp(payload)

            query = locators.build_query(role="link", position="second")
            resolution = locators.resolve_one(snapshot.candidate_list(), query)
            self.assertIsNotNone(resolution.chosen, "the second link did not resolve")
            self.assertEqual(resolution.chosen.name, "Second result")
            self.assertTrue(
                resolution.chosen.ref, "a candidate with no marker cannot be addressed"
            )

            result = await eng.act(
                engine.EngineAction(
                    kind="click",
                    query=query,
                    generation=snapshot.generation,
                    expected={"name": "Second result", "role": "link"},
                )
            )
            self.assertTrue(result["ok"])
            self.assertEqual(
                result["after"]["url"],
                f"{self.base}/watch?v=2",
                "the wrong link was activated",
            )

        self._with_engine(body)

    def test_an_action_planned_against_an_older_read_refuses(self):
        """Staleness, proved against a real page.

        A read, then another read, then an action carrying the *first*
        generation: it must refuse rather than click whatever is there now.
        """

        async def body(eng):
            await eng.navigate(self.base)
            # `observe()` reads the page; stamping is what records that a *caller*
            # has seen it, and it is the stamp that advances the generation.
            stale = eng.gate.stamp(await eng.observe()).generation
            eng.gate.stamp(await eng.observe())  # the page may have changed
            self.assertGreater(
                eng.gate.current().generation,
                stale,
                "the gate did not advance, so this test proves nothing",
            )
            with self.assertRaises(engine.EngineError):
                await eng.act(
                    engine.EngineAction(
                        kind="click",
                        query=locators.build_query(role="link"),
                        generation=stale,
                    )
                )

        self._with_engine(body)

    def test_an_action_whose_target_moved_is_refused(self):
        """The re-validation, proved against a real page.

        The action says it wants "Second result"; the page now says something
        else is there. It refuses rather than activating the wrong control.
        """

        async def body(eng):
            await eng.navigate(self.base)
            snapshot = eng.gate.stamp(await eng.observe())
            with self.assertRaises(engine.EngineError):
                await eng.act(
                    engine.EngineAction(
                        kind="click",
                        query=locators.build_query(role="link", position="second"),
                        generation=snapshot.generation,
                        expected={"name": "A control that is not on this page"},
                    )
                )

        self._with_engine(body)


if __name__ == "__main__":
    unittest.main()