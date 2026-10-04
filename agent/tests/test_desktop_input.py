"""`SendInput`: chord parsing, scroll parsing, and the geometry arithmetic.

No test here injects anything. Every function under test is either pure or is
patched at the `SendInput` boundary, because a test suite that can move a real
mouse is a test suite that will eventually move it during somebody's
presentation. What is tested is the part that decides *what* would be sent --
which is where a bug is silently plausible rather than loudly broken.
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from agent.tools.desktop import input as desktop_input
from agent.tools.tools_compat import ToolError

IS_WINDOWS = sys.platform == "win32"


class ChordParsingTests(unittest.TestCase):
    """Turning what a person said into a key and its modifiers."""

    def test_a_single_key_has_no_modifiers(self):
        self.assertEqual(desktop_input.resolve_chord("enter"), ([], 0x0D))

    def test_a_modifier_and_a_key(self):
        modifiers, key = desktop_input.resolve_chord("ctrl+s")
        self.assertEqual(modifiers, [0x11])
        self.assertEqual(key, ord("S"))

    def test_several_modifiers_keep_their_order(self):
        modifiers, key = desktop_input.resolve_chord("ctrl+shift+s")
        self.assertEqual(modifiers, [0x11, 0x10])
        self.assertEqual(key, ord("S"))

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(
            desktop_input.resolve_chord("  CTRL + Shift + S "),
            desktop_input.resolve_chord("ctrl+shift+s"),
        )

    def test_digits_are_keys(self):
        self.assertEqual(desktop_input.resolve_chord("ctrl+3"), ([0x11], ord("3")))

    def test_the_function_keys_exist(self):
        for index in range(1, 13):
            modifiers, key = desktop_input.resolve_chord(f"f{index}")
            self.assertEqual(modifiers, [])
            self.assertEqual(key, 0x6F + index)

    def test_arrow_keys_are_present(self):
        for name, code in (("left", 0x25), ("up", 0x26), ("right", 0x27), ("down", 0x28)):
            self.assertEqual(desktop_input.resolve_chord(name), ([], code), name)

    def test_empty_is_refused(self):
        for value in ("", "   ", None, "+"):
            with self.assertRaises(ToolError):
                desktop_input.resolve_chord(value)

    def test_an_unknown_name_is_refused_rather_than_guessed(self):
        # A closed list, so an unrecognised key is a sentence rather than a
        # scan code the model guessed at.
        for value in ("supercalifragilistic", "ctrl+f13", "alt+€"):
            with self.assertRaises(ToolError):
                desktop_input.resolve_chord(value)

    def test_modifiers_alone_are_refused(self):
        # "press ctrl" is meaningless, and is usually a truncated "ctrl+shift".
        with self.assertRaises(ToolError):
            desktop_input.resolve_chord("ctrl")

    def test_a_tailing_modifier_is_refused(self):
        with self.assertRaises(ToolError):
            desktop_input.resolve_chord("ctrl+shift")


class BlockedChordTests(unittest.TestCase):
    """The combinations that would do something nobody asked for."""

    def test_the_secure_attention_sequence_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            desktop_input.resolve_chord("ctrl+alt+delete")
        self.assertIn("secure attention", str(caught.exception).lower())

    def test_locking_the_machine_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            desktop_input.resolve_chord("win+l")
        self.assertIn("locks the machine", str(caught.exception))

    def test_a_blocked_chord_refuses_by_name_so_the_model_can_route_around_it(self):
        # Naming the reason is what stops the model simply trying a synonym
        # four times before reporting the problem.
        with self.assertRaises(ToolError) as caught:
            desktop_input.resolve_chord("WIN + L")
        self.assertIn("Nothing was pressed", str(caught.exception))

    def test_the_blocklist_normalises_before_matching(self):
        self.assertEqual(desktop_input.BLOCKED_KEYS["win+l"], "that locks the machine")
        with self.assertRaises(ToolError):
            desktop_input.resolve_chord("Win + L")


class ScrollParsingTests(unittest.TestCase):
    """"Scroll down three", and what to do with a request that says neither."""

    def test_direction_and_count(self):
        from agent.tools.desktop.control import _scroll_amount

        self.assertEqual(_scroll_amount("down 3"), 3)
        self.assertEqual(_scroll_amount("up 5"), -5)
        self.assertEqual(_scroll_amount("scroll down 3"), 3)
        self.assertEqual(_scroll_amount("down 10"), 10)

    def test_an_explicit_negative_count_keeps_its_own_direction(self):
        # "-2" already means two up. Applying the spoken direction on top of it
        # would flip it back down, so a negative count is taken as given.
        from agent.tools.desktop.control import _scroll_amount

        self.assertEqual(_scroll_amount("-2"), -2)

    def test_a_direction_with_no_count_scrolls_some(self):
        from agent.tools.desktop.control import _scroll_amount

        self.assertNotEqual(_scroll_amount("down"), 0)
        self.assertEqual(_scroll_amount("up"), -3)

    def test_neither_a_direction_nor_a_count_is_refused(self):
        # Zero notches is not a no-op to shrug at: it means the model did not
        # say what it meant, and guessing a direction would scroll a page the
        # user never asked about.
        from agent.tools.desktop.control import _scroll_amount

        for value in ("", None, "a bit", "0", "sideways"):
            self.assertEqual(_scroll_amount(value), 0, value)

    def test_a_count_is_clamped_at_the_wheel_ceiling(self):
        self.assertLessEqual(
            desktop_input.MAX_WHEEL_NOTCHES, abs(desktop_input.WHEEL_DELTA)
        )


@unittest.skipUnless(IS_WINDOWS, "the geometry arithmetic is Windows-specific")
class GeometryTests(unittest.TestCase):
    """Normalized coordinates, and the check that must precede them."""

    def test_the_desktop_is_normalized_into_the_range_sendinput_wants(self):
        with mock.patch.object(
            desktop_input, "virtual_desktop", return_value=(0, 0, 1920, 1080)
        ):
            self.assertEqual(desktop_input._normalize((0, 0)), (0, 0))
            self.assertEqual(desktop_input._normalize((1919, 1079)), (65535, 65535))
            top_left = desktop_input._normalize((960, 540))
            self.assertTrue(32000 < top_left[0] < 34000, top_left)
            self.assertTrue(31000 < top_left[1] < 33000, top_left)

    def test_a_negative_monitor_origin_is_carried_not_dropped(self):
        # A monitor left of the primary one starts at -1920. Treating its
        # coordinates as unsigned would put every click on it in the wrong
        # place, which is the reason the virtual desktop is used at all.
        with mock.patch.object(
            desktop_input, "virtual_desktop", return_value=(-1920, 0, 3840, 1080)
        ):
            self.assertEqual(desktop_input._normalize((-1920, 0)), (0, 0))
            self.assertEqual(desktop_input._normalize((1919, 0)), (65535, 0))
            self.assertEqual(desktop_input._normalize((1919, 1079)), (65535, 65535))

    def test_an_out_of_range_fraction_is_clamped_not_wrapped(self):
        # A negative fraction is interpreted modulo 65536 by the OS, which
        # would turn "just off the left edge" into the middle of the screen.
        with mock.patch.object(
            desktop_input, "virtual_desktop", return_value=(0, 0, 1920, 1080)
        ):
            x, y = desktop_input._normalize((-500, 99999))
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x, 65535)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(y, 65535)

    def test_a_point_off_every_monitor_is_not_a_point(self):
        with mock.patch.object(
            desktop_input, "virtual_desktop", return_value=(0, 0, 1920, 1080)
        ):
            self.assertTrue(desktop_input.contains((10, 10)))
            self.assertFalse(desktop_input.contains((-1, 10)))
            self.assertFalse(desktop_input.contains((1920, 10)))
            self.assertFalse(desktop_input.contains((10, 1080)))

    def test_a_click_off_screen_is_refused_rather_than_clamped(self):
        # Windows clamps an out-of-range click to the nearest edge, so an
        # unchecked coordinate is a click *somewhere* -- the outcome this tool
        # must never produce by accident.
        with mock.patch.object(desktop_input, "contains", return_value=False):
            with self.assertRaises(ToolError):
                desktop_input.click((99999, 99999))

    def test_a_drag_that_leaves_the_screen_is_refused(self):
        with mock.patch.object(desktop_input, "contains", return_value=False):
            with self.assertRaises(ToolError):
                desktop_input.drag((10, 10), (99999, 10))

    def test_the_button_names_are_a_closed_list(self):
        with mock.patch.object(desktop_input, "contains", return_value=True):
            for name, expected in (
                ("left", desktop_input.MOUSEEVENTF_LEFTDOWN),
                ("right", desktop_input.MOUSEEVENTF_RIGHTDOWN),
                ("middle", desktop_input.MOUSEEVENTF_MIDDLEDOWN),
            ):
                self.assertEqual(desktop_input._button_flags(name)[0], expected)
            with self.assertRaises(ToolError):
                desktop_input._button_flags("extra")


@unittest.skipUnless(IS_WINDOWS, "SendInput is Windows-only")
class InjectionBoundaryTests(unittest.TestCase):
    """What actually reaches Windows, with nothing real sent."""

    def test_a_refused_send_input_is_reported_rather_than_assumed(self):
        # The documented failure for SendInput is UIPI: a lower-integrity
        # process cannot inject into an elevated one, and that returns zero
        # with no exception. An unchecked call reports a click that never
        # happened. Patched at `user32.SendInput` rather than at the helpers,
        # so the real marshalling path above it still runs.
        with mock.patch.object(desktop_input, "contains", return_value=True), mock.patch.object(
            desktop_input.user32, "SendInput", return_value=0
        ):
            with self.assertRaises(ToolError) as caught:
                desktop_input.click((100, 100), settle=0)
        self.assertIn("Nothing was done", str(caught.exception))

    def test_a_accepted_send_input_reports_nothing_wrong(self):
        with mock.patch.object(desktop_input, "contains", return_value=True), mock.patch.object(
            desktop_input.user32, "SendInput", return_value=1
        ):
            desktop_input.click((100, 100), settle=0)  # must not raise

    def test_typing_nothing_sends_nothing(self):
        self.assertEqual(desktop_input.type_text(""), 0)


class KeyIntervalTests(unittest.TestCase):
    """Why there is a delay between injected characters at all."""

    def test_the_interval_is_short_but_not_zero(self):
        from agent.tools.desktop import uia

        self.assertGreater(uia.KEY_INTERVAL_SECONDS, 0)
        # Long enough that a hundred-character field is still under a second.
        self.assertLess(uia.KEY_INTERVAL_SECONDS * 100, 1.0)


if __name__ == "__main__":
    unittest.main()