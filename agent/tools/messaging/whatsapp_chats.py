"""Changing a chat without sending anything: archive, pin, mute, read, delete.

Every verb here is "an act on a conversation", and the split that matters is not
the verb but the blast radius. Marking a chat read and deleting a message for
*everybody* are both "arranging WhatsApp", and lumping them together would force
the strictest onto the mildest -- either every read asks, which teaches a person
to answer "yes" without reading, or something irreversible stops asking.

So the classes differ per verb and live in `whatsapp_ops`: `mark_read`,
`mark_unread`, `pin`, `mute`, `archive` and `star` are ordinary acts and run
without a question, while `delete` is destructive and `block` is sensitive. A
settings file can tighten any single verb (``whatsapp_chats.delete=ask``,
``whatsapp_chats.pin=off``) without touching the others.

## Every verb verifies its own effect

Not "the click landed" -- the marker, read back afterwards. A menu entry that
moved in a WhatsApp build change is otherwise a silent no-op, and "I archived
that for you" about a chat still sitting in the list is worse than a refusal,
because the user stops looking.

Two of these refuse rather than act, on purpose:

- **A draft is never overwritten.** Nothing here types, so this is about the
  *active* chat: acting on the wrong chat is the failure mode, and every verb
  re-reads which chat it is about to touch rather than trusting the last call.
- **Delete wants the tombstone, not the absence.** A conversation that scrolled
  past the message and one where the delete worked look identical from outside.
"""

from __future__ import annotations

from typing import Any

try:
    from ...runtime.progress import step as progress_step
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime.progress import step as progress_step

from . import whatsapp_ops as ops
from . import whatsapp_refs as refs
from . import whatsapp_web as web
from .whatsapp_flow import ChatCandidate, resolve_one_for_tool, kind_for_row

try:
    from ..tool_results import as_payload
    from ..tools_compat import RunContext, ToolError, function_tool
    from ..tools_text import clip
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tool_results import as_payload
    from tools_compat import RunContext, ToolError, function_tool
    from tools_text import clip

#: The chat-level verbs, and the row marker each one is confirmed by. The marker
#: is what makes the confirmation possible; a verb without one cannot be verified
#: and so is not offered.
CHAT_VERBS: dict[str, tuple[str, str]] = {
    "mark_read": ("unread", "read"),
    "mark_unread": ("unread", "unread"),
    "pin": ("pinned", "pinned"),
    "unpin": ("pinned", "unpinned"),
    "mute": ("muted", "muted"),
    "unmute": ("muted", "unmuted"),
    "archive": ("archived", "archived"),
    "unarchive": ("archived", "restored"),
}

#: The menu entry each verb clicks. Separate from the marker above because they
#: answer different questions: the marker is what we check, the icon is what we
#: click, and WhatsApp spells them differently again.
CHAT_MENU: dict[str, str] = {
    "mark_read": "mark_read",
    "mark_unread": "mark_unread",
    "pin": "pin",
    "unpin": "unpin",
    "mute": "mute",
    "unmute": "unmute",
    "archive": "archive",
    "unarchive": "unarchive",
}

MSG_NEED_CHAT = "Tell me which chat you mean -- give me the person's name."
MSG_UNKNOWN = (
    "I don't know how to do that to a chat. I can mark it read or unread, pin it, "
    "mute it, archive it, star a message, or delete a message for everyone."
)
MSG_BLOCKED_ON_DESKTOP = (
    "Blocking a contact needs WhatsApp Web -- WhatsApp Desktop can send and search, "
    "but not change who can reach you. Nothing was changed."
)


async def _search_candidates(name: str) -> list:
    """Search-box rows as :class:`ChatCandidate` objects, for the shared resolver."""
    rows = await web.search_chats(name)
    return [
        ChatCandidate(
            title=str(row.get("title") or "").strip(),
            kind=kind_for_row(row),
        )
        for row in rows
        if str(row.get("title") or "").strip()
    ]


async def _resolve(name: str) -> str:
    """One chat title for a spoken name, or a refusal naming what matched.

    The rule is `whatsapp_flow.resolve_one`, shared with the read and account
    tools. Only the closing clause differs -- here a failed lookup means nothing
    was *changed*, which is a different sentence from "nothing was read" and the
    difference is the whole point of telling somebody.
    """
    return await resolve_one_for_tool(
        _search_candidates, name, attempted="Nothing was changed."
    )


async def _delete(context: Any, chat: str, target: str) -> str:
    """Delete one message for everyone, addressed by its handle.

    The handle comes from a read, and the read is what asked the user for consent
    -- so by the time a delete is possible the user already knows Lumine can read
    these messages, and named the one they meant.
    """
    title = await _resolve(chat)
    index = refs.parse_handle(target)
    if index < 1:
        raise ToolError(
            "Tell me which message to delete -- use the id from the messages you "
            "read. Nothing was deleted."
        )
    await web.open_chat(title)
    with progress_step(
        context, ops.CHATS_TOOL, "delete",
        f"Deleting a message in {clip(title, 20)}", index=1, total=1,
    ) as step_state:
        if not await web.delete_message(index):
            raise ToolError(
                "I couldn't confirm that message was deleted, so I have not tried "
                "again. Check WhatsApp; if it's still there, ask me once more."
            )
        step_state["verified"] = "WhatsApp shows the deleted marker in its place"
    return f"Deleted that message in {title} for everyone."


EMOJI_NAMES: dict[str, str] = {
    "thumbs up": "👍",
    "thumbsup": "👍",
    "thumb up": "👍",
    "like": "👍",
    "heart": "❤️",
    "love": "❤️",
    "laugh": "😂",
    "joy": "😂",
    "funny": "😂",
    "lol": "😂",
    "surprised": "😮",
    "surprise": "😮",
    "wow": "😮",
    "sad": "😢",
    "cry": "😢",
    "tear": "😢",
    "pray": "🙏",
    "thanks": "🙏",
    "fire": "🔥",
    "lit": "🔥",
}


async def _react(context: Any, chat: str, target: str, emoji: str) -> str:
    title = await _resolve(chat)
    index = refs.parse_handle(target)
    if index < 1:
        raise ToolError(
            "Tell me which message to react to -- use the id from the messages you "
            "read (e.g. m1). Nothing was changed."
        )
    emoji_clean = str(emoji or "").strip().lower()
    emoji_char = EMOJI_NAMES.get(emoji_clean, str(emoji or "").strip()) or "👍"
    await web.open_chat(title)
    with progress_step(
        context, ops.CHATS_TOOL, "react",
        f"Reacting with {emoji_char} in {clip(title, 20)}", index=1, total=1,
    ) as step_state:
        if not await web.react(index, emoji_char):
            raise ToolError(
                f"I couldn't confirm the reaction was added in {title}, so I have "
                "not tried again."
            )
        step_state["verified"] = "the reaction badge is on that message"
    return f"Reacted with {emoji_char} to that message in {title}."


async def _forward(context: Any, source_chat: str, target: str, dest_name: str) -> str:
    if not dest_name:
        raise ToolError(
            "Tell me who to forward this message to -- give me the contact's name."
        )
    source_title = await _resolve(source_chat)
    dest_title = await _resolve(dest_name)
    index = refs.parse_handle(target)
    if index < 1:
        raise ToolError(
            "Tell me which message to forward -- use the id from the messages you "
            "read (e.g. m1). Nothing was forwarded."
        )
    await web.open_chat(source_title)
    with progress_step(
        context, ops.CHATS_TOOL, "forward",
        f"Forwarding message from {clip(source_title, 16)} to {clip(dest_title, 16)}", index=1, total=1,
    ) as step_state:
        if not await web.forward(index, destination=dest_title):
            raise ToolError(
                f"I couldn't confirm the message was forwarded to {dest_title}. "
                "Nothing was sent."
            )
        step_state["verified"] = "message forwarded into destination chat"
    return f"Forwarded that message from {source_title} to {dest_title}."


async def _clear_chat(context: Any, chat: str) -> str:
    title = await _resolve(chat)
    await web.open_chat(title)
    with progress_step(
        context, ops.CHATS_TOOL, "clear_chat",
        f"Clearing messages in {clip(title, 20)}", index=1, total=1,
    ) as step_state:
        if not await web.clear_chat():
            raise ToolError(
                f"I couldn't clear the messages in {title}. Check WhatsApp."
            )
        step_state["verified"] = "conversation messages cleared"
    return f"Cleared all messages in {title}."


async def _clear_composer_action(context: Any, chat: str) -> str:
    title = await _resolve(chat) if chat else await web.active_chat()
    if title:
        await web.open_chat(title)
    with progress_step(
        context, ops.CHATS_TOOL, "clear_composer",
        f"Clearing composer for {clip(title or 'WhatsApp', 20)}", index=1, total=1,
    ) as step_state:
        if not await web.clear_composer():
            raise ToolError(
                f"I couldn't clear the message box for {title or 'WhatsApp'}. Check WhatsApp."
            )
        step_state["verified"] = "composer is empty"
    return f"Cleared the composer in {title or 'WhatsApp'}."


@function_tool()
async def whatsapp_chats(
    context: RunContext,
    action: str,
    chat: str = "",
    message: str = "",
    emoji: str = "",
    to: str = "",
) -> str:
    """Arrange a WhatsApp chat: mark read, pin, mute, archive, star, delete, react, forward, clear_chat, clear_composer.

    For changing a conversation, not for reading one and not for sending a new message. If the
    user wants to know what was said, use ``whatsapp_read``; if they want to
    message somebody, use ``send_whatsapp``.

    Actions on a chat: ``mark_read``, ``mark_unread``, ``pin``, ``unpin``,
    ``mute``, ``unmute``, ``archive``, ``unarchive``, ``clear_chat``, ``clear_composer``. Give the person's name in
    ``chat``.

    ``clear_composer`` discards any unsent draft from the message box without affecting chat history.

    ``star`` marks one message -- give the person's name and the message id from
    a previous read. ``unstar`` removes the star.

    ``react`` adds an emoji reaction to a message. Give the person's name in ``chat``,
    the message id in ``message``, and the emoji in ``emoji`` (e.g. 👍, ❤️, 😂).

    ``forward`` sends a message to another contact. Give the source in ``chat``,
    the message id in ``message``, and the destination contact in ``to``.

    ``delete`` removes one message for *everyone* in the chat. It is irreversible
    for the other person too, so the user always confirms it first with the
    message named. Never delete on your own initiative and never delete more than
    the one message the user named.

    ``block`` stops a contact messaging you. ``unblock`` restores contact. Say what it does before confirming.

    Args:
        action: mark_read, mark_unread, pin, unpin, mute, unmute, archive,
            unarchive, star, unstar, delete, block, unblock, react, forward, clear_chat, or clear_composer.
        chat: The person's name, as it appears in WhatsApp.
        message: For star, delete, react and forward, the message id a read gave you (e.g. m1).
        emoji: For react, the emoji character or name (e.g. ❤️, 👍, fire, laugh).
        to: For forward, the destination contact name.
    """
    verb = str(action or "").strip().lower().replace("-", "_")
    wanted = str(chat or "").strip()
    target = str(message or "").strip()

    valid_verbs = (
        set(CHAT_VERBS)
        | {"star", "unstar", "delete", "block", "unblock", "react", "forward", "clear_chat", "clear_composer", "clear_draft", "discard_draft"}
    )
    if verb not in valid_verbs:
        raise ToolError(MSG_UNKNOWN)

    if verb in ("clear_composer", "clear_draft", "discard_draft"):
        return await _clear_composer_action(context, wanted)

    if not wanted:
        raise ToolError(MSG_NEED_CHAT)

    if verb == "delete":
        return await _delete(context, wanted, target)

    if verb == "react":
        return await _react(context, wanted, target, emoji)

    if verb == "forward":
        return await _forward(context, wanted, target, to)

    if verb == "clear_chat":
        return await _clear_chat(context, wanted)

    if verb == "block":
        return await _block(context, wanted, block=True)
    if verb == "unblock":
        return await _block(context, wanted, block=False)

    title = await _resolve(wanted)

    if verb in ("star", "unstar"):
        index = refs.parse_handle(target)
        if index < 1:
            raise ToolError(
                "Tell me which message to star -- use the id from the messages "
                "you read. Nothing was changed."
            )
        await web.open_chat(title)
        with progress_step(
            context, ops.CHATS_TOOL, verb,
            f"{'Starring' if verb == 'star' else 'Unstarring'} a message in {clip(title, 20)}",
            index=1, total=1,
        ) as step_state:
            if not await web.star_message(index, star=(verb == "star")):
                raise ToolError(
                    f"I couldn't confirm the message was starred in {title}, so I "
                    "have not tried again."
                )
            step_state["verified"] = "the marker is on that message"
        return f"{'Starred' if verb == 'star' else 'Unstarred'} that message in {title}."

    flag, _label = CHAT_VERBS[verb]
    want = flag in ("unread", "pinned", "muted", "archived") and verb in (
        "mark_unread", "pin", "mute", "archive"
    )
    with progress_step(
        context, ops.CHATS_TOOL, verb,
        f"{_gerund(verb)} {clip(title, 24)}", index=1, total=1,
    ) as step_state:
        ok = await web.set_flag(
            title, flag, CHAT_MENU[verb], want=want,
            fallback_text=_fallback_text(verb),
        )
        if not ok:
            raise ToolError(
                f"I couldn't confirm that {title} was {_gerund(verb)}ed, so I have "
                "not tried again. Check WhatsApp."
            )
        step_state["verified"] = f"the {flag} marker is where it should be"
    return f"{_done(verb)} {title}."


def _gerund(verb: str) -> str:
    return {
        "mark_read": "marking read",
        "mark_unread": "marking unread",
        "pin": "pinning",
        "unpin": "unpinning",
        "mute": "muting",
        "unmute": "unmuting",
        "archive": "archiving",
        "unarchive": "unarchiving",
    }.get(verb, verb)


def _done(verb: str) -> str:
    return {
        "mark_read": "Marked read:",
        "mark_unread": "Marked unread:",
        "pin": "Pinned:",
        "unpin": "Unpinned:",
        "mute": "Muted:",
        "unmute": "Unmuted:",
        "archive": "Archived:",
        "unarchive": "Restored:",
    }.get(verb, "Done:")


def _fallback_text(verb: str) -> str:
    """A last-resort menu label, for builds with no icon on the entry.

    English, and only reached when every `data-icon` spelling missed -- so the
    refusal path says the option was not found rather than silently clicking the
    nearest thing in the menu.
    """
    return {
        "mark_read": "mark as read",
        "mark_unread": "mark as unread",
        "pin": "pin chat",
        "unpin": "unpin chat",
        "mute": "mute notifications",
        "unmute": "unmute notifications",
        "archive": "archive chat",
        "unarchive": "unarchive chat",
    }.get(verb, "")


async def _block(context: Any, name: str, *, block: bool) -> str:
    """Block or unblock a contact, through the chat's own info drawer.

    ``sensitive`` rather than ``act``: it stops messages arriving, which is
    reversible by hand but easy to do by accident and annoying to discover later.
    Only WhatsApp Web can do it -- the Desktop path reaches a contact's settings
    through a drawer it cannot navigate reliably -- and the refusal says that
    rather than reporting that the block worked when nothing was clicked.
    """
    verb = "block" if block else "unblock"
    title = await _resolve(name)
    await web.require_ready()
    if not await web.open_chat(title):
        raise ToolError(f"I couldn't open {title}'s chat, so nothing was changed.")
    with progress_step(
        context, ops.CHATS_TOOL, verb,
        f"{'Blocking' if block else 'Unblocking'} {clip(title, 20)}", index=1, total=1,
    ) as step_state:
        if not await web.block_contact(block=block):
            raise ToolError(
                f"I couldn't find the {'block' if block else 'unblock'} option for "
                f"{title}, so nothing was changed -- WhatsApp's layout may have moved."
            )
        step_state["verified"] = "the block setting is now where it should be"
    return f"{'Blocked' if block else 'Unblocked'} {title}."