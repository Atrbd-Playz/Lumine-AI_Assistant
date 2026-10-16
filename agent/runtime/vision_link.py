"""The last inch between our gate and the provider -- and the proof it got there.

:mod:`agent.runtime.frame_sampler` decides *which* frame deserves to reach the
model. This module owns the three things only something standing at the provider
boundary can know, and each of them exists because the obvious signal lies.

**"``vision_ready`` is true" is not "the model received an image."** It is a
statement about capability and permission, both of which were settled before the
first frame arrived. The moment a picture becomes a request is one line of the
Google plugin -- ``push_video`` encoding a JPEG and handing a
``LiveClientRealtimeInput(video=Blob(...))`` to the send channel -- and it is
the last place at which *"attached"* is a fact rather than an inference. Every
video event therefore emits a ``vision_request`` record carrying the sequence,
the age, the provider, the model and ``image_attached``, so "did she actually
look?" is answerable from a log instead of argued from a badge. Never the
content: bytes are counted, never recorded.

**Nothing in the library prunes the send channel.** ``_msg_ch`` is a ``Chan``
with ``maxsize=0`` -- an unbounded deque -- and its consumer is a coroutine that
awaits a network round trip per message while audio keeps arriving beside it. A
stall therefore accumulates one encoded JPEG per frame, and when the stall
clears they are delivered in a burst, oldest first: the model is handed ten
screenshots of a screen it is now describing as current. The fix is *latest
wins*, and it is done by **replacement rather than removal** -- see
:meth:`VisionLink._replace_pending` for why deleting from that deque can kill
the consumer outright, which is a strictly worse failure than a stale frame.

**A frame can age after it has been chosen.** The sampler's gates run at
arrival; this is the only code that runs at enqueue, and it is therefore the
only place that can see the encode, the loop stall and the queue depth that all
happened *after* the decision. :meth:`VisionLink._before_video` refuses on age
and on a generation that moved, so the picture handed over is the newest usable
one rather than the one that was newest a moment ago.

Nothing here touches pixels, and nothing here can fail a frame: every path that
would raise has already counted, and the frame is either sent or not by a
deliberate decision rather than by an exception.

.. _vision-link-sdk-dependency:

**The version dependency, stated plainly.** This module stands on two private
names in the Google plugin: ``RealtimeSession._send_client_event`` (the method
wrapped) and ``_RealtimeOptions.image_encode_options`` (the field
:meth:`VisionLink.sync_profile` writes). Neither is public API, and the only
reason either can be relied on at all is that ``push_video`` reaches the first
after encoding the frame, which is what makes *"attached"* a fact rather than an
inference. Measured against ``livekit-agents`` **1.8.3** and
``livekit-plugins-google`` **1.8.2**, whose ``push_video`` is::

    encoded_data = images.encode(frame, self._opts.image_encode_options or ...)
    realtime_input = types.LiveClientRealtimeInput(video=types.Blob(...))
    self._send_client_event(realtime_input)

An upgrade that renames either name will not break the build and will not break
the session: images keep being sent, and only the *observation* of them stops,
which is the worst possible failure -- a log that quietly stops recording the
thing it exists to record. :class:`agent.tests.test_vision_scheduling.SdkHookTests`
is the guard, and it asserts the call in ``push_video``'s source rather than the
attribute's existence, because a method can still exist after it has stopped
being called.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .frame_sampler import IMAGE_PROFILES, MAX_PENDING_AGE, REQUEST_PRIORITY


@dataclass(frozen=True)
class ImageProfile:
    """How one frame is prepared for one kind of question.

    This is the *image preparation layer* -- resize and compress, nothing else.
    The numbers for ``general`` are the plugin's own catalogue defaults
    (1024x1024, quality 75) reproduced rather than referenced, so that
    "we changed nothing for a normal turn" is a comparison a reader can make
    without opening the SDK.

    ``detail`` exists because a 16:9 screen aspect-fit into a square loses
    almost half its height: at 1024 the whole desktop is 1024x576, which is
    legible for "what window is this" and not for the body copy of a dialog.
    600 more pixels of headroom is a real answer to reading small text, and it
    is paid only by whoever names the profile.

    **Crop is deliberately absent.** A crop needs a region, and the region is
    exactly what the model has not located yet; guessing one before it asks
    risks removing the very pixels the question was about. Wider, sharper and
    rarer is the honest version of "targeted crop" until something can name a
    target.
    """

    name: str
    width: int
    height: int
    quality: int


PROFILES: dict[str, ImageProfile] = {
    "general": ImageProfile("general", width=1024, height=1024, quality=75),
    "detail": ImageProfile("detail", width=1600, height=1600, quality=85),
}

assert set(PROFILES) == set(IMAGE_PROFILES), "the vocabulary and its encoder must agree"


def _is_video(msg: Any) -> bool:
    """Is this queued message a picture? Audio and text never count.

    Deliberately structural rather than ``isinstance``: importing the plugin's
    ``types`` here would make this module unimportable in a process that has the
    agent but not the Google plugin, and the question is answerable from the
    message's own shape anyway. An *empty* message -- what a swept slot becomes
    -- has no video, so it does not count as one and cannot re-arm the sweep.
    """
    if getattr(msg, "audio", None) is not None:
        return False
    if getattr(msg, "text", None) is not None:
        return False
    if getattr(msg, "activity_start", None) is not None:
        return False
    if getattr(msg, "activity_end", None) is not None:
        return False
    return getattr(msg, "video", None) is not None


class VisionLink:
    """The provider-boundary observer, coalescer and encoder profile.

    One per room. It reaches the realtime session lazily -- ``session ->
    _activity -> _rt_session`` does not exist until the job is running, and a
    link installed too early would wrap nothing -- which is why
    :meth:`install` is called from the sampler on every arriving frame rather
    than once at session start. It is idempotent per session object and costs an
    identity check once it has landed.
    """

    def __init__(
        self,
        *,
        provider: str = "",
        model: str = "",
        emit: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = str(provider or "")
        self.model = str(model or "")
        self._emit = emit
        self._clock = clock

        self._sampler: Any = None
        self._rt: Any = None
        self._profile = "general"
        self._queued_at: float | None = None

        self.installed = 0
        #: Images that went into the channel. The number `vision_ready` never
        #: gave anybody.
        self.attached = 0
        #: Refused at the boundary: too old, or superseded by a moved screen.
        self.dropped = 0
        #: Older images discarded by replacing their slot with a newer one.
        self.replaced = 0
        #: Pending images blanked because they sat past `MAX_PENDING_AGE`.
        self.swept = 0
        #: Records actually written. Fewer than `attached + dropped` means the
        #: telemetry channel itself is failing, which is worth seeing.
        self.emitted = 0
        self.last_record: dict[str, Any] | None = None

    # -- wiring -------------------------------------------------------------

    def bind(self, sampler: Any) -> None:
        """Give the link the gate's counters, and re-apply the profile."""
        self._sampler = sampler
        self._apply_profile()

    def set_profile(self, name: str) -> bool:
        """Prepare the next frame for this kind of question.

        Takes effect on the *next* encode: the plugin reads
        ``_opts.image_encode_options`` per frame rather than once at connect, so
        this is a live switch rather than a restart.
        """
        if name not in PROFILES:
            name = "general"
        self._profile = name
        return self._apply_profile()

    @property
    def profile(self) -> str:
        return self._profile

    def _apply_profile(self) -> bool:
        """Write this profile into the plugin's per-frame encode options.

        ## Why it writes rather than asks

        ``push_video`` reads ``self._opts.image_encode_options`` on *every*
        frame, so the encoder's configuration is a plain mutable field rather
        than something fixed at connect: switching profile is an assignment, not
        a reconnect, and the very next frame is already affected.

        ## Why it imports lazily

        The Google plugin is an optional dependency -- a ``legacy_cascade``
        install has no reason to carry it, and a module that fails to import
        because a profile it does not use is absent would take the whole vision
        scheduler down with it. The import therefore happens here, once, inside
        a guard, and a machine without the plugin simply loses the ability to
        *sharpen* a frame while every other guarantee still holds.

        Returns whether anything was written. ``False`` with no telemetry is the
        normal answer for a session that does not exist yet: :meth:`install`
        calls this too, and it runs on every arriving frame until the realtime
        session is there.
        """
        rt = self._rt
        if rt is None:
            return False
        opts = getattr(rt, "_opts", None)
        if opts is None:
            return False
        spec = PROFILES.get(self._profile, PROFILES["general"])
        try:
            from livekit.agents.utils import images
        except Exception:  # noqa: BLE001 - no plugin, no sharpening, no failure
            return False
        try:
            opts.image_encode_options = images.EncodeOptions(
                format="JPEG",
                quality=spec.quality,
                resize_options=images.ResizeOptions(
                    width=spec.width, height=spec.height, strategy="scale_aspect_fit"
                ),
            )
        except Exception:  # noqa: BLE001 - a read-only options object costs sharpness only
            return False
        return True

    def install(self, session: Any) -> bool:
        """Wrap the realtime session's send path. Idempotent per session.

        Returns whether anything was wrapped. A session with no realtime model
        -- the cascade, which never receives a frame anyway -- answers ``False``
        forever and loses nothing but the delivery telemetry.
        """
        activity = getattr(session, "_activity", None)
        rt = getattr(activity, "_rt_session", None)
        if rt is None:
            return False

        current = getattr(rt, "_send_client_event", None)
        if getattr(current, "__lumine_link__", None) is self:
            # Already ours. Checked by mark rather than by `self._rt is rt`,
            # which would re-wrap the moment a *second* session object appeared
            # and hand the first one two observers counting the same message.
            if self._rt is not rt:
                self._rt = rt
                self._apply_profile()
            return False
        if not callable(current):
            return False

        link = self

        def _send(event: Any) -> Any:
            if _is_video(event):
                if not link._before_video(event):
                    return None
            return current(event)

        _send.__lumine_link__ = link  # type: ignore[attr-defined]
        try:
            rt._send_client_event = _send
        except Exception:  # noqa: BLE001 - a read-only session costs telemetry only
            return False
        self._rt = rt
        self.installed += 1
        self._apply_profile()
        return True

    def attached_rt(self) -> Any:
        """The realtime session being observed, if it exists yet."""
        return self._rt

    def sync_profile(self) -> str:
        """Point the encoder at the profile the *pending* request asked for.

        ## Why this runs before the frame, not at the frame

        The obvious place to choose an image profile is where the image is
        handed over -- and it is too late by then. ``push_video`` encodes the
        frame *first* and only then calls ``_send_client_event``, so a decision
        made inside :meth:`_before_video` would describe the picture that was
        already compressed rather than the one about to be. The slot this has to
        sit in is therefore the sampler's per-frame pump, which runs at the top
        of every arriving frame -- before the gate, before the encode.

        ## Why it is the pending request

        The request is the only thing that knows *why* a frame is coming. A
        normal turn asks for ``general``; something that wants small text
        readable asks for ``detail``, and the answer to "which encoder?" is that
        the caller already said. Falling back to ``general`` for a frame with
        nothing pending is what keeps a scheduled frame on the catalogue
        defaults rather than leaving Lumine on the sharper, more expensive
        setting forever after one detail question.

        Returns the profile now in force.
        """
        want = "general"
        sampler = self._sampler
        if sampler is not None:
            try:
                pending = sampler.request_pending()
            except Exception:  # noqa: BLE001 - a broken seam costs one frame's detail
                pending = None
            if pending is not None:
                candidate = str(pending.get("profile") or "general")
                if candidate in PROFILES:
                    want = candidate
        if want != self._profile:
            self._profile = want
            self._apply_profile()
        return self._profile

    # -- the boundary -------------------------------------------------------

    def _before_video(self, event: Any) -> bool:
        """Decide about one encoded image. ``True`` means "append it".

        Runs between ``push_video``'s encode and its ``send_nowait``, which is
        the only instant in which *this* frame's fate can still be changed: the
        gate has already said yes, the bytes already exist, and the queue has
        not been touched yet.
        """
        now = self._clock()
        sampler = self._sampler
        allowed = True
        why = ""
        if sampler is not None:
            try:
                allowed, why = sampler.allows_send(now)
            except Exception:  # noqa: BLE001 - a broken gate must not eat the frame
                allowed, why = True, ""

        if not allowed:
            self.dropped += 1
            self._count_refusal(sampler, why)
            self._publish(
                self._record(event, now, attached=False, why=why, superseded=0)
            )
            return False

        superseded = 0
        if self._replace_pending(event):
            # The newest image is now in the queue at the older one's position.
            # Appending as well would be two frames where there is room for one.
            superseded = 1
            self.replaced += 1
            if sampler is not None:
                try:
                    sampler.superseded += 1
                except Exception:  # noqa: BLE001 - counters are not the frame
                    pass
        self.attached += 1
        self._queued_at = now
        self._publish(
            self._record(event, now, attached=True, why="", superseded=superseded)
        )
        return superseded == 0

    def _count_refusal(self, sampler: Any, why: str) -> None:
        if sampler is None:
            return
        try:
            if why == "expired":
                sampler.dropped_expired += 1
            elif why == "superseded":
                sampler.dropped_superseded += 1
        except Exception:  # noqa: BLE001 - counters are not the frame
            pass

    def _replace_pending(self, event: Any) -> bool:
        """Put this image where the waiting one is, instead of behind it.

        **Replacement, not removal, and the difference is load-bearing.**
        ``Chan.send_nowait`` appends and then wakes a parked getter *by removing
        it from ``_gets``*. Between that wake-up and the getter's own
        ``recv_nowait()`` there is a window -- it has not run yet, it is only
        scheduled -- in which ``_queue`` may be non-empty and ``_gets`` empty.
        Clearing the queue in that window makes the getter's ``recv_nowait``
        raise ``ChanEmpty``, which propagates out of ``_send_task``'s
        ``async for`` and **kills the channel permanently**: from then on
        nothing, audio included, is ever sent again. That is a far worse failure
        than one stale frame, and no ordering of checks can rule the window out
        from inside a synchronous function.

        Writing over a slot changes no length, wakes nobody, and cannot raise.
        The getter still pops exactly one item -- it simply pops the newest
        image instead of the oldest, which is the entire point.
        """
        queue = self._queue()
        if queue is None:
            return False
        try:
            for index, msg in enumerate(queue):
                if _is_video(msg):
                    queue[index] = event
                    return True
        except Exception:  # noqa: BLE001 - a deque we cannot write is a deque we leave alone
            return False
        return False

    def sweep(self, now: float | None = None) -> int:
        """Blank a waiting image that has sat past :data:`MAX_PENDING_AGE`.

        The timer for an image *already* queued. Without it, one video event
        sitting in a channel whose consumer is blocked would eventually be
        delivered after sitting there -- which is exactly what the age gate was
        written to prevent, and exactly the case the gate at enqueue cannot see,
        because at enqueue the image was fresh.

        Called from the sampler on every arriving frame, which is the only
        heartbeat guaranteed while the channel is wedged (if frames had stopped,
        nothing would be arriving to replace it either). The blank is the same
        trick as replacement: an empty message of the same type passes straight
        through ``_send_task``'s branch chain without matching any of them, so
        it costs nothing and says nothing.

        Returns how many were blanked.
        """
        if now is None:
            now = self._clock()
        if self._queued_at is None:
            return 0
        queue = self._queue()
        if queue is None:
            self._queued_at = None
            return 0
        try:
            slots = [i for i, m in enumerate(queue) if _is_video(m)]
        except Exception:  # noqa: BLE001
            self._queued_at = None
            return 0
        if not slots:
            # Already popped by the consumer: it is in flight, and there is
            # nothing here to age. Clearing the hint stops us re-checking a
            # channel that has moved on.
            self._queued_at = None
            return 0
        if (now - self._queued_at) <= MAX_PENDING_AGE:
            return 0

        blanked = 0
        for index in slots:
            try:
                queue[index] = type(queue[index])()
            except Exception:  # noqa: BLE001 - unconstructible: leave it, and count it below
                continue
            blanked += 1
        if not blanked:
            return 0
        self.swept += blanked
        self._queued_at = None
        sampler = self._sampler
        if sampler is not None:
            try:
                sampler.dropped_expired += blanked
            except Exception:  # noqa: BLE001
                pass
        self._publish(
            self._record(None, now, attached=False, why="expired_pending", superseded=0)
        )
        return blanked

    def _queue(self) -> Any | None:
        """The plugin's pending-message deque, or ``None`` if shape moved on.

        Every hop is looked up rather than cached: the plugin recreates
        ``_msg_ch`` on a reconnect, and a cached reference would then be
        pruning a channel nothing reads -- silently, because nothing would fail.
        """
        rt = self._rt
        if rt is None:
            return None
        channel = getattr(rt, "_msg_ch", None)
        if channel is None:
            return None
        queue = getattr(channel, "_queue", None)
        if queue is None or not hasattr(queue, "__setitem__"):
            return None
        return queue

    # -- the record ---------------------------------------------------------

    def _record(
        self,
        event: Any,
        now: float,
        *,
        attached: bool,
        why: str,
        superseded: int,
    ) -> dict[str, Any]:
        """Every field §15 asks for, and nothing that touches an image."""
        sampler = self._sampler
        frame_seq: int | None = None
        frame_age: float | None = None
        content_age: float | None = None
        dropped_stale: int | None = None
        dropped_unchanged: int | None = None
        accepted: int | None = None
        dropped_budget: int | None = None
        dropped_expired: int | None = None
        dropped_superseded: int | None = None
        result_generation: int | None = None
        current_generation: int | None = None
        reason = "scheduled"
        profile = self._profile
        requested_at: float | None = None

        if sampler is not None:
            try:
                latest = sampler.latest()
                if latest is not None:
                    frame_seq = latest.seq
                    result_generation = latest.generation
                    width_height = (latest.width, latest.height)
                else:
                    width_height = None
                frame_age = sampler.picture_age_ms(now)
                content_age = sampler.content_age_ms(now)
                dropped_stale = sampler.dropped_stale
                dropped_unchanged = sampler.dropped_unchanged
                dropped_budget = getattr(sampler, "dropped_budget", 0)
                dropped_expired = getattr(sampler, "dropped_expired", 0)
                dropped_superseded = getattr(sampler, "dropped_superseded", 0)
                accepted = getattr(sampler, "accepted", 0)
                current_generation = sampler.generation
                delivering = getattr(sampler, "_delivering", None) or {}
                reason = str(delivering.get("reason") or "scheduled")
                requested_at = delivering.get("requested_at")
            except Exception:  # noqa: BLE001 - a record must never cost a frame
                width_height = None
        else:
            width_height = None

        image_bytes = 0
        if attached and event is not None:
            try:
                blob = getattr(event, "video", None)
                data = getattr(blob, "data", None)
                image_bytes = len(data) if data is not None else 0
            except Exception:  # noqa: BLE001
                image_bytes = 0

        duration = None
        if requested_at is not None:
            duration = max(0.0, (now - requested_at) * 1000.0)

        record: dict[str, Any] = {
            "type": "vision_request",
            "frame_sequence": frame_seq,
            "frame_age_ms": None if frame_age is None else round(frame_age, 1),
            "content_age_ms": None if content_age is None else round(content_age, 1),
            "dropped_stale": dropped_stale,
            "dropped_unchanged": dropped_unchanged,
            # The rest of the ledger, so one record answers "captured, discarded
            # or sent?" without a second telemetry stream. Counters are
            # cumulative and read at the moment of delivery, which is what makes
            # conservation checkable: seen = accepted + every kind of drop.
            "accepted": accepted,
            "dropped_budget": dropped_budget,
            "dropped_expired": dropped_expired,
            "dropped_superseded": dropped_superseded,
            "request_reason": reason,
            "request_priority": REQUEST_PRIORITY.get(reason, "normal"),
            "provider": self.provider,
            "model": self.model,
            "image_attached": bool(attached),
            "image_bytes": image_bytes,
            "request_duration_ms": None if duration is None else round(duration, 1),
            "result_generation": result_generation,
            "current_generation": current_generation,
            "why": why,
            "profile": profile,
            "superseded_pending": superseded,
            "queued_video": self._pending_count(),
        }
        if width_height is not None:
            record["width"], record["height"] = width_height
        return record

    def _pending_count(self) -> int:
        queue = self._queue()
        if queue is None:
            return 0
        try:
            return sum(1 for m in queue if _is_video(m))
        except Exception:  # noqa: BLE001
            return 0

    def _publish(self, record: dict[str, Any]) -> None:
        """Write one `vision_request`. A telemetry write is never load-bearing.

        The worker's stdout is a pipe owned by the desktop app, and a write to a
        dead one raises `OSError` -- this codebase has already lost a whole
        session to an unguarded `print`. The emit is therefore best-effort, and
        the failure is *counted* rather than raised, so "no records" is visible
        from `stats()` instead of looking like "no images".
        """
        self.last_record = record
        emit = self._emit
        if emit is None:
            return
        try:
            emit("vision_request", **record)
            self.emitted += 1
        except Exception:  # noqa: BLE001 - telemetry must not end a frame
            pass

    # -- diagnostics --------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Counters only, for tests and for ``perf_report``."""
        return {
            "installed": self.installed,
            "attached": self.attached,
            "dropped": self.dropped,
            "replaced": self.replaced,
            "swept": self.swept,
            "emitted": self.emitted,
            "profile": self._profile,
            "provider": self.provider,
            "model": self.model,
            "pending_video": self._pending_count(),
        }
