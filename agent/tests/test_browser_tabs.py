"""More than one tab, and a tab that keeps what you read off it.

Two things are being pinned here, and they are the same change seen from the two
ends that made it worth making.

**The browser has tabs.** The old rule for a page the browser opened was to
adopt it and *close* the previous one, so a popup was a replacement rather than a
second page. That made "open this in a new tab" impossible, made comparing two
pages impossible, and made "keep the results while I follow the link" a memory
exercise. Every page the browser opens is now a tab, listed, named, and closable
by the model.

**A tab keeps its references while another tab is in use.** This is the half
that is easy to get wrong and impossible to notice. References are stamped into
the document that minted them (`data-lumine-gen`) and every action compares
against that stamp. With one counter for the whole browser, opening a second tab
moves the counter, so the *first* tab's perfectly good references suddenly read
as stale and every action on it is refused -- the model is told to observe a page
it just observed. A counter per tab fixes it without weakening anything: the DOM
still does the real work, so a navigation, a back, or a single-page app replacing
its body invalidate exactly as before.

Everything runs against fakes: no test here starts a browser or opens a window.
The two properties that genuinely need a browser -- that a tab's references
survive a round trip through another tab, and that a popup becomes a tab rather
than a replacement -- were measured against a real Chromium before being pinned
here, and the numbers this module encodes came from that run.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent.runtime import audit_log
from agent.tools.browser import actions, session
from agent.tools.browser import tools as browse_module
from agent.tools.browser.tools import browse
from agent.tools.tools_compat import ToolError


def run_browse(action: str, **kwargs) -> str:
    """Drive the tool synchronously against a session-less context."""
    return asyncio.run(browse(SimpleNamespace(), action, **kwargs))


class FakePage:
    """A page that answers the two questions the registry asks of it.

    ``redirect_to`` is what a `goto` ends up at instead of what it was asked for,
    which is the only way to exercise the second look at where a navigation
    actually landed -- the case the pre-flight check cannot see.
    """

    def __init__(self, url: str = "about:blank", *, redirect_to: str = "") -> None:
        self.url = url
        self.closed = False
        self.fronted = 0
        self.navigations: list[str] = []
        self.redirect_to = redirect_to

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def bring_to_front(self) -> None:
        self.fronted += 1

    async def title(self) -> str:
        return "A page"

    async def goto(self, url: str, **_kwargs) -> None:
        self.navigations.append(url)
        self.url = self.redirect_to or url


def _browser_with(*pages: FakePage) -> session._Browser:
    """A `_Browser` holding ``pages`` as its tabs, with no Chromium anywhere.

    ``_loop`` is set because a real browser always has one and `_drop` uses it to
    schedule the page close -- without it a closed tab's page would silently stay
    open in the window, which is the half of the behaviour worth asserting.
    """
    browser = session._Browser()
    try:
        browser._loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - called outside a loop
        browser._loop = None
    for page in pages:
        browser._register(page, activate=True)
    return browser


# -- the request, parsed ----------------------------------------------------


class TabRequestTests(unittest.TestCase):
    """Every accepted spelling, and the one refusal. No browser involved.

    The speech-facing cases live in `test_browser_tab_speech.py`, which is where
    the measured list of what people actually say is. This is the shape of the
    result.
    """

    def test_saying_nothing_asks_which_tabs_are_open(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                self.assertEqual(actions.parse_tab_request(value), ("list", ""))

    def test_new_is_a_new_tab_and_may_carry_an_address(self):
        self.assertEqual(actions.parse_tab_request("new"), ("new", ""))
        self.assertEqual(
            actions.parse_tab_request("new https://example.com/"),
            ("new", "https://example.com/"),
        )

    def test_a_bare_number_is_a_switch(self):
        # The request that carries a target needs no word at all, which matters
        # on a voice turn: "tab three" is something a person says.
        self.assertEqual(actions.parse_tab_request("3"), ("switch", "3"))
        self.assertEqual(actions.parse_tab_request("switch 3"), ("switch", "3"))
        self.assertEqual(actions.parse_tab_request("close two"), ("close", "2"))

    def test_a_page_title_or_site_name_switches_without_a_number(self):
        for said, want in (
            ("switch to the YouTube tab", ("switch", "title:YouTube")),
            ("could you switch to the YouTube tab", ("switch", "title:YouTube")),
            ("please close the Music tab", ("close", "title:Music")),
            ("go to the Spotify tab", ("switch", "title:Spotify")),
            ("open the Gmail tab", ("switch", "title:Gmail")),
            ("close the Music tab", ("close", "title:Music")),
            ("tab named YouTube", ("switch", "title:YouTube")),
        ):
            with self.subTest(said=said):
                self.assertEqual(actions.parse_tab_request(said), want)

    def test_close_takes_an_optional_target(self):
        self.assertEqual(actions.parse_tab_request("close"), ("close", ""))
        self.assertEqual(actions.parse_tab_request("close 2"), ("close", "2"))

    def test_speech_is_normalized_rather_than_required(self):
        for said, want in (("NEW", ("new", "")), ("  Close  4 ", ("close", "4")), ("Tabs", ("list", ""))):
            with self.subTest(said=said):
                self.assertEqual(actions.parse_tab_request(said), want)

    def test_a_close_that_named_no_tab_it_can_resolve_is_refused(self):
        # The parser's only refusal, and it is deliberately narrow. "1.5" and
        # "one" are attempts to name a tab that resolve to nothing, and reading
        # them as "the tab in use" would close a page nobody mentioned -- which is
        # the reported failure, not a hypothetical one.
        for value in ("close 1.5", "close one", "close tab one"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    actions.parse_tab_request(value)

    def test_an_unrecognised_sentence_lists_the_tabs_rather_than_refusing(self):
        # The deliberate change from the first version, which refused everything
        # that was not an exact word. A refusal here is not a dead end: the model
        # retries, and the retry is where a second approval card comes from.
        for value in ("sideways", "?", "list 2", "tab stuff"):
            with self.subTest(value=value):
                self.assertEqual(actions.parse_tab_request(value)[0], "list")

    def test_next_and_previous_are_directions_not_tab_numbers(self):
        # Which is why they are "+1"/"-1" and why "last" is its own marker: "the
        # last tab" is a specific tab, "the previous one" is a direction.
        self.assertEqual(actions.parse_tab_request("next tab"), ("switch", "+1"))
        self.assertEqual(actions.parse_tab_request("previous tab"), ("switch", "-1"))
        self.assertEqual(actions.parse_tab_request("delete the last tab"), ("close", "last"))

    def test_closing_the_others_is_its_own_operation(self):
        self.assertEqual(
            actions.parse_tab_request("close the other tabs"), ("close_others", "")
        )


# -- the registry ------------------------------------------------------------


class TabRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_unique_site_name_switches_to_its_tab(self):
        youtube = FakePage("https://www.youtube.com/")
        youtube.title = mock.AsyncMock(return_value="YouTube")
        other = FakePage("https://example.com/")
        other.title = mock.AsyncMock(return_value="Example")
        browser = _browser_with(youtube, other)

        result = await browser.switch_tab("title:youtube")

        self.assertEqual(result["url"], "https://www.youtube.com/")
        self.assertEqual(browser._page, youtube)

    async def test_an_ambiguous_site_name_asks_for_a_title_not_a_number(self):
        first = FakePage("https://www.youtube.com/watch?v=one")
        first.title = mock.AsyncMock(return_value="First video - YouTube")
        second = FakePage("https://www.youtube.com/watch?v=two")
        second.title = mock.AsyncMock(return_value="Second video - YouTube")
        browser = _browser_with(first, second)

        with self.assertRaisesRegex(ToolError, "page title or site"):
            await browser._resolve_target("title:youtube")

    async def test_a_missing_tab_name_suggests_a_page_title_or_site(self):
        browser = _browser_with(FakePage("https://example.com/"))

        with self.assertRaisesRegex(ToolError, "name a page title or site"):
            await browser._resolve_target("title:spotify")

    async def test_joining_does_not_navigate_an_existing_page_to_blank(self):
        existing = FakePage("https://www.youtube.com/")
        new_page = FakePage()
        context = SimpleNamespace(
            pages=[existing],
            new_page=mock.AsyncMock(return_value=new_page),
        )

        selected = await session._joined_start_page(context)

        self.assertIs(selected, new_page)
        self.assertEqual(existing.url, "https://www.youtube.com/")
        self.assertEqual(existing.navigations, [])
        context.new_page.assert_awaited_once_with()

    async def test_joining_reuses_an_existing_blank_tab(self):
        existing = FakePage()
        context = SimpleNamespace(
            pages=[FakePage("https://www.youtube.com/"), existing],
            new_page=mock.AsyncMock(),
        )

        selected = await session._joined_start_page(context)

        self.assertIs(selected, existing)
        context.new_page.assert_not_awaited()

    async def test_a_second_tab_gets_its_own_number_and_its_own_document(self):
        browser = _browser_with(FakePage("https://one.example/"))
        first = browser._active
        first_gen = browser._active_generation()

        browser.ensure = mock.AsyncMock()
        browser.allowed = mock.AsyncMock(return_value=(True, ""))
        browser._context = SimpleNamespace(new_page=mock.AsyncMock(return_value=FakePage()))
        made = await browser.new_tab("https://two.example/")

        self.assertNotEqual(made["tab"], first)
        self.assertEqual(browser._active, made["tab"])
        self.assertEqual(len(browser._tabs), 2)
        # Distinct, and both still live: the point is that the numbers are
        # per tab, not that either tab forgot its own.
        self.assertNotEqual(browser._active_generation(), first_gen)
        self.assertEqual(
            browser._tab_by_key(first).generation,
            first_gen,
            "opening another tab moved the first tab's document",
        )

    async def test_switching_tabs_does_not_invalidate_the_tab_left_behind(self):
        # The property multi-tab exists for. Read tab 1, go to tab 2, come back,
        # act on what tab 1 showed. A counter that moved on the switch would
        # refuse every one of those acts.
        one, two = FakePage("https://one.example/"), FakePage("https://two.example/")
        browser = _browser_with(one)
        first = browser._active
        browser._register(two, activate=True)

        await browser.switch_tab(first)

        self.assertEqual(browser._page, one)
        self.assertEqual(
            browser._active_generation(),
            browser._tab_by_key(first).generation,
            "switching tabs minted a new document number",
        )

    async def test_a_popup_becomes_a_tab_and_the_old_one_is_kept(self):
        # The change that removed the whole failure mode. The old rule adopted
        # the newest page and closed the previous one, so a link that opened its
        # own window destroyed the page you were on.
        one = FakePage("https://one.example/")
        browser = _browser_with(one)
        first = browser._active

        popup = FakePage("https://popup.example/")
        browser._on_page(popup)

        self.assertEqual(len(browser._tabs), 2)
        self.assertIs(browser._page, popup, "a popup should become the tab in use")
        self.assertFalse(one.closed, "the page that opened the popup was closed")
        self.assertIsNotNone(browser._tab_by_key(first))

    async def test_the_whatsapp_tab_is_not_a_tab_of_the_models(self):
        browser = _browser_with(FakePage())
        wa = FakePage("https://web.whatsapp.com/")
        browser._wa_page = wa
        browser._on_page(wa)
        self.assertEqual(len(browser._tabs), 1, "the messaging tab showed up in the model's list")

    async def test_reserving_a_page_gives_the_users_tab_back(self):
        # `context.new_page()` raises Playwright's `page` event *before* it
        # returns, so `_on_page` sees the WhatsApp tab as an unregistered page --
        # indistinguishable from a popup -- and used to adopt it and close the
        # page the user had open. Every messaging call site creates its tab that
        # way, so this is the path, not an edge of it.
        one = FakePage("https://one.example/")
        browser = _browser_with(one)
        first = browser._active

        wa = FakePage("about:blank")
        browser._on_page(wa)          # what the event does, mid-await
        self.assertIs(browser._page, wa)

        browser._wa_page = wa
        browser._reserve(wa)           # what the caller does once it has the page

        self.assertEqual(len(browser._tabs), 1)
        self.assertIs(browser._page, one, "the user's own tab was not given back")
        self.assertIsNotNone(browser._tab_by_key(first))
        self.assertFalse(one.closed)

    async def test_closing_the_active_tab_lands_on_a_real_one(self):
        one, two = FakePage("https://one.example/"), FakePage("https://two.example/")
        browser = _browser_with(one, two)
        second = browser._active

        await browser.close_tab("")
        await asyncio.sleep(0)  # the page is closed on a task, deliberately

        self.assertEqual(len(browser._tabs), 1)
        self.assertIs(browser._page, one)
        self.assertTrue(two.closed, "the closed tab's page was left open in the window")
        self.assertIsNone(browser._tab_by_key(second))

    async def test_closing_the_last_tab_opens_a_blank_one_rather_than_a_second_browser(self):
        # The failure this avoids is not cosmetic. With no tab left, `ensure`
        # used to fall through to `_start`, which launches a *second* Chromium
        # against a profile the first one still holds -- and fails with
        # "existing browser session", the sentence `MSG_PROFILE_LOCKED` exists to
        # translate. So "close the last tab" would have reported the browser as
        # locked.
        only = FakePage()
        browser = _browser_with(only)
        replacement = FakePage()
        context = SimpleNamespace(new_page=mock.AsyncMock(return_value=replacement))
        browser._context = context
        started = []
        browser._start = mock.AsyncMock(side_effect=lambda *a, **k: started.append(1))

        listing = await browser.close_tab("")

        self.assertEqual(started, [], "a second browser was launched to replace one tab")
        self.assertEqual(listing["count"], 1)
        self.assertIs(browser._page, replacement)

    async def test_closing_a_tab_the_model_did_not_name_is_refused(self):
        # An empty target is not in this list: `close` with nothing after it is a
        # complete request meaning "the tab you are looking at".
        browser = _browser_with(FakePage())
        for which in ("7", "two", "1.5", "eleven"):
            with self.subTest(which=which):
                with self.assertRaises(ToolError) as caught:
                    await browser.close_tab(which)
                self.assertIn("tab", str(caught.exception).lower())

    async def test_closing_the_tab_in_use_is_what_an_empty_target_means(self):
        browser = _browser_with(FakePage("https://one.example/"), FakePage("https://two.example/"))
        second = browser._active
        listing = await browser.close_tab("")
        self.assertEqual(listing["closed"], second)
        self.assertEqual(listing["count"], 1)

    async def test_naming_the_first_tab_closes_it_and_leaves_the_active_one_alone(self):
        # The reported failure: "delete the 1st tab" closed the tab in front. Two
        # things had to be true and neither was. The request has to *reach* a tab
        # by number rather than falling back to the one in use, and closing a
        # background tab must not move the active tab off the page.
        pages = [FakePage(f"https://{n}.example/") for n in ("one", "two", "three")]
        browser = _browser_with(*pages)
        active_before = browser._active

        listing = await browser.close_tab("1")
        await asyncio.sleep(0)  # the page is closed on a task, deliberately

        self.assertEqual(listing["closed"], 1)
        self.assertTrue(pages[0].closed, "the named tab was not closed")
        self.assertFalse(pages[2].closed, "the tab in use was closed instead")
        self.assertEqual(browser._active, active_before)
        self.assertIs(browser._page, pages[2])

    async def test_the_last_tab_is_a_tab_and_not_a_direction(self):
        pages = [FakePage(f"https://{n}.example/") for n in ("one", "two", "three")]
        browser = _browser_with(*pages)
        last = browser._tabs[-1].key

        listing = await browser.close_tab("last")

        self.assertEqual(listing["closed"], last)

    async def test_next_and_previous_wrap_around(self):
        # What a browser does: there is no such thing as no next tab, so refusing
        # at the end would be inventing a rule the user did not ask for.
        pages = [FakePage(f"https://{n}.example/") for n in ("one", "two", "three")]
        browser = _browser_with(*pages)
        first, last = browser._tabs[0].key, browser._tabs[-1].key

        await browser.switch_tab("+1")  # from the last, forward to the first
        self.assertEqual(browser._active, first)

        await browser.switch_tab("-1")  # and back round to the last
        self.assertEqual(browser._active, last)

    async def test_switching_names_the_page_the_model_can_now_act_on(self):
        pages = [FakePage("https://one.example/"), FakePage("https://two.example/")]
        browser = _browser_with(*pages)
        first = browser._tabs[0].key

        await browser.switch_tab(str(first))

        self.assertEqual(browser._active, first)
        self.assertIs(browser._page, pages[0])

    async def test_closing_the_others_keeps_the_one_in_use(self):
        pages = [FakePage(f"https://{n}.example/") for n in ("one", "two", "three")]
        browser = _browser_with(*pages)
        keep = browser._active
        doomed = [tab.key for tab in browser._tabs if tab.key != keep]

        listing = await browser.close_other_tabs()

        self.assertEqual(sorted(listing["closed"]), sorted(doomed))
        self.assertNotIn(keep, listing["closed"], "the tab in use was closed")
        self.assertEqual(listing["count"], 1)
        self.assertEqual(browser._active, keep)
        self.assertFalse(pages[2].closed)

    async def test_closing_the_others_with_one_tab_says_so_rather_than_pretending(self):
        browser = _browser_with(FakePage())
        with self.assertRaises(ToolError) as caught:
            await browser.close_other_tabs()
        self.assertIn("only one tab", str(caught.exception))

    async def test_tab_numbers_survive_a_close_so_a_stale_number_cannot_hit_another_tab(self):
        # Deliberate: numbers identify a tab rather than counting the list, so a
        # number the model was given earlier cannot quietly come to mean a
        # different page after something is closed.
        pages = [FakePage(f"https://{n}.example/") for n in ("one", "two", "three")]
        browser = _browser_with(*pages)
        await browser.close_tab("1")

        self.assertEqual([tab.key for tab in browser._tabs], [2, 3])
        with self.assertRaises(ToolError):
            await browser.switch_tab("1")

    async def test_switching_to_a_tab_that_does_not_exist_is_refused_by_name(self):
        browser = _browser_with(FakePage())
        for which in ("9", "one", "1.5"):
            with self.subTest(which=which):
                with self.assertRaises(ToolError):
                    await browser.switch_tab(which)

    async def test_a_tab_the_user_closed_here_is_pruned_rather_than_returned(self):
        # The window is the user's, not ours: they can close a tab from the
        # toolbar at any moment, and the registry has to notice -- both that the
        # page is not the one to hand out, and that its entry is gone rather than
        # still counting against the ceiling and still listed as switchable.
        gone, alive = FakePage(), FakePage("https://alive.example/")
        browser = _browser_with(alive, gone)
        gone.closed = True

        page = await browser.ensure()

        self.assertIs(page, alive)
        self.assertEqual(len(browser._tabs), 1)

    async def test_the_listing_marks_the_tab_in_use_and_leaves_out_whatsapp(self):
        one, two = FakePage("https://one.example/"), FakePage("https://two.example/")
        browser = _browser_with(one, two)
        browser._wa_page = FakePage("https://web.whatsapp.com/")

        listing = await browser.tab_list()

        self.assertEqual(listing["count"], 2)
        self.assertEqual([row["active"] for row in listing["tabs"]], [False, True])
        self.assertEqual(listing["active"], browser._active)
        self.assertNotIn(
            "whatsapp", " ".join(str(row["url"]) for row in listing["tabs"]).lower()
        )

    async def test_opening_a_tab_past_the_ceiling_is_refused_rather_than_silently_reshaped(self):
        browser = _browser_with(*[FakePage() for _ in range(session.MAX_WORK_TABS)])
        browser.ensure = mock.AsyncMock()
        browser._context = SimpleNamespace(new_page=mock.AsyncMock(return_value=FakePage()))

        with self.assertRaises(ToolError) as caught:
            await browser.new_tab()

        message = str(caught.exception)
        self.assertIn(str(session.MAX_WORK_TABS), message)
        self.assertEqual(len(browser._tabs), session.MAX_WORK_TABS, "a tab was opened anyway")

    async def test_a_page_that_opens_windows_cannot_grow_the_registry_past_the_ceiling(self):
        # Different from the refusal above, and deliberately so: this one was not
        # the model's decision, so there is nobody to ask. It gives up a tab the
        # model is *not* looking at -- never the one it holds references to.
        pages = [FakePage() for _ in range(session.MAX_WORK_TABS)]
        browser = _browser_with(*pages)
        in_use = browser._active
        in_use_page = browser._page

        browser._on_page(FakePage())

        self.assertEqual(len(browser._tabs), session.MAX_WORK_TABS)
        self.assertIsNotNone(
            browser._tab_by_key(in_use),
            "the tab in use was the one given up",
        )
        self.assertFalse(in_use_page.closed)
        self.assertIs(browser._page.url, in_use_page.url)
        self.assertIsNot(browser._page, in_use_page, "the popup should become the tab in use")

    async def test_a_refused_address_never_becomes_a_new_tab(self):
        browser = _browser_with(FakePage())
        browser.ensure = mock.AsyncMock()
        browser._context = SimpleNamespace(new_page=mock.AsyncMock(return_value=FakePage()))
        browser.allowed = mock.AsyncMock(return_value=(False, "it is on your own network"))

        with self.assertRaises(ToolError):
            await browser.new_tab("http://127.0.0.1/")

        self.assertEqual(len(browser._tabs), 1)

    async def test_a_redirect_inward_in_a_new_tab_is_stopped_out_loud(self):
        # The pre-check judged the address we asked for; this judges where it
        # landed. Discarding the second verdict -- which the old shape of this
        # method did -- lets a redirect inward into a new tab with no word said.
        landing = FakePage("about:blank", redirect_to="http://127.0.0.1/")
        browser = _browser_with(FakePage())
        browser.ensure = mock.AsyncMock()
        browser._context = SimpleNamespace(new_page=mock.AsyncMock(return_value=landing))
        allowed = mock.AsyncMock(
            side_effect=[(True, ""), (False, "it is on your own network")]
        )
        browser.allowed = allowed

        with self.assertRaises(ToolError) as caught:
            await browser.new_tab("https://example.com/")

        self.assertIn("127.0.0.1", str(caught.exception))

    async def test_releasing_the_browser_forgets_every_tab(self):
        browser = _browser_with(FakePage(), FakePage())
        browser._loop = asyncio.get_running_loop()
        browser._context = SimpleNamespace(close=mock.AsyncMock())
        browser._pw = SimpleNamespace(stop=mock.AsyncMock())

        await browser.release()

        self.assertEqual(browser._tabs, [])
        self.assertIsNone(browser._active)
        self.assertIsNone(browser._page)


# -- the window --------------------------------------------------------------


class BackgroundWindowTests(unittest.IsolatedAsyncioTestCase):
    """A warm-up must not put a window on the screen, and must be restorable.

    The complaint this answers: on every call, while the user was still speaking,
    a browser window appeared over the desktop with WhatsApp loading in it -- on
    machines that never send a message. It came from two places, both measured:
    the pre-warm called the WhatsApp page with focus on, and Chromium takes the
    window forward whenever a tab is created in it.
    """

    async def test_a_headless_browser_has_no_window_to_move(self):
        browser = _browser_with(FakePage())
        browser._context = SimpleNamespace(new_cdp_session=mock.AsyncMock())
        with mock.patch.object(session, "headless_default", return_value=True):
            await browser._minimize_window()
        browser._context.new_cdp_session.assert_not_called()

    async def test_a_browser_with_no_page_is_left_alone(self):
        browser = session._Browser()
        await browser._minimize_window()  # no context, no page, nothing to raise

    async def test_the_window_is_hidden_over_cdp(self):
        browser = _browser_with(FakePage())
        cdp = SimpleNamespace(
            send=mock.AsyncMock(return_value={}),
            detach=mock.AsyncMock(),
        )
        browser._context = SimpleNamespace(new_cdp_session=mock.AsyncMock(return_value=cdp))
        cdp.send.return_value = {"windowId": 4242}

        await browser._minimize_window()

        calls = [call.args[0] for call in cdp.send.await_args_list]
        self.assertEqual(calls, ["Browser.getWindowForTarget", "Browser.setWindowBounds"])
        bounds = cdp.send.await_args_list[1].args[1]
        self.assertEqual(bounds["windowId"], 4242)
        self.assertEqual(bounds["bounds"], {"windowState": "minimized"})

    async def test_a_window_that_will_not_hide_is_not_a_failure(self):
        # The page loaded either way. Refusing here would turn a cosmetic
        # problem into "I couldn't start the browser".
        browser = _browser_with(FakePage())
        browser._context = SimpleNamespace(
            new_cdp_session=mock.AsyncMock(side_effect=RuntimeError("no devtools"))
        )
        await browser._minimize_window()

    async def test_a_background_start_hides_the_window_and_a_normal_one_does_not(self):
        # The launch is told which it is, and the window follows. Asserted on
        # the argument rather than on a window, because the window part needs a
        # real Chromium -- and this is the decision that decides it.
        async def _start(_self, _loop, *, background: bool = False):
            _self._asked_for_background.append(background)
            return _self._register(FakePage()).page

        for background in (True, False):
            with self.subTest(background=background):
                browser = session._Browser()
                browser._asked_for_background = []
                with mock.patch.object(session._Browser, "_start", _start):
                    await browser.ensure(background=background)
                self.assertEqual(browser._asked_for_background, [background])

    async def test_a_browser_we_joined_is_never_hidden(self):
        # It is somebody else's window -- the desktop app's, another room's, the
        # user's own. A background load is not licence to hide it.
        browser = session._Browser()
        starts: list[bool] = []

        async def _start(_self, _loop, *, background: bool = False):
            starts.append(background)
            _self._shared = True
            return _self._register(FakePage()).page

        with mock.patch.object(session._Browser, "_start", _start), mock.patch.object(
            browser, "_minimize_window", new=mock.AsyncMock()
        ) as minimize:
            await browser.ensure(background=True)

        self.assertEqual(starts, [True])
        minimize.assert_not_called()

    async def test_opening_a_page_brings_the_window_back(self):
        # The only thing that has to un-hide it, which is why the warm-up is
        # allowed to hide one. Measured: `bring_to_front` on a minimized Chromium
        # window restores it to normal.
        page = FakePage()
        browser = _browser_with(page)
        browser.allowed = mock.AsyncMock(return_value=(True, ""))
        browser._watch_verdict = mock.AsyncMock(return_value={"verdict": "other"})

        await browser.open_page("https://example.com/")

        self.assertEqual(page.fronted, 1)

    async def test_switching_tabs_brings_the_window_back_too(self):
        one, two = FakePage("https://one.example/"), FakePage("https://two.example/")
        browser = _browser_with(one, two)
        second = browser._active

        await browser.switch_tab(second)

        self.assertEqual(two.fronted, 1)

    async def test_a_window_manager_that_refuses_focus_is_not_a_refusal(self):
        page = FakePage()
        page.bring_to_front = mock.AsyncMock(side_effect=RuntimeError("no window manager"))
        browser = _browser_with(page)
        await browser._reveal(page)  # returns rather than raising


# -- the WhatsApp warm-up ----------------------------------------------------


class WhatsAppWarmUpTests(unittest.IsolatedAsyncioTestCase):
    """`prewarm_whatsapp` is a module function over the singleton, so what is
    asserted is the argument it makes to the browser underneath -- which is the
    whole of the fix."""

    @staticmethod
    async def _warm(browser) -> bool:
        original = session._browser
        session._browser = browser
        try:
            return await session.prewarm_whatsapp()
        finally:
            session._browser = original

    async def test_the_warm_up_asks_for_no_focus(self):
        # The complaint, at the level the session is started from. It used to
        # call `whatsapp_page()` with focus on, and `bring_to_front` is what
        # pulled a 900x700 window over the user mid-sentence -- every call, on
        # every machine, whether or not they ever send a message.
        browser = session._Browser()
        handed: list[bool] = []

        async def _page(*, navigate: bool = True, focus: bool = True):
            handed.append(focus)
            return SimpleNamespace(is_closed=lambda: False, url=session.WHATSAPP_URL)

        with mock.patch.object(browser, "whatsapp_page", side_effect=_page):
            result = await self._warm(browser)

        self.assertEqual(handed, [False], "the warm-up asked for the window after all")
        self.assertTrue(result)

    async def test_a_failed_warm_up_is_reported_as_false_not_raised(self):
        # A user who has not signed in yet will not have a tab, and that is not
        # an error worth surfacing while the greeting is still being spoken.
        browser = session._Browser()
        browser.whatsapp_page = mock.AsyncMock(side_effect=RuntimeError("no profile"))
        self.assertFalse(await self._warm(browser))


class BackgroundWhatsAppTabTests(unittest.IsolatedAsyncioTestCase):
    async def test_creating_the_tab_hides_the_window_it_just_raised(self):
        # Measured, and the reason this is not just "minimize on launch":
        # Chromium brings the window forward whenever a tab is created in it, so
        # a warm-up that minimized the fresh window had it raised again by its
        # own next step.
        #
        # `_hid_window` is set here because it is the precondition the code checks,
        # and it is what `ensure` sets after launching and minimizing. Mocking
        # `ensure` bypasses that, so without this line the test asserts that we
        # put away a window we never touched -- which the code deliberately does
        # not do, because on a browser that was *already open* the window belongs
        # to somebody else. The companion test below pins that half.
        page = FakePage()
        browser = session._Browser()
        browser._context = SimpleNamespace(new_page=mock.AsyncMock(return_value=page))
        browser.ensure = mock.AsyncMock()
        browser.allowed = mock.AsyncMock(return_value=(True, ""))
        browser._hid_window = True
        minimized: list[int] = []
        browser._minimize_window = mock.AsyncMock(side_effect=lambda: minimized.append(1))

        await browser.whatsapp_page(focus=False)

        self.assertEqual(minimized, [1], "the window was raised and left raised")

    async def test_a_browser_we_only_joined_is_never_put_away(self):
        # The other half of the same rule, and the reason it is a rule: creating
        # the tab raises the window even when we did not launch it, and that
        # window may be one the user is looking at. Putting it away is not ours to
        # do.
        page = FakePage()
        browser = session._Browser()
        browser._context = SimpleNamespace(new_page=mock.AsyncMock(return_value=page))
        browser.ensure = mock.AsyncMock()
        browser.allowed = mock.AsyncMock(return_value=(True, ""))
        browser._hid_window = False
        minimized: list[int] = []
        browser._minimize_window = mock.AsyncMock(side_effect=lambda: minimized.append(1))

        await browser.whatsapp_page(focus=False)

        self.assertEqual(
            minimized, [], "a window we joined was minimized by a background warm-up"
        )

    async def test_a_real_request_still_focuses_and_does_not_re_hide(self):
        # The send flow depends on this: the user approves a draft they can see.
        # Background must not have quietly taken it away.
        page = FakePage()
        browser = session._Browser()
        browser._context = SimpleNamespace(new_page=mock.AsyncMock(return_value=page))
        browser.ensure = mock.AsyncMock()
        browser.allowed = mock.AsyncMock(return_value=(True, ""))
        focused: list[int] = []
        minimized: list[int] = []
        browser._focus_whatsapp = mock.AsyncMock(side_effect=lambda: focused.append(1))
        browser._minimize_window = mock.AsyncMock(side_effect=lambda: minimized.append(1))

        await browser.whatsapp_page(focus=True)

        self.assertEqual(focused, [1])
        self.assertEqual(minimized, [], "a focused request hid the window the user must see")


# -- the tool ----------------------------------------------------------------


class BrowseTabActionTests(unittest.TestCase):
    """The verb the model actually calls."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {audit_log.AUDIT_PATH_ENV: str(Path(tmp.name) / "audit.jsonl")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_verb_is_part_of_the_documented_surface(self):
        self.assertIn("tab", browse_module.ACTIONS)
        self.assertIn("tab", browse_module.MSG_BAD_ACTION.lower())

    def test_an_unknown_action_still_names_the_real_ones(self):
        with self.assertRaises(ToolError) as caught:
            run_browse("teleport")
        self.assertIn("tab", str(caught.exception))

    def test_a_request_that_is_not_a_tab_request_is_refused(self):
        with mock.patch.object(browse_module, "require_approval", new_callable=mock.AsyncMock):
            with self.assertRaises(ToolError) as caught:
                run_browse("tab", text="close two")
        self.assertIn("tab", str(caught.exception).lower())

    def test_listing_is_a_read(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module,
            "tab_list",
            new_callable=mock.AsyncMock,
            return_value={
                "tabs": [
                    {"tab": 1, "title": "Results", "url": "https://a.example/", "active": False},
                    {"tab": 2, "title": "Video", "url": "https://b.example/", "active": True},
                ],
                "active": 2,
                "count": 2,
            },
        ):
            result = run_browse("tab")

        self.assertEqual(approval.call_args.kwargs["action_class"], "read")
        self.assertIn("tab 1", result)
        self.assertIn("tab 2", result)
        self.assertIn("Video", result)
        self.assertIn("the one you're on", result)

    def test_no_tabs_at_all_says_so_without_inventing_one(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module, "tab_list", new_callable=mock.AsyncMock, return_value={"tabs": [], "active": None, "count": 0}
        ):
            self.assertIn("blank", run_browse("tab"))

    def test_opening_a_tab_is_a_read_and_is_counted(self):
        # It was an `act`, which asked the user about a page being put on screen.
        # That is the same act as `open`, and `open` is a read -- so it was asking
        # twice for one request, and the second ask is what a retry looks like.
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module,
            "new_tab",
            new_callable=mock.AsyncMock,
            return_value={"tab": 2, "title": "", "url": "about:blank", "count": 2},
        ) as opener:
            result = run_browse("tab", text="new")

        self.assertEqual(approval.call_args.kwargs["action_class"], "read")
        opener.assert_awaited_once_with("")
        self.assertIn("tab 2", result)
        self.assertIn("2 tabs open", result)

    def test_a_tab_opened_with_an_address_reaches_the_browser(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module,
            "new_tab",
            new_callable=mock.AsyncMock,
            return_value={"tab": 3, "title": "Example", "url": "https://example.com/", "count": 3},
        ) as opener:
            result = run_browse("tab", text="new", url="https://example.com/")

        opener.assert_awaited_once_with("https://example.com/")
        self.assertIn("Example", result)

    def test_switching_tells_the_model_to_read_before_it_acts(self):
        # Said out loud because it is the cost of the feature: a reference minted
        # on the tab you just left says nothing about this one.
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module,
            "switch_tab",
            new_callable=mock.AsyncMock,
            return_value={"tab": 4, "title": "Fourth", "url": "https://d.example/", "count": 3},
        ) as switch:
            result = run_browse("tab", text="4")

        self.assertEqual(approval.call_args.kwargs["action_class"], "read")
        switch.assert_awaited_once_with("4")
        self.assertIn("tab 4", result)
        self.assertIn("Read it before acting", result)

    def test_closing_reports_what_is_left_and_which_tab_is_in_use(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module,
            "close_tab",
            new_callable=mock.AsyncMock,
            return_value={
                "closed": 3,
                "count": 2,
                "active": 1,
                "tabs": [
                    {"tab": 1, "title": "Results", "url": "https://a.example/", "active": True},
                    {"tab": 2, "title": "", "url": "about:blank", "active": False},
                ],
            },
        ) as closer:
            result = run_browse("tab", text="close 3")

        self.assertEqual(approval.call_args.kwargs["action_class"], "act")
        closer.assert_awaited_once_with("3")
        self.assertIn("Closed tab 3", result)
        self.assertIn("Results", result)

    def test_closing_the_last_tab_says_a_blank_one_took_its_place(self):
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ), mock.patch.object(
            browse_module,
            "close_tab",
            new_callable=mock.AsyncMock,
            return_value={"closed": 1, "count": 0, "active": 1, "tabs": []},
        ):
            result = run_browse("tab", text="close")

        self.assertIn("last one", result)
        self.assertIn("blank", result)

    def test_closing_the_browser_is_still_its_own_verb(self):
        # `tab close` closes one tab and `close` closes all of them, and the two
        # are easy to confuse -- so `close` must not have become an alias.
        with mock.patch.object(
            browse_module, "require_approval", new_callable=mock.AsyncMock
        ) as approval, mock.patch.object(
            browse_module, "release", new_callable=mock.AsyncMock
        ) as released:
            result = run_browse("close")

        self.assertEqual(approval.call_args.kwargs["action_class"], "act")
        released.assert_awaited_once()
        self.assertIn("closed", result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()