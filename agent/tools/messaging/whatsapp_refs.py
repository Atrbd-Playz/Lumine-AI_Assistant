"""Naming one chat and one message, so an action cannot drift onto the wrong thing.

Everything that is not "send a new message" needs to point at something that
already exists: react to *this* message, delete *that* one, forward *it*, reply
quoting *it*. A position on screen is not an identity. WhatsApp re-renders its
conversation constantly -- a new message arrives at the bottom, the list scrolls,
a reaction re-renders the bubble -- so an index captured a moment ago is an index
into a list that has since changed, and acting on it means acting on whatever
moved into that slot.

So a reference carries three things a bare index cannot:

1. **Where from the end.** Counted backwards from the newest message, because
   that is the end that moves: new messages push everything above them down, so a
   position counted from the top is stale the moment anybody is spoken to and a
   position counted from the bottom survives it. It is still only a hint -- it is
   what we look *near*, not what we act on.
2. **What it said.** A digest of the text, so the reference can be checked against
   what is actually there before anything is done to it. This is the part that
   makes the position safe: if the bubble at that position is not the bubble we
   meant, we refuse rather than act.
3. **Which read it came from.** A generation counter per chat, bumped by every
   read of that chat. A reference minted from a read the caller has not seen is a
   reference into a page state nobody reasoned about -- the identical rule
   `browser/actions.py` enforces on `data-lumine-ref`, and for the identical
   reason.

## Why the store is process-local and why that is right

A reference is valid for as long as the conversation it was read out of. It is
not a durable identifier, not a database key, and deliberately not serialisable
into anything the model can see: WhatsApp has no stable public message id in this
interface, so minting one ourselves and writing it into the chat context would be
inventing an identifier the next call could not check. References live in the
store, and the model gets back a short *handle* it can pass again -- which is
enough to say "the third one from the bottom" and not enough to be trusted
without the checks below.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

#: How long a reference stays usable. Long enough to survive an approval
#: question -- which is a minute of the user's time, deliberately -- and short
#: enough that a reference from a conversation twenty minutes ago is refused
#: rather than guessed at. The refresh path is one read.
REF_TTL_SECONDS = 180.0

#: Refusal sentences. Each names what to do, because "invalid reference" leaves
#: the model to invent a recovery and it will invent "try again".
MSG_STALE_CHAT = "that chat has been read again since -- read it once more and I'll try that"
MSG_GONE = "that message is no longer there -- read the chat again"
MSG_CHANGED = "that position holds a different message now -- read the chat again"
MSG_UNKNOWN = "I don't have that reference -- read the chat again and use the id it gives you"


def _digest(*parts: object) -> str:
    """A short, stable digest. Truncated because it is an identity, not a secret,
    and because it is handed to the model as a handle."""
    joined = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def _normalize(text: object) -> str:
    return " ".join(str(text or "").split())


@dataclass(frozen=True)
class MessageRef:
    """One message, addressed well enough to check and to refuse with.

    ``index_from_end`` is 1 for the newest visible message. ``text_digest`` is a
    digest of the message's own text; ``""`` for a message with no text (an image
    or a sticker), which is checked by position and kind alone -- and *only* by
    position and kind, because there is nothing to compare.
    """

    index_from_end: int
    text_digest: str = ""
    preview: str = ""
    outgoing: bool = False
    kind: str = "text"
    chat: str = ""
    generation: int = 0
    created_at: float = field(default_factory=time.monotonic)

    @property
    def handle(self) -> str:
        """The short id the model passes back. Opaque on purpose."""
        return f"m{self.index_from_end}"

    def matches(self, other: "MessageRef") -> bool:
        """Whether two references describe the same message.

        Position, direction, kind, and text must all agree. Text is skipped only
        when neither side has any -- two adjacent stickers are otherwise
        indistinguishable, and refusing to act on those is better than picking one
        of them.
        """
        if self.index_from_end != other.index_from_end:
            return False
        if self.outgoing != other.outgoing or self.kind != other.kind:
            return False
        if not self.text_digest or not other.text_digest:
            return True
        return self.text_digest == other.text_digest

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.handle,
            "fromEnd": self.index_from_end,
            "kind": self.kind,
            "mine": self.outgoing,
        }
        if self.preview:
            out["text"] = self.preview
        return out


@dataclass(frozen=True)
class ChatRef:
    """One chat, as the read that found it described it."""

    title: str
    kind: str = "contact"
    participants: tuple[str, ...] = ()
    unread: int = 0
    preview: str = ""
    pinned: bool = False
    generation: int = 0
    created_at: float = field(default_factory=time.monotonic)

    @property
    def key(self) -> str:
        """The store key. Case- and whitespace-folded, because "Afia" and
        " afia " are one chat and treating them as two would make a reference
        look stale when nothing moved."""
        return _normalize(self.title).casefold()

    @property
    def handle(self) -> str:
        return self.key[:16] or "chat"

    @property
    def is_group(self) -> bool:
        return self.kind == "group" or len(self.participants) > 1

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"chat": self.title, "kind": self.kind}
        if self.unread:
            out["unread"] = self.unread
        if self.preview:
            out["last"] = self.preview
        if self.pinned:
            out["pinned"] = True
        return out


class RefStore:
    """Generations per chat, and the freshness check everything else defers to.

    Process-local and unshared between sessions, like the send ledger it sits
    beside. Two sessions in one worker are two conversations about two different
    WhatsApp accounts-as-they-appear-on-screen, and a reference minted while
    driving the first must not authorize an action during the second.

    The generation is bumped by *reads*, not by writes. A read is the only event
    that changes what the model believes it saw, so it is the only event that can
    make a previous belief wrong.
    """

    def __init__(self, *, ttl: float = REF_TTL_SECONDS) -> None:
        self._generations: dict[str, int] = {}
        self._issued: dict[str, float] = {}
        self._ttl = float(ttl)

    # -- generations ---------------------------------------------------------

    def bump(self, chat: object) -> int:
        """Mark that `chat` has just been read. Returns the new generation."""
        key = _normalize(chat).casefold()
        if not key:
            return 0
        generation = self._generations.get(key, 0) + 1
        self._generations[key] = generation
        self._issued[key] = time.monotonic()
        return generation

    def generation(self, chat: object) -> int:
        return self._generations.get(_normalize(chat).casefold(), 0)

    def freshness(self, ref: MessageRef) -> str:
        """``""`` when a reference may still be acted on, else why not.

        Three checks, in the order that gives the most useful sentence. Expiry
        first because it is the cheapest and the most common after a long pause;
        then the generation, because that is the "you have not seen this" case;
        the text comparison is *not* here, because that needs a live read and
        belongs in the backend, immediately before the action.
        """
        if ref.index_from_end < 1:
            return MSG_UNKNOWN
        if time.monotonic() - ref.created_at > self._ttl:
            return MSG_STALE_CHAT
        if ref.chat and self.generation(ref.chat) != ref.generation:
            return MSG_STALE_CHAT
        return ""

    def clear(self, chat: object = "") -> None:
        if not chat:
            self._generations.clear()
            self._issued.clear()
            return
        key = _normalize(chat).casefold()
        self._generations.pop(key, None)
        self._issued.pop(key, None)


#: One store for the process, mirroring `whatsapp.SendLedger`. Deliberately
#: module-level: the alternative is threading a store through every tool call, and
#: a per-call store would mint generation 1 on every call and never expire
#: anything.
store = RefStore()


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def message_refs(rows: Sequence[Any], *, chat: str, generation: int) -> list[MessageRef]:
    """Addressable references for the messages just read, newest last.

    ``rows`` is whatever the backend produced -- dicts with ``text``, ``mine`` and
    ``kind`` -- in the order the conversation renders, oldest first. The
    conversion to ``index_from_end`` happens here rather than in each backend, so
    "which end counts from" is decided once and cannot differ between the Web
    path and the Desktop path.
    """
    total = len(rows)
    refs: list[MessageRef] = []
    for position, row in enumerate(rows):
        text = _normalize(row.get("text")) if isinstance(row, dict) else ""
        refs.append(
            MessageRef(
                index_from_end=max(1, total - position),
                text_digest=_digest(text) if text else "",
                preview=text[:120],
                outgoing=bool(row.get("mine")) if isinstance(row, dict) else False,
                kind=str(row.get("kind") or "text") if isinstance(row, dict) else "text",
                chat=chat,
                generation=generation,
            )
        )
    return refs


def chat_refs(rows: Sequence[Any], *, generation: int = 0) -> list[ChatRef]:
    """Addressable references for the chats just read."""
    out: list[ChatRef] = []
    for row in rows:
        title = _normalize(row.get("title") if isinstance(row, dict) else row)
        if not title:
            continue
        participants = row.get("participants") if isinstance(row, dict) else ()
        out.append(
            ChatRef(
                title=title,
                kind=str(row.get("kind") or "contact") if isinstance(row, dict) else "contact",
                participants=tuple(str(name) for name in (participants or ())),
                unread=int(row.get("unread") or 0) if isinstance(row, dict) else 0,
                preview=_normalize(row.get("preview"))[:80] if isinstance(row, dict) else "",
                pinned=bool(row.get("pinned")) if isinstance(row, dict) else False,
                generation=generation,
            )
        )
    return out


def parse_handle(value: object) -> int:
    """``"m3"`` -> ``3``. Anything else -> ``0``, meaning "not a handle"."""
    text = str(value or "").strip().lower()
    if not text.startswith("m"):
        return 0
    tail = text[1:]
    return int(tail) if tail.isdigit() else 0