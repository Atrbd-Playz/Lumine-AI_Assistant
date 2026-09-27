"""Groq LLM settings, resolved from the environment.

Deliberately free of livekit imports so the budget and retry values can be
asserted in unit tests without a plugin, a worker, or provider credentials.

The defaults exist to keep voice replies small. Without a completion cap, an
ordinary turn can request thousands of completion tokens, which is what pushed
requests past the Groq per-minute token limit and produced HTTP 429s.
"""

import os
from typing import Any

DEFAULT_MODEL = "openai/gpt-oss-20b"

#: A voice reply is one or two sentences, but GPT-OSS is a *reasoning* model: its
#: thinking is drawn from the same budget, and a cap that is generous enough to
#: think with leaves nothing to say with. That is how a turn produces a perfect
#: internal monologue and no audio at all. 900 leaves room to think briefly and
#: still speak.
DEFAULT_MAX_COMPLETION_TOKENS = 900

DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_RETRIES = 2
DEFAULT_CONNECT_MAX_RETRY = 1

#: How hard the model thinks. "low" is deliberate: a voice turn pays latency for
#: reasoning the user will never hear, and reasoning is billed.
DEFAULT_REASONING_EFFORT = "low"
REASONING_EFFORTS = ("low", "medium", "high")

# A voice turn needs at most one provider retry; anything higher turns a rate
# limit into a retry storm that spends the remaining quota.
MAX_ALLOWED_RETRIES = 2
MIN_COMPLETION_TOKENS = 16


def _int_env(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def model() -> str:
    return (os.getenv("GROQ_MODEL") or "").strip() or DEFAULT_MODEL


def max_completion_tokens() -> int:
    return _int_env(
        "GROQ_MAX_COMPLETION_TOKENS", DEFAULT_MAX_COMPLETION_TOKENS, MIN_COMPLETION_TOKENS
    )


def temperature() -> float:
    return _float_env("GROQ_TEMPERATURE", DEFAULT_TEMPERATURE)


def max_retries() -> int:
    """Provider-level retries, used for the client inside the Groq plugin."""
    return _int_env("GROQ_MAX_RETRIES", DEFAULT_MAX_RETRIES, 0, MAX_ALLOWED_RETRIES)


def connect_max_retry() -> int:
    """LiveKit-level retries around a failed LLM completion.

    Attempts are this value plus one, so the default of 1 means at most two
    attempts instead of the previous four.
    """
    return _int_env("GROQ_CONNECT_MAX_RETRY", DEFAULT_CONNECT_MAX_RETRY, 0, MAX_ALLOWED_RETRIES)


def reasoning_effort() -> str:
    """How much the model thinks before answering.

    Only sent for a model that takes it. Sending it to one that does not is a
    400, which is the same silent-no-audio failure this whole module exists to
    avoid.
    """
    value = (os.getenv("GROQ_REASONING_EFFORT") or "").strip().lower()
    if value in REASONING_EFFORTS:
        return value
    return DEFAULT_REASONING_EFFORT


def llm_config() -> dict[str, Any]:
    """Keyword arguments for ``livekit.plugins.groq.LLM``."""
    return {
        "model": model(),
        "temperature": temperature(),
        "max_completion_tokens": max_completion_tokens(),
        # One tool per turn keeps a reply to a single extra completion round.
        "parallel_tool_calls": False,
        "max_retries": max_retries(),
        "reasoning_effort": reasoning_effort(),
    }
