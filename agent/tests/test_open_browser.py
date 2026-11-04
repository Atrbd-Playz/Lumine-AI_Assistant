from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import open_browser
from agent.tools.browser import session


class OpenBrowserCommandTests(unittest.TestCase):
    def test_command_targets_lumines_own_directory_and_opens_a_real_new_tab(self):
        """Full isolation: her own user-data directory, and no native-profile
        flag. The user's Brave is untouched by construction."""
        directory = (
            Path("C:/Users/example/AppData/Local")
            / "lumine"
            / "browser-profile-brave"
        )
        command = open_browser._browser_command(
            "brave",
            r"C:\Brave\brave.exe",
            directory,
        )

        self.assertEqual(command[0], r"C:\Brave\brave.exe")
        self.assertIn(
            "--user-data-dir=C:\\Users\\example\\AppData\\Local\\lumine\\browser-profile-brave",
            command,
        )
        self.assertFalse(
            any(arg.startswith("--profile-directory") for arg in command)
        )
        port_flag = next(
            argument for argument in command
            if argument.startswith("--remote-debugging-port=")
        )
        self.assertNotEqual(port_flag, "--remote-debugging-port=0")
        self.assertEqual(command[-1], "brave://newtab/")
        self.assertFalse(any("disable" in argument.casefold() for argument in command))

    def test_existing_endpoint_is_focused_instead_of_launching_another_window(self):
        with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                mock.patch.object(session, "headless_default", return_value=False), \
                mock.patch.object(session, "_endpoint_file", return_value=Path("browser-cdp-brave.txt")), \
                mock.patch.object(session, "_read_endpoint", return_value="http://127.0.0.1:9333"), \
                mock.patch.object(session, "_endpoint_alive", return_value=True), \
                mock.patch.object(open_browser, "_focus_browser", new_callable=mock.AsyncMock) as focus, \
                mock.patch.object(open_browser, "_launch_detached") as launch:
            selected = open_browser.open_browser()

        self.assertEqual(selected, "brave")
        focus.assert_awaited_once_with("brave")
        launch.assert_not_called()

    def test_a_fresh_brave_runtime_publishes_its_actual_devtools_port(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            directory = root / "lumine" / "browser-profile-brave"
            process = mock.Mock(spec=subprocess.Popen)
            process.poll.return_value = None
            with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "headless_default", return_value=False), \
                    mock.patch.object(session, "profile_dir", return_value=directory), \
                    mock.patch.object(session, "_endpoint_file", return_value=root / "browser-cdp-brave.txt"), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "_endpoint_alive", return_value=False), \
                    mock.patch.object(session, "profile_in_use", return_value=False), \
                    mock.patch.object(session, "brave_executable", return_value=Path("C:/Brave/brave.exe")), \
                    mock.patch.object(open_browser, "_launch_detached", return_value=process) as launch, \
                    mock.patch.object(open_browser, "_wait_for_browser", return_value=9471) as wait, \
                    mock.patch.object(session, "_publish_endpoint") as publish:
                selected = open_browser.open_browser()

        self.assertEqual(selected, "brave")
        command = launch.call_args.args[0]
        self.assertEqual(command[0], "C:\\Brave\\brave.exe")
        self.assertIn(f"--user-data-dir={directory}", command)
        self.assertFalse(
            any(arg.startswith("--profile-directory") for arg in command)
        )
        self.assertEqual(command[-1], "brave://newtab/")
        self.assertEqual(wait.call_args.args[:2], (directory, process))
        # The number in the command line is the number waited on -- the two
        # lose each other exactly when everything page-facing breaks.
        port_flag = next(
            a for a in command if a.startswith("--remote-debugging-port=")
        )
        self.assertEqual(port_flag.split("=")[1], str(wait.call_args.args[2]))
        publish.assert_called_once_with(
            9471,
            root / "browser-cdp-brave.txt",
        )

    def test_a_held_directory_refuses_as_hers_never_as_the_users(self):
        """The user's Brave being open is *not* this refusal: her directory is
        only ever held by a browser she started. Full isolation removed the
        spare, so the one refusal left names a browser of hers."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            directory = root / "browser-profile-brave"
            with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "headless_default", return_value=False), \
                    mock.patch.object(session, "profile_dir", return_value=directory), \
                    mock.patch.object(session, "brave_executable", return_value=None), \
                    mock.patch.object(session, "profile_in_use", return_value=True), \
                    mock.patch.object(open_browser, "_launch_detached") as launch:
                with self.assertRaisesRegex(
                    RuntimeError, "Lumine's own browser directory"
                ):
                    open_browser.open_browser()

        launch.assert_not_called()

    def test_a_held_directory_with_a_live_marker_is_focused_not_refused(self):
        """The Open browser button recovers the same orphan the worker does.

        The refusal is the floor, not the first word: the marker read
        (`_active_port`, one attempt over `DevToolsActivePort`) runs before
        `profile_in_use`, so a browser holding her directory with no endpoint
        file is joined and focal, not refused."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            directory = root / "browser-profile-brave"
            endpoint_file = root / "browser-cdp-brave.txt"
            with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "headless_default", return_value=False), \
                    mock.patch.object(session, "profile_dir", return_value=directory), \
                    mock.patch.object(session, "_endpoint_file", return_value=endpoint_file), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "_endpoint_alive", return_value=True), \
                    mock.patch.object(session, "profile_in_use", return_value=True), \
                    mock.patch.object(open_browser, "_active_port", return_value=7021), \
                    mock.patch.object(session, "_publish_endpoint") as publish, \
                    mock.patch.object(
                        open_browser, "_focus_browser", new_callable=mock.AsyncMock
                    ) as focus, \
                    mock.patch.object(open_browser, "_launch_detached") as launch:
                self.assertEqual(open_browser.open_browser(), "brave")

        publish.assert_called_once_with(7021, endpoint_file)
        focus.assert_awaited_once_with("brave")
        launch.assert_not_called()

    def test_an_explicit_brave_choice_never_silently_opens_chromium(self):
        with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                mock.patch.object(session, "headless_default", return_value=False), \
                mock.patch.object(session, "profile_dir", return_value=Path("C:/profile")), \
                mock.patch.object(session, "_endpoint_file", return_value=Path("endpoint.txt")), \
                mock.patch.object(session, "_read_endpoint", return_value=""), \
                mock.patch.object(session, "_endpoint_alive", return_value=False), \
                mock.patch.object(session, "profile_in_use", return_value=False), \
                mock.patch.object(session, "brave_executable", return_value=None), \
                mock.patch.object(open_browser, "_launch_detached") as launch:
            with self.assertRaisesRegex(RuntimeError, "Brave is not installed"):
                open_browser.open_browser()

        launch.assert_not_called()

    def test_headless_mode_refuses_to_claim_it_opened_a_window(self):
        with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                mock.patch.object(session, "headless_default", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "without a window"):
                open_browser.open_browser()


class FocusExistingBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_a_blank_tab_is_changed_to_the_browser_new_tab_page(self):
        page = mock.Mock()
        page.url = "about:blank"
        page.goto = mock.AsyncMock()
        browser = mock.Mock()
        browser.ensure = mock.AsyncMock(return_value=page)
        browser._reveal = mock.AsyncMock()
        browser.release = mock.AsyncMock()

        with mock.patch.object(open_browser.session, "_Browser", return_value=browser):
            await open_browser._focus_browser("brave")

        page.goto.assert_awaited_once_with("brave://newtab/")
        browser._reveal.assert_awaited_once_with(page)
        browser.release.assert_awaited_once()

    async def test_existing_page_is_focused_without_navigation(self):
        page = mock.Mock()
        page.url = "https://example.com/"
        page.goto = mock.AsyncMock()
        browser = mock.Mock()
        browser.ensure = mock.AsyncMock(return_value=page)
        browser._reveal = mock.AsyncMock()
        browser.release = mock.AsyncMock()

        with mock.patch.object(open_browser.session, "_Browser", return_value=browser):
            await open_browser._focus_browser("brave")

        page.goto.assert_not_awaited()
        browser._reveal.assert_awaited_once_with(page)
        browser.release.assert_awaited_once()
