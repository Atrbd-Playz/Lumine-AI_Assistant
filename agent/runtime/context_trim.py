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

The budget is counted in **tokens**, not items. It used to count items, on the
reasoning that the item count is what the API exposes and a token estimate would
be a guess. That reasoning was wrong in a way that cost the whole session: a tool
result is *one item* however long it is, so forty items of already-compressed
results is roughly 8,800 tokens on its own. Added to a fixed prompt that was
itself over the limit, no amount of item-counting could have kept a per-minute
request inside it. An estimate that errs high is not a guess -- it is the only
number available before the provider answers, and over-estimating costs a little
context while under-estimating costs the reply.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

#: Module scope, in the shape the rest of the worker uses. A relative import
#: inside a function body breaks the script path -- `python agent/agent.py`, with
#: `agent/` on `sys.path` and no package above it -- and `test_import_shapes` fails
#: the build for exactly that. The registry is imported here rather than lazily
#: because it has to be measured, and it does not reach back into this module.
try:
    from ..tools.tools_policy import compose_instructions
    from ..tools.tools_registry import get_tools
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools.tools_policy import compose_instructions
    from tools.tools_registry import get_tools

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


#: Characters per token, for the estimate. Deliberately on the high side -- English
#: averages nearer 4 -- so the budget is spent a little early rather than a little
#: late. JSON, tool names and punctuation all push the real ratio down toward this
#: number, and the cost of guessing low is a rejected request.
CHARS_PER_TOKEN = 3.6

#: Per-minute token allowances, where the provider publishes one. This is the
#: number the whole budget exists to respect, and it is *total*: prompt and
#: completion together, which is why the completion cap is subtracted before the
#: conversation is given anything.
#:
#: Deliberately short. A provider absent from this table is not given an invented
#: ceiling -- see :data:`DEFAULT_HISTORY_TOKENS`, and the reason that inventing
#: one was wrong is written there.
PROVIDER_TPM = {
    # Groq's free chat tier. Also why `max_completion_tokens` is 900 and
    # `reasoning_effort` is low: thinking is drawn from the same allowance, so a
    # generous completion cap spends the turn's whole budget before the model has
    # said anything.
    "groq": 8_000,
}

#: The conversation budget for a provider with **no** published per-minute cap.
#:
#: This was previously a made-up 32,000 "TPM" that every unmeasured provider was
#: subtracted from, which was a category error with a real cost: it looked like a
#: measured limit and behaved like a generous one, so a realtime session never
#: trimmed and nobody could say why. A per-minute ceiling is a thing some tiers
#: have. Gemini Live has no such ceiling -- it has a context window, per-token
#: pricing and implicit caching of the stable prefix -- so asking what its "TPM"
#: is has no answer, and a conversation 20k tokens deep is fine there.
#:
#: So this is a working default rather than a limit someone published, and it is
#: sized for the thing that is genuinely a problem on an unmeasured provider: the
#: conversation growing until every request pays for it again. Twelve thousand
#: tokens is roughly a dozen exchanges with their tool results -- long enough that
#: a normal conversation is never touched, short enough that a session left
#: running stops re-sending everything it ever did.
DEFAULT_HISTORY_TOKENS = 12_000

#: Never hand the conversation less than this, however tight the arithmetic gets.
#: A model with the persona, the tool policy and one turn of history can still
#: answer "what did I just ask you"; with nothing left it cannot.
MIN_HISTORY_TOKENS = 600

#: Overrides the computed history budget outright, for a provider whose limit is
#: not in the table and for anyone who would rather choose the number themselves.
BUDGET_ENV = "LUMINE_CONTEXT_TOKEN_BUDGET"


def estimate_tokens(text: object) -> int:
    """A cheap, deliberately generous estimate of a string's token count.

    Not a tokenizer, and not presented as one: nothing on this path ships a BPE
    for the cascade's providers, and installing one to measure a prompt already
    held in memory would be a poor trade. Dividing characters by a conservative
    ratio is accurate enough to keep a rate limit from being hit, which is the
    only job this has.
    """
    return round(len(str(text or "")) / CHARS_PER_TOKEN)


def provider_tpm(provider: object) -> int:
    """A published per-minute allowance, or 0 when the provider has none.

    0 rather than a fallback, because "no published cap" and "some cap I made up"
    are different facts and only one of them can be acted on. Callers that treat
    0 as unlimited are saying "there is no ceiling here", which is the truth for
    a realtime model and is why :func:`history_budget` can give the conversation a
    real budget on one and an almost nonexistent one on the other.
    """
    return PROVIDER_TPM.get(str(provider or "").strip().lower(), 0)


def fixed_prompt_tokens(persona: str = "") -> int:
    """What every request costs before the conversation starts.

    Measured, not declared, because this is the part that drifts: a tool added, a
    docstring edited, a line added to the policy all change it, and a budget
    computed from a number written down months ago is wrong in exactly the
    situation where being wrong is expensive. Both halves are already in this
    process -- the composed instructions and the tool schemas -- so counting their
    characters is free.

    The persona is passed in rather than imported. The entrypoint owns it, and
    importing `agent.agent` from here would be a cycle; more to the point, a
    budget function that reaches into the module that calls it is a budget that
    cannot be tested without starting the whole worker.

    Returns 0 rather than raising if the registry cannot be read, so a missing
    import cannot become the reason a session has no budget.
    """
    total = 0
    try:
        if persona:
            total += estimate_tokens(compose_instructions(persona))
        for tool in get_tools():
            info = getattr(tool, "info", None)
            if info is None:
                continue
            total += estimate_tokens(
                json.dumps(
                    {
                        "name": getattr(info, "name", ""),
                        "description": getattr(info, "description", "") or "",
                        "params": {
                            str(name): {
                                "type": str(getattr(param, "type", "")),
                                "description": getattr(param, "description", "") or "",
                            }
                            for name, param in (getattr(info, "params", None) or {}).items()
                        },
                    }
                )
            )
    except Exception:  # noqa: BLE001 - a budget must never be the thing that raises
        logger.debug("[Context] could not measure the fixed prompt cost", exc_info=True)
    return total


def history_budget(
    provider: object = "", *, output_cap: int = 0, persona: str = ""
) -> dict[str, int]:
    """How many tokens the conversation may occupy, and the arithmetic behind it.

    The provider's per-minute allowance, minus what the fixed prompt costs on
    every request, minus the completion cap, is what is left for the
    conversation. Measuring the fixed cost rather than assuming it is what makes
    adding a tool *cost history* instead of costing the reply -- which is the
    difference between a smaller memory and no answer at all.

    That arithmetic only runs for a provider that *has* a published per-minute
    ceiling. A realtime model and a self-hosted one have none -- their limit is a
    context window, not a minute -- so both get :data:`DEFAULT_HISTORY_TOKENS`
    instead, and the difference between the two cases is a number that was
    published rather than one that was guessed. One code path serves a
    rate-limited cloud provider, a realtime model and a self-hosted one.
    """
    override = _int_env(BUDGET_ENV, 0, 0, 1_000_000)
    if override:
        return {"budget": override, "tpm": 0, "fixed": 0, "output": 0, "overridden": 1}
    tpm = provider_tpm(provider)
    fixed = fixed_prompt_tokens(persona)
    output = max(0, int(output_cap))
    if not tpm:
        # No published per-minute ceiling. There is nothing to subtract from, so
        # the working default is the budget -- rather than a fabricated allowance
        # minus the fixed cost, which produced a number that read as measured and
        # behaved as though nobody had thought about it.
        return {
            "budget": DEFAULT_HISTORY_TOKENS,
            "tpm": 0,
            "fixed": fixed,
            "output": output,
            "overridden": 0,
        }
    return {
        "budget": max(MIN_HISTORY_TOKENS, tpm - fixed - output),
        "tpm": tpm,
        "fixed": fixed,
        "output": output,
        "overridden": 0,
    }


@dataclass(frozen=True)
class TrimOutcome:
    """What trimming did, for the log and for the diagnostics page."""

    items_before: int
    items_after: int
    trimmed: bool
    #: Estimated tokens in the conversation before and after. Reported because
    #: the item count is not the thing that was wrong: forty items can be nine
    #: thousand tokens, and a panel that only shows "40 -> 12" cannot explain why
    #: a reply stopped arriving.
    tokens_before: int = 0
    tokens_after: int = 0
    budget: int = 0

    @property
    def removed(self) -> int:
        return max(0, self.items_before - self.items_after)

    @property
    def over_budget(self) -> bool:
        """Whether the conversation is still too big to send.

        True when trimming could not get under the budget -- the fixed prompt
        alone exceeds the provider's allowance, or the system message plus the
        floor exceeds what is left. This is the state that used to present as
        "the model went quiet", and it is worth saying out loud.
        """
        return bool(self.budget) and self.tokens_after > self.budget


def conversation_tokens(items: list[Any]) -> int:
    """Estimated tokens held by a list of chat items.

    Text is read off whatever shape the item has -- a plain string, a list of
    content parts, an object with ``text_content`` -- because a chat context holds
    all three and a tool result is the long one that matters. Anything unreadable
    counts as a small constant rather than zero, so an item this cannot measure
    biases the budget towards trimming rather than towards overrunning it.
    """
    total = 0
    for item in items:
        text = _item_text(item)
        if not text:
            total += 8
            continue
        total += estimate_tokens(text)
    return total


def _item_text(item: Any) -> str:
    content = getattr(item, "content", item)
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            else:
                parts.append(str(getattr(part, "text", "") or getattr(part, "text_content", "") or ""))
        return " ".join(p for p in parts if p)
    for attribute in ("text_content", "text"):
        value = getattr(item, attribute, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _is_instruction(item: Any) -> bool:
    """The system message, which carries the persona and the tool policy."""
    return getattr(item, "type", "") == "message" and getattr(item, "role", "") in (
        "system",
        "developer",
    )


def trim_context(
    session: Any,
    *,
    limit: int | None = None,
    token_budget: int | None = None,
) -> TrimOutcome:
    """Trim a session's chat context to the item *and* token budget.

    Takes the session rather than a ``ChatContext`` so this can be called from an
    event handler, and tolerates a session whose SDK version has no
    ``chat_ctx``: an agent that cannot be trimmed is still an agent that works,
    and failing here would be worse than running long.

    Both ceilings apply. The item count stops a pathological number of tiny
    items; the token budget is what actually keeps a per-minute request legal,
    because item count and cost are unrelated.

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
    tokens_before = conversation_tokens(items)
    budget = int(token_budget or 0)

    over_items = before > max(ceiling, MIN_ITEMS_BEFORE_TRIM)
    over_tokens = bool(budget) and tokens_before > budget
    if not over_items and not over_tokens:
        return TrimOutcome(before, before, trimmed=False, tokens_before=tokens_before,
                           tokens_after=tokens_before, budget=budget)

    # The token budget is expressed in items too, because that is the only thing
    # the SDK can drop. Counting backwards from the newest keeps the exchange the
    # user is having; the instruction message is never a candidate, and
    # `ChatContext.truncate` puts it back regardless.
    keep = ceiling
    if budget:
        kept: list[Any] = []
        running = 0
        for item in reversed(items):
            cost = max(8, estimate_tokens(_item_text(item)))
            if running + cost > budget and not _is_instruction(item):
                break
            kept.append(item)
            running += cost
        # Reversed, and the instruction message has to survive even when it alone
        # exceeds the budget -- a model with no persona is not a working agent.
        keep = max(1, min(ceiling, len(kept)))

    try:
        chat_ctx.truncate(max_items=keep)
    except Exception as exc:  # noqa: BLE001 - see above
        logger.warning("[Context] could not trim the chat context: %s", exc)
        return TrimOutcome(before, before, trimmed=False, tokens_before=tokens_before,
                           tokens_after=tokens_before, budget=budget)

    after_items = list(getattr(chat_ctx, "items", []) or [])
    tokens_after = conversation_tokens(after_items)
    logger.info(
        "[Context] trimmed the conversation from %d to %d items (%d -> %d tokens, budget %d)",
        before,
        len(after_items),
        tokens_before,
        tokens_after,
        budget,
    )
    return TrimOutcome(
        before,
        len(after_items),
        trimmed=len(after_items) < before,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        budget=budget,
    )


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
