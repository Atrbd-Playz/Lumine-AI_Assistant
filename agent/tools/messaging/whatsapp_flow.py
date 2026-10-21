"""Contact search, chat selection, and the draft-first send flow.

The existing send path (`tools/messaging/whatsapp.py` and its desktop adapter)
already gets the *safety* right: resolve before ask, ask before type, never retry
an unconfirmed send. What it does not do is the thing the user actually asked for
-- **find the contact by name**.

Today `open_chat(name)` searches the *rendered chat list* for a title equal to the
name. On a real account that list is the last few dozen conversations and nothing
else, so "find Afia" works only if Afia happens to be in the visible window. The
fix is not a bigger list: it is to drive WhatsApp's **own search box**, the same
control a person uses, and then read the filtered list.

## What this adds

1. **Search-then-read.** Type a query into the search control, wait for the list
   to actually change (the same "the filter must have arrived" rule the number
   search already uses), then read the candidates. The box is cleared afterwards
   in a ``finally``, because a filter left behind narrows every later lookup.
2. **Group awareness.** A group row carries its participants; a contact row does
   not. Both are distinguishable, and a group is *never* auto-selected when the
   user named one person -- asking is correct and picking is not.
3. **Active-chat verification.** After opening, re-read the conversation header
   and compare. "I clicked the row" is not "the right chat is open", and the second
   is the one that must gate a send.
4. **Draft-first, always.** Typing happens *after* the approval question in
   automation mode -- the existing contract -- and the recipient is re-verified
   both before typing and again after, because a chat list can change under us.

## Approval binding

`DraftApproval` binds a draft to a *recipient, a message body, a chat identity and
a request id*. Any change to the recipient or the text invalidates it. That is
what stops "yes, send it" from carrying over to a reworded message or a
differently-resolved contact, which is the failure mode a draft-first flow has if
it only checks "was there an approval".

Nothing here reads message history and nothing here claims access to WhatsApp's
contact database: every name comes from the interface the user's own account
shows, and contact names are the only thing that ever leaves it.

## What is and is not on the send path

`ChatCandidate`, `ContactChoice`, `choose`, `match` and `is_group` are in daily
use -- they are how a spoken name becomes one chat.

`DraftApproval`, `DraftState`, `ContactFlow` and `FlowError` **are not.** They
were written for this flow before it had a Web path, and they remain here as
tested, documented logic, but nothing in `messaging.whatsapp` calls them. The
running send path instead drafts through `Backend.draft` (which types the text
and *reads the composer back to confirm it took*), gates through
`permission_gate`, and verifies delivery through the backend's own observation.

That means the "an approval is bound to a recipient, a body and a chat identity"
rule now lives in three places: `DraftApproval.valid_for` here, the gate's
summary/target fingerprint, and the live header check inside `open_chat`. They
agree today. **Do not wire this module in alongside that path** -- a second
implementation of "may I send this" is precisely the drift this repository keeps
warning about, and it would be invisible until two of them disagreed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

try:
    from ..tools_compat import ToolError
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_compat import ToolError

# -- candidates ---------------------------------------------------------------


@dataclass(frozen=True)
class ChatCandidate:
    """One row in the chat list, as WhatsApp rendered it."""

    title: str
    kind: str = "contact"
    participants: tuple[str, ...] = ()
    #: Whether this row is the one currently open. Read, never assumed from the
    #: click that opened it.
    active: bool = False
    ref: str = ""

    @property
    def is_group(self) -> bool:
        return self.kind == "group" or len(self.participants) > 1

    @property
    def maybe_group(self) -> bool:
        """Whether this **could** be a group, including when we cannot tell.

        Distinct from :attr:`is_group` on purpose. WhatsApp no longer exposes a JID
        on a chat-list row, so "is this a group" is sometimes unanswerable -- and
        answering `False` because we could not look would quietly switch off the
        rule that a group is never silently substituted for a person. Unknown is
        treated as *possibly a group*, which asks the user instead of guessing.
        """
        return self.kind in ("group", "unknown") or len(self.participants) > 1

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"title": self.title, "kind": self.kind}
        if self.participants:
            out["participants"] = list(self.participants)
        if self.active:
            out["active"] = True
        if self.ref:
            out["ref"] = self.ref
        return out


def normalize_title(raw: object) -> str:
    return " ".join(str(raw or "").split())


def is_group(title: object, participants: Sequence[str] = ()) -> bool:
    """Whether a chat title denotes a group.

    A group has *participants*; a contact does not. WhatsApp does not prefix group
    names, so the name itself cannot tell us -- which is why the adapter reports
    participants rather than a guess from the string.
    """
    del title
    return len({normalize_title(item) for item in participants if normalize_title(item)}) > 1


#: Shortest token that may be matched approximately. Four characters is where a
#: single edit stops being a different word and starts being a typo -- below it,
#: "Me" is within one edit of most short names, and a send to the wrong person is
#: the most expensive mistake this product can make.
_FUZZY_MIN_TOKEN = 4

#: Word tokens, split on everything that is not a letter or a digit. Used instead
#: of ``str.split`` so "Jubayat-Reza" and "Jubayat Reza" are the same query.
_TOKEN_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT.split(text.casefold()) if token]


def _edit_distance(left: str, right: str) -> int:
    """Levenshtein distance, two rows at a time. Tokens are short; this is cheap."""
    if left == right:
        return 0
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        for j, b in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,        # delete
                    current[j - 1] + 1,     # insert
                    previous[j - 1] + (a != b),  # substitute
                )
            )
        previous = current
    return previous[-1]


def _same_word_spoken_two_ways(said: str, shown: str) -> bool:
    """Whether two tokens are one word, heard and written slightly differently.

    Deliberately unforgiving. One edit on a short token, two on a long one, and
    **every** word of the query has to find a partner -- so "Jubayat Reza" reaches
    "Jubayed Reza" while "Jubay" still reaches nothing.
    """
    if said == shown:
        return True
    if min(len(said), len(shown)) < _FUZZY_MIN_TOKEN:
        return False
    budget = 1 if min(len(said), len(shown)) <= 5 else 2
    return _edit_distance(said, shown) <= budget


def _every_word_matches(query: str, title: str) -> bool:
    """Whether every word of ``query`` is present in ``title``, approximately."""
    wanted = _tokens(query)
    if not wanted:
        return False
    have = _tokens(title)
    return all(any(_same_word_spoken_two_ways(word, other) for other in have) for word in wanted)


def match(query: object, candidates: Sequence[ChatCandidate], *, limit: int = 6) -> list[ChatCandidate]:
    """Chat rows whose title answers ``query``, best first.

    Four tiers, in order: exact, prefix, containment, and -- last -- the same
    words said slightly differently. Containment is what makes "Afia" find "Afia
    Rahman". Whitespace is collapsed so a name dictated with odd spacing matches.

    ## Why the fourth tier exists

    The three literal tiers all require the query to appear *character for
    character*. Speech recognition does not spell. The live account holds a contact
    named `Jubayed Reza`; the request said `Jubayat`; and the answer was "I couldn't
    read your chat list -- nothing matched" for somebody demonstrably on screen.

    It is last, and narrow, on purpose. A wrong recipient is the worst thing this
    product can do, so the fuzzy tier demands that **every** word of the query find
    a near-partner in the title, allows one or two edits depending on length, and
    refuses to look at tokens under four characters. It also still loses to
    containment, so a literal match is never displaced by a fuzzy one -- and
    :func:`choose` still asks when two rows tie, which a fuzzy tier makes more
    likely rather than less.
    """
    wanted = normalize_title(query).casefold()
    if not wanted:
        return []
    hits: list[tuple[int, int, ChatCandidate]] = []
    for position, candidate in enumerate(candidates):
        title = normalize_title(candidate.title).casefold()
        if title == wanted:
            tier = 0
        elif title.startswith(wanted):
            tier = 1
        elif wanted in title:
            tier = 2
        elif _every_word_matches(wanted, title):
            tier = 3
        else:
            continue
        hits.append((tier, position, candidate))
    hits.sort(key=lambda row: (row[0], row[1]))
    return [item for _tier, _position, item in hits[: max(0, limit)]]


@dataclass(frozen=True)
class ContactChoice:
    """Which chat was chosen, or why not."""

    chosen: ChatCandidate | None
    candidates: tuple[ChatCandidate, ...] = ()
    ambiguous: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.chosen is not None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"resolved": self.ok}
        if self.chosen is not None:
            out["chosen"] = self.chosen.to_dict()
        if self.ambiguous:
            out["ambiguous"] = True
            out["candidates"] = [item.title for item in self.candidates[:5]]
        if self.reason:
            out["reason"] = self.reason
        return out


def kind_for_row(row: object) -> str:
    """``"group"``, ``"contact"``, or ``"unknown"`` for one chat-list row.

    The third value is the point. WhatsApp no longer puts a ``data-id`` on a
    chat-list row, so the JID that used to say ``@g.us`` is gone and a group is
    often indistinguishable from a contact. Four call sites previously wrote
    ``"group" if row.get("group") else "contact"``, which turns *could not tell*
    into *definitely not a group* -- and that silently switched off the rule that
    a group is never substituted for a person, on exactly the builds where the rule
    matters most.

    So "unknown" is carried all the way through, and
    :attr:`ChatCandidate.maybe_group` treats it as a reason to ask.
    """
    if not isinstance(row, dict):
        return "unknown"
    if row.get("group") is True:
        return "group"
    try:
        if int(row.get("participants") or 0) > 1:
            return "group"
    except (TypeError, ValueError):
        pass
    if row.get("contact") is True:
        return "contact"
    if row.get("group") is False:
        return "contact"
    return "unknown"


def choose(
    query: object, candidates: Sequence[ChatCandidate], *, allow_group: bool = False
) -> ContactChoice:
    """Resolve a name to one chat, or explain the ambiguity.

    An exact single match is chosen. Anything else -- several partial matches, or
    one *group* when a person was named -- returns candidates and no choice. This
    is the step that stops "message Afia" from landing in "Afia, Rahim and 3
    others" because WhatsApp listed that row first.
    """
    wanted = normalize_title(query).casefold()
    if not wanted:
        return ContactChoice(None, reason="tell me who to message")
    hits = match(wanted, candidates)
    if not hits:
        return ContactChoice(None, reason="I couldn't find that chat in your WhatsApp")

    exact = [item for item in hits if normalize_title(item.title).casefold() == wanted]
    if len(exact) == 1:
        # `maybe_group`, not `is_group`. This build gives no JID on a chat row, so
        # "definitely not a group" is often only "we could not tell" -- and reading
        # that as a yes would let a group be chosen for a person without asking,
        # which is the one substitution this whole module exists to prevent.
        if exact[0].maybe_group and not allow_group:
            return ContactChoice(
                None,
                candidates=tuple(hits),
                ambiguous=True,
                reason=(
                    f"{exact[0].title!r} is a group"
                    if exact[0].is_group
                    else f"{exact[0].title!r} may be a group chat; tell me if you mean it or someone in it"
                ),
            )
        return ContactChoice(exact[0], candidates=tuple(hits))

    if len(exact) > 1:
        return ContactChoice(
            None, candidates=tuple(exact), ambiguous=True, reason="several chats have that exact name"
        )
    if len(hits) == 1:
        return ContactChoice(hits[0], candidates=tuple(hits))
    return ContactChoice(
        None,
        candidates=tuple(hits),
        ambiguous=True,
        reason="more than one chat matches that name",
    )


# -- the draft, and what may authorize it --------------------------------------


@dataclass(frozen=True)
class DraftApproval:
    """A prepared message, and the facts an approval is bound to.

    ``fingerprint`` is a digest over the recipient, the message and the chat
    identity. :meth:`valid_for` is what a caller must check immediately before
    sending: if the recipient, the text, or the active chat changed since the
    question was asked, the answer was given for something else, and the honest
    response is to ask again rather than to send.
    """

    recipient: str
    message: str
    chat_identity: str
    request_id: str
    granted_at: float = 0.0
    expires_at: float = 0.0

    def is_expired(self, now: float | None = None) -> bool:
        if not self.expires_at:
            return False
        return (time.monotonic() if now is None else now) >= self.expires_at

    def valid_for(
        self, recipient: object, message: object, chat_identity: object, *, now: float | None = None
    ) -> str:
        """``""`` when this approval may authorize that send, else why not."""
        if normalize_title(recipient).casefold() != self.recipient.casefold():
            return "the recipient changed"
        if " ".join(str(message or "").split()) != self.message:
            return "the message changed"
        if normalize_title(chat_identity).casefold() != self.chat_identity.casefold():
            return "the active chat is not the one you approved"
        if self.is_expired(now):
            return "the approval expired"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipient": self.recipient,
            "chat": self.chat_identity,
            "requestId": self.request_id,
            "grantedAt": round(self.granted_at, 1),
            "expiresAt": round(self.expires_at, 1),
        }


@dataclass
class DraftState:
    """The prepared draft, and whether it may still be sent.

    Held per session and cleared on *any* change. Keeping it here rather than in
    the tool means the invalidation rule is one method with one test, rather than
    a check somebody has to remember at four call sites.
    """

    recipient: str = ""
    message: str = ""
    chat_identity: str = ""
    request_id: str = ""
    prepared_at: float = 0.0
    approval: DraftApproval | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    def prepare(self, recipient: str, message: str, chat_identity: str, request_id: str) -> None:
        self.recipient = normalize_title(recipient)
        self.message = " ".join(str(message or "").split())
        self.chat_identity = normalize_title(chat_identity)
        self.request_id = str(request_id or "")
        self.prepared_at = time.monotonic()
        # A new draft invalidates the previous approval, unconditionally.
        self.approval = None

    def approve(self, approval: DraftApproval) -> None:
        if approval.recipient.casefold() != self.recipient.casefold():
            # An approval for a different recipient is not an approval for this
            # one, and binding it anyway is how a "yes" gets spent twice.
            self.approval = None
            return
        if approval.message != self.message:
            self.approval = None
            return
        self.approval = approval

    def may_send(self, *, now: float | None = None) -> str:
        """``""`` when the send is authorized; the refusal sentence otherwise."""
        if self.approval is None:
            return "the message was never approved"
        return self.approval.valid_for(
            self.recipient, self.message, self.chat_identity, now=now
        )

    def clear(self) -> None:
        self.recipient = self.message = self.chat_identity = self.request_id = ""
        self.approval = None
        self.notes.clear()


# -- the flow ------------------------------------------------------------------


class FlowError(Exception):
    """A step in the contact flow could not be completed. Carries a sentence."""

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        #: ``True`` when something may have happened and we cannot tell. The send
        #: ledger treats this exactly like an unconfirmed send, which is the only
        #: correct response to "I pressed send and then lost the thread".
        self.uncertain = uncertain


#: The three refusals a spoken name can produce, phrased once. They were written
#: out three times -- once per tool -- and three copies is how they come to
#: disagree: one gains a clause, the others do not, and a user is told a
#: different thing depending on which tool they happened to reach for.
MSG_NO_CONTACT_ARG = "Tell me who you mean -- give me the person's name."
MSG_NO_MATCH = "I couldn't find that in your WhatsApp. Nothing was read."
MSG_AMBIGUOUS = (
    "Several chats match that name: {names}. Ask me which one."
)


def ambiguous_message(titles: Sequence[str], *, limit: int = 4) -> str:
    """The "which one?" sentence, names clipped and a count stated.

    Names are read back rather than *sent*, so listing them is safe -- but the
    user has to be able to say one back, which is why they are clipped rather
    than elided and why the list is capped: five candidates they cannot read is
    a worse question than three they can.
    """
    shown = [str(title).strip()[:24] for title in titles[:limit] if str(title).strip()]
    return MSG_AMBIGUOUS.format(names=", ".join(shown))


async def resolve_one(
    search: Any, name: object, *, too_many: str = "", none_found: str = ""
) -> str:
    """One chat title for a spoken name, or a refusal naming what matched.

    The rule every messaging tool needs and none of them should own: a name
    resolves to exactly one chat, a near-miss resolves to *candidates*, and a
    group is never silently substituted for a person. Keeping it here rather than
    in each tool is what stops "did Afia reply?" from being answered with a
    different set of rules than "archive Afia".

    ``search`` is a callable taking the name and returning an iterable of
    :class:`ChatCandidate`, so this stays a pure function of its input and needs
    no adapter: the Web path passes `search_chats`, a test passes a list.
    """
    wanted = normalize_title(name)
    if not wanted:
        raise FlowError(MSG_NO_CONTACT_ARG)

    rows = list(await search(wanted))
    titles = [row.title for row in rows if row.title]
    if not titles:
        raise FlowError(none_found or MSG_NO_MATCH)

    exact = [title for title in titles if title.casefold() == wanted.casefold()]
    if len(exact) == 1:
        return exact[0]
    # One hit, just not spelled the way it was said: "Afia" finding
    # "Afia Rahman" is what the user meant, and asking them to pick from a list
    # of one is a question with only one possible answer.
    if len(titles) == 1:
        return titles[0]
    raise FlowError(too_many or ambiguous_message(titles))


#: The tails `resolve_one` writes, and the order they are matched in. Every
#: caller needs its refusal to end in what did *not* happen, and the verbs differ
#: -- reading changes nothing, archiving changes something. Two tools disagreeing
#: about whether a failed lookup means an effect occurred is precisely the
#: confusion this exists to prevent, so the substitution happens here.
_NOT_DONE = (" Nothing was read.", " Nothing was changed.", " Nothing was sent.")


def reword(sentence: str, attempted: str) -> str:
    """`attempted` in this caller's own words, e.g. "Nothing was changed"."""
    for tail in _NOT_DONE:
        if sentence.endswith(tail):
            return sentence[: -len(tail)] + f" {attempted}"
    return sentence


async def resolve_one_for_tool(
    search: Any, name: object, *, attempted: str = "Nothing was read."
) -> str:
    """:func:`resolve_one` with its ``FlowError`` translated to a ``ToolError`.

    The tool layer wants its refusals to reach the model as ``ToolError``, and
    wants them to name their own consequence. Translating here rather than in
    each of the three callers is what keeps the rule singular -- the version that
    was copied three times is the version where one copy grows a clause and the
    other two do not.
    """
    try:
        return await resolve_one(search, name)
    except FlowError as exc:
        raise ToolError(reword(str(exc), attempted)) from None


@dataclass
class ContactFlow:
    """The sequence, with each step's precondition stated.

    Written as explicit steps rather than a single function because the *order* is
    the safety property: resolve, verify, ask, type, re-verify, send. Each step
    below names what it assumes, so a reordering shows up as a missing argument
    rather than as a subtle change in behaviour.
    """

    #: ``(query) -> list[ChatCandidate]``
    search: Any = None
    #: ``(title) -> str`` -- the chat that became active.
    open_chat: Any = None
    #: ``() -> str`` -- the chat currently open.
    active_chat: Any = None
    #: ``(text) -> None``
    type_message: Any = None
    #: ``() -> None``
    press_send: Any = None

    async def find(self, name: str) -> ContactChoice:
        """Search by name and resolve, or refuse with candidates.

        Never sends and never types. A read, so it is safe to call before any
        approval exists.
        """
        if self.search is None:
            raise FlowError("I can't search WhatsApp right now")
        candidates = await self.search(name)
        return choose(name, candidates)

    async def open_and_verify(self, name: str) -> ChatCandidate:
        """Open the chat, then read back which chat is actually open."""
        if self.open_chat is None or self.active_chat is None:
            raise FlowError("I can't open chats right now")
        choice = await self.find(name)
        if not choice.ok:
            raise FlowError(choice.reason or "I couldn't find that chat")
        assert choice.chosen is not None
        opened = await self.open_chat(choice.chosen.title)
        if not opened:
            raise FlowError(f"{choice.chosen.title} didn't open")
        current = normalize_title(await self.active_chat())
        if current.casefold() != choice.chosen.title.casefold():
            # The row was clicked but a different chat is open -- a reconnect, a
            # notification, or a row that moved. Sending here would message
            # whoever is actually open.
            raise FlowError(
                f"I opened {choice.chosen.title} but WhatsApp is showing {current or 'no chat'}"
            )
        return choice.chosen

    async def type_into_open_chat(self, text: str) -> None:
        """Type into the chat that is open, after re-verifying it.

        The verification is repeated here rather than trusted from
        :meth:`open_and_verify` because the two are separated by an approval
        question, which can take a minute. WhatsApp does not change chats on its
        own -- but a person does, by clicking.
        """
        if self.active_chat is None or self.type_message is None:
            raise FlowError("I can't type into WhatsApp right now")
        current = normalize_title(await self.active_chat())
        if not current:
            raise FlowError("no chat is open in WhatsApp")
        await self.type_message(text)

    async def send(self, state: DraftState, *, now: float | None = None) -> None:
        """The last two steps: re-verify, then send.

        The order is the whole safety contract. The chat is read *again*, the
        approval is checked against the exact recipient and text, and only then is
        Enter pressed. Anything that fails before ``press_send`` means nothing was
        sent and a retry is safe; anything that fails after it raises
        ``FlowError(uncertain=True)``, which the ledger holds like an unconfirmed
        send.
        """
        if self.active_chat is None or self.press_send is None:
            raise FlowError("I can't send from WhatsApp right now")
        current = normalize_title(await self.active_chat())
        refusal = state.may_send(now=now)
        if refusal:
            raise FlowError(f"I did not send it: {refusal}")
        if current.casefold() != state.chat_identity.casefold():
            raise FlowError(
                f"WhatsApp is showing {current or 'no chat'}, not {state.chat_identity}"
            )
        try:
            await self.press_send()
        except FlowError:
            # Raised by a backend that knows it may have sent.
            raise FlowError("I pressed send and could not tell whether it went", uncertain=True) from None
        except Exception as exc:  # noqa: BLE001 - an unknown failure after Enter
            raise FlowError(f"I pressed send and then lost track ({str(exc)[:80]})", uncertain=True) from None