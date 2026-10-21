"""Every WhatsApp operation Lumine can perform, declared once.

This table is the answer to a question that used to have two answers in two
places: *what may WhatsApp do, how much does each thing cost the user, and which
path can do it.* The registry (`tools_registry.TOOL_ACTION_CLASS`), the Tools-page
cards, the permission floor, and the runtime all used to answer it separately, and
"separately" is how a badge comes to promise something the tool never does.

One row per operation, and everything reads it:

- the model-facing tools are *built* from the rows (so a verb the table does not
  list cannot be called),
- `permission_gate` is handed the row's ``action_class`` (so the badge cannot
  disagree with the behaviour),
- the backends answer `supports()` from the row (so "WhatsApp Desktop cannot
  delete a message" is a fact with a name, not a `AttributeError`),
- the ledger reads ``holds_on_uncertainty`` (so "do not retry" is per-operation
  rather than per-tool).

## Why per-operation rather than per-tool

Because the blast radii are not the same size. Listing your chats and deleting a
message for everyone are both "WhatsApp", and treating them as one thing forces
the strictest of the two onto the mildest: either every read asks, which trains
the user to answer "yes" without reading, or something irreversible stops asking.
Splitting them is what keeps ``read`` cheap and ``destructive`` honest.

## The class floor is a floor

``action_class`` here is what the *call site* passes to the gate. It is not a
suggestion the settings file can lower: `permission_gate.CLASS_FLOOR` is applied
after the policy level, so a saved document can only ever add a question. A row
marked ``read`` therefore means "asks for nothing under a default policy", and a
row marked ``destructive`` means "asks, and no configuration can stop it asking".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

#: The action classes, mildest first. Mirrors ``permission_gate.ACTION_CLASSES``;
#: ``test_whatsapp_ops.py`` pins the two so a class invented here and not there
#: fails rather than quietly becoming "sensitive" at the gate.
ACTION_CLASSES = ("read", "act", "sensitive", "destructive")

#: The sole WhatsApp messaging path.
PATHS = ("web",)

#: The path every operation runs on.
ONLY_PATH = "web"

#: Model-facing tool ids. Declared here rather than in the registry so this table
#: can name the tool a verb belongs to without importing it -- the registry imports
#: *this*, and a cycle resolved by import order is a cycle nobody can debug.
READ_TOOL = "whatsapp_read"
SEND_TOOL = "send_whatsapp"
CHATS_TOOL = "whatsapp_chats"
ACCOUNT_TOOL = "whatsapp_account"

#: Every tool the messaging surface owns, in the order the registry registers
#: them: reads first because they are the cheapest thing that can answer a
#: question, and the account tool before the send tool because a send is the one
#: thing here nobody should reach for first.
MESSAGING_TOOLS = (READ_TOOL, SEND_TOOL, CHATS_TOOL, ACCOUNT_TOOL)


@dataclass(frozen=True)
class Operation:
    """One operation, and everything the runtime needs to be honest about it."""

    #: The verb, as the model writes it and as a policy key names it
    #: (``whatsapp_chats.delete=ask``). Lower case, underscores, stable: a
    #: permission key is a thing a person typed into a settings file, so renaming
    #: one silently disarms a decision they made.
    verb: str
    #: The model-facing tool that exposes it.
    tool: str
    #: The permission class at the call site.
    action_class: str
    #: Whether WhatsApp Web can carry it out.
    web: bool = True
    #: How a completed operation proves it happened, in one short clause. Written
    #: down because "observed" is not optional for anything that changes a
    #: person's chat, and the specific evidence differs per verb: a sent message
    #: is a bubble, a deleted one is a *tombstone* (absence is not evidence --
    #: the list may simply have scrolled), an archived chat is one that moved.
    verify: str = ""
    #: Whether an unconfirmed outcome blocks a retry, and for how long.
    #: ``0`` means a retry is safe, because nothing left this process. A send
    #: that may have gone out is the case this exists for.
    hold_seconds: float = 0.0
    #: Whether this operation reads message history. The one property in this
    #: table that carries a privacy weight rather than a mechanical one, and the
    #: reason `whatsapp_read` exists as a separate tool at all.
    reads_history: bool = False
    #: A short, model-facing description of what the verb does.
    summary: str = ""
    #: Whether any tool can actually be *asked* for this verb.
    #:
    #: The distinction this field exists for: the Web path implements more than
    #: the tools expose. ``react`` and ``forward`` are real operations with real
    #: verification clauses -- and no `send_whatsapp` parameter reaches either of
    #: them. As ordinary rows they were phantom switches:
    #: `messaging.whatsapp.react=ask` would tighten a verb the model can never
    #: invoke, so it would tighten nothing while appearing to.
    #:
    #: The row stays -- it is the record of what the path can do -- and this flag
    #: says whether a tool dispatches on it. Only selectable verbs contribute to
    #: a tool's badge or floor.
    selectable: bool = True

    @property
    def irreversible(self) -> bool:
        """Whether an unconfirmed outcome may exist outside this process."""
        return self.hold_seconds > 0

    @property
    def supported_paths(self) -> tuple[str, ...]:
        return tuple(name for name in PATHS if getattr(self, name))

    def supports(self, path: str) -> bool:
        """Whether this operation is available on the sole Web path."""
        wanted = str(path or "").strip().lower()
        if wanted != ONLY_PATH:
            return False
        return bool(getattr(self, ONLY_PATH, False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "verb": self.verb,
            "tool": self.tool,
            "action_class": self.action_class,
            "paths": list(self.supported_paths),
            "verify": self.verify,
            "reads_history": self.reads_history,
            "selectable": self.selectable,
        }


#: How long an unconfirmed send blocks a retry. Five minutes, matching
#: `whatsapp.SendLedger`'s uncertain hold: long enough for a person to look at
#: their phone, short enough that the ledger is not a permanent scar.
SEND_HOLD_SECONDS = 300.0

#: Delete-for-everyone is the worst case in the table -- the message is gone for
#: the other person too, and there is no undo and no "recall". It gets the same
#: hold as a send, because from the user's point of view the two failures are the
#: same failure: *did Lumine do the irreversible thing or not?*
DELETE_HOLD_SECONDS = 300.0

#: Blocking a contact is reversible by hand, so a shorter hold. Long enough that
#: a retry inside the same confused moment is refused; short enough that
#: "unblock them again" is not blocked by a stale entry.
BLOCK_HOLD_SECONDS = 60.0


OPERATIONS: tuple[Operation, ...] = (
    # -- reads: no history --------------------------------------------------
    Operation(
        verb="chats",
        tool=READ_TOOL,
        action_class="read",
        web=True,
        verify="the rows WhatsApp itself renders in the chat list",
        summary="List recent chats with unread counts and last-message previews.",
    ),
    Operation(
        verb="unread",
        tool=READ_TOOL,
        action_class="read",
        web=True,
        verify="the unread badge on each row",
        summary="List only the chats with unread messages.",
    ),
    Operation(
        # Named for the tool's own action, not for the backend call it drives.
        # The two are not the same thing: `whatsapp_read find` calls
        # `web.search_chats`, and this table is what a settings key is matched
        # against. A verb here the tool does not accept is a switch that tightens
        # nothing -- the floor is read per verb, so a name nothing dispatches on
        # is a promise the gate silently cannot keep.
        verb="find",
        tool=READ_TOOL,
        action_class="read",
        web=True,
        verify="the filtered result list from WhatsApp's own search box",
        summary="Find chats by name, through WhatsApp's search box.",
    ),
    Operation(
        verb="contact",
        tool=READ_TOOL,
        action_class="read",
        web=True,
        verify="the profile pane the contact's own row opens",
        summary="Read a contact's saved name, number, and about text.",
    ),
    # No `message_search` row, deliberately. It used to be declared here and was
    # never callable: `whatsapp_read` has no such action, so a settings key
    # naming it tightened nothing while the table advertised a verb that could
    # only ever refuse. Declaring an operation nothing can reach is the same
    # mistake as advertising one, so the row is gone rather than pending --
    # searching inside a conversation is a feature, and adding it means adding
    # the branch and this row in the same change.
    # -- reads: history. Separate because the consent question is different. ---
    Operation(
        verb="messages",
        tool=READ_TOOL,
        action_class="sensitive",
        web=True,
        verify="the message bubbles WhatsApp renders in the open conversation",
        reads_history=True,
        summary="Read the most recent messages in one chat.",
    ),
    Operation(
        verb="search_messages",
        tool=READ_TOOL,
        action_class="read",
        web=True,
        verify="the search results WhatsApp renders matching the query",
        summary="Search messages for keywords across or inside chats.",
    ),
    # -- sending ------------------------------------------------------------
    Operation(
        verb="send_text",
        tool=SEND_TOOL,
        action_class="destructive",
        web=True,
        verify="this exact text appearing as an outgoing bubble, read after the send",
        hold_seconds=SEND_HOLD_SECONDS,
        summary="Type a message into a chat and send it.",
    ),
    Operation(
        verb="prepare_draft",
        tool=SEND_TOOL,
        action_class="act",
        web=True,
        verify="the composer holding exactly the requested text, and still unsent",
        summary="Type a message into a chat and leave it there, unsent.",
    ),
    Operation(
        verb="reply",
        tool=SEND_TOOL,
        action_class="destructive",
        web=True,
        verify="the new outgoing bubble carrying the quoted original",
        hold_seconds=SEND_HOLD_SECONDS,
        summary="Reply to one specific message, quoting it.",
    ),
    Operation(
        verb="attach",
        tool=SEND_TOOL,
        action_class="destructive",
        web=True,
        verify="the attachment bubble present with the right caption",
        hold_seconds=SEND_HOLD_SECONDS,
        summary="Send a file from this computer as a message.",
    ),
    Operation(
        verb="typing",
        tool=SEND_TOOL,
        action_class="act",
        web=True,
        verify="nothing -- it is a hint, not an effect that must persist",
        summary="Show the typing indicator in a chat for a moment.",
    ),
    # -- chat management ----------------------------------------------------
    Operation(
        verb="react",
        tool=CHATS_TOOL,
        action_class="act",
        web=True,
        verify="the reaction badge on that specific message",
        summary="React to one specific message with an emoji.",
        selectable=True,
    ),
    Operation(
        verb="forward",
        tool=CHATS_TOOL,
        action_class="destructive",
        web=True,
        verify="the forwarded bubble present in the destination chat",
        hold_seconds=SEND_HOLD_SECONDS,
        summary="Forward one message to somebody else.",
        selectable=True,
    ),
    Operation(
        verb="clear_chat",
        tool=CHATS_TOOL,
        action_class="destructive",
        web=True,
        verify="all messages removed from the open conversation",
        hold_seconds=DELETE_HOLD_SECONDS,
        summary="Clear all messages in a chat.",
        selectable=True,
    ),
    Operation(
        verb="clear_composer",
        tool=CHATS_TOOL,
        action_class="act",
        web=True,
        verify="the composer holding no text",
        summary="Clear or discard any unsent draft in the message box.",
        selectable=True,
    ),
    Operation(
        verb="mark_read",
        tool=CHATS_TOOL,
        action_class="act",
        web=True,
        verify="the unread badge gone from that row",
        summary="Clear a chat's unread badge.",
    ),
    Operation(
        verb="mark_unread",
        tool=CHATS_TOOL,
        action_class="act",
        web=True,
        verify="the unread badge present on that row",
        summary="Put a chat's unread badge back.",
    ),
    Operation(
        verb="pin",
        tool=CHATS_TOOL,
        action_class="act",
        web=True,
        verify="the pin marker on that row",
        summary="Pin a chat to the top of the list, or unpin it.",
    ),
    Operation(
        verb="mute",
        tool=CHATS_TOOL,
        action_class="act",
        web=True,
        verify="the muted marker on that row",
        summary="Silence notifications for a chat, or unsilence them.",
    ),
    Operation(
        verb="archive",
        tool=CHATS_TOOL,
        action_class="act",
        web=True,
        verify="the row gone from the main list and present in the archive",
        summary="Archive a chat, or bring it back.",
    ),
    Operation(
        verb="star",
        tool=CHATS_TOOL,
        action_class="act",
        web=True,
        verify="the starred marker on that message",
        summary="Star or unstar one message.",
    ),
    Operation(
        verb="delete",
        tool=CHATS_TOOL,
        action_class="destructive",
        web=True,
        verify=(
            "the replacement tombstone WhatsApp renders in the message's place -- "
            "not its absence, which a scrolled list produces anyway"
        ),
        hold_seconds=DELETE_HOLD_SECONDS,
        summary="Delete one message for everyone in the chat.",
    ),
    Operation(
        verb="block",
        tool=CHATS_TOOL,
        action_class="sensitive",
        web=True,
        verify="the contact appearing in the account's blocked list",
        hold_seconds=BLOCK_HOLD_SECONDS,
        summary="Block or unblock a contact.",
    ),
    # -- account ------------------------------------------------------------
    Operation(
        verb="presence",
        tool=ACCOUNT_TOOL,
        action_class="read",
        web=True,
        verify="the last-seen line in the chat header",
        summary="Read whether a contact is online and when they were last seen.",
    ),
    Operation(
        verb="devices",
        tool=ACCOUNT_TOOL,
        action_class="read",
        web=True,
        verify="the rows WhatsApp lists under linked devices",
        summary="List the other devices signed in to this account.",
    ),
    Operation(
        verb="about",
        tool=ACCOUNT_TOOL,
        action_class="read",
        web=True,
        verify="the profile pane WhatsApp opens for the signed-in account",
        summary="Read this account's own name, number, and about text.",
    ),
    Operation(
        verb="unlink",
        tool=ACCOUNT_TOOL,
        action_class="destructive",
        web=True,
        verify="that device absent from the linked-device list",
        hold_seconds=BLOCK_HOLD_SECONDS,
        summary="Sign one other device out of this account.",
    ),
    Operation(
        verb="call",
        tool=ACCOUNT_TOOL,
        action_class="destructive",
        web=True,
        verify="the call surface WhatsApp opens over the conversation",
        hold_seconds=BLOCK_HOLD_SECONDS,
        summary="Start a voice or video call with a contact.",
    ),
)


def _index() -> dict[str, Operation]:
    return {op.verb: op for op in OPERATIONS}


_BY_VERB: dict[str, Operation] = _index()


def operation(verb: object) -> Operation | None:
    """The row for a verb, or ``None``.

    ``None`` rather than a refusal that raises: callers use this to answer "is
    this thing a thing I know how to do", and an unknown verb must read as *no*
    rather than as an exception from a lookup table.
    """
    return _BY_VERB.get(str(verb or "").strip().lower().replace("-", "_"))


def verbs_for(tool: object) -> tuple[str, ...]:
    """Every verb a tool exposes, in declaration order."""
    wanted = str(tool or "").strip().lower()
    return tuple(op.verb for op in OPERATIONS if op.tool == wanted)


def operations_for(tool: object) -> tuple[Operation, ...]:
    wanted = str(tool or "").strip().lower()
    return tuple(op for op in OPERATIONS if op.tool == wanted)


def selectable_operations(tool: object) -> tuple[Operation, ...]:
    """The verbs a settings key can actually name.

    Excludes the implemented-but-unexposed rows. Everything reading this table on
    the *authority* side -- a tool's badge, its floor, a per-verb policy key --
    goes through here, because a verb no tool dispatches on cannot be tightened
    and must not appear to be tightenable.
    """
    return tuple(op for op in operations_for(tool) if op.selectable)


def tool_action_class(tool: object) -> str:
    """The strictest class any of a tool's operations reaches.

    This is the Tools-page badge and the registry's ``TOOL_ACTION_CLASS`` row,
    and it is computed rather than written down twice -- ``test_tools.py`` pins it
    against the registry so a card cannot claim less than the gate will do.

    ``destructive`` wins over everything, ``sensitive`` over ``act``, ``act`` over
    ``read``: a tool whose worst verb deletes a message for everyone must not
    advertise itself as something that "answers questions".

    Read from the *selectable* rows only. The strictest verb has to be one the
    tool can be asked for, or the badge describes a capability that does not
    exist -- and would stay `destructive` long after the verb is gone.
    """
    rows = selectable_operations(tool)
    if not rows:
        return "sensitive"
    rank = {name: index for index, name in enumerate(ACTION_CLASSES)}
    return max((row.action_class for row in rows), key=lambda name: rank.get(name, 2))


def verify_clause(verb: object) -> str:
    """How an operation proves it happened, or ``""`` when nothing does."""
    row = operation(verb)
    return row.verify if row is not None else ""


def refusal_for(verb: object, path: str) -> str:
    """The sentence for "this path cannot do that", naming the path.

    One sentence per reason and every path's own reason at once, because a user
    who is told "WhatsApp can't delete that" and then "WhatsApp can't either" has
    learned nothing except that something is broken.
    """
    row = operation(verb)
    name = {"web": "WhatsApp Web"}.get(
        str(path or "").strip().lower(), "that path"
    )
    normalized = _phrase(str(verb or "").strip())
    if row is None:
        return f"{name} cannot {normalized or 'do that'}."
    return f"{name} cannot {_phrase(row.verb)}."


def _phrase(verb: str) -> str:
    """A verb turned into something a sentence can contain."""
    return str(verb or "").strip().lower().replace("_", " ")


def supported_by(verb: object, path: str) -> bool:
    row = operation(verb)
    return bool(row is not None and row.supports(path))


def reads_history(verb: object) -> bool:
    row = operation(verb)
    return bool(row is not None and row.reads_history)


def hold_seconds(verb: object) -> float:
    row = operation(verb)
    return float(row.hold_seconds) if row is not None else 0.0


def catalog() -> dict[str, Any]:
    """The whole table, for the Tools page and for diagnostics."""
    return {
        "tools": [
            {
                "id": tool,
                "actionClass": tool_action_class(tool),
                "operations": [row.to_dict() for row in operations_for(tool)],
            }
            for tool in MESSAGING_TOOLS
        ]
    }


def iter_operations(rows: Iterable[Operation] | None = None) -> tuple[Operation, ...]:
    return tuple(OPERATIONS if rows is None else rows)