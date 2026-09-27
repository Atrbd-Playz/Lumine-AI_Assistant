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

from .apps import open_app
from .news import get_news
from .persona import recall_persona
from .tools_permission import disabled_reason, filter_permissions, tool_id
from .weather import get_weather
from .web_search import search_web

#: Ordered cheapest-first. The order is a nudge: on a tight token budget the
#: model should reach for the tool that returns the least.
ALL_TOOLS: tuple[Any, ...] = (get_weather, get_news, search_web, recall_persona, open_app)


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
        "Reaches the internet to answer a question. Nothing here changes your machine.",
    ),
    (
        "Internal",
        "Lumine's own guidance, held in the app. No network and no machine access.",
    ),
    (
        "Desktop",
        "Starts something on this computer. This is the only category that acts.",
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
#: `effect` is the field that matters most. Four of these five only answer a
#: question; `open_app` acts on the machine, and a person deciding whether to
#: leave a tool switched on needs that distinction stated rather than inferred from
#: a name.
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
        "summary": "Reads Lumine's own guidance for a delicate moment. Off by default; it is not something a conversation normally needs.",
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
        card["enabled"] = not reason
        card["disabledReason"] = reason
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
