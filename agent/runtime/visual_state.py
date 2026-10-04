"""What Lumine can currently see, and how old that is.

A frame that reaches the model is a photograph, not a video: the model has no way
to know it was taken four seconds ago or ten minutes ago, and a description of an
old frame delivered as "here is what is on screen" is the exact failure this
module exists to make impossible to hide.

It tracks three things and publishes them:

* **Which source** -- camera, screen, or nothing. Fed from the room's own track
  events, so "the share stopped" is observed rather than inferred.
* **How many frames** have been accepted, in order, and when the newest arrived.
* **How many were dropped** for being stale or unchanged, which is what makes the
  difference between "Lumine is looking at a live view" and "Lumine is looking at
  one screenshot from before you switched tabs".

Nothing here inspects pixels beyond their dimensions, and nothing here ever
publishes image data or frame content -- only ages, counts and identifiers.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

#: How often a frame heartbeat may be published. Frames arrive at 0.3-1 fps, so
#: this mostly does nothing; it exists so a bursty stream cannot flood stdout.
DEFAULT_EMIT_INTERVAL = 1.0

#: Reasons a ``visual`` record is published. Read by the UI as state, not as log.
REASONS = ("started", "source", "stopped", "frame", "idle")


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
        self.dropped_stale = 0
        self.dropped_unchanged = 0
        self.width = 0
        self.height = 0

    # -- inputs -------------------------------------------------------------

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
        elif reason == "unchanged":
            self.dropped_unchanged += 1

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
        }

    # -- publishing ---------------------------------------------------------

    def _publish(self, reason: str, *, force: bool, age_ms: float | None = None) -> bool:
        if self._emit is None:
            return False
        now = self._clock()
        if not force and (now - self._last_emit_at) < self._emit_interval:
            return False
        self._last_emit_at = now
        payload = self.snapshot()
        if age_ms is not None:
            payload["frame_age_ms"] = age_ms
        payload["reason"] = reason
        try:
            self._emit("visual", **payload)
        except Exception:  # noqa: BLE001 - telemetry must never break a session
            return False
        return True
