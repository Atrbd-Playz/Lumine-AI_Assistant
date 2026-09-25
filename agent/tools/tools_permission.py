"""Decides which tools Lumine exposes to the LLM.

Kept apart from the registry so the desktop layer can widen or narrow the tool
surface through environment variables instead of code changes.
"""

import os
from typing import Any, Iterable

# Tools that act on the user's machine instead of only answering a question.
DESKTOP_TOOLS = frozenset({"open_app"})

DISABLED_TOOLS_VAR = "LUMINE_DISABLED_TOOLS"
APP_LAUNCH_VAR = "LUMINE_ENABLE_APP_LAUNCH"

_TRUTHY = {"1", "true", "yes", "on"}


def tool_id(tool: Any) -> str:
    """Return a tool's id whether it is a FunctionTool or a plain function."""
    return str(getattr(tool, "id", None) or getattr(tool, "__name__", "") or tool)


def _disabled_ids() -> set[str]:
    raw = os.getenv(DISABLED_TOOLS_VAR, "") or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def tool_allowed(tool: Any) -> bool:
    """Check a single tool against the environment toggles."""
    ident = tool_id(tool).lower()
    if ident in _disabled_ids():
        return False
    if ident in DESKTOP_TOOLS:
        enabled = (os.getenv(APP_LAUNCH_VAR, "true") or "true").strip().lower()
        if enabled not in _TRUTHY:
            return False
    return True


def filter_permissions(tools: Iterable[Any]) -> list[Any]:
    """Drop every tool the current environment does not permit."""
    return [tool for tool in tools if tool_allowed(tool)]
