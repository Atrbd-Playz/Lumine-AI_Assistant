"""Launching desktop applications without handing the LLM a shell.

Names are allow-listed to letters, digits, spaces, and a few safe punctuation
marks, then resolved through PATH or the OS application registry. No command
string is ever assembled, so a scripted prompt cannot smuggle in extra
arguments or shell operators.

A launch is also **idempotent for a few seconds**. One ``launch()`` starts
exactly one process -- that is verified by a test, not assumed -- so a second
instance means the tool was *called* twice: two identical function calls in
one turn, or the same request arriving again while the first is still
completing. Nothing above this layer can prevent that, so the side effect
defends itself here, where the fact that it already happened actually lives.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from . import app_index, tool_results
from .tools_compat import RunContext, ToolError, function_tool
from .tools_text import clip

try:
    from ..runtime.permission_gate import require_approval
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime.permission_gate import require_approval

logger = logging.getLogger("lumine")

SAFE_NAME = re.compile(r"^[A-Za-z0-9 ._+#-]{1,64}$")

#: How long one successful launch of an app suppresses the next one.
#:
#: Ten seconds is the difference between "the same request, not a new one" and
#: a deliberate second request: long enough to swallow a duplicate issued inside
#: a single turn, far shorter than the gap between asking for something and
#: asking again.
#:
#: It cannot be fixed upstream instead. ``parallel_tool_calls: False`` is set
#: only for the cascade (``settings/llm_config.py``), and the Gemini Live
#: realtime models this project defaults to expose no such switch at all -- so
#: a provider-level suppression does not exist to configure.
#:
#: ``LUMINE_LAUNCH_DEDUP_SECONDS`` overrides it, and ``0`` turns the guard off
#: for anyone who genuinely wants two.
LAUNCH_DEDUP_SECONDS = 10.0

#: Ceiling on that override, so a fat-fingered year does not silence launches
#: until the worker restarts.
MAX_LAUNCH_DEDUP_SECONDS = 3600.0

_launch_lock = threading.Lock()
#: Normalised name -> monotonic time it was last claimed. Module state on
#: purpose: the duplicate it exists to catch may arrive in a different
#: ``asyncio.to_thread`` worker than the call it is duplicating.
_launches: dict[str, float] = {}

#: Read through a name so tests can move time instead of sleeping.
_now = time.monotonic


def dedup_window() -> float:
    """Seconds during which a repeat launch of one app is suppressed."""
    raw = (os.getenv("LUMINE_LAUNCH_DEDUP_SECONDS", "") or "").strip()
    if not raw:
        return LAUNCH_DEDUP_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return LAUNCH_DEDUP_SECONDS
    if not math.isfinite(value):
        return LAUNCH_DEDUP_SECONDS
    return max(0.0, min(value, MAX_LAUNCH_DEDUP_SECONDS))


def launch_key(name: str) -> str:
    """What "the same app" means to the dedup window.

    Case-folded and stripped of the Windows extension, so ``Notepad``,
    ``notepad`` and ``notepad.exe`` are one app rather than three -- a model
    that spelled the same request three ways should not get three windows.
    """
    key = " ".join(name.split()).casefold()
    if key.endswith(".exe"):
        key = key[: -len(".exe")].rstrip()
    return key


def _claim(key: str) -> bool:
    """Reserve a launch of ``key``.

    ``False`` means a launch of the same app is already in flight or is fresh
    enough that this is the same request. The reservation is written *before*
    the process starts, so two calls racing each other cannot both win: the
    lock is what makes check-and-set one step rather than two.
    """
    window = dedup_window()
    if window <= 0:
        return True
    now = _now()
    with _launch_lock:
        for stale in [name for name, at in _launches.items() if now - at >= window]:
            _launches.pop(stale, None)
        if key in _launches:
            return False
        _launches[key] = now
        return True


def _release(key: str) -> None:
    """Give a reservation back when nothing was launched, so a retry can be."""
    with _launch_lock:
        _launches.pop(key, None)


def reset_launch_history() -> None:
    """Forget every recent launch. Tests and nothing else use this."""
    with _launch_lock:
        _launches.clear()


#: Public names for the same two operations. The orchestrator in `task_runner`
#: claims through these rather than reaching for the underscore-prefixed pair,
#: because the dedup window is a property of *launching an application* and both
#: entry points have to share it -- two implementations of "already opening" is
#: two windows waiting to happen.
claim_launch = _claim
release_launch = _release


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


def _run_opener(command: list[str]) -> None:
    """Run a macOS/Linux opener and convert failure into ``OSError``.

    Written once because `start_entry` and the legacy chain both need the same
    rule: a non-zero exit or a missing binary is a *miss*, not a success and
    not a crash -- `launch` treats it exactly like a path that did not resolve.
    """
    try:
        result = subprocess.run(command, capture_output=True, timeout=5, check=False)
    except FileNotFoundError as exc:
        raise OSError(f"{command[0]} is not installed") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(str(exc)) from exc
    if result.returncode != 0:
        raise OSError(f"{command[0]} exited {result.returncode}")


def start_entry(app: app_index.AppEntry) -> None:
    """Start one indexed entry.

    Every branch is a list handed to ``Popen``/``startfile`` or a bare
    ``scheme:`` -- never a string with anything spliced in, and never a shell.
    The target is data this machine reported (a path, an AppID, a desktop id),
    and `validate_app_name` already refused anything a request might have tried
    to smuggle an argument through. An entry that no longer resolves raises
    ``OSError`` so `launch` can fall through to its legacy chain, which is
    unchanged from when it was the only chain.
    """
    if not app.target:
        raise OSError("the index entry has no target")
    if app.kind == app_index.KIND_START_APP:
        # Validated once at collection and again here: this is the one kind
        # whose target gets paired with a constant prefix, and a prefix plus an
        # unchecked value is exactly how "no shell" quietly becomes one.
        if not app_index.valid_app_id(app.target):
            raise OSError("unusable app id in the index")
        spawn(["explorer.exe", f"shell:AppsFolder\\{app.target}"])
        return
    if app.kind == app_index.KIND_PROTOCOL:
        # Arguments are never appended: `startfile("spotify:")` opens Spotify,
        # and `startfile("http:...")` is unreachable anyway -- that scheme is
        # on the deny list in `app_index`.
        if not hasattr(os, "startfile"):
            raise OSError("protocol launches are Windows-only here")
        os.startfile(app.target)
        return
    if app.kind == app_index.KIND_DESKTOP:
        # By id, through the desktop environment -- the Exec line of a
        # .desktop file is a command line we refuse to parse.
        _run_opener(["gtk-launch", app.target])
        return
    if app.kind == app_index.KIND_BUNDLE or sys.platform == "darwin":
        _run_opener(["open", app.target])
        return
    if app.kind == app_index.KIND_SHORTCUT:
        if not hasattr(os, "startfile"):
            raise OSError("shortcuts are Windows-only here")
        # Windows resolves the .lnk/.url itself -- target, arguments, working
        # directory and all -- which is one less thing to parse and trust.
        os.startfile(app.target)
        return
    spawn([app.target])


def launch_with_entry(app: app_index.AppEntry) -> str:
    """Start one already-resolved index entry.

    Split out of :func:`launch` so a caller that has *already* resolved a name --
    `app_resolver`, which needed the window facts to make that decision -- does
    not run the whole chain again and reach a second answer. Two resolutions of
    the same name in one turn is how "open Krita" picks a shortcut on the first
    pass and an App Paths executable on the second.
    """
    start_entry(app)
    return f"Opened {app.name}."


def launch(name: str) -> str:
    found = False
    last_error: OSError | None = None

    # The index first. PATH and the App Paths registry cannot see a Start Menu
    # shortcut, a Store app, or a browser's PWA -- which is most of what a
    # person means by "an app" -- and a registered protocol is how some apps
    # (Spotify) expose a launcher at all. A miss, or an entry that no longer
    # resolves, falls through to the chain below unchanged: the index widens
    # what can be found, it does not become the only door.
    app = app_index.resolve(name)
    if app is not None:
        found = True
        try:
            start_entry(app)
            return f"Opened {name}."
        except OSError as exc:
            last_error = exc

    # Anything the OS resolves through PATH wins: no shell, no guessing.
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
    If you do not know the app's exact name, `find_app` can look it up first.

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

    # Approval runs before the dedup claim below: a refused launch must not
    # consume the window, or the user's "yes, do it after all" would find the
    # second attempt suppressed as a duplicate of something that never
    # happened. At the default policy this returns immediately -- the gate
    # exists so a per-tool `ask` in settings has somewhere real to bite.
    await require_approval(
        context,
        tool="open_app",
        action="launch",
        action_class="act",
        summary=f"Open the app {name}",
        target=name,
    )

    # Belt to that suspenders' braces. `disallow_interruptions` stops a second
    # *utterance*; this stops a second *call* -- two identical function calls
    # in one turn, which no interruption setting can see because there is only
    # ever one utterance.
    key = launch_key(name)
    if not _claim(key):
        logger.info("[open_app] suppressed a repeat launch of %s", name)
        return f"I already opened {name} just now."
    try:
        return await asyncio.to_thread(launch, name)
    except BaseException:
        # Nothing started, so nothing is open and the window must not say
        # otherwise: a model that is told "already open" about a launch that
        # failed will not try again, and the user gets silence instead of an
        # app.
        _release(key)
        raise


#: How many names `find_app` will return. Five is enough to disambiguate a
#: vague request and small enough that the result joins the conversation as a
#: list rather than as a paragraph -- this payload is re-sent on every later
#: turn, which is what the ceiling in `tool_results` exists for.
MAX_FIND = 5


@function_tool()
async def find_app(context: RunContext, app_name: str) -> str:
    """Find installed applications by name, without opening anything.

    Use this when the user asks what is installed, or when you are unsure how
    an app's name is spelled before opening it. It searches the Start menu,
    Store apps, and registered programs, and returns matching names only. It
    starts nothing and changes nothing.

    Args:
        app_name: Part of the program's name, such as "spotify" or
            "visual studio".
    """
    name = validate_app_name(app_name)

    # A read, so the default policy answers immediately -- this call is here so
    # that a settings entry for `find_app` (ask, or off) has something real to
    # bite. `search_web` and `get_news` pass through the same door for the
    # same reason.
    await require_approval(
        context,
        tool="find_app",
        action="search",
        action_class="read",
        summary=f"Look up installed apps matching {name}",
        target=name,
    )

    # In a worker thread: a cold *fast* build is ~88 ms of directory walks, and
    # a miss may then spend up to three seconds on the slow tier. Either way the
    # turn must not hold the event loop while it happens.
    matches = await asyncio.to_thread(app_index.find, name, limit=MAX_FIND, complete=True)
    if not matches:
        raise ToolError(f"I couldn't find an app called '{clip(name, 60)}'.")
    # Wrapped, because an app's display name is chosen by whoever installed
    # it -- a PWA's shortcut is named by the site that wrote it -- and this
    # list is data the model reads, not text it wrote.
    return tool_results.wrap_untrusted(
        tool_results.as_payload({"matches": [{"name": app.name} for app in matches]})
    )
