"""Keep the conversation context inside a token budget.

A voice session is long-lived. Every turn appends to the chat context, and the
whole thing is re-sent on every completion, so an unbounded conversation is
re-charged in full each time. On a per-minute token allowance that is what turns
a working agent into a silent one, and it is why the context gets trimmed here
rather than left to the provider.

Two rules, both learned the hard way:

* **The trimmer is a backstop, not the strategy.** Tool results are compressed
  at the source, and the persona is a short core, so this normally has nothing
  to do. It exists so that a long session degrades by forgetting old turns
  rather than by failing.
* **The instruction message is never dropped.** ``ChatContext.truncate`` puts it
  back, which matters because the persona *is* the system message: losing it
  would leave a model with no personality and no tool policy mid-conversation.

The cap is counted in chat items rather than tokens because the item count is
what the API exposes, and a rough token estimate would be a guess. A tool result
is one item however long it is, which is precisely why compression at the source
matters more than this.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("lumine")

#: Chat items to keep. Chosen so a normal exchange of a dozen turns fits without
#: any trimming at all, and a long one degrades instead of failing.
DEFAULT_MAX_ITEMS = 40

#: Below this the trimmer does not run: an item count this small cannot be the
#: cause of a context-length error, and trimming early would throw away context
#: the model can still use.
MIN_ITEMS_BEFORE_TRIM = 24


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    import os

    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def max_context_items() -> int:
    return _int_env("LUMINE_MAX_CONTEXT_ITEMS", DEFAULT_MAX_ITEMS, 8, 400)


@dataclass(frozen=True)
class TrimOutcome:
    """What trimming did, for the log and for the diagnostics page."""

    items_before: int
    items_after: int
    trimmed: bool

    @property
    def removed(self) -> int:
        return max(0, self.items_before - self.items_after)


def trim_context(session: Any, *, limit: int | None = None) -> TrimOutcome:
    """Trim a session's chat context to the budget.

    Takes the session rather than a ``ChatContext`` so this can be called from an
    event handler, and tolerates a session whose SDK version has no
    ``chat_ctx``: an agent that cannot be trimmed is still an agent that works,
    and failing here would be worse than running long.

    Never raises.
    """
    ceiling = limit or max_context_items()
    try:
        chat_ctx = session.chat_ctx
        items = list(getattr(chat_ctx, "items", []) or [])
    except Exception as exc:  # noqa: BLE001 - trimming is best-effort by design
        logger.debug("[Context] could not read the chat context: %s", exc)
        return TrimOutcome(0, 0, trimmed=False)

    before = len(items)
    if before <= max(ceiling, MIN_ITEMS_BEFORE_TRIM):
        return TrimOutcome(before, before, trimmed=False)

    try:
        chat_ctx.truncate(max_items=ceiling)
    except Exception as exc:  # noqa: BLE001 - see above
        logger.warning("[Context] could not trim the chat context: %s", exc)
        return TrimOutcome(before, before, trimmed=False)

    after = len(list(getattr(chat_ctx, "items", []) or []))
    logger.info("[Context] trimmed the conversation from %d to %d items", before, after)
    return TrimOutcome(before, after, trimmed=after < before)


def instructions_intact(session: Any) -> bool:
    """Whether the system message survived a trim.

    Exists so a test can assert the property that matters rather than only
    asserting an item count: a trimmed context that lost the persona is worse
    than one that failed loudly.
    """
    try:
        items = list(getattr(session.chat_ctx, "items", []) or [])
    except Exception:  # noqa: BLE001
        return False
    return any(
        getattr(item, "type", "") == "message" and getattr(item, "role", "") in ("system", "developer")
        for item in items
    )
