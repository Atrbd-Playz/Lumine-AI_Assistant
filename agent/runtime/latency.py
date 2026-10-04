"""Latency recording for the voice runtime, scoped to the turn it happened in.

The recorder emits the same ``LUMINE_EVENT`` contract as the rest of the worker,
and deliberately stores only stage names, durations and ids -- never conversation
text, page content or credentials.

Two shapes of measurement come out of here, and they answer different questions:

* :meth:`LatencyTracker.mark` -- "how long since the worker started". This is the
  original API and it is unchanged: the timeline renders it as a stage row and
  existing callers depend on the meaning.
* :meth:`LatencyTracker.span` / :meth:`LatencyTracker.emit_duration` -- "how long
  did *this* take". Monotonic start/stop, correlated with a session id, a turn id
  and (where there is one) a tool-call id.

Every duration carries a ``phase`` so that model time, tool time, permission wait
and network time are never read as one number. The end-to-end figure -- the one
the user actually feels -- is emitted as ``turn_end_to_end`` when a turn closes.

The session registry at the bottom exists so a tool can annotate its own work
without being handed the tracker: a tool receives a ``RunContext`` and nothing
else, and ``permission_gate`` already established the pattern of resolving
session-scoped state from a module-level registry.
"""

from __future__ import annotations

import time
import uuid
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

#: How a duration is classified. ``e2e`` is the turn as a whole -- user finished
#: speaking to reply actually spoken -- and everything else is a component of it.
PHASES = ("stt", "model", "tool", "permission", "network", "tts", "visual", "local", "e2e")

#: The default phase for a span nobody classified. ``local`` says "this was our
#: own work", which is the safe reading of an unlabelled measurement.
DEFAULT_PHASE = "local"

#: Two final transcripts closer together than this are one utterance, not two
#: turns. Speech-to-text can commit a single sentence in pieces, and restarting
#: the turn on each piece would publish a hundred-millisecond "end-to-end"
#: answer that never described anything a person waited for.
MIN_TURN_GAP = 0.5


class LatencyTracker:
    """Stage marks and measured spans for one session."""

    def __init__(
        self,
        emit: Callable[..., Any],
        *,
        session_id: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emit = emit
        self._clock = clock
        self._started = clock()
        self._stage_times: dict[str, float] = {}
        self._seq = 0
        self.session_id = str(session_id) or uuid.uuid4().hex[:8]
        self.turn_id = 0
        self._turn_opened_at: float | None = None
        self._turn_answered = False

    # -- legacy stage marks -------------------------------------------------

    def mark(self, stage: str, **details: Any) -> float:
        """Mark a stage as having happened. Elapsed is since the worker started.

        Unchanged from the original recorder on purpose: the activity timeline
        reads ``elapsed_ms`` as "how far into the session", and redefining it
        would silently reinterpret rows already on screen.
        """
        now = self._clock()
        elapsed_ms = round((now - self._started) * 1000)
        self._stage_times[stage] = now
        self._seq += 1
        self._emit(
            "latency",
            stage=stage,
            elapsed_ms=elapsed_ms,
            phase=DEFAULT_PHASE,
            session_id=self.session_id,
            turn_id=self.turn_id,
            seq=self._seq,
            **details,
        )
        return float(elapsed_ms)

    def since(self, stage: str) -> float | None:
        started = self._stage_times.get(stage)
        if started is None:
            return None
        return round((self._clock() - started) * 1000, 3)

    # -- measured durations -------------------------------------------------

    def emit_duration(
        self,
        stage: str,
        duration_ms: float,
        *,
        phase: str = DEFAULT_PHASE,
        **details: Any,
    ) -> float:
        """Publish one measured duration. Used when the clock is someone else's.

        Provider metrics arrive with their own timings -- ``ttft`` from the LLM,
        ``ttfb`` from the TTS -- and re-measuring them locally would report our
        event-loop latency as the provider's. So the number travels, unrounded in
        spirit: ``duration_ms`` keeps a decimal, ``elapsed_ms`` is the integer the
        timeline renders.
        """
        value = round(float(duration_ms), 1)
        self._seq += 1
        self._emit(
            "latency",
            stage=stage,
            elapsed_ms=round(value),
            duration_ms=value,
            phase=phase,
            session_id=self.session_id,
            turn_id=self.turn_id,
            seq=self._seq,
            **details,
        )
        return value

    @contextmanager
    def span(self, stage: str, *, phase: str = DEFAULT_PHASE, **details: Any) -> Iterator[None]:
        """Measure the block around it, on a monotonic clock.

        ``time.time`` is wrong for this: a wall-clock step (NTP, sleep/resume)
        would report a negative duration or an hour-long one for a block that ran
        normally. Monotonic cannot be adjusted underneath us.
        """
        started = self._clock()
        try:
            yield
        finally:
            self.emit_duration(stage, (self._clock() - started) * 1000, phase=phase, **details)

    # -- turns --------------------------------------------------------------

    def begin_turn(self, **details: Any) -> int:
        """Open a turn, closing the one it replaces.

        A new final transcript while a turn is still open means the previous turn
        ended. Closing it here rather than at "agent went idle" keeps the
        end-to-end figure honest: a turn measured to the interruption is still a
        real measurement of how long the user waited.

        ``interrupted`` is not "a turn was still open" -- it is "the turn was
        still open *and had not answered yet*". Those are different, and
        collapsing them makes the flag constant. A realtime model answers and
        returns to listening rather than going idle, so the idle close never
        arrives and every turn was reported as cut off; the answer the user
        actually heard was recorded as an interruption. :meth:`note_answer` is
        what separates the two.

        Finals arriving within :data:`MIN_TURN_GAP` of the turn opening are the
        same utterance and are ignored, so a segmented transcript cannot publish
        a series of turns nobody actually had.
        """
        if self._turn_opened_at is not None:
            if (self._clock() - self._turn_opened_at) < MIN_TURN_GAP:
                return self.turn_id
            self.end_turn(interrupted=not self._turn_answered)
        self.turn_id += 1
        self._turn_opened_at = self._clock()
        self._turn_answered = False
        if details:
            self.emit_duration("turn_open", 0.0, phase="stt", **details)
        return self.turn_id

    def note_answer(self) -> None:
        """Record that the open turn's answer is final.

        The agent's own final transcript is the only trustworthy end-of-turn
        signal, because it is the only one that means the words existed. A tool
        call is not an answer, and neither is the agent returning to listening
        mid-turn to run one.
        """
        self._turn_answered = True

    def end_turn(self, *, interrupted: bool = False, **details: Any) -> float | None:
        """Close the open turn and publish its end-to-end duration."""
        opened_at = self._turn_opened_at
        if opened_at is None:
            return None
        duration_ms = (self._clock() - opened_at) * 1000
        self._turn_opened_at = None
        self._turn_answered = False
        self.emit_duration(
            "turn_end_to_end",
            duration_ms,
            phase="e2e",
            interrupted=interrupted,
            **details,
        )
        return duration_ms

    @property
    def turn_opened(self) -> bool:
        return self._turn_opened_at is not None

    def since_turn(self) -> float | None:
        """Milliseconds since the current turn opened, or ``None`` if none is."""
        if self._turn_opened_at is None:
            return None
        return (self._clock() - self._turn_opened_at) * 1000


# ---------------------------------------------------------------------------
# Session registry
#
# The same shape as permission_gate's gate registry: session-keyed, resolved
# from whatever a tool was handed, gone when the session closes. Weak keys so a
# session that was never released cannot leak one of these.
# ---------------------------------------------------------------------------

_TRACKERS: weakref.WeakKeyDictionary[Any, LatencyTracker] = weakref.WeakKeyDictionary()


def register_tracker(session: Any, tracker: LatencyTracker) -> None:
    try:
        _TRACKERS[session] = tracker
    except TypeError:
        # An object that cannot hold a weak reference is not an AgentSession in
        # any supported version. Dropping the registration costs tool sub-spans;
        # raising here would cost the session.
        pass


def unregister_tracker(session: Any) -> None:
    try:
        _TRACKERS.pop(session, None)
    except TypeError:
        pass


def tracker_for(session: Any) -> LatencyTracker | None:
    if session is None:
        return None
    try:
        return _TRACKERS.get(session)
    except TypeError:
        return None


def emit_duration(
    session: Any,
    stage: str,
    duration_ms: float,
    *,
    phase: str = "tool",
    **details: Any,
) -> None:
    """Publish a duration measured elsewhere against the session's tracker.

    A no-op when nothing registered the session -- a script, a unit test, a tool
    run outside a room. Telemetry that has nowhere to go must not raise, for the
    same reason ``write_event_line`` must not: this is called from inside tools.
    """
    tracker = tracker_for(session)
    if tracker is None:
        return
    try:
        tracker.emit_duration(stage, duration_ms, phase=phase, **details)
    except Exception:  # noqa: BLE001 - measurement must never break the caller
        pass


@contextmanager
def measure(
    session: Any,
    stage: str,
    *,
    phase: str = "tool",
    **details: Any,
) -> Iterator[None]:
    """Context form of :func:`emit_duration`. Safe with no tracker registered."""
    tracker = tracker_for(session)
    if tracker is None:
        yield
        return
    with tracker.span(stage, phase=phase, **details):
        yield
