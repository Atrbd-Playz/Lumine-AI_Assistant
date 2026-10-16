"""A video sampler that refuses to hand the model a frame it cannot vouch for.

The stock ``VoiceActivityVideoSampler`` answers one question -- "has enough time
passed?" -- and that is exactly why a session can describe a picture from before
you switched windows. It has no idea how the frame reached it, and the path it
came down has two properties that make "enough time passed" a lie:

* **The input queue is unbounded.** ``room_io`` reads decoded frames off a
  ``Chan`` with ``maxsize=0``; when the event loop stalls -- a tool doing
  synchronous work, a slow encode -- frames collect in the deque and are handed
  over afterwards. A frame consumed now may have been captured seconds ago.
* **The clock it reads is the clock at consumption**, not at capture. So the
  frame that arrives right after a stall looks exactly as fresh as one that
  arrives right after an idle wait, although one is current and the other is
  queued history.

There is no capture timestamp to consult: on the installed ``livekit-rtc``,
``VideoFrame`` exposes only ``width``, ``height``, ``type`` and ``data``. So age
has to be *inferred* from delivery behaviour, which is what this class does:

* frames delivered back-to-back (a gap far below the stream's own cadence) were
  queued, not captured -- they are dropped while the drain lasts;
* a long pause followed by a frame means the loop was blocked while frames could
  have piled up -- the first frame after that pause is dropped, and the drain is
  not considered finished until delivery cadence looks like real arrival again.
  How long is "long" is measured against the rate this source has actually been
  arriving at, so a slow-but-healthy stream is never mistaken for a stall.

Both rules fail in the safe direction. A dropped frame costs at most one
interval; a queued frame presented as current is a wrong answer about what the
user is looking at right now.

On top of freshness: an fps budget (unchanged from the library defaults, since
Gemini tokenizes every frame), a content hash so an unchanged screen stops
burning tokens, and :meth:`FreshnessVideoSampler.request_fresh` so a turn can
pull a frame immediately instead of waiting out a 3.3 second silent interval.

Two things sit *beside* those, and neither changes how often a frame is sent:

* **A slot, not a buffer.** :class:`LatestFrame` holds the one frame Lumine
  would show somebody who asked "what is this?" now -- sequence, generation and
  arrival time on the same object, so an age can never describe the average of a
  queue. Frames arriving while the model is busy replace it.
* **A generation.** ``generation`` moves whenever the *observed* screen changes,
  including while the budget is refusing to sample; ``picture_generation`` is
  what the model's newest picture carried. The two being equal is the whole of
  :meth:`FreshnessVideoSampler.picture_is_current`, which is what an act
  consults before it trusts a screenshot. The detector runs ahead of the budget
  deliberately: "may we spend a frame on this?" and "did the screen move?" are
  different questions, and answering the first one last is how a picture two
  window-switches old keeps claiming to be current.

Delivery -- whether the image actually reached the provider, and whether only the
newest one was ever waiting for it -- belongs to
:mod:`agent.runtime.vision_link`.
"""

from __future__ import annotations

import time
import weakref
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: A delivery gap below this means the consumer did not wait for a frame to be
#: captured -- it drained one that was already queued. 12ms sits below the
#: fastest sane source interval (60fps = 16.7ms) and well above a deque pop.
MIN_ARRIVAL_GAP = 0.012

#: A gap above this is a stall *for a stream we have not measured yet*. It is
#: the floor, not the whole rule -- see :meth:`FreshnessVideoSampler.stall_threshold`.
STALE_SUSPECT_GAP = 0.25

#: How many observed intervals a silence has to exceed before it counts as a
#: stall. Judged against the source's own rate so a slow source is not measured
#: against a number chosen for a fast one.
STALL_INTERVALS = 3.0

#: The longest silence that is still taught to the cadence, in seconds. Below
#: 0.33 fps a "video" source is delivering nothing the fps budget would have
#: sent anyway, so there is no legitimate arrival interval that long -- which
#: makes it the line between "this camera is slow" and "the loop was blocked".
#: This bound is what lets a 3fps source be *learned* while a 10s blockage is
#: still refused: without it, a source slower than 4fps is starved by a
#: threshold the cadence was never allowed to correct, because the cadence was
#: only ever updated by gaps that threshold had already passed.
LEARNABLE_GAP = 3.0

#: Cadence used before enough arrivals exist to estimate one. 15fps matches the
#: capture rate the frontend asks for.
DEFAULT_CADENCE = 1.0 / 15.0

#: How far the sampling interval stretches per consecutive unchanged sample.
#: Geometric, so the cost of an idle screen falls away quickly instead of
#: linearly: 2x, 4x, 8x, 16x...
QUIET_BACKOFF = 2.0

#: The floor on that stretch, and an honest statement of what it costs: a change
#: on a screen nobody has asked about can go unnoticed for up to this long. Ten
#: seconds is the compromise -- long enough that an untouched screen costs three
#: samples a minute instead of eighteen, short enough that starting to type is
#: noticed while it is still happening.
#:
#: A change is *never* delayed this long when it matters, because a turn starting
#: calls :meth:`request_fresh`, which bypasses the budget entirely. The delay only
#: applies to a screen changing with nobody in the conversation.
MAX_QUIET_INTERVAL = 10.0

#: How long a frame that has already been *chosen* may wait before it stops
#: describing the screen and becomes history.
#:
#: This is not the fps budget. The budget decides *when* to look, and a frame
#: sampled after a 3.3s silent interval is still the picture at the moment it
#: arrived -- there is no queue in front of the model. What this bounds is the
#: other direction: once we have decided "this is the frame", the encode and the
#: plugin's own send channel take real time, and if the event loop stalls for
#: several seconds in between, the picture we hand over is several seconds old.
#: Two seconds is chosen against two facts rather than taste: the speaking
#: budget is 1.0s, so anything beyond ~2x of it means something blocked, and it
#: is comfortably under :data:`visual_state.READY_INTERVAL` (15s), so dropping
#: an expired frame never flips a healthy share to ``vision_degraded`` -- a
#: newer frame follows on the next arrival.
#:
#: Dropping is the safe direction. A refusal costs the model a turn's picture; a
#: send costs it a wrong belief about what is on screen, which is the one
#: failure this module exists to prevent.
MAX_PENDING_AGE = 2.0

#: How current the picture must be before Lumine acts on it.
#:
#: Deliberately much stricter than :data:`MAX_PENDING_AGE`. Reading a stale
#: screenshot is a bad answer; *clicking* based on one is a wrong action, and
#: the screen under a cursor moves fastest when something is about to be
#: clicked -- a menu closes, a dialog appears, the window scrolls. Half a second
#: is roughly the human-perceptible boundary for "did that move?", which is
#: exactly the standard an act is held to.
#:
#: This is a *second* gate, not the only one: ``tools/desktop/capture.py``
#: re-reads the accessibility tree before any irreversible act regardless of
#: what any picture says. What this adds is that the model's own reasoning is
#: current, and that "acted on a six-second-old picture" is a number somebody
#: can read rather than a thing that happened silently.
ACTION_MAX_AGE = 0.5

#: Why a frame was asked for. Recorded on the delivery so ``perf_report`` can
#: say *which* kind of request cost what, rather than only that frames went out.
#:
#: ``scheduled`` is not a request at all -- it is the budget deciding on its own.
#: Kept in the same vocabulary so a query never has to distinguish "no reason"
#: from "a reason we forgot to name".
REQUEST_REASONS = ("turn", "question", "action", "step", "scheduled")

#: Reason -> priority. Purely descriptive at the moment -- every reason buys the
#: same single fresh frame -- but it is what makes the telemetry answer "were we
#: late because this was a low-priority turn?" instead of "we were late".
REQUEST_PRIORITY: dict[str, str] = {
    "turn": "normal",
    "question": "high",
    "action": "high",
    "step": "high",
    "scheduled": "low",
}

#: The image-preparation profiles a request may name. The *encoder* that
#: implements them lives in :mod:`agent.runtime.vision_link`; the vocabulary
#: lives here because it is a statement about what the model needs to read, and
#: this module is the one that knows what the request was for.
#:
#: ``general`` is the catalogue default (1024x1024, quality 75) and is what a
#: normal turn gets. ``detail`` is the same frame prepared to survive reading
#: small text -- a dialog's body copy, a status bar, a table of numbers.
#:
#: **Crop is deliberately not built.** A crop needs a region, and the region is
#: exactly what the model has not located yet; inventing one before it asks is a
#: guess about what matters, and a wrong guess removes the very pixels the
#: question was about. Wider, sharper, and shorter-lived is the honest version.
IMAGE_PROFILES = ("general", "detail")


def content_key(frame: Any) -> tuple[int, int, int]:
    """A cheap exact identity for a frame's pixels.

    Kept as the *fallback*, and no longer the primary test. An exact CRC of the
    whole buffer is defeated by a single pixel, and on a shared screen there is
    always a single pixel: a blinking text caret, the mouse pointer, a taskbar
    clock, an animated emoji, a video playing in a tab. Measured on a real
    session, exact dedup dropped **2** frames out of ~200 -- which is to say it
    did nothing at all, while costing a full-buffer CRC on every sampled frame.
    See :func:`luma_signature` for the test that actually holds.
    """
    width = int(getattr(frame, "width", 0) or 0)
    height = int(getattr(frame, "height", 0) or 0)
    data = getattr(frame, "data", None)
    if data is None:
        return (width, height, 0)
    try:
        checksum = zlib.crc32(data)
    except TypeError:  # an exotic buffer type -- pay for the copy once
        checksum = zlib.crc32(bytes(data))
    return (width, height, int(checksum))


#: Grid the luma plane is sampled on. Small on purpose: this is a "did anything
#: meaningful change" test, and a fine grid would put the caret back in charge.
#: 32x18 keeps the aspect of a 16:9 screen and costs ~576 reads per frame instead
#: of a CRC over ~2.9 million bytes.
SIGNATURE_COLS = 32
SIGNATURE_ROWS = 18

#: Two luma samples count as different above this delta (0-255). Wide enough that
#: codec noise and antialiasing on unchanged text do not register, tight enough
#: that a button changing colour does.
LUMA_DELTA = 12

#: Fraction of the grid that must change before a frame counts as new. One cell
#: out of 576 is a blinking caret. A real change -- a dialog opening, a page
#: scrolling, a video starting -- moves far more than 2% of a screen. This is the
#: threshold that makes the dedup work on a screen somebody is actually sharing,
#: rather than on a static image.
CHANGE_RATIO = 0.02


def _byte_view(plane: Any) -> Any:
    """The plane as something whose ``[i]`` is an ``int``, whatever exported it.

    A ``livekit-rtc`` plane is a memoryview whose format is the *byte-ordered*
    struct code ``"<B"``, and CPython only indexes the bare forms -- reading
    ``plane[offset]`` off it raises
    ``NotImplementedError: memoryview: unsupported format <B``. That is not a
    corner: it is **every frame the room delivers**, because they are planar
    (I420/NV12) and plane 0 is exported by the FFI. Measured on a live room, and
    it is why the video forward task died on its first frame.

    ``cast("B")`` reinterprets the same buffer as bare bytes with no copy, which
    is the whole fix. Plain ``bytes`` has no ``cast`` and is already right, so it
    is returned untouched.
    """
    cast = getattr(plane, "cast", None)
    if cast is None:
        return plane
    try:
        return cast("B")
    except Exception:  # noqa: BLE001 - an exotic or non-contiguous buffer
        return plane


def luma_signature(frame: Any) -> tuple[int, ...] | None:
    """A coarse brightness grid for a frame, or ``None`` if it cannot be read.

    Read from the frame's first plane, which for every planar format the SDK
    delivers is luma -- the channel that carries "what is where", at a fraction of
    the cost of touching the pixels. Interleaved formats expose the same buffer as
    plane 0, and a coarse grid over interleaved bytes still tracks gross change,
    so the fallback needs no special case.

    No decode and no FFI round-trip, unlike ``VideoFrame.convert``: this runs on
    every arriving frame, and a per-frame FFI call to answer a question a few
    hundred byte reads can answer is the wrong trade.

    **This function never raises.** It cannot: it runs inside
    ``AgentSession._forward_video_task``, and an exception there does not cost a
    frame, it costs the session's *entire* video path for the rest of the room --
    which is exactly what the ``"<B"`` bug did. Returning ``None`` costs one
    image, because :func:`change_ratio` treats "cannot tell" as "changed".
    """
    try:
        plane = frame.get_plane(0)
    except Exception:  # noqa: BLE001 - an unreadable plane is a fallback, not a failure
        return None
    if plane is None:
        return None
    width = int(getattr(frame, "width", 0) or 0)
    height = int(getattr(frame, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return None
    try:
        view = _byte_view(plane)
        length = len(view)
    except Exception:  # noqa: BLE001 - see the docstring: never raises
        return None

    # Bytes per pixel, derived rather than assumed. This is the difference between
    # a sampler that works and one that silently does nothing: planar luma is one
    # byte per pixel and interleaved RGBA is four, and hard-coding either reads
    # past the end of the plane and returns "cannot tell" for every frame -- which
    # the change test then has to treat as changed, so the dedup never fires.
    pixels = width * height
    if pixels <= 0 or length < pixels:
        return None
    stride = max(1, length // pixels)

    cells: list[int] = []
    try:
        for row in range(SIGNATURE_ROWS):
            y = min(height - 1, (row * height) // SIGNATURE_ROWS)
            for col in range(SIGNATURE_COLS):
                x = min(width - 1, (col * width) // SIGNATURE_COLS)
                # For a planar luma plane this is the pixel's own brightness. For an
                # interleaved buffer it is the first channel of some pixel, which is
                # still a stable sample point -- the grid asks "did this area change",
                # not "what colour is it".
                offset = (y * width + x) * stride
                if offset >= length:
                    return None
                cells.append(int(view[offset]))
    except Exception:  # noqa: BLE001 - see the docstring: never raises
        return None
    return tuple(cells)


def change_ratio(previous: tuple[int, ...] | None, current: tuple[int, ...] | None) -> float:
    """Fraction of the grid that moved, or 1.0 when it cannot be compared.

    Returning 1.0 for "cannot tell" is the safe direction: the frame is treated
    as new and sent, which costs tokens. The alternative -- treating an unreadable
    frame as unchanged -- would let a genuinely changed screen be reported as
    still, which is the one failure this whole module exists to prevent.
    """
    if not previous or not current or len(previous) != len(current):
        return 1.0
    moved = sum(1 for a, b in zip(previous, current) if abs(a - b) > LUMA_DELTA)
    return moved / len(previous)


@dataclass(frozen=True)
class LatestFrame:
    """The one frame Lumine would show somebody who asked "what is this?" now.

    A *slot*, not a list. Frames arriving while the model is busy replace it, so
    there is no scenario in which frames 101-110 are held and frame 101 is the
    next one analysed -- the counter, the picture and the moment it arrived are
    all three the same frame's, which is what makes ``frame_age_ms`` mean
    something rather than describe the average of a queue.

    The frame object itself is the SDK's ``VideoFrame``: ``width``, ``height``,
    ``type``, ``data``. There is no capture timestamp on it (documented in the
    module header), which is why ``received_at`` is ours and is measured at the
    gate rather than inferred afterwards.
    """

    frame: Any
    seq: int
    generation: int
    received_at: float
    width: int
    height: int


class FreshnessVideoSampler:
    """Drop what is stale, budget what is fresh, never send what did not change.

    Call-compatible with the library sampler -- ``(frame, session) -> bool`` --
    and exposing the same ``speaking_fps`` / ``silent_fps`` attributes, because
    the pipeline declares those numbers and a test reads them back.
    """

    def __init__(
        self,
        *,
        speaking_fps: float = 1.0,
        silent_fps: float = 0.3,
        visual: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.speaking_fps = float(speaking_fps)
        self.silent_fps = float(silent_fps)
        self._clock = clock
        self._visual = visual
        self._link: Any = None

        self._last_seen: float | None = None
        self._cadence = DEFAULT_CADENCE
        self._suspect = False
        self._last_sampled: float | None = None
        self._fresh_requested = False
        self._last_key: tuple[int, int, int] | None = None
        self._signature: tuple[int, ...] | None = None
        #: Consecutive samples that found nothing meaningful changed. Drives the
        #: backoff below, and is the honest measure of a screen nobody is
        #: touching -- the case where sending a frame a second is pure cost.
        self._quiet = 0

        #: Monotonic ids for frames that made it through, so an answer can be
        #: tied to "frame 42" rather than to "a picture, sometime".
        self.seq = 0
        self.accepted = 0
        self.dropped_stale = 0
        self.dropped_unchanged = 0
        self.dropped_budget = 0
        #: Refused at the *send* boundary rather than at the gate: a frame we
        #: chose, and which then sat too long to still be the screen. Counted
        #: here rather than in the link so ``stats()`` stays one object.
        self.dropped_expired = 0
        #: Refused at the send boundary because the screen moved after the frame
        #: was chosen -- the generation, not the clock, is what rejected it.
        self.dropped_superseded = 0
        #: Older frames discarded from the plugin's send channel so only the
        #: newest one is ever waiting. See ``vision_link`` -- the channel has
        #: ``maxsize=0`` and nothing in the library prunes it.
        self.superseded = 0

        # -- content generation ------------------------------------------------
        #
        # The cheap local change detector's output, and the whole of "is the
        # picture she holds still the screen?".
        #
        # `generation` moves whenever the *observed* screen changes, including
        # while the fps budget is refusing to sample -- so a change seen during a
        # refusal is not lost, it simply means the picture she holds is already
        # superseded. `picture_generation` is the generation the newest accepted
        # frame carried; the two being equal is the only thing that makes
        # `picture_is_current()` true.
        self.generation = 0
        self.picture_generation = 0
        #: Latest observed grid, which is what the change detector compares
        #: against. Deliberately a *separate* field from `_signature`, which is
        #: the grid the model's current picture had. One field doing both would
        #: either never notice a change the budget refused to sample, or accept
        #: a frame as new when it is not.
        self._observed_signature: tuple[int, ...] | None = None
        self._changed_at: float | None = None

        # -- the slot ---------------------------------------------------------
        self._latest: LatestFrame | None = None

        # -- the request being waited on --------------------------------------
        self._pending_request: dict[str, Any] | None = None
        #: The reason attached to the frame currently being handed over, so the
        #: delivery record can name it. `"scheduled"` when the budget decided.
        self._delivering: dict[str, Any] = {"reason": "scheduled"}

    # -- wiring -------------------------------------------------------------

    def attach(self, visual: Any) -> None:
        """Register the :class:`~agent.runtime.visual_state.VisualState` to report to."""
        self._visual = visual

    def attach_link(self, link: Any) -> None:
        """Register the :class:`~agent.runtime.vision_link.VisionLink`.

        The link is the only thing that knows the image reached the provider, so
        the sampler cannot prove delivery on its own. It is optional: a test, a
        CLI, or a cascade profile that never sends a frame has no link and loses
        nothing but the delivery telemetry.
        """
        self._link = link

    def request_fresh(self, reason: str = "turn", profile: str = "general") -> None:
        """Ask for the next good frame now, regardless of the fps budget.

        Used when a turn starts, when a tool is about to act on the screen, and
        when the model has asked a question that can only be answered by
        looking. It does **not** bypass the freshness gate -- asking for a frame
        is not permission to present a queued one as current.

        The first reason armed wins: it is the one the delivery was actually
        waiting for, and a later, louder reason does not change *why* we are
        waiting. The profile is the only thing that upgrades, because a
        "detail" request and a "general" one are the same wait with different
        preparation.
        """
        if reason not in REQUEST_REASONS:
            reason = "turn"
        if profile not in IMAGE_PROFILES:
            profile = "general"
        self._fresh_requested = True
        if self._pending_request is None:
            self._pending_request = {
                "reason": reason,
                "profile": profile,
                "requested_at": self._clock(),
            }
        else:
            if profile == "detail":
                self._pending_request["profile"] = "detail"

    def request_pending(self) -> dict[str, Any] | None:
        """The request we are waiting to fulfil, if any, or ``None``."""
        return None if self._pending_request is None else dict(self._pending_request)

    # -- decision -----------------------------------------------------------

    def __call__(self, frame: Any, session: Any) -> bool:
        now = self._clock()
        self._pump_link(session, now)
        previous = self._last_seen
        self._last_seen = now

        if previous is None:
            # First frame of the session. Nothing to be behind on.
            gap = None
        else:
            gap = now - previous

        if gap is not None and gap < MIN_ARRIVAL_GAP:
            # Delivered without waiting: this frame was in a queue. A drain is
            # by definition behind, and the newest frame has not arrived yet.
            self.dropped_stale += 1
            self._note_drop("stale")
            return False

        if gap is not None:
            # Judged against the rate this source had *before* this gap -- a gap
            # is not allowed to raise the bar it is being measured against --
            # and then taught to it either way. Both halves matter: learning
            # only from gaps the rule already passed means a source at 3fps,
            # whose every arrival is "too long", could never teach the rule
            # anything and would be starved forever. Anything beyond
            # LEARNABLE_GAP is still refused outright, so one five-second
            # blockage cannot redefine the stream as a five-second stream.
            suspect_gap = gap > self.stall_threshold()
            if gap <= LEARNABLE_GAP:
                self._cadence += 0.2 * (gap - self._cadence)

            if suspect_gap:
                # The loop was quiet for far longer than this stream's own
                # interval. Either the share paused or we were blocked; both
                # mean this frame may be queued history, and one dropped frame
                # is the cheap way to find out.
                self._suspect = True
                self.dropped_stale += 1
                self._note_drop("stale")
                return False

        if self._suspect:
            if gap is not None and gap >= self._catchup_gap():
                # Delivery waited for a frame instead of draining one, so the
                # queue is empty and this frame is the newest there is.
                self._suspect = False
            else:
                self.dropped_stale += 1
                self._note_drop("stale")
                return False

        # Observe before deciding. This is the cheap local change detector that
        # makes a stable screen cheap and a moving one expensive only when it
        # actually moves, and it runs *ahead of the budget* on purpose: the
        # budget answers "may we spend a frame on this?", which is a different
        # question from "did the screen move?". Behind the budget a change that
        # arrives during a refusal would be invisible, and `picture_is_current()`
        # -- which is what an act consults -- would keep saying yes about a
        # picture two window-switches old.
        #
        # A grid only, never the full-buffer CRC: `content_key` is rate-limited
        # by the budget for a reason, and this would otherwise run on all 15
        # arriving frames a second. Nothing is inferred from an unreadable grid
        # -- no bump, no claim -- because "cannot tell" is not evidence of
        # either outcome.
        signature = luma_signature(frame)
        if signature is not None:
            if self._observed_signature is None:
                self._observed_signature = signature
            elif change_ratio(self._observed_signature, signature) >= CHANGE_RATIO:
                self._observed_signature = signature
                self.note_content_change()

        # Budget: identical to the library's own rule so the declared frame rate
        # still means what the pipeline says it means. A rate of zero is a
        # profile that does not send frames at all, and a pending fresh request
        # does not override a budget nobody set.
        interval = self._interval_for(session)
        if interval <= 0:
            # A profile that sends no frames at all. Counted rather than silent:
            # "the budget refused this one" and "video is switched off" should
            # not look identical to whoever reads the counters.
            self.dropped_budget += 1
            return False

        fresh = self._fresh_requested
        self._fresh_requested = False
        if not fresh and self._last_sampled is not None and (now - self._last_sampled) < interval:
            # The library takes the first frame of a stream immediately -- there
            # is no previous sample to be inside of -- and so does this, because
            # `_last_sampled` is still None until a frame reaches the gate.
            self.dropped_budget += 1
            return False
        # Recorded even when the content hash below rejects the frame: this is
        # also what rate-limits hashing, so an unchanged screen costs one
        # checksum per interval instead of one per arriving frame.
        self._last_sampled = now

        key = content_key(frame)
        if signature is not None and self._signature is not None:
            # The test that works on a real screen: a coarse brightness grid with
            # a tolerance, so a blinking caret or a moved pointer is not a new
            # picture while a dialog or a scroll is.
            if change_ratio(self._signature, signature) < CHANGE_RATIO:
                self._quiet += 1
                self.dropped_unchanged += 1
                self._note_drop("unchanged")
                return False
            self._quiet = 0
            self._signature = signature
        elif key == self._last_key:
            # Nothing moved, and the grid was not readable. Sending it again
            # spends tokens to say nothing, and the model already holds this
            # exact picture.
            self._quiet += 1
            self.dropped_unchanged += 1
            self._note_drop("unchanged")
            return False
        else:
            self._quiet = 0
            if signature is not None:
                self._signature = signature

        self._last_key = key
        self._fresh_requested = False
        self.seq += 1
        self.accepted += 1
        self.picture_generation = self.generation
        request = self._pending_request
        self._pending_request = None
        self._delivering = (
            dict(request) if request is not None else {"reason": "scheduled"}
        )
        self._delivering["requested_at"] = (
            request.get("requested_at", now) if request is not None else now
        )
        self._latest = LatestFrame(
            frame=frame,
            seq=self.seq,
            generation=self.picture_generation,
            received_at=now,
            width=int(getattr(frame, "width", 0) or 0),
            height=int(getattr(frame, "height", 0) or 0),
        )
        self._report_frame(frame, changed=True)
        return True

    # -- content generation -------------------------------------------------

    def note_content_change(self) -> int:
        """The screen moved. Bump the generation and say what changed.

        Called by the cheap detector above and by a tool that knows something
        moved without looking at pixels (a window switch, an action that
        repaints). It publishes nothing: the *phase* did not change, and a
        `visual` record per observed change would be a heartbeat in disguise.
        The generation is read by `picture_is_current()` and by the delivery
        telemetry, both of which ask the question at the moment they need it.
        """
        self.generation += 1
        self._changed_at = self._clock()
        return self.generation

    def latest(self) -> LatestFrame | None:
        """The newest frame that passed every gate, or ``None``."""
        return self._latest

    def picture_age_ms(self, now: float | None = None) -> float | None:
        """How old the picture Lumine holds is, in milliseconds.

        ``None`` when she holds no picture at all -- which is a different answer
        from "zero", and reporting it as zero would be the same class of lie as
        sending a stale frame.
        """
        if self._latest is None:
            return None
        if now is None:
            now = self._clock()
        return max(0.0, (now - self._latest.received_at) * 1000.0)

    def content_age_ms(self, now: float | None = None) -> float | None:
        """How long ago the screen was last seen to move, in milliseconds."""
        if self._changed_at is None:
            return None
        if now is None:
            now = self._clock()
        return max(0.0, (now - self._changed_at) * 1000.0)

    def picture_is_current(self) -> bool:
        """Is the picture she holds still this screen?

        False when there is no picture, and False when the screen has moved
        since it was taken. This is the answer an act consults, and it is the
        one thing here that is allowed to be wrong in only one direction: a
        "yes" about a superseded screen is how a click lands somewhere else.
        """
        if self._latest is None:
            return False
        return self.picture_generation == self.generation

    def allows_send(self, now: float | None = None) -> tuple[bool, str]:
        """May the frame we just chose still be sent? ``(allowed, why)``.

        The last gate, and the only one that can see *after* the decision: the
        gate in :meth:`__call__` sees the frame at arrival, this one sees it at
        the moment the encoded image is about to be queued. Two things can have
        happened in between -- the loop stalled past :data:`MAX_PENDING_AGE`, or
        the screen moved -- and both mean the answer is no.

        Returns a reason rather than a bool so the delivery record can say *why*
        nothing was attached instead of only that nothing was.
        """
        if self._latest is None:
            return False, "no_frame"
        if now is None:
            now = self._clock()
        if self.picture_generation != self.generation:
            return False, "superseded"
        age = now - self._latest.received_at
        if age > MAX_PENDING_AGE:
            return False, "expired"
        return True, ""

    # -- helpers ------------------------------------------------------------

    def _interval_for(self, session: Any) -> float:
        """The frame budget for the current activity, backed off while quiet.

        The declared rate is a *ceiling on change*, not a schedule. A screen
        nobody is touching produces no new pictures however often it is sampled,
        so once a run of samples has found nothing, the interval stretches
        geometrically up to :data:`MAX_QUIET_INTERVAL`.

        **Only while the user is silent.** The speaking rate exists precisely
        because somebody is talking to Lumine about the thing on screen, and
        backing off to ten seconds during that conversation trades a real answer
        for a few saved tokens. Silence is where the saving is free.

        This is the part that stops the cost, and it is safe because of what it
        does *not* do: it never suppresses a change, only the sampling of a screen
        that has not moved. :meth:`request_fresh` bypasses it entirely, so a turn
        starting always gets a current picture.
        """
        speaking = getattr(session, "user_state", None) == "speaking"
        fps = self.speaking_fps if speaking else self.silent_fps
        if fps <= 0:
            return 0.0
        interval = 1.0 / fps
        if self._quiet and not speaking:
            interval *= min(QUIET_BACKOFF**self._quiet, MAX_QUIET_INTERVAL / interval)
        return min(interval, MAX_QUIET_INTERVAL)

    def _catchup_gap(self) -> float:
        """How long delivery must wait before the queue counts as empty.

        Derived from the observed cadence so a 60fps camera and a 15fps screen
        are both judged against their own arrival rate rather than a number that
        suits one of them.
        """
        return max(self._cadence * 0.8, MIN_ARRIVAL_GAP + 0.002)

    def stall_threshold(self) -> float:
        """How long a silence has to be before this frame is suspect.

        Several intervals of *this source's* rate, with :data:`STALE_SUSPECT_GAP`
        as the floor for one we have not measured yet. The floor alone is not
        enough: at 15fps it is nearly four frame intervals and does the right
        thing, but a source arriving every 333ms would clear it on every single
        frame while being perfectly healthy -- and because the cadence was only
        ever updated by gaps that had already passed this check, it could never
        be corrected. A slow source would therefore have been starved of frames
        it was entitled to send, with every counter saying "stale" and no error
        anywhere.
        """
        return max(STALE_SUSPECT_GAP, self._cadence * STALL_INTERVALS)

    def _note_drop(self, reason: str) -> None:
        visual = self._visual
        if visual is None:
            return
        try:
            visual.note_drop(reason)
        except Exception:  # noqa: BLE001 - observation must not break sampling
            pass

    def _report_frame(self, frame: Any, *, changed: bool) -> None:
        visual = self._visual
        if visual is None:
            return
        try:
            visual.note_frame(
                seq=self.seq,
                width=int(getattr(frame, "width", 0) or 0),
                height=int(getattr(frame, "height", 0) or 0),
                changed=changed,
            )
        except Exception:  # noqa: BLE001 - observation must not break sampling
            pass

    def _pump_link(self, session: Any, now: float) -> None:
        """Let the delivery link install itself, and sweep what has gone stale.

        Runs on every arriving frame because that is the only heartbeat this
        module is guaranteed to get. `install` is idempotent and costs one
        identity check once it has landed, and `sweep` is the *timer* for a frame
        already waiting in the plugin's send channel -- without it, one video
        event sitting in an idle channel would eventually be delivered after
        sitting there, which is the exact failure `MAX_PENDING_AGE` exists to
        prevent.

        Nothing in here may raise. A link that is mid-teardown, or a session
        object being replaced under us, must cost a frame's worth of
        housekeeping, never the frame.
        """
        link = self._link
        if link is None:
            return
        try:
            link.install(session)
            link.sweep(now)
            # Before the gate and therefore before the encode: `push_video`
            # compresses the frame on its way to `_send_client_event`, so the
            # profile has to be chosen while the frame is still being judged.
            link.sync_profile()
        except Exception:  # noqa: BLE001 - delivery bookkeeping is not the frame
            pass

    # -- diagnostics --------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Counters only. Used by tests and by ``perf_report``."""
        return {
            "seq": self.seq,
            "accepted": self.accepted,
            "dropped_stale": self.dropped_stale,
            "dropped_unchanged": self.dropped_unchanged,
            "dropped_budget": self.dropped_budget,
            "suspect": self._suspect,
            "cadence_ms": round(self._cadence * 1000, 2),
            # The two numbers that answer "why is she still spending tokens on a
            # screen that is not moving": how long the current quiet run is, and
            # how far the interval has stretched because of it.
            "quiet_run": self._quiet,
            "interval_ms": round(self._interval_for(_QuietSession()) * 1000, 2),
            # How much of what she is holding still describes this screen, and
            # how long ago it stopped if not.
            "generation": self.generation,
            "picture_generation": self.picture_generation,
            "picture_current": self.picture_is_current(),
            "picture_age_ms": self.picture_age_ms(),
            "content_age_ms": self.content_age_ms(),
            # Refused after the gate, which is the only place the refusal can
            # see the time that passed while an image was being encoded.
            "dropped_expired": self.dropped_expired,
            "dropped_superseded": self.dropped_superseded,
            "superseded": self.superseded,
            "pending_request": self._pending_request,
        }


class _QuietSession:
    """A stand-in with no user state, for reporting the *silent* interval.

    ``stats`` has no session to ask, and the interesting number is the one for a
    screen nobody is talking over -- the backoff is largest exactly then.
    """

    user_state = "listening"


# -- the session seam --------------------------------------------------------
#
# A tool cannot reach the sampler the session was built with: the worker serves
# several rooms at once, so there is no module-level "the sampler" to import, and
# handing a tool a session object it did not ask for would be a second seam next
# to the one `runtime/progress.py` already uses for step narration. This is that
# same seam, shaped for one method.

_SAMPLERS: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


def register_sampler(session: Any, sampler: Any) -> None:
    """Make ``sampler`` the one a tool will find for ``session``."""
    try:
        _SAMPLERS[session] = sampler
    except TypeError:  # a session that cannot be weakly referenced -- nothing to lose
        pass


def unregister_sampler(session: Any) -> None:
    """Drop the registration. Safe to call for a session never registered."""
    try:
        _SAMPLERS.pop(session, None)
    except TypeError:  # pragma: no cover - mirrors register_sampler
        pass


def _session_of(context: Any) -> Any:
    """Unwrap ``RunContext.session``, or take the context if it *is* one.

    Tools are handed a ``RunContext``; some call sites already hold the session.
    Guessing wrong here means a request that silently goes nowhere, so the
    unwrapping is explicit rather than by duck-typed attribute name.
    """
    session = getattr(context, "session", None)
    return session if session is not None else context


def sampler_for(context: Any) -> Any | None:
    """The sampler behind this tool call, or ``None`` in a process without one."""
    try:
        return _SAMPLERS.get(_session_of(context))
    except TypeError:  # an unhashable context -- there is nothing registered for it
        return None


def request_fresh_frame(
    context: Any,
    *,
    reason: str = "turn",
    profile: str = "general",
) -> bool:
    """Ask this session's sampler for the next good frame now.

    Non-blocking on purpose. It arms a request; the frame arrives on the next
    delivery, which at a 15fps capture is milliseconds away and at 0.3fps is up
    to 3.3s. Nothing here *waits*, because a tool that blocked on a screenshot
    would be a tool whose latency is dominated by a share nobody asked for.

    Returns whether a sampler was found, so a caller can tell "no sharing is
    running" from "sharing is running and I asked". It never raises: a tool must
    not be able to fail because of a bookkeeping seam.
    """
    try:
        sampler = sampler_for(context)
        if sampler is None:
            return False
        sampler.request_fresh(reason, profile)
        return True
    except Exception:  # noqa: BLE001 - a vision hint must never break a tool
        return False


def need_picture(
    context: Any,
    *,
    reason: str = "action",
    profile: str = "general",
    max_age: float = ACTION_MAX_AGE,
) -> dict[str, Any]:
    """Is the picture Lumine holds good enough to act on? Ask for one if not.

    The pre-condition for a consequential act, and the only place the two
    freshness rules meet: `picture_is_current()` answers "is this still the
    screen?", and ``max_age`` answers "is it recent enough to click on?". Both
    must pass. When they do not, a fresh frame is requested -- **but the caller
    is not told to stop**, because waiting for a screenshot is not the safety
    property. `tools/desktop/capture.py::confirm_target` re-reads the
    accessibility tree before anything irreversible, and that is what actually
    stops a wrong click. What this adds is that the model's *reasoning* is not
    seconds behind, and that "the picture was N ms old" is a number in the log
    rather than a thing nobody noticed.

    Never raises, and is meaningful with no sampler at all: a session with no
    share reports ``current=False`` with ``age_ms=None``, which is honest --
    there is no picture to act from, and saying otherwise would be the fake
    "Lumine can see" claim this whole feature exists to avoid.
    """
    report: dict[str, Any] = {
        "reason": reason if reason in REQUEST_REASONS else "action",
        "requested": False,
        "current": False,
        "age_ms": None,
        "generation": None,
        "picture_generation": None,
    }
    try:
        sampler = sampler_for(context)
        if sampler is None:
            return report
        age = sampler.picture_age_ms()
        report["age_ms"] = age
        report["generation"] = sampler.generation
        report["picture_generation"] = sampler.picture_generation
        fresh = sampler.picture_is_current()
        if fresh and age is not None and age > max_age * 1000.0:
            fresh = False
        report["current"] = bool(fresh)
        if not fresh:
            report["requested"] = bool(
                request_fresh_frame(context, reason=report["reason"], profile=profile)
            )
    except Exception:  # noqa: BLE001 - a tool must not fail on its own pre-check
        pass
    return report
