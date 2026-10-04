"""Does this frame describe the screen *now*? The gate that answers it.

The stock sampler asks one question -- "has enough time passed?" -- and answers
it at the moment the frame is *consumed*. On an unbounded queue that can be
seconds after capture, so the question is answered about the wrong instant.

Everything here runs against a clock the test drives, because the whole point
is the relationship between *when a frame was delivered* and *when it was asked
for*. Three properties are worth naming before the tests do:

* **A backlog drains faster than it was captured.** Frames arriving closer
  together than any real capture interval were queued, so they are refused.
* **A stall is long relative to this source, not in absolute terms.** A fixed
  250ms floor is right for a 15fps camera and starves a 3fps one, which is why
  the threshold is also several observed intervals. The regression test for that
  is :meth:`FreshnessGateTests.test_a_source_slower_than_the_floor_is_not_starved`.
* **Observation never decides.** A visual state that raises must not stop a
  frame being sampled: the freshest thing we can do is the thing that must not
  be broken by our own bookkeeping.

Schedules are written as "frames at a steady rate" rather than as one frame per
assertion, and the counts that are asserted are derived from the schedule where
a floating-point boundary could move them. A test that only passes when
``20 * 0.05`` rounds one particular way is testing IEEE 754, not freshness.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from agent.runtime.frame_sampler import (
    DEFAULT_CADENCE,
    LEARNABLE_GAP,
    STALE_SUSPECT_GAP,
    STALL_INTERVALS,
    FreshnessVideoSampler,
    content_key,
)


class Frame:
    """Only the three attributes ``rtc.VideoFrame`` actually exposes."""

    def __init__(self, data: bytes | list | None = b"pixels", width: int = 640, height: int = 480) -> None:
        self.width = width
        self.height = height
        self.data = data


def frame(number: int) -> Frame:
    """A frame whose content differs from every other one."""
    return Frame(data=f"pixels-{number}".encode())


#: The session states the budget reads. `speaking` uses `speaking_fps`.
SPEAKING = SimpleNamespace(user_state="speaking")
SILENT = SimpleNamespace(user_state="silent")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def at(self, seconds: float) -> "FakeClock":
        self.now = float(seconds)
        return self


class RecordingVisual:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.drops: list[str] = []

    def note_frame(self, **payload) -> None:
        self.frames.append(payload)

    def note_drop(self, reason: str) -> None:
        self.drops.append(reason)


class ExplodingVisual:
    def note_frame(self, **payload) -> None:
        raise RuntimeError("bookkeeping failed")

    def note_drop(self, reason: str) -> None:
        raise RuntimeError("bookkeeping failed")


def sampler(**kwargs) -> tuple[FreshnessVideoSampler, FakeClock]:
    clock = FakeClock()
    kwargs.setdefault("clock", clock)
    return FreshnessVideoSampler(**kwargs), clock


def stream(
    s: FreshnessVideoSampler,
    clock: FakeClock,
    *,
    start: float,
    count: int,
    step: float,
    session,
    content: int = 0,
    still: bool = False,
) -> list[bool]:
    """Deliver `count` frames `step` seconds apart, starting at `start`.

    Times are computed as ``start + index * step`` rather than accumulated, so
    the schedule does not drift, and the caller can reason about which frames
    land on a budget boundary.

    By default every frame is a *different* picture, which is what a live screen
    looks like. `still=True` holds one picture for the whole schedule -- what a
    screen nobody is touching looks like -- and `content` chooses which.
    """
    results = []
    for index in range(count):
        clock.at(start + index * step)
        results.append(s(frame(content if still else content * 10_000 + index), session))
    return results


class FreshnessGateTests(unittest.TestCase):
    def test_the_first_frame_of_a_session_is_taken(self):
        # There is nothing to be behind on, and refusing it would open a
        # conversation that cannot describe the first thing it is shown.
        s, _ = sampler(speaking_fps=60)
        self.assertTrue(s(frame(1), SPEAKING))
        self.assertEqual(s.accepted, 1)

    def test_a_backlog_drain_is_refused_frame_by_frame(self):
        # Queued frames are delivered back-to-back. Each is behind the last, so
        # each is refused -- dropping only the first would leave the model
        # holding a picture from before the stall.
        s, clock = sampler(speaking_fps=60)
        self.assertTrue(s(frame(1), SPEAKING))
        clock.at(0.001)
        self.assertFalse(s(frame(2), SPEAKING))
        clock.at(0.002)
        self.assertFalse(s(frame(3), SPEAKING))

        self.assertEqual(s.accepted, 1)
        self.assertEqual(s.dropped_stale, 2)

    def test_a_silence_makes_the_next_frame_suspect_and_delivery_clears_it(self):
        # The first frame after a blockage may be the oldest one in the queue,
        # so it is refused; delivery is trusted again only once it demonstrably
        # waits for a frame instead of handing one over.
        s, clock = sampler(speaking_fps=60)
        self.assertTrue(s(frame(1), SPEAKING))

        clock.at(1.0)
        self.assertFalse(s(frame(2), SPEAKING), "the frame after a silence was trusted")

        clock.at(1.07)  # a 70ms interval: delivery is waiting again
        self.assertFalse(s(frame(3), SPEAKING), "the drain had not caught up yet")

        clock.at(1.4)  # a real interval, past the catch-up bar
        self.assertTrue(s(frame(4), SPEAKING))

        self.assertEqual(s.accepted, 2)
        self.assertEqual(s.dropped_stale, 2)
        self.assertFalse(s.stats()["suspect"], "the sampler never recovered from the stall")

    def test_a_drain_is_refused_even_while_suspect(self):
        s, clock = sampler(speaking_fps=60)
        self.assertTrue(s(frame(1), SPEAKING))
        clock.at(1.0)
        self.assertFalse(s(frame(2), SPEAKING))
        clock.at(1.005)
        self.assertFalse(s(frame(3), SPEAKING))
        clock.at(1.01)
        self.assertFalse(s(frame(4), SPEAKING))

        self.assertEqual(s.accepted, 1)
        self.assertEqual(s.dropped_stale, 3)

    def test_a_source_slower_than_the_floor_is_not_starved(self):
        # The reason the threshold is measured against the source. A 1fps stream
        # arrives every 1000ms; against a fixed 250ms floor every arrival reads
        # as a stall, and -- because the cadence was only ever updated by gaps
        # that had already passed that check -- nothing could correct it. The
        # sampler would then refuse every frame after the first, forever, with
        # counters saying "stale" and no error raised anywhere: a video input
        # that silently delivers nothing is worse than one that delivers
        # something four seconds late, and neither is what was asked for.
        s, clock = sampler(speaking_fps=60)
        results = [s(frame(second + 1), SPEAKING) for second, clock in _seconds(clock, 6)]

        self.assertEqual(
            results,
            [True, False, False, True, True, True],
            "a healthy slow source was refused every frame after the first",
        )
        self.assertEqual(s.accepted, 4)
        self.assertEqual(s.dropped_stale, 2, "the two frames it could not yet vouch for")
        self.assertGreater(s.stall_threshold(), STALE_SUSPECT_GAP, "the threshold never learned this rate")
        self.assertGreater(s.stall_threshold(), 1.0, "the threshold should end up several intervals wide")

    def test_the_threshold_starts_at_the_floor_for_a_stream_not_yet_seen(self):
        s, _ = sampler()
        self.assertEqual(s.stall_threshold(), STALE_SUSPECT_GAP)
        self.assertGreater(STALE_SUSPECT_GAP, DEFAULT_CADENCE * STALL_INTERVALS)

    def test_a_gap_beyond_the_learnable_ceiling_is_never_taught(self):
        # A ten-second blockage must not make the sampler believe the stream
        # arrives every ten seconds: that would call every *real* gap normal
        # while the queue behind it was still draining.
        s, clock = sampler(speaking_fps=60)
        self.assertTrue(s(frame(1), SPEAKING))
        clock.at(10.0)
        self.assertFalse(s(frame(2), SPEAKING))

        self.assertLessEqual(s.stats()["cadence_ms"], STALE_SUSPECT_GAP * 1000, "the blockage was learned")

    def test_the_learnable_ceiling_is_above_the_stall_floor(self):
        # The two constants have to agree about what "slow" and "blocked" mean;
        # a ceiling below the floor would teach only gaps that could never be
        # stalls, which is no teaching at all.
        self.assertGreater(LEARNABLE_GAP, STALE_SUSPECT_GAP)


class BudgetTests(unittest.TestCase):
    def test_the_budget_holds_for_a_stream_that_keeps_arriving(self):
        # speaking_fps=1 is a 1000ms budget against frames arriving every 50ms:
        # exactly the real shape, where capture is far faster than what is worth
        # sending to a tokenizer.
        s, clock = sampler(speaking_fps=1)
        count = 22  # t = 0.00 .. 1.05, so two frames clear the interval
        stream(s, clock, start=0.0, count=count, step=0.05, session=SPEAKING)

        self.assertEqual(s.accepted, 2, "the first frame, then the one that paid the interval")
        self.assertEqual(s.dropped_stale, 0, "a steady stream is not a stalled one")
        self.assertEqual(s.dropped_budget, count - s.accepted)

    def test_a_fresh_request_bypasses_the_budget(self):
        # A turn starts while a silent interval is still running. Without this
        # the model answers a question about the screen using whatever it last
        # saw -- which is the failure the feature exists to prevent.
        s, clock = sampler(speaking_fps=1)
        stream(s, clock, start=0.0, count=4, step=0.05, session=SPEAKING)
        self.assertEqual(s.accepted, 1)

        s.request_fresh()
        clock.at(0.20)
        self.assertTrue(s(frame(99), SPEAKING), "the fresh request was ignored")
        self.assertEqual(s.accepted, 2)

    def test_a_fresh_request_is_not_spent_on_a_frame_that_was_refused(self):
        # Asking for a frame is not permission to present a queued one. The
        # request has to survive the refusal, or the turn that asked for a look
        # is the one that goes unanswered.
        s, clock = sampler(speaking_fps=1)
        stream(s, clock, start=0.0, count=4, step=0.05, session=SPEAKING)

        s.request_fresh()
        clock.at(0.155)  # a 5ms gap: queued, refused
        self.assertFalse(s(frame(50), SPEAKING))
        self.assertEqual(s.dropped_stale, 1)

        clock.at(0.20)  # 45ms: a real arrival, so the request may be spent
        self.assertTrue(s(frame(51), SPEAKING), "the request died with the frame it was attached to")
        self.assertEqual(s.accepted, 2)

    def test_a_switched_off_budget_sends_nothing_and_says_why(self):
        # silent_fps=0 is a profile with video present but transmission off. It
        # must be visible as a *budget* decision, not look like a stream that
        # simply never produced anything.
        s, clock = sampler(silent_fps=0.0)
        results = stream(s, clock, start=0.0, count=3, step=0.05, session=SILENT)

        self.assertEqual(results, [False, False, False])
        self.assertEqual(s.accepted, 0)
        self.assertEqual(s.dropped_budget, 3)

    def test_a_switched_off_budget_does_not_spend_a_pending_request(self):
        # The request is a wish, not an override: a profile that sends nothing
        # stays silent, and the wish is still there for when it comes back.
        s, clock = sampler(silent_fps=1.0)
        self.assertTrue(s(frame(1), SILENT))

        s.silent_fps = 0.0
        clock.at(0.10)
        self.assertFalse(s(frame(2), SILENT))

        s.request_fresh()
        clock.at(0.15)
        self.assertFalse(s(frame(3), SILENT), "a fresh request overrode a disabled budget")

        s.silent_fps = 1.0
        clock.at(0.20)
        self.assertTrue(s(frame(4), SILENT), "the pending request was spent on a refused frame")

        clock.at(0.25)
        self.assertFalse(s(frame(5), SILENT), "the budget is not back in force")
        self.assertEqual(s.accepted, 2)
        self.assertEqual(s.dropped_budget, 3)


class ContentTests(unittest.TestCase):
    def test_an_unchanged_screen_is_not_sent_twice(self):
        # Sending identical pixels again spends tokens to say nothing, and the
        # model already holds that exact picture. The refusal must also be
        # counted *separately* from staleness: "nothing moved" is an accurate
        # observation, and reporting it as age would make a still screen look
        # like a stale one.
        s, clock = sampler(speaking_fps=1)
        unchanged = 61  # t = 0.00 .. 3.00 of one still picture
        stream(s, clock, start=0.0, count=unchanged, step=0.05, session=SPEAKING, content=1, still=True)
        changed = 61  # t = 3.05 .. 6.05 after the picture changes
        stream(s, clock, start=3.05, count=changed, step=0.05, session=SPEAKING, content=2, still=True)

        self.assertEqual(s.dropped_stale, 0, "a static screen is not a stale one")
        # Two pictures, two sends. The count of *refusals* is no longer pinned to a
        # number: the sampler now stretches its interval while a screen is still,
        # so it looks less often and refuses less often. What has to hold is that
        # each picture went out once, that "nothing moved" is reported as its own
        # thing rather than as age, and that every frame is accounted for.
        self.assertGreaterEqual(s.dropped_unchanged, 1, "the dedup never fired")
        self.assertEqual(s.accepted, 2, "the first frame, then the changed one")
        self.assertEqual(
            s.accepted + s.dropped_unchanged + s.dropped_budget + s.dropped_stale,
            unchanged + changed,
            "a frame was neither sent, nor refused, nor counted",
        )

    def test_content_key_is_exact_about_one_pixel(self):
        # A sampled hash can miss a one-pixel change, and a missed change means
        # a screen that moved while Lumine insists nothing happened.
        base = Frame(data=b"\x00" * 64)
        changed = Frame(data=b"\x00" * 63 + b"\x01")
        self.assertNotEqual(content_key(base), content_key(changed))

    def test_content_key_separates_two_sizes_of_the_same_bytes(self):
        wide = Frame(data=b"same", width=1920, height=1080)
        tall = Frame(data=b"same", width=1080, height=1920)
        self.assertNotEqual(content_key(wide), content_key(tall))

    def test_content_key_of_a_buffer_that_cannot_be_hashed_still_answers(self):
        exotic = Frame(data=[1, 2, 3])
        ordinary = content_key(Frame(data=bytes([1, 2, 3])))
        self.assertEqual(content_key(exotic), (640, 480, ordinary[2]))

    def test_content_key_of_a_frame_with_no_data_is_only_its_shape(self):
        self.assertEqual(content_key(Frame(data=None)), (640, 480, 0))


class ObservationTests(unittest.TestCase):
    def test_an_accepted_frame_is_reported_with_its_sequence(self):
        visual = RecordingVisual()
        s, _ = sampler(speaking_fps=60, visual=visual)
        self.assertTrue(s(frame(1), SPEAKING))

        (record,) = visual.frames
        self.assertEqual(record["seq"], 1)
        self.assertEqual(record["width"], 640)
        self.assertEqual(record["height"], 480)
        self.assertTrue(record["changed"])
        self.assertEqual(visual.drops, [])

    def test_a_refused_frame_is_reported_with_its_reason(self):
        visual = RecordingVisual()
        s, clock = sampler(speaking_fps=60, visual=visual)
        self.assertTrue(s(frame(1), SPEAKING))
        recorded = len(visual.frames)  # the accepted one, correctly

        clock.at(0.001)
        self.assertFalse(s(frame(2), SPEAKING))

        self.assertEqual(visual.drops, ["stale"])
        self.assertEqual(
            len(visual.frames),
            recorded,
            "a refused frame was reported as one that arrived",
        )

    def test_bookkeeping_that_raises_cannot_stop_sampling(self):
        # The visual state is observation, not the decision. A bug in it must
        # cost us a telemetry row, never the frame.
        s, clock = sampler(speaking_fps=60, visual=ExplodingVisual())
        self.assertTrue(s(frame(1), SPEAKING))
        clock.at(1.0)
        self.assertFalse(s(frame(2), SPEAKING))

    def test_attaching_later_wires_the_rest_of_the_stream(self):
        s, _ = sampler(speaking_fps=60)
        visual = RecordingVisual()
        s.attach(visual)
        self.assertTrue(s(frame(1), SPEAKING))
        self.assertEqual(len(visual.frames), 1)

    def test_stats_reports_the_counters_the_report_reads(self):
        s, clock = sampler(speaking_fps=1)
        s(frame(1), SPEAKING)
        clock.at(0.05)
        s(frame(2), SPEAKING)

        stats = s.stats()
        self.assertEqual(
            set(stats),
            {
                "seq", "accepted", "dropped_stale", "dropped_unchanged",
                "dropped_budget", "suspect", "cadence_ms",
                # How long the current quiet run is, and how far the sampling
                # interval has stretched because of it. Together they answer "why
                # is she still paying for a screen nobody is touching", which is
                # the question a person actually has when a share is running.
                "quiet_run", "interval_ms",
            },
        )
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["dropped_budget"], 1)
        self.assertEqual(stats["quiet_run"], 0)

    def test_it_is_the_call_shape_the_session_demands(self):
        # `AgentSession` calls it as `(frame, session) -> bool` and the pipeline
        # declares the two rates by attribute; both are read by other code.
        s = FreshnessVideoSampler()
        self.assertEqual((s.speaking_fps, s.silent_fps), (1.0, 0.3))
        self.assertIsInstance(s(frame(1), SPEAKING), bool)
        self.assertIsInstance(s(frame(2), SILENT), bool)


def _seconds(clock: FakeClock, count: int):
    """One frame per whole second -- a source far slower than the stall floor."""
    for second in range(count):
        clock.at(float(second))
        yield second, clock


if __name__ == "__main__":
    unittest.main()
