"""Tabs as a *product feature*: reaching them, knowing where you are, and not
breaking what was already playing.

`test_browser_tabs.py` covers the registry mechanics. This file covers the three
things that made tabs unusable from the outside, each of which was a wiring fault
rather than a missing feature:

* **A tab request never reached the browser.** "switch to the other tab" carries
  `switch`, which is in the router's `WINDOW_VERBS`, and that rule routes to
  `manage_window` on the desktop -- a tool that arranges application windows and
  has no notion of a tab. So the control existed and was unreachable.

* **She could not tell which tab she was on.** The fix that works is not a better
  instruction to the model; it is that every read now *says* it. A model that has
  to remember to ask which tab she is on asks on the turn after she has already
  acted on the wrong one.

* **A background warm-up minimized a window the user was looking at.** Measured
  with a video playing in it: `whatsapp_page(focus=False)` against a browser that
  was already open and visible left the window MINIMIZED. A warm-up may keep its
  own window quiet. It is never allowed to hide one that was already on screen.

The window/media behaviour was measured against a real headed Chromium before any
of it was pinned here -- a synthetic `<audio>` element survives tab switching and
minimizing, so the reproducible defect was the window, not the audio.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.runtime import router
from agent.tools.browser import control as control_mod
from agent.tools.browser import engine as engine_mod
from agent.tools.browser import session as S
from agent.tools.browser import tools as browse_module
from agent.tools.browser.tools import browse
from agent.tools.tool_results import UNTRUSTED_CLOSE
from agent.tools.tools_compat import ToolError


def _payload_of(said: str) -> dict:
    """The JSON a tool result carries, with the retrieved-data wrapper removed.

    `wrap_untrusted` wraps every page-derived result as
    ``<<<LUMINE_TOOL_DATA`` / a line of prose about treating it as facts /
    the JSON / ``LUMINE_TOOL_DATA>>>``, so the string is not JSON and
    `json.loads` on it fails. Taken from the first brace to the last, which is
    the object and nothing of the wrapper around it.
    """
    body = said.split(UNTRUSTED_CLOSE, 1)[0] if UNTRUSTED_CLOSE in said else said
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        raise AssertionError(f"no JSON payload in tool result: {said[:120]!r}")
    return json.loads(body[start : end + 1])


def _route(text: str):
    return router.classify(text, facts=router.MachineFacts())


# -- reaching the browser ----------------------------------------------------


class TabRoutingTests(unittest.TestCase):
    """A tab request is a browser request, whatever else it says."""

    def test_switching_tabs_reaches_the_browser_and_not_the_desktop(self):
        # The reported failure, at the level it was decided. "switch" is a
        # WINDOW_VERB, so this used to come back as `manage_window` on the
        # desktop -- the browser tool that cannot open a tab.
        for said in (
            "switch to the other tab",
            "go back to the first tab",
            "open this in a new tab",
            "close that tab",
            "what tabs do I have open",
            "open example.com in a new tab",
        ):
            with self.subTest(said=said):
                route = _route(said)
                self.assertEqual(route.environment, router.BROWSER, route.reason)
                self.assertEqual(route.tool, "browse")
                # The verb, not just the tool: both `open` and `tab` live on
                # `browse`, and handing over only the tool leaves the model to
                # guess -- and it guesses `open`, which throws the page away.
                self.assertEqual(route.action, "tab", "the verb was left to a guess")

    def test_a_window_request_is_still_a_window_request(self):
        # The rule the tab rule sits in front of. If this moved, "minimize the
        # window" would become a tab request.
        for said in ("minimize the window", "close that window", "bring the window forward"):
            with self.subTest(said=said):
                route = _route(said)
                self.assertEqual(route.environment, router.DESKTOP, route.reason)
                self.assertEqual(route.tool, "manage_window")

    def test_a_named_application_is_still_an_application(self):
        route = _route("switch to chrome")
        self.assertEqual(route.tool, "open_app")

    def test_messaging_still_outranks_the_tab_noun(self):
        # "switch to the WhatsApp tab" names a *conversation*, and the tab behind
        # it is not what was asked about. Every other rule in this module obeys
        # the same order -- an explicit claim beats a generic noun -- and a tab
        # rule that swallowed messaging would send messages from the wrong place.
        for said in ("switch to the whatsapp tab", "message her on the other tab"):
            with self.subTest(said=said):
                self.assertEqual(_route(said).environment, router.MESSAGING)

    def test_the_verb_family_is_reported(self):
        self.assertIn("tab", router.verb_families("switch to the other tab"))
        self.assertNotIn("tab", router.verb_families("switch to the other window"))

    def test_the_route_serialises_the_verb(self):
        # It is only useful if it survives into what the model is told.
        self.assertEqual(_route("close that tab").to_dict().get("action"), "tab")


# -- the window we are not allowed to hide -----------------------------------


class BackgroundWindowRegressionTests(unittest.IsolatedAsyncioTestCase):
    """The defect, pinned so it cannot come back.

    A warm-up that hides a window the user is looking at is worse than the popup
    it was written to remove: the popup was a window appearing, this is a window
    the user was watching disappearing.
    """

    def _browser_with_a_running_one(self):
        page = _page()
        browser = S._Browser()
        browser._context = SimpleNamespace(new_page=mock.AsyncMock(return_value=page))
        browser.ensure = mock.AsyncMock()
        browser.allowed = mock.AsyncMock(return_value=(True, ""))
        browser._register(page, activate=True)
        return browser, page

    async def test_a_warm_up_never_hides_a_window_that_is_already_open(self):
        browser, _page = self._browser_with_a_running_one()
        hidden: list[int] = []
        browser._minimize_window = mock.AsyncMock(side_effect=lambda: hidden.append(1))

        await browser.whatsapp_page(focus=False)

        self.assertEqual(hidden, [], "a background warm-up hid a window that was already open")

    async def test_the_window_a_browser_was_started_hidden_is_hidden_again(self):
        # The other half, and the reason the guard is a flag rather than a
        # blanket refusal: creating a tab raises the window, so a browser *we*
        # put in the taskbar has to stay there.
        browser, _page = self._browser_with_a_running_one()
        browser._hid_window = True
        hidden: list[int] = []
        browser._minimize_window = mock.AsyncMock(side_effect=lambda: hidden.append(1))

        await browser.whatsapp_page(focus=False)

        self.assertEqual(hidden, [1])

    async def test_bringing_the_window_forward_gives_up_the_right_to_hide_it(self):
        # Otherwise the bug returns one step later: a warm-up that runs after the
        # user has been shown something minimizes it on its next tab.
        browser, _page = self._browser_with_a_running_one()
        browser._hid_window = True

        await browser._reveal(browser._page)

        self.assertFalse(browser._hid_window, "the window was still ours to hide")

    async def test_a_real_request_still_hides_nothing_and_focuses(self):
        browser, _page = self._browser_with_a_running_one()
        browser._hid_window = True
        focused: list[int] = []
        hidden: list[int] = []
        browser._focus_whatsapp = mock.AsyncMock(side_effect=lambda: focused.append(1))
        browser._minimize_window = mock.AsyncMock(side_effect=lambda: hidden.append(1))

        await browser.whatsapp_page(focus=True)

        self.assertEqual(focused, [1], "the send flow lost its focus")
        self.assertEqual(hidden, [], "a focused request hid the window")

    async def test_releasing_the_browser_gives_up_the_right_too(self):
        browser, _page = self._browser_with_a_running_one()
        browser._hid_window = True
        browser._loop = asyncio.get_running_loop()
        browser._pw = SimpleNamespace(stop=mock.AsyncMock())

        await browser.release()

        self.assertFalse(browser._hid_window)

    def test_the_window_is_told_not_to_declare_itself_occluded(self):
        # Chromium asks the window server whether this window is completely
        # covered, and answers yes for a window behind the Lumine app. An occluded
        # window gets throttling the timer flags do not cover, which is where
        # media left playing in a background tab stalls.
        flags = S._window_args()
        self.assertIn("--disable-features=CalculateNativeWinOcclusion", flags)
        self.assertIn("--disable-backgrounding-occluded-windows", flags)


# -- knowing where she is ----------------------------------------------------


class TabAwarenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_read_says_which_tab_it_read_and_what_else_is_open(self):
        one, two = _page(), _page()
        browser = S._Browser()
        browser._register(one, activate=True)
        browser._register(two, activate=True)

        context = await browser.tab_context()

        self.assertEqual(context["tab"], browser._active)
        self.assertEqual(context["tabCount"], 2)
        self.assertEqual([row["tab"] for row in context["otherTabs"]], [one_tab(browser, one)])

    async def test_the_other_tabs_are_capped(self):
        # A read is re-billed on every later turn for the rest of the
        # conversation, so the list it carries cannot grow without limit.
        browser = S._Browser()
        for _ in range(S.MAX_WORK_TABS + 3):
            browser._register(_page(), activate=True)

        context = await browser.tab_context()

        self.assertEqual(context["tabCount"], S.MAX_WORK_TABS + 3)
        self.assertLessEqual(len(context["otherTabs"]), S.MAX_REPORTED_TABS)

    async def test_an_observe_carries_the_tab_context(self):
        one, two = _page("https://one.example/"), _page("https://two.example/")
        browser = S._Browser()
        browser._register(one, activate=True)
        browser._register(two, activate=True)
        browser.ensure = mock.AsyncMock(return_value=two)
        browser._remember_video_id = lambda _u: None

        result = await browser.observe_page()

        self.assertEqual(result["tab"], browser._active)
        self.assertEqual(result["tabCount"], 2)


class FakePage:
    """A page that answers what the registry and the window code ask of it."""

    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.closed = False
        self.fronted = 0
        #: What `evaluate` hands back. The scripts are literal strings evaluated
        #: in the page, so a fake returns the shape they would have produced.
        self.script_result: dict = {"title": "A page", "url": url, "text": "", "controls": []}

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, _script, *_args):
        return dict(self.script_result)

    async def title(self) -> str:
        return "A page"

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    async def close(self) -> None:
        self.closed = True

    async def bring_to_front(self) -> None:
        self.fronted += 1


def _page(url: str = "about:blank") -> FakePage:
    return FakePage(url)


def one_tab(browser, page):
    for tab in browser._tabs:
        if tab.page is page:
            return tab.key
    raise AssertionError("page is not registered")


# -- the tool surface --------------------------------------------------------


class BrowseAwarenessTests(unittest.TestCase):
    def test_an_observe_ends_with_which_tab_it_was(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module,
            "observe_page",
            new_callable=mock.AsyncMock,
            return_value={
                "title": "Results",
                "url": "https://example.com/",
                "text": "hi",
                "controls": [],
                "tab": 2,
                "tabCount": 3,
                "otherTabs": [{"tab": 1, "title": "Search"}, {"tab": 3, "title": "Video"}],
            },
        ):
            said = asyncio.run(browse(SimpleNamespace(), "observe"))

        # Names the *numbers*, because a list of titles without them is a fact
        # the model cannot act on -- the `tab` verb takes a number.
        self.assertIn("tab 2 of 3", said)
        self.assertIn("tab 1: Search", said)
        self.assertIn("tab 3: Video", said)

    def test_an_open_says_when_it_threw_a_page_away(self):
        # The step that actually loses the user's page. `open` reuses the tab in
        # use, so with other tabs open the model has to be told it replaced one --
        # and that `tab new` is the alternative.
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module,
            "open_page",
            new_callable=mock.AsyncMock,
            return_value={"title": "Example", "url": "https://example.com/"},
        ), mock.patch.object(
            browse_module,
            "tab_context",
            new_callable=mock.AsyncMock,
            return_value={"tab": 1, "tabCount": 3, "otherTabs": []},
        ):
            said = asyncio.run(browse(SimpleNamespace(), "open", url="https://example.com/"))

        self.assertIn("replaced", said)
        self.assertIn("tab new", said)

    def test_an_open_with_one_tab_says_nothing_about_replacing(self):
        # Noise on the common case is how a useful note stops being read.
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module,
            "open_page",
            new_callable=mock.AsyncMock,
            return_value={"title": "Example", "url": "https://example.com/"},
        ), mock.patch.object(
            browse_module,
            "tab_context",
            new_callable=mock.AsyncMock,
            return_value={"tab": 1, "tabCount": 1, "otherTabs": []},
        ):
            said = asyncio.run(browse(SimpleNamespace(), "open", url="https://example.com/"))

        self.assertNotIn("replaced", said)

    def test_the_docstring_says_open_reuses_and_tab_new_adds(self):
        # The model reads this. `actions` and the docstring are the whole of what
        # she is told, and the failure was her picking the destructive one.
        self.assertIn("tab new", browse.__doc__ or "")


class WebControlParityTests(unittest.IsolatedAsyncioTestCase):
    """Both browser tools drive one browser, so both offer the same control.

    A model that reaches for `web_control` and finds no way to open a tab does
    something worse: it calls `open`, which navigates the tab in use and throws
    away whatever the user had there.
    """

    def setUp(self):
        self.engine = mock.Mock()
        self.engine.gate = None
        self.engine.tab_context = mock.AsyncMock(
            return_value={"tab": 1, "tabCount": 2, "otherTabs": []}
        )
        control_mod.set_engine(self.engine)
        self.addCleanup(control_mod.set_engine, None)
        patcher = mock.patch.object(
            control_mod, "require_approval", new_callable=mock.AsyncMock
        )
        self.approval = patcher.start()
        self.addCleanup(patcher.stop)

    async def _call(self, action: str, **kwargs):
        return await control_mod.web_control(SimpleNamespace(), action, **kwargs)

    async def test_every_tab_operation_is_reachable_here_too(self):
        self.engine.tab_list = mock.AsyncMock(
            return_value={
                "tabs": [
                    {"tab": 1, "title": "Search", "url": "https://a/", "active": False},
                    {"tab": 2, "title": "Video", "url": "https://b/", "active": True},
                ],
                "active": 2,
                "count": 2,
            }
        )
        self.engine.new_tab = mock.AsyncMock(
            return_value={"tab": 3, "title": "New", "url": "https://c/", "count": 3}
        )
        self.engine.switch_tab = mock.AsyncMock(
            return_value={"tab": 1, "title": "Search", "url": "https://a/", "count": 2}
        )
        self.engine.close_tab = mock.AsyncMock(
            return_value={
                "closed": 2,
                "count": 1,
                "active": 1,
                "tabs": [{"tab": 1, "title": "Search", "url": "https://a/", "active": True}],
            }
        )

        self.assertIn("tab 1", await self._call("tab"))
        self.assertIn("Opened tab 3", await self._call("tab", text="new", url="https://c/"))
        self.engine.new_tab.assert_awaited_once_with("https://c/")
        self.assertIn("On tab 1 now", await self._call("tab", text="1"))
        self.engine.switch_tab.assert_awaited_once_with("1")
        self.assertIn("Closed tab 2", await self._call("tab", text="close 2"))
        self.engine.close_tab.assert_awaited_once_with("2")

    async def test_the_operations_are_charged_by_what_they_can_lose(self):
        # The same table `browse` uses, imported rather than copied -- so a card
        # for "switch to tab 2" cannot be priced differently depending on which
        # browser tool she reached for.
        self.engine.tab_list = mock.AsyncMock(return_value={"tabs": [], "active": None, "count": 0})
        self.engine.new_tab = mock.AsyncMock(return_value={"tab": 1, "title": "", "url": "", "count": 1})
        self.engine.switch_tab = mock.AsyncMock(return_value={"tab": 2, "title": "", "url": "", "count": 2})
        self.engine.close_tab = mock.AsyncMock(return_value={"closed": 1, "count": 1, "active": 2, "tabs": []})

        for text, want in (("", "read"), ("new", "read"), ("2", "read"), ("close 1", "act")):
            with self.subTest(text=text):
                await self._call("tab", text=text)
                self.assertEqual(self.approval.call_args.kwargs["action_class"], want)
                self.assertEqual(self.approval.call_args.kwargs["action"], "tab")

    async def test_a_close_that_named_no_tab_it_can_resolve_is_refused(self):
        # The parser's one refusal. "1.5" and "one" aim at a tab and resolve to
        # nothing, and reading them as "the tab in use" would close a page nobody
        # named -- which is the reported failure.
        with self.assertRaises(ToolError):
            await self._call("tab", text="close 1.5")

    def test_the_page_tab_strip_is_still_reachable_under_its_old_name(self):
        # Renamed to remove a collision, not to remove a capability. `tabs` still
        # means what it always meant, so nothing that exists today breaks.
        self.assertNotIn("tabs", control_mod.ACTIONS)
        self.assertEqual(control_mod.VERB_ALIASES["tabs"], "pagetabs")
        self.assertIn("pagetabs", control_mod.ACTIONS)

    async def test_the_old_spelling_reads_the_page_strip_not_the_browser_tabs(self):
        self.engine.tabs = mock.AsyncMock(
            return_value={"tabs": [{"name": "Billing", "selected": True}], "url": "https://a/"}
        )
        self.engine.tab_list = mock.AsyncMock(return_value={"tabs": [], "active": None, "count": 0})

        result = _payload_of(await self._call("tabs"))

        self.engine.tabs.assert_awaited_once()
        self.engine.tab_list.assert_not_awaited()
        self.assertEqual(result["tabs"], [{"name": "Billing", "selected": True}])

    async def test_a_read_carries_which_tab_it_was_of(self):
        self.engine.observe = mock.AsyncMock(
            return_value={
                "url": "https://a/",
                "title": "A",
                "text": "hi",
                "candidates": [],
                "ready": "complete",
            }
        )

        result = _payload_of(await self._call("read"))

        self.assertEqual(result["tab"], 1)
        self.assertEqual(result["tabCount"], 2)

    async def test_awareness_never_fails_a_read(self):
        # It is a nicety. A browser that cannot say where it is must still be
        # readable, or awareness becomes an outage.
        self.engine.observe = mock.AsyncMock(
            return_value={"url": "https://a/", "title": "A", "text": "hi", "candidates": []}
        )
        self.engine.tab_context = mock.AsyncMock(side_effect=RuntimeError("gone"))

        result = _payload_of(await self._call("read"))

        self.assertEqual(result["url"], "https://a/")
        self.assertNotIn("tab", result)

    def test_the_engine_exposes_the_tab_verbs(self):
        for name in ("tab_list", "tab_context", "new_tab", "switch_tab", "close_tab"):
            self.assertTrue(hasattr(engine_mod.PlaywrightEngine, name), name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()