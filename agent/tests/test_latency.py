"""What the latency recorder measures -- and the one thing it must never keep.

The recorder answers two different questions with two different shapes, and
both are load-bearing on the other side of the process:

* ``mark`` is the *original* shape: a stage name plus an integer ``elapsed_ms``
  measured from worker start. The activity timeline reads ``elapsed_ms`` as "how
  far into the session" and redefining it would silently reinterpret rows
  already on screen -- so it is pinned here rather than trusted.
* ``duration_ms`` / ``phase`` / ``turn_id`` are the measured shape: how long
  *this* took, attributed to the turn it happened in, with a phase so model time
  is never read as tool time.

The third contract is about what is *not* recorded. Stage names, durations and
ids travel; conversation text does not. The final test here reads a payload back
after the session that produced it is gone and asserts nothing survived but
numbers and names.
"""

from __future__ import annotations

import unittest

from agent.runtime import latency as latency_module
from agent.runtime.latency import (
    DEFAULT_PHASE,
    MIN_TURN_GAP,
    PHASES,
    LatencyTracker,
    emit_duration,
    measure,
    register_tracker,
    tracker_for,
    unregister_tracker,
)


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class Sink:
    """Collects every record the tracker publishes."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def __call__(self, kind: str, **payload) -> None:
        self.records.append({"type": kind, **payload})

    def of(self, kind: str) -> list[dict]:
        return [record for record in self.records if record["type"] == kind]

    def stages(self) -> list[str]:
        return [record.get("stage") for record in self.of("latency")]


class Session:
    """A stand-in for an ``AgentSession``: an ordinary weakref-able object.

    Not ``object()``. The builtin ``object`` refuses weak references, so a test
    built on it registers nothing, resolves nothing, and passes for the wrong
    reason -- which is exactly how a registry that works in the test file and
    not in the worker would go unnoticed. ``AgentSession`` itself was checked
    against this: instances of it do support weak references.
    """

    pass


class LegacyMarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.sink = Sink()
        self.tracker = LatencyTracker(self.sink, session_id="session-1", clock=self.clock)

    def test_a_mark_is_the_shape_the_timeline_already_reads(self):
        self.clock.advance(1.25)
        returned = self.tracker.mark("session_started")

        (record,) = self.sink.of("latency")
        self.assertEqual(record["stage"], "session_started")
        self.assertEqual(record["elapsed_ms"], 1250, "elapsed_ms is an integer count of ms")
        self.assertEqual(record["elapsed_ms"], int(record["elapsed_ms"]))
        self.assertEqual(record["phase"], DEFAULT_PHASE)
        self.assertEqual(record["session_id"], "session-1")
        self.assertEqual(record["turn_id"], 0, "no turn is open, so it belongs to none")
        self.assertEqual(returned, 1250.0)

    def test_marks_count_from_the_worker_start_not_from_the_turn(self):
        # The distinction the whole feature rests on: a mark taken three seconds
        # into the session while a turn opened one second ago reads 3000, not
        # 2000. A recorder that silently re-based itself on the turn would make
        # every existing row on screen mean something else.
        self.clock.advance(1.0)
        self.tracker.mark("first")
        self.clock.advance(1.0)
        self.tracker.begin_turn()
        self.clock.advance(1.0)
        self.tracker.mark("second")

        first, second = (record["elapsed_ms"] for record in self.sink.of("latency")[:2])
        self.assertEqual(first, 1000)
        self.assertEqual(second, 3000)

    def test_since_measures_a_known_stage_and_admits_an_unknown_one(self):
        self.assertIsNone(self.tracker.since("never_marked"))
        self.clock.advance(2.0)
        self.tracker.mark("ready")
        self.clock.advance(0.5)
        self.assertEqual(self.tracker.since("ready"), 500.0)
        self.assertIsNone(self.tracker.since("never_marked"))

    def test_sequence_numbers_only_go_up(self):
        # The UI correlates records within a turn by arrival order; a sequence
        # that repeats would let two rows of one turn read as one.
        self.clock.advance(0.1)
        self.tracker.mark("a")
        self.tracker.emit_duration("b", 10.0, phase="model")
        self.tracker.begin_turn()
        self.tracker.end_turn()
        seqs = [record["seq"] for record in self.sink.of("latency")]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs), "a sequence number repeated")


class DurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.sink = Sink()
        self.tracker = LatencyTracker(self.sink, session_id="s", clock=self.clock)

    def test_a_duration_keeps_its_decimal_for_the_report(self):
        returned = self.tracker.emit_duration("llm_first_token", 123.456, phase="model")

        (record,) = self.sink.of("latency")
        self.assertEqual(record["duration_ms"], 123.5, "a tenth is kept, not rounded away")
        self.assertEqual(record["elapsed_ms"], 124, "the timeline still renders whole ms")
        self.assertEqual(record["phase"], "model")
        self.assertEqual(returned, 123.5)

    def test_every_phase_is_one_of_the_published_ones(self):
        # The report groups by phase. A typo'd phase is a row that groups under
        # nothing at all, which is how a metric silently disappears.
        for phase in ("stt", "model", "tool", "permission", "network", "tts", "visual", "local", "e2e"):
            with self.subTest(phase=phase):
                self.assertIn(phase, PHASES)
                self.tracker.emit_duration("stage", 1.0, phase=phase)
        self.assertEqual({record["phase"] for record in self.sink.of("latency")}, set(PHASES))

    def test_an_unlabelled_duration_is_local_work(self):
        self.tracker.emit_duration("stage", 5.0)
        (record,) = self.sink.of("latency")
        self.assertEqual(record["phase"], "local")

    def test_the_default_phase_is_itself_a_declared_phase(self):
        # A fallback that names a bucket the report does not know is a row that
        # groups under nothing, which is how a metric silently disappears.
        self.assertIn(DEFAULT_PHASE, PHASES)

    def test_a_span_measures_the_block_around_it(self):
        with self.tracker.span("read_file", phase="local"):
            self.clock.advance(0.4)
        (record,) = self.sink.of("latency")
        self.assertEqual(record["stage"], "read_file")
        self.assertAlmostEqual(record["duration_ms"], 400.0, places=3)

    def test_a_span_still_reports_when_the_block_raises(self):
        # A failing block is exactly when a duration is worth having: the report
        # should show time spent, not a hole where the failure was.
        with self.assertRaises(ValueError):
            with self.tracker.span("boom", phase="tool"):
                self.clock.advance(0.2)
                raise ValueError("nope")
        (record,) = self.sink.of("latency")
        self.assertAlmostEqual(record["duration_ms"], 200.0, places=3)


class TurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.sink = Sink()
        self.tracker = LatencyTracker(self.sink, session_id="s", clock=self.clock)

    def test_a_turn_is_opened_closed_and_priced_end_to_end(self):
        self.assertFalse(self.tracker.turn_opened)
        turn = self.tracker.begin_turn(final="read it out")
        self.assertTrue(self.tracker.turn_opened)
        self.assertEqual(turn, 1)

        (opened,) = self.sink.of("latency")
        self.assertEqual(opened["stage"], "turn_open")
        self.assertEqual(opened["final"], "read it out")
        self.assertEqual(opened["phase"], "stt")

        self.clock.advance(0.8)
        duration = self.tracker.end_turn()

        self.assertAlmostEqual(duration, 800.0, places=3)
        self.assertFalse(self.tracker.turn_opened)
        closing = [record for record in self.sink.of("latency") if record["stage"] == "turn_end_to_end"]
        (record,) = closing
        self.assertEqual(record["phase"], "e2e")
        self.assertAlmostEqual(record["duration_ms"], 800.0, places=3)
        self.assertFalse(record["interrupted"])
        self.assertEqual(record["turn_id"], turn)

    def test_closing_a_turn_that_was_never_opened_emits_nothing(self):
        self.assertIsNone(self.tracker.end_turn())
        self.assertEqual(self.sink.records, [])

    def test_since_turn_is_none_without_an_open_turn(self):
        self.assertIsNone(self.tracker.since_turn())
        self.tracker.begin_turn()
        self.clock.advance(0.25)
        self.assertAlmostEqual(self.tracker.since_turn(), 250.0, places=3)

    def test_segmented_finals_are_one_utterance_not_two(self):
        # Speech-to-text commits a sentence in pieces. If each final minted a
        # turn, the report would fill with 200ms "end-to-end" rows describing
        # nothing anyone waited for -- and the real turn would be lost.
        self.tracker.begin_turn(final="one")
        self.clock.advance(MIN_TURN_GAP / 2)
        same = self.tracker.begin_turn(final="two")

        self.assertEqual(same, 1, "a final this soon is the same utterance")
        opens = [r for r in self.sink.of("latency") if r["stage"] == "turn_open"]
        self.assertEqual(
            len(opens),
            1,
            "the segmented final minted a second turn",
        )
        self.assertEqual(opens[0]["final"], "one", "the utterance is named by its first final")

        self.clock.advance(MIN_TURN_GAP)
        next_turn = self.tracker.begin_turn(final="three")
        self.assertEqual(next_turn, 2)
        self.assertTrue(self.tracker.turn_opened)

        closed = [r for r in self.sink.of("latency") if r["stage"] == "turn_end_to_end"]
        (record,) = closed
        self.assertEqual(record["turn_id"], 1)
        self.assertTrue(record["interrupted"], "the replaced turn is closed as interrupted")
        # Measured from the *first* final, so the figure still covers the whole
        # utterance rather than only the last fragment of it.
        self.assertAlmostEqual(record["duration_ms"], (MIN_TURN_GAP * 1.5) * 1000, places=3)

    def test_the_first_final_of_a_session_starts_the_only_turn(self):
        self.assertEqual(self.tracker.begin_turn(), 1)
        self.assertEqual(self.tracker.begin_turn(), 1)
        self.assertEqual(self.tracker.turn_id, 1)

    def test_a_turn_that_answered_is_not_reported_as_interrupted(self):
        # The regression: a turn was closed as "interrupted" purely because a
        # turn was still open when the next one began. A realtime model answers
        # and returns to listening rather than going idle, so the idle close
        # never arrived and *every* turn was flagged -- a field that is always
        # true tells a reader nothing and reads as though every answer were cut
        # off. "Still open" and "still open without having answered" are
        # different facts, and the answer is what separates them.
        self.tracker.begin_turn(final="how far is it")
        self.clock.advance(1.0)
        self.tracker.note_answer()
        self.clock.advance(0.5)
        self.tracker.begin_turn(final="and now")

        (record,) = [r for r in self.sink.of("latency") if r["stage"] == "turn_end_to_end"]
        self.assertFalse(record["interrupted"], "the answer was spoken in full")
        self.assertAlmostEqual(record["duration_ms"], 1500.0, places=3)

    def test_a_turn_replaced_before_it_answered_is_interrupted(self):
        # The other half, and the reason the flag exists at all: no answer, so
        # the user was still waiting when the next turn opened.
        self.tracker.begin_turn(final="and now")
        self.clock.advance(1.0)
        self.tracker.begin_turn(final="never mind")

        (record,) = [r for r in self.sink.of("latency") if r["stage"] == "turn_end_to_end"]
        self.assertTrue(record["interrupted"], "nothing was ever answered")

    def test_the_answer_does_not_carry_into_the_next_turn(self):
        # `note_answer` is per turn. Left set, every later turn would report
        # itself as answered before it had produced a word.
        self.tracker.begin_turn()
        self.tracker.note_answer()
        self.clock.advance(MIN_TURN_GAP)
        self.tracker.begin_turn()
        self.clock.advance(MIN_TURN_GAP)
        self.tracker.begin_turn()

        closed = [r for r in self.sink.of("latency") if r["stage"] == "turn_end_to_end"]
        self.assertFalse(closed[0]["interrupted"], "turn 1 answered")
        self.assertTrue(closed[1]["interrupted"], "turn 2 did not")


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sink = Sink()
        self.tracker = LatencyTracker(self.sink, session_id="s", clock=FakeClock())

    def test_a_registered_session_resolves_to_its_tracker(self):
        session = Session()
        register_tracker(session, self.tracker)
        self.assertIs(tracker_for(session), self.tracker)
        unregister_tracker(session)
        self.assertIsNone(tracker_for(session))

    def test_a_missing_or_none_session_is_not_an_error(self):
        self.assertIsNone(tracker_for(None))
        self.assertIsNone(tracker_for(Session()))  # weakref-able, never registered

    def test_a_key_the_registry_cannot_hold_is_answered_not_raised(self):
        # `object()` refuses weak references, as do modules and builtins. Such a
        # key cannot be *stored*, so there is nothing to find -- but looking one
        # up must still answer rather than raise, because the caller is a tool.
        register_tracker(object(), self.tracker)  # must not raise
        self.assertIsNone(tracker_for(object()))

    def test_a_session_that_cannot_be_weakly_referenced_is_dropped_not_raised(self):
        # WeakKeyDictionary raises TypeError on such a key. The registration is
        # an optimisation for tool sub-spans; losing it costs a number, while
        # raising here would cost the session.
        class NoWeakRefs:
            __slots__ = ()

        session = NoWeakRefs()
        register_tracker(session, self.tracker)  # must not raise
        unregister_tracker(session)  # must not raise either
        self.assertIsNone(tracker_for(session))

    def test_a_tracker_is_reachable_only_through_a_live_session(self):
        session = Session()
        register_tracker(session, self.tracker)
        self.assertIs(tracker_for(session), self.tracker)
        # Held weakly: a session that was never unregistered must not keep its
        # tracker -- or it -- alive after the room closes.
        import gc

        del session
        gc.collect()
        self.assertEqual(len(latency_module._TRACKERS), 0, "the registry pinned a dead session")

    def test_emit_without_a_tracker_is_a_no_op(self):
        emit_duration(Session(), "tool_exec", 12.0)
        self.assertEqual(self.sink.records, [], "an unregistered session published something")

    def test_a_raising_emitter_does_not_reach_the_caller(self):
        class Exploding:
            def __call__(self, *args, **kwargs) -> None:
                raise OSError("the pipe is gone")

        tracker = LatencyTracker(Exploding(), session_id="s", clock=FakeClock())
        session = Session()
        register_tracker(session, tracker)
        try:
            # Called from inside tools: telemetry that raises would turn a
            # working tool call into a failed one, which is the same failure
            # `write_event_line` exists to prevent on stdout.
            emit_duration(session, "tool_exec", 5.0, phase="tool")
        finally:
            unregister_tracker(session)

    def test_measure_yields_around_the_block_when_registered(self):
        clock = FakeClock()
        tracker = LatencyTracker(self.sink, session_id="s", clock=clock)
        session = Session()
        register_tracker(session, tracker)
        try:
            with measure(session, "resolve", phase="local"):
                clock.advance(0.05)
        finally:
            unregister_tracker(session)
        (record,) = self.sink.of("latency")
        self.assertEqual(record["stage"], "resolve")
        self.assertAlmostEqual(record["duration_ms"], 50.0, places=3)

    def test_measure_without_a_tracker_still_runs_the_block(self):
        ran = []
        with measure(Session(), "resolve"):
            ran.append(1)
        self.assertEqual(ran, [1])
        self.assertEqual(self.sink.records, [])


class WhatIsNotRecordedTests(unittest.TestCase):
    def test_a_payload_carries_names_durations_and_ids_and_nothing_else(self):
        # The whole point of writing these to stdout: the desktop app re-emits
        # them, so anything conversational in here would be a transcript written
        # to a log file the moment it was spoken.
        clock = FakeClock()
        sink = Sink()
        tracker = LatencyTracker(sink, session_id="s", clock=clock)
        allowed = {
            "type",
            "stage",
            "elapsed_ms",
            "duration_ms",
            "phase",
            "session_id",
            "turn_id",
            "seq",
            "interrupted",
            "final",
            "tool",
            "tool_call_id",
            "status",
            "action",
            "action_class",
            "decision",
            "channel",
        }
        tracker.mark("session_started", tool="open_app")
        tracker.begin_turn(final="open notepad")
        tracker.emit_duration("tool_exec", 12.0, phase="tool", tool="open_app", tool_call_id="call-1")
        tracker.end_turn()

        for record in sink.records:
            with self.subTest(record=record["type"]):
                unexpected = set(record) - allowed
                self.assertFalse(unexpected, f"a record carries fields the contract does not declare: {unexpected}")
                for key, value in record.items():
                    self.assertNotIn("\n", str(value), f"{key} looks like it is carrying prose")
                    self.assertLess(len(str(value)), 200, f"{key} is long enough to be content, not metadata")


if __name__ == "__main__":
    unittest.main()
