"""WhatsApp Desktop adapter: its rules, decided without the app.

Everything here runs offline against fakes and pure functions: the
keystroke shape (the emoji case is why this file exists), the row
structure that separates a chat from navigation furniture, the
three-part capability check, and the watchdog that keeps a wedged
application from holding the session hostage.

`LiveAdapterTests` is the deliberate exception: it reads the real chat
list, and only when the user already has the window *showing* -- a test
run must not pop a tray app open, click a chat (that would clear an
unread badge), or type a word. What it cannot cover without a person at
a real account -- opening a chat, the composer, pressing send -- stays
a manual check, labeled as one, rather than being faked here.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.tools.desktop import uia
from agent.tools.messaging import whatsapp_desktop as desktop
from agent.tools.tools_compat import ToolError


def rect(left: int, top: int, right: int, bottom: int) -> SimpleNamespace:
    return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)


class FakeNode:
    """Just enough of a UIA control for the tree-reading functions."""

    def __init__(self, kind: str, name: str = "", box=None, children=()):
        self.ControlTypeName = kind
        self.Name = name
        self.BoundingRectangle = box
        self._children = list(children)

    def GetChildren(self):
        return list(self._children)


def chat_row(title: str, when: str, box) -> FakeNode:
    """A conversation row the way the real app exposes one: an inner
    wrapper carrying title-then-timestamp, exactly as probed."""
    return FakeNode(
        "DataItemControl",
        name=f"{title} {when}",
        box=box,
        children=[
            FakeNode(
                "DataItemControl",
                name=f"{title} {when}",
                box=box,
                children=[
                    FakeNode("TextControl", name=title, box=box),
                    FakeNode("TextControl", name=when, box=box),
                ],
            )
        ],
    )


def unread_row(title: str, when: str, badge: str, box) -> FakeNode:
    """A row with an unread badge *beside* the inner wrapper -- the shape
    that would make a naive walk take the badge for the chat's name."""
    row = chat_row(title, when, box)
    row._children.append(FakeNode("TextControl", name=badge, box=box))
    return row


class KeystrokeTests(unittest.TestCase):
    """What `type_message` actually sends to the keyboard.

    The emoji case is the whole reason this is not the library's
    `SendKeys`: that parser truncates a code point outside the BMP into
    its 16-bit scan field, so typing ``"😋"`` would write U+F60B -- a
    different, wrong character -- into the user's real message. These
    assertions pin the surrogate-pair shape that gets it right, and the
    wrong value explicitly, so the fix cannot silently regress.
    """

    @staticmethod
    def scans(text: str) -> list[int]:
        return [event.union.ki.wScan for event in desktop._keystrokes(text)]

    def test_plain_characters_are_one_event_each(self):
        self.assertEqual(self.scans("ab"), [ord("a"), ord("b")])

    def test_an_emoji_becomes_a_surrogate_pair_not_a_truncated_code_point(self):
        self.assertEqual(self.scans("😋"), [0xD83D, 0xDE0B])
        # 0xF60B is what a 16-bit truncation of U+1F60B produces; it must
        # never appear, because that is the bug this path replaced.
        self.assertNotIn(0xF60B, self.scans("😋"))

    def test_every_event_is_raw_unicode_with_no_virtual_key(self):
        events = desktop._keystrokes("a😋b")
        self.assertEqual(
            [event.union.ki.wScan for event in events],
            [0x61, 0xD83D, 0xDE0B, 0x62],
        )
        unicode_flag = desktop._driver().KeyboardEventFlag.KeyUnicode
        for event in events:
            self.assertEqual(event.union.ki.dwFlags, unicode_flag)
            self.assertEqual(event.union.ki.wVk, 0)


class RowStructureTests(unittest.TestCase):
    """What counts as a chat, and which of its texts is the name."""

    def test_the_title_is_the_first_text_and_the_timestamp_is_not_it(self):
        row = chat_row("Nur Nasid Dipto", "6:48 am", rect(0, 0, 100, 40))
        self.assertEqual(
            desktop._row_title(row, [desktop.WALK_BUDGET]), "Nur Nasid Dipto"
        )

    def test_a_row_whose_first_text_would_be_its_badge_still_names_the_chat(self):
        # Unread rows carry the badge next to the title wrapper. Visiting
        # siblings in reverse would report "3" as the contact's name --
        # and a contact named "3" is a wrong send waiting to happen.
        row = unread_row("Alice", "6:48 am", "3", rect(0, 0, 100, 40))
        self.assertEqual(desktop._row_title(row, [desktop.WALK_BUDGET]), "Alice")

    def test_a_row_with_one_text_is_furniture_not_a_conversation(self):
        # "Archived", a section heading: exactly one text, and it must
        # never appear as somebody you could send to.
        label = FakeNode(
            "DataItemControl",
            name="Archived",
            box=rect(0, 0, 100, 30),
            children=[FakeNode("TextControl", name="Archived", box=rect(0, 0, 100, 30))],
        )
        self.assertIsNone(desktop._row_title(label, [desktop.WALK_BUDGET]))

    def test_whitespace_collapses_but_the_words_survive(self):
        row = FakeNode(
            "DataItemControl",
            box=rect(0, 0, 100, 40),
            children=[
                FakeNode("TextControl", name="  Family   Group \n", box=rect(0, 0, 100, 40)),
                FakeNode("TextControl", name="Yesterday", box=rect(0, 0, 100, 40)),
            ],
        )
        self.assertEqual(desktop._row_title(row, [desktop.WALK_BUDGET]), "Family Group")

    def test_duplicate_branches_of_one_row_collapse_to_a_single_chat(self):
        # Chromium exposes each row in several flattened branches; the
        # bounding rectangle is what they agree on, so it is the key.
        box = rect(10, 10, 200, 60)
        root = FakeNode(
            "PaneControl",
            children=[
                FakeNode("GroupControl", children=[chat_row("Alice", "6:48 am", box)]),
                FakeNode("GroupControl", children=[chat_row("Alice", "6:48 am", box)]),
            ],
        )
        out: list = []
        desktop._rows_in(root, out, set(), [desktop.WALK_BUDGET])
        self.assertEqual([title for title, _node in out], ["Alice"])

    def test_distinct_rows_all_arrive_in_document_order(self):
        root = FakeNode(
            "PaneControl",
            children=[
                chat_row("Alice", "6:48 am", rect(10, 10, 200, 60)),
                chat_row("Bob", "Yesterday", rect(10, 70, 200, 120)),
            ],
        )
        out: list = []
        desktop._rows_in(root, out, set(), [desktop.WALK_BUDGET])
        self.assertEqual([title for title, _node in out], ["Alice", "Bob"])

    def test_the_walk_stops_at_the_title_limit(self):
        root = FakeNode(
            "PaneControl",
            children=[
                chat_row(f"Chat {index}", "6:48 am", rect(10, 10 + index, 200, 60 + index))
                for index in range(desktop.MAX_TITLES + 5)
            ],
        )
        out: list = []
        desktop._rows_in(root, out, set(), [desktop.WALK_BUDGET])
        self.assertEqual(len(out), desktop.MAX_TITLES)

    def test_a_chat_below_the_cap_is_found_when_it_is_the_one_asked_for(self):
        # The cap counts *matches*. Filtering after the cut would look at
        # only the first twelve chats and report "no such contact" about
        # somebody sitting at number fifteen -- a false answer to a
        # question the tree could have answered.
        root = FakeNode(
            "PaneControl",
            children=[
                chat_row(f"Chat {index}", "6:48 am", rect(10, 10 + index, 200, 60 + index))
                for index in range(desktop.MAX_TITLES + 3)
            ],
        )
        out: list = []
        desktop._rows_in(
            root, out, set(), [desktop.WALK_BUDGET], wanted=f"chat {desktop.MAX_TITLES + 2}"
        )
        self.assertEqual([title for title, _node in out], [f"Chat {desktop.MAX_TITLES + 2}"])

    def test_a_row_we_cannot_place_is_a_row_we_do_not_claim(self):
        # No rectangle means no way to deduplicate it against its own
        # duplicate branches -- so it is skipped rather than risk matching
        # the same chat (or the wrong one) twice.
        root = FakeNode(
            "PaneControl",
            children=[chat_row("Alice", "6:48 am", box=None)],
        )
        out: list = []
        desktop._rows_in(root, out, set(), [desktop.WALK_BUDGET])
        self.assertEqual(out, [])


class CapabilityTests(unittest.TestCase):
    """Three facts, cheapest first, each with its own actionable refusal."""

    def test_a_non_windows_machine_says_so(self):
        with mock.patch.object(sys, "platform", "linux"):
            answer = desktop.desktop_capability()
        self.assertFalse(answer["available"])
        self.assertIn("Windows", answer["reason"])

    def test_a_missing_driver_names_the_package_to_install(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(
            desktop.importlib.util, "find_spec", return_value=None
        ):
            answer = desktop.desktop_capability()
        self.assertFalse(answer["available"])
        self.assertIn("uiautomation", answer["reason"])

    def test_a_closed_app_says_it_is_not_running(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(
            desktop.importlib.util, "find_spec", return_value=mock.sentinel.spec
        ), mock.patch.object(desktop, "_main_window", return_value=None):
            answer = desktop.desktop_capability()
        self.assertFalse(answer["available"])
        self.assertIn("isn't running", answer["reason"])

    def test_all_three_present_is_available(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(
            desktop.importlib.util, "find_spec", return_value=mock.sentinel.spec
        ), mock.patch.object(desktop, "_main_window", return_value=1234):
            answer = desktop.desktop_capability()
        self.assertEqual(answer, {"available": True, "reason": ""})

    def test_the_capability_is_recomputed_rather_than_remembered(self):
        # The window is found, then closed, then asked again: a yes from a
        # minute ago is exactly the stale claim this product must not make.
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(
            desktop.importlib.util, "find_spec", return_value=mock.sentinel.spec
        ), mock.patch.object(
            desktop, "_main_window", side_effect=[1234, None]
        ) as window:
            self.assertTrue(desktop.desktop_capability()["available"])
            self.assertFalse(desktop.desktop_capability()["available"])
        self.assertEqual(window.call_count, 2)

    def test_every_state_token_has_a_sentence(self):
        self.assertEqual(desktop.state_reason("not_running"), desktop.MSG_NOT_RUNNING)
        self.assertEqual(desktop.state_reason("blind"), desktop.MSG_BLIND)
        self.assertEqual(desktop.state_reason("surprise"), desktop.MSG_BLIND)


class SearchResolveTests(unittest.TestCase):
    """Reading the *filtered* list -- the wrong-recipient guard.

    The number path resolves a person by taking search's first row. If
    the read lands before WhatsApp's filter does, that row is simply
    whoever sorts first, and "first row" stops meaning "best match" --
    which is how a message gets aimed at the wrong chat. These tests pin
    the baseline rule: a list still identical to the pre-search one is
    not believed, whatever it contains.
    """

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        # No real window, no real typing, no real focus -- and no sleep
        # between polls, since the rule under test is about *what* is
        # read, not how fast.
        self.stack.enter_context(
            mock.patch.object(desktop, "_main_window", return_value=9999)
        )
        self.stack.enter_context(
            mock.patch.object(desktop, "_find_search", return_value=object())
        )
        self.stack.enter_context(mock.patch.object(desktop, "_type_into"))
        self.stack.enter_context(
            mock.patch.object(desktop, "_foreground_pid", return_value=None)
        )
        self.stack.enter_context(mock.patch.object(desktop, "_pid_of", return_value=1))
        self.stack.enter_context(
            mock.patch.object(desktop, "SEARCH_POLL_SECONDS", 0.0)
        )

    @staticmethod
    def _titles(*titles: str) -> list:
        return [(title, object()) for title in titles]

    def test_a_read_that_predates_the_filter_is_not_believed(self):
        # Baseline and first poll are identical: the filter had not
        # landed, so the first row means nothing yet. The second poll
        # differs -- now the list is the filter's answer.
        polls = iter(
            [
                self._titles("Alice", "Bob"),  # baseline, before typing
                self._titles("Alice", "Bob"),  # poll: stale, must not be believed
                self._titles("Carol"),  # poll: the filter's actual answer
            ]
        )
        with mock.patch.object(desktop, "_rows", side_effect=lambda *a, **k: next(polls)):
            self.assertEqual(desktop._search_sync("15551234567"), "Carol")

    def test_a_search_that_changes_nothing_is_no_result(self):
        # Eight polls of "the list still looks exactly like it did before
        # typing" = the filter never arrived = we learned nothing about
        # who this number matches. None, not the chat that sorts first.
        stale = self._titles("Alice", "Bob")
        with mock.patch.object(desktop, "_rows", return_value=stale):
            self.assertIsNone(desktop._search_sync("15551234567"))

    def test_a_shrinking_list_counts_as_filtered_even_for_its_first_row(self):
        # The match was already near the top; the filter's proof is that
        # everything *else* disappeared.
        polls = iter(
            [
                self._titles("Alice", "Bob", "Carol"),  # baseline
                self._titles("Alice"),  # poll: narrowed -- and Alice is the match
            ]
        )
        with mock.patch.object(desktop, "_rows", side_effect=lambda *a, **k: next(polls)):
            self.assertEqual(desktop._search_sync("15551234567"), "Alice")

    def test_an_empty_result_keeps_polling_then_gives_up_cleanly(self):
        # "No chats found" is the filter's answer too -- it just is not a
        # person. None means "not one of your chats", never a guess.
        polls = iter(
            [
                self._titles("Alice"),  # baseline
                [],  # poll: filtered to nothing
                [],  # poll: still nothing
            ]
        )
        with mock.patch.object(desktop, "_rows", side_effect=lambda *a, **k: next(polls)):
            with mock.patch.object(desktop, "SEARCH_POLLS", 2):
                self.assertIsNone(desktop._search_sync("15551234567"))


class WatchdogTests(unittest.TestCase):
    """A call over its deadline cools the harness down instead of hanging.

    Exercised against `desktop.uia` rather than this module, because that is
    where the worker thread, the deadline, and the cooldown now live. The
    behaviour under test did not move -- one worker thread and a shared
    cooldown for every UI Automation path -- and `test_desktop_uia.py` covers
    it from the harness's own side. What is asserted here is that WhatsApp
    still goes through it, which is the thing this module owns.
    """

    def setUp(self):
        uia._COOL_UNTIL = 0.0

    def tearDown(self):
        uia._COOL_UNTIL = 0.0

    def test_an_overrunning_call_refuses_the_next_ones_too(self):
        def slow():
            time.sleep(0.4)
            return "too late"

        with mock.patch.object(uia, "CALL_TIMEOUT_SECONDS", 0.05):
            with self.assertRaisesRegex(ToolError, "stopped answering"):
                asyncio.run(desktop._run(slow))

        # The single worker may still be stuck behind that call. Later
        # calls must refuse fast -- so the shared sequence can fall back
        # or fail closed -- rather than queueing behind a wedged app.
        self.assertGreater(uia._COOL_UNTIL, time.monotonic())
        ran: list = []
        with self.assertRaisesRegex(ToolError, "stopped answering"):
            asyncio.run(desktop._run(lambda: ran.append("never")))
        self.assertEqual(ran, [])

    def test_whatsapp_and_the_desktop_tool_share_one_worker(self):
        """Two harnesses would be two places for a wedged app to hide.

        The alias is what makes this true rather than merely intended: both
        modules must be reaching the same pool and the same cooldown, so a
        WhatsApp call that times out stops the desktop path too instead of
        leaving it to queue behind a thread that is never coming back.
        """
        self.assertIs(desktop._driver, uia.driver)
        self.assertIs(desktop._run, uia.run)
        self.assertIs(desktop._keystrokes, uia.keystrokes)
        self.assertIs(desktop._texts_of, uia.texts_of)
        self.assertIs(desktop._pids_named, uia.pids_named)
        self.assertIs(desktop._window_rect, uia.window_rect)

    def test_a_call_within_the_deadline_returns_normally(self):
        self.assertEqual(asyncio.run(desktop._run(lambda: "fine")), "fine")


@unittest.skipUnless(sys.platform == "win32", "WhatsApp Desktop is a Windows app")
class LiveAdapterTests(unittest.TestCase):
    """The real app, read only -- and only if the user already has it open.

    Skipped when the window is hidden or minimized: revealing a tray app
    during a unit run is exactly the surprise this product should not
    spring on someone. Nothing here clicks, types, or sends.
    """

    def setUp(self):
        if sys.platform != "win32":
            self.skipTest("WhatsApp Desktop is a Windows app")
        capability = desktop.desktop_capability()
        if not capability["available"]:
            self.skipTest(f"desktop path unavailable: {capability['reason']}")
        import ctypes

        hwnd = desktop._main_window()
        user32 = ctypes.windll.user32
        if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            self.skipTest(
                "WhatsApp Desktop is not showing; a test run must not reveal it"
            )

    def test_state_reads_the_real_chat_list_as_ready(self):
        self.assertEqual(asyncio.run(desktop.state()), "ready")

    def test_contacts_reads_titles_without_touching_anything(self):
        titles = asyncio.run(desktop.list_contacts(""))
        self.assertIsInstance(titles, list)
        self.assertTrue(titles, "a signed-in app has a chat list")
        self.assertTrue(all(isinstance(title, str) and title for title in titles))
        self.assertLessEqual(len(titles), desktop.MAX_TITLES)


if __name__ == "__main__":
    unittest.main()
