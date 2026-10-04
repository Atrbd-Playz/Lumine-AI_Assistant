"""Window control on Windows: ctypes calls, no shell, no input in a command.

Two things this module will not do are worth stating first. It will not start
anything -- that is `open_app`, and keeping the verbs apart is what lets each
tool carry an honest action class. And it will not pass a window *title* to a
command line: titles are arbitrary text (a browser's title bar is a string the
open internet chose), and this module only ever hands handles to Win32 calls,
so there is nothing to escape and nothing to quote.

The verbs and their classes are fixed in :data:`ACTION_CLASSES`, in code.
``list`` is a read and arranging a window is an act. **Closing is an act too,
and deliberately so**: ``WM_CLOSE`` hands the decision to the window's own
process, so a document with unsaved work still prompts exactly as it would if
a person had clicked the X -- the application keeps the safeguard that matters
and this tool keeps the ordinary request ordinary. What stays behind the
question are the two verbs that cannot hand anything back:

- ``terminate`` force-kills the process, which no dialog can interrupt, so it
  is ``destructive`` and always asks;
- ``close_all`` shuts many windows at once, so it is ``sensitive`` and the
  approval read-back carries the count -- the number is the thing a person
  needs to see before saying yes to a bulk action.

A tool declares its class at the call site; the model never touches that
value. A verb that could lose work is also *verified*: the close is confirmed
by watching the handle disappear, and a window that is still there is
reported as still there rather than as closed. Saying "Closed" about a window
that merely received a request is the same class of lie as describing a stale
frame as current.

Cross-platform: the platform has one seam. Everything past :func:`list_windows`
is Windows-only and says so with a plain ``ToolError`` instead of an
``AttributeError`` about ``windll``, so a macOS/Linux session gets a sentence
it can relay.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from . import tool_results
from .tools_compat import RunContext, ToolError, function_tool
from .tools_text import clip

try:
    from ..runtime.permission_gate import require_approval
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime.permission_gate import require_approval

#: The verbs, and the class each one carries. A new verb without a class here
#: fails :func:`classify_action`'s fail-closed default (``sensitive``), so the
#: ladder can only be climbed down by editing this map -- which is a code
#: change with a test next to it, not a word in a request.
#:
#: The line between ``act`` and ``sensitive`` here is *whether something other
#: than this tool can still say no*. ``close`` asks the window's own process,
#: which answers with its unsaved-work dialog; ``terminate`` and ``close_all``
#: have nothing behind them to do that, so they are the ones that ask us.
ACTION_CLASSES: dict[str, str] = {
    "list": "read",
    "focus": "act",
    "minimize": "act",
    "maximize": "act",
    "restore": "act",
    "close": "act",
    "close_all": "sensitive",
    "terminate": "destructive",
}

ACTIONS: tuple[str, ...] = tuple(ACTION_CLASSES)

MSG_UNSUPPORTED = "Window control works on Windows right now. Nothing was done."

#: How many titles the ``list`` action shows, and how long each may be. A
#: window list is re-sent on every later turn like any other tool result, so
#: it is the *count* that answers "what is open" and ten titles that answer
#: "which one did you mean".
MAX_LISTED = 10
TITLE_LIMIT = 60

#: How long a close is watched before the tool decides it did not take. Long
#: enough for a real application to save and shut, short enough that a window
#: stuck on an unsaved-work dialog does not stall the turn -- the point is not
#: to force an answer but to stop claiming one.
CLOSE_CONFIRM_SECONDS = 2.0
CLOSE_POLL_SECONDS = 0.1


def classify_action(action: object) -> str:
    """The action class for a verb, fail-closed.

    Unknown means ``sensitive`` rather than ``act``: mislabelling an action as
    milder is the dangerous direction, and an unknown verb should cost a
    question even if one day it slips past validation.
    """
    return ACTION_CLASSES.get(str(action or "").strip().lower(), "sensitive")


# ---------------------------------------------------------------------------
# Pure matching
# ---------------------------------------------------------------------------


def normalize_title(title: object) -> str:
    """Lowercased, whitespace-collapsed -- what "the same window" means here."""
    return " ".join(str(title or "").split()).casefold()


def match_window(query: object, windows: "list[WindowInfo]", *, limit: int = 5) -> "list[WindowInfo]":
    """Windows whose title answers ``query``, best first.

    Same three tiers as the app index -- exact, prefix, containment -- because
    a person says "chrome", not the whole title bar. Ties keep enumeration
    order, so the first of two identically-named windows is always the same
    one and a test can assert which.
    """
    wanted = normalize_title(query)
    if not wanted:
        return []
    hits: list[tuple[int, int, WindowInfo]] = []
    for position, window in enumerate(windows):
        title = normalize_title(window.title)
        if title == wanted:
            tier = 0
        elif title.startswith(wanted):
            tier = 1
        elif wanted in title:
            tier = 2
        else:
            continue
        hits.append((tier, position, window))
    hits.sort(key=lambda row: (row[0], row[1]))
    return [window for _tier, _position, window in hits[: max(0, limit)]]


# ---------------------------------------------------------------------------
# Enumeration -- Windows only
# ---------------------------------------------------------------------------


class WindowInfo:
    """One visible top-level window.

    A plain class rather than a dataclass because it is constructed inside a
    ctypes callback, where simplicity beats expressiveness -- and because the
    handle is an int, it stays trivially assertable.
    """

    __slots__ = ("handle", "title", "pid")

    def __init__(self, handle: int, title: str, pid: int = 0) -> None:
        self.handle = handle
        self.title = title
        self.pid = pid

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"WindowInfo(handle={self.handle!r}, title={self.title!r}, pid={self.pid!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WindowInfo):
            return NotImplemented
        return (self.handle, self.title, self.pid) == (other.handle, other.title, other.pid)

    def __hash__(self) -> int:
        # Defining `__eq__` clears `__hash__` by default, which would make
        # these unusable in sets; this tuple is the whole content anyway.
        return hash((self.handle, self.title, self.pid))


#: DWMWA_CLOAKED. A UWP app holding a suspended background window reports
#: itself visible through ``IsWindowVisible``; cloaking is what marks it as
#: not really on screen, and listing those would mean "focus" and "close"
#: aiming at windows the user cannot see.
_DWMWA_CLOAKED = 14


def _enum_windows() -> "list[WindowInfo]":
    """Every visible, uncloaked, titled top-level window. Win32 only.

    The enum callback must never raise: an exception unwinding through ctypes
    is undefined behaviour, not an error message -- so everything inside it is
    wrapped and a failure skips one window rather than killing the listing.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    ours = os.getpid()
    found: list[WindowInfo] = []

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            cloaked = wintypes.DWORD(0)
            if dwmapi.DwmGetWindowAttribute(
                hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
            ) == 0 and cloaked.value:
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == ours:
                # The worker shows no window today; the guard exists so a
                # future one cannot be closed from inside its own tool call.
                return True
            found.append(WindowInfo(handle=int(hwnd), title=title, pid=int(pid.value)))
        except Exception:  # noqa: BLE001 - never unwind through ctypes
            return True
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return found


def list_windows() -> "list[WindowInfo]":
    """Visible windows, or a plain refusal on a platform without them."""
    if sys.platform != "win32":
        raise ToolError(MSG_UNSUPPORTED)
    return _enum_windows()


# ---------------------------------------------------------------------------
# The verbs -- Windows only
# ---------------------------------------------------------------------------


def _user32():
    import ctypes

    return ctypes.windll.user32


#: Win32 show commands, spelled out because ``ShowWindow(6)`` is unreadable
#: and getting one wrong means "minimize" that maximizes.
_SW_MINIMIZE = 6
_SW_MAXIMIZE = 3
_SW_RESTORE = 9
_WM_CLOSE = 0x0010


def _focus(window: WindowInfo) -> None:
    """Bring a window to the front, working around the foreground lock.

    Windows refuses to hand foreground to a background process -- the rule that
    stops apps stealing focus. The documented workaround is to attach our input
    queue to the current foreground window's thread while we ask, which is what
    a human's alt-tab does implicitly. If the window still is not front after
    that, this raises rather than reporting a focus that did not happen.
    """
    user32 = _user32()
    hwnd = int(window.handle)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
    foreground = int(user32.GetForegroundWindow() or 0)
    if foreground != hwnd:
        current_thread = user32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        attached = user32.AttachThreadInput(current_thread, foreground_thread, True)
        try:
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
    else:
        user32.SetForegroundWindow(hwnd)
    if int(user32.GetForegroundWindow() or 0) != hwnd:
        raise OSError("the window would not come to the front")


def _minimize(window: WindowInfo) -> None:
    if not _user32().ShowWindow(int(window.handle), _SW_MINIMIZE):
        raise OSError("the window would not minimize")


def _maximize(window: WindowInfo) -> None:
    if not _user32().ShowWindow(int(window.handle), _SW_MAXIMIZE):
        raise OSError("the window would not maximize")


def _restore(window: WindowInfo) -> None:
    if not _user32().ShowWindow(int(window.handle), _SW_RESTORE):
        raise OSError("the window would not restore")


def _is_window(hwnd: int) -> bool:
    """Whether a handle is still a live window. Never raises: see `_still_open`."""
    try:
        return bool(_user32().IsWindow(int(hwnd)))
    except Exception:  # noqa: BLE001 - ctypes failures must not kill a verdict
        return False


def _still_open(handles: "set[int]", seconds: float = CLOSE_CONFIRM_SECONDS) -> "set[int]":
    """The handles that are *still* windows after up to ``seconds``.

    Checked immediately before any sleeping, so a window that closed the
    moment it was asked costs no wait at all, and a caller asking about one
    handle never blocks for longer than the deadline. A failure to ask the
    question at all answers ``True`` -- "still open" is the reading under
    which nothing further happens, which is the safe direction.
    """
    remaining = {int(handle) for handle in handles if handle}
    deadline = time.monotonic() + float(seconds)
    while remaining:
        remaining = {handle for handle in remaining if _is_window(handle)}
        if not remaining or time.monotonic() >= deadline:
            break
        time.sleep(CLOSE_POLL_SECONDS)
    return remaining


def _post_close(window: WindowInfo) -> bool:
    """``WM_CLOSE`` -- a request, not a kill.

    The window's own process decides what to do: a document with unsaved work
    prompts, which is exactly the behaviour a user wants and the reason this
    verb is an ordinary act rather than a destructive one. Nothing here
    force-terminates, so nothing here can lose a file the app would have
    asked about first.
    """
    return bool(_user32().PostMessageW(int(window.handle), _WM_CLOSE, 0, 0))


def _close(window: WindowInfo) -> str:
    """Ask a window to close, then watch to see whether it did.

    The sentence this returns is the whole point of the wait: "asked to
    close" and "closed" are different claims, and a model relaying the first
    as the second is how a user ends up believing a window is gone when it is
    sitting on an unsaved-changes dialog.
    """
    if not _post_close(window):
        raise OSError("the close request would not reach the window")
    title = clip(window.title, TITLE_LIMIT)
    if _still_open({int(window.handle)}):
        return f"Asked {title} to close, but it is still open -- it may be asking about unsaved work."
    return f"Closed {title}."


#: Win32 process rights and results, spelled out because the defaults ctypes
#: guesses at are 32-bit and a handle that has been truncated is a handle to
#: something else entirely.
_PROCESS_TERMINATE = 0x0001
_PROCESS_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0

#: Never a candidate for a force kill, whatever window claims to belong to
#: them. Our own process is already excluded by the enumerator; these two are
#: excluded here, so an enumeration bug cannot become a system crash.
_PROTECTED_PIDS = frozenset({0, 4})


def _kill(pid: int) -> None:
    """Force a process to exit and wait until it has. Windows only.

    Opened with ``SYNCHRONIZE`` as well as ``TERMINATE`` for one reason:
    ``TerminateProcess`` returns as soon as the request is *accepted*, so a
    caller that trusted it alone would report a kill that had not happened
    yet. Waiting on the same handle is what turns "requested" into "done".
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SYNCHRONIZE, False, int(pid))
    if not handle:
        raise OSError("that process could not be opened for termination")
    try:
        if not kernel32.TerminateProcess(handle, 1):
            raise OSError("the process refused to be terminated")
        waited = kernel32.WaitForSingleObject(handle, int(CLOSE_CONFIRM_SECONDS * 1000))
        if waited != _WAIT_OBJECT_0:
            raise OSError("the process did not exit after the termination request")
    finally:
        kernel32.CloseHandle(handle)


def _terminate(window: WindowInfo) -> str:
    """Force-kill the process behind a window, and say so only when it went.

    This is the verb with no dialog behind it, which is why it is destructive
    and always asks. It ends every window of that process, not only this one,
    so the sentence names the process as well as the window -- a user who
    asked about one tab should not learn the app is gone from a message that
    only ever mentioned the tab.
    """
    pid = int(window.pid)
    if pid in _PROTECTED_PIDS:
        raise ToolError("That window belongs to a protected system process. Nothing was done.")
    _kill(pid)
    title = clip(window.title, TITLE_LIMIT)
    if _still_open({int(window.handle)}):
        raise OSError("the window outlived its process")
    return f"Force-terminated {title} (process {pid})."


def _close_many(windows: "list[WindowInfo]") -> str:
    """Ask every window in the list to close, then report each outcome.

    One shared deadline instead of one per window: a bulk close must not cost
    ``2s x N``, and every request goes out at the same moment anyway. The
    result names what closed and what did not, because "closed three windows"
    when one is still on a save dialog is a summary that cannot be trusted.
    """
    if sys.platform != "win32":
        raise ToolError(MSG_UNSUPPORTED)
    asked: "list[WindowInfo]" = []
    refused: "list[WindowInfo]" = []
    for window in windows:
        if _post_close(window):
            asked.append(window)
        else:
            refused.append(window)

    still = _still_open({int(window.handle) for window in asked}) if asked else set()
    closed = [window for window in asked if int(window.handle) not in still]
    waiting = [window for window in asked if int(window.handle) in still]

    parts: "list[str]" = []
    if closed:
        noun = "window" if len(closed) == 1 else "windows"
        parts.append(f"Closed {len(closed)} {noun}: {_name_list(closed)}.")
    if waiting:
        parts.append(
            f"Still open: {_name_list(waiting)} -- {len(waiting)} did not close, "
            "so at least one is probably asking about unsaved work."
        )
    if refused:
        parts.append(f"{len(refused)} ignored the close request.")
    return " ".join(parts) or "No window answered the close request."


_APPLIERS = {
    "focus": _focus,
    "minimize": _minimize,
    "maximize": _maximize,
    "restore": _restore,
    "close": _close,
    "terminate": _terminate,
}


def _describe(action: str, window: WindowInfo) -> str:
    """What to tell the user happened, in one sentence they can check.

    Only reached for verbs that do not watch their own result (see
    :func:`_apply`). It still spells out ``close`` and ``terminate`` so that a
    future applier which stopped returning a sentence would produce a truthful
    one -- "asked to close", "sent a termination request" -- rather than a
    claim that something finished when nobody checked.

    The title is clipped: it is arbitrary text this module did not choose, and
    one clipped title inside our own sentence is a caption rather than a
    payload. The bulk list -- the place where many titles become *data* the
    model reads -- is wrapped as untrusted instead, in the tool below.
    """
    title = clip(window.title, TITLE_LIMIT)
    if action == "focus":
        return f"Focused {title}."
    if action == "minimize":
        return f"Minimized {title}."
    if action == "maximize":
        return f"Maximized {title}."
    if action == "restore":
        return f"Restored {title}."
    if action == "terminate":
        return f"Sent {title} a termination request."
    return f"Asked {title} to close."


def _apply(action: str, window: WindowInfo) -> str:
    """Run one already-approved verb. Separate so tests never touch ctypes."""
    if sys.platform != "win32":
        raise ToolError(MSG_UNSUPPORTED)
    applier = _APPLIERS.get(action)
    if applier is None:
        raise ToolError(f"Tell me one of: {', '.join(ACTIONS)}.")
    sentence = applier(window)
    # A verb that watched the outcome writes its own sentence: only it knows
    # whether the window actually went. The arranging verbs had nothing to
    # watch, so theirs comes from `_describe`.
    return sentence if isinstance(sentence, str) else _describe(action, window)


def _name_list(windows: "list[WindowInfo]", limit: int = 3) -> str:
    """Up to ``limit`` titles and a count -- what a bulk read-back needs.

    Titles are text the open internet chose, so only a few are quoted and
    each is clipped. The *number* is what a person decides on when approving
    a bulk action; the names only make the number concrete.
    """
    names = [clip(window.title, TITLE_LIMIT) for window in windows[:limit]]
    extra = len(windows) - limit
    if extra > 0:
        names.append(f"and {extra} more")
    return ", ".join(names)


def _bulk_summary(action: str, windows: "list[WindowInfo]") -> str:
    """The approval read-back for a verb that applies to every match."""
    noun = "window" if len(windows) == 1 else "windows"
    if action == "close_all":
        return f"Close {len(windows)} {noun}: {_name_list(windows)}"
    return f"Act on {len(windows)} {noun}: {_name_list(windows)}"


def _summary(action: str, window: WindowInfo) -> str:
    """The approval read-back: what will happen, to which window, verbatim."""
    title = clip(window.title, TITLE_LIMIT)
    if action == "close":
        return f"Close the {title} window"
    if action == "terminate":
        return f"Force-terminate the process running {title}"
    if action == "focus":
        return f"Bring the {title} window to the front"
    if action == "minimize":
        return f"Minimize the {title} window"
    if action == "maximize":
        return f"Maximize the {title} window"
    if action == "restore":
        return f"Restore the {title} window"
    return f"Act on the {title} window"


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def _context_latch(context: RunContext) -> None:
    """Hold the turn still while an action is in flight, like `open_app` does."""
    latch = getattr(context, "disallow_interruptions", None)
    if callable(latch):
        latch()


@function_tool()
async def manage_window(context: RunContext, action: str, window: str = "") -> str:
    """List, focus, minimize, maximize, restore, close, or force-terminate windows.

    Use it when the user asks to switch to a window, put it back, close it,
    or close several at once. "close" sends the window a normal close
    request, so the window's own application still gets to warn about unsaved
    work, and it runs without asking; the result says whether the window
    actually closed. "terminate" force-kills the process and "close_all"
    closes every matching window -- both ask the user first, and the
    termination ends every window that process has. It never starts a program
    (that is open_app) and never touches files. Window titles are matched
    loosely, so "chrome" finds a Chrome window; use action "list" to see what
    is open.

    Args:
        action: What to do: "list", "focus", "minimize", "maximize",
            "restore", "close", "close_all", or "terminate".
        window: Part of the window's title, such as "chrome". Not needed for
            "list". For "close_all" it selects which windows to close.
    """
    normalized = str(action or "").strip().lower()
    if normalized not in ACTIONS:
        raise ToolError(f"Tell me one of: {', '.join(ACTIONS)}.")

    if normalized == "list":
        # A read of the desktop, through the same gate as every other tool so
        # a settings entry for `manage_window` can tighten it. Under the
        # default policy this returns immediately.
        await require_approval(
            context,
            tool="manage_window",
            action="list",
            action_class="read",
            summary="List the open windows",
        )
        windows = await asyncio.to_thread(list_windows)
        titles = [clip(item.title, TITLE_LIMIT) for item in windows[:MAX_LISTED]]
        # Wrapped, because a browser's title bar is text the open internet
        # chose: a list of titles is exactly the shape a prompt injection hides
        # in, and it lands in the model's context verbatim.
        return tool_results.wrap_untrusted(
            tool_results.as_payload({"windows": titles, "open": len(windows)})
        )

    _context_latch(context)

    query = " ".join(str(window or "").split())
    if not query:
        raise ToolError("Which window? Tell me part of its title, or use action 'list'.")

    windows = await asyncio.to_thread(list_windows)
    matches = match_window(query, windows)
    if not matches:
        raise ToolError(f"I can't find a window matching '{clip(query, 60)}'.")

    # The gate runs before the verb, and the summary names what was actually
    # matched -- so a close names the window of two similarly named ones, and
    # a bulk close states its count, before anything happens rather than
    # afterwards.
    if normalized == "close_all":
        # Every match, never the first: a bulk verb that closed one window and
        # reported a count would be worse than one that refused outright.
        await require_approval(
            context,
            tool="manage_window",
            action="close_all",
            action_class=classify_action("close_all"),
            summary=_bulk_summary("close_all", matches),
            target=_name_list(matches),
        )
        return await asyncio.to_thread(_close_many, matches)

    chosen = matches[0]
    await require_approval(
        context,
        tool="manage_window",
        action=normalized,
        action_class=classify_action(normalized),
        summary=_summary(normalized, chosen),
        target=chosen.title,
    )

    return await asyncio.to_thread(_apply, normalized, chosen)
