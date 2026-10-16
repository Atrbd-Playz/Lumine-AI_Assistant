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

from agent.runtime.visual_state import (
    DEFAULT_EMIT_INTERVAL,
    PERMISSIONS,
    PHASES,
    READY_INTERVAL,
    REASONS,
    VISION_NOTE,
    VisualState,
)

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
        # reports transitions after that. It goes through `mark_published`, which
        # is why `idle` is in the vocabulary this class now produces: a record
        # emitted beside the state rather than by it cannot be counted as
        # "already published", and the tick below would announce the same phase
        # a second time.
        self.assertIn("idle", REASONS)

        visual, sink, _ = make()
        self.assertFalse(visual.note_source(None))
        self.assertFalse(visual.note_source(None))
        self.assertEqual(sink.records, [], "the baseline was re-published on every call")

        self.assertTrue(visual.mark_published("idle"))
        self.assertEqual(sink.reasons(), ["idle"])
        self.assertFalse(
            visual.pending_phase_change(),
            "the baseline counts as the published phase, or the first tick repeats it",
        )


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
                "phase",
                "vision_ready",
                "frame_available",
                "can_see",
                "permission",
                "error",
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
                "phase",
                "vision_ready",
                "frame_available",
                "can_see",
                "permission",
                "error",
            },
        )
        self.assertNotIn("reason", snapshot, "the reason belongs to the record, not the state")
        # Every added field is a value a reader can act on without recomputing
        # the ladder -- which is the point of adding them: a UI that combines
        # `sharing` and `frame_seq` itself is a second implementation of
        # `phase()`, and the two would eventually disagree.
        for key in ("phase", "vision_ready", "frame_available", "permission"):
            self.assertIsNotNone(snapshot[key])


class PhaseTests(unittest.TestCase):
    """The ladder, and the four facts it reads that nothing else carries.

    `phase()` is what both readers consume: the app draws a status line from it
    and the model is pushed it in her instructions. So it is tested as a
    *function of the facts* rather than as a sequence of calls, because the
    failure this exists to prevent is a phase that disagrees with the state it
    was computed from.
    """

    def test_nothing_shared_yet_is_not_requested(self):
        visual, _, _ = make()
        self.assertEqual(visual.phase(), "not_requested")

    def test_a_grant_with_nothing_shared_is_enabled(self):
        visual, _, _ = make()
        visual.note_permission("enabled")
        self.assertEqual(visual.phase(), "enabled")

    def test_a_refusal_wins_over_a_share_that_otherwise_looks_fine(self):
        # The order matters and is the whole point: an operating system that
        # said no leaves `sharing` untouched (nothing was ever published), so a
        # ladder that read the frame facts first would report `not_requested`
        # for a capture the user watched fail.
        visual, _, _ = make()
        visual.note_capability(True)
        visual.note_permission("denied")
        visual.note_source("screen")
        visual.note_frame(seq=1)
        self.assertEqual(visual.phase(), "denied")

    def test_a_model_that_declares_no_image_input_is_unavailable(self):
        # LiveKit drops frames a model cannot read *without raising*, so this is
        # the only place the difference between "no video" and "nowhere for video
        # to go" is stated anywhere in the system.
        visual, _, _ = make()
        visual.note_capability(False)
        visual.note_permission("enabled")
        self.assertEqual(visual.phase(), "unavailable")
        self.assertFalse(visual.vision_ready())

    def test_a_capture_that_failed_is_an_error_before_it_is_anything_else(self):
        visual, _, _ = make()
        visual.note_capability(True)
        visual.note_error("Another application is using the camera.")
        self.assertEqual(visual.phase(), "error")

    def test_a_share_with_no_frame_through_it_yet_is_capturing(self):
        # Not `vision_degraded`: nothing has gone wrong, the first frame simply
        # has not arrived. Reporting it as degraded would teach the reader that
        # the label means "not ready yet", which is exactly the drift that makes
        # a real degradation unreadable.
        visual, _, _ = make()
        visual.note_capability(True)
        visual.note_permission("enabled")
        visual.note_source("screen")
        self.assertEqual(visual.phase(), "capturing")
        self.assertFalse(visual.vision_ready())

    def test_a_share_that_restarts_does_not_inherit_the_last_shares_liveness(self):
        # `seq` survives a stop on purpose -- the frames really are still in the
        # model's context -- but the *delivery* clock belongs to the old track.
        # Without this the second share opens as `vision_degraded` because the
        # first one went quiet, and says so for as long as it takes a frame.
        visual, _, clock = make()
        visual.note_capability(True)
        visual.note_permission("enabled")
        visual.note_source("screen")
        visual.note_frame(seq=1)
        clock.at(START + 40.0)
        visual.note_source(None)
        visual.note_source("screen")
        self.assertEqual(visual.phase(), "frame_available")
        visual.note_frame(seq=2)
        self.assertEqual(visual.phase(), "vision_ready")

    def test_ready_holds_while_frames_arrive_and_fails_once_they_stop(self):
        visual, _, clock = make()
        visual.note_capability(True)
        visual.note_permission("enabled")
        visual.note_source("screen")
        visual.note_frame(seq=1)
        self.assertEqual(visual.phase(), "vision_ready")

        # Inside the window: still current, even though a second has passed.
        clock.at(START + READY_INTERVAL - 0.5)
        self.assertEqual(visual.phase(), "vision_ready")

        clock.at(START + READY_INTERVAL + 0.1)
        self.assertEqual(visual.phase(), "vision_degraded")
        self.assertFalse(visual.vision_ready())

        # One frame brings it back, which is what makes `vision_degraded`
        # a statement about the stream rather than a latch.
        visual.note_frame(seq=2)
        self.assertEqual(visual.phase(), "vision_ready")

    def test_an_unchanged_screen_stays_ready_because_frames_are_still_arriving(self):
        # Acceptance case 4. The sampler backs off to ten seconds on a quiet
        # screen and refuses what it samples as `unchanged` -- and a refusal is
        # still a frame arriving. Reading `last_frame_at` instead of the
        # delivery clock is what would report an idle, perfectly healthy share
        # as broken, which is the same as reporting it as stale.
        visual, _, clock = make()
        visual.note_capability(True)
        visual.note_permission("enabled")
        visual.note_source("screen")
        visual.note_frame(seq=1)
        visual.note_drop("unchanged")

        clock.at(START + READY_INTERVAL - 0.5)
        visual.note_drop("unchanged")
        self.assertEqual(visual.phase(), "vision_ready")
        self.assertIsNotNone(visual.age_ms(), "the picture is still ageing, as it should")

    def test_a_frame_that_arrived_too_late_is_not_liveness(self):
        # A `stale` drop is evidence the gaps had already exceeded the sampler's
        # own threshold. Counting it as delivery would report a chronically
        # lagging stream as ready on the strength of frames nobody could use.
        visual, _, clock = make()
        visual.note_capability(True)
        visual.note_permission("enabled")
        visual.note_source("screen")
        visual.note_frame(seq=1)
        clock.at(START + 60.0)
        visual.note_drop("stale")
        self.assertEqual(visual.phase(), "vision_degraded")

    def test_stopping_the_share_is_stopped_and_not_a_degraded_one(self):
        visual, _, clock = make()
        visual.note_capability(True)
        visual.note_permission("enabled")
        visual.note_source("screen")
        visual.note_frame(seq=3)
        clock.at(START + 120.0)
        visual.note_source(None)
        self.assertEqual(visual.phase(), "stopped")
        self.assertIsNone(visual.delivery_age_ms(), "there is no live stream to be aged")

    def test_every_phase_it_can_return_is_one_it_declares(self):
        # A phase nobody has a sentence for is a phase the interface renders as
        # blank. Derived by walking the states rather than by listing outputs,
        # so a new rung has to be declared or the test finds it.
        visual, _, clock = make()
        seen: set[str] = set()

        def walk() -> None:
            seen.add(visual.phase())

        walk()
        visual.note_capability(False)
        walk()
        visual.note_capability(True)
        visual.note_error("boom")
        walk()
        visual.note_error("")
        visual.note_permission("denied")
        walk()
        visual.note_permission("requesting_permission")
        walk()
        visual.note_permission("enabled")
        walk()
        visual.note_source("screen")
        walk()
        visual.note_frame(seq=1)
        walk()
        clock.at(START + READY_INTERVAL + 1.0)
        walk()
        visual.note_source(None)
        walk()

        self.assertLessEqual(seen, set(PHASES))
        # And the ones that matter are actually reachable, so the declared list
        # cannot quietly become a set of values nothing produces.
        for expected in (
            "unavailable",
            "error",
            "denied",
            "requesting_permission",
            "not_requested",
            "enabled",
            "capturing",
            "vision_ready",
            "vision_degraded",
            "stopped",
        ):
            self.assertIn(expected, seen)


class AppReportTests(unittest.TestCase):
    """The one path by which an operating system's answer reaches the worker."""

    def test_a_refusal_is_recorded_from_bytes(self):
        visual, sink, _ = make()
        visual.note_capability(True)

        changed = visual.note_report(b'{"state": "denied"}')

        self.assertTrue(changed)
        self.assertEqual(visual.permission, "denied")
        self.assertEqual(visual.phase(), "denied")
        self.assertIn("permission", sink.reasons())

    def test_a_grant_clears_the_failure_it_replaced(self):
        visual, _, _ = make()
        visual.note_error("The camera could not be started.")
        self.assertEqual(visual.phase(), "error")

        visual.note_report('{"state": "enabled"}')

        self.assertIsNone(visual.error_message)
        self.assertNotEqual(visual.phase(), "error")

    def test_a_failure_carries_the_apps_own_words(self):
        visual, _, _ = make()
        visual.note_report('{"state": "error", "message": "Screen capture was refused."}')

        self.assertEqual(visual.error_message, "Screen capture was refused.")
        self.assertEqual(visual.phase(), "error")

    def test_a_failure_can_be_withdrawn_so_one_bad_capture_ends(self):
        # `error` is the first rung of the ladder, so a failure with no way out
        # masks every later success for the rest of the session -- a camera
        # that failed once would report `error` while frames arrived.
        visual, _, _ = make()
        visual.note_capability(True)
        visual.note_error("The camera could not be started.")
        self.assertEqual(visual.phase(), "error")

        self.assertTrue(visual.note_error(""))
        self.assertIsNone(visual.error_message)

        visual.note_permission("enabled")
        visual.note_source("camera")
        visual.note_frame(seq=1)
        self.assertEqual(visual.phase(), "vision_ready")

        # And withdrawing what was never there is not a change.
        self.assertFalse(visual.note_error(""))

    def test_a_reported_failure_with_no_words_still_reports_one(self):
        # The empty message means *clear* on the direct path, so the fallback
        # lives in the report: the app said "error", it did not say "never mind".
        visual, _, _ = make()
        self.assertTrue(visual.note_report('{"state": "error"}'))
        self.assertEqual(visual.error_message, "the capture failed.")

    def test_a_payload_that_does_not_parse_changes_nothing(self):
        # This arrives on a data topic, which means anything in the room can
        # send it. The honest outcome for a payload that is not the documented
        # shape is "nothing happened" -- not a plausible-looking state.
        visual, _, _ = make()
        visual.note_capability(True)
        before = visual.snapshot()

        for payload in (b"\xff\xfe not utf8", "{not json", "null", "[1, 2]", 42, None):
            with self.subTest(payload=payload):
                self.assertFalse(visual.note_report(payload))
                self.assertEqual(visual.snapshot(), before)

    def test_an_unrecognised_state_word_is_ignored_rather_than_stored(self):
        visual, _, _ = make()
        before = visual.permission

        self.assertFalse(visual.note_report('{"state": "maybe"}'))

        self.assertEqual(visual.permission, before)
        for word in PERMISSIONS:
            self.assertIn(word, PHASES + ("not_requested", "requesting_permission", "denied", "enabled"))

    def test_a_message_is_bounded_because_a_record_is_republished(self):
        visual, _, _ = make()
        visual.note_error("x" * 5000)
        self.assertEqual(len(visual.error_message), 160)


class VisionBlockTests(unittest.TestCase):
    """The one line the model is pushed, and what it must never contain."""

    def test_it_carries_the_facts_a_model_cannot_observe_for_itself(self):
        visual, _, _ = make()
        visual.note_capability(True)
        visual.note_permission("enabled")
        visual.note_source("screen")
        visual.note_frame(seq=5)

        facts = visual.vision_facts()

        self.assertEqual(
            set(facts),
            {"can_see", "permission", "screen_share", "source", "frame_available", "vision_ready", "phase"},
        )
        self.assertEqual(facts["phase"], "vision_ready")
        self.assertTrue(facts["vision_ready"])

    def test_it_contains_no_age_no_counter_and_no_timestamp(self):
        # The load-bearing property. An age pushed into the instructions once is
        # stale for every later turn, and a stale age presented as current is
        # precisely the failure this whole feature exists to prevent -- so the
        # block is written so that it *cannot* say one.
        #
        # Part one: a state with nothing in flight has a block that does not
        # move when time does. If a timestamp lived anywhere in it, an hour
        # would change the bytes and the dedup key with them.
        quiet, _, clock = make()
        quiet.note_capability(True)
        quiet.note_permission("enabled")
        before = quiet.vision_block()

        clock.at(START + 3600.0)
        quiet.note_drop("stale")

        self.assertEqual(
            quiet.vision_block(),
            before,
            "an hour passed and the block is byte-identical: no timestamp, "
            "no age, no counter has aged inside it",
        )

        # Part two: a state that *is* looking at a frame still says nothing
        # about that frame except that it exists. Its dimensions, its sequence
        # number and its age are all absent -- `phase` may move (ready is a
        # claim about now), which is exactly why the tick re-reads the block
        # rather than pushing it once.
        visual, _, _ = make()
        visual.note_capability(True)
        visual.note_permission("enabled")
        visual.note_source("screen")
        visual.note_frame(seq=7, width=1920, height=1080)
        live = visual.vision_block()
        payload = live.split("[vision] ", 1)[-1]

        # The prose may talk *about* an age -- it has to, to say there isn't
        # one. The facts line is where a number could hide.
        self.assertNotIn("_ms", payload)
        self.assertNotIn("age", payload)
        self.assertNotIn("1920", payload, "the frame's dimensions are not a fact about sight")
        self.assertNotIn("1080", payload)
        self.assertNotIn("seq", payload, "a sequence number is not a fact about sight")

    def test_it_is_byte_identical_for_identical_state_so_a_push_can_be_deduped(self):
        # The whole cost model rests on this: `update_instructions` re-sends the
        # persona with every push, so "has this changed?" is a string compare
        # and the string has to be stable for a state that has not moved.
        first, _, clock = make()
        first.note_capability(True)
        first.note_permission("enabled")

        second, _, _ = make()
        second.note_capability(True)
        second.note_permission("enabled")

        self.assertEqual(first.vision_block(), second.vision_block())

        clock.at(START + 30.0)
        first.note_drop("unchanged")
        self.assertEqual(first.vision_block(), second.vision_block(), "an idle clock moved the block")

    def test_it_changes_on_the_transitions_that_earn_a_push(self):
        visual, _, _ = make()
        blocks = [visual.vision_block()]

        visual.note_capability(True)
        blocks.append(visual.vision_block())
        visual.note_permission("denied")
        blocks.append(visual.vision_block())
        visual.note_permission("enabled")
        blocks.append(visual.vision_block())
        visual.note_source("screen")
        blocks.append(visual.vision_block())
        visual.note_frame(seq=1)
        blocks.append(visual.vision_block())

        self.assertEqual(len(set(blocks)), len(blocks))
        # And nothing repeated: a block that came back for a non-transition
        # would turn a dedup key into a coin flip.
        visual.note_source("screen")
        visual.note_frame(seq=2)
        self.assertEqual(visual.vision_block(), blocks[-1])

    def test_it_tells_a_model_that_cannot_see_that_it_cannot_see(self):
        visual, _, _ = make()
        visual.note_capability(False)

        self.assertIn('"can_see": false', visual.vision_block())
        self.assertEqual(visual.vision_facts()["phase"], "unavailable")

    def test_the_prose_around_it_is_constant(self):
        # Because the block is compared whole: prose that varied would make two
        # identical states compare unequal and push anyway.
        self.assertIn(VISION_NOTE, make()[0].vision_block())
        self.assertNotIn("{", VISION_NOTE)


class PendingPhaseTests(unittest.TestCase):
    """The tick's only question: has *this* phase reached anyone yet?"""

    def test_a_phase_caused_by_an_event_is_not_announced_twice(self):
        visual, _, _ = make()
        visual.note_capability(True)
        visual.note_permission("enabled")
        visual.mark_published("idle")

        visual.note_source("screen")  # already published, by the transition

        self.assertFalse(visual.pending_phase_change())

    def test_a_phase_that_moves_with_no_event_is_announced(self):
        visual, _, clock = make()
        visual.note_capability(True)
        visual.note_permission("enabled")
        visual.note_source("screen")
        visual.note_frame(seq=1)
        visual.mark_published("phase")
        self.assertFalse(visual.pending_phase_change())

        # Nothing happens. That is the entire problem: a stream that stops
        # producing frames produces no record, and silence is the one signal an
        # event-driven state structurally cannot hear.
        clock.at(START + READY_INTERVAL + 1.0)

        self.assertTrue(visual.pending_phase_change())
        visual.mark_published("phase")
        self.assertFalse(visual.pending_phase_change())

    def test_a_throttled_record_does_not_count_as_published(self):
        visual, sink, _ = make()
        visual.note_source("screen")
        visual.note_frame(seq=1)  # inside the transition's throttle window
        self.assertEqual(sink.reasons(), ["started"])
        self.assertTrue(
            visual.pending_phase_change(),
            "the phase moved but nothing carried it, so the tick still has to",
        )


if __name__ == "__main__":
    unittest.main()
