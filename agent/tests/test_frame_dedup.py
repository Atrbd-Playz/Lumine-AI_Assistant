"""The dedup has to survive a real screen, not a static image.

A shared desktop is never still: a caret blinks, a pointer moves, a taskbar clock
ticks. The exact-CRC test this replaces dropped 2 frames out of ~200 on a live
session -- it was not deduplicating, it was paying a full-buffer CRC to conclude
"something, somewhere, changed". These pin the test that does work, and pin the
backoff, without needing a browser or a codec.

They also pin the *shape* of the fake plane, which is not a detail. The first
live run of the vision pipeline found every frame dying inside
``luma_signature`` with ``NotImplementedError: memoryview: unsupported format
<B`` -- the real exporter's format -- while these tests were green, because a
``memoryview`` built over ``bytes`` is format ``"B"`` and indexes fine. The fake
now fails the way the SDK does, so it can catch what it used to hide.
"""

from __future__ import annotations

import ctypes
import unittest
from unittest import mock

from agent.runtime import frame_sampler as fs
from agent.runtime.frame_sampler import FreshnessVideoSampler


class FakeFrame:
    """A frame whose luma plane we control, one byte per pixel."""

    def __init__(self, width: int = 640, height: int = 360, value: int = 100, data: bytes | None = None):
        self.width = width
        self.height = height
        self.type = 0
        self._data = data if data is not None else bytes([value]) * (width * height)

    @property
    def data(self):
        return self._data

    def get_plane(self, plane_nth: int = 0):
        # ``"<B"``, not ``"B"``: ctypes' uint8 array is the stdlib's only
        # exporter of a byte-*ordered* memoryview format, which is the shape
        # livekit-rtc really hands over -- and which CPython refuses to index.
        # See the note on ``test_vision_scheduling.PixelFrame``.
        return memoryview((ctypes.c_uint8 * len(self._data)).from_buffer_copy(self._data))


def with_pixel(frame: FakeFrame, index: int, value: int) -> FakeFrame:
    """A copy with one pixel changed -- a caret, a pointer, a clock tick."""
    buf = bytearray(frame._data)
    buf[index] = value
    return FakeFrame(frame.width, frame.height, data=bytes(buf))


def with_block(frame: FakeFrame, top: int, count: int, value: int) -> FakeFrame:
    """A copy with a region changed -- a dialog, a scroll, a video starting."""
    buf = bytearray(frame._data)
    for offset in range(top, min(top + count, len(buf))):
        buf[offset] = value
    return FakeFrame(frame.width, frame.height, data=bytes(buf))


class SignatureTests(unittest.TestCase):
    def test_an_unchanged_frame_has_an_identical_signature(self):
        self.assertEqual(
            fs.luma_signature(FakeFrame()), fs.luma_signature(FakeFrame())
        )

    def test_one_moved_pixel_is_not_a_new_picture(self):
        # The whole reason the exact CRC was abandoned. 1 cell of 576.
        before = fs.luma_signature(FakeFrame())
        after = fs.luma_signature(with_pixel(FakeFrame(), 12_345, 250))
        self.assertLess(fs.change_ratio(before, after), fs.CHANGE_RATIO)

    def test_a_moving_pointer_is_not_a_new_picture(self):
        before = fs.luma_signature(FakeFrame())
        after = fs.luma_signature(with_block(FakeFrame(), 20_000, 40, 250))
        self.assertLess(fs.change_ratio(before, after), fs.CHANGE_RATIO)

    def test_a_real_change_is_a_new_picture(self):
        # A dialog covering a good part of the screen. This is the direction that
        # matters: a false negative here means she answers about a stale screen.
        before = fs.luma_signature(FakeFrame())
        after = fs.luma_signature(with_block(FakeFrame(), 0, 40_000, 250))
        self.assertGreaterEqual(fs.change_ratio(before, after), fs.CHANGE_RATIO)

    def test_a_frame_it_cannot_read_counts_as_changed(self):
        # Never "unchanged": sending costs tokens, missing a change costs an answer.
        self.assertEqual(fs.change_ratio(None, (1, 2, 3)), 1.0)
        self.assertEqual(fs.change_ratio((1, 2, 3), None), 1.0)
        self.assertEqual(fs.change_ratio((1, 2), (1, 2, 3)), 1.0)

    def test_an_unreadable_plane_falls_back_rather_than_raising(self):
        class NoPlane:
            width = height = 64
            data = b""

            def get_plane(self, _n):
                raise RuntimeError("no plane here")

        self.assertIsNone(fs.luma_signature(NoPlane()))

    def test_the_fixture_plane_is_shaped_like_the_sdk(self):
        """The fake has to be able to fail the way the SDK fails.

        ``memoryview(bytes)`` is format ``"B"`` and indexes; ``livekit-rtc``'s
        plane is format ``"<B"`` and raises ``NotImplementedError`` on the same
        expression. A fake built the easy way therefore indexes fine, and every
        regression in this module becomes invisible -- which is how the video
        forward task stayed dead on every real frame behind a green suite.
        """
        plane = FakeFrame().get_plane(0)
        self.assertEqual(plane.format, "<B")
        with self.assertRaises(NotImplementedError):
            int(plane[0])
        # ... and the sampler reads it anyway, because that is the whole point.
        self.assertIsNotNone(fs.luma_signature(FakeFrame()))


class DeduplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = mock.Mock()
        self.clock.return_value = 0.0
        self.sampler = FreshnessVideoSampler(
            speaking_fps=1.0, silent_fps=0.3, clock=self.clock
        )
        self.session = mock.Mock()
        self.session.user_state = "listening"

    def _deliver(self, frame, *, at: float, gap: float = 0.4):
        self.clock.return_value = at
        return self.sampler(frame, self.session)

    def test_a_still_screen_is_sampled_once_and_then_left_alone(self):
        first = FakeFrame()
        self.assertTrue(self._deliver(first, at=0.0), "the first frame of a stream is sent")
        sent = 0
        step = 1.0 / 15.0
        at = step
        for _ in range(600):  # ~40 seconds of an untouched screen at a real rate
            if self._deliver(FakeFrame(), at=at):
                sent += 1
            at += step
        self.assertEqual(sent, 0, "an unmoving screen kept being sent")
        self.assertEqual(self.sampler.accepted, 1)

    def test_a_caret_blinking_does_not_resume_the_stream(self):
        self.assertTrue(self._deliver(FakeFrame(), at=0.0))
        sent = 0
        step = 1.0 / 15.0
        at = step
        for index in range(300):
            # Every other frame has one pixel toggled: a text caret.
            frame = with_pixel(FakeFrame(), 9_000, 255 if index % 2 else 0)
            if self._deliver(frame, at=at):
                sent += 1
            at += step
        self.assertEqual(sent, 0, "a blinking caret was treated as a new screen")
        self.assertEqual(self.sampler.accepted, 1)

    def test_a_real_change_is_still_delivered_after_a_quiet_run(self):
        # The honest contract, not the flattering one. The backoff stretches
        # *sampling*, so a change on a screen nobody has asked about can wait up
        # to one interval. What must not happen is it being dropped outright, or
        # the quiet run not resetting once it arrives.
        step = 1.0 / 15.0
        self.assertTrue(self._deliver(FakeFrame(), at=0.0))
        at = step
        while at < 40.0:  # an untouched screen for forty seconds
            self._deliver(FakeFrame(), at=at)
            at += step
        self.assertGreater(self.sampler._quiet, 0, "expected a quiet run to build")
        self.assertGreater(
            self.sampler._interval_for(self.session),
            1.0,
            "expected the interval to have stretched",
        )
        changed = with_block(FakeFrame(), 0, 40_000, 250)
        delivered_at = None
        for _ in range(int(fs.MAX_QUIET_INTERVAL / step) + 4):
            if self._deliver(changed, at=at):
                delivered_at = at
                break
            at += step
        self.assertIsNotNone(
            delivered_at,
            "a real change was never delivered, however long the backoff",
        )
        self.assertLessEqual(
            delivered_at - 40.0,
            fs.MAX_QUIET_INTERVAL + step,
            "a change waited longer than the documented worst case",
        )
        self.assertEqual(self.sampler._quiet, 0, "the change should reset the quiet run")

    def test_a_fresh_request_is_never_delayed_by_the_backoff(self):
        # A turn starting must not wait out a stretched interval: the user asked
        # a question about what is on screen, and a stale answer costs more than
        # the frame.
        step = 1.0 / 15.0
        self.assertTrue(self._deliver(FakeFrame(), at=0.0))
        at = step
        while at < 40.0:
            self._deliver(FakeFrame(), at=at)
            at += step
        self.assertGreater(self.sampler._interval_for(self.session), 1.0)
        self.sampler.request_fresh()
        changed = with_block(FakeFrame(), 0, 40_000, 250)
        self.assertTrue(
            self._deliver(changed, at=at),
            "a fresh request was still made to wait out the backoff",
        )

    def test_the_interval_stretches_while_nothing_is_changing(self):
        self.session.user_state = "listening"
        base = self.sampler._interval_for(self.session)
        for _ in range(6):
            self.sampler._quiet += 1
        stretched = self.sampler._interval_for(self.session)
        self.assertGreater(stretched, base)
        self.assertLessEqual(stretched, fs.MAX_QUIET_INTERVAL)

    def test_the_backoff_never_applies_while_the_user_is_talking(self):
        # The rule that matters most. The speaking rate exists because somebody is
        # describing the screen to Lumine; stretching it to ten seconds mid
        # conversation trades a real answer for a few saved tokens.
        self.session.user_state = "speaking"
        base = self.sampler._interval_for(self.session)
        for _ in range(8):
            self.sampler._quiet += 1
        self.assertEqual(
            self.sampler._interval_for(self.session),
            base,
            "the frame rate was cut while the user was speaking",
        )

    def test_the_disabled_budget_sends_nothing(self):
        sampler = FreshnessVideoSampler(speaking_fps=0.0, silent_fps=0.0, clock=self.clock)
        self.clock.return_value = 0.0
        self.assertFalse(sampler(FakeFrame(), self.session))
        self.assertEqual(sampler.dropped_budget, 1)


if __name__ == "__main__":
    unittest.main()
