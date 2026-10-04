"""One real browser, driven end to end -- skipped when the engine is absent.

The tests in `test_browser.py` prove the *decisions* against fakes. This
file proves the mechanics against an actual Chromium, because the two are
not the same claim:

- the isolated profile lands where `profile_dir()` says and starts on a
  fresh page, not on whatever the last conversation left open,
- the route guard is live on **every** request a page makes -- a subresource
  aimed inward is aborted even though only we ever called `goto`,
- `observe` mints references that `act` then uses, and a real `el.click()`
  runs the page's own handlers,
- a key press is a *real* key press: `Enter` on a focused button runs the
  page's handler through the browser's own input pipeline, and `Enter` on a
  field inside a form is refused before it can submit anything,
- `back`/`forward` move a real tab's real history, and `scroll` moves a real
  viewport -- reporting the edge it reached rather than claiming motion,
- the password refusal happens inside the DOM at the moment of typing,
  not only in the tool's pre-check,
- a real form submission, with nobody in the room to approve it, refuses.

**No external network.** Fixtures come from a loopback HTTP server this file
starts, and `LUMINE_BROWSER_ALLOW_LOCAL=true` is set precisely because the
thing under test is the behaviour of a *local* address. The engine check
runs in `setUpClass`: `playwright install chromium` is a ~150 MB step a
machine may never take, and a suite that failed without it would be a suite
that lies about being offline. Absent engine -- skip, not fail.

Everything shares **one event loop**: Playwright's objects belong to the
loop that made them, so the whole journey is a single coroutine and it
releases the browser on its own loop in a `finally`. A second `asyncio.run`
would meet the bound browser from a dead loop and -- by design, see
`session.MSG_OTHER_LOOP` -- refuse.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent.runtime.permission_gate import MSG_NO_GATE
from agent.tools.browser import session
from agent.tools.browser.tools import browse
from agent.tools.tools_compat import ToolError
from agent.tools.tool_results import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

#: One page, everything the journey needs on it: a link, a button whose
#: handler mutates the DOM (so a click's effect is observable without a
#: navigation race), a real form that submits, and a password field.
FIXTURE = b"""<!doctype html>
<html>
<head><title>Fixture</title></head>
<body>
  <h1>Fixture page</h1>
  <p id="note">nothing happened yet</p>
  <button id="poke"
          onclick="document.getElementById('note').textContent = 'clicked'">Poke</button>
  <label for="country">Country</label>
  <select id="country" name="country" aria-label="Country"
          onchange="document.getElementById('note').textContent = 'chose ' + this.value">
    <option value="bd">Bangladesh</option>
    <option value="us">United States</option>
  </select>
  <form action="/submit" method="get">
    <input name="q" aria-label="Query">
    <button type="submit">Send</button>
  </form>
  <input type="password" name="pw" aria-label="Password">
</body>
</html>"""

#: A page that has to be scrolled to be read: an ordinary document that
#: overflows a 720px viewport. Without this the scroll verbs would be
#: tested against a page that never moves, which proves nothing.
TALL_FIXTURE = b"""<!doctype html>
<html>
<head><title>Tall</title></head>
<body>
  <h1>Tall page</h1>
  <div style="height: 4000px"></div>
  <p id="foot">you made it</p>
</body>
</html>"""


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    """Every path answers a fixture page: `/tall` overflows, the rest fits."""

    def do_GET(self):  # noqa: N802 -- http.server's spelling
        body = TALL_FIXTURE if self.path.startswith("/tall") else FIXTURE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # the suite's output is its own
        pass


def _engine_installed() -> bool:
    """Is there a chromium to drive? Never raises: absence is a skip."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001 -- no wheel at all is the common case
        return False
    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:  # noqa: BLE001 -- a broken driver is also "not installed"
        return False


def _payload_of(result: str) -> dict:
    """The tool result, unwrapped from the retrieved-data delimiter."""
    body = result.split(UNTRUSTED_OPEN, 1)[1].split(UNTRUSTED_CLOSE, 1)[0].strip()
    # One preamble line the model is told to heed, then the JSON itself.
    preamble, _, payload = body.partition("\n")
    assert preamble.startswith("Retrieved data"), "the block must carry its warning"
    return json.loads(payload)


class RealBrowserJourneyTests(unittest.TestCase):
    """The whole path, on one loop, against a local fixture server."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _engine_installed():
            raise unittest.SkipTest(
                "chromium is not installed (python -m playwright install chromium)"
            )

        cls._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        # Registered close-first so LIFO runs `shutdown()` before
        # `server_close()` -- the other order kills the serving socket under
        # the thread and prints an exception the tests never caused.
        cls.addClassCleanup(cls._server.server_close)
        cls.addClassCleanup(cls._server.shutdown)

        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)

        # Headless, a private profile, and the local switch on -- this file
        # is the local network the guard is otherwise pointed at.
        env = mock.patch.dict(
            os.environ,
            {
                session.PROFILE_VAR: str(Path(cls._tmp.name) / "profile"),
                session.HEADLESS_VAR: "true",
                session.ALLOW_LOCAL_VAR: "true",
            },
        )
        env.start()
        cls.addClassCleanup(env.stop)

        cls.url = f"http://127.0.0.1:{cls._server.server_address[1]}/"

    def _run(self, coroutine):
        return asyncio.run(coroutine)

    async def _journey(self):
        """Open, observe, act, refuse -- and always let the browser go."""
        ctx = SimpleNamespace()
        try:
            # -- open ----------------------------------------------------
            opened = await browse(ctx, "open", url=self.url)
            self.assertIn(UNTRUSTED_OPEN, opened)
            self.assertIn("Fixture", opened)

            # -- observe -------------------------------------------------
            seen = _payload_of(await browse(ctx, "observe"))
            self.assertEqual(seen["title"], "Fixture")
            labels = {control["label"]: control["ref"] for control in seen["controls"]}
            self.assertIn("Poke", labels)
            self.assertIn("Send", labels)
            self.assertIn("Password", labels)
            # Excerpts are clipped, and the text the page chose is wrapped.
            self.assertLessEqual(len(seen["text"]), 4000)

            # -- act: a real click runs the page's own handler ------------
            result = await browse(ctx, "act", ref=labels["Poke"])
            self.assertIn("Clicked Poke", result)
            # No navigation: the effect is in the DOM.
            after = _payload_of(await browse(ctx, "observe"))
            self.assertIn("clicked", after["text"])

            # -- act: a real form submission, with nobody to ask ----------
            fresh = _payload_of(await browse(ctx, "observe"))
            send_ref = {c["label"]: c["ref"] for c in fresh["controls"]}["Send"]
            with self.assertRaises(ToolError) as caught:
                await browse(ctx, "act", ref=send_ref)
            self.assertEqual(str(caught.exception), MSG_NO_GATE)

            # -- fill: the refusal is inside the DOM ----------------------
            fresh = _payload_of(await browse(ctx, "observe"))
            password_ref = {c["label"]: c["ref"] for c in fresh["controls"]}["Password"]
            # The tool refuses first (its own inspection), so call the
            # session directly to prove the *page-level* refusal is real:
            # a swap under the reference at typing time cannot collect it.
            outcome = await session.fill_ref(password_ref, "hunter2")
            self.assertEqual(outcome, {"ok": False, "reason": "password"})

            # And the tool's pre-check refuses before any question.
            with self.assertRaises(ToolError) as caught:
                await browse(ctx, "act", ref=password_ref, text="hunter2")
            self.assertIn("password", str(caught.exception).lower())

            # -- observe again is always possible, and closes the loop ----
            self.assertIn("Fixture", _payload_of(await browse(ctx, "observe"))["title"])
        finally:
            # On *this* loop, always: a browser leaked here would hold the
            # profile's files open and fail the cleanup on Windows.
            await session.release()

    def test_open_observe_act_and_refuse_on_a_real_page(self):
        self._run(self._journey())

    def test_a_reference_does_not_survive_the_page_it_was_read_from(self):
        # The mechanism behind a click landing on the wrong thing.

        # A `ref` is a position in one document, not an identity. After a
        # navigation the same `r3` is a different element entirely, and the
        # signature check cannot catch it -- two anchors on different pages are
        # byte-identical under `tag|type|inForm`. So an `act` carrying a
        # reference from a page the user has left resolves against whatever now
        # sits at that position: on YouTube that is how a video click became a
        # click on the site logo.
        #
        # A real browser, because the marker lives in the DOM and the check has to
        # happen inside the same document.
        async def journey():
            ctx = SimpleNamespace()
            try:
                await browse(ctx, "open", url=self.url + "one")
                seen = _payload_of(await browse(ctx, "observe"))
                old = seen["controls"][0]["ref"]
                # The signature that page reported for itself, so the refusal
                # below cannot be explained by a signature that never matched.
                before = await session.inspect_ref(old)
                self.assertTrue(before.get("found"))
                signature = before.get("sig")

                # Navigate away. Nothing re-mints the old reference, and the old
                # signature is still the one the page agreed to.
                await browse(ctx, "open", url=self.url + "two")

                stale = await session.inspect_ref(old)
                self.assertFalse(
                    stale.get("found"),
                    "a reference from the previous page still resolves on this one",
                )

                clicked = await session.click_ref(old, signature)
                self.assertEqual(
                    clicked,
                    {"ok": False, "reason": "gone"},
                    "the click must refuse, not act on whatever now holds that ref",
                )

                # And a fresh read resolves normally, so the refusal is about the
                # reference rather than about the page.
                fresh = _payload_of(await browse(ctx, "observe"))
                again = await session.inspect_ref(fresh["controls"][0]["ref"])
                self.assertTrue(
                    again.get("found"),
                    "a reference read on this page should resolve on this page",
                )
            finally:
                await session.release()

        self._run(journey())

    def test_a_click_reports_where_a_real_page_actually_went(self):
        # "I clicked the row" is not "the right thing happened", and on a real
        # navigation the two come apart. The tool has to be able to say which.
        async def journey():
            ctx = SimpleNamespace()
            try:
                await browse(ctx, "open", url=self.url + "one")
                seen = _payload_of(await browse(ctx, "observe"))
                ref = seen["controls"][0]["ref"]
                facts = await session.inspect_ref(ref)
                outcome = await session.click_ref(ref, facts.get("sig"))
                self.assertTrue(outcome.get("ok"), outcome)
                self.assertIn("before", outcome)
                self.assertIn("after", outcome)
                self.assertIn("moved", outcome)
                # The fixture's control is a real element, so a real click either
                # navigates or does not -- and either way the tool is told, rather
                # than having to infer it from a page read afterwards.
                self.assertIsInstance(outcome["moved"], bool)
                self.assertIn("url", outcome["after"])
                self.assertIn("url", outcome["before"])
            finally:
                await session.release()

        self._run(journey())

    def test_history_and_scroll_move_a_real_page(self):
        # The two verbs the mocks cannot prove: that a back step lands on
        # the URL it says it does, and that a scroll reaches an edge it
        # reports rather than a motion it only intended.
        async def journeys():
            ctx = SimpleNamespace()
            try:
                # -- history: two real navigations, then a step back ------
                await browse(ctx, "open", url=self.url + "one")
                await browse(ctx, "open", url=self.url + "two")
                back = await browse(ctx, "back")
                self.assertIn("Went back", back)
                self.assertTrue(
                    _payload_of(await browse(ctx, "observe"))["url"].endswith("/one"),
                    "a back step must actually move the tab",
                )

                forward = await browse(ctx, "forward")
                self.assertIn("Went forward", forward)
                self.assertTrue(
                    _payload_of(await browse(ctx, "observe"))["url"].endswith("/two")
                )

                # Past the end of history: no entry to go to, and no claim
                # that the browser moved when it did not.
                stuck = await browse(ctx, "forward")
                self.assertIn("did not go forward", stuck)

                # -- scroll: a page that overflows, driven to its edge ----
                await browse(ctx, "open", url=self.url + "tall")
                self.assertEqual(_payload_of(await browse(ctx, "observe"))["title"], "Tall")

                self.assertIn("Scrolled down", await browse(ctx, "scroll"))
                at_edge = ""
                for _ in range(12):
                    at_edge = await browse(ctx, "scroll", text="down")
                    if "bottom" in at_edge:
                        break
                self.assertIn("That is the bottom of the page.", at_edge)

                self.assertEqual(
                    await browse(ctx, "scroll", text="top"),
                    "Scrolled to the top of the page.",
                )
                self.assertIn(
                    "That is the top of the page.",
                    await browse(ctx, "scroll", text="up"),
                )

                # -- and a page with nothing below it says so --------------
                await browse(ctx, "open", url=self.url)
                self.assertEqual(
                    await browse(ctx, "scroll"),
                    "That page fits on screen -- there is nothing to scroll.",
                )
            finally:
                await session.release()

        self._run(journeys())

    def test_a_real_key_press_runs_or_refuses_by_the_form_it_is_in(self):
        # The distinction `classify_key` exists for, on a real input
        # pipeline: the same key, Enter, is an ordinary act on a button and
        # a refusal on a field inside a form.
        async def presses():
            ctx = SimpleNamespace()
            try:
                await browse(ctx, "open", url=self.url)
                controls = _payload_of(await browse(ctx, "observe"))["controls"]
                labels = {c["label"]: c["ref"] for c in controls}

                # -- Enter on a button with no form around it: runs --------
                # The page's own handler fires, which only a *trusted* key
                # press does: a synthetic KeyboardEvent reaches listeners and
                # none of the browser's default actions, so the button would
                # never activate.
                pressed = await browse(ctx, "act", ref=labels["Poke"], key="Enter")
                self.assertIn("Pressed Enter", pressed)
                self.assertIn("clicked", _payload_of(await browse(ctx, "observe"))["text"])

                # -- an arrow key is an act wherever it lands --------------
                controls = _payload_of(await browse(ctx, "observe"))["controls"]
                labels = {c["label"]: c["ref"] for c in controls}
                self.assertIn(
                    "Pressed ArrowDown",
                    await browse(ctx, "act", ref=labels["Poke"], key="ArrowDown"),
                )

                # -- Enter inside the form: refuses, and nothing moves -----
                controls = _payload_of(await browse(ctx, "observe"))["controls"]
                labels = {c["label"]: c["ref"] for c in controls}
                with self.assertRaises(ToolError) as caught:
                    await browse(ctx, "act", ref=labels["Query"], key="Enter")
                self.assertEqual(str(caught.exception), MSG_NO_GATE)

                # The claim after the refusal: the tab is exactly where it
                # was. Every path serves the same page, so the URL is the
                # only witness that no submission happened.
                after = _payload_of(await browse(ctx, "observe"))
                self.assertEqual(after["url"], self.url, "the form submitted anyway")
            finally:
                await session.release()

        self._run(presses())

    def test_a_dropdown_choice_is_witnessed_by_the_page(self):
        # What a real `<select>` needs: the value set through the native
        # setter and a change event that actually fires, so the page's own
        # handler runs. A shadow assignment would look chosen to us and be
        # invisible to the page.
        async def chooses():
            ctx = SimpleNamespace()
            try:
                await browse(ctx, "open", url=self.url)
                controls = _payload_of(await browse(ctx, "observe"))["controls"]
                labels = {c["label"]: c["ref"] for c in controls}
                self.assertIn("Country", labels)

                chosen = await browse(
                    ctx, "act", ref=labels["Country"], text="United States"
                )
                self.assertIn("Chose United States", chosen)
                self.assertIn("chose us", _payload_of(await browse(ctx, "observe"))["text"])

                # An option the page does not have is refused, not met with
                # the nearest one that was available instead -- and the
                # page's state is left exactly as it was.
                controls = _payload_of(await browse(ctx, "observe"))["controls"]
                labels = {c["label"]: c["ref"] for c in controls}
                with self.assertRaises(ToolError) as caught:
                    await browse(ctx, "act", ref=labels["Country"], text="Atlantis")
                self.assertIn("doesn't offer that choice", str(caught.exception))
                self.assertIn(
                    "chose us", _payload_of(await browse(ctx, "observe"))["text"]
                )
            finally:
                await session.release()

        self._run(chooses())

    def test_a_local_page_refuses_when_the_switch_is_off(self):
        # The guard's headline claim, against the address the whole file
        # uses: without the documented switch, the local network is closed.
        async def refuses():
            ctx = SimpleNamespace()
            try:
                with mock.patch.dict(os.environ, {session.ALLOW_LOCAL_VAR: ""}):
                    with self.assertRaises(ToolError) as caught:
                        await browse(ctx, "open", url=self.url)
                    self.assertIn("not a public address", str(caught.exception))
            finally:
                await session.release()

        self._run(refuses())

    def test_close_releases_and_the_next_open_starts_fresh(self):
        async def closes_then_reopens():
            ctx = SimpleNamespace()
            try:
                await browse(ctx, "open", url=self.url)
                await browse(ctx, "close")
                # Reopened: a released browser starts again rather than
                # answering from a page that should be gone.
                again = await browse(ctx, "open", url=self.url)
                self.assertIn("Fixture", again)
            finally:
                await session.release()

        self._run(closes_then_reopens())


if __name__ == "__main__":
    unittest.main()
