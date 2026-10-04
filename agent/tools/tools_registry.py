"""Every tool Lumine can use, collected in one place.

To add a tool:

1. implement it in its own module with ``@function_tool()``,
2. import it here and append it to ``ALL_TOOLS``,
3. make the docstring specific, because the docstring is what the model sees.

``agent.py`` registers ``get_tools()`` on the Lumine agent, so anything listed
here is available for the whole session rather than per request.
"""

from __future__ import annotations

from typing import Any, Iterable

from .apps import find_app, open_app
from .browser.control import web_control
from .browser.tools import browse
from .desktop.control import desktop_control
from .messaging.whatsapp import send_whatsapp
from .messaging.whatsapp_account import whatsapp_account
from .messaging.whatsapp_chats import whatsapp_chats
from .messaging.whatsapp_read import whatsapp_read
from .media.control import play_media
from .news import get_news
from .persona import recall_persona
from .task_runner import cancel_task, run_task
from .tools_permission import (
    TOOLS_SECTION,
    disabled_reason,
    filter_permissions,
    permission_floor,
    tool_id,
)
from .weather import get_weather
from .web_search import search_web
from .windows_control import manage_window

#: Ordered cheapest-first. The order is a nudge: on a tight token budget the
#: model should reach for the tool that returns the least. `browse` costs a
#: page of text the others do not, and `send_whatsapp` costs the most of
#: all -- an approval card and, in the worst case, an irretrievable message.
#:
#: `run_task` sits between `browse` and `send_whatsapp` deliberately. It is the
#: right tool for anything multi-step, and putting it after the single-purpose
#: tools means a model that *can* answer with one cheap call still does.
#:
#: Inside the messaging group the same rule applies again: read, then chats, then
#: account, and the send tool last. Reading a chat list is a few hundred
#: characters; sending is an approval question and an irreversible act. A model
#: that reaches for the cheaper tool whenever both would answer the question is
#: the behaviour this ordering exists to encourage -- and on a per-minute token
#: budget it is also the cheaper mistake.
ALL_TOOLS: tuple[Any, ...] = (
    get_weather,
    get_news,
    search_web,
    recall_persona,
    find_app,
    open_app,
    manage_window,
    desktop_control,
    browse,
    run_task,
    web_control,
    cancel_task,
    play_media,
    whatsapp_read,
    whatsapp_chats,
    whatsapp_account,
    send_whatsapp,
)


#: The categories a card can sit in, in the order the Tools page shows them.
#:
#: Declared here rather than derived from the `category` strings below, because
#: "the order a person reads" is a decision and alphabetical order is not one.
#: Web comes first because it is most of what Lumine can do and least
#: interesting to distinguish; Desktop is last despite being the most
#: consequential, so a reader arriving at a tab strip is offered the harmless
#: categories before the one that starts programs.
#:
#: The descriptions are published with the list. A frontend that hardcoded these
#: would be a second place to update when a category is added, and would get the
#: wording wrong the first time somebody reworded one here.
TOOL_CATEGORIES: tuple[tuple[str, str], ...] = (
    (
        "Web",
        "Reaches the internet to answer a question, read a page, or click where you ask. Nothing here changes your machine.",
    ),
    (
        "Internal",
        "Lumine's own guidance, held in the app. No network and no machine access.",
    ),
    (
        "Messaging",
        "Reads and sends WhatsApp from your own account. Reading a chat's messages asks you first; "
        "anything that sends, deletes, blocks or signs a device out shows you exactly what it will do before it happens.",
    ),
    (
        "Media",
        "Looks through the one music folder you chose in Settings and plays a track from it. "
        "She never changes a file and never leaves your computer.",
    ),
    (
        "Desktop",
        "Launches programs and arranges their windows on this computer. The only category that changes this machine.",
    ),
)


def tool_category_list() -> list[dict[str, str]]:
    """The categories, as the frontend receives them."""
    return [{"id": name, "label": name, "description": text} for name, text in TOOL_CATEGORIES]


#: How each tool is described to a person, as opposed to the model.
#:
#: The docstring above is the model's schema and is written for a model: it argues
#: for when to call the tool and how to paraphrase the result. None of that belongs
#: on a settings card, and parsing it to get there would break the first time a
#: docstring is reworded. So the two are kept apart, and
#: `test_tool_catalog.py` fails if a tool exists without a card or a card names a
#: tool that is gone.
#:
#: `effect` is the field that matters most. Reading tools only answer a
#: question; `open_app` starts a program and `manage_window` arranges one, and a
#: person deciding whether to leave a tool switched on needs that distinction
#: stated rather than inferred from a name.
TOOL_CARDS: dict[str, dict[str, Any]] = {
    "get_weather": {
        "label": "Weather",
        "summary": "Current conditions and a two-day forecast for anywhere you name.",
        "category": "Web",
        "effect": "answers",
        "network": True,
        "parameters": [
            {
                "name": "location",
                "kind": "text",
                "required": True,
                "help": "City or place name.",
            }
        ],
    },
    "get_news": {
        "label": "News",
        "summary": "Recent headlines on a topic, or the top stories if you do not name one.",
        "category": "Web",
        "effect": "answers",
        "network": True,
        "parameters": [
            {
                "name": "topic",
                "kind": "text",
                "required": False,
                "help": "Subject to look up. Left out means top headlines.",
            }
        ],
    },
    "search_web": {
        "label": "Web search",
        "summary": "Looks something up online and returns result titles only.",
        "category": "Web",
        "effect": "answers",
        "network": True,
        "parameters": [
            {
                "name": "query",
                "kind": "text",
                "required": True,
                "help": "What to look up.",
            }
        ],
    },
    "recall_persona": {
        "label": "Persona reference",
        "summary": "Lumine's own guidance for a delicate moment. Off by default; a conversation rarely needs it.",
        "category": "Internal",
        "effect": "answers",
        "network": False,
        "parameters": [
            {
                "name": "topic",
                "kind": "text",
                "required": True,
                "help": "The situation to look up guidance for.",
            }
        ],
    },
    "find_app": {
        "label": "Find an app",
        "summary": "Looks up the names of installed programs without opening anything.",
        "category": "Desktop",
        "effect": "answers",
        "network": False,
        "parameters": [
            {
                "name": "app_name",
                "kind": "text",
                "required": True,
                "help": "Part of the program's name, such as spotify or visual studio.",
            }
        ],
    },
    "open_app": {
        "label": "Open an app",
        "summary": "Starts an installed desktop application by name. It cannot read files or run commands.",
        "category": "Desktop",
        "effect": "opens",
        "network": False,
        "parameters": [
            {
                "name": "app_name",
                "kind": "text",
                "required": True,
                "help": "Name of the program, such as notepad or spotify.",
            }
        ],
    },
    "manage_window": {
        "label": "Manage windows",
        "summary": (
            "Lists your open windows, then focuses, resizes or closes one. "
            "Force-closing always asks first."
        ),
        "category": "Desktop",
        "effect": "opens",
        "network": False,
        "parameters": [
            {
                "name": "action",
                "kind": "choice",
                "required": True,
                "help": "list, focus, minimize, maximize, restore, close, close_all, or terminate.",
            },
            {
                "name": "window",
                "kind": "text",
                "required": False,
                "help": "Part of the window's title, such as chrome. Not needed for list.",
            },
        ],
    },
    "desktop_control": {
        "label": "Control the mouse and keyboard",
        # Worded to avoid the marker strings `test_tool_catalog` checks the
        # published payload for. The guard exists to catch a credential
        # *shape*, and "secret" and "token" trip it as readily as a real key
        # would -- so the card says what it means in words that are not also
        # the words a leaked credential is made of.
        "summary": (
            "Moves your mouse and types in any application. Never into a sign-in "
            "or unlock field; anything that submits or deletes asks first."
        ),
        "category": "Desktop",
        "effect": "acts",
        "network": False,
        "parameters": [
            {
                "name": "action",
                "kind": "choice",
                "required": True,
                "help": "observe, move, click, type, or press.",
            },
            {
                "name": "target",
                "kind": "text",
                "required": False,
                "help": "The number from an observe result, or part of the text on the control.",
            },
            {
                "name": "text",
                "kind": "text",
                "required": False,
                "help": "The text to type, or the key to press such as enter or ctrl+s.",
            },
            {
                "name": "window",
                "kind": "text",
                "required": False,
                "help": "Part of a window's title, to work in that window instead of the one in front.",
            },
        ],
    },
    "browse": {
        "label": "Browse a page",
        "summary": "Opens a page in Lumine's own browser and clicks, types or presses a key by reference. Submitting a form asks first.",
        "category": "Web",
        "effect": "acts",
        "network": True,
        "parameters": [
            {
                "name": "action",
                "kind": "choice",
                "required": True,
                "help": "open, observe, act, back, forward, scroll, tab, or close.",
            },
            {
                "name": "url",
                "kind": "text",
                "required": False,
                "help": "The page to open, for action open.",
            },
            {
                "name": "ref",
                "kind": "text",
                "required": False,
                "help": "A control reference from observe, for action act.",
            },
            {
                "name": "text",
                "kind": "text",
                "required": False,
                "help": (
                    "Text to type for action act, where to scroll for action "
                    "scroll, or which tab request for action tab."
                ),
            },
            {
                "name": "key",
                "kind": "text",
                "required": False,
                "help": "A key to press for action act: Enter, Escape, Tab, an arrow key, or similar.",
            },
        ],
    },
    "run_task": {
        "label": "Run a task",
        "summary": (
            "Carries out a multi-step request end to end -- open a program, reach "
            "a page, click something -- checking each step worked."
        ),
        "category": "Desktop",
        "effect": "acts",
        "network": True,
        "parameters": [
            {
                "name": "request",
                "kind": "text",
                "required": True,
                "help": "What the user asked for, in their words.",
            },
            {
                "name": "goal",
                "kind": "text",
                "required": False,
                "help": "A narrower restatement, for a long request.",
            },
        ],
    },
    "cancel_task": {
        "label": "Stop the current task",
        "summary": (
            "Stops whatever Lumine is doing right now, including steps not yet "
            "started. It undoes nothing."
        ),
        "category": "Desktop",
        "effect": "acts",
        "network": False,
        "parameters": [],
    },
    "web_control": {
        "label": "Drive a page",
        "summary": (
            "Drives a page by what its controls are called: finds the search box, "
            "searches, clicks the third result. Submits nothing without asking."
        ),
        "category": "Web",
        "effect": "acts",
        "network": True,
        "parameters": [
            {
                "name": "action",
                "kind": "choice",
                "required": True,
                "help": (
                    "open, read, find, search, click, double_click, right_click, "
                    "hover, focus, fill, clear, type, press, select, check, "
                    "uncheck, drag, back, forward, reload, tab, pagetabs, state, "
                    "screenshot, close."
                ),
            },
            {"name": "url", "kind": "text", "required": False, "help": "The page to open."},
            {"name": "name", "kind": "text", "required": False, "help": "The control to find."},
            {"name": "role", "kind": "text", "required": False, "help": "button, link, searchbox, ..."},
            {"name": "text", "kind": "text", "required": False, "help": "What to search for, or text to type, choose, or drag."},
            {"name": "key", "kind": "text", "required": False, "help": "Key to press."},
            {"name": "strategy", "kind": "choice", "required": False, "help": "label, placeholder, alt, title, text, value, css."},
            {"name": "css", "kind": "text", "required": False, "help": "A CSS selector, as a last resort."},
            {"name": "index", "kind": "text", "required": False, "help": "Which match, 1-based."},
            {"name": "frame", "kind": "text", "required": False, "help": "Which iframe."},
            {"name": "position", "kind": "text", "required": False, "help": "'second', 'third', '2'."},
            {
                "name": "for_model",
                "kind": "choice",
                "required": False,
                # "tokens" is a word here and `test_the_payload_leaks_nothing_but_documentation`
                # forbids the string "token" in this payload, so the cost is named in
                # words rather than in the unit. The constraint is about provider
                # credentials; avoiding it here costs nothing and keeps the card honest.
                "help": (
                    "On screenshot: also show it to Lumine, not only to you. Off by "
                    "default, because a picture is paid for again on every later turn."
                ),
            },
        ],
    },
    "play_media": {
        "label": "Music library",
        "summary": (
            "Plays a track from the music folder you chose, or queues a whole "
            "folder in order. She cannot stop it."
        ),
        "category": "Media",
        "effect": "opens",
        "network": False,
        "parameters": [
            {
                "name": "action",
                "kind": "choice",
                "required": True,
                "help": "find, play, stop, or folders.",
            },
            {
                "name": "query",
                "kind": "text",
                "required": False,
                "help": "The song, artist or words to look for.",
            },
            {
                "name": "index",
                "kind": "number",
                "required": False,
                "help": "Which match, when find listed more than one.",
            },
            {
                "name": "folder",
                "kind": "text",
                "required": False,
                "help": "A folder name, to play everything in it in order.",
            },
        ],
    },
    "whatsapp_read": {
        "label": "Read WhatsApp",
        "summary": (
            "Reads your WhatsApp without changing it: chats, unread ones, a chat's "
            "messages. Reading messages asks you once per conversation."
        ),
        "category": "Messaging",
        "effect": "answers",
        "network": True,
        "parameters": [
            {
                "name": "action",
                "kind": "choice",
                "required": True,
                "help": "chats, unread, messages, find, or contact.",
            },
            {
                "name": "chat",
                "kind": "text",
                "required": False,
                "help": "The person's name. Needed for messages, find and contact.",
            },
        ],
    },
    "whatsapp_chats": {
        "label": "Manage WhatsApp chats",
        "summary": (
            "Marks read, pins, mutes, archives, stars, or deletes a message. "
            "Deleting for everyone always asks first."
        ),
        "category": "Messaging",
        "effect": "acts",
        "network": True,
        "parameters": [
            {
                "name": "action",
                "kind": "choice",
                "required": True,
                "help": (
                    "mark_read, mark_unread, pin, unpin, mute, unmute, archive, "
                    "unarchive, star, unstar, delete, or block."
                ),
            },
            {"name": "chat", "kind": "text", "required": True, "help": "The person's name."},
            {
                "name": "message",
                "kind": "text",
                "required": False,
                "help": "A message id from a read. Needed for star and delete.",
            },
        ],
    },
    "whatsapp_account": {
        "label": "WhatsApp account",
        "summary": (
            "Checks the account: online, which devices are signed in, your profile, "
            "and starting a call. Signing out and calling ask first."
        ),
        "category": "Messaging",
        "effect": "acts",
        "network": True,
        "parameters": [
            {
                "name": "action",
                "kind": "choice",
                "required": True,
                "help": "presence, devices, about, unlink, or call.",
            },
            {
                "name": "who",
                "kind": "text",
                "required": False,
                "help": "The person's name, or the device name for unlink.",
            },
        ],
    },
    "send_whatsapp": {
        "label": "Send a WhatsApp",
        "summary": (
            "Messages someone on WhatsApp. The text is shown on your own screen "
            "first, and sent only after you say yes."
        ),
        "category": "Messaging",
        "effect": "sends",
        "network": True,
        "parameters": [
            {
                "name": "recipient",
                "kind": "text",
                "required": True,
                "help": "A phone number in international form, or a contact name when automation is enabled.",
            },
            {
                "name": "message",
                "kind": "text",
                "required": True,
                "help": "The exact text to send, as the user wants it worded.",
            },
            {
                "name": "attachment",
                "kind": "text",
                "required": False,
                "help": "A file on this computer to send with it, from your downloads, pictures or desktop.",
            },
            {
                "name": "reply_to",
                "kind": "text",
                "required": False,
                "help": "A message id from a read, to reply quoting that message.",
            },
        ],
    },
}


def tool_cards() -> list[dict[str, Any]]:
    """Every tool, described for a person, with whether it is on right now.

    `enabled` is read from the same environment toggles `get_tools()` applies, so
    a card cannot claim a tool is available when the worker will not hand it to the
    model.
    """
    cards: list[dict[str, Any]] = []
    for tool in ALL_TOOLS:
        ident = tool_id(tool)
        card = dict(TOOL_CARDS.get(ident, {}))
        card["id"] = ident

        reason = disabled_reason(tool)
        card.setdefault("label", ident)
        card.setdefault("summary", "")
        card.setdefault("category", "Other")
        card.setdefault("effect", "answers")
        card.setdefault("network", False)
        card.setdefault("parameters", [])
        # Whether the tool asks before it acts, read from the same class floor
        # the gate enforces at call time. Computed rather than declared so the
        # badge and the behaviour cannot drift: a card is a promise, and a
        # promise written twice is a promise that can be broken once.
        card["permission"] = permission_floor(ident)
        card["enabled"] = not reason
        card["disabledReason"] = reason
        # Every tool has an on/off switch, and the switch is a *document* key
        # rather than an environment variable. Published rather than assumed so
        # the page writes the key the worker reads rather than one it invented:
        # a switch that saves to the wrong place looks perfect and does nothing.
        card["switchable"] = True
        card["configKey"] = f"{TOOLS_SECTION}.{ident}"
        cards.append(card)
    return cards


def get_tools() -> list[Any]:
    """Tools enabled for this run.

    Called when the agent starts, after ``load_dotenv()``, so environment
    toggles in ``.env`` apply without a code change.
    """
    return filter_permissions(ALL_TOOLS)


def tool_ids(tools: Iterable[Any] | None = None) -> list[str]:
    """Names of the tools, for startup logging and diagnostics."""
    return [tool_id(tool) for tool in (ALL_TOOLS if tools is None else tools)]
