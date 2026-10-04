"""The one vocabulary for "did that actually happen?".

A tool call that returns without raising proves almost nothing. `subprocess.Popen`
returning means a process was *requested*; `os.startfile` returning means Windows
accepted a request; `locator.click()` resolving means Playwright dispatched a
mouse event, not that a page navigated; a WhatsApp send click means a button was
pressed, not that a message left the account. Each of those is a real thing that
really happened, and each is routinely reported as the next one up.

So every operation that changes something outside this process reports one of
these instead of a boolean:

===================  =====================================================
``requested``        We decided to do it. Nothing has left yet.
``dispatched``       A call was made against the OS, a page, or an API.
``accepted``         The underlying system took the request (the API answered
                     2xx, the OS accepted the launch, the page dispatched the
                     event without refusing it).
``observed``         We read the state *afterwards* and it had changed.
``failed``           It did not happen, and we know why.
``timed_out``        We stopped waiting. The outcome may still be unknown.
``uncertain``        Something may have happened and we cannot tell.
===================  =====================================================

The ladder only ever goes *up* on evidence. ``observed`` implies the three below
it, because a post-hoc reading is evidence that the earlier stages happened. The
inverse is never assumed: ``dispatched`` never implies ``observed``.

`uncertain` is the load-bearing one. A tool that cannot tell must say so, because
"uncertain" is the only value that makes a retry a *decision* rather than an
accident -- and a retry after a send is how a person gets two messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

REQUESTED = "requested"
DISPATCHED = "dispatched"
ACCEPTED = "accepted"
OBSERVED = "observed"
FAILED = "failed"
TIMED_OUT = "timed_out"
UNCERTAIN = "uncertain"

#: The ladder, weakest first. Index doubles as evidence count.
LADDER: tuple[str, ...] = (REQUESTED, DISPATCHED, ACCEPTED, OBSERVED)

#: Outcomes that are *not* successes, and must never be summarised as one.
FAILURES: frozenset[str] = frozenset({FAILED, TIMED_OUT, UNCERTAIN})

#: Outcomes after which an externally visible effect may exist. A retry past one
#: of these has to be a human decision, not a retry.
AMBIGUOUS: frozenset[str] = frozenset({UNCERTAIN, TIMED_OUT})


def normalize(status: object) -> str:
    """A known status, or ``uncertain``.

    Unknown strings are ``uncertain`` rather than ``failed``: an unrecognised
    claim about an effect we may have produced must not read as "it did not
    happen".
    """
    text = str(status or "").strip().lower()
    return text if text in LADDER or text in FAILURES else UNCERTAIN


def at_least(status: object, floor: object) -> bool:
    """Whether ``status`` carries at least as much evidence as ``floor``."""
    wanted = normalize(floor)
    try:
        return LADDER.index(normalize(status)) >= LADDER.index(wanted)
    except ValueError:
        # A floor that is itself a failure is satisfied only by itself.
        return normalize(status) == wanted


def succeeded(status: object) -> bool:
    return normalize(status) == OBSERVED


def may_have_happened(status: object) -> bool:
    """Whether a retry after this status could duplicate something."""
    return normalize(status) in AMBIGUOUS


@dataclass(frozen=True)
class ActionOutcome:
    """One operation's result, in the vocabulary above.

    ``detail`` is the short human sentence and ``evidence`` is what was actually
    read. Both are clipped by the caller; neither is the same field, because
    "it worked" and "I saw the window appear" fail differently and the second is
    what a person needs when the first one is wrong.
    """

    action: str
    status: str
    detail: str = ""
    evidence: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.__dict__.get("status"), str):  # pragma: no cover
            object.__setattr__(self, "status", normalize(self.status))

    @property
    def ok(self) -> bool:
        return succeeded(self.status)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action, "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        if self.evidence:
            out["evidence"] = self.evidence
        if self.data:
            out["data"] = self.data
        return out


def observed(action: str, detail: str = "", *, evidence: str = "", **data: Any) -> ActionOutcome:
    return ActionOutcome(action, OBSERVED, detail, evidence, data)


def accepted(action: str, detail: str = "", **data: Any) -> ActionOutcome:
    return ActionOutcome(action, ACCEPTED, detail, "", data)


def dispatched(action: str, detail: str = "", **data: Any) -> ActionOutcome:
    return ActionOutcome(action, DISPATCHED, detail, "", data)


def failed(action: str, detail: str, **data: Any) -> ActionOutcome:
    return ActionOutcome(action, FAILED, detail, "", data)


def timed_out(action: str, detail: str, **data: Any) -> ActionOutcome:
    return ActionOutcome(action, TIMED_OUT, detail, "", data)


def uncertain(action: str, detail: str, **data: Any) -> ActionOutcome:
    return ActionOutcome(action, UNCERTAIN, detail, "", data)