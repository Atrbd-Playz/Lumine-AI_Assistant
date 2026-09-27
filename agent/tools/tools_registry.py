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
from .tools_permission import filter_permissions, tool_id
from .weather import get_weather
from .web_search import search_web

#: Ordered cheapest-first. The order is a nudge: on a tight token budget the
#: model should reach for the tool that returns the least.
ALL_TOOLS: tuple[Any, ...] = (get_weather, get_news, search_web, recall_persona, open_app)


def get_tools() -> list[Any]:
    """Tools enabled for this run.

    Called when the agent starts, after ``load_dotenv()``, so environment
    toggles in ``.env`` apply without a code change.
    """
    return filter_permissions(ALL_TOOLS)


def tool_ids(tools: Iterable[Any] | None = None) -> list[str]:
    """Names of the tools, for startup logging and diagnostics."""
    return [tool_id(tool) for tool in (ALL_TOOLS if tools is None else tools)]
