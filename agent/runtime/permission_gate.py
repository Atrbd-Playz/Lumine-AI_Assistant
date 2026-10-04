"""Session-bound approval for actions the user has not pre-allowed.

The rule this module implements: **reading is automatic, ordinary acting is
usually automatic, sensitive actions ask, destructive actions always ask --
and nothing can lower that floor.** Not the model (which could simply call the
tool again with a calmer description), not a settings value, not an approval
minted for a different conversation.

## Where the decision happens

LiveKit has no pre-execution "cancel this tool call" hook, so approval runs
*inside* the tool, before any side effect: the tool calls
:func:`require_approval`, which returns (allowed) or raises ``ToolError``
(refused), and only then does the tool act. A gate living in a prompt would be
a gate on the honor system, which is why enforcement is code.

## The two channels, and why each is trusted

- **UI card.** Published on the ``lumine.permission`` data topic. A response
  must carry the request's id: a random, single-use token minted at arm time
  and checked against *this session's* pending set. A response for another
  session's request, a replayed id, or a request already answered all fail to
  resolve and are audited as ignored. Ids are ``secrets.token_urlsafe``: the
  model never sees one, and could not deliver one anyway -- tool results flow
  *toward* the model, never back.
- **Voice.** A final ``user_input_transcribed`` event during the request's
  pending window. That event is produced by this session's speech-to-text from
  the room's audio, and room access requires a token minted by the desktop
  app, so a final transcript is the authenticated local user speaking. Model
  text arrives on a different path (conversation items) and is never consulted.
  The transcript must *match* an approval or refusal phrase; anything
  ambiguous resolves nothing and the request expires to a refusal.

## Failure is a refusal

Every ambiguity collapses to "not allowed":

- no answer before the deadline -> refused (``asyncio.wait_for`` bound)
- session or room closes mid-ask -> refused (:meth:`PermissionGate.close`
  wakes every waiter with a refusal)
- the UI packet never arrived -> the wait still has a deadline; voice can
  still answer, and silence still refuses
- a second identical request while the first is still pending -> refused
  (``MSG_DUPLICATE``), because one action deserves one question and one
  approval, and a duplicate card the user answers twice would run a
  non-idempotent action off a single "yes"
- an unknown policy value -> treated as ``ask``, never as ``auto``
- an unknown action class -> treated as ``sensitive``

## Approvals cannot outlive their session

Pending requests live in the gate, the gate lives in a registry keyed by the
session object, and ``release_gate`` removes both at session end. Nothing is
persisted: a restarted worker starts empty, so an id minted before a restart
cannot be answered afterwards. Expiry is re-checked when a decision arrives,
so a late packet after the deadline records "expired" instead of approving.

## What this module deliberately does not do

It does not approve *for* the user, does not remember a decision for the next
call (each call asks again; "always allow" is a settings change, never a
runtime shortcut), and does not retry anything. A send whose outcome is
unknown is recorded as uncertain by the tool that sent it and must not be
retried -- see ``tools.messaging.whatsapp``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

try:
    from ..settings.config_store import effective_document
    from ..tools.tools_compat import ToolError
    from . import audit_log
    from .latency import tracker_for as latency_tracker_for
    from .voice_line import GENERATED, SAID, SILENT, speak_line, spoken_via_label
except ImportError:  # running as a top-level script with agent/ on sys.path
    from settings.config_store import effective_document
    from tools.tools_compat import ToolError
    from runtime import audit_log
    from runtime.latency import tracker_for as latency_tracker_for
    from runtime.voice_line import GENERATED, SAID, SILENT, speak_line, spoken_via_label

try:
    from livekit import rtc as _rtc

    _AGENT_KIND: Any = _rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
except Exception:  # pragma: no cover - livekit-agents absent (tests and tooling)
    _AGENT_KIND = "agent"

logger = logging.getLogger("lumine")

#: The data topic the approval card travels on, and the topic its answer
#: travels back on. Separate from ``lumine.notice``/``lumine.tool`` so a card
#: can never be confused with a status line.
TOPIC = "lumine.permission"

#: The classes an action can have, mildest first. A tool declares its own
#: class in code, at the call site -- a value the model never touches, which is
#: the whole point: a model that could relabel "close this window" as *reading*
#: would not need to ask.
ACTION_CLASSES = ("read", "act", "sensitive", "destructive")

#: The handling each class gets by default, and the floor nothing may lower.
#: ``sensitive`` and ``destructive`` both land on ``ask``; ``destructive``
#: additionally requires the action to be read back (tools spell the summary
#: out) and can never be reached by an ``auto`` policy value.
CLASS_FLOOR: dict[str, str] = {
    "read": "auto",
    "act": "auto",
    "sensitive": "ask",
    "destructive": "ask",
}

#: Policy values a settings file or ``LUMINE_PERMISSIONS`` may use.
#: ``auto`` is an accepted spelling of ``default``: it means "the class
#: floor", so writing it for a sensitive tool does not -- cannot -- downgrade
#: that tool to automatic.
VALID_LEVELS = ("default", "auto", "ask", "off")

HANDLING_AUTO = "auto"
HANDLING_ASK = "ask"
HANDLING_OFF = "off"

PERMISSIONS_VAR = "LUMINE_PERMISSIONS"
PERMISSION_TIMEOUT_VAR = "LUMINE_PERMISSION_TIMEOUT"

#: Whether Lumine *speaks* the question as well as showing the card.
#:
#: On by default, and the reasoning is short: this is a voice conversation, and a
#: question that only exists as a rectangle in the corner of a window is a question
#: a person talking to Lumine will not notice. The card stays -- it is what carries
#: the exact recipient and wording, which a sentence cannot -- and the spoken part
#: carries only the question.
#:
#: Set to ``false`` to go quiet, for someone who prefers the card alone. Turning it
#: off does not change *whether* an action is asked for, only whether it is
#: announced; the gate still blocks and still refuses on silence.
VOICE_APPROVAL_VAR = "LUMINE_VOICE_APPROVAL"

#: The longest question that will be spoken. A read-back that runs to a paragraph
#: is not a question anybody answers; the card shows the detail regardless.
MAX_SPOKEN_QUESTION = 160

#: Spoken after every action, so a bare "yes" or "no" is a complete answer. A
#: question with no such ending ("Shall I send it?") invites "what?" back, which
#: decides nothing and costs the user the whole minute.
_QUESTION_SUFFIX = ". Shall I go ahead?"

#: How long an unanswered card waits. Sixty rather than thirty because this is
#: a real person's decision, taken on a device they may have put down: half a
#: minute is what a voice conversation loses to a question, but a minute is
#: still short enough that silence reads as absence rather than as the app
#: having frozen -- and the card shows the countdown either way, so the number
#: is legible rather than a deadline guessed at.
DEFAULT_TIMEOUT_SECONDS = 60.0
MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 120.0

# Model-facing refusals. ToolError text goes to the model, which is expected to
# relay it; each names what did *not* happen, because "permission denied" alone
# invites the model to try a cleverer phrasing of the same request.
MSG_NO_GATE = "This action needs approval, and this run has no approval channel. Nothing was done."
MSG_NO_USER = "Nobody is here to approve that right now. Nothing was done."
MSG_DENIED = "The user declined this action."
MSG_EXPIRED = "Permission was not given in time. Nothing was done."
MSG_CANCELLED = "The session ended before permission was given. Nothing was done."
MSG_OFF = "This action is not allowed: the tool is switched off in configuration."
#: A second identical ask while the first is still on screen. Not a second
#: card: two cards for one action is a question the user has to answer twice,
#: and answering "yes" to both would run a non-idempotent action -- a send, a
#: force-kill -- twice off one decision.
MSG_DUPLICATE = (
    "The same action is already waiting for approval, so it was not asked for a "
    "second time. The original request is still on screen and will run if it is "
    "approved; this call did nothing."
)


# ---------------------------------------------------------------------------
# Pure policy rules
# ---------------------------------------------------------------------------


def normalize_level(level: object) -> str:
    """A policy value, fail-closed.

    Unknown spellings become ``ask`` rather than being ignored: a typo in a
    line that was meant to say ``off`` must not silently produce *automatic*.
    The cost of being wrong this way is an extra question; the cost of the
    other way is an action nobody meant to allow.
    """
    text = str(level or "").strip().lower()
    if text in ("default", "auto"):
        return "default"
    if text == "ask":
        return "ask"
    if text == "off":
        return "off"
    return "ask"


def effective_handling(action_class: object, level: object = "default") -> str:
    """``auto``, ``ask``, or ``off`` for one action under one policy level.

    The floor is applied *after* the level, and the level can only raise it:
    ``default``/``auto`` yield the floor, ``ask`` forces a question even for a
    read, ``off`` stops the tool entirely. An unknown action class lands on
    ``ask`` because mislabelling something milder is the dangerous direction.
    """
    handling = normalize_level(level)
    if handling == HANDLING_OFF:
        return HANDLING_OFF
    floor = CLASS_FLOOR.get(str(action_class or "").strip().lower(), HANDLING_ASK)
    if handling == HANDLING_ASK:
        return HANDLING_ASK
    return floor


def clamp_timeout(value: object) -> float:
    """Seconds to wait for an answer, inside 5..120."""
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, seconds))


def parse_level_map(raw: object) -> dict[str, str]:
    """Parse ``{key: level}`` from a document section.

    A key is either a tool (``open_app``) or one verb of it
    (``manage_window.close``); :meth:`PermissionGate.level_for` consults the
    verb first and falls back to the tool, so naming one action tightens that
    action alone and leaves the rest of the tool's verbs on their own terms.

    Entries with an unusable level are kept as ``ask`` rather than dropped --
    see :func:`normalize_level`. A key with no usable name is dropped, because
    there is no tool to apply it to.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for ident, level in raw.items():
        name = str(ident or "").strip().lower()
        if name:
            out[name] = normalize_level(level)
    return out


def env_permission_levels() -> dict[str, str]:
    """``LUMINE_PERMISSIONS`` as ``{key: level}``.

    The env form is a comma-separated list: ``open_app=ask,browse=off``. Keys
    may name one verb of a tool as well, with the same precedence a settings
    document gets: ``LUMINE_PERMISSIONS=manage_window.close=ask``.
    """
    raw = os.getenv(PERMISSIONS_VAR, "") or ""
    out: dict[str, str] = {}
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        name, sep, value = entry.partition("=")
        name = name.strip().lower()
        if not name:
            continue
        out[name] = normalize_level(value if sep else None)
    return out


def env_permission_timeout() -> float:
    """``LUMINE_PERMISSION_TIMEOUT`` as seconds, clamped."""
    raw = (os.getenv(PERMISSION_TIMEOUT_VAR, "") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    return clamp_timeout(raw)


def load_policy(document: Mapping[str, Any] | None = None) -> tuple[dict[str, str], float]:
    """The effective ``(levels, timeout)`` for one session.

    Precedence mirrors the settings document everywhere else: the saved
    document wins *per key*, the environment is the base underneath it. A
    document without a ``permissions`` section changes nothing, so a plain
    voice profile does not silently drop the environment's toggles.
    """
    levels = dict(env_permission_levels())
    timeout = env_permission_timeout()
    if document is None:
        document, _source, _diagnostics = effective_document()
    section = document.get("permissions") if isinstance(document, Mapping) else None
    if isinstance(section, Mapping):
        levels.update(parse_level_map(section.get("tools")))
        if section.get("timeout") is not None:
            timeout = clamp_timeout(section.get("timeout"))
    return levels, timeout


# ---------------------------------------------------------------------------
# Voice consent vocabulary
# ---------------------------------------------------------------------------
#
# Matching is exact against a normalized transcript, plus a small tail rule
# for "yes, please"-shaped answers. Refusals additionally match on their first
# word ("stop it", "no way"), because refusing when the user was agreeing
# costs a question, while approving when they were refusing costs the action
# itself. Anything else -- "hmm", "maybe later", "are you sure?" -- resolves
# nothing, and the request expires to a refusal.

_APPROVE_PHRASES = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "fine",
        "go ahead",
        "go for it",
        "do it",
        "do that",
        "please do",
        "of course",
        "sure thing",
        "yes please",
        "yeah please",
        "approve",
        "approved",
        "confirm",
        "confirmed",
        "proceed",
        "send it",
        "absolutely",
        "certainly",
        "thats right",
        "correct",
    }
)

#: First words that mean yes, when followed by one of these (possibly empty)
#: continuations. The tail list is deliberately closed: "yes, but later" and
#: "yeah I guess" resolve nothing, because approving a deferred answer is
#: approving something the user did not say.
_APPROVE_LEADS = frozenset({"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "fine"})
_APPROVE_TAILS = frozenset(
    {
        "",
        "please",
        "do it",
        "do that",
        "go ahead",
        "go for it",
        "proceed",
        "approve",
        "approved",
        "approve it",
        "confirm",
        "confirmed",
        "send it",
        "yes",
        "sure",
        "ok",
        "okay",
        "absolutely",
        "certainly",
        "thats right",
        "correct",
    }
)

_DENY_PHRASES = frozenset(
    {
        "no",
        "nope",
        "nah",
        "stop",
        "cancel",
        "abort",
        "wait",
        "not now",
        "deny",
        "denied",
        "no thanks",
        "no thank you",
        "do not",
        "dont",
        "dont do it",
        "do not do it",
        "leave it",
        "never mind",
        "nevermind",
        "absolutely not",
        "no way",
        # Withdrawals. The user changing their mind a moment after saying yes is
        # a normal thing to do, and it has to be answerable -- before the action
        # begins, which is what these land inside.
        "withdraw",
        "withdrawn",
        "undo",
        "take it back",
        "i changed my mind",
        "changed my mind",
        "forget it",
        "not yet",
        "not right now",
    }
)

#: Phrases that answer the question the gate actually spoke. "shall I go ahead?"
#: invites "go ahead" and "yes", and both are already covered; these are the ones
#: a speech recogniser produces when the user answers the *sentence* rather than
#: the yes/no question.
_APPROVE_PHRASES |= frozenset(
    {
        "go ahead",
        "yes go ahead",
        "yes please go ahead",
        "send it",
        "send the message",
        "yes send it",
        "do it now",
        "yes do it now",
        "yes you can",
        "you can send it",
        "go on then",
        "alright do it",
        "okay do it",
        "ok do it",
        "yes ok",
        "yes okay",
        "affirmative",
        "sounds good",
        "that works",
    }
)

#: First words that mean no, whatever follows. Broader than the approve side
#: on purpose; see the note above the vocabularies.
_DENY_LEADS = frozenset({"no", "nope", "nah", "stop", "cancel", "abort", "wait", "dont", "never", "not"})

_WORD = re.compile(r"[^a-z0-9\s]+")


def normalize_transcript(text: object) -> str:
    """Lowercase, drop punctuation and apostrophes, collapse whitespace."""
    lowered = str(text or "").lower().replace("'", "").replace("\u2019", "")
    return " ".join(_WORD.sub(" ", lowered).split())


def match_voice(text: object) -> str | None:
    """A final transcript as ``"approve"``, ``"deny"``, or ``None``.

    ``None`` means "this utterance decides nothing" -- the request keeps
    waiting and will expire to a refusal if nothing else arrives.
    """
    normalized = normalize_transcript(text)
    if not normalized:
        return None
    if normalized in _APPROVE_PHRASES:
        return "approve"
    if normalized in _DENY_PHRASES:
        return "deny"
    head, _, rest = normalized.partition(" ")
    if head in _DENY_LEADS:
        return "deny"
    if head in _APPROVE_LEADS and rest in _APPROVE_TAILS:
        return "approve"
    return None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass
class PermissionRequest:
    """One pending ask, and everything needed to answer it exactly once."""

    id: str
    tool: str
    action: str
    action_class: str
    summary: str
    target: str
    fingerprint: str
    created_at: float
    expires_at: float
    state: str = "pending"
    future: asyncio.Future[str] | None = field(default=None, repr=False)


#: Answers that are *not* decisions. Someone who says "are you sure?" or "what
#: exactly?" has not agreed to anything, and treating the question as approval is
#: the single worst failure this matcher can have. These resolve nothing, so the
#: request keeps waiting and expires to a refusal if nothing clearer arrives.
NOT_DECISIONS: frozenset[str] = frozenset(
    {
        "what",
        "what exactly",
        "are you sure",
        "are you sure that is safe",
        "why",
        "why are you asking",
        "hold on",
        "one moment",
        "wait a second",
        "i am not sure",
        "not sure",
        "hmm",
        "um",
        "uh",
        "maybe",
        "maybe later",
        "later",
        "in a minute",
        "what will it do",
        "which one",
        "explain",
    }
)


def is_decision(text: object) -> bool:
    """Whether a transcript is clearly *not* an answer to the question.

    Checked before :func:`match_voice` in the same place, because the ordering is
    the safety property: a hedge must never reach the approve matcher, where a
    longer phrase table would eventually match something in it.
    """
    return normalize_transcript(text) in NOT_DECISIONS


def _speech_handle() -> Callable[..., Any]:
    """The ``SpeechHandle`` class, from wherever this SDK version keeps it.

    Two import paths because the pinned SDK moved it: ``livekit.agents`` no longer
    re-exports ``speech_handle``, it lives under ``livekit.agents.voice``. Only
    reached on the legacy ``say(handle)`` signature -- the current one takes text --
    and a failure here is swallowed by the caller, because a session that cannot
    speak a question still gets the card.
    """
    try:
        from livekit.agents import speech_handle as module  # type: ignore

        return module.SpeechHandle
    except ImportError:
        from livekit.agents.voice import speech_handle as module  # type: ignore

        return module.SpeechHandle


class PermissionGate:
    """Approvals for one session. Not shared between sessions, ever.

    The session object is the boundary: ``resolve_ui`` only knows ids that
    were minted *here*, so an answer captured in conversation A cannot be
    replayed into conversation B, and ``close`` refuses everything still
    waiting when the session ends.

    **Voice is the primary channel here.** When a request is armed, the gate
    speaks the question (:meth:`_announce`) in addition to publishing the card,
    because this is a voice conversation and a card in the corner of a window is
    something a person will not notice. The card remains the channel that carries
    the *exact* recipient and wording -- the two things a sentence cannot safely
    paraphrase -- and either channel resolves the same request id, so answering
    twice still runs the action once.
    """

    def __init__(
        self,
        session: Any,
        *,
        room: Any = None,
        publisher: Any = None,
        policy: Mapping[str, str] | None = None,
        timeout: float | None = None,
        label: str = "",
    ) -> None:
        self.session = session
        self.room = room
        self.publisher = publisher
        self.policy = {str(k).lower(): v for k, v in (policy or {}).items()}
        self.timeout = float(timeout) if timeout else DEFAULT_TIMEOUT_SECONDS
        self.label = label or str(getattr(room, "name", "") or "")
        self._pending: dict[str, PermissionRequest] = {}
        self._order: list[str] = []
        self._closed = False
        self._unsubscribe: Callable[[], None] | None = None
        self._subscribe_voice()

    # -- wiring -------------------------------------------------------------

    def _subscribe_voice(self) -> None:
        on = getattr(self.session, "on", None)
        if not callable(on):
            return
        try:
            unsubscribe = on("user_input_transcribed", self._on_user_transcript)
            if callable(unsubscribe):
                self._unsubscribe = unsubscribe
        except Exception:  # noqa: BLE001 - a stub session in a test may refuse
            logger.debug("[Permission] no voice consent channel on this session", exc_info=True)

    def _has_user(self) -> bool:
        """Whether anyone could answer at all.

        Fail closed: a room with no known non-agent participant means no card
        can be pressed and no transcript will arrive, so arming would only run
        a clock down. Unknown participant kinds do not count as users.
        """
        remote = getattr(self.room, "remote_participants", None) or {}
        values = remote.values() if hasattr(remote, "values") else []
        for participant in values:
            kind = getattr(participant, "kind", None)
            if kind is not None and kind != _AGENT_KIND:
                return True
        return False

    # -- the tool-facing entry point ---------------------------------------

    def level_for(self, tool: str, action: str) -> str:
        """The policy level for one *verb*, falling back to the whole tool.

        This is what makes ``manage_window.close=ask`` mean something: it puts
        the question back on a verb that is otherwise an ordinary act, while
        the tool-level entry keeps governing every verb it does not name. One
        action can therefore be tightened without rewriting the rest of the
        tool's policy or quietly relaxing them, and a key nobody ever wrote
        lands on ``default`` -- the class floor -- never on anything milder.
        """
        verb = f"{str(tool).strip().lower()}.{str(action).strip().lower()}"
        if verb in self.policy:
            return self.policy[verb]
        return self.policy.get(str(tool).strip().lower(), "default")

    async def require(
        self,
        *,
        tool: str,
        action: str,
        action_class: str,
        summary: str,
        target: str = "",
    ) -> None:
        """Return when the action may proceed; raise ``ToolError`` when not."""
        level = self.level_for(tool, action)
        handling = effective_handling(action_class, level)
        fingerprint = audit_log.digest(summary, target)
        common = {
            "session": self.label,
            "tool": tool,
            "action": action,
            "action_class": action_class,
            "target": target,
            "digest": fingerprint,
        }

        if handling == HANDLING_OFF:
            audit_log.record("decision", decision="refused", channel="policy", detail="tool is off", **common)
            raise ToolError(MSG_OFF)
        if handling == HANDLING_AUTO:
            # Nothing to ask, nothing to record: the audit log is decisions a
            # person made, and an automatic launch is not one. Logging every
            # read would also make every test run write into a real user's
            # audit file.
            return

        if self._closed:
            audit_log.record("decision", decision="refused", channel="session", detail="gate closed", **common)
            raise ToolError(MSG_CANCELLED)
        if not self._has_user():
            audit_log.record("decision", decision="refused", channel="session", detail="no user in the room", **common)
            raise ToolError(MSG_NO_USER)

        # Asked last, because it is about the pending set: the checks above
        # answer "may anything be asked at all", and this one answers "is the
        # question already on screen".
        twin = self._pending_identical(tool, action, fingerprint)
        if twin is not None:
            audit_log.record(
                "decision",
                decision="refused",
                channel="policy",
                detail=f"duplicate of a request still waiting ({twin.id[:8]})",
                **common,
            )
            raise ToolError(MSG_DUPLICATE)

        request = self._arm(tool=tool, action=action, action_class=action_class, summary=summary, target=target)
        published = await self._publish_request(request)
        # Announced *after* the card is published, so the spoken question can
        # never arrive before the thing it is asking about is on screen. Both are
        # best-effort and neither can refuse: the wait still has a deadline.
        await self._announce(request)
        audit_log.record(
            "armed",
            session=self.label,
            tool=tool,
            action=action,
            action_class=action_class,
            summary=summary,
            target=target,
            digest=request.fingerprint,
            request_id=request.id,
            timeout=round(self.timeout, 1),
            published=published,
        )

        remaining = max(0.0, request.expires_at - time.time())
        try:
            outcome = await asyncio.wait_for(request.future, timeout=remaining)
        except asyncio.TimeoutError:
            if request.state == "pending":
                self._finish(request, "expired", channel="deadline", detail="no answer before the deadline")
            raise ToolError(MSG_EXPIRED)

        if outcome == "approved":
            return
        if outcome == "expired":
            raise ToolError(MSG_EXPIRED)
        if outcome == "cancelled":
            raise ToolError(MSG_CANCELLED)
        raise ToolError(MSG_DENIED)

    # -- arming and resolving ----------------------------------------------

    def _arm(
        self, *, tool: str, action: str, action_class: str, summary: str, target: str
    ) -> PermissionRequest:
        """Mint a pending request. Must be called from inside the loop.

        The future is bound to the running loop on purpose: a request's answer
        arrives as a future result, and a future created on a different loop can
        never be woken by it.
        """
        now = time.time()
        loop = asyncio.get_running_loop()
        request = PermissionRequest(
            id=secrets.token_urlsafe(24),
            tool=tool,
            action=action,
            action_class=action_class,
            summary=summary,
            target=target,
            fingerprint=audit_log.digest(summary, target),
            created_at=now,
            expires_at=now + self.timeout,
            future=loop.create_future(),
        )
        self._pending[request.id] = request
        self._order.append(request.id)
        return request

    def _pending_identical(self, tool: str, action: str, fingerprint: str) -> PermissionRequest | None:
        """A request with the same identity that is still waiting, if any.

        Identity is (tool, verb, what it would do) -- the same triple the card
        on screen shows. The match disappears the moment one of them is
        answered, because each call asks again: this suppresses a
        *simultaneous* duplicate, never a later repetition of a decision the
        user has already made one way or the other.
        """
        wanted_tool = str(tool).strip().lower()
        wanted_action = str(action).strip().lower()
        for ident in self._order:
            request = self._pending.get(ident)
            if (
                request is not None
                and request.state == "pending"
                and str(request.tool).strip().lower() == wanted_tool
                and str(request.action).strip().lower() == wanted_action
                and request.fingerprint == fingerprint
            ):
                return request
        return None

    def _ui_record(self, request: PermissionRequest) -> dict[str, Any]:
        """What the card carries.

        ``question`` is the *same* sentence :meth:`spoken_question` builds, so the
        card and the voice agree word for word. A card that phrased the action
        differently from the spoken question would let a user answer "yes" to one
        and believe they had agreed to the other -- which is the failure this
        product cannot afford on the one screen where the user is deciding whether
        Lumine may act in their name.
        """
        return {
            "type": "permission_request",
            "id": request.id,
            "tool": request.tool,
            "action": request.action,
            "action_class": request.action_class,
            "summary": request.summary,
            "target": request.target,
            "question": self.spoken_question(request),
            "expires_at": request.expires_at,
        }

    async def _publish_request(self, request: PermissionRequest) -> bool:
        """Put the card on the data channel. True when it actually left.

        Also echoed to stdout for diagnostics, through the publisher's
        write-safe path. A failed publish does not itself refuse: voice is an
        independent consent channel and the wait still has a deadline, so
        silence after a failed publish refuses exactly as silence after a
        successful one.
        """
        record = self._ui_record(request)
        if self.publisher is not None:
            try:
                self.publisher.emit_record(dict(record))
            except Exception:  # noqa: BLE001 - telemetry never breaks an arm
                logger.debug("[Permission] stdout echo failed", exc_info=True)
        participant = getattr(self.room, "local_participant", None)
        if participant is None:
            return False
        try:
            await participant.publish_data(
                json.dumps(record, default=str, separators=(",", ":")),
                reliable=True,
                topic=TOPIC,
            )
            return True
        except Exception:  # noqa: BLE001 - reported, then left to the deadline
            logger.warning("[Permission] could not publish the approval card", exc_info=True)
            return False

    # -- the spoken question --------------------------------------------------

    def voice_approval_enabled(self) -> bool:
        """Whether an armed request is announced out loud.

        Read per arm rather than at construction, so a settings change takes
        effect on the next question instead of the next session.
        """
        raw = (os.getenv(VOICE_APPROVAL_VAR, "") or "").strip().lower()
        return raw not in ("0", "false", "no", "off")

    #: Set when an armed request could only be announced on the card. Read by
    #: diagnostics and emitted on the request record, because "she said nothing"
    #: and "she spoke and you did not hear it" need different answers and the
    #: only evidence is whether the speech channel was even available.
    last_announce_failed: bool = False

    #: Which channel carried the last question -- one of `voice_line.SAID`,
    #: `GENERATED` or `SILENT`. Kept next to `last_announce_failed` because the
    #: two answer different questions: a failed announcement is a bug to fix, and
    #: a question carried by the model rather than a synthesiser is a property of
    #: the profile the user chose.
    last_announce_channel: str = SILENT

    async def _announce(self, request: PermissionRequest) -> None:
        """Speak the question -- and keep trying until one channel carries it.

        Built from the *tool's own* summary, which the tool wrote in code with the
        class and target already in it, so the *card* is never model-authored.

        The question itself goes through :func:`voice_line.speak_line`, which is
        the whole point. This used to call ``say()`` and swallow the failure, and
        on the default profile that swallowed **every** approval question: a
        realtime model has no TTS stage, so ``say()`` raises, and the result was
        a card appearing in the corner of a window with no voice, no hint that an
        answer was expected, and sixty seconds of silence -- and silence is a
        denial. The user was asked to consent to something they had only been
        shown.

        So the escalation is explicit: ``say()`` first (verbatim, on a profile
        with a TTS), then the realtime model asked to say it (audible, wording not
        guaranteed, which is exactly why the card stays authoritative), and only
        then a published notice. :data:`last_announce_channel` records which one
        carried it, because "she said nothing" and "she spoke and you did not hear
        it" need different answers.
        """
        self.last_announce_failed = False
        self.last_announce_channel = SILENT
        if not self.voice_approval_enabled():
            return
        channel = await speak_line(
            self.session,
            self.spoken_question(request),
            # An approval question that can be cut off mid-sentence is an approval
            # that is sometimes never heard, and silence refuses.
            allow_interruptions=False,
        )
        self.last_announce_channel = channel
        self._publish_announce_channel(request, channel)
        if channel == SILENT:
            self.last_announce_failed = True
            logger.warning(
                "[Permission] no audio channel for the approval question (%s). The "
                "card is the only channel. A realtime profile speaks through the "
                "model; a profile with a TTS stage speaks it verbatim.",
                spoken_via_label(channel),
            )
            await self._announce_failed_notice(request)
        else:
            logger.info(
                "[Permission] approval question spoken via %s", spoken_via_label(channel)
            )

    def _publish_announce_channel(self, request: PermissionRequest, channel: str) -> None:
        """Tell the app which channel carried the question, after the fact.

        A separate record rather than a field on the card, because of the order:
        the card is published *first* on purpose -- the spoken question must never
        arrive before the thing it asks about -- so at that moment the channel is
        not yet known. Publishing it afterwards is the only way to report it
        without either lying (`"none"`, because nothing had been attempted yet) or
        reordering the two channels for the sake of a diagnostic.

        The app needs it: a question carried by the model rather than a
        synthesiser is normal on this profile, and a question carried by nothing
        at all means the user is looking at a card nobody told them to answer.
        """
        if self.publisher is None:
            return
        try:
            self.publisher.emit(
                "permission_announced",
                id=request.id,
                tool=request.tool,
                action=request.action,
                spoken_via=spoken_via_label(channel),
                audible=channel != SILENT,
            )
        except Exception:  # noqa: BLE001 - a diagnostic must never break a gate
            logger.debug("[Permission] the announce record did not send", exc_info=True)

    async def _announce_failed_notice(self, request: PermissionRequest) -> None:
        """Tell the user the question is on the card only, and that voice still answers.

        This is the difference between a silent card and a usable one. The
        *answer* path is unaffected -- `user_input_transcribed` comes from this
        session's speech-to-text and does not need a synthesiser -- so a user who
        is told "say yes or no out loud" can act, where a user shown a silent
        card has no idea an answer is expected of them.

        Best-effort like everything else here: a notice that cannot be published
        is a card that stays silent, which is where we started.
        """
        if self.publisher is None:
            return
        try:
            self.publisher.emit_and_publish(
                "lumine.notice",
                {
                    "type": "info",
                    "title": "Waiting for your yes or no",
                    "message": (
                        "I couldn't speak this one -- it's on the card instead. "
                        "You can still just say yes or no."
                    ),
                },
            )
        except Exception:  # noqa: BLE001 - a courtesy notice must never break a gate
            logger.debug("[Permission] the announce-failed notice did not send", exc_info=True)

    @staticmethod
    def spoken_question(request: PermissionRequest) -> str:
        """The sentence Lumine says.

        One short question naming the action and its consequence, ending so that a
        bare "yes" or "no" is a complete answer. The detail the card shows -- the
        full target, the exact wording -- is deliberately *not* read out: a
        read-back long enough to paraphrase is a read-back that can be misheard,
        and the card is right there with the exact text.
        """
        summary = " ".join(str(request.summary or "").split())
        if not summary:
            summary = "do that"
        # The suffix is part of the budget, not an addition to it: a read-back
        # that runs long stops being a sentence anybody answers.
        room = MAX_SPOKEN_QUESTION - len(_QUESTION_SUFFIX)
        if len(summary) > room:
            summary = summary[: max(0, room - 1)].rstrip() + "…"
        return f"{summary}{_QUESTION_SUFFIX}"

    def _finish(
        self,
        request: PermissionRequest,
        outcome: str,
        *,
        channel: str,
        sender: str = "",
        detail: str = "",
    ) -> bool:
        """Resolve a request once. Returns whether this call was the one."""
        common = {
            "session": self.label,
            "tool": request.tool,
            "action": request.action,
            "action_class": request.action_class,
            "target": request.target,
            "digest": request.fingerprint,
            "request_id": request.id,
            "channel": channel,
            "sender": sender,
        }
        if request.state != "pending":
            audit_log.record(
                "ignored",
                detail=f"already {request.state}{': ' + detail if detail else ''}",
                **common,
            )
            return False
        answered_after_deadline = outcome != "expired" and time.time() > request.expires_at
        if answered_after_deadline:
            outcome, detail = "expired", "answered after the deadline"

        request.state = outcome
        if request.id in self._order:
            self._order.remove(request.id)
        self._pending.pop(request.id, None)

        if request.future is not None and not request.future.done():
            # "expired" is delivered to the waiter as a refusal as well; the
            # waiter's own message never claims approval.
            request.future.set_result(outcome if outcome in ("approved", "denied", "cancelled") else "denied")

        audit_log.record(
            "decision",
            decision=outcome,
            detail=detail,
            summary=request.summary,
            **common,
        )
        # The wait is latency the user feels directly: a card nobody answers
        # for the full deadline is a minute of a voice conversation doing
        # nothing, and before this row existed it was invisible -- the tool
        # simply "took a while". Measured on the wall clock the deadline was
        # set from, because `created_at` and `expires_at` are both wall-clock
        # and mixing clocks would make the wait disagree with the countdown
        # shown on the card.
        try:
            tracker = latency_tracker_for(self.session)
            if tracker is not None:
                tracker.emit_duration(
                    "permission_wait",
                    max(0.0, (time.time() - request.created_at) * 1000),
                    phase="permission",
                    tool=request.tool,
                    action=request.action,
                    action_class=request.action_class,
                    decision=outcome,
                    channel=channel,
                )
        except Exception:  # noqa: BLE001 - telemetry must never block a decision
            pass
        # "Counted" means a person's decision was applied. An answer that
        # arrived after the deadline settled the request but did not answer
        # it, and the caller is told so.
        return not answered_after_deadline

    def resolve_ui(self, request_id: object, decision: object, *, sender: str = "") -> bool:
        """Apply one answer from the desktop app. Returns whether it counted."""
        ident = str(request_id or "")
        request = self._pending.get(ident)
        if request is None:
            audit_log.record(
                "ignored",
                session=self.label,
                detail="unknown, replayed, or already answered request id",
                channel="ui",
                sender=sender,
                request_id=ident[:64],
            )
            return False
        normalized = str(decision or "").strip().lower()
        if normalized in ("approve", "approved", "allow", "yes"):
            outcome = "approved"
        elif normalized in ("deny", "denied", "refuse", "reject", "no"):
            outcome = "denied"
        else:
            audit_log.record(
                "ignored",
                session=self.label,
                detail=f"unusable decision {normalized[:32]!r}",
                channel="ui",
                sender=sender,
                request_id=ident,
            )
            return False
        return self._finish(request, outcome, channel="ui", sender=sender)

    def handle_channel_message(
        self,
        payload: Any,
        *,
        sender_kind: Any = None,
        sender_identity: str = "",
    ) -> bool:
        """Parse and apply one packet from ``TOPIC``. The trust boundary.

        Everything an untrusted sender can put in front of the gate is checked
        here: the sender must be a known non-agent participant (agents do not
        approve, and this session's own packets are not echoed back), the
        payload must parse, and the type must be the response type. The model
        has no way to publish on this topic -- it produces conversation items,
        not data packets.
        """
        if sender_kind is None or sender_kind == _AGENT_KIND:
            audit_log.record(
                "ignored",
                session=self.label,
                detail="sender is not a known user participant",
                channel="ui",
                sender=sender_identity,
            )
            return False
        if isinstance(payload, (bytes, bytearray)):
            try:
                text = bytes(payload).decode("utf-8")
            except UnicodeDecodeError:
                return False
        else:
            text = str(payload)
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("type") != "permission_response":
            return False
        return self.resolve_ui(data.get("id"), data.get("decision"), sender=sender_identity)

    # -- voice consent ------------------------------------------------------

    def _on_user_transcript(self, event: Any) -> None:
        """A final transcript, while something is waiting, may be the answer.

        Only this session's own speech-to-text produces this event, and only
        the room's participants (token-gated by the desktop app) can be heard
        by it -- which is what makes the speaker the authenticated local user
        rather than anything the model said. Non-final and non-matching
        transcripts decide nothing.

        A *hedge* also decides nothing, and that check comes first: "are you
        sure?" reaching an approve matcher that has grown a hundred phrases is
        how a question becomes consent.
        """
        if not self._pending or self._closed:
            return
        if not getattr(event, "is_final", False):
            return
        transcript = getattr(event, "transcript", "")
        if is_decision(transcript):
            logger.info("[Permission] heard a question, not an answer: %r", str(transcript)[:40])
            return
        decision = match_voice(transcript)
        if decision is None:
            return
        request = self._oldest_pending()
        if request is None:
            return
        self._finish(
            request,
            "approved" if decision == "approve" else "denied",
            channel="voice",
            sender=str(getattr(event, "speaker_id", "") or ""),
        )

    def _oldest_pending(self) -> PermissionRequest | None:
        for ident in self._order:
            request = self._pending.get(ident)
            if request is not None and request.state == "pending":
                return request
        return None

    # -- lifecycle ----------------------------------------------------------

    def close(self, reason: str = "session ended") -> None:
        """Refuse everything waiting, and refuse everything asked later.

        Idempotent: the session-close event and the shutdown callback both
        call this, and neither should double-report.
        """
        self._closed = True
        for request in list(self._pending.values()):
            self._finish(request, "cancelled", channel="session", detail=reason)
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # noqa: BLE001 - unsubscription must not raise
                logger.debug("[Permission] voice unsubscribe failed", exc_info=True)
            self._unsubscribe = None

    @property
    def pending_count(self) -> int:
        return sum(1 for request in self._pending.values() if request.state == "pending")


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------

#: Keyed by ``id(session)``. The gate holds the session itself, so an id cannot
#: be reused by a different session while its gate is registered.
_GATES: dict[int, PermissionGate] = {}


def acquire_gate(
    session: Any,
    *,
    room: Any = None,
    publisher: Any = None,
    label: str = "",
) -> PermissionGate:
    """Create this session's gate and register it.

    The policy is read here, once per session: the saved document wins over
    ``agent/.env`` exactly as the pipeline's own configuration does, and a
    settings change therefore takes effect on the next dispatch, not on the
    next tool call mid-conversation.
    """
    policy, timeout = load_policy()
    gate = PermissionGate(
        session,
        room=room,
        publisher=publisher,
        policy=policy,
        timeout=timeout,
        label=label,
    )
    previous = _GATES.get(id(session))
    if previous is not None:
        previous.close("gate re-acquired")
    _GATES[id(session)] = gate
    return gate


def gate_for(session: Any) -> PermissionGate | None:
    """The registered gate for a session, or ``None``.

    ``None`` means this run has no approval channel, and the callers treat
    that as a refusal for anything that would have had to ask.
    """
    if session is None:
        return None
    return _GATES.get(id(session))


def release_gate(session: Any) -> None:
    """Drop the session's gate, refusing anything still pending."""
    gate = _GATES.pop(id(session), None)
    if gate is not None:
        gate.close("session released")


async def require_approval(
    context: Any,
    *,
    tool: str,
    action: str,
    action_class: str,
    summary: str,
    target: str = "",
) -> None:
    """The call every side-effecting tool makes before it does anything.

    With a gate, the gate decides. Without one (a unit test, a script, a
    worker that never joined a room) an ``auto``-floor action still proceeds
    and anything that would have had to ask is refused -- there is nobody to
    ask, so the answer is no.
    """
    session = getattr(context, "session", None)
    gate = gate_for(session)
    if gate is None:
        handling = effective_handling(action_class, "default")
        if handling == HANDLING_AUTO:
            return
        audit_log.record(
            "decision",
            tool=tool,
            action=action,
            action_class=action_class,
            target=target,
            decision="refused",
            channel="policy",
            detail="no gate registered for this run",
        )
        raise ToolError(MSG_NO_GATE)
    await gate.require(
        tool=tool,
        action=action,
        action_class=action_class,
        summary=summary,
        target=target,
    )
