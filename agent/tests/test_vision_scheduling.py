"""Does Lumine look at the screen she is describing? A through J, actually tested.

The availability layer answered "can she see, and may she?" and it answered it
correctly. It could not answer the question that matters: **did an image reach
the model, and was it the newest one?** `vision_ready` is settled before the
first frame exists, so a pipeline that dropped every frame would report exactly
the same badge as one that did not.

These are the ten acceptance scenarios, and each one is a test rather than a
paragraph:

===  ==========================================================================
A    The slot holds the newest accepted frame, never an older one.
B    One hundred frames arriving while the model is busy leave *one* image.
C    A picture older than the pending limit is never sent, and a queued one
     that ages out is blanked rather than delivered.
D    An unchanged screen produces one image, not twelve.
E    A screen change marks the held picture stale, and the next frame cures it.
F    Four window switches deliver only the last one.
G    A result built from generation 10 is refused when generation is 11.
H    An explicit visual question gets a frame *inside* the silent interval.
I    An act refuses a picture that is not current, and asks for one.
J    A minute of a still screen costs one image.
===  ==========================================================================

Everything runs against a fake clock and a fake realtime session, because the
two things under test are *relationships between times* and *what is in a
queue* -- and both are more legible without a real event loop.

The fake realtime session is deliberately shaped like the real one: a
``_send_client_event`` method, a ``_msg_ch`` with a ``_queue`` deque, and an
``_opts`` carrying the encode options. That is the seam the link wraps, so a
test which does not use that shape is not testing the seam.
"""

from __future__ import annotations

import ctypes
import importlib
import inspect
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from agent.runtime.frame_sampler import (
    ACTION_MAX_AGE,
    CHANGE_RATIO,
    LUMA_DELTA,
    MAX_PENDING_AGE,
    FreshnessVideoSampler,
    need_picture,
    register_sampler,
    sampler_for,
    unregister_sampler,
)
from agent.runtime.vision_link import PROFILES, VisionLink

AGENT_DIR = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def at(self, seconds: float) -> "FakeClock":
        self.now = float(seconds)
        return self


class PixelFrame:
    """A frame with a luma plane the change detector can actually read.

    The fixture used by ``test_frame_sampler`` carries a short byte string, which
    :func:`luma_signature` (rightly) reports as unreadable -- so every decision
    in that suite is taken on the CRC fallback. These tests are *about* the
    change detector, so the grid has to be readable and it has to be able to
    tell two screens apart.
    """

    def __init__(self, fill: int, width: int = 640, height: int = 480) -> None:
        self.width = width
        self.height = height
        self.fill = fill
        self.data = bytes([fill]) * (width * height)

    def get_plane(self, index: int) -> memoryview:
        if index != 0:
            raise ValueError("one plane only")
        # ctypes' uint8 array exports a memoryview whose *format* is the
        # byte-ordered ``"<B"``, and CPython refuses to index that form. That is
        # exactly the shape ``livekit-rtc`` hands over for a planar frame, and it
        # is what killed the video forward task on its first real frame. A
        # memoryview over ``bytes`` is format ``"B"`` and indexes fine, so a
        # double built the easy way cannot see the failure this shape exists to
        # prevent -- the suite was green while the pipeline was dead.
        return memoryview((ctypes.c_uint8 * len(self.data)).from_buffer_copy(self.data))


class FakeChan:
    """The plugin's send channel, at the three fields the link reads.

    ``_gets`` is present because the *reason* replacement is safe and removal is
    not lives there: a parked getter has been woken and not yet run, and emptying
    the queue under it raises ``ChanEmpty`` from the consumer's ``async for`` and
    kills the channel permanently. Nothing in the tests drains it -- a channel
    that never drains is exactly the "model is busy" condition under test.
    """

    def __init__(self) -> None:
        self._queue: deque = deque()
        self._gets: deque = deque()

    def send_nowait(self, message) -> None:
        self._queue.append(message)


class Session:
    """The session, at the two attributes everything here reads.

    A stand-in for ``AgentSession`` rather than a ``SimpleNamespace``, and the
    difference is load-bearing. ``SimpleNamespace`` defines ``__eq__`` and
    therefore has **no** ``__hash__`` -- it is unhashable -- so
    :func:`register_sampler` would hit the ``TypeError`` its guard swallows and
    silently register nothing, while ``sampler_for`` would answer ``None`` for a
    session the test believes it registered. Every ``need_picture`` assertion
    would then be asserting against "no share is running", which is the one
    report this feature must never fabricate.
    """

    def __init__(self, user_state: str = "speaking", rt: Any = None) -> None:
        self.user_state = user_state
        self._activity = SimpleNamespace(_rt_session=rt)


class FakeRt:
    """The realtime session, at the one seam ``VisionLink`` wraps.

    ``drain`` decides whether the consumer is keeping up. A healthy channel
    empties as fast as things arrive, which is the normal case and the one most
    of these tests want; ``drain=False`` is the reported failure -- the LLM is
    busy for seconds while frames keep coming -- and it is the only condition in
    which a queue can exist at all.
    """

    def __init__(self, *, drain: bool = True) -> None:
        self._msg_ch = FakeChan()
        self._opts = SimpleNamespace(image_encode_options=None)
        self.drain = drain

    def _send_client_event(self, event):
        self._msg_ch.send_nowait(event)
        if self.drain:
            self._msg_ch._queue.clear()
        return event


def video_event(payload: bytes):
    """What ``push_video`` hands ``_send_client_event`` after it encodes."""
    return SimpleNamespace(
        audio=None,
        text=None,
        activity_start=None,
        activity_end=None,
        video=SimpleNamespace(data=payload),
    )


#: Brightnesses far enough apart that every grid cell counts as moved. The
#: detector needs ``CHANGE_RATIO`` (2%) of the grid to differ by more than
#: ``LUMA_DELTA`` (12); two fills 60 apart clear both on every cell.
DISTINCT = (20, 80, 140, 200)


class VisionCase(unittest.TestCase):
    """A sampler, a link, a realtime session and an emit, wired as `agent.py` does it."""

    def make(
        self,
        *,
        user_state: str = "speaking",
        drain: bool = True,
        **fps,
    ) -> None:
        self.clock = FakeClock()
        self.sampler = FreshnessVideoSampler(clock=self.clock, **fps)
        self.rt = FakeRt(drain=drain)
        self.session = Session(user_state=user_state, rt=self.rt)
        self.records: list[tuple[str, dict]] = []
        self.link = VisionLink(
            provider="google",
            model="gemini-3.8-live",
            emit=lambda event_type, **payload: self.records.append((event_type, payload)),
            clock=self.clock,
        )
        self.link.bind(self.sampler)
        self.sampler.attach_link(self.link)
        register_sampler(self.session, self.sampler)
        self.addCleanup(unregister_sampler, self.session)
        # What a tool is handed. `_session_of` reads `.session` off it.
        self.context = SimpleNamespace(session=self.session)

    def deliver(self, fill: int, at: float) -> tuple[bool, bytes | None]:
        """One frame through the whole path: gate, encode, channel.

        Mirrors ``AgentSession._forward_video_task`` exactly -- the frame is only
        pushed when the sampler accepts it, which is what makes a refused frame a
        saved token rather than a saved *suggestion*.
        """
        self.clock.at(at)
        accepted = self.sampler(PixelFrame(fill), self.session)
        if not accepted:
            return False, None
        payload = b"jpg-%d-%d" % (self.sampler.seq, fill)
        self.rt._send_client_event(video_event(payload))
        return True, payload

    def queued(self) -> list:
        return [m for m in self.rt._msg_ch._queue if getattr(m, "video", None) is not None]

    def queued_payloads(self) -> list[bytes]:
        return [bytes(m.video.data) for m in self.queued()]

    def last_record(self) -> dict:
        self.assertTrue(self.records, "no telemetry was written at all")
        self.assertEqual(self.records[-1][0], "vision_request")
        return self.records[-1][1]


class LatestFrameSlotTests(VisionCase):
    """A. The slot holds the newest accepted frame, never an older one."""

    def test_a_the_slot_is_replaced_not_appended(self):
        self.make(speaking_fps=60)
        accepted: list[int] = []
        for index, fill in enumerate(DISTINCT):
            ok, _ = self.deliver(fill, at=index * 0.05)
            self.assertTrue(ok, f"frame {index} should have been accepted")
            accepted.append(self.sampler.seq)

        latest = self.sampler.latest()
        self.assertIsInstance(latest, type(self.sampler.latest()))
        self.assertEqual(latest.seq, self.sampler.seq)
        self.assertEqual(latest.seq, accepted[-1])
        # The slot is one object, replaced in place: nothing accumulates, and the
        # frame it names is the same one the counters say was last accepted.
        self.assertEqual(self.sampler.accepted, len(accepted))
        self.assertEqual(self.sampler.generation, self.sampler.picture_generation)

    def test_a_a_frame_refused_by_the_budget_never_enters_the_slot(self):
        # Ten frames arrive inside one 1s budget window. None of them may be
        # remembered as "the picture", because none of them was ever sent --
        # reporting the first as current would describe a screen that stopped
        # being the screen nine frames ago.
        self.make(speaking_fps=1)
        ok, _ = self.deliver(DISTINCT[0], at=0.0)
        self.assertTrue(ok)
        first_seq = self.sampler.seq

        # Frames keep arriving at the source's own rate for the whole second
        # the budget is refusing them -- a real capture does not stop while we
        # decide, and a test that stopped would also have stopped the cadence
        # learning, which would make the frame at 1.0s look like a stall.
        for index in range(1, 20):
            ok, _ = self.deliver(DISTINCT[index % 4], at=index * 0.05)
            self.assertFalse(ok, f"frame {index} fell inside the one-second budget")
            self.assertEqual(
                self.sampler.latest().seq, first_seq,
                "a refused frame must not become the picture she holds",
            )

        ok, _ = self.deliver(DISTINCT[2], at=1.0)
        self.assertTrue(ok, "one second of budget has elapsed; this frame is due")
        self.assertEqual(self.sampler.latest().seq, self.sampler.seq)
        self.assertGreater(self.sampler.latest().seq, first_seq)


class QueueBoundednessTests(VisionCase):
    """B and F. A busy model never accumulates more than one waiting image."""

    def test_b_one_hundred_frames_while_busy_leave_one_image(self):
        self.make(speaking_fps=60, drain=False)
        payloads: list[bytes] = []
        for index in range(100):
            at = index * 0.05
            ok, payload = self.deliver(DISTINCT[index % 4], at=at)
            self.assertTrue(ok, f"frame {index} should have been accepted")
            payloads.append(payload)

            # The model never drains the channel. This is the whole scenario:
            # LLM latency of seconds against a capture rate of frames a second.
            self.assertLessEqual(
                len(self.queued()), 1, f"the queue grew to {len(self.queued())} images"
            )

        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.queued_payloads(), [payloads[-1]])
        self.assertEqual(self.link.replaced, 99)
        self.assertEqual(self.link.attached, 100)
        # Not one frame was dropped for age: everything below happened inside a
        # single simulated second and a half, well under the pending limit.
        self.assertEqual(self.sampler.dropped_expired, 0)

    def test_f_four_window_switches_deliver_only_the_last(self):
        # Chrome -> Discord -> VS Code -> Spotify, asked "what am I looking at?"
        # in the middle of it. Four pictures exist; the model may see one.
        self.make(speaking_fps=60, drain=False)
        payloads = []
        for index, fill in enumerate(DISTINCT):
            ok, payload = self.deliver(fill, at=index * 0.05)
            self.assertTrue(ok)
            payloads.append(payload)

        self.assertEqual(self.queued_payloads(), [payloads[-1]])
        self.assertEqual(self.link.stats()["pending_video"], 1)
        self.assertEqual(self.link.replaced, 3)
        # Every intermediate semantic request was discarded, not deferred: the
        # three pictures that lost are counted, not queued.
        self.assertEqual(self.sampler.superseded, 3)

    def test_the_queue_is_only_touched_by_writing_over_a_slot(self):
        # The property that makes coalescing safe. `Chan.send_nowait` wakes a
        # parked getter by popping it off `_gets`; between that wake-up and the
        # getter's own `recv_nowait` the queue may be non-empty and `_gets`
        # empty, and clearing it then raises `ChanEmpty` inside the consumer's
        # `async for` -- which stops *audio* too, permanently. A replacement
        # changes no length and cannot wake, refuse, or raise.
        self.make(speaking_fps=60, drain=False)
        self.deliver(DISTINCT[0], at=0.0)
        parked = object()
        self.rt._msg_ch._gets.append(parked)  # a getter is mid-wake

        ok, payload = self.deliver(DISTINCT[1], at=0.05)
        self.assertTrue(ok)
        self.assertEqual(list(self.rt._msg_ch._gets), [parked])
        self.assertEqual(len(self.rt._msg_ch._queue), 1)
        self.assertEqual(self.queued_payloads(), [payload])


class StaleFrameTests(VisionCase):
    """C. An aged picture is not sent, and a queued one that ages is blanked."""

    def test_c_a_frame_older_than_the_pending_limit_is_refused_at_the_boundary(self):
        # The gate approved this frame when it arrived. Between then and now the
        # event loop stalled: the encode finished, the channel did not drain, and
        # three seconds have gone by. `allows_send` is the only check that can
        # see that, because the gate already ran.
        self.make(speaking_fps=60, drain=False)
        ok, _ = self.deliver(DISTINCT[0], at=0.0)
        self.assertTrue(ok)

        self.clock.at(MAX_PENDING_AGE + 1.0)
        before = len(self.queued())
        reached = self.rt._send_client_event(video_event(b"jpg-late"))

        self.assertIsNone(reached, "an expired image must not enter the channel")
        self.assertEqual(len(self.queued()), before)
        self.assertEqual(self.sampler.dropped_expired, 1)
        record = self.last_record()
        self.assertFalse(record["image_attached"])
        self.assertEqual(record["why"], "expired")
        self.assertEqual(record["image_bytes"], 0)
        self.assertGreater(record["frame_age_ms"], MAX_PENDING_AGE * 1000)

    def test_c_a_queued_image_that_ages_out_is_blank_rather_than_delivered(self):
        # The other half of the same failure: the image is *already* queued when
        # it goes stale, so the enqueue-time gate cannot help. The pump is the
        # only heartbeat left -- if frames had stopped arriving, nothing would be
        # waiting to replace it either.
        self.make(speaking_fps=60, drain=False)
        ok, _ = self.deliver(DISTINCT[0], at=0.0)
        self.assertTrue(ok)
        self.assertEqual(self.link.stats()["pending_video"], 1)

        self.clock.at(MAX_PENDING_AGE + 1.0)
        swept = self.link.sweep()

        self.assertEqual(swept, 1)
        self.assertEqual(self.link.swept, 1)
        # Blank, not gone: the deque keeps its length, and a blank message falls
        # through every branch of the plugin's send task without matching one.
        self.assertEqual(len(self.rt._msg_ch._queue), 1)
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.sampler.dropped_expired, 1)
        self.assertEqual(self.last_record()["why"], "expired_pending")

    def test_c_a_healthy_image_is_left_alone_by_the_sweep(self):
        self.make(speaking_fps=60, drain=False)
        self.deliver(DISTINCT[0], at=0.0)
        self.clock.at(MAX_PENDING_AGE - 0.01)

        self.assertEqual(self.link.sweep(), 0)
        self.assertEqual(self.link.swept, 0)
        self.assertEqual(self.link.stats()["pending_video"], 1)


class DuplicateFrameTests(VisionCase):
    """D and J. An unchanged screen produces one image, not many."""

    def test_d_twelve_identical_frames_produce_exactly_one_image(self):
        self.make(speaking_fps=60, drain=False)
        payloads: list[bytes] = []
        for index in range(12):
            ok, payload = self.deliver(DISTINCT[0], at=index * 0.05)
            if ok:
                payloads.append(payload)

        self.assertEqual(len(payloads), 1)
        self.assertEqual(self.sampler.accepted, 1)
        self.assertEqual(self.sampler.dropped_unchanged, 11)
        self.assertEqual(self.link.attached, 1)
        self.assertEqual(self.link.stats()["pending_video"], 1)
        self.assertEqual(len(self.records), 1)
        self.assertTrue(self.records[0][1]["image_attached"])

    def test_j_a_minute_of_a_still_screen_costs_one_image(self):
        # 600 frames over sixty simulated seconds. The fps budget lets roughly
        # every fifth one as far as the content check, and every one of those is
        # refused as unchanged -- so the model pays for the first picture and
        # then nothing at all until something actually moves.
        self.make(speaking_fps=5)
        for index in range(600):
            self.deliver(DISTINCT[0], at=index * 0.1)

        self.assertEqual(self.sampler.accepted, 1)
        self.assertEqual(self.link.attached, 1)
        self.assertEqual(self.link.emitted, 1)
        # The channel keeps up in this scenario -- there is no stall to hold an
        # image in, which is the whole difference between "a still screen is
        # cheap" and "a busy model queues". Nothing was ever waiting.
        self.assertEqual(self.link.stats()["pending_video"], 0)
        # Every arriving frame is one of three outcomes: refused by the budget,
        # refused as unchanged, or sent. Conservation is the honest form of
        # "near-zero" -- a threshold only says a number was not reached, while
        # this says no frame quietly took a fourth path through the gates.
        self.assertEqual(
            self.sampler.dropped_budget
            + self.sampler.dropped_unchanged
            + self.sampler.accepted,
            600,
        )
        self.assertGreater(self.sampler.dropped_unchanged, 200)
        self.assertEqual(self.sampler.dropped_stale, 0)
        self.assertEqual(self.sampler.dropped_expired, 0)


class ScreenChangeTests(VisionCase):
    """E. A move behind the model's back is noticed before the next sample."""

    def test_e_a_screen_change_makes_the_held_picture_stale_immediately(self):
        self.make(speaking_fps=1)  # a one-second budget, so nothing is sampled yet
        ok, _ = self.deliver(DISTINCT[0], at=0.0)
        self.assertTrue(ok)
        self.assertTrue(self.sampler.picture_is_current())
        before = self.sampler.generation

        # Arrives 50ms later and is refused by the budget -- but the change
        # detector runs *ahead* of the budget, which is the whole point: the
        # budget answers "may we spend a frame?", not "did the screen move?".
        ok, _ = self.deliver(DISTINCT[1], at=0.05)
        self.assertFalse(ok)

        self.assertEqual(self.sampler.generation, before + 1)
        self.assertFalse(self.sampler.picture_is_current())
        report = need_picture(self.context, reason="action")
        self.assertFalse(report["current"])
        self.assertTrue(report["requested"])
        self.assertEqual(report["reason"], "action")

        # The next frame the budget allows cures it. The source keeps delivering
        # the whole second, exactly as it would in production -- a capture that
        # stopped while we waited would also stop teaching the cadence rule.
        # What cures it is not the second ticking over: `need_picture` armed a
        # request above, and a request is honoured by the very next arrival. A
        # cure that took a full second would mean the pre-act check had asked
        # for a picture and then waited for the schedule to provide one.
        cured_at: float | None = None
        for index in range(2, 21):
            at = index * 0.05
            ok, _ = self.deliver(DISTINCT[1], at=at)
            if ok and cured_at is None:
                cured_at = at
                # Asked again the instant the fresh picture landed: current
                # *and* young, so nothing is requested -- the stable-screen
                # guarantee is restored the moment the change has been absorbed.
                self.assertTrue(self.sampler.picture_is_current())
                report = need_picture(self.context)
                self.assertTrue(report["current"])
                self.assertFalse(report["requested"])
        self.assertIsNotNone(cured_at, "the armed request was never fulfilled")
        self.assertLess(
            cured_at, 1.0,
            "a frame the tool asked for must not have waited out the whole budget",
        )
        self.assertTrue(self.sampler.picture_is_current())

    def test_e_a_change_seen_during_a_refusal_is_not_lost_twice(self):
        # The observed signature advances even when the frame is refused, so a
        # second identical frame after the refusal does not bump the generation
        # again -- generation counts *changes*, not arrivals.
        self.make(speaking_fps=1)
        self.deliver(DISTINCT[0], at=0.0)
        self.deliver(DISTINCT[1], at=0.05)
        first = self.sampler.generation

        self.deliver(DISTINCT[1], at=0.1)
        self.deliver(DISTINCT[1], at=0.15)
        self.assertEqual(self.sampler.generation, first)


class StaleResultTests(VisionCase):
    """G. A picture from generation 10 is refused at generation 11."""

    def test_g_a_result_from_an_older_generation_never_reaches_the_model(self):
        self.make(speaking_fps=60)
        ok, _ = self.deliver(DISTINCT[0], at=0.0)
        self.assertTrue(ok)
        held = self.sampler.latest()
        self.assertEqual(held.generation, self.sampler.generation)

        # Something moves between the gate saying yes and the image being
        # handed over. In the synchronous pass-through that window is normally
        # microseconds -- which is exactly why it needs a guard rather than an
        # assumption: a future await, a stalled loop, or a plugin change widens
        # it silently.
        moved = self.sampler.note_content_change()
        self.assertEqual(moved, held.generation + 1)

        reached = self.rt._send_client_event(video_event(b"jpg-stale"))

        self.assertIsNone(reached)
        self.assertEqual(self.sampler.dropped_superseded, 1)
        record = self.last_record()
        self.assertFalse(record["image_attached"])
        self.assertEqual(record["why"], "superseded")
        self.assertEqual(record["result_generation"], held.generation)
        self.assertEqual(record["current_generation"], moved)
        self.assertNotEqual(
            record["result_generation"], record["current_generation"],
            "a record that reports the two generations as equal is the bug this exists to catch",
        )


class VisualQuestionTests(VisionCase):
    """H. A question about the screen is answered inside the silent interval."""

    def test_h_a_requested_frame_bypasses_the_silent_budget(self):
        self.make(user_state="silent")  # silent_fps 0.3 -> a 3.3s budget
        ok, _ = self.deliver(DISTINCT[0], at=0.0)
        self.assertTrue(ok)

        # The source keeps delivering on its own cadence and the silent budget
        # refuses every one of them -- 3.3 seconds is a long time to go without
        # looking, and that is exactly the saving a still screen is supposed to
        # buy. It is also exactly what an explicit question has to cut through.
        for index in range(1, 5):
            ok, _ = self.deliver(DISTINCT[1], at=index * 0.05)
            self.assertFalse(ok, "the silent budget should still be refusing")

        self.sampler.request_fresh("question", "detail")
        self.assertEqual(self.sampler.request_pending()["reason"], "question")
        self.assertEqual(self.sampler.request_pending()["profile"], "detail")

        ok, _ = self.deliver(DISTINCT[1], at=0.25)
        self.assertTrue(ok, "a requested frame must not wait out a 3.3 second interval")
        self.assertIsNone(self.sampler.request_pending())

        record = self.last_record()
        self.assertTrue(record["image_attached"])
        self.assertEqual(record["request_reason"], "question")
        self.assertEqual(record["request_priority"], "high")
        self.assertEqual(record["profile"], "detail")
        # Measured, not implied: the picture arrived this millisecond, and the
        # screen last *moved* 200ms ago. Both are reported so "she was looking at
        # it when she said that" is a number rather than an assumption.
        self.assertEqual(record["frame_age_ms"], 0.0)
        self.assertEqual(record["content_age_ms"], 200.0)
        self.assertIsNotNone(record["request_duration_ms"])

    def test_h_the_encoder_follows_the_request_then_returns_to_the_default(self):
        # The profile has to be chosen *before* the encode: `push_video`
        # compresses the frame and only then calls `_send_client_event`, so a
        # decision taken at the hand-over would describe the picture already
        # made. The pump runs at the top of every arriving frame for this.
        self.make(user_state="silent")
        self.deliver(DISTINCT[0], at=0.0)
        self.sampler.request_fresh("question", "detail")

        ok, _ = self.deliver(DISTINCT[1], at=0.05)
        self.assertTrue(ok)
        self.assertEqual(self.link.profile, "detail")
        self.assertEqual(
            self.rt._opts.image_encode_options.resize_options.width,
            PROFILES["detail"].width,
        )

        # The next frame has no request waiting for it, so it goes back on the
        # catalogue default -- otherwise one question would leave every later
        # frame on the sharper, dearer setting forever. The frame itself is
        # refused by the budget; it is the *profile* that matters here, and the
        # profile is chosen before the gate is asked.
        self.deliver(DISTINCT[2], at=0.1)
        self.assertEqual(self.link.profile, "general")
        self.assertEqual(
            self.rt._opts.image_encode_options.resize_options.width,
            PROFILES["general"].width,
        )


class ConsequentialActTests(VisionCase):
    """I. Before anything moves, the picture has to be this screen."""

    def test_i_a_current_picture_asks_for_nothing(self):
        self.make(speaking_fps=1)
        self.deliver(DISTINCT[0], at=0.0)
        self.clock.at(0.1)

        report = need_picture(self.context, reason="action")
        self.assertTrue(report["current"])
        self.assertFalse(report["requested"])
        self.assertEqual(report["reason"], "action")

    def test_i_a_picture_older_than_the_act_limit_is_refused(self):
        # No change, so the picture still *is* the screen -- but half a second is
        # the standard an act is held to, and this one is not. Reading a stale
        # screenshot is a bad answer; clicking one is a wrong action.
        self.make(speaking_fps=1)
        self.deliver(DISTINCT[0], at=0.0)

        self.clock.at(ACTION_MAX_AGE - 0.1)
        self.assertTrue(need_picture(self.context)["current"])

        self.clock.at(ACTION_MAX_AGE + 0.1)
        report = need_picture(self.context, reason="action")
        self.assertFalse(report["current"])
        self.assertTrue(report["requested"])
        self.assertGreater(report["age_ms"], ACTION_MAX_AGE * 1000)

    def test_i_a_session_with_no_share_is_honest_about_it(self):
        # The trap this whole feature exists to avoid: a session that cannot see
        # must not report a picture, a current one, or an age of zero. An
        # absent picture and a zero-millisecond-old picture are different facts,
        # and this branch has no sampler to ask -- a video track nobody opened,
        # a cascade profile that never sees a frame, a tool called from a CLI.
        report = need_picture(SimpleNamespace(session=Session()), reason="action")

        self.assertFalse(report["current"])
        self.assertFalse(report["requested"])
        self.assertIsNone(report["age_ms"])
        self.assertIsNone(report["generation"])
        self.assertIsNone(report["picture_generation"])

    def test_i_a_registered_sampler_that_has_never_seen_a_frame_says_so(self):
        # Different from the case above and reported differently: there *is* a
        # share running, it simply has not produced a picture yet. Asking for
        # one is correct; claiming an age of zero would not be.
        self.make(speaking_fps=60)
        report = need_picture(self.context, reason="action")

        self.assertFalse(report["current"])
        self.assertTrue(report["requested"])
        self.assertIsNone(report["age_ms"])
        self.assertEqual(report["generation"], 0)

    def test_i_a_seam_that_cannot_be_resolved_never_raises(self):
        # An unhashable or absent context is the common case in a CLI or a test
        # stub, and a tool must not fail because of its own pre-check.
        report = need_picture(SimpleNamespace(session=None), reason="action")
        self.assertFalse(report["requested"])
        self.assertIsNone(report["generation"])

        self.assertIsNone(sampler_for(object()))


class ToolWiringTests(VisionCase):
    """The two call sites: a single tool call and a planned step."""

    def test_observe_is_not_a_reason_to_spend_an_image(self):
        from agent.tools.desktop.control import _fresh_before_act

        self.make(speaking_fps=1)
        self.deliver(DISTINCT[0], at=0.0)
        self.clock.at(5.0)  # far past the act limit on purpose

        self.assertEqual(_fresh_before_act(self.context, "observe"), {})
        self.assertEqual(self.sampler.request_pending(), None)
        self.assertIsNone(self.sampler._pending_request)

        report = _fresh_before_act(self.context, "click")
        self.assertEqual(report["reason"], "action")
        self.assertTrue(report["requested"])
        self.assertIsNotNone(self.sampler.request_pending())

    def test_a_planned_step_arms_the_same_seam_with_its_own_reason(self):
        from agent.tools import task_runner

        self.make(speaking_fps=1)
        original = task_runner.need_picture
        calls: list[tuple[object, dict]] = []
        task_runner.need_picture = lambda ctx, **kw: calls.append((ctx, kw)) or {"current": True}
        self.addCleanup(setattr, task_runner, "need_picture", original)

        async def approve(**_kwargs):
            return None

        step = SimpleNamespace(args={"action": "click", "target": "9"})
        ctx = task_runner.StepContext(
            step=step,
            route=SimpleNamespace(target="", environment="desktop"),
            request="click 9",
            outputs={"generation": "42"},
            approve=approve,
            session=self.session,
        )

        # `by_ref` finds nothing, so the step refuses -- but only *after* the
        # picture was asked for, which is the order that matters: the request
        # must be in flight while the target is being resolved, not after it.
        observation = SimpleNamespace(by_ref=lambda _n: None)
        original_check = task_runner.desktop_capture.check_generation
        task_runner.desktop_capture.check_generation = lambda _g: observation
        self.addCleanup(
            setattr, task_runner.desktop_capture, "check_generation", original_check
        )

        result = self.call(task_runner._step_desktop_act, ctx)

        self.assertTrue(calls, "a planned act never asked for a current picture")
        _ctx, kwargs = calls[0]
        self.assertEqual(kwargs["reason"], "step")
        self.assertIs(_ctx.session, self.session)
        self.assertEqual(
            result.outcome.status, "failed",
            "the target was absent, so the step must refuse -- the point of the "
            "order is that the picture was asked for while it was still looking",
        )


    def call(self, fn, *args):
        import asyncio

        return asyncio.run(fn(*args))


class TelemetryTests(VisionCase):
    """The record that makes 'did she look?' answerable instead of arguable."""

    def test_every_field_the_brief_asks_for_is_present_and_true_when_sent(self):
        self.make(speaking_fps=60)
        self.sampler.request_fresh("turn", "general")
        self.deliver(DISTINCT[0], at=0.0)

        record = self.last_record()
        for field in (
            "frame_sequence",
            "frame_age_ms",
            "content_age_ms",
            "dropped_stale",
            "dropped_unchanged",
            "accepted",
            "dropped_budget",
            "dropped_expired",
            "dropped_superseded",
            "request_reason",
            "request_priority",
            "provider",
            "model",
            "image_attached",
            "image_bytes",
            "request_duration_ms",
            "result_generation",
            "current_generation",
        ):
            self.assertIn(field, record, f"{field} is missing from the delivery record")

        self.assertTrue(record["image_attached"])
        self.assertEqual(record["frame_sequence"], self.sampler.seq)
        self.assertEqual(record["provider"], "google")
        self.assertEqual(record["model"], "gemini-3.8-live")
        self.assertEqual(record["request_reason"], "turn")
        self.assertEqual(record["request_priority"], "normal")
        self.assertGreater(record["image_bytes"], 0)
        self.assertEqual(record["result_generation"], record["current_generation"])

    def test_an_absence_is_recorded_too(self):
        # The record that matters most is the one saying nothing went out. If
        # refusals were silent, 'no images in the log' would be
        # indistinguishable from 'the log is missing'.
        self.make(speaking_fps=60)
        self.deliver(DISTINCT[0], at=0.0)
        self.clock.at(MAX_PENDING_AGE + 1.0)
        self.rt._send_client_event(video_event(b"jpg-late"))

        record = self.last_record()
        self.assertFalse(record["image_attached"])
        self.assertEqual(record["image_bytes"], 0)

    def test_a_failing_emit_never_costs_a_frame(self):
        def explode(**_payload):
            raise OSError("the desktop app closed the pipe")

        self.clock = FakeClock()
        self.sampler = FreshnessVideoSampler(clock=self.clock, speaking_fps=60)
        self.rt = FakeRt(drain=False)
        self.session = Session(user_state="speaking", rt=self.rt)
        self.link = VisionLink(provider="google", model="m", emit=explode, clock=self.clock)
        self.link.bind(self.sampler)
        self.sampler.attach_link(self.link)
        register_sampler(self.session, self.sampler)
        self.addCleanup(unregister_sampler, self.session)

        ok, payload = self.deliver(DISTINCT[0], at=0.0)
        self.assertTrue(ok, "a broken telemetry channel must not eat the frame")
        self.assertEqual(self.queued_payloads(), [payload])
        self.assertEqual(self.link.emitted, 0)
        self.assertIsNotNone(self.link.last_record)


class ImagePreparationTests(VisionCase):
    """§10. The preparation layer, and the profile a request actually buys."""

    def test_general_is_the_catalogue_default_and_detail_is_wider(self):
        self.make(speaking_fps=60)
        self.assertTrue(self.link.install(self.session))
        self.assertEqual(self.link.profile, "general")
        general = self.rt._opts.image_encode_options
        self.assertIsNotNone(general)

        self.assertTrue(self.link.set_profile("detail"))
        detail = self.rt._opts.image_encode_options

        self.assertEqual(general.quality, 75)
        self.assertEqual(general.resize_options.width, 1024)
        self.assertEqual(general.resize_options.strategy, "scale_aspect_fit")
        self.assertGreater(detail.resize_options.width, general.resize_options.width)
        self.assertEqual(detail.format, "JPEG")

    def test_an_unknown_profile_falls_back_rather_than_raising(self):
        self.make(speaking_fps=60)
        self.link.install(self.session)
        self.assertTrue(self.link.set_profile("a-crop-nobody-defined"))
        self.assertEqual(self.link.profile, "general")
        self.assertEqual(
            set(PROFILES),
            {"general", "detail"},
            "the profile vocabulary and the encoder must not drift apart",
        )

    def test_an_unknown_request_profile_does_not_reach_the_encoder(self):
        self.make(speaking_fps=60)
        self.sampler.request_fresh("turn", "4k-ultra")
        self.deliver(DISTINCT[0], at=0.0)
        self.assertEqual(self.link.profile, "general")


class SeparationTests(unittest.TestCase):
    """The two systems that must never become one system."""

    def test_the_state_heartbeat_is_not_wired_to_a_frame_scheduler(self):
        # `VISION_SYNC_SECONDS` pushes *instructions* -- whether the desktop is
        # sharing, whether permission is granted -- and nothing in it may ever
        # become "send a screenshot every 5 seconds". A cheap state heartbeat and
        # an expensive semantic image are different costs with different
        # triggers, and the moment one schedule drives both, the guarantee that
        # a stable screen costs nothing is gone.
        for name in ("frame_sampler.py", "vision_link.py"):
            source = (AGENT_DIR / "runtime" / name).read_text(encoding="utf-8")
            self.assertNotIn(
                "VISION_SYNC_SECONDS",
                source,
                f"{name} has reached for the state heartbeat as a vision schedule",
            )

    def test_the_sampler_still_uses_the_documented_gates(self):
        # The defaults this module must not quietly change.
        from agent.runtime import frame_sampler

        self.assertEqual(frame_sampler.MAX_PENDING_AGE, 2.0)
        self.assertEqual(frame_sampler.ACTION_MAX_AGE, 0.5)
        self.assertLess(
            frame_sampler.ACTION_MAX_AGE,
            frame_sampler.MAX_PENDING_AGE,
            "an act has to be held to a stricter standard than a queued frame",
        )
        self.assertLess(
            frame_sampler.MAX_PENDING_AGE,
            15.0,  # visual_state.READY_INTERVAL
            "dropping an expired frame must never be able to flip a healthy share to degraded",
        )
        self.assertGreater(frame_sampler.CHANGE_RATIO, 0.0)


class SdkHookTests(unittest.TestCase):
    """The private seam this whole feature stands on, checked against the real SDK.

    :class:`~agent.runtime.vision_link.VisionLink` wraps
    ``RealtimeSession._send_client_event``. That is not public API -- it is one
    method on the Google plugin's realtime session, reached by ``push_video``
    *after* it encodes, and it is the only place at which "an image is in this
    request" is a fact rather than an inference.

    Every other test in this module uses a fake that carries its own
    ``_send_client_event``, so a plugin upgrade that renames the real one leaves
    all of them green while the feature quietly stops observing every image it
    sends. That is the worst shape this failure could take: images still go to
    the model, telemetry still says nothing is wrong, and "did she look?" is
    answered from a log that has simply stopped recording the answer.

    This class is why the version dependency is *documented* rather than
    assumed. Measured against ``livekit-agents`` 1.8.3 and
    ``livekit-plugins-google`` 1.8.2, where ``push_video`` reads::

        encoded_data = images.encode(frame, self._opts.image_encode_options ...)
        realtime_input = types.LiveClientRealtimeInput(video=types.Blob(...))
        self._send_client_event(realtime_input)
    """

    PLUGIN = "livekit.plugins.google.realtime.realtime_api"

    def _plugin(self):
        try:
            return importlib.import_module(self.PLUGIN)
        except ImportError:  # pragma: no cover - plugin not installed
            self.skipTest("livekit-plugins-google is not installed")

    def test_the_session_still_exposes_the_method_the_link_wraps(self):
        plugin = self._plugin()
        session = getattr(plugin, "RealtimeSession", None)
        self.assertIsNotNone(session, "the plugin no longer defines RealtimeSession")
        self.assertTrue(
            hasattr(session, "_send_client_event"),
            "RealtimeSession._send_client_event is gone; VisionLink wraps it and "
            "would now observe nothing",
        )

    def test_push_video_still_reaches_the_wrapped_method(self):
        """The *behaviour*, not merely the attribute.

        ``hasattr`` would pass even if ``push_video`` had stopped calling the
        method -- and that is the exact failure that would leave every image
        unobserved while it kept being sent.
        """
        plugin = self._plugin()
        session = getattr(plugin, "RealtimeSession", None)
        push = getattr(session, "push_video", None)
        self.assertIsNotNone(push, "RealtimeSession.push_video is gone")
        source = inspect.getsource(push)
        self.assertIn(
            "_send_client_event",
            source,
            "push_video no longer calls _send_client_event; the image-attachment "
            "seam has moved and VisionLink must follow it",
        )

    def test_the_encode_profile_is_still_writable_at_runtime(self):
        """``sync_profile`` writes this per frame, so a rename breaks it silently.

        ``_RealtimeOptions`` is deliberately not frozen, which is what lets a
        profile switch take effect without reconnecting. If it were frozen, or
        the field renamed, ``sync_profile`` would raise on every frame -- and
        :meth:`VisionLink._apply_profile` guards that import, so the guard
        would swallow it and the profile would simply never change.
        """
        plugin = self._plugin()
        options = getattr(plugin, "_RealtimeOptions", None)
        self.assertIsNotNone(options, "_RealtimeOptions is gone")
        fields = getattr(options, "__dataclass_fields__", {})
        self.assertIn(
            "image_encode_options",
            fields,
            "_RealtimeOptions no longer carries image_encode_options; "
            "sync_profile has nothing to write",
        )

    def test_the_image_helpers_the_profiles_are_built_from_still_exist(self):
        """``_apply_profile`` constructs these, and only inside a lazy import."""
        try:
            from livekit.agents.utils import images
        except ImportError:  # pragma: no cover - plugin not installed
            self.skipTest("livekit-agents is not installed")
        for name in ("EncodeOptions", "ResizeOptions"):
            self.assertTrue(
                hasattr(images, name),
                f"livekit.agents.utils.images.{name} is gone; the image profiles "
                "are built from it",
            )


if __name__ == "__main__":
    unittest.main()
