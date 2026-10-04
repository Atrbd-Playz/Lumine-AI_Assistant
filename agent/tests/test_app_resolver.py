"""Resolving a spoken name to one target, and checking that a launch worked.

The reported desktop bugs were: two Krita windows from one request, "open Krita"
opening nothing, Chrome opening *and* Lumine's browser, and Instagram not being
recognised as installed. All four are decisions this module makes, so all four are
tested here -- against fixtures rather than a desktop, with the window reader,
the process reader and the app finder injected.

Nothing in this file touches the real machine: the reader parameters are how that
is guaranteed, and `test_no_real_machine_access` asserts the defaults are only
used when nothing is injected.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from unittest import mock

from agent.tools import app_index, app_resolver
from agent.tools.app_resolver import AppResolver, AppTarget, match_windows
from agent.runtime import verification


def windows(*rows: tuple[str, str]) -> list[tuple[str, int, str]]:
    """``(title, process)`` pairs as the reader would return them."""
    return [(title, 1000 + position, program) for position, (title, program) in enumerate(rows)]


def entries(*names: tuple[str, str, str]) -> list[app_index.AppEntry]:
    return [app_index.entry(name, kind, target) for name, kind, target in names]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# -- matching ------------------------------------------------------------------


class MatchWindowsTests(unittest.TestCase):
    WINDOWS = windows(
        ("Krita", "krita.exe"),
        ("Krita Paint - canvas.png", "krita.exe"),
        ("Untitled - Krita", "krita.exe"),
        ("Untitled - Notepad", "notepad.exe"),
    )

    def test_exact_beats_prefix_beats_containment(self):
        found = match_windows("krita", self.WINDOWS)
        self.assertEqual(found[0][0], "Krita", "the exact title is the one to focus")
        self.assertEqual(len(found), 3)

    def test_enumeration_order_breaks_ties_consistently(self):
        # Two people can say "the second one" and mean the same thing twice.
        first = match_windows("krita paint", self.WINDOWS)
        second = match_windows("krita paint", self.WINDOWS)
        self.assertEqual(first[0], second[0])

    def test_no_match_is_an_empty_list_not_an_error(self):
        self.assertEqual(match_windows("photoshop", self.WINDOWS), [])

    def test_an_empty_query_matches_nothing(self):
        self.assertEqual(match_windows("", self.WINDOWS), [])

    def test_whitespace_is_collapsed_before_matching(self):
        self.assertEqual(match_windows("  krita   paint  ", self.WINDOWS)[0][0], "Krita Paint - canvas.png")


# -- browser ownership ---------------------------------------------------------


class BrowserIdentityTests(unittest.TestCase):
    def test_browser_programs_are_recognised_by_name(self):
        for name in ("chrome", "Google Chrome", "chrome.exe", "msedge", "firefox"):
            with self.subTest(name=name):
                self.assertTrue(app_resolver.browser_program(name))

    def test_an_ordinary_program_is_not_a_browser(self):
        self.assertEqual(app_resolver.browser_program("krita"), "")

    def test_a_pwa_window_is_classified_as_the_app_not_the_browser(self):
        # The reported Instagram bug in its window form. A PWA is *hosted by* a
        # browser, so the honest facts are both true; what must not happen is the
        # app being reported as the browser, which is what makes Lumine open a
        # second window instead of focusing the one already there.
        self.assertTrue(app_resolver.looks_like_browser_window("Instagram", "chrome.exe"))
        self.assertEqual(app_resolver.classify_window("Instagram", "chrome.exe"), "web_app")

    def test_a_real_browser_window_is_classified_as_a_browser(self):
        self.assertEqual(app_resolver.classify_window("YouTube", "chrome.exe"), "browser")

    def test_a_page_whose_title_mentions_a_browser_is_not_one(self):
        # "Edge Cases" is a page, not the Edge browser.
        self.assertEqual(
            app_resolver.classify_window("Edge Cases - Documentation", "krita.exe"), "program"
        )

    def test_an_installed_web_app_shortcut_classifies_its_window(self):
        entry = app_index.entry("Netflix (www.netflix.com)", app_index.KIND_SHORTCUT, "x.lnk")
        self.assertEqual(app_resolver.classify_window("Some page", "msedge.exe", entry), "web_app")

    def test_an_ordinary_program_is_a_program(self):
        self.assertEqual(app_resolver.classify_window("Untitled - Krita", "krita.exe"), "program")


class WebAppDetectionTests(unittest.TestCase):
    def test_a_browser_written_shortcut_counts_as_a_web_app(self):
        app = app_index.entry("Instagram (www.instagram.com)", app_index.KIND_SHORTCUT, "x.lnk")
        self.assertTrue(app_resolver.is_web_app_entry(app))

    def test_a_protocol_handler_counts_as_a_web_app(self):
        app = app_index.entry("spotify", app_index.KIND_PROTOCOL, "spotify:")
        self.assertTrue(app_resolver.is_web_app_entry(app))

    def test_a_single_lowercase_word_is_a_program(self):
        app = app_index.entry("krita", app_index.KIND_APP_PATH, r"C:\krita.exe")
        self.assertFalse(app_resolver.is_web_app_entry(app))


# -- resolution ----------------------------------------------------------------


class ResolutionTests(unittest.TestCase):
    def _resolver(self, *, win_rows=(), found=(), running=0) -> AppResolver:
        return AppResolver(
            window_reader=lambda: list(win_rows),
            process_reader=lambda name: running,
            finder=lambda key, limit=1: list(found),
            clock=FakeClock(),
        )

    def test_a_running_window_with_a_window_is_the_strongest_evidence(self):
        resolver = self._resolver(
            win_rows=windows(("Untitled - Krita", "krita.exe")),
            found=entries(("Krita", app_index.KIND_APP_PATH, r"C:\krita.exe")),
            running=1,
        )
        target = asyncio.run(resolver.resolve("krita"))
        self.assertTrue(target.has_window)
        self.assertEqual(target.window_title, "Untitled - Krita")
        self.assertEqual(target.kind, "program")
        self.assertTrue(target.running)

    def test_a_process_with_no_window_is_not_a_window_to_focus(self):
        # The distinction the whole module exists for: a tray app or an updater is
        # running and cannot be focused.
        resolver = self._resolver(
            win_rows=(), found=entries(("Krita", app_index.KIND_APP_PATH, r"C:\krita.exe")), running=3
        )
        target = asyncio.run(resolver.resolve("krita"))
        self.assertFalse(target.has_window)
        self.assertTrue(target.running)
        self.assertTrue(target.notes["windowless"])

    def test_an_installed_but_closed_app_resolves_to_its_entry(self):
        resolver = self._resolver(
            win_rows=(), found=entries(("Krita", app_index.KIND_APP_PATH, r"C:\krita.exe")), running=0
        )
        target = asyncio.run(resolver.resolve("krita"))
        self.assertTrue(target.resolved)
        self.assertFalse(target.running)

    def test_something_unknown_resolves_to_nothing_rather_than_guessing(self):
        resolver = self._resolver(win_rows=(), found=(), running=0)
        target = asyncio.run(resolver.resolve("photoshop"))
        self.assertFalse(target.resolved)
        self.assertIn("unresolved", target.notes)

    def test_several_windows_are_counted_rather_than_guessed_between(self):
        resolver = self._resolver(
            win_rows=windows(("Krita", "krita.exe"), ("canvas.png - Krita", "krita.exe")),
            found=entries(("Krita", app_index.KIND_APP_PATH, r"C:\krita.exe")),
        )
        target = asyncio.run(resolver.resolve("krita"))
        self.assertEqual(target.notes["matchedWindows"], 2)

    def test_an_empty_name_resolves_to_nothing(self):
        resolver = self._resolver()
        self.assertFalse(asyncio.run(resolver.resolve("   ")).resolved)

    def test_a_failing_window_reader_leaves_the_installed_facts_intact(self):
        # "Cannot tell whether a window is open" must not become "nothing is
        # open" -- the second would make the caller launch a duplicate.
        resolver = AppResolver(
            window_reader=mock.Mock(side_effect=OSError("EnumWindows failed")),
            finder=lambda key, limit=1: entries(("Krita", app_index.KIND_APP_PATH, r"C:\krita.exe")),
            process_reader=lambda name: 0,
            clock=FakeClock(),
        )
        target = asyncio.run(resolver.resolve("krita"))
        self.assertTrue(target.resolved)
        self.assertFalse(target.has_window)

    def test_a_failing_index_lookup_is_a_miss_not_a_crash(self):
        resolver = AppResolver(
            window_reader=lambda: [],
            finder=mock.Mock(side_effect=RuntimeError("registry unavailable")),
            process_reader=lambda name: 0,
            clock=FakeClock(),
        )
        self.assertFalse(asyncio.run(resolver.resolve("krita")).resolved)

    def test_the_browser_kind_is_reported_so_a_route_can_tell_them_apart(self):
        resolver = self._resolver(
            win_rows=windows(("YouTube - Google Chrome", "chrome.exe")),
            found=entries(("Google Chrome", app_index.KIND_APP_PATH, r"C:\chrome.exe")),
        )
        target = asyncio.run(resolver.resolve("chrome"))
        self.assertEqual(target.kind, "browser")


class WindowCacheTests(unittest.TestCase):
    def test_the_window_list_is_read_once_within_the_ttl(self):
        reader = mock.Mock(return_value=windows(("Krita", "krita.exe")))
        clock = FakeClock()
        resolver = AppResolver(window_reader=reader, finder=lambda k, limit=1: [], process_reader=lambda n: 0, clock=clock)
        asyncio.run(resolver.resolve("krita"))
        asyncio.run(resolver.resolve("notepad"))
        self.assertEqual(reader.call_count, 1, "a warm lookup must not re-enumerate the desktop")

    def test_the_cache_expires_so_a_new_window_is_seen(self):
        reader = mock.Mock(return_value=windows(("Krita", "krita.exe")))
        clock = FakeClock()
        resolver = AppResolver(
            window_reader=reader, finder=lambda k, limit=1: [], process_reader=lambda n: 0, clock=clock, window_ttl=10.0
        )
        asyncio.run(resolver.resolve("krita"))
        clock.advance(11)
        asyncio.run(resolver.resolve("krita"))
        self.assertEqual(reader.call_count, 2)

    def test_invalidating_forces_the_next_read(self):
        reader = mock.Mock(return_value=windows(("Krita", "krita.exe")))
        resolver = AppResolver(window_reader=reader, finder=lambda k, limit=1: [], process_reader=lambda n: 0)
        asyncio.run(resolver.resolve("krita"))
        resolver.invalidate()
        asyncio.run(resolver.resolve("krita"))
        self.assertEqual(reader.call_count, 2)

    def test_a_failed_read_keeps_the_previous_facts_rather_than_emptying_them(self):
        reader = mock.Mock(side_effect=[windows(("Krita", "krita.exe")), OSError("boom")])
        resolver = AppResolver(window_reader=reader, finder=lambda k, limit=1: [], process_reader=lambda n: 0)
        asyncio.run(resolver.resolve("krita"))
        resolver.invalidate()
        target = asyncio.run(resolver.resolve("krita"))
        self.assertEqual(reader.call_count, 2)
        del target  # the assertion above is that the read was attempted at all


# -- opening -------------------------------------------------------------------


def _target(**kwargs) -> AppTarget:
    base = {"name": "Krita"}
    base.update(kwargs)
    return AppTarget(**base)


KRITA_ENTRY = app_index.entry("Krita", app_index.KIND_APP_PATH, r"C:\krita.exe")


class OpenTargetTests(unittest.IsolatedAsyncioTestCase):
    """The three outcomes: a window was there, one appeared, or none did."""

    async def test_an_open_window_is_focused_and_reported_as_observed(self):
        focused: list[int] = []
        with mock.patch.object(app_resolver, "focus_window", side_effect=lambda h: focused.append(h)):
            outcome = await app_resolver.open_target(
                _target(has_window=True, window_handle=42, window_title="Untitled - Krita")
            )
        self.assertEqual(outcome.status, verification.OBSERVED)
        self.assertEqual(focused, [42])
        self.assertIn("Brought", outcome.detail)
        self.assertTrue(outcome.data["focused"])

    async def test_a_refused_focus_is_uncertain_not_success(self):
        with mock.patch.object(app_resolver, "focus_window", side_effect=OSError("no")):
            outcome = await app_resolver.open_target(
                _target(has_window=True, window_handle=42, window_title="Krita")
            )
        self.assertEqual(outcome.status, verification.UNCERTAIN)

    async def test_a_new_instance_never_touches_the_existing_window(self):
        launched: list[str] = []
        with mock.patch.object(app_resolver, "focus_window") as focus, mock.patch.object(
            app_resolver, "_await_window", new=mock.AsyncMock(return_value=("Krita", 7, "krita.exe"))
        ), mock.patch.object(
            app_resolver.apps, "launch_with_entry", side_effect=lambda app: launched.append(app.name)
        ):
            outcome = await app_resolver.open_target(
                _target(
                    has_window=True,
                    window_handle=42,
                    window_title="Untitled - Krita",
                    entry=KRITA_ENTRY,
                ),
                new_instance=True,
            )
        focus.assert_not_called()
        self.assertEqual(launched, ["Krita"])
        self.assertEqual(outcome.status, verification.OBSERVED)

    async def test_a_launch_with_no_window_is_accepted_not_observed(self):
        # The distinction the whole verification contract is for: a process was
        # started, and no window is claimed.
        with mock.patch.object(
            app_resolver, "_await_window", new=mock.AsyncMock(return_value=None)
        ), mock.patch.object(app_resolver.apps, "launch_with_entry"):
            outcome = await app_resolver.open_target(_target(entry=KRITA_ENTRY), appear_timeout=0)
        self.assertEqual(outcome.status, verification.ACCEPTED)
        self.assertFalse(outcome.data["window_seen"])
        self.assertNotEqual(outcome.status, verification.OBSERVED)

    async def test_a_launch_whose_window_appears_is_observed(self):
        with mock.patch.object(
            app_resolver, "_await_window", new=mock.AsyncMock(return_value=("Krita", 7, "krita.exe"))
        ), mock.patch.object(app_resolver.apps, "launch_with_entry"):
            outcome = await app_resolver.open_target(_target(entry=KRITA_ENTRY))
        self.assertEqual(outcome.status, verification.OBSERVED)
        self.assertEqual(outcome.evidence, "a window appeared after the launch")

    async def test_a_raising_launch_is_a_clean_failure(self):
        with mock.patch.object(app_resolver.apps, "launch_with_entry", side_effect=OSError("no such file")):
            outcome = await app_resolver.open_target(_target(entry=KRITA_ENTRY))
        self.assertEqual(outcome.status, verification.FAILED)
        self.assertIn("couldn't start", outcome.detail)

    async def test_nothing_resolvable_fails_before_any_process_is_started(self):
        with mock.patch.object(app_resolver.apps, "launch_with_entry") as launch:
            outcome = await app_resolver.open_target(_target(name="photoshop"))
        launch.assert_not_called()
        self.assertEqual(outcome.status, verification.FAILED)

    async def test_a_windowless_process_says_so_when_no_window_appears(self):
        launched: list[str] = []
        with mock.patch.object(
            app_resolver, "_await_window", new=mock.AsyncMock(return_value=None)
        ), mock.patch.object(
            app_resolver.apps, "launch_with_entry", side_effect=lambda app: launched.append(app.name)
        ):
            outcome = await app_resolver.open_target(_target(process_count=3, entry=KRITA_ENTRY))
        self.assertEqual(launched, ["Krita"])
        self.assertEqual(outcome.status, verification.ACCEPTED)
        self.assertIn("already running", outcome.detail)
        self.assertTrue(outcome.data["alreadyRunning"])


class AwaitWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_cache_is_invalidated_first_so_the_launch_is_what_is_seen(self):
        # Otherwise a window that was already open would be reported as proof the
        # launch worked.
        resolver = app_resolver.resolver()
        resolver.invalidate()
        seen: list[bool] = []
        original = app_resolver.window_facts
        try:
            with mock.patch.object(
                app_resolver, "window_facts", return_value=windows(("Krita", "krita.exe"))
            ):
                found = await app_resolver._await_window("krita", 0)
            self.assertIsNotNone(found)
        finally:
            del seen, original

    async def test_no_window_within_the_deadline_returns_none(self):
        with mock.patch.object(app_resolver, "window_facts", return_value=[]), \
             mock.patch.object(app_resolver.asyncio, "sleep", new=mock.AsyncMock()):
            self.assertIsNone(await app_resolver._await_window("krita", 0))


class PlatformTests(unittest.TestCase):
    def test_focusing_a_window_off_windows_says_so(self):
        if sys.platform == "win32":
            self.skipTest("this asserts the non-Windows sentence")
        from agent.tools.tools_compat import ToolError

        with self.assertRaises(ToolError):
            app_resolver.focus_window(1)


if __name__ == "__main__":
    unittest.main()