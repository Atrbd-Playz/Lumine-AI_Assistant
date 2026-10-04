"""What is on screen right now -- and the generation token that says *when*.

## Why the token is the whole point

An element index is a claim about a specific picture. "Click 7" means "click
the control that was seventh when I looked", and that equivalence stops being
true the moment anything moves: a list scrolls, a spinner finishes, a dialog
opens over the top, a progress bar advances. The click still lands. It just
lands on whatever is *now* seventh, which on a scrolled list is one row below
where the model meant, and rows on a scrolled list include Send, Delete and
Pay.

So every ``observe`` mints a **generation**, and every act names the generation
it was planned against. A mismatch is a refusal in :func:`check_generation`,
not a warning in the prompt -- a model that is told to be careful is a model
that is sometimes not careful, and the failure it produces is an irreversible
click on a control the user never named. This is the same rule
``browser/state.py`` applies to page snapshots and ``messaging/whatsapp_refs``
applies to chat freshness, and it is the reason this module exists rather than
a dozen lines inside ``control.py``.

## What is read, and what is deliberately not

Elements come from the accessibility tree, which is the OS's own account of
what is on screen. No pixels are read, no screenshot is taken, and no OCR runs:
a picture would cost tokens on every later turn, and the tree gives better
answers than a picture would -- a role and a name, rather than a rectangle to
be squinted at.

What the tree cannot see -- a canvas, a game, a remote desktop -- is what
:mod:`input`'s coordinate tier is for. This module reports that as a fact
(``addressable: false``) rather than pretending otherwise, so the tool can say
"nothing here can be clicked by name" instead of returning an empty list that
reads as "the screen is empty".

## Everything read here is somebody's text

A control's accessible name is text an application chose, and in a browser it
is text a *web page* chose. It therefore travels wrapped and untrusted, and the
model is told so in the same payload rather than in a system prompt it may
have summarized away.
"""

from __future__ import annotations

import itertools
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

try:
    from ..tools_compat import ToolError
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_compat import ToolError  # type: ignore[no-redef]

try:
    from .. import windows_control
except ImportError:  # running as a top-level script with agent/tools on sys.path
    import windows_control  # type: ignore[no-redef]

from . import input as desktop_input
from . import uia
from . import ops

#: How many elements one observation may list.
#:
#: Bounded because this payload is re-sent on every later turn for the rest of
#: the session -- it is a tool result, so it is paid for repeatedly. Twenty is
#: enough to describe a dialog or a form and short enough that a window with
#: two hundred controls does not blow the budget to describe the first twelve.
MAX_ELEMENTS = 20

#: An element smaller than this in either axis is not clickable in any useful
#: sense -- it is a divider, a caret, or an artefact of the tree. Clicking one
#: reliably does nothing, so listing it teaches the model to aim at furniture.
MIN_ELEMENT_SIDE = 6

#: Upper bound on the walk. Generous relative to :data:`uia.WALK_BUDGET`
#: because this collects rather than searches for one node, and a real
#: Chromium tree exposes each element several times over.
MAX_NODES = 3000

#: Control types worth listing, and how to say them to the model.
#:
#: The list is short on purpose. Listing every `PaneControl` and `GroupControl`
#: would fill the budget with containers that cannot be pressed, and the
#: question "what can I click" is answered by what can be pressed.
INTERACTIVE_TYPES: dict[str, str] = {
    "ButtonControl": "button",
    "HyperlinkControl": "link",
    "EditControl": "field",
    "DocumentControl": "field",
    "ComboBoxControl": "dropdown",
    "ListItemControl": "item",
    "TabItemControl": "tab",
    "CheckBoxControl": "checkbox",
    "RadioButtonControl": "radio",
    "MenuItemControl": "menu item",
    "TreeItemControl": "item",
    "SliderControl": "slider",
    "SpinnerControl": "spinner",
    "ThumbControl": "handle",
    "DataItemControl": "item",
}

#: Text a control may show. Long names are clipped rather than dropped: a
#: truncated label still identifies the control, and dropping it would leave
#: the model unable to tell two buttons apart.
NAME_LIMIT = 60

#: How long an observation stays usable without a fresh look.
#:
#: Not a cache TTL and not a correctness rule -- :func:`check_generation` is
#: that. This bounds how long the tool will *reuse* an index for a plain read
#: ("what is on screen"), where a slightly stale answer is better than paying
#: for a fresh walk on every question. Anything that acts requires an exact
#: generation match, and this number does not relax that.
STALE_AFTER_SECONDS = 20.0


@dataclass
class Element:
    """One addressable control, as the model is told about it."""

    #: The index the model refers to. Stable within one observation, and the
    #: only identifier an act may use: a name is ambiguous and a coordinate is
    #: exactly the thing this tool exists to avoid.
    ref: int
    #: The role, in words a model can reason about ("button", not "ButtonControl").
    role: str
    #: The accessible name, clipped. May be empty -- plenty of controls are
    #: unlabelled, and an empty string is honest where a guess is not.
    name: str
    #: ``(left, top, right, bottom)`` in virtual-desktop pixels.
    rect: tuple[int, int, int, int]
    #: Whether it can be pressed at all.
    enabled: bool = True
    #: Whether it is a password field. The single most important flag here,
    #: and the reason ``type`` can be cheap: refusing to type into one is a
    #: structural refusal that needs no approval question.
    is_password: bool = False
    #: The live UIA control, held for the act that follows. Not serialized.
    control: Any = field(default=None, repr=False, compare=False)

    @property
    def center(self) -> tuple[int, int]:
        """Where to aim: the middle of the rectangle, rounded inward.

        The middle rather than an edge because an edge is shared with whatever
        is next to it -- clicking the left edge of a button lands on the
        border, which some applications treat as a resize and some ignore.
        """
        left, top, right, bottom = self.rect
        return ((left + right) // 2, (top + bottom) // 2)

    def describe(self) -> str:
        """One line, for a text summary the model reads before the image."""
        bits = [f"{self.ref}. {self.role}"]
        if self.name:
            bits.append(f'"{self.name}"')
        if not self.enabled:
            bits.append("(disabled)")
        if self.is_password:
            bits.append("(password field - I won't type here)")
        return " ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "role": self.role,
            "name": self.name,
            "rect": list(self.rect),
            "enabled": self.enabled,
            "isPassword": self.is_password,
        }


@dataclass
class Observation:
    """One look at one window, with the token that makes it checkable."""

    #: Monotonic, process-wide. Not a timestamp: two observations in the same
    #: millisecond must not compare equal, and only a counter guarantees that.
    generation: int
    #: The window handle acted on.
    hwnd: int
    #: Its title, for the model's own confirmation that it is looking at the
    #: right thing.
    title: str
    #: Its executable, lowercased and without `.exe`.
    process: str
    #: The elements, in the order they were found. Reading order is preserved
    #: deliberately: it is the order a person would describe the window in,
    #: and re-sorting by geometry puts a dialog's title bar after its buttons.
    elements: list[Element] = field(default_factory=list)
    #: Whether anything was addressable at all. False for a canvas or a game,
    #: and the difference between "this screen is empty" and "this screen does
    #: not say what it is" -- which the model needs in order to fall back to a
    #: coordinate rather than reporting a bug.
    addressable: bool = True
    #: Whether the process reads coordinates in the same space it reports them
    #: in. False means every click lands at the scale factor's distance from
    #: where it was aimed, and it is reported rather than raised because on a
    #: system old enough to lack the API it is the best available answer.
    dpi_aligned: bool = True
    #: When the walk finished.
    observed_at: float = field(default_factory=time.time)
    #: The whole desktop as ``(left, top, right, bottom)``, spanning every
    #: monitor. Published with the elements because every coordinate in the
    #: payload lives in this space, and a monitor left of the primary one
    #: starts at a *negative* x -- so without these bounds a model reading
    #: ``(240, 300)`` cannot tell a second monitor from the first, and would
    #: guess a coordinate that lands off the edge entirely.
    desktop: tuple[int, int, int, int] = (0, 0, 0, 0)
    #: Whether the observed window was the foreground one. False means it was
    #: found by name elsewhere on the desktop, which is why acting on it brings
    #: it forward rather than assuming it already has the keyboard.
    foreground: bool = True

    def by_ref(self, ref: object) -> Element | None:
        """The element with this index, or ``None``."""
        try:
            wanted = int(ref)
        except (TypeError, ValueError):
            return None
        for element in self.elements:
            if element.ref == wanted:
                return element
        return None

    def describe(self) -> list[str]:
        """One line per element, for the model's text view of the screen."""
        return [element.describe() for element in self.elements]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "window": self.title,
            "process": self.process,
            "addressable": self.addressable,
            "dpiAligned": self.dpi_aligned,
            "desktop": list(self.desktop),
            "foreground": self.foreground,
            "elements": [element.to_dict() for element in self.elements],
        }


_GENERATIONS = itertools.count(1)


def next_generation() -> int:
    """The next generation number. A counter, not a clock.

    Two observations inside one millisecond must not compare equal, and only a
    monotonic counter guarantees that -- a timestamp would make two different
    screens look like the same one, which is precisely the confusion the token
    exists to prevent.
    """
    return next(_GENERATIONS)

#: The most recent observation. Module-level rather than per-session because
#: there is one desktop per machine and a stale handle from a previous session
#: is worse than no handle at all -- it would let a new conversation act on an
#: old screen. The generation check below is what makes that safe: an
#: observation from a previous session has a number this session never minted.
_LAST: Observation | None = None


def last_observation() -> Observation | None:
    """The most recent observation on this machine, if any."""
    return _LAST


#: The refusal for a screen that has moved under a reference. One sentence for
#: two quite different causes -- the control is gone, or it is somewhere else --
#: because the remedy is the same for the model and splitting them would let it
#: retry the same number against a different screen.
MSG_SCREEN_MOVED = (
    "The screen has moved since I looked at it, so that would point at "
    "something else now. Nothing was done -- observe again and I will do it "
    "to the new one."
)


def require_observation() -> Observation:
    """The current observation, or a refusal saying to look first."""
    current = _LAST
    if current is None:
        raise ToolError(
            "I haven't looked at the screen yet. Nothing was done -- call "
            "observe first, then act on what it shows."
        )
    return current


def check_generation(claimed: object) -> Observation:
    """The current observation, if ``claimed`` still names it.

    **Meaningful across steps, vacuous within one call.** The orchestrator
    passes the generation its ``observe`` step recorded, so a plan that ran a
    read, a wait and a click can be refused for acting on a screen older than
    its own first step. A single tool call has no such gap to detect: the tool
    reads the generation itself, so this would be ``x == x`` and always pass.

    That is why the movement check is :func:`confirm_target` instead — a second
    reading of the tree, which is the only thing that can actually tell that the
    screen moved. This function keeps the plan-level check and answers "have you
    looked at all", and must not be relied on for freshness on its own.
    """
    current = require_observation()
    wanted = _as_generation(claimed)
    if wanted is None:
        raise ToolError(
            "I need the number from the screen I looked at, so I know the "
            "thing you mean hasn't moved. Nothing was done."
        )
    if wanted != current.generation:
        raise ToolError(MSG_SCREEN_MOVED)
    return current


def confirm_target(observation: Observation, element: Element) -> None:
    """Refuse if the screen has moved since ``element`` was read.

    **This is the check that makes a reference mean anything**, and it is a
    re-read rather than a timestamp. A generation token alone cannot do it: the
    tool supplies the generation itself, so comparing it against the current one
    is always `x == x` and always passes. The token identifies *which* look of
    the screen a number came from -- which is what makes the tool result and the
    UI panel agree -- but detecting movement needs an actual second reading.

    So before anything irreversible, the tree is walked again and the target is
    looked up *by what it is* rather than by its index:

    * **gone** -- the control is no longer there. Refuse. A dialog closed, a
      list scrolled, an application navigated.
    * **moved** -- the same name is somewhere else on screen. Refuse. This is
      the case the whole design exists for: a scrolled list puts index 7 on a
      *different* control, and the rows include Send, Delete and Pay.
    * **present and in the same place** -- proceed.

    Matched by role and name, deliberately, and never by index: an index is what
    moved. Cost is one bounded tree walk, which is the same cost as the
    observation itself and the reason this can be synchronous in the worker
    thread rather than something scheduled.
    """
    if element.is_password or not element.name:
        # Unnamed controls cannot be re-found, so there is nothing to compare
        # against. Not refused for it: a nameless button is still a button, and
        # refusing would make a large class of real interface unusable. The
        # coordinate carries the click; the risk is the ordinary one.
        return
    try:
        now = _collect(observation.hwnd)
    except Exception:  # noqa: BLE001 -- an unreadable screen is not proof of movement
        raise ToolError(MSG_SCREEN_MOVED) from None
    if not now:
        # The tree came back empty, which is what a mid-refresh or a crashed
        # application looks like. Refusing is the safe reading: we cannot show
        # the control is still there, and the alternative is clicking on the
        # strength of a picture that may be minutes old.
        raise ToolError(MSG_SCREEN_MOVED)
    same_name = [
        candidate
        for candidate in now
        if candidate.role == element.role and candidate.name == element.name
    ]
    if not same_name:
        raise ToolError(
            f"{MSG_SCREEN_MOVED} The {element.role} called "
            f"'{element.name[:30]}' isn't there any more."
        )
    if not any(candidate.rect == element.rect for candidate in same_name):
        raise ToolError(
            f"{MSG_SCREEN_MOVED} The {element.role} called "
            f"'{element.name[:30]}' has moved since I looked."
        )


def is_stale(observation: Observation | None = None, *, seconds: float = STALE_AFTER_SECONDS) -> bool:
    """Whether an observation is too old to reuse for a plain read."""
    current = observation if observation is not None else _LAST
    if current is None:
        return True
    return (time.time() - current.observed_at) > seconds


def _as_generation(claimed: object) -> int | None:
    """A generation number, or ``None`` for anything that is not one.

    An ``int`` or a run of digits, and nothing else. A ``float`` is refused
    rather than truncated: ``int(1.5)`` is ``1``, which would silently match the
    observation that number was derived from and so defeat the very check it
    was passed through. A float only ever arrives from a model that miscounted,
    which is precisely the case this function exists to catch.
    """
    if isinstance(claimed, bool) or claimed is None:
        return None
    if isinstance(claimed, int):
        return claimed
    if isinstance(claimed, str):
        text = claimed.strip()
        if text.isdigit():
            return int(text)
    return None


# ---------------------------------------------------------------------------
# Reading a window
# ---------------------------------------------------------------------------


def foreground_process(hwnd: int) -> str:
    """The window's executable, lowercased and without ``.exe``.

    Empty when it cannot be read, which callers must treat as "unknown" rather
    than as "not integrated" -- an unreadable process name is exactly the case
    where refusing to guess matters most.
    """
    if sys.platform != "win32" or not hwnd:  # pragma: no cover - non-Windows
        return ""
    try:
        import psutil  # noqa: PLC0415 -- optional, with a ctypes fallback

        return str(psutil.Process(uia.pid_of(hwnd)).name() or "").lower()
    except Exception:  # noqa: BLE001 -- psutil absent or the process exited
        return ""


def find_window(query: str, *, limit: int = 5) -> list[tuple[int, str]]:
    """``(handle, title)`` for every open window whose title answers ``query``.

    Loose matching, the same three tiers ``windows_control.match_window`` uses,
    because that is the house style for "chrome" meaning a browser window. This
    is what makes a second monitor usable: with foreground-only observation, a
    window on the other display was unreachable no matter what the user said.
    """
    wanted = " ".join(str(query or "").split())
    if not wanted:
        return []
    try:
        matches = windows_control.match_window(wanted, windows_control.list_windows())
    except Exception:  # noqa: BLE001 -- "no windows" is a legitimate answer
        return []
    return [(item.handle, item.title) for item in matches[: max(0, limit)]]


def resolve_window(query: object) -> tuple[int, str]:
    """The one window ``query`` names, or a refusal explaining the ambiguity.

    One window or none, never the first of several: two windows called
    "Notepad" are two documents, and picking one to type into is the kind of
    guess this tool refuses everywhere else.
    """
    text = str(query or "").strip()
    matches = find_window(text)
    if not matches:
        raise ToolError(f"I can't find a window matching '{text}'. Nothing was done.")
    if len(matches) > 1:
        listed = ", ".join(title for _handle, title in matches[:4])
        raise ToolError(
            f"more than one window matches '{text}' ({listed}). Nothing was "
            f"done -- tell me which one."
        )
    return matches[0]


def candidate_windows() -> list[tuple[int, str]]:
    """Every window a user could mean by "the screen", best candidate first.

    **Foreground is not the answer.** It was, and it is wrong in three ways that
    a person hits immediately:

    * While you are *talking* to Lumine the foreground window is **Lumine**.
      Reading it returns her own chrome -- "Close tab", "New Tab" -- which are
      not what anybody meant.
    * An always-on-top overlay takes the foreground without the user doing
      anything. A Rainmeter clock widget is a few dozen pixels of transparent
      window that exposes **zero** controls, and it made `observe` report "a
      blank page" while Notepad sat right behind it.
    * The desktop itself ("Program Manager") is never what "click Save" means.

    So the list is filtered for *usefulness* rather than for z-order, and the
    foreground only wins if it is actually a candidate.
    """
    foreground = uia.foreground_window()
    usable: list[tuple[int, str]] = []
    for window in windows_control.list_windows():
        handle, title = int(window.handle), str(window.title or "")
        if not title.strip():
            continue
        # Our own window is never the target: see the docstring.
        if uia.is_our_own(handle):
            continue
        # A shell surface -- "Program Manager" -- is the desktop, not a document.
        if title.strip().casefold() in _SHELL_TITLES:
            continue
        usable.append((handle, title))

    foreground_title = uia.window_title(foreground) if foreground else ""
    # The foreground first *when it is a real window*, so "the window I am
    # looking at" still wins whenever that is a sane reading.
    ordered = sorted(
        usable,
        key=lambda row: (
            0 if foreground and row[0] == int(foreground) else 1,
            0 if foreground_title and row[1] == foreground_title else 1,
        ),
    )
    return ordered


#: Window titles that are a shell rather than a document. "Program Manager" is
#: the desktop; clicking a desktop icon is possible but is never what "click the
#: save button" means.
_SHELL_TITLES = frozenset({"program manager", "desktop", "search", "run", "task manager"})


def best_window() -> tuple[int, str]:
    """The window a request with no target most likely means, or a refusal.

    Tries the candidates in order and takes the first that exposes at least one
    control. That fallback is what a floating clock widget or a desktop surface
    cannot defeat: they expose nothing, so they are skipped, and Notepad behind
    them is what gets read.

    Costs one walk per skipped candidate and at most one per candidate. Bounded
    by :data:`CANDIDATE_TRIES`, because the alternative -- reading whichever
    window happened to be focused -- is the bug this exists to fix.
    """
    foreground = uia.foreground_window()
    for index, (handle, title) in enumerate(candidate_windows()[:CANDIDATE_TRIES]):
        try:
            elements = _collect(handle)
        except Exception:  # noqa: BLE001 -- an unreadable window is not a candidate
            continue
        if elements:
            return handle, title
        if index == 0 and handle == int(foreground or 0):
            continue
    raise ToolError(
        "I can't see a window with anything in it to click. Nothing was done "
        "-- tell me which application to work in, and I will look at that one."
    )


#: How many windows `best_window` will try before giving up. Small on purpose:
#: each attempt is an accessibility-tree walk, and the fix for "I found nothing"
#: is to name a window rather than to scan the whole desktop.
CANDIDATE_TRIES = 6


def observe(
    hwnd: int | None = None,
    *,
    include_text: bool = True,
    window: str = "",
) -> Observation:
    """Read a window and mint a generation.

    The most useful window by default -- see :func:`best_window`, because the
    foreground window is Lumine herself while you are talking to her -- or the
    one ``window`` names, which is how a window on a second monitor is reached.

    Walks the accessibility tree on the worker thread via :mod:`uia`, collects
    the interactive controls, and returns them with a fresh token. Bounds are
    inherited rather than restated: :data:`uia.MAX_DEPTH` and
    :data:`uia.WALK_BUDGET` are the harness's, and a second set of walk limits
    in this module is a second thing to keep in step.
    """
    foreground = uia.foreground_window()
    if window:
        target, _title = resolve_window(window)
    elif hwnd:
        target = int(hwnd)
    else:
        target, _title = best_window()
    if not target:
        raise ToolError("I can't see a window with anything in it to click. Nothing was done.")

    # Before anything is measured, not after: window rectangles read in
    # virtualized coordinates and clicks sent in physical ones are off by
    # exactly the scale factor, silently.
    aligned = desktop_input.set_dpi_awareness()

    uia.reveal(target)
    title = uia.window_title(target)
    observation = Observation(
        generation=next_generation(),
        hwnd=target,
        title=title,
        process=foreground_process(target),
        elements=[],
        addressable=False,
        dpi_aligned=bool(aligned) or desktop_input.dpi_aware(),
        desktop=desktop_input.screen_rect(),
        foreground=bool(foreground) and int(target) == int(foreground),
    )
    if include_text:
        observation.elements = _collect(target)
    observation.addressable = bool(observation.elements)

    global _LAST
    _LAST = observation
    return observation


def _collect(hwnd: int) -> list[Element]:
    """The interactive controls under a window, in reading order.

    Runs inside the worker thread: every call here touches COM, and the event
    loop must not. Returns at most :data:`MAX_ELEMENTS`, but *walks* far more
    than that -- the cap is applied to what is kept, not to where the walk goes,
    because a control past a naive node limit is exactly the one in a deep
    dialog that the model needs.

    Every child window is walked, not just the top-level handle. Electron and
    WinUI applications put their real content in a child window and leave the
    parent's tree holding nothing but the title bar, so a parent-only walk
    reports a full window as nearly empty -- three corner buttons in VS Code,
    which is the case this was found on.
    """
    auto = uia.driver()
    found: list[Element] = []
    seen: set[tuple] = set()
    budget = [MAX_NODES]

    def visit(node: Any, _depth: int) -> None:
        if len(found) >= MAX_ELEMENTS or budget[0] <= 0:
            return
        budget[0] -= 1
        kind = str(getattr(node, "ControlTypeName", "") or "")
        role = INTERACTIVE_TYPES.get(kind)
        if role is None:
            return
        rect = uia.rect_of(node)
        if rect is None:
            return
        if rect[2] - rect[0] < MIN_ELEMENT_SIDE or rect[3] - rect[1] < MIN_ELEMENT_SIDE:
            return
        # Chromium exposes one control in several flattened branches; the
        # rectangle is what they disagree about least, so it is the key.
        if rect in seen:
            return
        name = " ".join(str(getattr(node, "Name", "") or "").split())
        if len(name) > NAME_LIMIT:
            name = name[: NAME_LIMIT - 1] + "…"
        seen.add(rect)
        found.append(
            Element(
                ref=len(found) + 1,
                role=role,
                name=name,
                rect=rect,
                enabled=_is_enabled(node),
                is_password=_is_password(node),
                control=node,
            )
        )

    for child in uia.descendant_windows(hwnd):
        try:
            root = auto.ControlFromHandle(child)
        except Exception:  # noqa: BLE001 -- one dead child is not the whole window
            continue
        uia.walk(root, visit)
        if len(found) >= MAX_ELEMENTS or budget[0] <= 0:
            break
    return found


def _is_enabled(control: Any) -> bool:
    """Whether the control accepts input.

    Read through ``IsEnabled`` with the control's own property, falling back to
    assuming yes. Assuming *no* would be the safe direction and the wrong one:
    a control whose provider does not implement the property reports nothing,
    and refusing to click every such control would make this tool useless
    against exactly the modern applications that most need it. The gate is
    where uncertainty belongs -- an unknown control still asks when its name
    says it is consequential.
    """
    try:
        return bool(control.IsEnabled)
    except Exception:  # noqa: BLE001
        return True


def _is_password(control: Any) -> bool:
    """Whether this is a password field, and typing here is refused.

    Checked through the value pattern rather than the name, because the name is
    the string "Password" on a well-behaved form and something like "Enter your
    login" on the rest, while ``IsPassword`` is the accessibility contract for
    "what goes in here must not be shown". Belt and braces: a name that says
    password counts even when the pattern does not, since a false positive
    refuses a field rather than writing a credential into a field.
    """
    try:
        if bool(control.IsPassword):
            return True
    except Exception:  # noqa: BLE001
        pass
    role = str(getattr(control, "ControlTypeName", "") or "")
    if role not in ("EditControl", "DocumentControl"):
        return False
    name = " ".join(str(getattr(control, "Name", "") or "").lower().split())
    return "password" in name or "passcode" in name or "pin" in name


def find_by_name(observation: Observation, query: str) -> list[Element]:
    """Elements whose name answers ``query``, best first.

    The same three tiers as ``windows_control.match_window`` -- exact, prefix,
    containment -- because a person says "send" rather than the whole label,
    and the same shape is already the house style for matching a spoken name
    against a list. Ties keep walk order, so the first of two identically-named
    buttons is always the same one.
    """
    wanted = " ".join(str(query or "").split()).casefold()
    if not wanted:
        return []
    hits: list[tuple[int, int, Element]] = []
    for position, element in enumerate(observation.elements):
        name = " ".join(element.name.split()).casefold()
        if name == wanted:
            tier = 0
        elif name.startswith(wanted):
            tier = 1
        elif wanted in name:
            tier = 2
        else:
            continue
        hits.append((tier, position, element))
    hits.sort(key=lambda row: (row[0], row[1]))
    return [element for _tier, _position, element in hits]


def process_names_for(hwnd: int) -> list[str]:
    """The executable owning a window, for the integration check.

    Only the *owning* process, and that is the whole correctness of the check
    rather than a simplification. A window belongs to exactly one process, and
    that process is what the user is looking at; its children are renderers and
    helpers, which never own a top-level window. Collecting them looks more
    thorough and is actively wrong -- ``msedgewebview2.exe`` is a child of
    every Tauri and WinUI application, so including children reports Lumine's
    own window as a browser and refuses to drive the whole desktop shell.

    Returns a list because the answer is genuinely unknown when the name cannot
    be read, and a caller that must not guess gets ``[]`` rather than a guess.
    """
    name = foreground_process(hwnd)
    return [name] if name else []


def describe_environment() -> dict[str, Any]:
    """What this machine can do, for the probe and for a refused call."""
    return {
        "windows": sys.platform == "win32",
        "driver": uia.supported(),
        "dpiAware": desktop_input.dpi_aware(),
        "desktop": list(desktop_input.screen_rect()),
        "foreground": uia.window_title(uia.foreground_window()),
        "integratedTools": list(ops.integrated_tool_names()),
        "verbs": list(ops.VERBS),
        "ownPid": os.getpid(),
    }
