"""Classify a failed LLM call, so a failure can be explained rather than swallowed.

A voice agent that fails silently is indistinguishable from a broken microphone:
the session is alive, the room is connected, and the user hears nothing. Every
failure mode here has a different remedy, so they are named rather than collapsed
into "error":

===================  ===========================================================
``rate_limited``     Too many requests this minute. A short wait fixes it.
``quota_exhausted``  The daily allowance is gone. Waiting minutes will not help.
``context_length``   The conversation outgrew the window. Needs trimming, not a
                     retry.
``auth``             The key is wrong or revoked. Retrying cannot help.
``model_unavailable`` The model does not exist, or this key cannot reach it. The
                     provider is healthy; the configuration is wrong. Retrying
                     fails identically forever, and only a settings change helps.
``provider_down``    Their side is broken or unreachable. Not the user's problem.
``unknown``          Anything unrecognised. Never reported as a quota problem.
===================  ===========================================================

The distinction that matters most is the first two. Both arrive as HTTP 429, and
the difference is whether the message names a per-minute limit or an exhausted
allowance. Telling someone to "try again shortly" when their daily quota is spent
sends them away to wait for something that will not help.

Pure and offline by construction: it reads a status code and a message, and never
imports a provider SDK, so every branch is unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: The categories, in the order they are tested. A message can mention several
#: things, so the first match wins and the order encodes precedence.
KIND_RATE_LIMITED = "rate_limited"
KIND_QUOTA_EXHAUSTED = "quota_exhausted"
KIND_CONTEXT_LENGTH = "context_length"
KIND_AUTH = "auth"
KIND_MODEL_UNAVAILABLE = "model_unavailable"
KIND_PROVIDER_DOWN = "provider_down"
KIND_UNKNOWN = "unknown"

#: Statuses that are never a credential or quota problem, whatever the message
#: says. A 400 from a malformed request of ours is our bug, and saying "quota
#: exceeded" would send the user to check a limit that is fine.
_NEVER_A_QUOTA = frozenset({400, 404, 405, 422})

#: The provider is answering normally; the model behind the key is the problem.
#:
#: Checked before the status-code rules because the signal lives in the body, and
#: the status is genuinely ambiguous: Groq answers a model your key cannot reach
#: with 404, and a model that no longer exists at all is the same 404. Reporting
#: that as an outage is wrong in a way that costs the user the whole diagnosis --
#: they go looking at their network instead of at the model dropdown.
_MODEL_WORDS = (
    "model_not_found",
    "model not found",
    "does not exist or you do not have access",
    "does not exist",
    "no access to this model",
    "you do not have access to",
    "is not available for your account",
    "unknown model",
    "model not supported",
)

_QUOTA_WORDS = (
    "quota exceeded",
    "exceeded your current quota",
    "quota has been exceeded",
    "insufficient_quota",
    "billing",
    "resource_exhausted",
)

_RATE_WORDS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "requests per minute",
    "tokens per minute",
    "try again in",
)

_CONTEXT_WORDS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "reduce the length",
    "context window",
    "prompt is too long",
)

_AUTH_WORDS = (
    "invalid api key",
    "api key not valid",
    "unauthorized",
    "permission denied",
    "invalid_api_key",
    "forbidden",
)

_DOWN_WORDS = (
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "overloaded",
    "connection reset",
    "connection refused",
    "timed out",
    "timeout",
    "name or service not known",
)


@dataclass(frozen=True)
class LlmFailure:
    """What went wrong, in terms a person can act on."""

    kind: str
    #: Short, user-facing. Never contains provider internals or a key.
    message: str
    #: Whether trying the same request again could plausibly work.
    retryable: bool
    #: Seconds to wait, when the provider said so. ``None`` when it did not.
    retry_after: int | None = None
    #: The provider's own words, trimmed. Useful in a log, never spoken.
    provider_detail: str = ""

    @property
    def is_limit(self) -> bool:
        return self.kind in (KIND_RATE_LIMITED, KIND_QUOTA_EXHAUSTED)


def _status_code(error: Any) -> int | None:
    """Pull an HTTP status out of whichever exception shape arrived.

    LiveKit wraps provider SDKs, so the code can sit on the exception itself, on
    a ``status_code`` attribute, or inside a body that is a JSON string.
    """
    for attribute in ("status_code", "status", "code"):
        try:
            value = getattr(error, attribute, None)
        except Exception:  # noqa: BLE001 - see _body_text
            continue
        if isinstance(value, int) and 100 <= value < 600:
            return value
    return None


def _body_text(error: Any) -> str:
    """Whatever prose the provider gave, flattened to one searchable string.

    Tolerates an exception whose ``__str__`` raises, which is rare but real: a
    classifier that can itself throw would turn a provider error into a crash
    inside the error handler.
    """
    parts: list[str] = []
    for attribute in ("body", "message", "detail", "reason"):
        try:
            value = getattr(error, attribute, None)
        except Exception:  # noqa: BLE001 - a property that raises is not a crash
            continue
        if isinstance(value, str) and value.strip():
            parts.append(value)
    if parts:
        return " ".join(parts)
    try:
        text = str(error)
    except Exception:  # noqa: BLE001
        return ""
    # Only the first line: a traceback is not something to match against.
    return text.strip().splitlines()[0] if text.strip() else ""


def _retry_after(error: Any, text: str) -> int | None:
    """Seconds the provider asked us to wait, if it said so."""
    for attribute in ("retry_after", "retry_after_seconds"):
        try:
            value = getattr(error, attribute, None)
        except Exception:  # noqa: BLE001 - see _body_text
            continue
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    # Groq and Google both put a human duration in the message.
    match = re.search(r"(?:retry in|try again in|please retry in)\s*~?(\d+)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"retryDelay\"?:\s*\"?(\d+)", text)
    if match:
        return int(match.group(1))
    return None


def _matches(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in words)


def classify(error: Any) -> LlmFailure:
    """Name a failure. Never raises, so a caller always has something to say.

    A provider that answers 400 with a quota-shaped message is still a malformed
    request, not an exhausted allowance: a retry would fail identically, and
    telling the user to check their quota would be wrong.
    """
    text = _body_text(error)
    status = _status_code(error)
    retry_after = _retry_after(error, text)

    def failure(kind: str, message: str, retryable: bool) -> LlmFailure:
        return LlmFailure(
            kind=kind,
            message=message,
            retryable=retryable,
            retry_after=retry_after,
            provider_detail=text[:400],
        )

    # Checked before anything status-based. A model the key cannot reach is
    # reported as an outage by every provider that has a generic 404 for it, and
    # the resulting advice -- "I couldn't reach my thinking service, try again"
    # -- sends the user to debug their network instead of their settings.
    if _matches(text, _MODEL_WORDS):
        return failure(
            KIND_MODEL_UNAVAILABLE,
            "That model isn't available on this account. Pick another one in Settings.",
            retryable=False,
        )

    # A 400/404/405/422 is a malformed request of ours whatever the body claims.
    if status in _NEVER_A_QUOTA:
        if _matches(text, _CONTEXT_WORDS):
            return failure(
                KIND_CONTEXT_LENGTH,
                "I lost track of the thread there — our conversation got too long.",
                retryable=False,
            )
        if _matches(text, _AUTH_WORDS):
            return failure(KIND_AUTH, "My access key for that service is not working.", retryable=False)
        return failure(KIND_PROVIDER_DOWN, "Something went wrong on my side. Try that again?", retryable=True)

    if status in (401, 403):
        return failure(KIND_AUTH, "My access key for that service is not working.", retryable=False)

    if status == 429:
        # Quota first: Groq uses 429 for both, and the message is the only
        # difference between "wait a moment" and "come back tomorrow".
        if _matches(text, _QUOTA_WORDS):
            return failure(
                KIND_QUOTA_EXHAUSTED,
                "I've used up my request allowance for now. Let's pick that up a bit later.",
                retryable=False,
            )
        return failure(
            KIND_RATE_LIMITED,
            "I'm going a little fast — give me a moment.",
            retryable=True,
        )

    if _matches(text, _QUOTA_WORDS):
        return failure(
            KIND_QUOTA_EXHAUSTED,
            "I've used up my request allowance for now. Let's pick that up a bit later.",
            retryable=False,
        )

    if _matches(text, _RATE_WORDS):
        return failure(KIND_RATE_LIMITED, "I'm going a little fast — give me a moment.", retryable=True)

    if _matches(text, _CONTEXT_WORDS):
        return failure(
            KIND_CONTEXT_LENGTH,
            "I lost track of the thread there — our conversation got too long.",
            retryable=False,
        )

    if _matches(text, _AUTH_WORDS):
        return failure(KIND_AUTH, "My access key for that service is not working.", retryable=False)

    if _matches(text, _DOWN_WORDS) or (status is not None and status >= 500) or status is None:
        return failure(KIND_PROVIDER_DOWN, "I couldn't reach my thinking service. Try that again?", retryable=True)

    if status is not None and 200 <= status < 300:  # pragma: no cover - defensive
        return failure(KIND_UNKNOWN, "Something unexpected happened.", retryable=True)

    return failure(KIND_UNKNOWN, "Something went wrong. Try that again?", retryable=True)
