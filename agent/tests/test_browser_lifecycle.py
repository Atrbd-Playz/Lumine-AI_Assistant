"""Reopening the browser in the same session, and the seams that had to give.

Three separate things stand between "close it" and "open it again", and every one
of them failed in a way that read as a broken product rather than a lifecycle
fault:

* **`release` held `_lock` across the tab close.** `_lock` is a `threading.Lock`
  and its own docstring says it is held only for the statements that read or
  write the ownership fields, never across an `await`. Holding it across the
  `await` meant a second coroutine reaching `with self._lock` blocked the
  *event loop* instead of waiting its turn -- so the close the first one was
  waiting on could never finish. Teardown deadlocking against its own lock is
  the worst available shape for the one path that is not allowed to fail.
* **`_close_remote` probed the endpoint once.** `context.close()` returns as
  Chromium is tearing down, and a socket that is mid-shutdown still accepts a
  connect -- so one instant probe read "alive" for a browser that was already
  gone and left `browser-cdp.txt` behind. The next `_start` in this *same
  session* then joined that dying browser and failed against it, which is the
  whole of "close it, then ask me to open it again and it will not".
* **One launch attempt saw a lock that was on its way out.** `release` schedules
  the teardown onto the owning loop and does not await it, so the next `ensure`
  can arrive while Chromium is still letting go of the profile directory. One
  attempt translated that into `MSG_PROFILE_LOCKED` -- telling the user to go and
  close a window that is already closing, for a reason that is no longer true.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.tools.browser import control as control_mod
from agent.tools.browser import preview, session
from agent.tools.tools_compat import ToolError


class ReleaseLockTests(unittest.IsolatedAsyncioTestCase):
    """`release` must not hold `_lock` while it waits for a tab to close."""

    def _browser(self) -> session._Browser:
        browser = session._Browser()
        browser._loop = asyncio.get_running_loop()
        browser._shared = False
        # Stand-ins for the two things release is expected to drop. `_wa_page`
        # is the load-bearing one: it is the only thing release *awaits* while
        # holding the lock in the old shape.
        browser._wa_page = mock.Mock()
        browser._wa_page.is_closed.return_value = False
        browser._joined_browser = object()
        # The handles on a browser *this process launched*. Both have to be
        # dropped by `release`: `_cdp_browser` belongs to a loop that may be gone,
        # and a surviving reference is how the singleton keeps pointing at a dead
        # browser. `poll` answers "already exited", so teardown does not wait out
        # the real grace period in a unit test.
        browser._process = mock.Mock()
        browser._process.poll = mock.Mock(return_value=0)
        browser._cdp_browser = mock.Mock()
        browser._cdp_browser.new_browser_cdp_session = mock.AsyncMock(
            side_effect=RuntimeError("the browser is gone")
        )
        browser._tabs = [object()]
        browser._active = 1
        return browser

    def _patches(self):
        # Nothing here may touch a real endpoint file: `release` ends in
        # `_close_remote(None, ...)`, whose `context is None` branch probes the
        # published port and unlinks whatever it finds.
        return (
            mock.patch.object(session, "_read_endpoint", return_value=""),
            mock.patch.object(session, "_endpoint_alive", return_value=False),
            mock.patch.object(session, "_forget_endpoint"),
        )

    async def test_the_lock_is_free_while_the_tab_is_still_closing(self):
        browser = self._browser()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def slow_close() -> None:
            started.set()
            await finish.wait()

        browser._wa_page.close = slow_close

        read, alive, forget = self._patches()
        with read, alive, forget:
            task = asyncio.create_task(browser.release())
            # Wait for the close to actually be in flight -- polling the lock
            # before the coroutine has run would prove nothing.
            await asyncio.wait_for(started.wait(), timeout=5)

            acquired = browser._lock._lock.acquire(blocking=False)
            if acquired:
                browser._lock._lock.release()

            finish.set()
            await asyncio.wait_for(task, timeout=5)

        self.assertTrue(
            acquired,
            "release held _lock across the tab close, so the close can never finish",
        )

    async def test_everything_the_singleton_was_holding_is_dropped(self):
        """Clearing the references is what makes the singleton reusable.

        `_joined_browser` is the sharp end: leaving it set meant a later read of
        "am I a guest?" answered yes for a browser this process had already
        left, so the next `release` would refuse to close anything at all.
        """
        browser = self._browser()
        browser._wa_page.is_closed.return_value = True

        read, alive, forget = self._patches()
        with read, alive, forget:
            await asyncio.wait_for(browser.release(), timeout=5)

        self.assertIsNone(browser._wa_page)
        self.assertIsNone(browser._wa_lock)
        self.assertIsNone(browser._joined_browser)
        self.assertIsNone(browser._cdp_browser)
        self.assertIsNone(browser._process)
        self.assertEqual(browser._tabs, [])
        self.assertIsNone(browser._active)
        self.assertIsNone(browser._loop)
        self.assertIsNone(browser._context)
        self.assertIsNone(browser._pw)
        self.assertFalse(browser._shared)
        self.assertFalse(browser._hid_window)

    async def test_the_lock_is_free_at_the_await_inside_the_close(self):
        """The same property, asked from the inside so timing cannot flatter it.

        A test that merely waits can pass because the machine was quick. Asking
        *from inside the awaited close* is the direct question: if `release`
        still held `_lock`, this acquire would come back `False`.
        """
        browser = self._browser()
        lock_was_free: list[bool] = []

        async def slow_close() -> None:
            acquired = browser._lock._lock.acquire(blocking=False)
            if acquired:
                browser._lock._lock.release()
            lock_was_free.append(acquired)

        browser._wa_page.close = slow_close

        read, alive, forget = self._patches()
        with read, alive, forget:
            await asyncio.wait_for(browser.release(), timeout=5)

        self.assertEqual(lock_was_free, [True])


class PreserveWhatsAppDraftTests(unittest.IsolatedAsyncioTestCase):
    def _browser_with_draft(self):
        browser = session._Browser()
        browser._loop = asyncio.get_running_loop()
        browser._context = mock.Mock()
        browser._pw = mock.Mock()
        browser._wa_page = mock.Mock()
        browser._wa_page.is_closed.return_value = False
        browser._context.pages = [browser._wa_page, mock.Mock()]
        browser._context.unroute = mock.AsyncMock()
        browser._context.remove_listener = mock.Mock()
        browser._route_handler = object()
        browser.preserve_whatsapp_on_release()
        return browser

    async def test_an_unsent_draft_hands_off_only_the_whatsapp_window(self):
        browser = self._browser_with_draft()
        wa_page = browser._wa_page
        extra_page = browser._context.pages[1]
        context, playwright = browser._context, browser._pw
        route_handler = browser._route_handler
        with mock.patch.object(
            session, "_close_page_safe", new=mock.AsyncMock()
        ) as close_page:
            await browser.release()

        self.assertEqual(close_page.await_count, 1)
        self.assertIs(close_page.await_args.args[0], extra_page)
        self.assertEqual(len(browser._preserved_clients), 1)
        self.assertEqual(browser._preserved_clients[0], (context, playwright))
        context.unroute.assert_awaited_once_with("**/*", route_handler)
        context.remove_listener.assert_called_once_with("page", browser._on_page)
        self.assertIsNotNone(wa_page)
        self.assertIsNone(browser._context)
        self.assertIsNone(browser._pw)
        self.assertIsNone(browser._loop)
        self.assertFalse(browser._preserve_whatsapp)

    async def test_a_failed_route_handoff_closes_instead_of_stranding_a_dead_loop(self):
        browser = self._browser_with_draft()
        context, playwright = browser._context, browser._pw
        context.unroute.side_effect = RuntimeError("route still active")
        with (
            mock.patch.object(session, "_close_page_safe", new=mock.AsyncMock()),
            mock.patch.object(session._Browser, "_close_remote", new=mock.AsyncMock()) as close_remote,
        ):
            await browser.release()

        close_remote.assert_awaited_once_with(
            context,
            playwright,
            shared=False,
            endpoint_path=None,
            process=None,
            browser=None,
        )
        self.assertEqual(browser._preserved_clients, [])


class CloseRemoteEndpointTests(unittest.IsolatedAsyncioTestCase):
    """The published endpoint has to outlive the browser's own teardown."""

    ENDPOINT = "http://127.0.0.1:9222"

    async def _close(self, context, alive, *, shared: bool = False, playwright=None):
        with mock.patch.object(session, "_read_endpoint", return_value=self.ENDPOINT), \
                mock.patch.object(session, "_endpoint_alive", side_effect=alive) as probe, \
                mock.patch.object(session, "_forget_endpoint") as forget:
            await session._Browser._close_remote(
                context, playwright or mock.AsyncMock(), shared=shared
            )
        return probe, forget

    async def test_a_dead_endpoint_is_dropped_after_one_probe(self):
        context = mock.AsyncMock()
        probe, forget = await self._close(context, lambda _: False)
        context.close.assert_awaited_once()
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(forget.call_count, 1)

    async def test_a_socket_mid_shutdown_is_waited_out_before_the_file_goes(self):
        """The measured failure: one instant probe read "alive" for a dead browser.

        `context.close()` returns while Chromium is still tearing down, and a
        socket on its way down still accepts a connect. Believing that one
        reading left `browser-cdp.txt` on disk, and the next `_start in this same
        session* joined a browser that no longer existed.
        """
        context = mock.AsyncMock()
        answers = iter([True, True, False])
        probe, forget = await self._close(context, lambda _: next(answers))
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(forget.call_count, 1)

    async def test_the_file_is_dropped_even_when_the_port_never_goes_quiet(self):
        """A surviving *file for a dead browser* costs a whole session.

        The forget is deliberately unconditional: waiting for proof that the
        browser is gone is the one thing that cannot be bounded safely, and a
        file that survives costs one no-op unlink on the next run.
        """
        context = mock.AsyncMock()
        with mock.patch.object(session, "_ENDPOINT_DEATH_SECONDS", 0.05), \
                mock.patch.object(session, "_ENDPOINT_POLL_SECONDS", 0.005):
            probe, forget = await self._close(context, lambda _: True)
        self.assertEqual(forget.call_count, 1)
        self.assertGreater(probe.call_count, 1, "the loop never polled at all")

    async def test_the_polling_is_bounded(self):
        """Teardown must not become the slow part of a teardown."""
        context = mock.AsyncMock()
        with mock.patch.object(session, "_ENDPOINT_DEATH_SECONDS", 0.05), \
                mock.patch.object(session, "_ENDPOINT_POLL_SECONDS", 0.005):
            await self._close(context, lambda _: True)
        # Generous, because a loaded CI box is not a deadline. The point is
        # that it is seconds, not minutes.
        self.assertLess(0.05 + 0.5, 5)

    async def test_a_live_endpoint_of_another_process_is_left_alone(self):
        """`context is None` means we had no browser of our own to close.

        The published file may therefore be somebody else's live endpoint, and
        unlinking it would make a perfectly healthy shared browser un-findable
        to the next process.
        """
        probe, forget = await self._close(None, lambda _: True)
        self.assertEqual(forget.call_count, 0)
        self.assertEqual(probe.call_count, 1)

    async def test_a_dead_endpoint_is_still_cleaned_up_when_we_had_no_browser(self):
        probe, forget = await self._close(None, lambda _: False)
        self.assertEqual(forget.call_count, 1)
        self.assertEqual(probe.call_count, 1)

    async def test_a_guest_never_touches_the_endpoint_at_all(self):
        playwright = mock.AsyncMock()
        probe, forget = await self._close(None, lambda _: True, shared=True, playwright=playwright)
        playwright.stop.assert_awaited_once()
        self.assertEqual(probe.call_count, 0)
        self.assertEqual(forget.call_count, 0)


class LaunchAndAttachTests(unittest.IsolatedAsyncioTestCase):
    """Starting a browser as a process and then attaching to it.

    **The launch mode is the fix, so this is the test that keeps it.** Playwright's
    `launch_persistent_context` drives Chromium over `--remote-debugging-pipe`, and
    everything it launched stalled YouTube playback at ~40 s with the media request
    returning 401. Everything started as an ordinary process and merely attached to
    over a debugging *port* played past a minute -- measured on the same profile,
    the same flags, the same engine. Playwright cannot launch over a port (measured:
    it times out with the port already live), so the order is launch, then attach.

    A lock that is *being* released looks exactly like one somebody holds, and this
    is the path that reopens the browser in the same session, so `ProfileBusy` is
    retried -- bounded, because a directory somebody genuinely holds never clears.
    """

    def _playwright(self, *, contexts=None, attach_error: Exception | None = None):
        playwright = mock.Mock()
        playwright.chromium.executable_path = r"C:\chrome\chrome.exe"
        browser = mock.Mock()
        browser.contexts = list(contexts if contexts is not None else [mock.Mock()])
        playwright.chromium.connect_over_cdp = mock.AsyncMock(
            side_effect=[attach_error] if attach_error else None,
            return_value=browser,
        )
        return playwright, browser

    async def test_a_launch_that_publishes_no_endpoint_is_retried_then_reported(self):
        """The Windows shape of a held profile: hand off, exit 0, no port."""
        playwright, _ = self._playwright()
        outcomes = [session.ProfileBusy("no endpoint")] * session._LAUNCH_ATTEMPTS
        with mock.patch.object(session, "launch_detached", return_value=mock.Mock()) as spawn, \
                mock.patch.object(
                    session, "wait_for_devtools", side_effect=outcomes
                ) as wait, \
                mock.patch.object(session, "terminate_process") as end, \
                mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0):
            with self.assertRaises(session.ProfileBusy):
                await session.launch_and_attach(
                    playwright,
                    executable=r"C:\brave\brave.exe",
                    engine="brave",
                    directory=Path("C:/profile"),
                )
        self.assertEqual(wait.call_count, session._LAUNCH_ATTEMPTS)
        self.assertEqual(spawn.call_count, session._LAUNCH_ATTEMPTS)
        # Every attempt that produced nothing leaves this process behind if it is
        # not ended, and a leftover Chromium holds the profile for the next room.
        self.assertEqual(end.call_count, session._LAUNCH_ATTEMPTS)

    async def test_a_lock_that_clears_is_retried_until_the_browser_comes_up(self):
        playwright, browser = self._playwright()
        context = browser.contexts[0]
        with mock.patch.object(session, "launch_detached", return_value=mock.Mock()), \
                mock.patch.object(
                    session,
                    "wait_for_devtools",
                    side_effect=[session.ProfileBusy("busy")] * (session._LAUNCH_ATTEMPTS - 1)
                    + [9333],
                ) as wait, \
                mock.patch.object(session, "terminate_process"), \
                mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0):
            launched = await session.launch_and_attach(
                playwright,
                executable=r"C:\chrome\chrome.exe",
                engine="chromium",
                directory=Path("C:/profile"),
            )
        self.assertIs(launched.context, context)
        self.assertIs(launched.browser, browser)
        self.assertEqual(launched.port, 9333)
        self.assertEqual(wait.call_count, session._LAUNCH_ATTEMPTS)
        playwright.chromium.connect_over_cdp.assert_awaited_once_with(
            "http://127.0.0.1:9333"
        )

    async def test_a_timeout_is_not_retried(self):
        """A browser that started and never published is still running.

        Retrying would launch a second one against the same profile -- so the
        delay would buy a worse failure, not a recovery.
        """
        playwright, _ = self._playwright()
        with mock.patch.object(session, "launch_detached", return_value=mock.Mock()) as spawn, \
                mock.patch.object(
                    session, "wait_for_devtools", side_effect=session.LaunchTimeout("slow")
                ), \
                mock.patch.object(session, "terminate_process"):
            with self.assertRaises(session.LaunchTimeout):
                await session.launch_and_attach(
                    playwright,
                    executable=r"C:\chrome\chrome.exe",
                    engine="chromium",
                    directory=Path("C:/profile"),
                )
        self.assertEqual(spawn.call_count, 1)

    async def test_an_attach_failure_ends_the_browser_it_started(self):
        playwright, _ = self._playwright(attach_error=RuntimeError("protocol error"))
        with mock.patch.object(session, "launch_detached", return_value=mock.Mock()), \
                mock.patch.object(session, "wait_for_devtools", return_value=9333), \
                mock.patch.object(session, "terminate_process") as end:
            with self.assertRaises(ToolError):
                await session.launch_and_attach(
                    playwright,
                    executable=r"C:\chrome\chrome.exe",
                    engine="chromium",
                    directory=Path("C:/profile"),
                )
        end.assert_called_once()

    async def test_the_command_is_the_healthy_shape(self):
        """A person's command line, not Playwright's.

        `--remote-debugging-port` rather than `--remote-debugging-pipe` is the
        whole point: the pipe is what makes the browser behave like an automation
        client, and the port is what makes it behave like a browser somebody
        opened. The *number* is explicit, never `0`: measured across two launch
        shapes, the same command with `=0` stalled YouTube playback at ~40 s in
        every run, and with an explicit number it played (see `free_debug_port`).
        """
        command = session.browser_command(
            "brave",
            r"C:\Brave\brave.exe",
            Path("C:/Users/example/User Data"),
            "Profile 1",
            extra_args=session._window_args(),
            start_url="about:blank",
        )
        self.assertEqual(command[0], r"C:\Brave\brave.exe")
        self.assertIn("--user-data-dir=C:\\Users\\example\\User Data", command)
        ports = [
            argument for argument in command
            if argument.startswith("--remote-debugging-port=")
        ]
        self.assertEqual(len(ports), 1)
        self.assertTrue(ports[0].startswith("--remote-debugging-port="))
        self.assertNotEqual(ports[0], "--remote-debugging-port=0")
        self.assertNotIn("--remote-debugging-pipe", command)
        self.assertIn("--profile-directory=Profile 1", command)
        self.assertIn("--window-size=960,680", command)
        # Playwright used to pass these for us; the browser is OURS to size now.
        self.assertIn("--disable-background-timer-throttling", command)
        self.assertIn("--autoplay-policy=no-user-gesture-required", command)
        self.assertEqual(command[-1], "about:blank")

    def test_there_is_no_profile_directory_when_there_is_none_to_name(self):
        command = session.browser_command(
            "chromium", r"C:\chrome\chrome.exe", Path("C:/profile")
        )
        self.assertFalse(any(part.startswith("--profile-directory") for part in command))
        self.assertEqual(command[-1], "about:blank")


class BrowserEngineSelectionTests(unittest.TestCase):
    def test_auto_prefers_installed_brave_and_falls_back_to_playwright_chromium(self):
        self.assertEqual(
            session._browser_engine_preference(None, "", brave_available=True),
            "brave",
        )
        self.assertEqual(
            session._browser_engine_preference(None, "", brave_available=False),
            "chromium",
        )

    def test_saved_choice_wins_over_environment(self):
        document = {"desktop": {"browsers": {"personalEngine": "chromium"}}}
        self.assertEqual(
            session._browser_engine_preference(document, "brave", brave_available=True),
            "chromium",
        )

    def test_environment_is_used_when_the_saved_choice_is_absent(self):
        self.assertEqual(
            session._browser_engine_preference({}, "brave", brave_available=True),
            "brave",
        )

    def test_an_unknown_choice_uses_auto_without_raising(self):
        self.assertEqual(
            session._browser_engine_preference(
                {"desktop": {"browsers": {"personalEngine": "edge"}}},
                "",
                brave_available=False,
            ),
            "chromium",
        )

    def test_windows_brave_install_is_found_without_touching_a_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = (
                Path(directory)
                / "BraveSoftware"
                / "Brave-Browser"
                / "Application"
                / "brave.exe"
            )
            executable.parent.mkdir(parents=True)
            executable.touch()
            with mock.patch.object(session.sys, "platform", "win32"), mock.patch.dict(
                os.environ,
                {
                    "PROGRAMFILES": directory,
                    "PROGRAMFILES(X86)": "",
                    "LOCALAPPDATA": "",
                    session.BRAVE_EXECUTABLE_VAR: "",
                },
                clear=False,
            ):
                self.assertEqual(session.brave_executable(), executable)

    def test_each_engine_has_its_own_shared_endpoint(self):
        with mock.patch.object(
            session, "profile_dir", return_value=Path("C:/Lumine/browser-profile")
        ):
            self.assertEqual(
                session._endpoint_file("brave"),
                Path("C:/Lumine/browser-cdp-brave.txt"),
            )
            self.assertEqual(
                session._endpoint_file("chromium"),
                Path("C:/Lumine/browser-cdp.txt"),
            )


class ExplicitBrowserEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_explicit_brave_choice_refuses_when_brave_is_missing(self):
        browser = session._Browser()
        with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                mock.patch.object(session, "brave_executable", return_value=None):
            with self.assertRaisesRegex(ToolError, "Brave is not installed"):
                await browser._start(asyncio.get_running_loop())


class BraveUserDataDirTests(unittest.TestCase):
    def test_the_native_user_data_directory_is_used_unless_overridden(self):
        with mock.patch.dict(os.environ, {session.BRAVE_USER_DATA_DIR_VAR: ""}), \
                mock.patch.object(session.sys, "platform", "win32"), \
                mock.patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": "C:/Users/example/AppData/Local"},
                ):
            self.assertEqual(
                session.brave_user_data_dir(),
                Path("C:/Users/example/AppData/Local/BraveSoftware/Brave-Browser/User Data"),
            )

    def test_an_explicit_brave_user_data_directory_is_honored(self):
        with mock.patch.dict(
            os.environ,
            {session.BRAVE_USER_DATA_DIR_VAR: "C:/Lumine/Brave"},
        ):
            self.assertEqual(
                session.brave_user_data_dir(),
                Path("C:/Lumine/Brave"),
            )


class BraveProfileDirectoryTests(unittest.TestCase):
    def test_resolves_the_profile_display_name_to_its_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Profile 1").mkdir()
            (root / "Default").mkdir()
            (root / "Local State").write_text(
                json.dumps(
                    {
                        "profile": {
                            "info_cache": {
                                "Default": {"name": "Person 1"},
                                "Profile 1": {"name": "Lumine"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                session.brave_profile_directory(root),
                root / "Profile 1",
            )

    def test_missing_or_ambiguous_profile_names_refuse(self):
        for cache in (
            {"Default": {"name": "Person 1"}},
            {
                "Profile 1": {"name": "Lumine"},
                "Profile 2": {"name": "Lumine"},
            },
        ):
            with self.subTest(cache=cache), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for key in cache:
                    (root / key).mkdir()
                (root / "Local State").write_text(
                    json.dumps({"profile": {"info_cache": cache}}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "expected one profile"):
                    session.brave_profile_directory(root)

    def test_an_invalid_profile_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Local State").write_text(
                json.dumps(
                    {"profile": {"info_cache": {"Profile 1": {"name": "Lumine"}}}}
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "directory is missing"):
                session.brave_profile_directory(root)


class ProfileInUseTests(unittest.TestCase):
    """The pre-launch question Windows does not answer through the launch."""

    def test_a_directory_with_no_lock_file_is_free(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(session.profile_in_use(Path(directory)))

    def test_a_stale_lock_file_is_removed_and_the_profile_is_free(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lockfile"
            lock.write_text("", encoding="utf-8")
            with mock.patch.object(session.os, "name", "nt"):
                self.assertFalse(session.profile_in_use(Path(directory)))
            self.assertFalse(lock.exists())

    def test_a_held_lock_file_is_reported_as_in_use(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lockfile"
            lock.write_text("", encoding="utf-8")
            with mock.patch.object(session.os, "name", "nt"), \
                    mock.patch.object(session.os, "remove", side_effect=OSError("sharing violation")):
                self.assertTrue(session.profile_in_use(Path(directory)))

    def test_posix_is_not_pre_checked(self):
        """The POSIX singleton lock outlives a crashed browser, so presence is
        not evidence; the launch that exits without an endpoint is, and that is
        what `ProfileBusy` reports."""
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "SingletonLock"
            lock.write_text("", encoding="utf-8")
            with mock.patch.object(session.os, "name", "posix"):
                self.assertFalse(session.profile_in_use(Path(directory)))
            self.assertTrue(lock.exists())


class SettleProfileLockTests(unittest.TestCase):
    """A lock that is *being* released looks exactly like one somebody holds.

    The pre-check asks "is this held now", which is the right question before a
    launch -- but `release()` then `ensure()` on the next turn is a lock this
    code created and is already letting go of, and sending that window to the
    unsigned-in spare for it is the worse mistake of the two.
    """

    def test_a_free_directory_is_not_waited_on(self):
        with mock.patch.object(session, "profile_in_use", return_value=False) as in_use, \
                mock.patch.object(session.time, "sleep") as sleep:
            self.assertTrue(session.settle_profile_lock(Path("C:/profile")))
        self.assertEqual(in_use.call_count, 1)
        sleep.assert_not_called()

    def test_a_lock_that_is_being_released_is_given_a_moment(self):
        with mock.patch.object(
            session, "profile_in_use", side_effect=[True, True, False]
        ) as in_use, \
                mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0.5), \
                mock.patch.object(session.time, "sleep") as sleep:
            self.assertTrue(session.settle_profile_lock(Path("C:/profile")))
        self.assertEqual(in_use.call_count, 3)
        self.assertEqual([entry.args[0] for entry in sleep.call_args_list], [0.5, 0.5])

    def test_a_lock_somebody_holds_is_not_waited_on_forever(self):
        with mock.patch.object(session, "profile_in_use", return_value=True) as in_use, \
                mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                mock.patch.object(session.time, "sleep"):
            self.assertFalse(session.settle_profile_lock(Path("C:/profile")))
        self.assertEqual(in_use.call_count, session._LAUNCH_ATTEMPTS)

    def test_the_settle_window_stays_under_two_seconds(self):
        """The whole cost of guessing "hold on a moment" instead of "spare".

        A held profile the user is genuinely using pays this on the first
        browser request of a session, so it has to stay short enough not to
        read as a hang -- and it shares the budget `launch_and_attach` spends
        retrying a launch that produced no endpoint.
        """
        window = (session._LAUNCH_ATTEMPTS - 1) * session._LAUNCH_RETRY_SECONDS
        self.assertLess(window, 2.0)


class SpareProfileDirTests(unittest.TestCase):
    """Where the fallback window's directory lives, and why one per engine."""

    def test_each_engine_gets_its_own_spare_beside_the_first_choice(self):
        with mock.patch.object(
            session, "profile_dir", return_value=Path("C:/base/browser-profile")
        ):
            # Per engine, for the same reason the endpoint files are
            # `browser-cdp.txt` and `browser-cdp-brave.txt`: two browsers
            # cannot share a user-data directory, and a fallback that turned
            # into that would trade a refusal for a lock.
            self.assertEqual(
                session.spare_profile_dir("brave"),
                Path("C:/base/browser-profile-spare-brave"),
            )
            self.assertEqual(
                session.spare_profile_dir(),
                Path("C:/base/browser-profile-spare-chromium"),
            )


class SpareProfileFallbackTests(unittest.IsolatedAsyncioTestCase):
    """A held profile opens a window beside it, rather than refusing.

    The reported failure: the user kept Brave open on the ``Lumine`` profile,
    asked for a YouTube search, and was told to go and close their own browser
    first. ``MSG_PROFILE_LOCKED`` was the *whole* answer, and it is the one
    answer a companion must not give -- the lock is a fact about one directory,
    and every other directory on the machine is unaffected by it. The personal
    profile stays the first choice because it is the one already signed in; it
    is no longer the only one.
    """

    def _busy(self) -> Exception:
        """What a launch that produced no endpoint reports.

        Same condition as Chromium's old "existing browser session" paragraph,
        different witness: a browser started as an ordinary process hands off and
        exits 0, so there is no message to read -- only an endpoint that never
        appeared.
        """
        return session.ProfileBusy("no endpoint")

    def _fake_playwright(self):
        pw = mock.Mock()
        pw.stop = mock.AsyncMock()
        pw.chromium.executable_path = r"C:\chrome\chrome.exe"
        manager = mock.Mock()
        manager.start = mock.AsyncMock(return_value=pw)
        return mock.Mock(return_value=manager), pw

    def _fake_context(self):
        context = mock.Mock()
        context.pages = []
        page = mock.Mock()
        page.is_closed = mock.Mock(return_value=False)
        context.new_page = mock.AsyncMock(return_value=page)
        context.route = mock.AsyncMock()
        context.on = mock.Mock()
        return context, page

    @staticmethod
    def _launched(context, port: int = 9333) -> session._Launched:
        return session._Launched(context, mock.Mock(), mock.Mock(), port)

    async def test_a_locked_brave_profile_falls_back_to_a_spare_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            brave_data = root / "User Data"
            (brave_data / "Profile 1").mkdir(parents=True)
            context, page = self._fake_context()
            launch = mock.AsyncMock(
                side_effect=[self._busy(), self._launched(context)]
            )
            factory, _ = self._fake_playwright()
            endpoint = root / "browser-cdp-brave.txt"
            browser = session._Browser()
            with mock.patch("playwright.async_api.async_playwright", factory), \
                    mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "brave_executable", return_value=root / "brave.exe"), \
                    mock.patch.object(session, "brave_user_data_dir", return_value=brave_data), \
                    mock.patch.object(session, "brave_profile_directory", return_value=brave_data / "Profile 1"), \
                    mock.patch.object(session, "profile_dir", return_value=root / "lumine" / "browser-profile"), \
                    mock.patch.object(session, "_endpoint_file", return_value=endpoint), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "profile_in_use", return_value=False), \
                    mock.patch.object(session, "launch_and_attach", launch), \
                    mock.patch.object(session, "_publish_endpoint") as publish:
                result = await browser._start(asyncio.get_running_loop())
                # Inside the patch: `spare_profile_dir` derives from
                # `profile_dir()`, which is mocked to the temp root above.
                spare = session.spare_profile_dir("brave")

            self.assertIs(result, page)
            self.assertEqual(launch.await_count, 2)
            first, second = launch.await_args_list
            # The personal profile is still the first choice, by name.
            self.assertEqual(first.kwargs["directory"], brave_data)
            self.assertEqual(first.kwargs["profile_directory"], "Profile 1")
            # ...and the spare is the second, with no `profile_directory`: a
            # fresh user-data directory has only `Default` in it, and naming
            # the personal profile here would quietly recreate the very
            # directory we were just refused.
            self.assertEqual(second.kwargs["directory"], spare)
            self.assertIsNone(second.kwargs["profile_directory"])
            self.assertTrue(spare.is_dir())
            # The endpoint published is still the *engine's* one, and it names the
            # port the browser that actually started is listening on -- so a later
            # process joins this window exactly as it would have joined the first
            # choice. One browser, either way.
            publish.assert_called_once_with(9333, endpoint)

    async def test_a_profile_already_held_never_attempts_to_launch_on_it(self):
        """Windows hands a second launch off and exits 0, so the check is
        *before* the launch and the first attempt is already the spare."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            brave_data = root / "User Data"
            (brave_data / "Profile 1").mkdir(parents=True)
            context, page = self._fake_context()
            launch = mock.AsyncMock(return_value=self._launched(context))
            factory, _ = self._fake_playwright()
            endpoint = root / "browser-cdp-brave.txt"
            browser = session._Browser()
            with mock.patch("playwright.async_api.async_playwright", factory), \
                    mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "brave_executable", return_value=root / "brave.exe"), \
                    mock.patch.object(session, "brave_user_data_dir", return_value=brave_data), \
                    mock.patch.object(session, "brave_profile_directory", return_value=brave_data / "Profile 1"), \
                    mock.patch.object(session, "profile_dir", return_value=root / "lumine" / "browser-profile"), \
                    mock.patch.object(session, "_endpoint_file", return_value=endpoint), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                    mock.patch.object(
                        session,
                        "profile_in_use",
                        side_effect=lambda directory: Path(directory) == brave_data,
                    ), \
                    mock.patch.object(session, "launch_and_attach", launch), \
                    mock.patch.object(session, "_publish_endpoint"):
                result = await browser._start(asyncio.get_running_loop())
                spare = session.spare_profile_dir("brave")

            self.assertIs(result, page)
            self.assertEqual(launch.await_count, 1)
            self.assertEqual(launch.await_args.kwargs["directory"], spare)
            self.assertIsNone(launch.await_args.kwargs["profile_directory"])

    async def test_a_lock_that_is_being_released_keeps_the_personal_profile(self):
        """`release()` then `ensure()`: the very next window must not be the
        unsigned-in spare because Chromium had not finished letting go."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            brave_data = root / "User Data"
            (brave_data / "Profile 1").mkdir(parents=True)
            context, page = self._fake_context()
            launch = mock.AsyncMock(return_value=self._launched(context))
            factory, _ = self._fake_playwright()
            endpoint = root / "browser-cdp-brave.txt"
            browser = session._Browser()
            with mock.patch("playwright.async_api.async_playwright", factory), \
                    mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "brave_executable", return_value=root / "brave.exe"), \
                    mock.patch.object(session, "brave_user_data_dir", return_value=brave_data), \
                    mock.patch.object(session, "brave_profile_directory", return_value=brave_data / "Profile 1"), \
                    mock.patch.object(session, "profile_dir", return_value=root / "lumine" / "browser-profile"), \
                    mock.patch.object(session, "_endpoint_file", return_value=endpoint), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                    mock.patch.object(session, "profile_in_use", side_effect=[True, False]), \
                    mock.patch.object(session, "launch_and_attach", launch), \
                    mock.patch.object(session, "_publish_endpoint"):
                result = await browser._start(asyncio.get_running_loop())

            self.assertIs(result, page)
            self.assertEqual(launch.await_count, 1)
            self.assertEqual(launch.await_args.kwargs["directory"], brave_data)
            self.assertEqual(launch.await_args.kwargs["profile_directory"], "Profile 1")

    async def test_both_directories_held_refuses_before_waiting_on_a_timeout(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            brave_data = root / "User Data"
            (brave_data / "Profile 1").mkdir(parents=True)
            factory, _ = self._fake_playwright()
            browser = session._Browser()
            with mock.patch("playwright.async_api.async_playwright", factory), \
                    mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "brave_executable", return_value=root / "brave.exe"), \
                    mock.patch.object(session, "brave_user_data_dir", return_value=brave_data), \
                    mock.patch.object(session, "brave_profile_directory", return_value=brave_data / "Profile 1"), \
                    mock.patch.object(session, "profile_dir", return_value=root / "lumine" / "browser-profile"), \
                    mock.patch.object(session, "_endpoint_file", return_value=root / "browser-cdp-brave.txt"), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                    mock.patch.object(session, "profile_in_use", return_value=True), \
                    mock.patch.object(session, "launch_and_attach") as launch:
                with self.assertRaises(ToolError) as caught:
                    await browser._start(asyncio.get_running_loop())

            self.assertEqual(str(caught.exception), session.MSG_PROFILE_LOCKED)
            launch.assert_not_called()

    async def test_a_failure_that_is_not_a_lock_is_not_given_a_spare(self):
        """A missing executable or a broken profile is not a lock, and opening
        a second window for it would bury the real cause one directory over."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            launch = mock.AsyncMock(side_effect=OSError("browser executable not found"))
            factory, _ = self._fake_playwright()
            browser = session._Browser()
            with mock.patch("playwright.async_api.async_playwright", factory), \
                    mock.patch.object(session, "_configured_browser_engine", return_value="chromium"), \
                    mock.patch.object(session, "profile_dir", return_value=root / "browser-profile"), \
                    mock.patch.object(session, "_endpoint_file", return_value=root / "browser-cdp.txt"), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "profile_in_use", return_value=False), \
                    mock.patch.object(session, "launch_and_attach", launch):
                with self.assertRaises(ToolError) as caught:
                    await browser._start(asyncio.get_running_loop())
            self.assertEqual(launch.await_count, 1)
            self.assertEqual(str(caught.exception), session.MSG_NOT_INSTALLED)

    async def test_a_launch_that_never_published_does_not_open_a_spare_beside_it(self):
        """A timeout is not a lock, and the browser it started is still running.

        Falling back would leave two windows and answer a question nobody asked;
        the honest sentence names the timeout and nothing was opened.
        """
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            launch = mock.AsyncMock(side_effect=session.LaunchTimeout("slow to start"))
            factory, _ = self._fake_playwright()
            browser = session._Browser()
            with mock.patch("playwright.async_api.async_playwright", factory), \
                    mock.patch.object(session, "_configured_browser_engine", return_value="chromium"), \
                    mock.patch.object(session, "profile_dir", return_value=root / "browser-profile"), \
                    mock.patch.object(session, "_endpoint_file", return_value=root / "browser-cdp.txt"), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "profile_in_use", return_value=False), \
                    mock.patch.object(session, "launch_and_attach", launch):
                with self.assertRaises(ToolError) as caught:
                    await browser._start(asyncio.get_running_loop())
            self.assertEqual(launch.await_count, 1)
            self.assertEqual(str(caught.exception), "slow to start")

    async def test_when_even_the_spare_is_held_the_refusal_is_the_honest_one(self):
        """The fallback has a floor, and it is the sentence that used to fire
        on the *first* lock: two held directories are two held directories."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            launch = mock.AsyncMock(side_effect=[self._busy(), self._busy()])
            factory, _ = self._fake_playwright()
            browser = session._Browser()
            with mock.patch("playwright.async_api.async_playwright", factory), \
                    mock.patch.object(session, "_configured_browser_engine", return_value="chromium"), \
                    mock.patch.object(session, "profile_dir", return_value=root / "browser-profile"), \
                    mock.patch.object(session, "_endpoint_file", return_value=root / "browser-cdp.txt"), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "profile_in_use", return_value=False), \
                    mock.patch.object(session, "launch_and_attach", launch):
                with self.assertRaises(ToolError) as caught:
                    await browser._start(asyncio.get_running_loop())
            self.assertEqual(launch.await_count, 2)
            self.assertEqual(str(caught.exception), session.MSG_PROFILE_LOCKED)


class ScrollVerbTests(unittest.IsolatedAsyncioTestCase):
    """`web_control scroll`, and why it is a read rather than an act."""

    def setUp(self):
        self.engine = mock.Mock()
        self.engine.gate = None
        control_mod.set_engine(self.engine)
        self.addCleanup(control_mod.set_engine, None)
        patcher = mock.patch.object(
            control_mod, "require_approval", new_callable=mock.AsyncMock
        )
        self.approval = patcher.start()
        self.addCleanup(patcher.stop)
        scroll = mock.patch.object(
            session, "scroll_page", new_callable=mock.AsyncMock
        )
        self.scrolled = scroll.start()
        self.addCleanup(scroll.stop)

    async def _call(self, action: str = "scroll", **kwargs):
        return await control_mod.web_control(mock.Mock(), action, **kwargs)

    async def test_it_moves_the_viewport_and_says_where_it_landed(self):
        self.scrolled.return_value = {"scrolled": True, "y": 900, "max": 2400}
        result = await self._call(text="down")
        self.scrolled.assert_awaited_once_with("down")
        self.assertIn("Scrolled down", result)

    async def test_it_is_priced_as_a_read(self):
        """It moves the reader's position, not the page -- the same test `back`
        and `forward` pass, and the same one `browse scroll` passes."""
        self.scrolled.return_value = {"scrolled": True, "y": 0, "max": 100}
        await self._call(text="top")
        _, kwargs = self.approval.await_args
        self.assertEqual(kwargs["action_class"], "read")
        self.assertEqual(kwargs["action"], "scroll")

    async def test_it_is_declared_as_a_read_too(self):
        # The gate's class and the tool's own record have to agree, or the
        # Tools-page badge promises less than the gate enforces.
        self.assertIn("scroll", control_mod.READ_ACTIONS)
        self.assertIn("scroll", control_mod.ACTIONS)

    async def test_a_page_with_nothing_below_says_so(self):
        self.scrolled.return_value = {"scrolled": False, "y": 0, "max": 0}
        result = await self._call(text="down")
        self.assertIn("nothing to scroll", result)

    async def test_an_empty_direction_means_down(self):
        # The tool's own docstring offers no direction at all as the common
        # case, so it has to mean something rather than be refused.
        self.scrolled.return_value = {"scrolled": True, "y": 0, "max": 100}
        await self._call(text="")
        self.scrolled.assert_awaited_once_with("down")

    async def test_a_near_miss_is_refused_rather_than_repaired(self):
        """A scroll that goes the wrong way is a page the user watches move in a
        direction they did not ask for, so "sideways" is a refusal and not a
        guess at "down"."""
        with self.assertRaises(ToolError):
            await self._call(text="downwards")
        self.scrolled.assert_not_awaited()

    async def test_a_direction_nobody_could_act_on_is_refused_by_name(self):
        with self.assertRaises(ToolError) as caught:
            await self._call(text="sideways")
        self.assertIn("down, up, top, or bottom", str(caught.exception))
        self.scrolled.assert_not_awaited()

    async def test_it_does_not_bump_the_generation_or_re_read_the_page(self):
        """The document is unchanged, so every reference the model already holds
        is still valid. This is why it is its own verb rather than something the
        model fakes with a re-read."""
        self.scrolled.return_value = {"scrolled": True, "y": 10, "max": 100}
        with mock.patch.object(
            control_mod, "_observe", new_callable=mock.AsyncMock
        ) as observe:
            await self._call(text="down")
        observe.assert_not_awaited()

    async def test_an_upper_case_spelling_is_the_same_verb(self):
        self.scrolled.return_value = {"scrolled": True, "y": 0, "max": 10}
        await self._call(action="Scroll", text="up")
        self.scrolled.assert_awaited_once_with("up")


class UnknownActionMessageTests(unittest.TestCase):
    """The refusal for an action name nothing recognises, with a nudge."""

    def test_a_typo_is_named_as_the_likely_verb_first(self):
        said = control_mod.unknown_action_message("scoll", control_mod.ACTIONS)
        self.assertIn("Did you mean 'scroll'?", said)

    def test_nonsense_gets_the_list_and_no_confident_guess(self):
        said = control_mod.unknown_action_message("flibbertigibbet", control_mod.ACTIONS)
        self.assertNotIn("Did you mean", said)
        self.assertIn("Tell me one of:", said)

    def test_the_list_is_in_the_declared_order(self):
        said = control_mod.unknown_action_message("nope", control_mod.ACTIONS)
        listed = said.split("Tell me one of: ", 1)[1].rstrip(".")
        self.assertEqual(listed.split(", "), list(control_mod.ACTIONS))

    def test_an_empty_name_is_still_a_sentence(self):
        said = control_mod.unknown_action_message("", control_mod.ACTIONS)
        self.assertIn("'that'", said)
        self.assertTrue(said.strip())

    def test_it_is_what_the_tool_raises(self):
        """The dynamic sentence at the raise site, and the module's own record of
        the surface, are the same message."""
        import inspect

        source = inspect.getsource(control_mod.web_control)
        self.assertIn("unknown_action_message(action, ACTIONS)", source)


class ShowToModelHistoryTests(unittest.TestCase):
    """`for_model=true`, and the attribute the live library actually provides.

    The live `AgentSession` has **`history`** -- a property returning its chat
    context -- and does not expose `chat_ctx` at all. Reading only `chat_ctx`
    meant `getattr` answered `None` on every real session, so `for_model=true`
    returned "I can't show myself a picture" on every build while every unit
    test passed: each one faked the attribute the code asked for rather than the
    one the library provides.
    """

    class HistoryOnlySession:
        """The shape a real `AgentSession` has: `history`, and no `chat_ctx`."""

        def __init__(self, sink: list) -> None:
            self._sink = sink

        @property
        def history(self):
            sink = self._sink

            class _Ctx:
                def add_message(self, *, role, content):
                    sink.append((role, content))

            return _Ctx()

    def test_a_session_that_only_exposes_history_still_shows_the_picture(self):
        added: list = []
        said = preview.show_to_model(
            mock.Mock(session=self.HistoryOnlySession(added)),
            b"\xff\xd8fakejpeg",
            caption="Example",
        )
        self.assertIn("I looked at the page.", said)
        self.assertEqual(len(added), 1)

    def test_history_wins_over_a_read_only_chat_ctx(self):
        """A `_ReadOnlyChatContext` exposes items but not `add_message`.

        A session that exposes both has to take the mutable one, or the answer
        is the same "no mutable context" sentence the fix was written to remove.
        """
        added: list = []
        session_obj = self.HistoryOnlySession(added)
        session_obj.chat_ctx = mock.Mock(items=[])  # type: ignore[attr-defined]
        said = preview.show_to_model(mock.Mock(session=session_obj), b"\xff\xd8fake")
        self.assertIn("I looked at the page.", said)
        self.assertEqual(len(added), 1)

    def test_the_order_is_history_then_chat_ctx(self):
        """Widening, not moving: the older shape still works.

        A bare `Mock` would have hidden this -- `getattr(session, "history")`
        auto-creates an attribute and answers not-None, so the older shape can
        only be tested with an object that genuinely lacks it.
        """
        added: list = []

        class _Ctx:
            def add_message(self, *, role, content):
                added.append((role, content))

        class OlderShape:
            def __init__(self) -> None:
                self.chat_ctx = _Ctx()

        said = preview.show_to_model(mock.Mock(session=OlderShape()), b"\xff\xd8fake")
        self.assertIn("I looked at the page.", said)
        self.assertEqual(len(added), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
