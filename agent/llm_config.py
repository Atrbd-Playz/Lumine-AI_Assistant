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
DEFAULT_MAX_COMPLETION_TOKENS = 300
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_RETRIES = 2
DEFAULT_CONNECT_MAX_RETRY = 1

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


def llm_config() -> dict[str, Any]:
    """Keyword arguments for ``livekit.plugins.groq.LLM``."""
    return {
        "model": model(),
        "temperature": temperature(),
        "max_completion_tokens": max_completion_tokens(),
        # One tool per turn keeps a reply to a single extra completion round.
        "parallel_tool_calls": False,
        "max_retries": max_retries(),
    }
