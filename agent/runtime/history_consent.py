"""Consent to read message history, once per conversation.

Lumine could not read a chat's messages before this, and that was a promise
rather than an accident: the tool docstrings, the Tools page card, and the
repository instructions all said so. Almost everything people actually want from
their WhatsApp, though, needs it -- "did Afia reply?", "what was that address
she sent?", "what did we agree on?" are all questions about messages that already
exist. So the capability arrives, and with it the obligation to be honest about
what changed.

Three things make the change safe.

**It asks.** The class is ``sensitive``, so under any default policy the gate
raises a question and waits for a person. Silence refuses, a hedge refuses, and
the answer has to come from the user's own voice or a tap on their own card.

**It asks once, in plain words.** The grant is session-scoped, so "read my
messages" does not become a question on every turn of a conversation. That makes
the *wording* load-bearing: the spoken question and the card both say she can
read their WhatsApp for the rest of *this conversation*, because a grant the user
did not know they were giving is worse than no grant at all. A per-chat grant
would be stricter; it is a one-line change to :func:`require_history_consent` and
the trade is a question per chat instead of one per conversation.

**It is configuration, and configuration only narrows.** ``off`` reads nothing
ever, ``ask`` is the default above, ``auto`` reads without asking. Nothing here
can grant itself: the value comes from the saved document or the environment, and
the call sites pass their own class, so a settings file can make history read
more quietly but never make a *send* quieter.
"""

from __future__ import annotations

import logging
import os
import time
import weakref
from typing import Any, Mapping

try:
    from ..settings.config_store import effective_document
    from ..tools.tools_compat import ToolError
    from ..tools.tools_text import clip
    from .permission_gate import require_approval
except ImportError:  # running as a top-level script with agent/ on sys.path
    from settings.config_store import effective_document
    from tools.tools_compat import ToolError
    from tools.tools_text import clip

    # `runtime.`-prefixed even though this file sits inside `runtime/`, matching
    # `permission_gate`'s own fallback exactly. The shape that lands here is
    # "`agent/` is on sys.path and `runtime` is imported as a package", and in that
    # shape a bare `permission_gate` does not resolve -- `runtime/` itself is not
    # on the path. Two spellings of the same fallback in one package is the kind
    # of thing that only shows up in the one import shape nobody tests by hand,
    # which is why `test_import_shapes.py` exists.
    from runtime.permission_gate import require_approval

logger = logging.getLogger("lumine")

#: Saved-document key, then the environment underneath it -- the same precedence
#: every other setting in this product follows.
CONFIG_PATH = ("messaging", "whatsapp", "history")
MODE_VAR = "LUMINE_WHATSAPP_HISTORY"

#: ``ask`` is the default, and it is the only default that is not ``off``. A
#: capability this private should never be the answer to "what happens if nobody
#: configures anything".
MODES = ("off", "ask", "auto")
DEFAULT_MODE = "ask"

MSG_OFF = (
    "Reading WhatsApp messages is switched off in configuration, so I didn't "
    "read anything."
)
MSG_SESSION_NOTE = "for the rest of this conversation"

#: Session-scoped grants, keyed by the session object. Weak keys, so a session
#: that was never torn down cannot leak one -- the same argument `latency.py`
#: makes for its registry.
_GRANTS: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()


def resolve_mode() -> str:
    """The configured history mode, with ``ask`` as the floor.

    Mirrors ``permission_gate.normalize_level``: an unrecognised value becomes the
    *stricter* of the two plausible readings rather than the looser one, because
    the two ways to be wrong here are "asked the user about their messages when
    they had switched it off" and "read their messages when they had meant to
    switch it off".
    """
    raw: object = None
    try:
        document, _source, _diagnostics = effective_document()
        section = document.get(CONFIG_PATH[0]) if isinstance(document, Mapping) else None
        if isinstance(section, Mapping):
            inner = section.get(CONFIG_PATH[1])
            if isinstance(inner, Mapping) and inner.get(CONFIG_PATH[2]) is not None:
                raw = inner.get(CONFIG_PATH[2])
    except Exception:  # noqa: BLE001 - the environment is a complete fallback
        logger.debug("[History] saved document unavailable", exc_info=True)
    if raw is None:
        raw = os.getenv(MODE_VAR, "")
    text = str(raw or "").strip().lower()
    if text in MODES:
        return text
    # An unset value and an unparseable value are treated the same way on
    # purpose: both mean nobody made a decision, and the default is the question.
    return DEFAULT_MODE if not text else "ask"


def _session_of(context: Any) -> Any:
    inner = getattr(context, "session", None)
    return inner if inner is not None else context


def granted(session: Any) -> bool:
    """Whether this session already holds a history grant."""
    if session is None:
        return False
    try:
        return bool(_GRANTS.get(session))
    except TypeError:
        return False


def release(session: Any) -> None:
    """Drop a session's grant. Called at session end."""
    try:
        _GRANTS.pop(session, None)
    except TypeError:
        pass


def _remember(session: Any, *, what: str) -> None:
    try:
        _GRANTS[session] = {"what": str(what or "")[:80], "at": time.time()}
    except TypeError:
        # Nothing to do: the cost is one extra question per turn on a session
        # object that cannot hold a weak reference, which is not an AgentSession.
        logger.debug("[History] could not store the grant", exc_info=True)


async def require_history_consent(context: Any, *, tool: str, what: str) -> None:
    """Raise ``ToolError`` unless this session may read message history.

    ``what`` names the chat, and ends up in the question *and* on the card -- so
    it must be the chat, not a description of the request. A user approving "read
    Afia's messages" has agreed to something narrower than "read my messages", and
    that narrower agreement is the one worth having.

    The wording carries the scope deliberately. The grant outlives this call, so
    the question has to say so out loud rather than letting a single "yes" quietly
    turn into a session-wide licence.
    """
    mode = resolve_mode()
    if mode == "off":
        raise ToolError(MSG_OFF)

    session = _session_of(context)
    if mode == "ask" and not granted(session):
        subject = clip(str(what or "your messages"), 40)
        await require_approval(
            context,
            tool=tool,
            action="read_history",
            action_class="sensitive",
            # The same sentence reaches the card and the voice, because
            # `permission_gate` builds both from this one field. A card that
            # paraphrased the grant differently would let someone answer "yes" to
            # one and believe they had agreed to the other.
            summary=(
                f"Read your WhatsApp messages in the chat with {subject}, "
                f"{MSG_SESSION_NOTE}"
            ),
            target=subject,
        )
        _remember(session, what=subject)