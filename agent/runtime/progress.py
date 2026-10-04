"""A tool narrating what it is doing, while it is doing it.

A voice turn has one channel and it is already busy. Lumine says a sentence, calls
a tool, and the user hears *nothing at all* until the tool returns -- so a request
that takes four seconds is four seconds of a companion who has gone quiet, which
reads as a broken microphone rather than as work in progress. The
``tool_status`` started/completed pair fixes the endpoints and leaves the entire
middle invisible.

This module is the middle. A tool publishes named steps as it passes them, each
one arriving with its own duration, and the desktop app renders them as an ordered
list: *opening WhatsApp, found Afia, chat open, draft typed, waiting for you*.
Nothing is spoken, because narrating five steps aloud would cost more in latency
and annoyance than the silence it replaced -- the one thing that *is* spoken is the
approval question, which `permission_gate` already owns.

## Why a registry, and not a publisher on the context

A tool is handed a `RunContext` and nothing else: it does not get the room, the
publisher, or the latency tracker. `permission_gate` and `latency` both solved this
by keeping a session-keyed registry that a tool can resolve from the session it was
given, and this is the third instance of the same arrangement rather than a new
idea. Weak keys, so a session that was never torn down cannot leak one.

## Nothing here may raise

Every function swallows its own failures. This is called from inside tools, on the
path between "the user asked for something" and "the thing happened", and a
telemetry channel that can end a send is worse than no telemetry at all -- the
same argument `runtime_events.write_event_line` is built on, for the same reason.
Losing a step is acceptable. Losing the message is not.
"""

from __future__ import annotations

import logging
import time
import weakref
from contextlib import contextmanager
from typing import Any, Iterator

try:
    from . import latency
except ImportError:  # running as a top-level script with agent/ on sys.path
    import runtime.latency as latency  # type: ignore[no-redef]

logger = logging.getLogger("lumine")

#: The topic the step records travel on, alongside the tool statuses they sit
#: between. Separate from `lumine.permission` so a progress row can never be
#: mistaken for something the app is expected to answer.
TOPIC = "lumine.tool"

#: The status a step carries. Distinct from `started`/`completed`/`failed`, which
#: bracket the whole call: a step is neither an endpoint nor an outcome, and
#: reusing either would make the app report "finished" five times per call.
STEP_STATUS = "step"

#: How long a step's description may run. A step is a label on a list, not a
#: sentence; anything longer is a paragraph nobody scans.
MAX_STEP_MESSAGE = 90

_PUBLISHERS: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------


def register_publisher(session: Any, publisher: Any) -> None:
    """Let tools on `session` publish steps through `publisher`."""
    if session is None or publisher is None:
        return
    try:
        _PUBLISHERS[session] = publisher
    except TypeError:
        # An object that cannot hold a weak reference is not an AgentSession in
        # any supported version. Dropping the registration costs the user a
        # progress list; raising here would cost them the message.
        logger.debug("[Progress] session does not support weak references", exc_info=True)


def unregister_publisher(session: Any) -> None:
    try:
        _PUBLISHERS.pop(_session_of(session), None)
    except TypeError:
        pass


def publisher_for(session: Any) -> Any | None:
    """The publisher for a session, or ``None`` when nothing registered one.

    Accepts the session or a ``RunContext`` wrapping it, because the two are
    interchangeable everywhere in this codebase and a lookup that worked with one
    but not the other would be a progress list that silently disappears in
    exactly one of the call sites. `_session_of` does the unwrapping; resolving it
    here rather than at each call site is what keeps the two spellings honest.
    """
    session = _session_of(session)
    if session is None:
        return None
    try:
        return _PUBLISHERS.get(session)
    except TypeError:
        return None


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def _session_of(context: Any) -> Any:
    """The session behind whatever a tool was handed.

    Tolerates being given the session directly, because both call shapes exist in
    this codebase and a step that only worked with one of them would be a step
    that silently stopped appearing. Returns ``None`` for ``None`` and for
    anything without a session, so every caller below can treat it as "no
    publisher" rather than each repeating the check.
    """
    if context is None:
        return None
    inner = getattr(context, "session", None)
    return inner if inner is not None else context


def _clip(value: object) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= MAX_STEP_MESSAGE else text[: MAX_STEP_MESSAGE - 1] + "…"


def build_step_record(
    *,
    tool: str,
    action: str,
    message: str,
    status: str = STEP_STATUS,
    index: int = 0,
    total: int = 0,
    duration_ms: float | None = None,
    **detail: Any,
) -> dict[str, Any]:
    """The record itself, so the shape is testable without a room.

    ``id`` is ``tool.action`` rather than something unique: the app renders these
    as an ordered list keyed by step, and a fresh id per record would make five
    steps of one call look like five unrelated things. ``index``/``total`` carry
    the position, so "step 3 of 5" is readable without the app counting.
    """
    record: dict[str, Any] = {
        "type": "tool_status",
        "id": f"{tool}.{action}",
        "name": tool,
        "tool": tool,
        "action": action,
        "status": status,
        "message": _clip(message),
        "summary": _clip(message),
        "index": int(index),
        "timestamp": time.time(),
    }
    if total:
        record["total"] = int(total)
    if duration_ms is not None:
        record["duration_ms"] = round(float(duration_ms), 1)
    for key, value in detail.items():
        if value is not None:
            record[key] = value
    return record


def publish_step(
    context: Any,
    *,
    tool: str,
    action: str,
    message: str,
    index: int = 0,
    total: int = 0,
    status: str = STEP_STATUS,
    **detail: Any,
) -> None:
    """Publish one step. Never raises, and never blocks on the network.

    The data-channel publish is fire-and-forget by construction -- the publisher
    schedules it -- because a step that waits for a round trip to the app has
    inserted the app's latency into the middle of the user's request.
    """
    publisher = publisher_for(_session_of(context))
    if publisher is None:
        return
    record = build_step_record(
        tool=tool,
        action=action,
        message=message,
        status=status,
        index=index,
        total=total,
        **detail,
    )
    try:
        publisher.emit_and_publish(TOPIC, record)
    except Exception:  # noqa: BLE001 - a progress row must never break a tool
        logger.debug("[Progress] step %s/%s could not be published", tool, action, exc_info=True)


@contextmanager
def step(
    context: Any,
    tool: str,
    action: str,
    message: str,
    *,
    index: int = 0,
    total: int = 0,
    phase: str = "tool",
    **detail: Any,
) -> Iterator[dict[str, Any]]:
    """Publish a step on the way in and its duration on the way out.

    Two records rather than one, deliberately. A step that only appears when it
    *finishes* is invisible for exactly as long as the slow thing it is
    describing, so the wait is the one part of the sequence a person most wants to
    see. The closing record carries the same ``id`` and ``status: "done"``, which
    is what lets a UI tick the row off instead of leaving it spinning.

    The yielded dict is mutable and its contents are merged into the closing
    record, so a step can attach what it learned -- the chat it found, the
    duration of the search -- without a second publish call.
    """
    state: dict[str, Any] = {}
    started = time.monotonic()
    publish_step(
        context,
        tool=tool,
        action=action,
        message=message,
        index=index,
        total=total,
        status="started",
        **detail,
    )
    try:
        yield state
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised untouched
        elapsed = (time.monotonic() - started) * 1000
        publish_step(
            context,
            tool=tool,
            action=action,
            message=f"{message} -- failed",
            index=index,
            total=total,
            status="failed",
            duration_ms=elapsed,
            reason=_clip(exc) or type(exc).__name__,
            **state,
        )
        _measure(context, f"step_{action}", elapsed, tool=tool, phase=phase)
        raise
    elapsed = (time.monotonic() - started) * 1000
    publish_step(
        context,
        tool=tool,
        action=action,
        message=message,
        index=index,
        total=total,
        status="done",
        duration_ms=elapsed,
        **state,
    )
    _measure(context, f"step_{action}", elapsed, tool=tool, phase=phase)


def _measure(context: Any, stage: str, duration_ms: float, **detail: Any) -> None:
    """Put a step's duration in the timeline too.

    Two surfaces, one measurement: the step list shows *where* the time went and
    the activity timeline shows *how much*, and a step that only reached one of
    them would leave the other wrong.
    """
    try:
        latency.emit_duration(_session_of(context), stage, duration_ms, **detail)
    except Exception:  # noqa: BLE001 - measurement must never break the caller
        pass