"""``SendInput``: moving the pointer and pressing keys, with no opinion about
what is on screen.

This module knows coordinates and keys. It does not know what a button is,
does not read an accessibility tree, and does not need ``uiautomation`` to be
installed -- which is what makes it able to drive a canvas, a game, or a
remote desktop where nothing is addressable at all. ``capture`` and ``uia``
decide *what* to point at; this decides only how.

Three decisions worth stating, because each is the one a naive implementation
gets wrong:

**Absolute coordinates are normalized, not pixels.** ``MOUSEEVENTF_ABSOLUTE``
takes a 0-65535 fraction of the virtual desktop, not a pixel. Passing a pixel
value lands the pointer somewhere plausible and wrong, which is the worst kind
of wrong: it looks like it nearly worked.

**DPI awareness is set before anything is measured, or not at all.** At 150%
scaling a window rect and a click coordinate disagree by exactly the scale
factor, so every click lands half a control away from where the model meant
it. ``set_dpi_awareness`` is idempotent, is called by ``input``'s first use,
and is deliberately *not* undoable -- changing it mid-session would move every
coordinate already in flight.

**Injected events are still events.** Windows delivers them through the same
path as a physical input, so a focused text field receives them, an IME may
compose them, and a drag can be interrupted by the user grabbing the mouse.
Nothing here claims the target cooperated. That is what ``verification`` is
for.
"""

from __future__ import annotations

import ctypes
import sys
import time
from typing import Any

try:
    from ..tools_compat import ToolError
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_compat import ToolError  # type: ignore[no-redef]

from . import uia

if sys.platform == "win32":
    user32 = ctypes.windll.user32
else:  # pragma: no cover - non-Windows
    user32 = None


# -- Win32 constants, spelled out ---------------------------------------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_KEYUP = 0x0002

#: One wheel notch. Windows reports scroll in multiples of this, and a model
#: that says "scroll down" means *some* amount -- asking it to name 120 would
#: be asking it to know a Win32 constant.
WHEEL_DELTA = 120

#: Below this the pointer is already at the edge and further events are no-ops
#: that still wake the application. Clamped rather than refused: "scroll to the
#: bottom" is a legitimate request that arrives as a large delta.
MAX_WHEEL_NOTCHES = 40

#: Between two presses of the same key in a double-click. Windows' own default
#: is 500ms; comfortably under it, and comfortably over what a user can do by
#: accident.
DOUBLE_CLICK_MS = 120

#: Drag needs intermediate moves or the target sees a teleport, not a drag.
DRAG_STEPS = 12
DRAG_STEP_SECONDS = 0.012

#: How long to wait for the application to have processed a click before its
#: outcome is read. Not a "did it work" wait -- the reader does that. Long
#: enough that a fast UI has repainted, short enough not to be the reason a
#: voice turn feels slow.
SETTLE_SECONDS = 0.25

MSG_UNSUPPORTED = (
    "Controlling the mouse and keyboard needs Windows, and this isn't one. "
    "Nothing was done."
)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]


def _require_windows() -> None:
    if sys.platform != "win32":  # pragma: no cover - non-Windows
        raise ToolError(MSG_UNSUPPORTED)


# -- DPI ----------------------------------------------------------------------


_DPI_SET = False

#: PER_MONITOR_AWARE_V2. The value is the documented magic constant rather
#: than a looked-up enum, because the enum is not exported by name on every
#: supported Windows build and a failed lookup here would mean *staying* at
#: system DPI awareness -- the exact silent-misalignment this exists to avoid.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def set_dpi_awareness() -> bool:
    """Opt this process into per-monitor DPI awareness, once.

    Returns whether it is aware. Never raises: the API is absent before Windows
    10 1703, and on an older build the fallback is the *system* DPI setting,
    which is consistent across the session rather than wrong per-monitor. The
    caller cannot fix that, so it is reported rather than raised.
    """
    global _DPI_SET
    if sys.platform != "win32":  # pragma: no cover - non-Windows
        return False
    if _DPI_SET:
        return True
    try:
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
                _DPI_SET = True
                return True
        # Windows 8.1 and earlier. Set once and ignored if it fails.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            _DPI_SET = True
            return True
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False


def dpi_aware() -> bool:
    """Whether coordinates this process reads and coordinates it sets agree.

    Checked rather than assumed, because the failure is silent: a process that
    is not DPI aware reads window rectangles in virtualized coordinates and
    sends physical ones, and every click lands at the scale factor's distance
    from where it was aimed. Nothing errors. That is why ``capture`` reports
    this in its own result.
    """
    if sys.platform != "win32":  # pragma: no cover - non-Windows
        return False
    try:
        ctx = user32.GetThreadDpiAwarenessContext()
        if ctx is None:
            return False
        # DPI_AWARENESS_AWARE == 2, DPI_AWARENESS_PER_MONITOR_AWARE == 3
        return int(ctx) in (2, 3, -4)
    except Exception:  # noqa: BLE001
        return _DPI_SET


# -- Virtual desktop ----------------------------------------------------------


SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def virtual_desktop() -> tuple[int, int, int, int]:
    """``(left, top, width, height)`` of every monitor as one rectangle.

    The virtual desktop rather than the primary monitor, so a click works on a
    second display and is not silently clamped to the first. Negative origins
    are normal -- a monitor to the left of the primary one starts at -1920 --
    and the normalization below accounts for them rather than dropping the sign.
    """
    _require_windows()
    return (
        int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        max(1, int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))),
        max(1, int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))),
    )


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor_position() -> tuple[int, int] | None:
    """Where the pointer is, in virtual-desktop pixels, or ``None``.

    ``None`` rather than a zero point when the query fails: the caller decides
    what an unknown pointer position means, and it is not this module's call to
    make ``(0, 0)`` sound like an answer.
    """
    if sys.platform != "win32":  # pragma: no cover - non-Windows
        return None
    try:
        cursor = _POINT()
        if not user32.GetCursorPos(ctypes.byref(cursor)):
            return None
        return (int(cursor.x), int(cursor.y))
    except Exception:  # noqa: BLE001
        return None


def screen_rect() -> tuple[int, int, int, int]:
    """The virtual desktop as ``(left, top, right, bottom)``."""
    left, top, width, height = virtual_desktop()
    return (left, top, left + width, top + height)


def contains(point: tuple[int, int]) -> bool:
    """Whether a point is on some monitor.

    The check a raw coordinate must pass before it is used. A click outside
    every screen is not a no-op on Windows -- it clamps to the nearest edge --
    so an unchecked coordinate is a click *somewhere*, which is precisely the
    outcome this tool must never produce by accident.
    """
    left, top, right, bottom = screen_rect()
    x, y = int(point[0]), int(point[1])
    return left <= x < right and top <= y < bottom


# -- Injection ----------------------------------------------------------------


def _send(payload: _INPUT) -> None:
    """One ``SendInput`` call, and a refusal if Windows declined it.

    ``GetLastError`` is checked rather than assumed, because the documented
    failure for ``SendInput`` is UIPI: a *lower* integrity process cannot
    inject into an elevated one. That returns zero with no exception, so an
    unchecked call reports a click that never happened -- the same category of
    lie this codebase refuses everywhere else. The common case is a genuine
    block and the actionable advice is to not run Lumine as administrator.
    """
    sent = user32.SendInput(1, ctypes.byref(payload), ctypes.sizeof(_INPUT))
    if sent != 1:
        raise ToolError(
            "Windows refused that input. It usually means the window needs "
            "administrator rights -- try the same thing again with a normal "
            "window open. Nothing was done."
        )


def _mouse_event(flags: int, *, dx: int = 0, dy: int = 0, data: int = 0) -> None:
    payload = _INPUT(type=INPUT_MOUSE)
    payload.union.mi = _MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags)
    _send(payload)


def _keyboard_event(vk: int, flags: int) -> None:
    payload = _INPUT(type=INPUT_KEYBOARD)
    payload.union.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags)
    _send(payload)


def _normalize(point: tuple[int, int]) -> tuple[int, int]:
    """Virtual-desktop pixels to the 0-65535 fractions ``ABSOLUTE`` wants."""
    left, top, width, height = virtual_desktop()
    x = (int(point[0]) - left) * 65535 // max(1, width - 1)
    y = (int(point[1]) - top) * 65535 // max(1, height - 1)
    # Clamped, not wrapped: an out-of-range fraction is interpreted modulo
    # 65536 by the OS, which would turn "just off the left edge" into
    # "the middle of the screen".
    return (max(0, min(65535, x)), max(0, min(65535, y)))


def move_to(point: tuple[int, int], *, settle: float = SETTLE_SECONDS) -> None:
    """Put the pointer at ``(x, y)`` in virtual-desktop pixels.

    Absolute rather than relative: a model can name a position, and cannot
    name a displacement from wherever the pointer happened to be. One
    ``MOVE`` is also what makes a following click land where the observation
    said, rather than a frame behind a sequence of deltas.
    """
    _require_windows()
    set_dpi_awareness()
    if not contains(point):
        raise ToolError("That position isn't on the screen. The pointer didn't move.")
    nx, ny = _normalize(point)
    _mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, dx=nx, dy=ny)
    if settle:
        time.sleep(settle)


def _button_flags(button: str) -> tuple[int, int]:
    """``(down, up)`` flags for a button name, fail-closed on anything else."""
    name = str(button or "left").strip().lower()
    if name in ("left", "primary"):
        return MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    if name in ("right", "secondary"):
        return MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    if name == "middle":
        return MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
    raise ToolError("I can only click the left, right, or middle button.")


def click(
    point: tuple[int, int],
    *,
    button: str = "left",
    count: int = 1,
    settle: float = SETTLE_SECONDS,
) -> None:
    """Press and release a button at ``point``.

    ``count`` is 1 or 2. Two is not two calls in a row: it is press, release,
    press, release inside the double-click interval, because two separate
    clicks with a network round trip between them are two clicks to most
    applications and open a menu instead of activating the target.
    """
    _require_windows()
    set_dpi_awareness()
    if not contains(point):
        raise ToolError("That position isn't on the screen. Nothing was clicked.")
    times = 2 if int(count) >= 2 else 1
    down, up = _button_flags(button)
    for index in range(times):
        move_to(point, settle=0 if index == 0 and times > 1 else 0.02)
        _mouse_event(down)
        time.sleep(0.02)
        _mouse_event(up)
        if index + 1 < times:
            time.sleep(DOUBLE_CLICK_MS / 1000.0)
    if settle:
        time.sleep(settle)


def drag(
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    button: str = "left",
    settle: float = SETTLE_SECONDS,
) -> None:
    """Press at ``start``, move to ``end``, release there.

    Interpolated, because a single jump is a teleport: a slider, a window
    edge, and a text selection all need to see the pointer *travel*, and
    Windows itself will cancel a drag that jumps.
    """
    _require_windows()
    set_dpi_awareness()
    if not contains(start) or not contains(end):
        raise ToolError("That drag doesn't stay on the screen. Nothing was moved.")
    down, up = _button_flags(button)
    move_to(start, settle=0.02)
    _mouse_event(down)
    time.sleep(0.03)
    for step in range(1, DRAG_STEPS + 1):
        ratio = step / DRAG_STEPS
        x = int(start[0] + (end[0] - start[0]) * ratio)
        y = int(start[1] + (end[1] - start[1]) * ratio)
        _absolute_move(x, y)
        time.sleep(DRAG_STEP_SECONDS)
    _mouse_event(up)
    if settle:
        time.sleep(settle)


def _absolute_move(x: int, y: int) -> None:
    nx, ny = _normalize((x, y))
    _mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, dx=nx, dy=ny)


def scroll(
    *,
    amount: int,
    horizontal: bool = False,
    settle: float = SETTLE_SECONDS,
) -> None:
    """Turn the wheel. ``amount`` is in notches, positive for away-from-you.

    A wheel event carries no position, so this acts on whatever is under the
    pointer -- which is why the tool moves the pointer first when the caller
    named a target. That coupling is the caller's to make, and ``control``
    makes it, because the alternative is a scroll aimed at a scrollbar nobody
    moved the pointer to.
    """
    _require_windows()
    set_dpi_awareness()
    notches = max(-MAX_WHEEL_NOTCHES, min(MAX_WHEEL_NOTCHES, int(amount or 0)))
    if notches == 0:
        return
    data = notches * WHEEL_DELTA
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _mouse_event(flag, data=data)
    if settle:
        time.sleep(settle)


# -- Keys ---------------------------------------------------------------------

#: Virtual-key codes for the keys that are the same in every layout.
#:
#: Deliberately a closed list. A model that can name an arbitrary scan code can
#: type into the secure attention sequence, and "everything Win32 can do" is a
#: much larger surface than "the keys a person means by name". Unknown names
#: are refused by :func:`press_key` rather than passed through.
VIRTUAL_KEYS: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "control": 0x11,
    "ctrl": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "printscreen": 0x2C,
    "numlock": 0x90,
    "scrolllock": 0x91,
}
for _index in range(1, 13):
    VIRTUAL_KEYS[f"f{_index}"] = 0x6F + _index

#: Modifiers, as ``input`` accepts them. ``ctrl`` and ``win`` are here because
#: a chord is the ordinary case -- "press Ctrl+S" is a request to save.
MODIFIERS: dict[str, int] = {
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "windows": 0x5B,
    "super": 0x5B,
    "meta": 0x5B,
    "cmd": 0x5B,
}

#: Keys deliberately absent, and why. A closed list that explains its own
#: refusals is one a model can route around by asking for something else;
#: these are the ones that would do something no user asked for.
BLOCKED_KEYS: dict[str, str] = {
    "win+l": "that locks the machine",
    "ctrl+alt+delete": "that is Windows' secure attention sequence, and only Windows can send it",
    "win+d": "that shows the desktop",
}

MSG_BAD_KEY = (
    "I can only press named keys and simple combinations like Ctrl+S or "
    "Alt+Tab. Nothing was pressed."
)


def _normalize_key_name(name: str) -> str:
    return "".join(ch for ch in str(name or "").strip().lower() if ch.isalnum() or ch == "+")


def parse_chord(spec: str) -> list[str]:
    """``"ctrl+shift+s"`` -> ``["ctrl", "shift", "s"]``, or ``[]``.

    Split on ``+`` while keeping single characters intact, so ``ctrl+c`` is two
    parts and not three with an empty middle.
    """
    raw = str(spec or "").strip().lower()
    if not raw:
        return []
    parts: list[str] = []
    for piece in raw.split("+"):
        token = _normalize_key_name(piece)
        if token:
            parts.append(token)
    return parts


def resolve_chord(spec: str) -> tuple[list[int], int]:
    """``(modifier_vks, key_vk)`` for a chord, raising on anything unknown.

    A single key is a chord with no modifiers, so this covers ``press_key`` and
    ``hotkey`` as one function rather than two that could disagree about which
    names exist.
    """
    parts = parse_chord(spec)
    if not parts:
        raise ToolError(MSG_BAD_KEY)
    blocked = BLOCKED_KEYS.get("+".join(parts))
    if blocked:
        raise ToolError(f"I won't press that combination because {blocked}. Nothing was pressed.")
    modifiers = [MODIFIERS[name] for name in parts[:-1] if name in MODIFIERS]
    last = parts[-1]
    if last in MODIFIERS:
        # A chord of only modifiers ("press shift") is meaningless as an action
        # and usually a truncated one ("ctrl+shift").
        raise ToolError(MSG_BAD_KEY)
    if last in VIRTUAL_KEYS:
        return modifiers, VIRTUAL_KEYS[last]
    if len(last) == 1 and (last.isalpha() or last.isdigit()):
        # Letters and digits: the virtual-key code for those is a fixed offset
        # from the character, which is why this works on any layout for the
        # *unshifted* key. Shift is a separate modifier press, so "shift+4" is
        # shift held and the 4 key -- "$" on a US layout, "4" on a French one.
        code = ord(last.upper())
        if last.isalpha() or (0x30 <= code <= 0x39):
            return modifiers, code
    raise ToolError(MSG_BAD_KEY)


def press_key(spec: str, *, settle: float = SETTLE_SECONDS) -> None:
    """Press and release one key or chord.

    Modifiers go down in order and up in reverse, which is what Windows
    expects: a chord released out of order can leave a modifier latched down
    in the target application, and the user's next keystroke then does
    something neither of them asked for.
    """
    _require_windows()
    modifiers, key = resolve_chord(spec)
    for modifier in modifiers:
        _keyboard_event(modifier, 0)
    time.sleep(0.01)
    _keyboard_event(key, 0)
    time.sleep(0.03)
    _keyboard_event(key, KEYEVENTF_KEYUP)
    time.sleep(0.01)
    for modifier in reversed(modifiers):
        _keyboard_event(modifier, KEYEVENTF_KEYUP)
    if settle:
        time.sleep(settle)


def type_text(text: str, *, settle: float = SETTLE_SECONDS) -> int:
    """Type Unicode text through the shared keystroke path.

    Returns the number of events sent, which is *not* the character count: an
    emoji is two events. A caller that wants to bound its work wants the
    event count, because that is what the application has to process.
    """
    _require_windows()
    if not text:
        return 0
    events = uia.keystrokes(text)
    auto = uia.driver()
    for event in events:
        auto.SendInput(event)
        time.sleep(uia.KEY_INTERVAL_SECONDS)
    if settle:
        time.sleep(settle)
    return len(events)
