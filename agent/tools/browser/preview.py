"""Getting a page's picture out of the browser: to the app, and to the model.

Two destinations, one capture, and the difference between them is the whole
reason this is a module rather than a line in `control.py`.

**The app** gets a bounded JPEG on the ``lumine.browser`` data channel, so a
person can watch what Lumine is looking at. It is a preview: transient, capped,
and never in the model's context.

**The model** gets an ``ImageContent`` appended to the chat context, and only
when she asks for it. A tool cannot return an image -- LiveKit's
``_is_valid_function_output`` accepts only ``str | int | float | bool | complex |
None`` and nested containers, and anything else becomes "the tool returned an
invalid output" -- so this has to be a chat-context insertion rather than a
return value. That is why :func:`show_to_model` returns a sentence.

## Why the two budgets are so far apart

A tool result is re-sent on every later turn for the rest of the session, so an
image in the model's context is billed repeatedly. A data-channel publish is paid
for once. Hence ``state.MAX_PREVIEW_BYTES`` (900 KB) against
``state.MAX_MODEL_IMAGE_BYTES`` (320 KB), and a 900px edge against 512.

The honesty rule from `runtime/visual_state.py` applies here too: a screenshot
travels with its age and its source, because a picture with no timestamp reads as
live and is not.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

# The publisher is resolved through the session-keyed registry that
# `runtime/progress.py` keeps, because a tool is handed a `RunContext` and nothing
# else -- the same reason that module exists. Imported at module level in the
# `try/except` pair production actually runs: `python agent/agent.py` loads these
# as top-level modules, and a relative import inside a function body would pass
# every other test and then crash the first real session.
try:
    from ...runtime import progress
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime import progress  # type: ignore[no-redef]

logger = logging.getLogger("lumine")

#: Where a preview travels. Separate from `lumine.tool`, which is the toast and
#: status contract: a few hundred KB of base64 on the topic the app renders as
#: transient text would be a different product surface wearing the same name, and
#: a dropped preview must never be able to delay a tool status.
TOPIC = "lumine.browser"

#: The record type the app switches on.
RECORD_TYPE = "browser_preview"

#: How much of the payload is the picture. Everything else is metadata, and the
#: picture is the part that is allowed to be large.
MAX_PREVIEW_CHARS = int(1_400_000)


def encode_jpeg(data: bytes) -> str:
    """Base64 for a data URL, or ``""`` for anything that is not an image.

    The ``data:`` prefix is what makes it renderable by an ``<img src>`` with no
    second round trip to a file path, and it is why the app needs no knowledge of
    where the agent wrote anything -- because it does not.
    """
    if not isinstance(data, bytes) or not data:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def preview_record(
    data: bytes,
    *,
    url: str = "",
    title: str = "",
    age_ms: float | None = None,
    reason: str = "captured",
) -> dict[str, Any]:
    """The publishable record: metadata, then the picture.

    ``reason`` and ``age_ms`` are not decoration. ``visual_state`` publishes
    "last frame 2s ago" for the same reason this publishes age: a screenshot
    with no age is indistinguishable from a live view, and Lumine is holding a
    photograph.

    The cap is **enforced, not reported**. An earlier version set
    ``truncated: True`` on an oversized payload and then sent the whole payload
    anyway, which is the worst of both: the flag claims a bound nothing applied
    while the cost lands on the data channel and the renderer. So the picture is
    dropped at the ceiling and the record says ``imageOmitted`` -- a preview the
    user sees nothing for, against one that costs a decode stall and arrives
    whole, and the second is the more honest failure of the two.
    """
    record: dict[str, Any] = {
        "type": RECORD_TYPE,
        "reason": reason,
        "url": str(url or "")[:200],
        "title": str(title or "")[:90],
        "bytes": len(data) if isinstance(data, bytes) else 0,
        "captured_at": time.time(),
    }
    if age_ms is not None:
        record["age_ms"] = round(float(age_ms), 1)
    encoded = encode_jpeg(data)
    if not encoded:
        record["imageOmitted"] = "there was no picture"
    elif len(encoded) > MAX_PREVIEW_CHARS:
        # Bytes stay off the wire entirely. The metadata is the useful half here
        # anyway -- which page, how old -- and it is a few hundred bytes.
        record["imageOmitted"] = "the picture was too large to send"
    else:
        record["image"] = encoded
    return record


def publish_preview(context: Any, record: dict[str, Any]) -> bool:
    """Send a preview to the app. Never raises, and reports whether it went.

    Resolves the publisher through the session-keyed registry that
    `runtime/progress.py` already keeps, for the same reason that module does: a
    tool is handed a ``RunContext`` and nothing else. A screenshot that cannot be
    delivered is still a screenshot that was taken, so the caller carries on and
    says so.
    """
    try:
        publisher = progress.publisher_for(context)
        if publisher is None:
            return False
        publisher.emit_and_publish(TOPIC, record)
        return True
    except Exception:  # noqa: BLE001 - a preview is telemetry; it cannot break a tool
        logger.debug("[Browser] preview could not be published", exc_info=True)
        return False


def show_to_model(context: Any, data: bytes, *, caption: str = "") -> str:
    """Put the picture in front of the model's eyes, and return what to say.

    The insertion, not the return value, is the delivery -- see the module
    docstring for why a tool cannot hand an image back as its result.

    Fails closed into a sentence rather than raising. A model that asked to look
    and was told "the tool returned an invalid output" learns nothing; a model
    told the picture did not arrive can fall back to ``read`` and carry on.
    """
    if not isinstance(data, bytes) or not data:
        return "There was no picture to look at."

    try:
        try:
            from livekit.agents.llm import ImageContent
        except ImportError:  # pragma: no cover - livekit-agents absent
            ImageContent = None  # type: ignore[assignment]

        if ImageContent is None:
            return "I can't look at pictures on this build -- read the page instead."

        encoded = encode_jpeg(data)
        if not encoded:
            return "That picture could not be prepared."

        session = getattr(context, "session", None)
        chat_ctx = getattr(session, "chat_ctx", None)
        if chat_ctx is None:
            # `_ReadOnlyChatContext` exposes items but not add_message; the mutable
            # context is one level down on the agent. Reading it defensively here
            # is what turns "no mutable context" into a sentence.
            return "I can't show myself a picture right now -- read the page instead."

        add_message = getattr(chat_ctx, "add_message", None)
        if not callable(add_message):
            return "I can't show myself a picture right now -- read the page instead."

        content: list[Any] = []
        label = str(caption or "").strip()
        if label:
            # The caption is what turns a picture into evidence: without it the
            # model has an image and no idea which page it belongs to, and
            # "the page moved" stops being checkable.
            content.append(f"Screenshot of {label}. This is what the page looks like right now.")
        content.append(ImageContent(image=encoded))
        add_message(role="user", content=content)
        return "I looked at the page."
    except Exception:  # noqa: BLE001 - see publish_preview
        logger.debug("[Browser] the picture could not be shown to the model", exc_info=True)
        return "I couldn't look at that one -- read the page instead."