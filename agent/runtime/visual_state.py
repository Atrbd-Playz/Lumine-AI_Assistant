"""What Lumine can currently see, and how old that is.

A frame that reaches the model is a photograph, not a video: the model has no way
to know it was taken four seconds ago or ten minutes ago, and a description of an
old frame delivered as "here is what is on screen" is the exact failure this
module exists to make impossible to hide.

It tracks five things and publishes them:

* **Which source** -- camera, screen, or nothing. Fed from the room's own track
  events, so "the share stopped" is observed rather than inferred.
* **How many frames** have been accepted, in order, and when the newest arrived.
* **How many were dropped** for being stale or unchanged, which is what makes the
  difference between "Lumine is looking at a live view" and "Lumine is looking at
  one screenshot from before you switched tabs".
* **Whether anything can arrive at all** -- the model's declared input
  modalities, and whether the operating system granted the capture. Both are
  facts about *other* components, so both arrive as inputs rather than being
  inferred here.
* **What phase that leaves sight in** -- :meth:`VisualState.phase` computes one
  word from the facts above, so a reader never has to combine four fields to
  learn whether she is looking at something current.

Nothing here inspects pixels beyond their dimensions, and nothing here ever
publishes image data or frame content -- only ages, counts and identifiers.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

#: How often a frame heartbeat may be published. Frames arrive at 0.3-1 fps, so
#: this mostly does nothing; it exists so a bursty stream cannot flood stdout.
DEFAULT_EMIT_INTERVAL = 1.0

#: How long a share may go without delivering *anything* before the view stops
#: counting as current.
#:
#: Deliberately above ``frame_sampler.MAX_QUIET_INTERVAL`` (10s): a quiet screen
#: is sampled at most every ten seconds, so a live and perfectly healthy share
#: can legitimately be that silent. Judging it against a shorter number would
#: report every idle screen as degraded, and a state that cries wolf is worse
#: than no state -- the model would learn to ignore the one line that tells it
#: whether the picture in front of it is real.
READY_INTERVAL = 15.0

#: What the app can report about the capture, which happens in a browser window
#: this process never sees. Four words, and anything else is ignored rather than
#: trusted: this is the only path by which ``denied`` can be known here.
PERMISSIONS = ("not_requested", "requesting_permission", "denied", "enabled")

#: Every value :meth:`VisualState.phase` can return, most specific last.
#:
#: Computed rather than stored, so the states cannot drift apart: a stored
#: transition has to be set at every call site that changes one of the facts it
#: depends on, and every site that forgets is a state that lies.
PHASES = (
    "error",
    "unavailable",
    "denied",
    "requesting_permission",
    "not_requested",
    "enabled",
    "stopped",
    "capturing",
    "frame_available",
    "vision_ready",
    "vision_degraded",
)

#: Reasons a ``visual`` record is published. Read by the UI as state, not as log.
REASONS = ("started", "source", "stopped", "frame", "idle", "permission", "capability", "error")

#: Longest capture-failure sentence carried in a record. The app writes these
#: for a person reading a toast; a record is re-published on every heartbeat, so
#: an unbounded one would be the same paragraph in the log forever.
ERROR_MESSAGE_CHARS = 160

#: What the model is told about sight, as prose around the one line of facts.
#:
#: The prose is constant, so it is paid once per push and never differs between
#: two pushes -- which is what lets :meth:`VisualState.vision_block` be compared
#: as a whole string to decide whether a push is needed at all.
VISION_NOTE = """## Sight state (this session)

The line below is what is *actually* true of this session right now, and it
overrides the paragraphs above whenever they disagree. It is pushed again on
every change, so it is current at the moment it was written -- but it carries no
age, because an age written once is stale by the time it is read. Treat `phase`
as the freshness signal: `vision_ready` means a picture is arriving now,
`vision_degraded` means the share is up but frames have stopped, and anything
else means she is not looking at anything. She never quotes a frame age she was
not given in the same turn."""


def _decode(payload: Any) -> Any:
    """``bytes``/``str``/``dict`` -> a mapping, or anything else unchanged.

    A report arrives as UTF-8 bytes on a data topic. Accepting the other two
    shapes as well is not flexibility for its own sake -- it is what lets a test
    hand this function the object it means without first serialising it, and it
    costs one ``isinstance``.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    return payload


class VisualState:
    """Latest-frame state for one session."""

    def __init__(
        self,
        emit: Callable[..., Any] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        emit_interval: float = DEFAULT_EMIT_INTERVAL,
    ) -> None:
        self._emit = emit
        self._clock = clock
        self._wall = wall
        self._emit_interval = float(emit_interval)
        self._last_emit_at = 0.0

        self.source: str | None = None
        self.sharing = False
        self.seq = 0
        self.last_frame_at: float | None = None
        self.last_frame_wall: float | None = None
        self.last_change_at: float | None = None
        #: When the sampler last *saw* a frame through the current track,
        #: whatever it then decided to do with it. Advanced by an accepted frame
        #: and by an ``unchanged`` refusal alike, because both are evidence that
        #: frames are arriving -- and deliberately not by a ``stale`` refusal,
        #: which is evidence that they are arriving too late to be trusted.
        #:
        #: Distinct from ``last_frame_at`` on purpose: that one is the newest
        #: *picture the model holds*, and it stops moving on a screen nobody is
        #: touching. This one is the liveness of the stream.
        self.last_delivery_at: float | None = None
        self.dropped_stale = 0
        self.dropped_unchanged = 0
        self.width = 0
        self.height = 0

        #: Whether the *model* can read an image at all, from the catalog's own
        #: ``input_modalities``. ``None`` until the pipeline has said, because a
        #: guess here would be a claim about a provider this module never met.
        self.can_see: bool | None = None
        #: What the app reports about the operating system's answer.
        self.permission = "not_requested"
        #: The app's own words for a capture that failed, truncated. For the UI;
        #: the model gets the phase instead, which is the part that is stable.
        self.error_message: str | None = None

        #: The phase on the last record that was actually *emitted*, not the
        #: last one that was attempted. Read by :meth:`pending_phase_change`.
        self._published_phase: str | None = None

    # -- inputs -------------------------------------------------------------

    def note_capability(self, can_see: bool | None) -> bool:
        """Record whether the selected model declares image input.

        Set once per session by the pipeline factory's own answer, so the two
        can never disagree -- and asked rather than assumed, because LiveKit's
        documented behaviour for a model that cannot read frames is to drop them
        *silently*.
        """
        normalised = None if can_see is None else bool(can_see)
        if normalised == self.can_see:
            return False
        self.can_see = normalised
        self._publish("capability", force=True)
        return True

    def note_permission(self, state: str) -> bool:
        """Record what the operating system answered, as reported by the app.

        The capture happens in a browser window this process never sees, so
        ``denied`` is not observable here and has to be told. An unrecognised
        word is ignored rather than stored: a state machine that accepts a value
        it has no rendering for produces a phase nobody has a sentence for.
        """
        normalised = state if state in PERMISSIONS else None
        if normalised is None or normalised == self.permission:
            return False
        self.permission = normalised
        if normalised == "enabled":
            # A granted permission is not an error, and leaving the last one up
            # would keep the phase on `error` for a capture that worked.
            self.error_message = None
        self._publish("permission", force=True)
        return True

    def note_error(self, message: str) -> bool:
        """Record a capture that failed for a reason other than permission.

        An empty message means *no failure is being reported*, and clears one.
        It is spelled out rather than left to a caller guessing, because
        ``error`` is the first rung of :meth:`phase`: a failure that could never
        be withdrawn would keep masking every later success, so one bad capture
        would report as ``error`` for the rest of the session.
        """
        text = str(message or "").strip()[:ERROR_MESSAGE_CHARS]
        if not text:
            if self.error_message is None:
                return False
            self.error_message = None
            self._publish("error", force=True)
            return True
        if text == self.error_message:
            return False
        self.error_message = text
        self._publish("error", force=True)
        return True

    def note_report(self, payload: Any) -> bool:
        """Absorb one state report from the app that owns the capture.

        Deliberately narrow: bytes, a JSON string, or an already-decoded mapping
        holding exactly one ``state`` word. Anything else -- including a payload
        that parses -- changes nothing, because this is a path an unexpected
        packet can reach and the worst it can do must be "nothing happened".
        """
        data = _decode(payload)
        if not isinstance(data, dict):
            return False
        state = str(data.get("state", "") or "")
        if state == "error":
            # The app reporting a failure with no words still reported one, so
            # the fallback lives here rather than in `note_error` -- there an
            # empty message is the withdrawal of a previous failure.
            described = str(data.get("message", "") or "").strip() or "the capture failed."
            return self.note_error(described)
        if state in PERMISSIONS:
            return self.note_permission(state)
        return False

    def note_source(self, source: str | None) -> bool:
        """Record which track is feeding frames. ``None`` means none is.

        Returns whether anything changed, because publishing "camera, camera,
        camera" on every subscription event is noise and the UI needs only the
        transitions.
        """
        normalised = source if source in ("camera", "screen") else None
        if normalised == self.source and bool(normalised) == self.sharing:
            return False
        had = self.sharing
        self.source = normalised
        self.sharing = normalised is not None
        # A different track is a different stream, and the previous one's
        # liveness says nothing about it. Clearing this is what lets a share
        # start at `capturing` and become ready on its own first frame, instead
        # of opening as `vision_degraded` because the *last* share went quiet.
        self.last_delivery_at = None
        if not self.sharing:
            # A stopped share keeps its last frame on record -- the model still
            # has those frames in context -- so no new frame is recorded, and
            # the age of the last one keeps growing from here. `sharing` is what
            # says the view is no longer live; the age is what says how old the
            # picture everyone is still holding has become.
            reason = "stopped" if had else "idle"
        else:
            reason = "started" if not had else "source"
        self._publish(reason, force=True)
        return True

    def note_frame(
        self,
        *,
        seq: int,
        width: int = 0,
        height: int = 0,
        changed: bool = True,
        age_ms: float | None = None,
    ) -> bool:
        """Record one accepted frame. Returns whether it was published now."""
        self.seq = int(seq)
        self.width = int(width)
        self.height = int(height)
        self.last_frame_at = self._clock()
        self.last_frame_wall = self._wall()
        self.last_delivery_at = self.last_frame_at
        if changed:
            self.last_change_at = self.last_frame_at
        # Heartbeat-throttled: a frame record is a statement about freshness, so
        # it is worth publishing regularly while sharing, and not worth flooding
        # when the stream is fast.
        return self._publish(
            "frame",
            age_ms=age_ms,
            force=False,
        )

    def note_drop(self, reason: str) -> None:
        """Count a frame that was refused. ``stale`` or ``unchanged``."""
        if reason == "stale":
            self.dropped_stale += 1
            # Arriving too late to trust is not liveness. Leaving the delivery
            # clock alone here is what lets a stream that is technically flowing
            # but chronically late be reported as `vision_degraded` rather than
            # as healthy on the strength of frames nobody could use.
        elif reason == "unchanged":
            self.dropped_unchanged += 1
            # The screen did not move, which means a frame *did* arrive. This is
            # the whole reason a stable screen stays `vision_ready`: the sampler
            # backs off to ten seconds and every one of those samples refreshes
            # the delivery clock, so an idle share is reported as quiet, not as
            # broken.
            now = self._clock()
            if self.last_delivery_at is None or now > self.last_delivery_at:
                self.last_delivery_at = now

    # -- reads --------------------------------------------------------------

    def age_ms(self) -> float | None:
        """Milliseconds since the newest accepted frame, or ``None``."""
        if self.last_frame_at is None:
            return None
        return round((self._clock() - self.last_frame_at) * 1000, 1)

    def stale_ms(self) -> float | None:
        """Milliseconds since the picture last *changed*, or ``None``.

        Distinct from :meth:`age_ms` on purpose: a screen that has not moved for
        a minute is not a stale observation, it is an accurate one. What would be
        wrong is describing it as newly seen.
        """
        if self.last_change_at is None:
            return None
        return round((self._clock() - self.last_change_at) * 1000, 1)

    def delivery_age_ms(self) -> float | None:
        """Milliseconds since the last frame the sampler *saw*, or ``None``.

        The liveness of the stream rather than the age of the picture. A screen
        nobody touches stops producing new pictures and keeps producing frames,
        so :meth:`age_ms` grows while this stays small -- and that difference is
        the whole of "she is looking at a still" versus "her video has frozen".
        """
        if self.last_delivery_at is None:
            return None
        return round((self._clock() - self.last_delivery_at) * 1000, 1)

    def vision_ready(self) -> bool:
        """Whether a current picture is arriving *right now*.

        All four preconditions are necessary, and none is implied by the others:
        a share with no model to receive it, a model with no share to read, a
        share nobody is feeding, and a feed whose frames all arrive too late to
        trust are four different failures that all look like "sharing" from the
        outside.
        """
        if self.can_see is False:
            return False
        if not self.sharing or self.last_delivery_at is None:
            return False
        return (self._clock() - self.last_delivery_at) <= READY_INTERVAL

    def phase(self) -> str:
        """One word for what sight is doing, most specific answer first.

        Read top to bottom as a ladder of exclusions: each line is reached only
        because the lines above it were ruled out. The first three are facts
        about things this process cannot observe for itself -- a failed capture,
        a model without eyes, an operating system that said no -- and they win
        over everything about frames, because none of the frame questions has an
        answer when they hold.
        """
        if self.error_message:
            return "error"
        if self.can_see is False:
            return "unavailable"
        if self.permission == "denied":
            return "denied"
        if self.permission == "requesting_permission":
            return "requesting_permission"
        if not self.sharing:
            if self.seq == 0:
                # Nothing has ever been shared this session, which is a
                # different statement from "the share ended": one is an unset
                # capability and the other is a picture she already has.
                return "not_requested" if self.permission == "not_requested" else "enabled"
            return "stopped"
        if self.last_delivery_at is None:
            # A track is subscribed and nothing has come through it yet. If a
            # share ran earlier in this session its frames are still in context,
            # which is what `frame_available` says and `capturing` would hide.
            return "frame_available" if self.seq else "capturing"
        if self.vision_ready():
            return "vision_ready"
        return "vision_degraded"

    def vision_facts(self) -> dict[str, Any]:
        """The stable facts about sight, as plain JSON-compatible values.

        Nothing here moves between two events: no ages, no counters, no
        timestamps. That is the property that makes the block safe to push
        *instead of* streaming -- a number written into the instructions once is
        wrong for every later turn, while these hold until something actually
        happens.
        """
        return {
            "can_see": self.can_see,
            "permission": self.permission,
            "screen_share": self.sharing,
            "source": self.source,
            "frame_available": self.seq > 0,
            "vision_ready": self.vision_ready(),
            "phase": self.phase(),
        }

    def vision_block(self) -> str:
        """The instruction block: constant prose, then one line of current fact.

        Compared whole as the dedup key for pushing, so two pushes only happen
        when something in it differs -- which is what keeps a re-send of the
        entire instruction string down to the moments that earn one.
        """
        facts = json.dumps(self.vision_facts(), separators=(", ", ": "))
        return f"\n\n{VISION_NOTE}\n\n[vision] {facts}\n"

    def snapshot(self) -> dict[str, Any]:
        """A publish-shaped view of the current state. No pixels, no content."""
        return {
            "sharing": self.sharing,
            "source": self.source,
            "frame_seq": self.seq,
            "frame_age_ms": self.age_ms(),
            "content_age_ms": self.stale_ms(),
            "width": self.width,
            "height": self.height,
            "dropped_stale": self.dropped_stale,
            "dropped_unchanged": self.dropped_unchanged,
            # What a consumer reads instead of combining the rest: one ladder,
            # two readers, and the model gets the same value -- so the app
            # saying "Lumine can see this" and her instructions saying
            # `vision_ready` cannot disagree, because they are one field.
            "phase": self.phase(),
            "vision_ready": self.vision_ready(),
            "frame_available": self.seq > 0,
            "can_see": self.can_see,
            "permission": self.permission,
            "error": self.error_message,
        }

    # -- publishing ---------------------------------------------------------

    def mark_published(self, reason: str = "idle") -> bool:
        """Say that this state has just gone out, so the tick does not repeat it.

        The baseline record at session start used to be a bare ``emit`` beside
        the state that owns emission. Two writers for one channel is how a
        "has this already been said?" question ends up unanswerable.
        """
        return self._publish(reason, force=True)

    def pending_phase_change(self) -> bool:
        """Whether sight has moved to a new phase since anything was published.

        The one gap an event-driven state has is a transition with no event,
        and there is exactly one: frames going quiet. Every other phase change
        is caused by something this object was told about, which already
        published -- so comparing against the *published* phase rather than
        against a private copy is what stops a share start from being announced
        twice, once by the event and again by the tick that follows it.
        """
        return self.phase() != self._published_phase

    def _publish(self, reason: str, *, force: bool, age_ms: float | None = None) -> bool:
        if self._emit is None:
            return False
        now = self._clock()
        if not force and (now - self._last_emit_at) < self._emit_interval:
            # Returned before the phase is recorded on purpose: nothing left
            # this process, so nothing has been published to compare against.
            return False
        self._last_emit_at = now
        payload = self.snapshot()
        if age_ms is not None:
            payload["frame_age_ms"] = age_ms
        payload["reason"] = reason
        self._published_phase = payload["phase"]
        try:
            self._emit("visual", **payload)
        except Exception:  # noqa: BLE001 - telemetry must never break a session
            return False
        return True
