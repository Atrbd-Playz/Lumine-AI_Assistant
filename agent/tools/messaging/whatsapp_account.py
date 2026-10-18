"""The account itself: presence, linked devices, and the phone number.

Smaller than the other three, and the least interesting, which is why it is a
separate tool rather than verbs on ``whatsapp_chats``: these operations are about
the user's account rather than about a conversation, they act on state nobody
else in the chat can see, and lumping them in would mean the chat card on the
Tools page had to promise "and your account settings" to describe a tool people
use to pin a chat.

Calls are here too, and they are the weakest operation in the whole messaging
surface, so they are worth being honest about. A call **rings a real person's
phone**. That is a far larger interruption than a message, it is not reversible,
and unlike everything else here it happens whether or not the recipient is
available. It is therefore ``destructive`` -- the strongest class, always asking,
never lowerable by a settings file -- and it is verified by the call surface
opening, which is evidence the call started and not that anybody answered.
"""

from __future__ import annotations

from typing import Any

try:
    from ...runtime.progress import step as progress_step
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime.progress import step as progress_step

from . import whatsapp_ops as ops
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

MSG_NEED_CHAT = "Tell me who you mean -- give me the person's name."
MSG_NEED_DEVICE = "Tell me which device to sign out."
MSG_UNKNOWN = (
    "I don't know how to do that to your WhatsApp account. I can check whether "
    "someone is online, list the devices signed in, sign one out, read your own "
    "profile, or start a call."
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

    The rule is `whatsapp_flow.resolve_one`, shared with the read and chat tools.
    Here the closing clause is "Nothing was changed", because the worst thing this
    tool can be asked to do is sign a device out or start a call, and a refusal
    that said "nothing was read" would understate it.
    """
    return await resolve_one_for_tool(
        _search_candidates, name, attempted="Nothing was changed."
    )


@function_tool()
async def whatsapp_account(
    context: RunContext,
    action: str,
    who: str = "",
    call_type: str = "voice",
) -> str:
    """Check on the WhatsApp account itself: presence, devices, own profile, calls.

    This is about the account rather than about a conversation. To read a chat use
    ``whatsapp_read``; to change a chat use ``whatsapp_chats``; to message someone
    use ``send_whatsapp``.

    Actions:
    - ``presence``: whether someone is online and when they were last seen.
    - ``devices``: the other devices signed in to this account.
    - ``about``: this account's own name, number and about text.
    - ``unlink``: sign one other device out. Give its name in ``who``.
    - ``call``: start a voice or video call. Give the person in ``who`` and "voice" or "video" in ``call_type``.

    A call rings somebody's phone, so the user is always asked first and it only
    starts when they say yes. Never start one to "check if they're there" --
    ``presence`` answers that without ringing anyone.

    Args:
        action: presence, devices, about, unlink, or call.
        who: The person's name, for presence and call; the device name, for unlink.
        call_type: For call, either "voice" (default) or "video".
    """
    verb = str(action or "").strip().lower().replace("-", "_")

    if verb == "about":
        with progress_step(context, ops.ACCOUNT_TOOL, "about", "Reading your WhatsApp profile", index=1, total=1):
            info = await web.chat_info()
        return as_payload(
            {
                "name": clip(str(info.get("name") or ""), 40),
                "phone": clip(str(info.get("phone") or ""), 24),
                "about": clip(str(info.get("about") or ""), 100),
            },
            max_chars=260,
        )

    if verb == "devices":
        with progress_step(context, ops.ACCOUNT_TOOL, "devices", "Checking signed-in devices", index=1, total=1):
            rows = await web.devices()
        if not rows:
            raise ToolError(
                "I couldn't read the list of signed-in devices. Open WhatsApp's "
                "settings and check them yourself. Nothing was changed."
            )
        return as_payload(
            {"devices": [clip(str(row.get("text") or ""), 60) for row in rows[:6]]},
            max_chars=420,
        )

    if verb == "presence":
        title = await _resolve(who)
        with progress_step(
            context, ops.ACCOUNT_TOOL, "presence",
            f"Checking whether {clip(title, 20)} is around", index=1, total=2,
        ):
            if not await web.open_chat(title):
                raise ToolError(f"I couldn't open {title}'s chat. Nothing was changed.")
        with progress_step(
            context, ops.ACCOUNT_TOOL, "read", "Reading their status", index=2, total=2
        ) as step_state:
            info = await web.chat_info()
        line = clip(str(info.get("presence") or ""), 40)
        step_state["status"] = line or "not shown"
        if not line:
            return (
                f"WhatsApp isn't showing whether {title} is online -- that's turned "
                "off in their privacy settings."
            )
        return f"{title}: {line}."

    if verb == "unlink":
        wanted = str(who or "").strip()
        if not wanted:
            raise ToolError(MSG_NEED_DEVICE)
        with progress_step(
            context, ops.ACCOUNT_TOOL, "unlink",
            f"Signing out {clip(wanted, 24)}", index=1, total=1,
        ) as step_state:
            if not await web.unlink_device(wanted):
                raise ToolError(
                    f"I couldn't find a device called {wanted} in your linked "
                    "devices, so nothing was changed."
                )
            step_state["verified"] = "it is no longer in the linked-device list"
        return f"Signed {wanted} out of your WhatsApp."

    if verb == "call":
        title = await _resolve(who)
        is_video = "video" in str(call_type or "").lower()
        call_label = "video call" if is_video else "call"
        with progress_step(
            context, ops.ACCOUNT_TOOL, "call",
            f"Starting a {call_label} with {clip(title, 20)}", index=1, total=2,
        ):
            if not await web.open_chat(title):
                raise ToolError(f"I couldn't open {title}'s chat, so no call started.")
            if not await web.start_call(title, video=is_video):
                raise ToolError(
                    f"I couldn't start a {call_label} with {title} -- WhatsApp's call "
                    "button may have moved. No call was started."
                )
        return f"Started a {call_label} with {title}. I can't tell whether they picked up."

    raise ToolError(MSG_UNKNOWN)