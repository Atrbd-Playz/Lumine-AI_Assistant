"""The Web path's two identity steps: turning a name into a chat, and a chat into
a number.

Both existed and neither was tested against the other's failure, which is how the
product ended up contradicting itself in one breath: `search_chats` found a
contact through WhatsApp's own search box, and the very next call -- `open_chat`,
in the same tool, seconds later -- refused with "I couldn't find that in your
WhatsApp". Resolve said yes and open said no, and the user was left holding both.

The cause is structural rather than a bad selector. The rendered chat list is the
last few dozen conversations, and `search_chats` clears the filter in its own
`finally`, so by the time anything opens a chat the row is gone from the DOM. A
chat the account has and the visible list does not show was therefore unopenable.

No browser here. `open_chat` is driven against a page fake that answers the two
JavaScript shapes it uses, which is enough to pin the order of the three rules
that matter: try the list, fall back to the box, and clear the box on the way out
whether or not it worked.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import unittest
from typing import Any
from unittest import mock

from agent.tools.messaging import whatsapp as send_path
from agent.tools.messaging import whatsapp_web as web
from agent.tools.tools_compat import ToolError


class FakeKeyboard:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    async def type(self, text: str, delay: float | None = None) -> None:
        # Typing into the search box is what makes the account's whole history
        # reachable, so the fake applies the filter exactly as the page would --
        # and on the page the filter *is* the text in the box, which is the
        # relationship `clear_chat_search` now reads before it presses anything
        # (measured: `box='Master'` with the results up, and the baseline list the
        # instant the box was emptied).
        #
        # `delay` is accepted and ignored: the real caller passes it because
        # WhatsApp's composer drops characters typed faster than it re-renders.
        # The fake is not a React app and cannot drop anything, so honouring it
        # would only test that the fake is slow.
        del delay
        self._page.typed.append(text)
        self._page.search_text = (self._page.search_text or "") + text

    async def press(self, key: str) -> None:
        self._page.keys.append(key)
        if key == "Control+A":
            self._page.selection = True
        elif key == "Delete":
            if self._page.selection and self._page.search_text:
                self._page.search_text = ""
            self._page.selection = False
        elif key == "Escape":
            # The gesture `clear_chat_search` used to fire blindly. It clears here
            # only because this fake has no notion of focus. On the live page it
            # clears the filter while the box is focused and **closes the open
            # chat** when focus is on the row a press just landed on -- which is
            # the bug, so the tests below assert it no longer reaches the page.
            self._page.search_text = ""


class FakePage:
    """A page with a rendered chat list and a separate set of search results.

    It models the *mark-then-click* arrangement the code now uses: an evaluate
    marks an element, and `click` is a separate call. That is not incidental
    bookkeeping -- it is the shape of the fix. The earlier code did
    `el.click()` inside one evaluate, which is an untrusted event WhatsApp ignores,
    and the fake modelled that single call, so it could not have told the two
    apart. A fake that answers `click()` makes the distinction observable, and
    `test_an_untrusted_click_is_not_a_click` below pins it.
    """

    def __init__(self, rendered: list[str], search_results: list[str] | None = None) -> None:
        self.rendered = list(rendered)
        self.search_results = list(search_results or [])
        #: What the search box holds. The list is narrowed exactly while this is
        #: non-empty -- on the live page and in this fake alike, which is what lets
        #: `clear_chat_search` ask "is there a filter?" instead of firing a key at
        #: whatever has focus.
        self.search_text = ""
        #: Set by select-all, so the following delete knows it has something to
        #: delete. Modelled because the clear is select-all-then-delete now.
        self.selection = False
        self.typed: list[str] = []
        self.keys: list[str] = []
        #: What the conversation header reports. Set by a successful click.
        self.active = ""
        #: Set to make the header disagree with what was clicked, which is the
        #: case "I clicked the row" has to survive.
        self.header_override = ""
        self.keyboard = FakeKeyboard(self)
        #: The title the last *row* mark reserved, so `click` opens the right row.
        self.marked: str | None = None
        #: Selectors marked for a non-row target: composer, send button, search box.
        self.marked_controls: list[str] = []
        #: Every selector passed to `click`, so a test can assert which element
        #: was actually pressed.
        self.clicks: list[str] = []
        #: Set to make `click` fail, the way a covered or detached element does.
        #: A string limits it to one selector, so a test can make the *row* press
        #: fail while the search box still works.
        self.click_fails = False
        self.click_fails_for = ""
        #: How many times a targeted click fails before it works; `None` = always.
        self.click_fail_times: int | None = 0
        #: Set to make focus confirmation fail, so `_focus` falls through.
        self.focus_fails = False

    @property
    def filtering(self) -> bool:
        """Whether the list is narrowed -- exactly while the box holds text.

        Read-only on purpose: a test that wants a filter turns one on by giving
        the box the query the resolve step left in it, because that *is* what a
        filter is. Two flags that could disagree is how a fake passes while the
        real page is in a state the code never handles.
        """
        return bool(self.search_text)

    def _visible(self) -> list[str]:
        return self.search_results if self.filtering else self.rendered

    async def evaluate(self, script: str, arg: object = None) -> object:
        if "setAttribute" in script and "data-lumine-wa-row" in script:
            wanted = str(arg or "").strip().casefold()
            for title in self._visible():
                if title.casefold() == wanted:
                    # Reserved, *not* opened. Marking is preparation; only a real
                    # press opens the chat. Setting `active` here would make the
                    # fake claim the conversation opened when the press failed --
                    # which is the exact bug these tests exist to rule out, so the
                    # fake has to be honest about the difference too.
                    self.marked = title
                    return True
            return False
        if "document.activeElement" in script:
            # The focus *confirmation* read. It exists so `_focus` can tell "the
            # click landed" from "the click landed and the element is not editing".
            return not self.focus_fails
        if "lumineContactName" in script and "conversation-info-header" in script:
            # The header read. It now declares the contact-name helper inside the
            # arrow function, so it no longer matches the old "is it a function"
            # shape these fakes used to key on -- and it answers with a *dict*
            # carrying both the name and the text as displayed, so a contact shown
            # as 'Jubayed Reza (22-23)' can be matched on either.
            shown = self.reported_chat()
            return {"title": shown, "raw": shown, "open": bool(shown)}
        if "input.value" in script:
            # Both search-box reads land here, and they are two different
            # questions: `_JS_SEARCH_BOX_EMPTY` probes the box (and focuses it
            # when there is something to clear), while `_JS_SEARCH_BOX_VALUE`
            # simply reads what it holds.
            #
            # The old fake answered "empty" to *both*, even while it was
            # filtering -- so a test could not tell "cleared the filter" from
            # "never had one", which is the distinction `clear_chat_search` now
            # makes before it decides whether a keystroke is warranted at all.
            if "focus()" in script:
                return not self.search_text
            return self.search_text
        if "setAttribute" in script:
            # Any other mark -- the composer, the send button, the search box.
            # These exist on the fake, so the mark succeeds, but they are recorded
            # in `marked_controls` rather than `marked`: only a *row* mark opens a
            # conversation. Conflating them would let the search-box mark leave a
            # plausible non-empty chat behind, which is what the prefix test below
            # is there to catch.
            self.marked_controls.append(str(arg))
            return True
        raise AssertionError(f"unexpected evaluate: {script[:70]}")

    async def click(self, selector: str, **kwargs) -> None:
        del kwargs
        if self.click_fails:
            raise RuntimeError("element is not visible")
        # `click_fail_times` exists for the fallback test: the *same* selector has to
        # fail once and then work, because the open step presses the row twice --
        # once from the rendered list, once after the search filter. A fake that
        # failed forever would make the fallback unreachable, so the test would pass
        # while proving nothing about it.
        #
        # A budget rather than a boolean, and one that cannot silently mean
        # "never": `0` is a budget of zero failures, so the refusal test -- which
        # sets `click_fails` -- is the thing that fails every time, and a test that
        # forgets to set anything gets working clicks rather than a pass.
        if self.click_fails_for and selector == self.click_fails_for:
            if self.click_fail_times > 0:
                self.click_fail_times -= 1
                raise RuntimeError("element is not visible")
        self.clicks.append(selector)
        if self.marked:
            self.active = self.marked

    def reported_chat(self) -> str:
        return self.header_override or self.active


#: Which variable holds each marker, so the test above can check the click
#: selector derived from it exists. Written out rather than derived from the name,
#: because "the marker is clicked" is a claim about a second piece of code and
#: guessing that name from the value would test nothing.
_MARK_VARIABLES = {
    "data-lumine-wa-row": "_ROW_MARK",
    "data-lumine-wa-focus": "_FOCUS_MARK",
    "data-lumine-wa-click": "_CLICK_MARK",
    "data-lumine-wa-menu": "_MENU_MARK",
    "data-lumine-wa-item": "_ITEM_MARK",
    "data-lumine-wa-confirm": "_CONFIRM_MARK",
    "data-lumine-wa-emoji": "_EMOJI_MARK",
    "data-lumine-wa-block": "_BLOCK_MARK",
    "data-lumine-wa-unlink": "_UNLINK_MARK",
    "data-lumine-wa-call": "_CALL_MARK",
    "data-lumine-wa-rowmenu": "_ROW_MENU_MARK",
    "data-lumine-wa-typing": "_TYPING_MARK",
}


class _ComposerPage:
    """A page whose composer keeps what it is told -- or does not.

    The `keeps` argument is the point of the whole class. React re-renders the
    composer on every keystroke and drops characters typed faster than that, and a
    fake that keeps everything makes the verification unreachable and untestable:
    the bug it guards is *silent truncation*, which a perfect fake cannot show.
    """

    def __init__(
        self,
        keeps: str | None = None,
        *,
        focus_fails: bool = False,
        click_fails: bool = False,
    ) -> None:
        self._keeps = keeps
        self._focus_fails = focus_fails
        self.text = ""
        self.clicks: list[str] = []
        self.keys: list[str] = []
        self.focused = False
        #: How many times the `el.focus()` fallback was used.
        self.focus_calls = 0
        #: Set to make `page.click` refuse, as an overlay makes it.
        self.click_fails = click_fails

    def state(self) -> dict[str, object]:
        return {"found": True, "text": self.text, "empty": not self.text.strip(), "hasSend": bool(self.text)}

    class _Keyboard:
        def __init__(self, page: "_ComposerPage") -> None:
            self._page = page

        async def type(self, text: str, delay: float | None = None) -> None:
            del delay
            self._page.text = text if self._page._keeps is None else self._page._keeps

        async def press(self, key: str) -> None:
            self._page.keys.append(key)
            if key == "Backspace":
                self._page.text = ""

    @property
    def keyboard(self) -> "_ComposerPage._Keyboard":
        return _ComposerPage._Keyboard(self)

    async def evaluate(self, script: str, arg: object = None) -> object:
        del arg
        if "document.activeElement" in script:
            return not self._focus_fails
        if "el.focus()" in script:
            self.focus_calls += 1
            return True
        if "setAttribute" in script:
            self.focused = True
            return True
        raise AssertionError(f"unexpected evaluate: {script[:60]}")

    async def click(self, selector: str, **kwargs) -> None:
        del kwargs
        if self.click_fails:
            raise RuntimeError("element intercepted by overlay")
        self.clicks.append(selector)


def _with_page(page: object, **patches: object):
    """Patch `whatsapp_web`'s page accessor and composer read onto `page`.

    Every test below needs the same patches, and each one has to name the page
    explicitly -- a mock whose `return_value` is set by two different tests leaks
    between them in a way that only shows up as a mysterious failure in the third.

    ``require_ready`` is patched here because the steps that **act** -- typing,
    pressing send -- now wait for the page before they do. Left unpatched it polls
    the real browser for up to ninety seconds, so every one of these tests would
    hang rather than fail. A test suite that hangs on a behavioural change is a
    suite nobody can iterate against, and the change itself is right: WhatsApp
    renders the composer last, so typing into a page that is still mounting is a
    send aimed at whatever currently holds focus.
    """
    stack = contextlib.ExitStack()
    out = stack.__enter__()
    out.enter_context(
        mock.patch.object(web, "whatsapp_page", new=mock.AsyncMock(return_value=page))
    )
    out.enter_context(
        mock.patch.object(web, "composer", new=mock.AsyncMock(side_effect=page.state))
    )
    out.enter_context(
        mock.patch.object(web, "require_ready", new=mock.AsyncMock(return_value=None))
    )
    return out


def open_chat(page: FakePage, title: str, **kwargs) -> bool:
    """Drive `web.open_chat` against the fake, with nothing real involved.

    ``timeout`` defaults to the module's own floor rather than its four seconds:
    the header-confirmation wait is not the property under test, and leaving it at
    the default makes every *refusal* test busy-wait out the full window.
    """

    kwargs.setdefault("timeout", 0.5)

    async def no_sleep(_seconds: float) -> None:
        return None

    async def run() -> bool:
        with (
            mock.patch.object(web, "require_ready", new_callable=mock.AsyncMock),
            mock.patch.object(web, "whatsapp_page", new_callable=mock.AsyncMock, return_value=page),
            mock.patch.object(web, "active_chat", new_callable=mock.AsyncMock, side_effect=page.reported_chat),
            # `open_chat` reads the header through `active_chat_names`, which
            # answers `(name, as displayed)`. Patching only `active_chat` left the
            # real one running against a fake page -- so these tests stopped
            # exercising the header check at all, silently.
            mock.patch.object(
                web,
                "active_chat_names",
                new_callable=mock.AsyncMock,
                side_effect=lambda: ((lambda s: (s, s))(page.reported_chat())),
            ),
            mock.patch.object(web, "_sleep", new=no_sleep),
        ):
            return await web.open_chat(title, **kwargs)

    return asyncio.run(run())


class OpenChatTests(unittest.TestCase):
    def test_a_rendered_chat_opens_without_disturbing_the_search_box(self):
        # The fast path stays first: one round trip, and no keystrokes aimed at a
        # box the user may be looking at.
        page = FakePage(rendered=["Afia", "Family"])
        self.assertTrue(open_chat(page, "Afia"))
        self.assertEqual(page.typed, [], "typing into the search box for a visible chat")
        self.assertEqual(page.active, "Afia")

    def test_a_chat_the_list_does_not_show_is_reached_through_the_search_box(self):
        # The regression. A contact the account has but the visible window does
        # not contain used to be unopenable, so every read and send of it failed
        # while the name resolved a moment earlier.
        page = FakePage(rendered=["Family", "Work"], search_results=["Afia"])
        self.assertTrue(open_chat(page, "Afia"))
        self.assertEqual(page.typed, ["afia"], "the name is typed as it was resolved")
        self.assertEqual(page.active, "Afia")

    def test_the_search_box_is_left_clear_either_way(self):
        # A filter left behind narrows every later lookup, and the next read of
        # the chat list would return the filtered rows and look like a plausible
        # wrong answer.
        page = FakePage(rendered=["Family"], search_results=["Afia"])
        self.assertTrue(open_chat(page, "Afia"))
        self.assertFalse(page.filtering, "the filter outlived the call")
        self.assertIn("Control+A", page.keys, "the box was never actually cleared")
        # The regression the clear was replaced *for*. A blind Escape fires at
        # whatever holds focus, and after a row press that is the row -- measured
        # on the live account closing the conversation the press had just opened,
        # which is the whole of "I can't see the message box on this chat".
        self.assertNotIn(
            "Escape", page.keys, "a blind Escape reached the page again"
        )

    def test_a_previously_resolved_search_result_is_opened_without_searching_again(self):
        page = FakePage(rendered=["Family"], search_results=["Afia"])
        # The resolve step's own filter, left in place for the open step to reuse
        # -- which on the page means the box still holds the query.
        page.search_text = "Afia"

        self.assertTrue(open_chat(page, "Afia"))

        self.assertEqual(page.typed, [], "the already-visible result was searched again")
        self.assertEqual(page.active, "Afia")
        self.assertFalse(page.filtering, "the reuse left the filter behind")

    def test_name_search_can_leave_the_result_visible_for_the_open_step(self):
        rows = [{"title": "Afia", "jid": "15551234567@c.us"}]
        page = mock.Mock()
        page.keyboard.type = mock.AsyncMock()
        page.keyboard.press = mock.AsyncMock()
        with (
            mock.patch.object(web, "whatsapp_page", new=mock.AsyncMock(return_value=page)),
            mock.patch.object(web, "require_ready", new=mock.AsyncMock()),
            mock.patch.object(web, "list_chats", new=mock.AsyncMock(return_value=[])),
            mock.patch.object(web, "_focus", new=mock.AsyncMock(return_value=object())),
            mock.patch.object(web, "_empty_search_box", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(web, "_poll", new=mock.AsyncMock(return_value=(True, rows))),
        ):
            result = asyncio.run(web.search_chats("Afia", preserve_search=True))

        self.assertEqual(result, rows)
        page.keyboard.press.assert_not_awaited()

    def test_a_failed_open_still_clears_the_box(self):
        page = FakePage(rendered=["Family"])
        self.assertFalse(open_chat(page, "Nobody"))
        self.assertFalse(page.filtering, "a failed search left the list filtered")
        self.assertIn("Control+A", page.keys, "the box was never actually cleared")

    def test_a_prefix_is_not_a_match(self):
        # Exact titles only, on both routes. "Afia" must never open "Afia
        # Rahman": a message prepared for the wrong chat is the failure this
        # whole path is built to prevent.
        page = FakePage(rendered=["Afia Rahman"], search_results=["Afia Rahman"])
        self.assertFalse(open_chat(page, "Afia"))
        self.assertEqual(page.active, "")

    def test_the_header_still_has_the_last_word(self):
        # The row was clicked and the chat that opened is somebody else's. "I
        # clicked the row" is not "the right chat is open", and only the second
        # may gate a send.
        page = FakePage(rendered=["Afia"], search_results=["Afia"])
        page.header_override = "someone else"
        self.assertFalse(open_chat(page, "Afia"))

    def test_a_blank_title_is_refused(self):
        for title in ("", "   ", None):
            with self.subTest(title=title):
                self.assertFalse(open_chat(FakePage(rendered=["Afia"]), title))

    def test_a_visible_row_is_opened_by_a_real_click_not_a_scripted_one(self):
        """The bug behind "she selects the chat but nothing gets typed".

        `el.click()` inside the page dispatches an untrusted event, and WhatsApp's
        chat list does not open on one. The probe caught it as `opened: False`
        after a click that reported success -- so the code searched the whole
        account for a chat that was on screen, and typing then went to a
        conversation that was never open.

        The marker and the click are therefore two calls, and the test can tell
        them apart because the fake models them separately.
        """
        page = FakePage(rendered=["Afia", "Family"])
        self.assertTrue(open_chat(page, "Afia"))
        self.assertEqual(
            page.clicks,
            [f"[{web._ROW_MARK}='1']"],
            "the row must be pressed through Playwright, not from inside the page",
        )
        self.assertEqual(
            page.typed, [], "a visible chat must not fall through to the search box"
        )


#: Scripts allowed to click from inside the page. Intentionally empty.
#:
#: There was a version of this test with an allow-list -- a menu item and a
#: confirmation button, exempted on the argument that "a menu item is plain DOM".
#: The argument was backwards, and measuring it was what showed it: the click fired
#: the page's handler, the handler changed React state, and the menu closed -- so
#: the operation reported failure while the thing it asked for had happened, and
#: nothing in the module could tell those apart. Both are marked and pressed now.
#:
#: An empty list is the point. If a future build genuinely needs an in-page click,
#: this should be changed *deliberately and with a reason recorded here*, not
#: widened to make a new failure go away.
_PAGE_CLICK_EXEMPT: frozenset[str] = frozenset()


class ClickFidelityTests(unittest.TestCase):
    """Every click into WhatsApp is a real press, and none of them used to be.

    Twelve of them reached the page: the chat row, the composer (as `el.focus()`),
    the send button, the message menu, a menu item, a confirmation, an emoji, a row
    menu, the block control, a linked device, and the call button. Each was once an
    untrusted `el.click()` or `dispatchEvent` from inside the page, and each
    reported success while doing nothing -- so the sequence looked healthy right up
    to the point where no message arrived, and nothing in the module could say
    which step had actually happened.

    They are found one at a time here because each failure is silent in a different
    way: a row that does not open, a keystroke sent to no element, a menu that
    closes before it can be read. Pinning them together is the only way the fifth
    one gets found before a user does.
    """

    #: Scripts allowed to click from inside the page. Intentionally empty.
    #:
    #: There was a version of this test with an allow-list -- a menu item and a
    #: confirmation button, exempted on the argument that "a menu item is plain
    #: DOM". The argument was backwards, and measuring it was what showed it: the
    #: click fired the page's handler, the handler changed React state, and the
    #: menu closed -- so the operation reported failure while the thing it asked
    #: for had happened, and nothing in the module could tell those apart. Both are
    #: marked and pressed now.
    #:
    #: An empty list is the point. If a future build genuinely needs an in-page
    #: click, this should be changed *deliberately and with a reason recorded
    #: here*, not widened to make a new failure go away.
    _PAGE_CLICK_EXEMPT = frozenset()

    def test_no_script_clicks_or_fakes_events_from_inside_the_page(self):
        for name in dir(web):
            script = getattr(web, name)
            if not isinstance(script, str):
                continue
            if not script.lstrip().startswith(("(", "function")):
                continue
            # `dispatchEvent` is the same class of defect: the page's listeners run
            # and none of the browser's defaults happen. A synthetic `mouseover` is
            # how the row menu was "revealed" for years, and it is why the trigger
            # was never actually there to click.
            self.assertNotIn(".click()", script, f"{name} clicks from inside the page")
            self.assertNotIn("dispatchEvent", script, f"{name} fakes an input event")

    def test_the_exemption_list_stays_empty(self):
        # Named so that "let me just exempt this one" is a visible act rather than
        # a deletion.
        self.assertEqual(_PAGE_CLICK_EXEMPT, frozenset())

    def test_focus_is_not_done_by_asking_the_page_to_do_it(self):
        """`el.focus()` is the composer bug.

        It runs the page's focus handler and stops: no real press, so no caret, no
        editing mode, and nothing dismissed that was overlaying the box. Keystrokes
        then go somewhere that is not the composer -- which looks, from the user's
        side, like typing that went nowhere.
        """
        for name in dir(web):
            script = getattr(web, name)
            if isinstance(script, str) and script.lstrip().startswith("("):
                self.assertNotIn("el.focus()", script, f"{name} focuses from inside the page")

    def test_every_marker_is_declared_with_its_click_beside_it(self):
        """A marker nothing clicks is a no-op waiting to be found.

        Each of these was a separate bug -- a chat row, the composer, the send
        button, a message menu, a menu item, a confirmation, an emoji, a row menu,
        a device row, a call button -- and they were all found the same way: one at
        a time, each failing quietly while reporting success. Declared together so
        the next one is visible in the same place.
        """
        source = inspect.getsource(web)
        marks = [
            value
            for name, value in vars(web).items()
            if name.endswith("_MARK") and isinstance(value, str)
        ]
        self.assertGreaterEqual(len(marks), 10, "the marker list shrank -- check why")
        for mark in marks:
            self.assertTrue(mark.startswith("data-lumine-wa-"), mark)
            self.assertIn(
                f"[{{{_MARK_VARIABLES[mark]}}}='1']",
                source,
                f"{mark} is declared but nothing clicks it",
            )

    def test_the_composer_is_focused_by_pressing_it(self):
        """The composer is the one that decides whether typing works at all.

        `el.focus()` was the bug: it runs the page's focus handler and stops. No
        real press means no caret, no editing mode, and nothing dismissed that was
        overlaying the box -- so keystrokes went to an element that existed but was
        not being edited. From the user's side that is indistinguishable from
        "she typed and nothing happened".
        """
        page = _ComposerPage()

        async def run() -> Any:
            with _with_page(page):
                await web.type_message("hello")

        asyncio.run(run())
        self.assertIn(
            f"[{web._FOCUS_MARK}='1']", page.clicks, "the composer was never pressed"
        )

    def test_a_press_that_does_not_take_focus_is_not_accepted(self):
        # A `contenteditable` can absorb a click and still not be the active
        # element. Typing then goes to the search box or the page body, which is
        # where a message ends up when the composer "accepted" the text.
        page = _ComposerPage(focus_fails=True)

        async def run() -> Any:
            with _with_page(page):
                with self.assertRaises(ToolError):
                    await web.type_message("hello")

        asyncio.run(run())

    def test_typing_is_verified_and_a_short_draft_is_never_sent(self):
        """The guarantee, and the reason for ``KEY_DELAY_MS``.

        "I pressed the keys" is not "the text is in WhatsApp". React re-renders the
        composer on every keystroke, so text typed faster than it re-renders is
        silently truncated -- and the approval card then offers to send a draft
        that is missing its tail. A draft that lost characters is a failure, not a
        near miss.
        """
        page = _ComposerPage(keeps="helo")  # a dropped character

        async def run() -> ToolError:
            with (
                _with_page(page),
                mock.patch.object(
                    web, "_clear_composer", new=mock.AsyncMock(return_value=True)
                ),
            ):
                with self.assertRaises(ToolError) as caught:
                    await web.type_message("hello")
                return caught.exception

        refusal = asyncio.run(run())
        self.assertIn("did not keep", str(refusal))
        # It cleared the box itself, so telling the user to go and clear it would
        # send them to an empty box and read as "she cannot see her own messages".
        self.assertNotIn("clear the message box", str(refusal))

    def test_the_whole_message_must_arrive(self):
        page = _ComposerPage(keeps="hello and then some")

        async def run() -> Any:
            with (
                _with_page(page),
                mock.patch.object(
                    web, "_clear_composer", new=mock.AsyncMock(return_value=True)
                ),
            ):
                with self.assertRaises(ToolError):
                    await web.type_message("hello")

        asyncio.run(run())

    def test_invisible_marks_do_not_make_a_faithful_draft_look_wrong(self):
        # WhatsApp pads a contenteditable with zero-width direction marks and
        # non-breaking spaces. Stripped for comparison only -- a draft that arrived
        # intact must not be reported as a mismatch and cleared.
        self.assertEqual(web._comparable("\u200ehello \xa0there\u200f"), "hello there")
        self.assertNotEqual(web._comparable("hello"), web._comparable("hello there"))

    def test_the_two_invisible_sets_cannot_drift(self):
        """The bug that refused every send, and the one that hid every draft.

        The JavaScript emptiness test and the Python comparison were written
        separately and drifted. On the JS side the emptiness test stripped only two
        of the six characters WhatsApp uses, so an **empty** composer holding a
        zero-width space read as "there is already something written in WhatsApp's
        message box" -- and `type_message` refused on that, on every chat, forever.
        Measured on a real account: `text=''` while `empty=False`. The user could
        select a chat and never once get as far as typing.

        The Python side had the mirror-image bug: the literal two-character sequence
        `\\u200e` rather than the mark itself, so it stripped nothing and a draft that
        arrived intact was cleared out from under them.

        One list, emitted into both languages, is the only arrangement that cannot
        disagree again -- and the disagreement is invisible in both languages at
        once, which is why a test has to compare them.
        """
        escapes = re.findall(r"\\u([0-9a-fA-F]{4})", web._JS_COMPOSER)
        self.assertTrue(escapes, "the composer read no longer strips anything")
        from_js = {chr(int(code, 16)) for code in escapes}
        self.assertEqual(
            from_js,
            set(web._INVISIBLE),
            "the JS emptiness test and the Python comparison disagree about what "
            "'invisible' means, which is how an empty composer became a dirty one",
        )

    def test_a_composer_holding_only_padding_is_not_a_draft(self):
        """The regression, stated as the rule the JavaScript implements.

        `text` empty but `empty` false is the exact shape that was measured, and it
        is what the dirty-draft refusal read as. A Python test cannot run the
        browser half -- `verify_type.py` does that -- but it can pin the rule, which
        is what stops someone quietly widening the character set back.
        """
        self.assertIn("empty: visible.length === 0", web._JS_COMPOSER)
        for padding in (
            "",
            "\u200e",
            "\u200f",
            "\u200b",
            "\u2060",
            "\ufeff",
            "\u200e\u200f\u200b",
        ):
            with self.subTest(padding=repr(padding)):
                self.assertEqual(web._comparable(padding), "")

    def test_the_send_button_is_pressed_and_not_asked_to_click_itself(self):
        page = _ComposerPage()

        async def run() -> Any:
            with _with_page(page):
                await web.press_send()

        asyncio.run(run())
        self.assertIn(
            f"[{web._CLICK_MARK}='1']", page.clicks, "Send was not pressed for real"
        )

    def test_enter_fallback_is_used_when_the_send_button_is_absent(self):
        page = _ComposerPage()

        async def run() -> None:
            with (
                _with_page(page),
                mock.patch.object(
                    web, "_press_send_button", new=mock.AsyncMock(return_value=False)
                ),
                mock.patch.object(
                    web, "_focus_composer", new=mock.AsyncMock(return_value=True)
                ),
            ):
                await web.press_send()

        asyncio.run(run())
        self.assertIn("Enter", page.keys)

    def test_enter_is_never_pressed_without_verified_composer_focus(self):
        page = _ComposerPage()

        async def run() -> None:
            with (
                _with_page(page),
                mock.patch.object(
                    web, "_press_send_button", new=mock.AsyncMock(return_value=False)
                ),
                mock.patch.object(
                    web, "_focus_composer", new=mock.AsyncMock(return_value=False)
                ),
            ):
                with self.assertRaises(web.SendNotAttempted):
                    await web.press_send()

        asyncio.run(run())
        self.assertNotIn("Enter", page.keys)

    def test_a_failed_button_press_does_not_fall_through_to_enter(self):
        page = _ComposerPage(click_fails=True)

        async def run() -> None:
            with _with_page(page):
                with self.assertRaises(ToolError):
                    await web.press_send()

        asyncio.run(run())
        self.assertNotIn("Enter", page.keys)

    def test_a_row_that_cannot_be_clicked_is_refused_not_assumed(self):
        # Playwright raises when the element is covered or detached. Assuming the
        # click worked is what let the old code report success and open nothing.
        page = FakePage(rendered=["Afia"], search_results=[])
        page.click_fails = True
        self.assertFalse(open_chat(page, "Afia"))

    def test_a_search_fallback_still_runs_when_the_row_click_fails(self):
        # Refusing outright would be safe but useless: the chat really is there, we
        # just could not press that particular row.
        #
        # `click` fails only for the *row* marker, because the search box is
        # focused by the same mechanism and has to keep working -- otherwise this
        # test would pass for the wrong reason, with the fallback unreachable.
        page = FakePage(rendered=["Afia"], search_results=["Afia"])
        page.click_fails_for = f"[{web._ROW_MARK}='1']"
        page.click_fail_times = 1
        self.assertTrue(open_chat(page, "Afia"))
        self.assertEqual(page.typed, ["afia"])
        self.assertFalse(page.filtering, "the filter outlived the call")


class ChatRowReaderTests(unittest.TestCase):
    """The chat list, read from the test ids this build actually renders.

    A live dump of the account's own chat list showed the whole row shape, and it
    matched **none** of the selectors the reader used to depend on::

        <div role="row" data-testid="list-item-0" class="x10l6tqk xh8yej3 x1g42fcv">
          <div data-testid="cell-frame-title">Dipto</div>
          <div data-testid="cell-frame-primary-detail">9/10/2026</div>
          <div data-testid="cell-frame-secondary">Khali mone hoy meye</div>
          <span data-testid="last-msg-status">Khali mone hoy meye</span>

    Hashed classes again, and **no `data-id` on any row**. So every chat came back
    with an empty `jid`, an empty `preview`, `group: false` and no unread count --
    four of the five fields read from selectors this build does not have, and none
    of it announced itself.
    """

    def setUp(self):
        self.script = web._JS_READ_CHATS

    def test_the_title_comes_from_the_title_cell(self):
        self.assertIn("cell-frame-title", self.script)

    def test_the_preview_and_the_date_come_from_their_own_cells(self):
        # Without these the whole row lands in `text` and the model is handed a
        # title glued to a date glued to a message, which is what "preview: ''"
        # looked like from the outside.
        self.assertIn("cell-frame-secondary", self.script)
        self.assertIn("cell-frame-primary-detail", self.script)

    def test_no_row_selector_depends_on_a_data_id_that_is_not_there(self):
        self.assertNotIn('data-id*="@c.us"', self.script)
        self.assertNotIn('data-id*="@g.us"', self.script)
        self.assertIn('data-testid^="list-item"', self.script)

    def test_search_and_browse_read_a_row_the_same_way(self):
        """A search result used to be a strictly poorer version of the row.

        `_JS_FILTERED_ROWS` hard-coded `unread: 0, preview: '', pinned: false,
        muted: false`. So "has Afia replied?" resolved the name through search and
        then came back with nothing to say about the chat -- resolve and read
        disagreeing about the same chat, in one breath.
        """
        self.assertIn("lumineChatRow", web._JS_FILTERED_ROWS)
        self.assertNotIn("preview: ''", web._JS_FILTERED_ROWS)
        self.assertNotIn("unread: 0,", web._JS_FILTERED_ROWS)

    def test_a_helper_is_declared_inside_the_body_not_concatenated_before_it(self):
        # Playwright evaluates a string as one expression, so a leading
        # `function f(){}` before `(x) => {}` parses as `f()(x) => {}` and the page
        # answers `ReferenceError: lumineChatRow is not defined` -- which it did,
        # on the first run of this code. The same trap as `_with_rows`, reached
        # twice because the fix was not written down anywhere a reader would see.
        for script in (self.script, web._JS_FILTERED_ROWS):
            with self.subTest(script=script[:14]):
                self.assertRegex(script.strip(), r"^\(\w+\)\s*=>\s*\{")
                self.assertEqual(script.count("function lumineChatRow"), 1)


def _strip_js_comments(script: str) -> str:
    """A script with its ``//`` and ``/* */`` comments removed.

    Needed because these modules explain their own bugs in comments -- which is
    right, and which makes a naive substring assertion fail on a *correct* file. The
    comment that documents a mistake must not be the thing that reintroduces it.
    """
    without_block = re.sub(r"/\*.*?\*/", "", script, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", without_block)


class GroupDetectionTests(unittest.TestCase):
    """A group must be told apart from a person whose name ends in a digit.

    The reader briefly derived the group flag from ``title.match(/(\\d+)$/)`` and
    so classified the account's real contact **"Me2"** as a two-person group. That
    is the worst direction for the error to go: the rule that a group is never
    silently substituted for a person would have refused a message to somebody by
    the shape of their name.

    A participant count is a **separate element** inside the title cell. A person's
    name is text. Same characters on screen; only the element test separates them.
    """

    def test_the_title_is_never_regexed_for_a_participant_count(self):
        """Asserted on the script with its comments stripped.

        The comment above the code explains this very bug by name, so a substring
        check finds ``title.match`` in the *explanation* and fails on a file that
        is correct -- which is how a substring test teaches you to delete the
        comment that stops the next person repeating the mistake.
        """
        for name in ("_JS_READ_CHATS", "_JS_FILTERED_ROWS"):
            with self.subTest(name=name):
                script = _strip_js_comments(getattr(web, name))
                self.assertNotIn(
                    "title.match", script, "a name ending in a digit is not a group"
                )

    def test_the_count_comes_from_an_element_of_its_own(self):
        script = _strip_js_comments(web._JS_READ_CHATS)
        self.assertIn("child.querySelector('span, div')", script)
        self.assertIn("participants = parseInt", script)

    def test_unknown_is_not_the_same_as_not_a_group(self):
        """The three-valued flag, and what happens when it collapses.

        With no `data-id` there is no JID, so a group is often indistinguishable
        from a contact. Reporting `false` for "could not tell" turns *unknown* into
        *permission*, and quietly switches off the rule that protects the user from
        messaging a group they named a person in.
        """
        from agent.tools.messaging.whatsapp_flow import ChatCandidate, kind_for_row

        self.assertEqual(kind_for_row({"group": True}), "group")
        self.assertEqual(kind_for_row({"group": False}), "contact")
        self.assertEqual(kind_for_row({"contact": True}), "contact")
        self.assertEqual(kind_for_row({"contact": True, "group": True}), "group")
        self.assertEqual(kind_for_row({}), "unknown")
        self.assertEqual(kind_for_row({"participants": 4}), "group")
        self.assertEqual(kind_for_row(None), "unknown")

        unknown = ChatCandidate(title="The Tech Titans", kind="unknown")
        contact = ChatCandidate(title="Afia", kind="contact")
        group = ChatCandidate(title="Team", kind="group")
        self.assertTrue(unknown.maybe_group)
        self.assertFalse(contact.maybe_group)
        self.assertTrue(group.maybe_group)
        # `is_group` stays a definite answer; `maybe_group` is what guards the send.
        self.assertFalse(unknown.is_group)

    def test_an_unknown_chat_asks_rather_than_sending(self):
        from agent.tools.messaging.whatsapp_flow import choose, ChatCandidate

        choice = choose("The Tech Titans", [ChatCandidate("The Tech Titans", kind="unknown")])
        self.assertIsNone(choice.chosen)
        self.assertTrue(choice.ambiguous)

    def test_a_known_contact_is_still_chosen_without_asking(self):
        from agent.tools.messaging.whatsapp_flow import choose, ChatCandidate

        choice = choose("Afia", [ChatCandidate("Afia", kind="contact")])
        self.assertIsNotNone(choice.chosen)


class PartialListTests(unittest.TestCase):
    """The chat list is virtualised, and pretending otherwise is a wrong answer.

    Only four rows existed in the DOM against an account with many more chats. So
    ``list_chats(cap=8)`` cannot return eight rows, and a list that quietly
    returned four of thirty is exactly how "who haven't I replied to?" comes back
    wrong.
    """

    def test_the_read_reports_whether_the_list_is_partial(self):
        import inspect as _inspect

        source = _inspect.getsource(web.read_chat_list)
        self.assertIn("partial", source)
        self.assertIn("rendered", source)

    def test_an_empty_unread_result_can_mean_i_could_not_tell(self):
        import inspect as _inspect

        # No row exposing a badge is not the same as no unread chats, and the
        # difference matters to somebody asking what they have not answered.
        self.assertIn("unreadReadable", _inspect.getsource(web.read_chat_list))

    def test_the_tool_tells_the_model_rather_than_truncating_silently(self):
        from agent.tools.messaging import whatsapp_read

        source = inspect.getsource(whatsapp_read._chat_rows_payload)
        self.assertIn("only the chats WhatsApp is currently showing", source)
        self.assertIn("Unread counts were not visible", source)


class FocusFallbackTests(unittest.TestCase):
    """Focus can be reached two ways; what decides is whether it landed.

    Measured on the live account: Playwright's click **times out** on the composer
    because something overlays it, and the actionability check -- visible, stable,
    *receives events*, enabled -- refuses rather than clicking through. That turned
    a perfectly good focus into "WhatsApp's layout has changed and I couldn't find
    the message box", on a chat that was open and whose composer the same function
    had just read successfully.

    So the press is no longer the only route, and the refusal sentence is no longer
    the answer to a focus problem. What decides is the confirmation, either way.
    """

    def test_a_refused_press_falls_back_to_focus_and_still_verifies(self):
        page = _ComposerPage()
        page.click_fails = True  # the overlay case

        async def run() -> Any:
            with _with_page(page):
                await web.type_message("hello")

        asyncio.run(run())
        self.assertEqual(page.focus_calls, 1, "the fallback must actually be used")

    def test_a_fallback_that_does_not_land_is_still_refused(self):
        # Two mechanisms and no verification would be worse than either: the
        # keystrokes would go to whatever *is* focused, which may be the search box.
        page = _ComposerPage(focus_fails=True)
        page.click_fails = True

        async def run() -> Any:
            with _with_page(page):
                with self.assertRaises(ToolError):
                    await web.type_message("hello")

        asyncio.run(run())

    def test_a_refusal_is_not_reported_as_a_layout_change(self):
        # The sentence matters as much as the outcome: "WhatsApp's layout has
        # changed" sends the user looking for a WhatsApp update when the problem
        # was a focus.
        import inspect as _inspect

        source = _inspect.getsource(web._focus)
        self.assertIn("falling back to focus()", source)
        self.assertNotIn("MSG_NO_TARGET", source)


class ComposerArrivalTests(unittest.TestCase):
    """The draft step waits for the message box instead of asking it once.

    Measured from the user's own log: ``open`` closed ``done`` at 4564 ms and
    ``draft`` failed **10 ms** later with "I can't see the message box on this
    chat. Nothing was typed." -- twice in a row, for the same two contacts, while
    a retry against an already-open chat went through.

    The open step had returned on its own *"proceeding on the press and relying on
    the composer check"* fallback, which is a promise that the composer check is
    what comes next. `require_ready` does not keep that promise: it waits for
    ``#pane-side``, the chat list, which is up long before a freshly opened
    conversation has drawn its message box. So the only gate left was a single
    read taken inside the gap, and it answered "not there" for a chat that has one.

    Three properties, and all three are wrong without a poll of their own: it
    *waits* for a box that is about to mount, it still *refuses* when there is
    none, and it does not make a healthy send wait for something it already has.
    """

    @staticmethod
    def _absent() -> dict[str, object]:
        return {"found": False, "text": "", "empty": True, "hasSend": False}

    @staticmethod
    async def _no_sleep(_seconds: float) -> None:
        # The wait is what is under test, so no wall time may be spent on it.
        return None

    def test_a_composer_that_mounts_a_moment_later_is_waited_for(self):
        page = _ComposerPage()
        misses = {"left": 2}

        async def late_composer() -> dict[str, object]:
            if misses["left"]:
                misses["left"] -= 1
                return self._absent()
            return page.state()

        with (
            _with_page(page),
            mock.patch.object(web, "composer", new=mock.AsyncMock(side_effect=late_composer)),
            mock.patch.object(web, "_sleep", new=self._no_sleep),
        ):
            asyncio.run(web.type_message("hello"))

        self.assertEqual(page.text, "hello", "the draft never reached the box")
        self.assertEqual(misses["left"], 0, "the poll gave up before the box arrived")

    def test_a_chat_with_no_composer_still_refuses_and_still_terminates(self):
        page = _ComposerPage()
        reads = {"count": 0}

        async def never_appears() -> dict[str, object]:
            reads["count"] += 1
            return self._absent()

        with (
            _with_page(page),
            mock.patch.object(web, "composer", new=mock.AsyncMock(side_effect=never_appears)),
            mock.patch.object(web, "_sleep", new=self._no_sleep),
        ):
            with self.assertRaises(ToolError) as caught:
                asyncio.run(web.type_message("hello"))

        self.assertEqual(str(caught.exception), web.MSG_NO_COMPOSER)
        self.assertEqual(page.text, "", "nothing was typed into a chat with no box")
        # More than one look is the whole fix; a ceiling is what stops it being a
        # spin. Both directions are failures, so both are asserted.
        self.assertGreater(reads["count"], 1, "it asked once and refused, as before")
        self.assertLessEqual(
            reads["count"],
            40,
            "a chat with no composer must not turn the refusal into a hang",
        )

    def test_a_composer_already_there_costs_no_wait_at_all(self):
        # The other direction, and the one that would be easy to break quietly:
        # `_poll` reads before it sleeps, so a page that is ready types on the
        # first look exactly as it did before the fix.
        page = _ComposerPage()
        sleeps: list[float] = []

        async def record_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        with _with_page(page), mock.patch.object(web, "_sleep", new=record_sleep):
            asyncio.run(web.type_message("hello"))

        self.assertEqual(page.text, "hello")
        self.assertEqual(
            sleeps, [], "a healthy send began waiting for a box it already had"
        )


class StaleMarkerTests(unittest.TestCase):
    """A mark left behind silently redirects the next press.

    The bug behind "she can select the chat but nothing gets typed", found only by
    running the real path. Every press marks its element with a data attribute and
    then asks Playwright to click ``[data-lumine-wa-focus='1']`` -- and the marker
    was never removed.

    So they accumulated. Resolving a name focuses the **search box**, leaving the
    attribute on it; focusing the composer marked a *second* element; and
    ``page.click`` resolved through ``querySelector``, which returns the **first**
    match in document order -- the search box, which sits above the composer. Every
    press went to the search box, so the confirmation asked whether the composer had
    taken focus, and it never could.

    Measured: `composer()` read the box successfully and `type_message` refused
    ~200 ms later with "WhatsApp's layout has changed and I couldn't find the message
    box" -- on every chat, every time. Nothing about the layout had changed.

    Every marker now clears all others before setting its own.
    """

    #: The markers that are cleared before they are set, and the reason each is
    #: dangerous to leave behind.
    MARKED = (
        "_FOCUS_MARK",
        "_CLICK_MARK",
        "_ROW_MARK",
        "_MENU_MARK",
        "_ITEM_MARK",
        "_ROW_MENU_MARK",
        "_CONFIRM_MARK",
        "_EMOJI_MARK",
    )

    def test_every_marker_is_cleared_before_it_is_set(self):
        source = inspect.getsource(web)
        marks = {
            value
            for name, value in vars(web).items()
            if name.endswith("_MARK") and isinstance(value, str)
        }
        self.assertGreaterEqual(len(marks), 8, "the marker list shrank -- check why")
        # Focus, click and the typing hint clear through the shared helper, which
        # takes the marker as an argument -- so their names are not written as
        # literals. Everything else inlines the removal, and every one of them
        # needs it.
        shared = {web._FOCUS_MARK, web._CLICK_MARK, web._TYPING_MARK}
        for mark in sorted(marks):
            with self.subTest(marker=mark):
                if mark in shared:
                    self.assertIn("old.removeAttribute(mark)", web._JS_MARK_ONE)
                    continue
                self.assertIn(
                    f"removeAttribute('{mark}')",
                    source,
                    f"{mark} is set without clearing the others, so a second press "
                    "goes to whichever element happens to come first in the document",
                )

    def test_the_clear_is_a_removal_not_just_a_lookup(self):
        # A `querySelectorAll` that collects but never removes would look right and
        # change nothing, so the test names the whole shape rather than the intent.
        source = inspect.getsource(web)
        self.assertIn("for (const old of document.querySelectorAll", source)
        self.assertIn("old.removeAttribute(mark)", web._JS_MARK_ONE)

    def test_the_row_marker_is_shared_by_two_modules_and_cleared_by_both(self):
        """The Web open step and the send path use the *same* attribute.

        Which was the sharp end: open a chat, then send to a different one, and the
        send path's press would hit the row opened a moment earlier -- a message
        prepared for one chat and pressed on another.
        """
        self.assertIn("removeAttribute('data-lumine-wa-row')", web._JS_MARK_ROW)
        # There is now **one** implementation. This used to be two -- one here and
        # one in the send path -- and they drifted into disagreeing about whether a
        # chat not currently rendered can be opened at all. So the send tool
        # resolved a contact through WhatsApp's search box and then refused to open
        # it: `find` returned `chat: "Me2"` and 400 ms later `open_chat("Me2")`
        # reported "nothing matched".
        #
        # Asserted as an *absence*, because a second copy that happens to agree
        # today is the state this bug was found in.
        self.assertFalse(
            hasattr(send_path, "_JS_MARK_CHAT_ROW"),
            "a second row marker beside the Web one is how the two drifted; "
            "`whatsapp.open_chat` must delegate instead",
        )
        self.assertEqual(send_path._ROW_SELECTOR, web._ROW_SELECTOR)
        self.assertEqual(send_path._ROW_MARK, web._ROW_MARK)

    def test_the_header_rule_lives_on_the_one_implementation(self):
        """Refuse a mismatch, proceed on an unreadable header -- and only once.

        This rule was written for the send path and **lost** when two copies of
        ``open_chat`` were merged: the Web copy returned ``False`` whenever the
        title did not match inside the window, so an unreadable header became
    indistinguishable from a wrong chat.

    That matters because the two copies answered differently about the same page
    -- the send tool ran the copy with the rule and every read ran the one without
    -- and the symptom is a capability disappearing ("I can find Afia but cannot
    select her") rather than an error anybody can read.
        """
        source = inspect.getsource(web.open_chat)
        self.assertIn("saw_a_header", source)
        self.assertIn("proceeding on the press", source)

        # And the send path must not carry its own copy of the rule, because a
        # second copy is what let the two drift in the first place.
        send_source = inspect.getsource(send_path.open_chat)
        self.assertNotIn("saw_a_header", send_source)

    def test_the_send_path_delegates_its_open_chat(self):
        """One name, one implementation, reached through the Web copy.

        The wrapper is kept so the symbol ``web_backend`` binds still exists; what
        matters is that it forwards rather than re-deriving.
        """
        source = inspect.getsource(send_path.open_chat)
        self.assertIn("_web_open_chat", source)
        self.assertNotIn("_JS_MARK_CHAT_ROW", source)
        # And it forwards the whole name rather than a part of it -- the bug was a
        # resolved title reaching a narrower matcher, not a bad name.
        self.assertIn('str(name or "")', source)


class NumberFromJidTests(unittest.TestCase):
    """A contact's WhatsApp identity *is* its number, which is how a name becomes
    something a `whatsapp://` link can be built from.

    Every refusal below is a case where a number could be produced and must not
    be. A deeplink built from a wrong number opens a stranger's chat with the
    user's message already typed into it.
    """

    def test_a_contact_jid_is_its_number(self):
        self.assertEqual(web.number_from_jid("15551234567@c.us"), "15551234567")

    def test_a_group_has_no_number(self):
        self.assertEqual(web.number_from_jid("15551234567-1600000000@g.us"), "")

    def test_a_device_suffix_is_not_part_of_the_number(self):
        # The same person saved on a linked device, not a different person.
        self.assertEqual(web.number_from_jid("15551234567:12@c.us"), "15551234567")

    def test_a_broadcast_or_newsletter_identity_is_refused(self):
        for jid in ("15551234567@broadcast", "1234567890@newsletter", "status@broadcast"):
            with self.subTest(jid=jid):
                self.assertEqual(web.number_from_jid(jid), "")

    def test_nonsense_never_becomes_a_number(self):
        for jid in ("", None, "not-a-jid", "@c.us", "abcdef@c.us", "12@c.us"):
            with self.subTest(jid=jid):
                self.assertEqual(web.number_from_jid(jid), "")

    def test_non_ascii_digits_do_not_pass(self):
        # Arabic-Indic digits satisfy a naive isdigit() and would be handed to
        # the OS as a URL. The deep link has to be digits the OS will dial.
        self.assertEqual(web.number_from_jid("١٥٥٥١٢٣٤٥٦٧@c.us"), "")


#: The live header, exactly as measured by walking the real DOM. This is the
#: fixture every assertion below is about, kept as a literal so a reader can see
#: what "two things in one header" means without running anything.
MEASURED_HEADER = {
    "tag": "div",
    "testid": "conversation-info-header",
    "title": None,
    "textContent": "Me2click here for contact info",
    "children": [
        {"tag": "div", "testid": None, "title": None, "textContent": "Me2"},
        {
            "tag": "div",
            "testid": "chat-subtitle",
            "title": None,
            "textContent": "click here for contact info",
        },
    ],
}


class ChatHeaderReaderTests(unittest.TestCase):
    """Which chat is open -- read so it can *match*.

    `active_chat()` is what `open_chat` compares against, and that comparison
    refuses a mismatch on purpose: a mismatch means the conversation under the
    cursor is not the one that was resolved, so a message typed there goes to
    somebody who never agreed to receive it. The check is only worth having if the
    comparison is capable of succeeding.
    """

    def test_the_header_holds_two_things_not_one(self):
        # The premise, asserted so the fix below cannot be read as a workaround
        # for a problem that does not exist. `textContent` is the *whole* header.
        self.assertEqual(
            MEASURED_HEADER["textContent"], "Me2click here for contact info"
        )
        self.assertEqual(
            [child["testid"] for child in MEASURED_HEADER["children"]],
            [None, "chat-subtitle"],
            "the hint is a sibling, not a descendant of the name",
        )

    def test_the_reader_excludes_the_subtitle_rather_than_parsing_the_string(self):
        # Naming the exclusion is the fix. `replace('click here for contact
        # info', '')` would be a guess about wording, and WhatsApp localises it --
        # a user whose UI is not English gets the concatenated title back and the
        # same permanent refusal.
        source = web._JS_ACTIVE_CHAT
        self.assertIn("chat-subtitle", source)
        self.assertIn(':not([data-testid="chat-subtitle"])', source)

    def test_the_named_child_is_tried_before_the_whole_header(self):
        # Order matters. Falling back to `header.textContent` *first* would read
        # the concatenation and the exclusion would never be reached.
        source = web._JS_ACTIVE_CHAT
        self.assertLess(
            source.index(":scope > *:not"),
            source.index("header.getAttribute('title') || header.textContent"),
        )

    def test_a_header_with_no_subtitle_still_reads(self):
        # The exclusion must not become a requirement: a build that drops the
        # subtitle, or a group header shaped differently, has to keep working.
        # With no matching child the reader falls back to the header's own title
        # attribute or text, which is correct for a header holding one thing.
        source = web._JS_ACTIVE_CHAT
        self.assertIn("header.getAttribute('title') || header.textContent", source)
        self.assertIn("if (!header) return { title: '', raw: '', open: false }", source)

    def test_a_missing_header_is_not_an_open_chat(self):
        # `open` is what separates "WhatsApp is showing nothing" from "WhatsApp is
        # showing a chat whose name we could not parse". Collapsing them is how an
        # unreadable title starts reading as a match.
        source = web._JS_ACTIVE_CHAT
        self.assertIn("open: !!document.querySelector('#main') && !!title", source)

    def test_the_header_gets_the_same_phone_number_strip_the_rows_do(self):
        """A contact who has never been messaged shows their number in the header.

        Measured: opening `Jubayed Reza` renders the header as
        `'Jubayed Reza (22-23)'`. The row reader already strips it, so the row said
        `Jubayed Reza` while the header said `Jubayed Reza (22-23)' -- and a
        mismatch is *refused*. That is the whole of "she opened the chat and then
        said she couldn't".

        Both forms are reported, and `open_chat` accepts either, so a chat really
        called `'Team (2024)'` keeps working without this rule existing in Python
        as a second copy.
        """
        source = web._JS_ACTIVE_CHAT
        self.assertIn("lumineContactName(flat)", source)
        self.assertIn("raw: flat", source)

    def test_one_helper_serves_the_rows_the_marker_and_the_header(self):
        # Three readers, one definition. Two copies is how the row started
        # reporting a name the marker could not match.
        for name in ("_JS_CHAT_ROW_FN", "_JS_MARK_ROW", "_JS_ACTIVE_CHAT"):
            with self.subTest(script=name):
                self.assertEqual(
                    getattr(web, name).count("function lumineContactName"), 1
                )

    def test_the_title_is_whitespace_normalised(self):
        # The chat list builds names from squashed text and the comparison is a
        # plain `==`, so a stray double space from the DOM would be a mismatch.
        self.assertIn("replace(/\\s+/g, ' ').trim()", web._JS_ACTIVE_CHAT)


class ScriptShapeTests(unittest.TestCase):
    """The in-page scripts have to *parse*, and Python cannot see that.

    Two separate faults in this one session, both invisible to every other test in
    the file, and both of them total -- the affected script did nothing at all:

    - A function declaration concatenated in front of an arrow function. Playwright
      evaluates a string argument as a single expression, so
      `function f(){...}(x) => {...}` is a `SyntaxError`.
    - A wrapped script whose body never closed the arrow function, because
      `_with_chat_row` opens the brace and expects the body to close it.

    The symptom each time was not a crash: `open_chat` returned ``False`` for chats
    that opened perfectly well when clicked by hand, and the tool reported "I
    couldn't read your chat list" for a contact it had just found on screen. A
    balance check is not a JavaScript parser, but it catches exactly the mistake
    that keeps being made here, and it costs nothing.
    """

    #: Every script that :func:`web._with_rows` / :func:`web._with_chat_row` build.
    #: These are the ones with a wrapper to get wrong.
    WRAPPED = (
        "_JS_READ_CHATS",
        "_JS_FILTERED_ROWS",
        "_JS_MARK_ROW",
        "_JS_OPEN_MESSAGE_MENU",
        "_JS_READ_MESSAGES",
    )

    @staticmethod
    def _balanced(script: str) -> bool:
        depth = 0
        in_string = in_line_comment = False
        quote = ""
        index = 0
        while index < len(script):
            char = script[index]
            nxt = script[index + 1] if index + 1 < len(script) else ""
            if in_line_comment:
                if char == "\n":
                    in_line_comment = False
            elif in_string:
                # A backslash escapes the next character, so an escaped quote does
                # not end the string.
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    in_string = False
            elif char == "/" and nxt == "/":
                in_line_comment = True
            elif char in "'\"`":
                in_string, quote = True, char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    return False
            index += 1
        return depth == 0 and not in_string

    def test_every_wrapped_script_has_balanced_braces(self):
        for name in self.WRAPPED:
            with self.subTest(script=name):
                script = getattr(web, name, None)
                self.assertTrue(
                    isinstance(script, str) and script.strip(),
                    f"{name} is missing or empty",
                )
                self.assertTrue(
                    self._balanced(script),
                    f"{name} does not balance -- the wrapper opens a brace and the "
                    "body must close it, or the page gets a SyntaxError",
                )

    def test_every_wrapped_script_is_one_expression(self):
        """No declaration may sit before the arrow function.

        `function f(){...}` followed by `(x) => {...}` parses as
        `f(){...}(x) => {...}`. That is why `_with_rows` exists, and why a helper
        cannot simply be prepended -- it has to go *inside* the body.
        """
        for name in self.WRAPPED:
            with self.subTest(script=name):
                script = getattr(web, name)
                head = script.lstrip()
                self.assertFalse(
                    head.startswith("function"),
                    f"{name} starts with a function declaration, so Playwright "
                    "evaluates it as a statement and the page answers SyntaxError",
                )
                self.assertTrue(
                    head.startswith("("),
                    f"{name} must be a single parenthesised expression",
                )

    def test_the_contact_name_helper_is_declared_once(self):
        """One definition, reached by both readers.

        The reader strips a contact's phone number off its title so the row can be
        matched and the header can agree. If the marker had its own copy, one of
        them would strip and the other would not, and the row would resolve and
        then be unclickable -- which is exactly what happened.
        """
        source = inspect.getsource(web)
        self.assertEqual(
            source.count("function lumineContactName"),
            1,
            "the helper is defined once and composed in; a second definition is "
            "how the reader and the marker start disagreeing again",
        )
        # And it must be inside the shared declaration block, not prepended.
        self.assertIn("function lumineContactName", web._JS_CHAT_ROW_FN)
        self.assertIn("lumineContactName(value)", web._JS_MARK_ROW)


class HeaderGraceTests(unittest.TestCase):
    """The composer shortcut must not fire on the *previous* chat's composer.

    A press changes the DOM over a few frames, and in that window the conversation
    that was open before is still fully present -- header, composer, everything.
    So "a composer exists" says nothing about whether the right conversation
    opened, and believing it immediately means the next step types into the wrong
    chat. That is the one mistake `open_chat` exists to prevent, and it arrived
    *via* the optimisation meant to remove four seconds of waiting.

    The symptom the user reported was its milder cousin: the first attempt after a
    different chat reporting a failure or a success inconsistently, and the second
    attempt working, because by then the page had settled.
    """

    class _Clock:
        """A clock the test moves by sleeping, so no wall time is involved."""

        def __init__(self) -> None:
            self.now = 1000.0
            self.composer_checks: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += float(seconds)

        async def wait(self, seconds: float) -> None:
            # `_sleep` is awaited, so the double must be too.
            self.sleep(seconds)

    def _run(self, *, header: str, composer: bool, timeout: float = 4.0):
        clock = self._Clock()

        async def composer_ready() -> bool:
            clock.composer_checks.append(clock.now)
            return composer

        page = mock.Mock()

        async def evaluate(script, *args, **kwargs):
            del args, kwargs
            if "conversation-info-header" in script:
                return {"title": header, "raw": header, "open": bool(header)}
            if "data-lumine-wa-row" in script:
                # The row press lands, so `open_chat` gets past `_click_row` and
                # reaches the header wait that is actually under test here.
                return True
            return {}

        page.evaluate = evaluate
        page.click = mock.AsyncMock(return_value=None)

        with (
            mock.patch.object(web, "require_ready", new_callable=mock.AsyncMock),
            mock.patch.object(web, "whatsapp_page", new_callable=mock.AsyncMock, return_value=page),
            mock.patch.object(web, "_composer_is_ready", new=composer_ready),
            mock.patch.object(web, "_sleep", new=clock.wait),
            mock.patch.object(web.time, "monotonic", new=clock.monotonic),
        ):
            verdict = asyncio.run(web.open_chat("Jubayed Reza", timeout=timeout))
        return verdict, clock

    def test_the_composer_is_not_believed_before_the_page_has_settled(self):
        verdict, clock = self._run(header="", composer=True)
        self.assertTrue(verdict)
        self.assertTrue(clock.composer_checks, "the composer was never consulted")
        first = clock.composer_checks[0] - 1000.0
        self.assertGreaterEqual(
            first,
            web._HEADER_GRACE_SECONDS,
            "the shortcut fired inside the settle window, where the previous "
            "conversation's composer is still on screen",
        )

    def test_the_shortcut_still_saves_the_wait(self):
        """The reason it exists: a chat that will never have a header.

        Measured before the grace period: 4.94s, the full timeout, every time.
        """
        _verdict, clock = self._run(header="", composer=True)
        elapsed = clock.now - 1000.0
        self.assertLess(
            elapsed,
            3.0,
            "the wait is back to sitting out the whole timeout for a contact who "
            "has no conversation header",
        )

    def test_a_mismatched_header_refuses_without_asking_about_the_composer(self):
        # The refusal has to stay the first thing that happens. A composer is
        # evidence that *somewhere* to type exists, never evidence of *which*
        # conversation is open.
        verdict, clock = self._run(header="Siam Sheikh", composer=True)
        self.assertFalse(verdict)
        self.assertEqual(
            clock.composer_checks,
            [],
            "a readable header naming a different chat must refuse on its own",
        )

    def test_switching_from_an_open_chat_is_not_a_mismatch(self):
        """The case the user finally pinned down, and only this case.

        Ask for a contact *while another conversation is already open*. The press
        lands, and for a few hundred milliseconds the header still names the
        **previous** chat. Refusing on that read discards a click that worked --
        which is what produced "she searched it, then opened it, saying she can't
        open that", and why a second attempt (when the page had settled) always
        succeeded.
        """
        clock = self._Clock()
        seen: list[str] = []
        # The previous chat for the first reads, then the one we asked for.
        script = ["Siam Sheikh", "Siam Sheikh", "Jubayed Reza"]

        page = mock.Mock()

        async def evaluate(script_text, *args, **kwargs):
            del args, kwargs
            if "conversation-info-header" in script_text:
                title = script[min(len(seen), len(script) - 1)]
                seen.append(title)
                return {"title": title, "raw": title, "open": True}
            if "data-lumine-wa-row" in script_text:
                return True
            return {}

        page.evaluate = evaluate
        page.click = mock.AsyncMock(return_value=None)

        async def composer_ready() -> bool:
            return True

        with (
            mock.patch.object(web, "require_ready", new_callable=mock.AsyncMock),
            mock.patch.object(web, "whatsapp_page", new_callable=mock.AsyncMock, return_value=page),
            mock.patch.object(web, "_composer_is_ready", new=composer_ready),
            mock.patch.object(web, "_sleep", new=clock.wait),
            mock.patch.object(web.time, "monotonic", new=clock.monotonic),
        ):
            verdict = asyncio.run(web.open_chat("Jubayed Reza", timeout=4.0))

        self.assertTrue(
            verdict,
            "the previous chat's header was read as the wrong chat, so a "
            "successful click was refused",
        )
        self.assertIn("Siam Sheikh", seen, "the scenario never exercised the race")

    def test_no_header_and_no_composer_keeps_waiting_then_proceeds_on_the_press(self):
        # The original rule, unchanged: the press landed, and the composer read
        # will be the next line of defence when the text is typed.
        verdict, clock = self._run(header="", composer=False, timeout=2.0)
        self.assertTrue(verdict)
        self.assertGreaterEqual(clock.now - 1000.0, 2.0)


class StateReaderTests(unittest.TestCase):
    """One reader of "is WhatsApp signed in", and it waits.

    There were two. `whatsapp.py` looked at `#pane-side` exactly once and called
    everything else `"logged_out"`; `whatsapp_web.py` polled and reported `loading`
    as its own answer. The send path asked the *first* one, so on a cold tab --
    several seconds of splash, which is exactly what the pre-warm exists to cover
    -- a signed-in account was told to scan a QR code it had already scanned.
    """

    def test_whatsapp_state_delegates_to_the_polling_reader(self):
        with mock.patch.object(
            web, "state", new_callable=mock.AsyncMock, return_value="ready"
        ) as reader:
            self.assertEqual(asyncio.run(send_path.whatsapp_state()), "ready")
        reader.assert_awaited_once()

    def test_there_is_no_one_shot_reader_left_to_reach_for(self):
        # Named, not deleted: "we removed the naive one" is the useful fact, and a
        # future edit that reaches for it should find a comment explaining why it
        # went.
        self.assertIn("qrcode", send_path._JS_STATE_REMOVED)
        self.assertFalse(
            hasattr(send_path, "_JS_STATE"),
            "a second reader of the sign-in state is the fault being fixed",
        )

    def test_a_splash_screen_never_becomes_a_request_to_scan_a_code(self):
        for verdict in ("loading",):
            with self.subTest(state=verdict):
                with mock.patch.object(
                    send_path, "whatsapp_state", new_callable=mock.AsyncMock,
                    return_value=verdict,
                ):
                    why = asyncio.run(send_path.web_backend().check())
                self.assertIsNotNone(why)
                self.assertNotIn("QR code", why)
                self.assertIn("still loading", why)

    def test_only_a_real_signed_out_page_asks_for_a_code(self):
        with mock.patch.object(
            send_path, "whatsapp_state", new_callable=mock.AsyncMock,
            return_value="logged_out",
        ):
            why = asyncio.run(send_path.web_backend().check())
        self.assertIn("QR code", why)

    def test_ready_asks_nothing(self):
        with mock.patch.object(
            send_path, "whatsapp_state", new_callable=mock.AsyncMock, return_value="ready"
        ):
            self.assertIsNone(asyncio.run(send_path.web_backend().check()))


if __name__ == "__main__":
    unittest.main()
