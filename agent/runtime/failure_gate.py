"""Stop one bad turn from becoming a silent one.

The failure this exists for: the LLM request fails, nothing is spoken, and the
user cannot tell a rate limit from a muted microphone. A session that has hit a
rate limit also keeps trying, which burns the little quota that remains and
makes recovery slower.

So failures are counted, and after a few in a row the session stops trying for a
cooldown. One short apology is spoken, the reason is published for the desktop
app to show, and the circuit closes again on its own.

Deliberately not a retry loop. Backing off and *telling the user* is the useful
behaviour; hammering a rate-limited endpoint is how an allowance is spent on
requests that cannot succeed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from .llm_errors import LlmFailure, classify
except ImportError:  # running as a top-level module, which is how the worker loads it
    from runtime.llm_errors import LlmFailure, classify

#: Consecutive failures before the circuit opens. Three is enough to distinguish
#: a real problem from one unlucky request, and few enough that a single blip
#: never silences the agent.
DEFAULT_FAILURE_THRESHOLD = 3

#: How long the circuit stays open. Long enough for a per-minute limit to clear,
#: short enough that the user is not left with a mute companion.
DEFAULT_COOLDOWN = 45.0

#: Said once when the circuit opens. Short, apologetic, and it does not promise
#: anything -- a second failure to speak would compound the problem.
APOLOGY = "Sorry, I'm having trouble right now. Give me a moment."


@dataclass
class FailureGate:
    """Counts consecutive failures and opens a circuit on the run of them."""

    threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown: float = DEFAULT_COOLDOWN
    #: Injected so tests do not sleep.
    clock: Callable[[], float] = time.monotonic
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _spoken: bool = field(default=False, init=False)

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def is_open(self) -> bool:
        """Whether attempts should be skipped right now."""
        if self._opened_at is None:
            return False
        if self.clock() - self._opened_at >= self.cooldown:
            # Cooldown elapsed: let the next turn try again.
            self.reset()
            return False
        return True

    @property
    def should_speak_apology(self) -> bool:
        """True exactly once per opening, so a retry storm does not talk over itself."""
        return self._failures >= self.threshold and not self._spoken and not self._spoken

    def record_failure(self, failure: LlmFailure) -> None:
        self._failures += 1
        if self._failures >= self.threshold and self._opened_at is None:
            self._opened_at = self.clock()
            self._spoken = False

    def mark_spoken(self) -> None:
        self._spoken = True

    def reset(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._spoken = False


def describe(failure: LlmFailure) -> dict[str, Any]:
    """The event payload for the desktop app.

    Carries the provider's own words for a diagnostics view and never for the
    user: a raw provider error can contain an account id or a request id, which
    is fine in a log and noise in a toast.
    """
    return {
        "kind": failure.kind,
        "message": failure.message,
        "retryable": failure.retryable,
        "retryAfter": failure.retry_after,
        "isLimit": failure.is_limit,
        "providerDetail": failure.provider_detail,
    }


def handle_llm_error(gate: FailureGate, error: Any) -> LlmFailure:
    """Classify, count, and report one LLM failure."""
    failure = classify(error)
    gate.record_failure(failure)
    return failure
