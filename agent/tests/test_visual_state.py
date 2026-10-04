"""What Lumine can currently see, how old it is, and what it refuses to say.

Two distinctions carry the whole module, and both are the kind of thing that
silently rots into the opposite of what was meant:

* **Frame age and content age are different numbers.** A screen nobody has
  touched for a minute is not a stale observation -- it is an accurate one. What
  would be wrong is describing it as *newly* seen, which is what `frame_age_ms`
  and `content_age_ms` let the consumer tell apart.
* **Publishing is not the state.** The record is throttled, may be switched off,
  and may raise into a dead pipe. None of that may stop the counters, the ages
  or the source being right, because those are what a later turn reads to decide
  whether it can still honestly claim to be looking at something.

The clock starts high on purpose. `_last_emit_at` is seeded at `0.0` as "never",
which is only true because a monotonic clock does not rewind; a fake one
starting at zero would be testing the sentinel rather than the throttle.
"""

from __future__ import annotations

import unittest

from agent.runtime.visual_state import DEFAULT_EMIT_INTERVAL, REASONS, VisualState

#: A monotonic value that has been running for a while, as `time.monotonic()`
#: would be by the time a session exists.
START = 5000.0

#: A wall-clock value, for the fields a human timestamp reads.
WALL = 1760000000.0


class FakeClock:
    def __init__(self, start: float = START) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def at(self, seconds: float) -> "FakeClock":
        self.now = float(seconds)
        return self


class Sink:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def __call__(self, kind: str, **payload) -> None:
        self.records.append({"type": kind, **payload})

    def reasons(self) -> list[str]:
        return [record["reason"] for record in self.records]


def make(**kwargs) -> tuple[VisualState, Sink, FakeClock]:
    sink = Sink()
    clock = FakeClock()
    kwargs.setdefault("wall", lambda: WALL)
    kwargs.setdefault("emit", sink)
    kwargs.setdefault("clock", clock)
    return VisualState(**kwargs), sink, clock


class SourceTransitionTests(unittest.TestCase):
    def test_a_session_begins_with_nothing_shared_and_does_not_say_so_again(self):
        # Nothing has happened yet; publishing "idle" on every call would be the
        # noise `note_source` exists to avoid.
        visual, sink, _ = make()
        self.assertFalse(visual.note_source(None))
        self.assertEqual(sink.records, [])
        self.assertFalse(visual.sharing)
        self.assertIsNone(visual.source)

    def test_the_first_track_reports_a_start(self):
        visual, sink, _ = make()
        self.assertTrue(visual.note_source("camera"))

        (record,) = sink.records
        self.assertEqual(record["type"], "visual")
        self.assertEqual(record["reason"], "started")
        self.assertTrue(record["sharing"])
        self.assertEqual(record["source"], "camera")

    def test_the_same_source_repeatedly_is_not_a_transition(self):
        visual, sink, _ = make()
        visual.note_source("camera")
        self.assertFalse(visual.note_source("camera"))
        self.assertFalse(visual.note_source("camera"))
        self.assertEqual(len(sink.records), 1, "the UI was told 'camera' three times")

    def test_moving_from_camera_to_screen_reports_the_new_source(self):
        visual, sink, _ = make()
        visual.note_source("camera")
        self.assertTrue(visual.note_source("screen"))

        self.assertEqual(sink.reasons()[-1], "source")
        self.assertEqual(visual.source, "screen")
        self.assertTrue(visual.sharing)

    def test_a_share_that_ends_says_stopped_and_keeps_aging_the_last_frame(self):
        # The frames are still in the model's context, so the record of them is
        # kept -- but nobody may read a finished share as live, which is what
        # `sharing` and the reason are for. The age of that last frame must keep
        # growing: freezing it would report a thirty-second-old picture an hour
        # after the share ended, understating exactly what it claims to measure.
        visual, sink, clock = make()
        visual.note_source("screen")
        visual.note_frame(seq=7, width=1920, height=1080)
        clock.at(START + 30.0)
        age_when_it_stopped = visual.age_ms()

        self.assertTrue(visual.note_source(None))
        self.assertEqual(sink.reasons()[-1], "stopped")
        self.assertFalse(visual.sharing)
        self.assertIsNone(visual.source)
        self.assertEqual(age_when_it_stopped, 30000.0)

        clock.at(START + 90.0)
        self.assertEqual(visual.age_ms(), 90000.0, "the last frame is still getting older")
        self.assertEqual(visual.seq, 7, "no frame was recorded after the share ended")

    def test_an_unrecognised_source_is_read_as_no_source(self):
        # The room reports whatever the track claims. Anything that is not one
        # of the two named sources is not allowed to become a third answer -- it
        # reads as "nothing shared", which for a session that shared nothing is
        # not a transition and so is not reported as one.
        visual, sink, _ = make()
        self.assertFalse(visual.note_source("something_else"))
        self.assertIsNone(visual.source)
        self.assertFalse(visual.sharing)
        self.assertEqual(sink.records, [])

    def test_the_baseline_idle_record_comes_from_the_session_not_from_a_transition(self):
        # `agent.py` publishes one `reason="idle"` record at session start -- "so
        # the UI never has to infer 'no video' from silence" -- and `note_source`
        # reports transitions after that. The reason stays in the vocabulary
        # because the *topic* uses it, even though this class never produces it.
        self.assertIn("idle", REASONS)

        visual, sink, _ = make()
        self.assertFalse(visual.note_source(None))
        self.assertFalse(visual.note_source(None))
        self.assertEqual(sink.records, [], "the baseline was re-published on every call")


class FrameTests(unittest.TestCase):
    def test_a_frame_records_its_sequence_size_and_both_clocks(self):
        visual, _, _ = make()
        visual.note_frame(seq=12, width=1920, height=1080)

        self.assertEqual(visual.seq, 12)
        self.assertEqual(visual.width, 1920)
        self.assertEqual(visual.height, 1080)
        self.assertEqual(visual.last_frame_at, START)
        self.assertEqual(visual.last_frame_wall, WALL)

    def test_frame_age_counts_from_the_frame_and_content_age_from_the_change(self):
        # The distinction the whole feature rests on: an unchanging screen makes
        # the frame newer and the content no newer at all.
        visual, _, clock = make()
        visual.note_frame(seq=1, width=640, height=480)  # a picture appears

        clock.at(START + 4.0)
        self.assertEqual(visual.age_ms(), 4000.0)
        self.assertEqual(visual.stale_ms(), 4000.0, "both clocks start together")

        visual.note_frame(seq=2, changed=False)  # same picture, delivered again
        self.assertEqual(visual.age_ms(), 0.0, "this frame is brand new")
        self.assertEqual(visual.stale_ms(), 4000.0, "the picture did not move")

        clock.at(START + 10.0)
        self.assertEqual(visual.age_ms(), 6000.0)
        self.assertEqual(visual.stale_ms(), 10000.0)

    def test_both_ages_are_unknown_before_any_frame_has_arrived(self):
        visual, _, _ = make()
        self.assertIsNone(visual.age_ms())
        self.assertIsNone(visual.stale_ms())
        snapshot = visual.snapshot()
        self.assertIsNone(snapshot["frame_age_ms"])
        self.assertIsNone(snapshot["content_age_ms"])

    def test_heartbeat_records_are_throttled_but_forced_ones_are_not(self):
        # A frame record is a statement about freshness: worth saying regularly
        # while sharing, not worth flooding at the capture rate.
        visual, sink, clock = make()
        visual.note_source("camera")  # forced: a transition
        visual.note_frame(seq=1)  # inside the interval after it
        self.assertEqual(len(sink.records), 1, "a throttled heartbeat was published")

        clock.at(START + DEFAULT_EMIT_INTERVAL)
        self.assertTrue(visual.note_frame(seq=2))
        self.assertEqual(len(sink.records), 2)

        clock.at(START + DEFAULT_EMIT_INTERVAL + 0.1)
        self.assertFalse(visual.note_frame(seq=3))
        self.assertEqual(len(sink.records), 2)

    def test_a_forced_record_publishes_inside_the_throttle_window(self):
        visual, sink, clock = make()
        visual.note_source("camera")
        clock.at(START + 0.01)
        self.assertTrue(visual.note_source("screen"))
        self.assertEqual(len(sink.records), 2, "a transition was suppressed by the heartbeat throttle")

    def test_the_reported_age_can_be_the_ones_the_sampler_measured(self):
        # The sampler knows how old the frame it *refused* was; when it says so,
        # that number is the honest one and the locally computed age is not.
        visual, sink, _ = make()
        visual.note_frame(seq=1, age_ms=4200.0)

        self.assertEqual(visual.snapshot()["frame_age_ms"], 0.0, "the state kept the wrong age")
        record = [r for r in sink.records if r.get("reason") == "frame"][-1]
        self.assertEqual(record["frame_age_ms"], 4200.0)

    def test_a_refused_frame_is_counted_by_what_it_was(self):
        visual, _, _ = make()
        visual.note_drop("stale")
        visual.note_drop("stale")
        visual.note_drop("unchanged")
        visual.note_drop("something-else")  # ignored, not silently miscounted

        self.assertEqual(visual.dropped_stale, 2)
        self.assertEqual(visual.dropped_unchanged, 1)

    def test_the_first_frame_heartbeat_is_allowed(self):
        # `_last_emit_at` is seeded at 0.0 meaning "never", which holds because a
        # monotonic clock does not start at zero. A fake one that did would see
        # the first heartbeat throttled -- so start where a real clock would be.
        visual, sink, _ = make()
        visual.note_source("camera")
        visual._last_emit_at = 0.0
        self.assertTrue(visual.note_frame(seq=1), "the first heartbeat was suppressed")


class PublishTests(unittest.TestCase):
    def test_a_record_carries_the_state_and_a_reason_and_nothing_about_pixels(self):
        visual, sink, _ = make()
        visual.note_source("screen")
        visual.note_frame(seq=3, width=800, height=600)
        visual.note_drop("stale")

        record = sink.records[-1]
        self.assertEqual(
            set(record),
            {
                "type",
                "sharing",
                "source",
                "frame_seq",
                "frame_age_ms",
                "content_age_ms",
                "width",
                "height",
                "dropped_stale",
                "dropped_unchanged",
                "reason",
            },
        )
        for key, value in record.items():
            with self.subTest(field=key):
                self.assertIsInstance(value, (type(None), bool, int, float, str))

    def test_the_reason_is_for_the_record_not_the_snapshot(self):
        # The snapshot is read by code that wants state; `reason` is the message
        # type. Leaving it out of one and in the other is deliberate.
        visual, sink, _ = make()
        visual.note_source("camera")
        self.assertIn("reason", sink.records[-1])
        self.assertNotIn("reason", visual.snapshot())

    def test_every_reason_published_is_a_declared_one(self):
        visual, sink, _ = make()
        visual.note_source("camera")
        visual.note_frame(seq=1)
        visual.note_drop("stale")
        visual.note_source(None)

        self.assertEqual(set(sink.reasons()) - set(REASONS), set())

    def test_a_record_with_no_emitter_is_a_no_op_everywhere(self):
        # Nothing listening must not mean nothing happening: the transition and
        # the frame are facts about the room, and only their publication is
        # conditional on somebody being there to receive it.
        visual, _, _ = make(emit=None)
        self.assertTrue(visual.note_source("camera"))
        self.assertFalse(visual.note_frame(seq=1))
        visual.note_drop("stale")
        self.assertFalse(visual._publish("frame", force=True), "publishing with no emitter is not an error")
        self.assertEqual(visual.seq, 1, "state was lost because nobody was listening")
        self.assertEqual(visual.dropped_stale, 1)

    def test_an_emitter_that_raises_does_not_corrupt_the_state(self):
        # The desktop app pipes the worker's stdout; a dead pipe raises. That
        # killed sessions once already (see `broken_stdout_check.py`), and the
        # rule here is the same one: telemetry may fail, the answer may not.
        def boom(kind: str, **payload) -> None:
            raise OSError("the pipe is gone")

        visual = VisualState(emit=boom, clock=FakeClock(), wall=lambda: WALL)

        self.assertTrue(visual.note_source("camera"), "the share started; only its publication failed")
        self.assertFalse(visual.note_frame(seq=9, width=640, height=480), "nothing could receive it")
        visual.note_drop("stale")

        self.assertEqual(visual.seq, 9)
        self.assertTrue(visual.sharing)
        self.assertEqual(visual.dropped_stale, 1)
        self.assertEqual(visual.snapshot()["frame_seq"], 9)

    def test_the_snapshot_is_the_whole_contract_and_carries_no_content(self):
        visual, _, _ = make()
        visual.note_source("screen")
        visual.note_frame(seq=4, width=1024, height=768)

        snapshot = visual.snapshot()
        self.assertEqual(
            set(snapshot),
            {
                "sharing",
                "source",
                "frame_seq",
                "frame_age_ms",
                "content_age_ms",
                "width",
                "height",
                "dropped_stale",
                "dropped_unchanged",
            },
        )
        self.assertNotIn("reason", snapshot, "the reason belongs to the record, not the state")


if __name__ == "__main__":
    unittest.main()
