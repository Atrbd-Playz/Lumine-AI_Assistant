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
"""

from __future__ import annotations

import time
import zlib
from collections.abc import Callable
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
        length = len(plane)
    except TypeError:  # pragma: no cover - a memoryview always has a length
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
            cells.append(int(plane[offset]))
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

    # -- wiring -------------------------------------------------------------

    def attach(self, visual: Any) -> None:
        """Register the :class:`~agent.runtime.visual_state.VisualState` to report to."""
        self._visual = visual

    def request_fresh(self) -> None:
        """Ask for the next good frame now, regardless of the fps budget.

        Used when a turn starts: without it the model can be answering a
        question about the screen using a still that is up to a silent interval
        old. It does **not** bypass the freshness gate -- asking for a frame is
        not permission to present a queued one as current.
        """
        self._fresh_requested = True

    # -- decision -----------------------------------------------------------

    def __call__(self, frame: Any, session: Any) -> bool:
        now = self._clock()
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
        signature = luma_signature(frame)
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
        self._report_frame(frame, changed=True)
        return True

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
        }


class _QuietSession:
    """A stand-in with no user state, for reporting the *silent* interval.

    ``stats`` has no session to ask, and the interesting number is the one for a
    screen nobody is talking over -- the backoff is largest exactly then.
    """

    user_state = "listening"
