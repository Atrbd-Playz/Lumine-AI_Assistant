from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import open_browser
from agent.tools.browser import session


class OpenBrowserCommandTests(unittest.TestCase):
    def test_command_uses_the_native_brave_profile_and_opens_a_real_new_tab(self):
        command = open_browser._browser_command(
            "brave",
            r"C:\Brave\brave.exe",
            Path("C:/Users/example/AppData/Local/BraveSoftware/Brave-Browser/User Data"),
            "Profile 1",
        )

        self.assertEqual(command[0], r"C:\Brave\brave.exe")
        self.assertIn(
            "--user-data-dir=C:\\Users\\example\\AppData\\Local\\BraveSoftware\\Brave-Browser\\User Data",
            command,
        )
        self.assertIn("--profile-directory=Profile 1", command)
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
            profile = root / "User Data"
            (profile / "Profile 1").mkdir(parents=True)
            process = mock.Mock(spec=subprocess.Popen)
            process.poll.return_value = None
            with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "headless_default", return_value=False), \
                    mock.patch.object(session, "_endpoint_file", return_value=root / "browser-cdp-brave.txt"), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "_endpoint_alive", return_value=False), \
                    mock.patch.object(session, "brave_user_data_dir", return_value=profile), \
                    mock.patch.object(session, "brave_profile_directory", return_value=profile / "Profile 1"), \
                    mock.patch.object(session, "profile_in_use", return_value=False), \
                    mock.patch.object(session, "brave_executable", return_value=Path("C:/Brave/brave.exe")), \
                    mock.patch.object(open_browser, "_launch_detached", return_value=process) as launch, \
                    mock.patch.object(open_browser, "_wait_for_browser", return_value=9471) as wait, \
                    mock.patch.object(session, "_publish_endpoint") as publish:
                selected = open_browser.open_browser()

        self.assertEqual(selected, "brave")
        command = launch.call_args.args[0]
        self.assertEqual(command[0], "C:\\Brave\\brave.exe")
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertIn("--profile-directory=Profile 1", command)
        self.assertEqual(command[-1], "brave://newtab/")
        self.assertEqual(wait.call_args.args[:2], (profile, process))
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

    def test_a_held_profile_opens_the_spare_window_instead_of_refusing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            profile = root / "User Data"
            (profile / "Profile 1").mkdir(parents=True)
            spare = root / "browser-profile-spare-brave"
            process = mock.Mock(spec=subprocess.Popen)
            process.poll.return_value = None
            with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "headless_default", return_value=False), \
                    mock.patch.object(session, "_endpoint_file", return_value=root / "browser-cdp-brave.txt"), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "_endpoint_alive", return_value=False), \
                    mock.patch.object(session, "brave_user_data_dir", return_value=profile), \
                    mock.patch.object(session, "brave_profile_directory", return_value=profile / "Profile 1"), \
                    mock.patch.object(session, "spare_profile_dir", return_value=spare), \
                    mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                    mock.patch.object(
                        session,
                        "profile_in_use",
                        side_effect=lambda directory: Path(directory) == profile,
                    ), \
                    mock.patch.object(session, "brave_executable", return_value=Path("C:/Brave/brave.exe")), \
                    mock.patch.object(open_browser, "_launch_detached", return_value=process) as launch, \
                    mock.patch.object(open_browser, "_wait_for_browser", return_value=9471) as wait, \
                    mock.patch.object(session, "_publish_endpoint"):
                self.assertEqual(open_browser.open_browser(), "brave")

        command = launch.call_args.args[0]
        self.assertIn(f"--user-data-dir={spare}", command)
        self.assertFalse(any(arg.startswith("--profile-directory=") for arg in command))
        self.assertEqual(wait.call_args.args[:2], (spare, process))
        self.assertTrue(wait.call_args.args[2])

    def test_a_lock_that_is_being_released_keeps_the_personal_profile(self):
        """A browser that is *closing* holds the directory exactly as one that
        is open does, and the spare it would fall back to is not signed in."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            profile = root / "User Data"
            (profile / "Profile 1").mkdir(parents=True)
            process = mock.Mock(spec=subprocess.Popen)
            process.poll.return_value = None
            with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "headless_default", return_value=False), \
                    mock.patch.object(session, "_endpoint_file", return_value=root / "browser-cdp-brave.txt"), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "_endpoint_alive", return_value=False), \
                    mock.patch.object(session, "brave_user_data_dir", return_value=profile), \
                    mock.patch.object(session, "brave_profile_directory", return_value=profile / "Profile 1"), \
                    mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                    mock.patch.object(session, "profile_in_use", side_effect=[True, False]), \
                    mock.patch.object(session, "brave_executable", return_value=Path("C:/Brave/brave.exe")), \
                    mock.patch.object(open_browser, "_launch_detached", return_value=process) as launch, \
                    mock.patch.object(open_browser, "_wait_for_browser", return_value=9471) as wait, \
                    mock.patch.object(session, "_publish_endpoint"):
                self.assertEqual(open_browser.open_browser(), "brave")

        command = launch.call_args.args[0]
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertIn("--profile-directory=Profile 1", command)
        self.assertEqual(wait.call_args.args[:2], (profile, process))
        self.assertTrue(wait.call_args.args[2])

    def test_both_directories_held_is_the_one_refusal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            profile = root / "User Data"
            (profile / "Profile 1").mkdir(parents=True)
            spare = root / "browser-profile-spare-brave"
            with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "headless_default", return_value=False), \
                    mock.patch.object(session, "_endpoint_file", return_value=root / "browser-cdp-brave.txt"), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "_endpoint_alive", return_value=False), \
                    mock.patch.object(session, "brave_user_data_dir", return_value=profile), \
                    mock.patch.object(session, "brave_profile_directory", return_value=profile / "Profile 1"), \
                    mock.patch.object(session, "spare_profile_dir", return_value=spare), \
                    mock.patch.object(session, "_LAUNCH_RETRY_SECONDS", 0), \
                    mock.patch.object(session, "profile_in_use", return_value=True), \
                    mock.patch.object(session, "brave_executable", return_value=Path("C:/Brave/brave.exe")), \
                    mock.patch.object(open_browser, "_launch_detached") as launch:
                with self.assertRaisesRegex(RuntimeError, "already open"):
                    open_browser.open_browser()

        launch.assert_not_called()

    def test_a_missing_brave_profile_is_named_before_anything_is_launched(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                    mock.patch.object(session, "headless_default", return_value=False), \
                    mock.patch.object(session, "_endpoint_file", return_value=root / "browser-cdp-brave.txt"), \
                    mock.patch.object(session, "_read_endpoint", return_value=""), \
                    mock.patch.object(session, "_endpoint_alive", return_value=False), \
                    mock.patch.object(session, "brave_user_data_dir", return_value=root / "User Data"), \
                    mock.patch.object(
                        session,
                        "brave_profile_directory",
                        side_effect=ValueError("expected one profile named 'Lumine', found 0"),
                    ), \
                    mock.patch.object(open_browser, "_launch_detached") as launch:
                with self.assertRaisesRegex(RuntimeError, "existing Brave profile named 'Lumine'"):
                    open_browser.open_browser()

        launch.assert_not_called()

    def test_an_explicit_brave_choice_never_silently_opens_chromium(self):
        with mock.patch.object(session, "_configured_browser_engine", return_value="brave"), \
                mock.patch.object(session, "headless_default", return_value=False), \
                mock.patch.object(session, "_endpoint_file", return_value=Path("endpoint.txt")), \
                mock.patch.object(session, "_read_endpoint", return_value=""), \
                mock.patch.object(session, "_endpoint_alive", return_value=False), \
                mock.patch.object(session, "brave_user_data_dir", return_value=Path("C:/profile")), \
                mock.patch.object(session, "brave_profile_directory", return_value=Path("C:/profile/Profile 1")), \
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
