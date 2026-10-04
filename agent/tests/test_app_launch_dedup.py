"""The launch guard, which is what stops one request producing two windows.

These tests exist because the defect they cover was reported against a real
session: asking Lumine to open an app opened two of them. The cause was not
``launch()`` -- one call starts exactly one process, asserted in
``test_pipeline_runtime`` -- so it has to be the tool being *called* twice, and
nothing above the tool can see that. Hence a guard here, and hence tests that
assert the guard rather than the launcher.

Everything below drives ``open_app`` itself with ``launch`` patched out, so no
test in this file can start a real program.
"""

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.tools import apps
from agent.tools.tools_compat import ToolError

ENV_WINDOW = "LUMINE_LAUNCH_DEDUP_SECONDS"


def context() -> SimpleNamespace:
    """A run context whose interruption latch records whether it was pulled."""
    state = SimpleNamespace(calls=0)

    def latch() -> None:
        state.calls += 1

    state.disallow_interruptions = latch
    return state


def call(name: str) -> str:
    return asyncio.run(apps.open_app(context(), name))


class LaunchDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        apps.reset_launch_history()
        # A developer's own value must not decide what these tests assert.
        self._saved_window = os.environ.pop(ENV_WINDOW, None)

    def tearDown(self) -> None:
        apps.reset_launch_history()
        if self._saved_window is None:
            os.environ.pop(ENV_WINDOW, None)
        else:
            os.environ[ENV_WINDOW] = self._saved_window

    def test_one_request_launches_once(self) -> None:
        with patch.object(apps, "launch", return_value="Opened notepad.") as launch:
            self.assertEqual(call("notepad"), "Opened notepad.")
            self.assertEqual(call("notepad"), "I already opened notepad just now.")
        launch.assert_called_once_with("notepad")

    def test_the_suppressed_answer_is_something_the_model_can_say(self) -> None:
        # A tool result is re-sent on every later turn, so a bare `False` or a
        # stack trace here would be spoken aloud. The wording also states a
        # fact we actually know -- *we* opened it moments ago -- rather than
        # claiming to have looked at the user's screen.
        with patch.object(apps, "launch", return_value="Opened notepad."):
            call("notepad")
            suppressed = call("notepad")
        self.assertIn("already opened", suppressed)
        self.assertIn("notepad", suppressed)

    def test_case_and_extension_variants_are_the_same_app(self) -> None:
        # A model that spells one request three ways must not get three
        # windows; `candidates()` exists because models do spell it three ways.
        with patch.object(apps, "launch", return_value="Opened.") as launch:
            call("Notepad")
            call("notepad")
            call("notepad.exe")
        launch.assert_called_once()

    def test_a_different_app_is_not_caught_by_the_same_window(self) -> None:
        with patch.object(apps, "launch", return_value="Opened.") as launch:
            call("notepad")
            call("calc")
        self.assertEqual(launch.call_count, 2)

    def test_a_request_after_the_window_launches_again(self) -> None:
        # 0s for the first claim, then 10.5s -- past the 10s default. The gap
        # is deliberately the same request asked for again, which is a real
        # thing a person does and must not be refused.
        with patch.object(apps, "launch", return_value="Opened.") as launch, patch.object(
            apps, "_now", side_effect=[0.0, 10.5]
        ):
            call("notepad")
            call("notepad")
        self.assertEqual(launch.call_count, 2)

    def test_a_launch_that_failed_does_not_block_a_retry(self) -> None:
        # The reservation is released on failure because a model told
        # "already open" about a launch that never happened will not try
        # again, and the user is left with silence instead of an app.
        with patch.object(apps, "launch", side_effect=[ToolError("denied"), "Opened notepad."]) as launch:
            with self.assertRaises(ToolError):
                call("notepad")
            self.assertEqual(call("notepad"), "Opened notepad.")
        self.assertEqual(launch.call_count, 2)

    def test_zero_turns_the_guard_off(self) -> None:
        os.environ[ENV_WINDOW] = "0"
        with patch.object(apps, "launch", return_value="Opened.") as launch:
            call("notepad")
            call("notepad")
        self.assertEqual(launch.call_count, 2)

    def test_a_narrower_window_from_the_environment_is_honoured(self) -> None:
        os.environ[ENV_WINDOW] = "3"
        with patch.object(apps, "launch", return_value="Opened.") as launch, patch.object(
            apps, "_now", side_effect=[0.0, 2.0]
        ):
            call("notepad")
            call("notepad")
        launch.assert_called_once()

        apps.reset_launch_history()
        with patch.object(apps, "launch", return_value="Opened.") as launch, patch.object(
            apps, "_now", side_effect=[0.0, 4.0]
        ):
            call("notepad")
            call("notepad")
        self.assertEqual(launch.call_count, 2)

    def test_interruptions_are_still_disallowed_when_suppressed(self) -> None:
        # The latch runs before the guard, so a suppressed call still holds
        # the turn it is refusing to interrupt.
        with patch.object(apps, "launch", return_value="Opened."):
            first = context()
            asyncio.run(apps.open_app(first, "notepad"))
            second = context()
            asyncio.run(apps.open_app(second, "notepad"))
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)

    def test_an_unusable_window_value_falls_back(self) -> None:
        # A typo in a config file must not be able to switch launches off
        # silently, nor wedge them off for a year.
        for raw, expected in (("later", 10.0), ("nan", 10.0), ("inf", 10.0), ("-5", 0.0), ("99999", 3600.0)):
            with self.subTest(raw=raw):
                os.environ[ENV_WINDOW] = raw
                self.assertEqual(apps.dedup_window(), expected)

    def test_launch_key_normalises(self) -> None:
        self.assertEqual(apps.launch_key("  Notepad  "), "notepad")
        self.assertEqual(apps.launch_key("NOTEPAD.EXE"), "notepad")
        self.assertEqual(apps.launch_key("Visual Studio Code.exe"), "visual studio code")


if __name__ == "__main__":
    unittest.main()
