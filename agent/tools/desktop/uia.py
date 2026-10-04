"""The Windows UI Automation harness: one thread, one apartment, one watchdog.

This was the first and only copy of itself inside
``messaging.whatsapp_desktop``, and a generic desktop tool needs every part of
it -- the worker thread, the per-thread COM apartment, the deadline, the
cooldown, the keystrokes, the focus discipline. Extracting it here is the
difference between one driver and two.

Four rules, each here because of a way this could go wrong:

1. **The event loop never touches COM.** UI Automation is synchronous and its
   apartment belongs to the thread that first uses it, so one worker thread
   owns every walk (see :func:`run`), with a watchdog: a call that outruns its
   deadline puts the harness into a cooldown instead of hanging the session.
2. **A call that overruns cools the harness down**, it does not merely fail.
   The worker is single, so a wedged application would block every later call
   behind it. Cooling down makes later calls refuse *fast*, which is what a
   fallback path wants.
3. **Keystrokes only go where they are meant to.** Typing demands the
   foreground *and* the target control's own keyboard focus, and refuses --
   typing nothing -- when either cannot be had. Those two together are the
   answer to "where would these keystrokes go?".
4. **What is typed is exactly what was asked for.** ``SendKeys`` truncates
   astral characters into the 16-bit scan field, so text goes through
   :func:`keystrokes`, which emits UTF-16 surrogate pairs for anything outside
   the BMP. A message with an emoji in it is a real thing people send;
   silently changing it is not acceptable.

The walk limits are deliberately generous but bounded: real trees are deep and
Chromium exposes each row in several branches, and an unbounded walk of a live
application is a hang waiting to happen.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import importlib.util
import sys
import threading
import time
from typing import Any, Callable

try:
    from ..tools_compat import ToolError
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_compat import ToolError  # type: ignore[no-redef]

if sys.platform == "win32":
    from ctypes import wintypes as _wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
else:
    _wt = None
    user32 = None
    kernel32 = None

SW_SHOW = 5
SW_SHOWNOACTIVATE = 4

#: Walk limits. Real trees are deep (a chat list sits ~24 levels down) and
#: Chromium exposes each row in several branches, so the budget is generous
#: but finite.
MAX_DEPTH = 32
WALK_BUDGET = 14000

#: Child windows enumerated per top-level window. Chromium hands out a lot of
#: them and a runaway enumeration on one application must not be able to stall
#: the worker thread, so the list is capped rather than assumed small.
MAX_CHILD_WINDOWS = 64

#: One UIA call that exceeds this puts the harness into a cooldown.
CALL_TIMEOUT_SECONDS = 25.0
COOLDOWN_SECONDS = 60.0

#: A tray or minimized window exposes only its title bar -- the renderer is
#: suspended, so the content tree does not exist in it at all. Shown without
#: activation, because *reading* must not yank the user's focus.
REVEAL_WAIT_SECONDS = 1.0

#: Delay between injected key events. Without it a long string arrives faster
#: than most applications drain their input queue and characters are dropped.
KEY_INTERVAL_SECONDS = 0.002

#: The one message every caller gets when the harness is wedged. Deliberately
#: about the harness rather than about any particular tool: the same wording
#: serves a WhatsApp send and a desktop click, and a caller that wants a
#: specific sentence raises its own on top of this one.
MSG_WEDGED = (
    "Windows UI Automation stopped answering, so I'm leaving it alone for a "
    "minute rather than guessing. Nothing was done."
)

MSG_CANT_FOCUS = (
    "I couldn't bring that window to the front, so I typed nothing -- the text "
    "would have gone to whichever window had focus instead. Nothing was typed."
)


def supported() -> bool:
    """Whether this machine has the platform and the driver for any of this."""
    return sys.platform == "win32" and importlib.util.find_spec("uiautomation") is not None


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

_AUTO: Any = None
_initialized: set[int] = set()

_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="lumine-uia")
"""One worker, ever.

Two threads would mean two COM apartments fighting over the same desktop, and
a walk that started on one and read state from the other could interleave with
a user click. The cost is that a wedged application blocks the queue -- which
is what the cooldown is for.
"""

_COOL_UNTIL = 0.0


def driver() -> Any:
    """The ``uiautomation`` module, with this thread's apartment initialized.

    Lazily imported, and the apartment is per thread: COM belongs to the
    thread that first touches it, so a driver handed to the event loop would
    fail in ways that only appear under concurrency. The failure is swallowed
    because it means "already initialized, or no COM on this thread" -- both of
    which the caller discovers by the walk returning nothing.
    """
    global _AUTO
    if _AUTO is None:
        import uiautomation  # noqa: PLC0415 -- deliberately lazy

        _AUTO = uiautomation
    thread_id = threading.get_ident()
    if thread_id not in _initialized:
        _initialized.add(thread_id)
        try:
            _AUTO.InitializeUIAutomationInCurrentThread()
        except Exception:  # noqa: BLE001
            pass
    return _AUTO


def cooled_down() -> bool:
    """Whether the harness is refusing calls after a timeout."""
    return time.monotonic() < _COOL_UNTIL


def cool_down() -> None:
    """Put the harness into cooldown after a call overran its deadline."""
    global _COOL_UNTIL
    _COOL_UNTIL = time.monotonic() + COOLDOWN_SECONDS


def _consume(future: "concurrent.futures.Future") -> None:
    """Take the exception of a call whose awaiter already gave up on it."""
    try:
        future.exception()
    except Exception:  # noqa: BLE001 -- including cancellation
        pass


async def run(function: Callable, *args: Any) -> Any:
    """Run one UIA operation on the worker thread, under a deadline.

    A call that overruns does not merely fail: the single worker may be stuck
    behind it, so the harness cools down and every later call refuses fast
    rather than queueing behind a wedged application.
    """
    if cooled_down():
        raise ToolError(MSG_WEDGED)
    future = _POOL.submit(function, *args)
    future.add_done_callback(_consume)
    wrapped = asyncio.wrap_future(future)
    try:
        return await asyncio.wait_for(wrapped, CALL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        cool_down()
        raise ToolError(MSG_WEDGED) from None


# ---------------------------------------------------------------------------
# Window plumbing (ctypes only -- no COM, safe from any thread)
# ---------------------------------------------------------------------------


def pids_named(prefix: str) -> set[int]:
    """PIDs whose executable starts with ``prefix`` (Toolhelp snapshot)."""
    if sys.platform != "win32":
        return set()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (0, ctypes.c_void_p(-1).value):
        return set()
    found: set[int] = set()
    try:
        entry = _process_entry()
        if kernel32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                name = entry.szExeFile.decode("utf-8", "replace").lower()
                if name.startswith(prefix):
                    found.add(int(entry.th32ProcessID))
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def _process_entry():
    """A populated PROCESSENTRY32 sized for this process's struct layout."""

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_char * 260),
        ]

    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(ProcessEntry32)
    return entry


def windows_of_pids(pids: set[int], class_name: str = "") -> list[int]:
    """Top-level window handles belonging to ``pids``, optionally by class.

    Enumerated rather than looked up by title on purpose: the title is empty
    while an app sits in the tray, and several WinUI applications share a
    class. Returns every match in enumeration order, so the first of two
    identically-named windows is always the same one.
    """
    if sys.platform != "win32" or not pids:
        return []
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def consider(hwnd, _lparam):
        pid = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids or not user32.IsWindow(hwnd):
            return 1
        if class_name:
            buffer = ctypes.create_string_buffer(64)
            user32.GetClassNameA(hwnd, buffer, 64)
            if buffer.value.decode("utf-8", "replace") != class_name:
                return 1
        found.append(int(hwnd))
        return 1

    user32.EnumWindows(consider, 0)
    return found


def descendant_windows(hwnd: int) -> list[int]:
    """Every child window under ``hwnd``, depth-first, ``hwnd`` first.

    This is the difference between the tool working in a modern application and
    not working in one at all. Electron, WinUI and every Chromium shell put
    their actual content in a *child* window -- ``Chrome_RenderWidgetHostHWND``
    for Electron -- and the accessibility tree hanging off the top-level handle
    is the title bar and nothing else. Walking only the parent finds three
    buttons in the corner of VS Code and reports a window full of controls as
    nearly empty.

    Bounded and duplicate-free, because Chromium hands out a lot of children
    and a runaway enumeration on one application must not be able to stall the
    worker thread.
    """
    if sys.platform != "win32" or not hwnd:  # pragma: no cover - non-Windows
        return [hwnd] if hwnd else []
    found: list[int] = [int(hwnd)]
    seen: set[int] = {int(hwnd)}

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def consider(child, _lparam):
        handle = int(child)
        if handle not in seen and len(found) < MAX_CHILD_WINDOWS:
            seen.add(handle)
            found.append(handle)
        return 1

    try:
        user32.EnumChildWindows(hwnd, consider, 0)
    except Exception:  # noqa: BLE001 -- the parent alone is still worth reading
        pass
    return found


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """``(left, top, right, bottom)`` in virtual-screen pixels."""
    if _wt is None:  # pragma: no cover - non-Windows
        return (0, 0, 0, 0)
    rect = _wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def foreground_window() -> int:
    """The handle of the foreground window, or ``0``."""
    if sys.platform != "win32":  # pragma: no cover - non-Windows
        return 0
    return int(user32.GetForegroundWindow() or 0)


def foreground_pid() -> int | None:
    foreground = foreground_window()
    if not foreground:
        return None
    pid = ctypes.c_uint32()
    user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
    return int(pid.value) or None


def pid_of(hwnd: int) -> int:
    pid = ctypes.c_uint32()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_title(hwnd: int) -> str:
    """A window's title, or ``""`` -- never ``None``, never guessed at."""
    if sys.platform != "win32" or not hwnd:  # pragma: no cover - non-Windows
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def is_visible(hwnd: int) -> bool:
    """Whether the window is on screen and not minimized."""
    if sys.platform != "win32" or not hwnd:  # pragma: no cover - non-Windows
        return False
    return bool(user32.IsWindowVisible(hwnd)) and not bool(user32.IsIconic(hwnd))


#: Executable names that are the Lumine desktop shell. The worker is a *child*
#: of that shell, so a pid comparison against ``os.getpid()`` never matches the
#: app's own window -- the two are different processes, and the window belongs
#: to the parent.
APP_PROCESS_NAMES = ("lumine", "lumine-ui")


def _own_process_ids() -> frozenset[int]:
    """Every pid that is part of *this* application: us and the shell above.

    Computed once. Two reasons this has to include the parent rather than only
    ourselves:

    * the desktop app is the Tauri shell and the worker is its Python child, so
      the app's window is never ``os.getpid()`` -- an "is this our own window"
      test that only compared our own pid reported the app's window as an
      ordinary one, and the agent then read and aimed at its own interface;
    * an agent that can click the Tools page can switch its own tool off
      mid-turn, and then keep using it.

    Walking the parent chain is bounded and stops at the first process that is
    not part of the app, so a worker started from a terminal does not adopt the
    terminal's pid -- that would make every console window untouchable.
    """
    global _OWN_PIDS
    if _OWN_PIDS is not None:
        return _OWN_PIDS
    import os

    found = {os.getpid()}
    try:
        import psutil  # noqa: PLC0415 -- optional

        current = psutil.Process(os.getpid())
        for _ in range(3):
            parent = current.parent()
            if parent is None:
                break
            name = str(parent.name() or "").lower()
            stem = name[:-4] if name.endswith(".exe") else name
            if stem not in APP_PROCESS_NAMES:
                break  # not our shell -- a terminal, an IDE, a test runner
            found.add(int(parent.pid))
            current = parent
    except Exception:  # noqa: BLE001 -- psutil absent: our own pid is still ours
        pass
    _OWN_PIDS = frozenset(found)
    return _OWN_PIDS


_OWN_PIDS: "frozenset[int] | None" = None


def reset_own_processes() -> None:
    """Forget the cached app process ids. Tests only."""
    global _OWN_PIDS
    _OWN_PIDS = None


def is_our_own(hwnd: int) -> bool:
    """Whether this window belongs to Lumine itself -- the worker or its shell.

    The same guard ``tools/windows_control.py`` applies to closing, and for
    the same reason, with one addition: it has to cover the *desktop shell*,
    because that is where the window actually is. The worker is a child
    process, so comparing against its own pid alone never matched.
    """
    if not hwnd or sys.platform != "win32":  # pragma: no cover - non-Windows
        return False
    try:
        return pid_of(hwnd) in _own_process_ids()
    except Exception:  # noqa: BLE001
        return False


def reveal(hwnd: int) -> None:
    """Make the window readable without activating it.

    A tray or minimized window exposes only its title bar: the renderer is
    suspended, so the tree does not exist in it at all, and walking it would
    report "nothing here" about an application that has plenty. Shown *without*
    activation, because reading must not steal focus.
    """
    if sys.platform != "win32":  # pragma: no cover - non-Windows
        return
    if not is_visible(hwnd):
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        time.sleep(REVEAL_WAIT_SECONDS)


# ---------------------------------------------------------------------------
# Foreground discipline
# ---------------------------------------------------------------------------

#: Whose window we took the foreground from, so it can be handed back. Set
#: only when a steal actually happened; cleared when returned.
_STOLEN_FROM: int | None = None


def bring_to_front(hwnd: int) -> bool:
    """Make ``hwnd`` the foreground window, working around the focus lock.

    **Windows refuses to hand the foreground to a background process** while the
    user is interacting with another one, and it enforces that against the
    calling thread -- so a `SetForegroundWindow` from a worker simply does
    nothing, silently, on exactly the occasions a user would be watching.

    The documented way past it is ``AttachThreadInput``: lend the foreground
    window's input queue to ours for the duration of the call, so the call is
    treated as coming *from* the process that already owns the foreground. This
    is not a hack around a safety mechanism -- it is how a launcher or an IME
    does it -- and it is what `windows_control._focus` already uses, which is
    why it is here rather than written a third time.

    Returns whether the window ended up in front. The caller decides what a
    refusal means: refusing to *type* is right, because a keystroke aimed at the
    wrong window is unrecoverable; ignoring it for a *click* is right, because a
    click carries its own position.
    """
    if sys.platform != "win32" or not hwnd:  # pragma: no cover - non-Windows
        return False
    try:
        if is_visible(hwnd) is False and user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        foreground = int(user32.GetForegroundWindow() or 0)
        if foreground == int(hwnd):
            return True
        current_thread = user32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = False
        if foreground_thread and foreground_thread != current_thread:
            attached = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
        try:
            user32.SetForegroundWindow(int(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)
    except Exception:  # noqa: BLE001 -- focus rules are advisory
        return False


def ensure_foreground(hwnd: int, message: str = "") -> None:
    """Make certain keystrokes will land in *this* app -- or refuse.

    The check is by process, not by handle: Windows may report a different
    top-level of the same application as foreground, and a handle equality test
    would refuse a situation that is safe. What matters is that the target
    *process* owns the foreground, and -- for anything typed -- that the
    control itself reports keyboard focus afterwards. Together those two are
    the answer to "where would these keystrokes go?".

    Raises rather than returning a bool, and types nothing on failure: a
    string typed into the wrong window is the single worst outcome this module
    could produce, and it is not recoverable by the model afterwards.
    """
    global _STOLEN_FROM
    if sys.platform != "win32":  # pragma: no cover - non-Windows
        raise ToolError(message or MSG_CANT_FOCUS)
    if _foreground_matches(hwnd):
        user32.ShowWindow(hwnd, SW_SHOW)
        return
    previous = foreground_window()
    # `bring_to_front`, not a bare `SetForegroundWindow`: the bare call is
    # silently refused by the focus lock on exactly the turns a user would be
    # watching, which is how "she can't type" happened from a terminal.
    bring_to_front(int(hwnd))
    time.sleep(0.2)
    if not _foreground_matches(hwnd):
        raise ToolError(message or MSG_CANT_FOCUS)
    if previous and _STOLEN_FROM is None and int(previous) != int(hwnd):
        _STOLEN_FROM = int(previous)


def _foreground_matches(hwnd: int) -> bool:
    return foreground_pid() == pid_of(hwnd)


def release_foreground() -> None:
    """Hand focus back to whatever we took it from (best effort).

    Best effort on purpose: focus rules are advisory, the previous window may
    have been closed while we worked, and failing to restore it is not worth
    an error the model would have to explain to the user.
    """
    global _STOLEN_FROM
    previous, _STOLEN_FROM = _STOLEN_FROM, None
    if previous and sys.platform == "win32" and user32.IsWindow(previous):
        try:
            user32.SetForegroundWindow(previous)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Keystrokes
# ---------------------------------------------------------------------------


def keystrokes(text: str) -> list[Any]:
    """One keyboard INPUT event per UTF-16 code unit, nothing else.

    BMP characters are emitted directly; anything outside it (emoji, rare CJK,
    math symbols) becomes a surrogate pair, which is the shape
    ``KEYEVENTF_UNICODE`` needs -- passing the full code point truncates into
    the 16-bit scan field and types a *different* character.

    Deliberately ignorant of input method state: this is raw Unicode
    injection, so what the application receives is what was asked for,
    character for character. A composed IME sequence would be a different
    contract and would need different evidence.
    """
    auto = driver()
    events = []
    for char in text:
        code = ord(char)
        if code > 0xFFFF:
            rest = code - 0x10000
            units = (0xD800 + (rest >> 10), 0xDC00 + (rest & 0x3FF))
        else:
            units = (code,)
        for unit in units:
            events.append(auto.KeyboardInput(0, unit, auto.KeyboardEventFlag.KeyUnicode, 0))
    return events


def send_text(text: str) -> None:
    """Type Unicode text with real key events (see :func:`keystrokes`)."""
    auto = driver()
    for event in keystrokes(text):
        auto.SendInput(event)
        time.sleep(KEY_INTERVAL_SECONDS)


def focus_is_on(control: Any) -> bool:
    """Whether *this* control reports keyboard focus.

    Read back from the control rather than assumed from the window's, because
    a window being foreground says nothing about which of its controls will
    receive a keystroke. This is the check that makes "where would these
    keystrokes go?" answerable.
    """
    try:
        return bool(control.HasKeyboardFocus)
    except Exception:  # noqa: BLE001 -- an unreadable control is not focused
        return False


def focus_control(control: Any) -> None:
    """Give a control keyboard focus, and wait for the application to settle."""
    control.SetFocus()
    time.sleep(0.15)


# ---------------------------------------------------------------------------
# Tree walking
# ---------------------------------------------------------------------------


def texts_of(control: Any, left: list[int]) -> list[str]:
    """Non-empty text nodes under this control, in document order.

    Document order is not cosmetic: a row's *first* text is its title, and a
    walk visiting children in reverse would hand back the timestamp or an
    unread badge instead. Siblings are pushed reversed so the pop yields them
    in the order the application wrote them, and a text is recorded when it is
    visited rather than when it is queued.
    """
    found: list[str] = []
    stack: list[tuple[Any, int]] = [(control, 0)]
    while stack and left[0] > 0:
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        if node.ControlTypeName == "TextControl":
            value = " ".join(str(node.Name or "").split())
            if value:
                found.append(value)
        try:
            children = node.GetChildren()
        except Exception:  # noqa: BLE001 -- a dead node is not our error
            continue
        for child in reversed(list(children)):
            left[0] -= 1
            if left[0] <= 0:
                break
            stack.append((child, depth + 1))
    return found


def rect_of(control: Any) -> tuple | None:
    """``(left, top, right, bottom)`` for a control, or ``None`` if it has none.

    ``None`` rather than a zero rectangle: a control with no position is a
    control that cannot be pointed at, and a zero-size rect at the origin would
    resolve to the top-left corner of the screen -- which is a real click on
    something the user never named.
    """
    try:
        rect = control.BoundingRectangle
        if rect is None or rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:  # noqa: BLE001
        return None


def walk(node: Any, visit: Callable[[Any, int], None], *, depth: int = 0) -> int:
    """Depth- and budget-bounded pre-order walk, calling ``visit`` per node.

    Iterative rather than recursive on purpose. A live application's tree is
    an untrusted shape -- some application somewhere has a cycle in it -- and
    recursion turns that into a ``RecursionError`` inside the worker thread,
    which is exactly the kind of hang the watchdog exists to prevent. Returns
    the number of nodes visited, so a caller that stopped early can say so.
    """
    budget = [WALK_BUDGET]
    stack: list[tuple[Any, int]] = [(node, depth)]
    seen = 0
    while stack and budget[0] > 0:
        current, level = stack.pop()
        if level > MAX_DEPTH:
            continue
        seen += 1
        try:
            visit(current, level)
        except Exception:  # noqa: BLE001 -- one bad node must not end the walk
            pass
        try:
            children = current.GetChildren()
        except Exception:  # noqa: BLE001
            continue
        for child in reversed(list(children)):
            budget[0] -= 1
            if budget[0] <= 0:
                break
            stack.append((child, level + 1))
    return seen
