"""Launching desktop applications without handing the LLM a shell.

Names are allow-listed to letters, digits, spaces, and a few safe punctuation
marks, then resolved through PATH or the OS application registry. No command
string is ever assembled, so a scripted prompt cannot smuggle in extra
arguments or shell operators.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys

from .tools_compat import RunContext, ToolError, function_tool
from .tools_text import clip

SAFE_NAME = re.compile(r"^[A-Za-z0-9 ._+#-]{1,64}$")


def validate_app_name(app_name: object) -> str:
    """Normalise an app name and reject anything that looks like a command.

    Validation runs on the untruncated name so an over-long string fails
    instead of being silently shortened into something valid.
    """
    name = " ".join(str(app_name or "").split())
    if not name or not SAFE_NAME.match(name):
        raise ToolError(f"'{clip(app_name, 40)}' is not an app name I can open.")
    return name


def candidates(name: str) -> list[str]:
    """Try the bare name first, then the Windows extension variant."""
    if name.lower().endswith(".exe"):
        return [name]
    return [name, f"{name}.exe"]


def spawn(command: list[str]) -> None:
    """Start the program detached so it outlives the agent session."""
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    subprocess.Popen(command, **kwargs)


def launch(name: str) -> str:
    # Anything the OS resolves through PATH wins: no shell, no guessing.
    found = False
    last_error: OSError | None = None
    for candidate in candidates(name):
        resolved = shutil.which(candidate)
        if resolved:
            found = True
            try:
                spawn([resolved])
                return f"Opened {name}."
            except OSError as exc:
                last_error = exc
                continue

    if sys.platform == "win32":
        # ShellExecute also covers registered App Paths such as chrome.exe.
        for candidate in candidates(name):
            try:
                os.startfile(candidate)
                return f"Opened {name}."
            except FileNotFoundError:
                continue
            except OSError as exc:
                found = True
                last_error = exc
                continue
    elif sys.platform == "darwin":
        opener = shutil.which("open")
        if opener:
            try:
                result = subprocess.run(
                    [opener, "-a", name], capture_output=True, timeout=3, check=False
                )
                if result.returncode == 0:
                    return f"Opened {name}."
            except FileNotFoundError:
                pass
            except (OSError, subprocess.TimeoutExpired) as exc:
                found = True
                last_error = exc
    else:
        gtk_launch = shutil.which("gtk-launch")
        if gtk_launch:
            try:
                result = subprocess.run(
                    [gtk_launch, name], capture_output=True, timeout=3, check=False
                )
                if result.returncode == 0:
                    return f"Opened {name}."
            except FileNotFoundError:
                pass
            except (OSError, subprocess.TimeoutExpired) as exc:
                found = True
                last_error = exc

    if found:
        raise ToolError(f"I found {name}, but I couldn't start it.") from last_error
    raise ToolError(f"I couldn't find an app called '{name}'.")


@function_tool()
async def open_app(context: RunContext, app_name: str) -> str:
    """Launch an installed desktop application.

    Use it when the user asks to open, launch, or start a program. It only
    starts apps by name; it cannot read files, run scripts, or run commands.

    Args:
        app_name: Name of the program, such as "notepad", "chrome", or
            "spotify".
    """
    # A launch is a side effect. Do not let a second user utterance start the
    # same app again while the first tool call is still completing.
    disallow_interruptions = getattr(context, "disallow_interruptions", None)
    if callable(disallow_interruptions):
        disallow_interruptions()
    name = validate_app_name(app_name)
    return await asyncio.to_thread(launch, name)
