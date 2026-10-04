"""WhatsApp Desktop through Windows UI Automation -- live state, never a picture.

The desktop app is the primary automation path; WhatsApp Web (in
`messaging.whatsapp`) is what sits underneath it for machines where this
one is not usable. Everything here obeys four rules, and each one exists
because of a way this could go wrong:

1. **Every answer is read at the moment it is given.** There is no
   screenshot anywhere in this path, no cached tree, and no result reused
   across calls. A capability checked a minute ago is exactly the stale
   claim this product must not make, so `desktop_capability` re-finds the
   window every time, and a delivery check re-walks the tree after the
   send rather than trusting anything observed before it. The freshness
   guard itself -- an observation older than the send attempt cannot
   confirm that send -- lives in `messaging.whatsapp`.
2. **The event loop never touches COM.** UI Automation is synchronous and
   its apartment belongs to the thread that first uses it, so one worker
   thread owns every walk (see `_run`), with a watchdog: a call that
   outruns its deadline puts the backend into a cooldown instead of
   hanging the session.
3. **Keystrokes only go where they are meant to.** Reads reveal a hidden
   window without activating it (a tray window exposes no content tree,
   but reading must not steal focus). Typing is different: it demands the
   foreground *and* the target control's own keyboard focus, and refuses
   -- typing nothing -- when either cannot be had.
4. **What is typed is exactly what was asked for.** The library's own
   `SendKeys` truncates astral characters (`ord(emoji)` into 16 bits), so
   message text goes through `_keystrokes`, which emits UTF-16 surrogate
   pairs for anything outside the BMP. A message with an emoji in it is a
   real thing people send; silently changing it is not acceptable.

The structural facts this was written against (verified against the real
app, not assumed): the main window's class is ``WinUIDesktopWin32WindowClass``;
chat rows are ``DataItemControl``s whose child text nodes are *title first,
timestamp second*; the search box is an ``EditControl`` in the top of the
window; a row only counts as a chat when it carries at least two texts,
which is what separates a conversation from navigation furniture.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import importlib.util
import re
import sys
import threading
import time
from typing import Any, Callable

try:
    from ..tools_compat import ToolError
    from ..desktop import uia
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_compat import ToolError
    from desktop import uia

if sys.platform == "win32":
    from ctypes import wintypes as _wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
else:
    _wt = None
    user32 = None
    kernel32 = None

#: The window we drive. This class is WinUI 3's, and WhatsApp Desktop is a
#: WinUI shell around a WebView2 -- so the *app* window is ours, and the
#: chat rows live inside the WebView's accessibility branch of its tree.
MAIN_WINDOW_CLASS = "WinUIDesktopWin32WindowClass"
PROCESS_PREFIX = "whatsapp"

SW_SHOWNOACTIVATE = 4
SW_HIDE = 0

#: Walk limits: the tree is deep (the chat list sits ~24 levels down) and
#: Chromium exposes each row in several branches, so the budget is generous
#: but bounded -- an unbounded walk of a live app is a hang waiting to
#: happen, and `_run`'s watchdog exists for the rest.
MAX_DEPTH = 32
WALK_BUDGET = 14000
MAX_TITLES = 12

#: One UIA call that exceeds this puts the backend into a cooldown: the
#: worker thread is single, so a stuck walk would block every later call
#: behind it. Cool down instead of queueing behind a wedged app.
CALL_TIMEOUT_SECONDS = 25.0
COOLDOWN_SECONDS = 60.0

#: A tray window has no content tree (the renderer is suspended), so reads
#: reveal it first; v9 of the probes confirmed the tree appears within
#: about a second *without* activating the window.
REVEAL_WAIT_SECONDS = 1.0
STATE_POLLS = 3
STATE_POLL_SECONDS = 0.5

#: Composition geometry, as fractions of the window rect. WhatsApp renders
#: your own messages flush right in the conversation pane; the composer is
#: a strip at its foot; the chat list is the left column. These are layout
#: facts about one app, used only to *exclude* (a match inside the
#: composer's subtree never confirms a send) -- never to claim something
#: the tree did not say.
SIDEBAR_FRACTION = 0.28
COMPOSER_TOP_FRACTION = 0.6
COMPOSER_MAX_HEIGHT_FRACTION = 0.3
MIN_CONTROL_WIDTH = 40

SEARCH_POLL_SECONDS = 0.35
SEARCH_POLLS = 8
OPEN_SETTLE_SECONDS = 0.4

# Model-facing refusals. Each says what did *not* happen, so the model
# cannot rephrase its way past one.
MSG_NOT_WINDOWS = (
    "WhatsApp Desktop can only be controlled from Windows, and this isn't one. "
    "Nothing was sent."
)
MSG_NO_DRIVER = (
    "The UI Automation driver WhatsApp Desktop needs isn't installed (the "
    "`uiautomation` package). Nothing was sent."
)
MSG_NOT_RUNNING = "WhatsApp Desktop isn't running right now. Nothing was sent."
MSG_BLIND = (
    "WhatsApp Desktop is open but I can't read its chat list -- it may still "
    "be starting up, or not signed in. Nothing was sent."
)
MSG_NO_SEARCH = "I can't find WhatsApp's search box right now. Nothing was typed."
MSG_CANT_TYPE = (
    "I couldn't give WhatsApp the keyboard, so I typed nothing -- the message "
    "would have gone to whichever window had focus instead. Nothing was sent."
)
MSG_NO_COMPOSER = "I can't see the message box on this chat. Nothing was typed."
MSG_DIRTY = (
    "I started typing and couldn't clear the message box afterwards. Open "
    "WhatsApp and clear that box before asking me again -- nothing was sent."
)
MSG_WEDGED = (
    "WhatsApp Desktop stopped answering, so I'm leaving it alone for a minute "
    "rather than guessing. Nothing was sent."
)


class DirtyText(Exception):
    """Internal: typing failed part-way and the composer could not be cleared.

    Raised from the worker thread, translated in `messaging.whatsapp` into
    the same hold as an unconfirmed send: nothing *was* sent, but a retry
    would type on top of the leftovers, so the user must look first.
    """


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


#: The text walk is `desktop.uia`'s, for the same reason the worker is: it is
#: a bounded pre-order walk over an accessibility tree, not a WhatsApp detail.
#: The docstring it carried lives with the implementation now.
_texts_of = uia.texts_of


def _row_title(control: Any, left: list[int]) -> str | None:
    """The chat title this row carries, or ``None`` if it is not a chat.

    A conversation row shows *title first, timestamp second*; navigation
    furniture (an "Archived" label, a section header) carries at most one
    text. Requiring two is what keeps a heading out of the contact list --
    and the rule reads structure, not language, so it does not care what
    the timestamp says.
    """
    texts = _texts_of(control, left)
    if len(texts) < 2:
        return None
    return texts[0]


def _rows_in(
    node: Any, out: list, seen: set, left: list[int], depth: int = 0, wanted: str = ""
) -> None:
    """Collect ``(title, control)`` chat rows whose title matches `wanted`.

    Chromium exposes each row in several flattened branches; the bounding
    rectangle is what they disagree about least, so duplicates are keyed on
    it. The limit counts *matching* rows, not visited ones -- capping the
    walk first and filtering after would hide a chat sitting below the cap
    and report "no such contact" about a person who is right there.
    Collection stops once `out` is full, which is what keeps a state check
    from paying for the whole sidebar.
    """
    if depth > MAX_DEPTH or left[0] <= 0:
        return
    try:
        children = node.GetChildren()
    except Exception:  # noqa: BLE001
        return
    for child in children:
        if left[0] <= 0 or len(out) >= MAX_TITLES:
            return
        left[0] -= 1
        if child.ControlTypeName == "DataItemControl":
            title = _row_title(child, left)
            if title is not None and (not wanted or wanted in title.casefold()):
                key = _rect_key(child)
                if key is not None and key not in seen:
                    seen.add(key)
                    out.append((title, child))
        _rows_in(child, out, seen, left, depth + 1, wanted)


def _rect_key(control: Any) -> tuple | None:
    return uia.rect_of(control)


# ---------------------------------------------------------------------------
# Window plumbing (ctypes only -- no COM, safe from any thread)
# ---------------------------------------------------------------------------
#
# Also `desktop.uia`'s now. These are Win32 calls with nothing WhatsApp-shaped
# about them, and the desktop tool needs every one of them.


def _main_window() -> int | None:
    """The WhatsApp Desktop main window handle, or ``None``.

    Found by process *and* class rather than by title: the title is empty
    while the app sits in the tray, and other WinUI apps share the class.
    """
    handles = uia.windows_of_pids(uia.pids_named(PROCESS_PREFIX), MAIN_WINDOW_CLASS)
    return handles[0] if handles else None


#: The plumbing aliases. `_reveal` and `_ensure_foreground` carry their
#: reasoning in `uia`; this module's version of the second is the only one that
#: needed a WhatsApp-specific refusal sentence, so it stays here.
_pids_named = uia.pids_named
_process_entry = uia._process_entry
_window_rect = uia.window_rect
_reveal = uia.reveal
_foreground_pid = uia.foreground_pid
_pid_of = uia.pid_of


def _ensure_foreground(hwnd: int) -> None:
    """Make certain keystrokes will land in *this* app -- or refuse.

    `uia.ensure_foreground` does the work; the only thing this adds is
    WhatsApp's own refusal sentence, because "I typed nothing" is a sentence
    about a message the user was about to send and has its own consequences.
    """
    uia.ensure_foreground(hwnd, MSG_CANT_TYPE)


def _release_foreground() -> None:
    """Hand focus back to whatever we took it from (best effort)."""
    uia.release_foreground()


# ---------------------------------------------------------------------------
# Control finding (geometry over language: the UI may be any locale)
# ---------------------------------------------------------------------------


def _find_control(
    hwnd: int,
    kinds: tuple[str, ...],
    *,
    bottom_only: bool = False,
    top_only: bool = False,
) -> Any:
    """First control of one of `kinds` in the region asked for.

    The search box and the composer are both plain text controls, told
    apart by *where they are*: WhatsApp puts search at the top of the
    window and the composer in a strip at the foot of the conversation
    pane. A name-based test would break on a translated UI; the layout
    does not translate.
    """
    control = _driver().ControlFromHandle(hwnd)
    left_, top_, right_, bottom_ = _window_rect(hwnd)
    width = right_ - left_
    height = bottom_ - top_
    best: Any = None
    best_area: float | None = None
    state = {"left": WALK_BUDGET}

    def walk(node: Any, depth: int) -> None:
        nonlocal best, best_area
        if depth > MAX_DEPTH or state["left"] <= 0:
            return
        try:
            children = node.GetChildren()
        except Exception:  # noqa: BLE001
            return
        for child in children:
            if state["left"] <= 0:
                return
            state["left"] -= 1
            if child.ControlTypeName in kinds:
                try:
                    rect = child.BoundingRectangle
                except Exception:  # noqa: BLE001
                    continue
                ctrl_w = rect.right - rect.left
                ctrl_h = rect.bottom - rect.top
                rel_left = (rect.left - left_) / max(width, 1)
                rel_top = (rect.top - top_) / max(height, 1)
                if ctrl_w < MIN_CONTROL_WIDTH or ctrl_h < 8:
                    continue
                if bottom_only and rel_top < COMPOSER_TOP_FRACTION:
                    continue
                if bottom_only and ctrl_h / max(height, 1) > COMPOSER_MAX_HEIGHT_FRACTION:
                    continue  # a full-height pane is not the composer strip
                if top_only and rel_top >= COMPOSER_TOP_FRACTION:
                    continue
                area = ctrl_w * ctrl_h
                # Smallest wins: the composer strip is small, a pane is not.
                if best is None or (best_area is not None and area < best_area):
                    best = child
                    best_area = area
            walk(child, depth + 1)

    walk(control, 0)
    return best


def _find_search(hwnd: int) -> Any:
    """The search box: a text control in the top of the window."""
    return _find_control(hwnd, ("EditControl",), top_only=True)


def _find_composer(hwnd: int) -> Any:
    """The message box: a text control in the foot of the conversation.

    Both an ``EditControl`` (an input) and a ``DocumentControl`` (a
    contenteditable) qualify -- WhatsApp's composer has been both. A chat
    must be open for it to exist at all, which is why a missing composer
    after a successful open is a *refusal* rather than a guess: without it
    there is no proof the chat opened, and nothing may be typed.
    """
    return _find_control(
        hwnd, ("EditControl", "DocumentControl"), bottom_only=True
    )


# ---------------------------------------------------------------------------
# The worker: one thread, one apartment, one watchdog
# ---------------------------------------------------------------------------
#
# These live in `desktop.uia` now, shared with the generic desktop tool. Two
# harnesses would mean two copies of a bug fix, and none of this is WhatsApp's
# to own: a single worker thread, a COM apartment per thread, a deadline, and
# a cooldown for an application that stopped answering are all properties of
# UI Automation rather than of one application built on it.
#
# The aliases below are the names the rest of this module calls. Keeping them
# means the migration changed where the code lives, not what this file says.
#
# Note that the cooldown and the deadline now belong to `uia` as well. A
# WhatsApp call that times out cools down *desktop control too*, which is
# correct: the worker is one thread, so it is genuinely stuck, and reporting
# the desktop path as healthy while it queues behind a wedged WhatsApp would
# be the more misleading answer.

_driver = uia.driver
_run = uia.run
_keystrokes = uia.keystrokes
_send_text = uia.send_text


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


def desktop_capability() -> dict[str, Any]:
    """Can WhatsApp Desktop be driven *right now*? Live, never cached.

    Three facts, cheapest first: the platform, the driver, the window.
    Each has its own refusal so the answer is actionable -- "install this"
    and "start that" are different problems, and neither is helped by a
    single "unavailable".
    """
    if sys.platform != "win32":
        return {"available": False, "reason": MSG_NOT_WINDOWS}
    if importlib.util.find_spec("uiautomation") is None:
        return {"available": False, "reason": MSG_NO_DRIVER}
    if _main_window() is None:
        return {"available": False, "reason": MSG_NOT_RUNNING}
    return {"available": True, "reason": ""}


def state_reason(token: str) -> str:
    """The sentence for a state that is not ``"ready"``."""
    return {
        "not_running": MSG_NOT_RUNNING,
        "blind": MSG_BLIND,
    }.get(token, MSG_BLIND)


# ---------------------------------------------------------------------------
# The operations, on the worker thread
# ---------------------------------------------------------------------------


def _rows(hwnd: int, *, query: str = "", limit: int = MAX_TITLES) -> list:
    """Chat rows (title, control) matching `query`, deduplicated.

    The query is handed to the collector rather than applied afterwards:
    filtering a capped list finds only matches among the first
    `MAX_TITLES` chats, and "not a contact" about somebody further down
    is a false answer to a question the tree could have answered.
    """
    _reveal(hwnd)
    control = _driver().ControlFromHandle(hwnd)
    collected: list = []
    wanted = query.strip().casefold()
    _rows_in(control, collected, set(), [WALK_BUDGET], wanted=wanted)
    return collected[:limit]


def _state_sync() -> str:
    """``"ready"``, ``"not_running"``, or ``"blind"`` -- live, each time."""
    hwnd = _main_window()
    if hwnd is None:
        return "not_running"
    _reveal(hwnd)
    for attempt in range(STATE_POLLS):
        if _rows(hwnd, limit=1):
            return "ready"
        if attempt + 1 < STATE_POLLS:
            time.sleep(STATE_POLL_SECONDS)
    return "blind"


def has_search() -> bool:
    """Whether WhatsApp Desktop's search box is visible right now.

    Separate from :func:`state` on purpose, and asked *before* a search rather
    than after: a path that cannot search must be reported as such immediately,
    because falling through to a search that returns "no contacts" would be a
    false answer about somebody who is in the chat list.
    """
    hwnd = _main_window()
    if hwnd is None:
        return False
    return _find_search(hwnd) is not None


def _row_kind(control: Any, left: list[int]) -> str:
    """``"group"`` or ``"contact"`` for one chat row.

    Read from the row's own texts rather than from its title. A group's row shows
    the participant count alongside the name ("Afia, Rahim +2"), and that second
    text is the structural signal: a contact row carries title, timestamp and a
    preview, while a group's carries a count that is numeric. Guessing from the
    title's *language* would break in every locale but English.
    """
    texts = _texts_of(control, left)
    for value in texts[1:]:
        if re.fullmatch(r"\+\d+", value):
            return "group"
        if value.isdigit() and len(value) >= 3:
            # A four-digit year is a timestamp; a bare 3+ digit number is not.
            if len(value) == 4:
                return "contact"
    for value in texts[1:]:
        if "+" in value and re.search(r"\+\d", value):
            return "group"
    return "contact"


def _participants(control: Any, left: list[int]) -> tuple[str, ...]:
    """Names a group's row advertises, when it advertises any."""
    texts = _texts_of(control, left)
    for value in texts:
        if "," in value or re.search(r"\+\d", value):
            return tuple(part.strip() for part in re.split(r",|\+", value) if part.strip())
    return ()


def _contacts_sync(query: str) -> list[dict[str, Any]]:
    """Chat rows matching ``query``, found through WhatsApp's own search box.

    The three rules, all of them learned the hard way from the desktop adapter:

    1. **Search, then read.** The rendered list is the last few dozen chats;
       typing into search reaches the account's whole history, which is the
       difference between "find Afia" working and asking for a phone number.
    2. **The filter must arrive.** The list is read before typing, and a result
       is believed only once the list no longer looks like that baseline -- a read
       that landed before the filter applied returns whichever chat sorts first,
       which is how "message Afia" becomes "message whoever messaged last".
    3. **The box is cleared on the way out**, in a ``finally``. A filter left
       behind narrows every later lookup for the rest of the session.
    """
    hwnd = _main_window()
    if hwnd is None:
        raise ToolError(MSG_NOT_RUNNING)
    box = _find_search(hwnd)
    if box is None:
        raise ToolError(MSG_NO_SEARCH)
    wanted = " ".join(str(query or "").split()).casefold()
    baseline = {title.casefold() for title, _node in _rows(hwnd)}
    try:
        _type_into(hwnd, box, str(query or ""))
        for _ in range(SEARCH_POLLS):
            time.sleep(SEARCH_POLL_SECONDS)
            rows = _rows(hwnd)
            found: list[dict[str, Any]] = []
            for title, node in rows:
                if wanted and wanted not in title.casefold():
                    continue
                left = [WALK_BUDGET // 4]
                found.append(
                    {
                        "title": title,
                        "kind": _row_kind(node, left),
                        "participants": list(_participants(node, left)),
                    }
                )
                if len(found) >= MAX_TITLES:
                    break
            # A list identical to the pre-search one means the filter has not
            # applied yet; believing it would answer with the whole chat list.
            current = {title.casefold() for title, _node in rows}
            if found and (current != baseline or len(current) < len(baseline)):
                return found
        return []
    finally:
        try:
            if _foreground_pid() == _pid_of(hwnd) and box.HasKeyboardFocus:
                _driver().SendKeys("{Ctrl}a{Back}")
                time.sleep(0.2)
        except Exception:  # noqa: BLE001 -- best effort; nothing depends on it
            pass


async def search_contacts(query: str) -> list[dict[str, Any]]:
    """Chat rows matching ``query``, read live from the search box."""
    return await _run(_contacts_sync, query)


def _titles_sync(query: str) -> list[str]:
    """Chat titles matching `query` (all titles when it is empty)."""
    hwnd = _main_window()
    if hwnd is None:
        raise ToolError(MSG_NOT_RUNNING)
    return [title for title, _node in _rows(hwnd, query=query)]


def _type_into(hwnd: int, control: Any, text: str) -> None:
    """Focus `control` and type `text` -- or type nothing at all.

    The order is the safety property: foreground first, then focus, then
    the control's *own* keyboard-focus report, and only then events. Any
    step that cannot be verified raises before a single character is
    injected, because keystrokes with uncertain destination are keystrokes
    into somebody else's window.
    """
    _ensure_foreground(hwnd)
    control.SetFocus()
    time.sleep(0.15)
    if not control.HasKeyboardFocus:
        raise ToolError(MSG_CANT_TYPE)
    _send_text(text)


def _search_sync(number: str) -> str | None:
    """Type a number into search, read the chat it found, then erase it.

    The list is read once *before* typing, and a result is only believed
    once the list no longer looks like that baseline -- a list identical
    to the pre-search one is a read that landed before the filter did,
    and believing its first row would mean believing whichever chat
    happens to sort first. WhatsApp's own filter is what makes "first
    row" mean "best match", so the filter arriving is the precondition
    for this read saying anything at all. (A chat arriving in the same
    half-second would also differ from the baseline; to be believed it
    must additionally survive the filter, which by the first poll has
    long applied -- keystrokes land as the digits are typed, and the
    first read comes a beat later.)

    The box is cleared on the way out (in a ``finally``): a filter left
    behind would silently narrow every later contact lookup, and a tool
    that only works once is a tool that lies on the second call.
    """
    hwnd = _main_window()
    if hwnd is None:
        raise ToolError(MSG_NOT_RUNNING)
    box = _find_search(hwnd)
    if box is None:
        raise ToolError(MSG_NO_SEARCH)
    baseline = [title for title, _node in _rows(hwnd)]
    try:
        _type_into(hwnd, box, number)
        for _ in range(SEARCH_POLLS):
            time.sleep(SEARCH_POLL_SECONDS)
            rows = [title for title, _node in _rows(hwnd)]
            if rows and (rows[0] not in baseline or len(rows) < len(baseline)):
                return rows[0]
        return None
    finally:
        try:
            if _foreground_pid() == _pid_of(hwnd) and box.HasKeyboardFocus:
                _driver().SendKeys("{Ctrl}a{Back}")
                time.sleep(0.2)
        except Exception:  # noqa: BLE001 -- best effort; nothing depends on it
            pass


def _open_sync(name: str) -> bool:
    """Click the row whose title is exactly `name`; say whether one was.

    False means *no such chat* -- the caller turns that into the standard
    refusal. The title goes into the collector's query rather than being
    searched for among an unfiltered cap: a chat below the first
    `MAX_TITLES` ones must still open, or "candidate" from one call would
    become "doesn't exist" on the next. The click itself is a real mouse
    click on a foregrounded window: coordinates aimed at a window that is
    merely visible can land on whatever is covering it, so foreground is
    required here too.
    """
    hwnd = _main_window()
    if hwnd is None:
        raise ToolError(MSG_NOT_RUNNING)
    rows = _rows(hwnd, query=name)
    match = next(
        (node for title, node in rows if title.casefold() == name.casefold()), None
    )
    if match is None:
        return False
    _ensure_foreground(hwnd)
    match.Click()
    time.sleep(OPEN_SETTLE_SECONDS)
    return True


def _composer_text(composer: Any) -> str | None:
    """Everything currently in the composer, or ``None`` if unreadable.

    Read through the text pattern (Chromium exposes it on contenteditable
    boxes), because what must be known before typing is whether there is
    already a draft in there: typing on top of one would send the user's
    half-written words mixed with ours.
    """
    try:
        pattern = composer.GetTextPattern()
    except Exception:  # noqa: BLE001
        return None
    if pattern is None:
        return None
    try:
        value = pattern.DocumentRange.GetText(-1)
    except Exception:  # noqa: BLE001
        return None
    return " ".join(str(value or "").split())


def _clear_composer(composer: Any) -> bool:
    """Select-all and erase, then verify the box is actually empty."""
    try:
        composer.SetFocus()
        time.sleep(0.1)
        _driver().SendKeys("{Ctrl}a{Back}")
        time.sleep(0.2)
    except Exception:  # noqa: BLE001
        return False
    text = _composer_text(composer)
    return text == ""


def _type_sync(text: str) -> None:
    """Put exactly `text` in the message box -- or leave the box clean.

    Two ways out that a retry can survive, and one that it cannot: no
    composer (nothing typed), a draft already there (nothing typed, and
    the user's words untouched), typing that failed with the box cleared
    afterwards (nothing typed), and typing that failed with leftovers the
    clear could not remove -- `DirtyText`, which the shared sequence holds
    for as long as an unconfirmed send, because that is the state it is.
    """
    hwnd = _main_window()
    if hwnd is None:
        raise ToolError(MSG_NOT_RUNNING)
    composer = _find_composer(hwnd)
    if composer is None:
        raise ToolError(MSG_NO_COMPOSER)
    existing = _composer_text(composer)
    if existing is None:
        raise ToolError(MSG_NO_COMPOSER)
    if existing:
        # Refuse rather than erase: a draft is the user's words, and
        # typing over it would send them without anyone approving them.
        raise ToolError(
            "There's already something written in WhatsApp's message box, so I "
            "didn't add to it. Send or clear that first, then ask me again. "
            "Nothing was sent."
        )
    try:
        _type_into(hwnd, composer, text)
    except Exception:
        if _clear_composer(composer):
            raise
        raise DirtyText(MSG_DIRTY) from None


def _send_sync() -> None:
    """Press Enter in the composer -- the same act, by another route.

    Enter is what WhatsApp treats as send (its default, locale-independent
    -- a UIA search for a button *named* "Send" would work only in English).
    Focus is re-established first: if the composer cannot take focus now,
    this raises *after* the text was typed, which the shared sequence reads
    correctly as "attempted, outcome unknown" rather than as a clean failure.
    """
    hwnd = _main_window()
    if hwnd is None:
        raise ToolError(MSG_NOT_RUNNING)
    composer = _find_composer(hwnd)
    if composer is None:
        raise ToolError(MSG_NO_COMPOSER)
    try:
        _ensure_foreground(hwnd)
        composer.SetFocus()
        time.sleep(0.15)
        if not composer.HasKeyboardFocus:
            raise ToolError(MSG_CANT_TYPE)
        _driver().SendKeys("{Enter}")
        time.sleep(0.2)
    finally:
        _release_foreground()


def _delivery_sync(text: str) -> bool:
    """Is this exact text in the conversation *now*? One live reading.

    Three exclusions, each closing a way this could confirm something
    false: text inside the composer's subtree (a send that failed leaves
    the message sitting exactly there), text in the left column (the chat
    list previews the last message, so a *previous* send would keep
    matching forever), and anything whose shape is not a text node. What
    remains is the message appearing in the conversation pane -- read
    after the send, never before it, which is the staleness rule from the
    module docstring made concrete.
    """
    wanted = " ".join(str(text or "").split())
    if not wanted:
        return False
    hwnd = _main_window()
    if hwnd is None:
        return False
    left_, top_, right_, bottom_ = _window_rect(hwnd)
    width = max(right_ - left_, 1)
    height = max(bottom_ - top_, 1)
    control = _driver().ControlFromHandle(hwnd)
    state = {"left": WALK_BUDGET, "found": False}

    def walk(node: Any, depth: int) -> None:
        if depth > MAX_DEPTH or state["left"] <= 0 or state["found"]:
            return
        try:
            children = node.GetChildren()
        except Exception:  # noqa: BLE001
            return
        for child in children:
            if state["left"] <= 0 or state["found"]:
                return
            state["left"] -= 1
            try:
                rect = child.BoundingRectangle
            except Exception:  # noqa: BLE001
                continue
            rel_left = (rect.left - left_) / width
            rel_top = (rect.top - top_) / height
            in_composer = (
                child.ControlTypeName in ("EditControl", "DocumentControl")
                and rel_top >= COMPOSER_TOP_FRACTION
                and (rect.bottom - rect.top) / height
                <= COMPOSER_MAX_HEIGHT_FRACTION
            )
            if in_composer:
                continue  # never read our own unsent text as a delivery
            if (
                child.ControlTypeName == "TextControl"
                and rel_left >= SIDEBAR_FRACTION
            ):
                value = " ".join(str(child.Name or "").split())
                if value == wanted:
                    state["found"] = True
                    return
            walk(child, depth + 1)

    walk(control, 0)
    if state["found"]:
        return True
    return False


def _delivery_poll_sync(text: str) -> bool:
    """Poll `delivery_seen` for a few seconds, like the Web path does."""
    for _ in range(SEARCH_POLLS):
        if _delivery_sync(text):
            return True
        time.sleep(SEARCH_POLL_SECONDS)
    return False


# ---------------------------------------------------------------------------
# The public surface, mirrored one-for-one on the Web path's
# ---------------------------------------------------------------------------


async def state() -> str:
    """``"ready"`` / ``"not_running"`` / ``"blind"`` -- checked before typing."""
    return await _run(_state_sync)


async def list_contacts(query: str) -> list[str]:
    """Chat-list titles matching ``query`` (all titles when it is empty)."""
    return await _run(_titles_sync, query)


async def search_number(number: str) -> str | None:
    """Type the number into search and return the chat it found, or none."""
    return await _run(_search_sync, number)


async def open_chat(name: str) -> bool:
    """Click exactly this chat; ``False`` when no chat is called that."""
    return await _run(_open_sync, name)


async def type_message(text: str) -> None:
    """Put exactly `text` in the message box, or leave it clean."""
    await _run(_type_sync, text)


async def clear_composer() -> bool:
    """Empty the message box and confirm it is empty.

    Exists for the refusal path. A draft left in the composer after a declined
    send is not harmless: the next send refuses with "there is already something
    written there", so one "no" would break messaging until the user found the
    window and cleared the box by hand. Returns whether it worked, and never
    raises -- this runs while an error is already on its way out.
    """
    try:
        return await _run(_clear_sync)
    except Exception:  # noqa: BLE001 - cleanup must not raise into the refusal
        return False


def _clear_sync() -> bool:
    """Worker-thread body of :func:`clear_composer`."""
    hwnd = _main_window()
    if hwnd is None:
        return False
    composer = _find_composer(hwnd)
    if composer is None:
        return False
    return _clear_composer_node(composer)


def _clear_composer_node(composer: Any) -> bool:
    """`_clear_composer`, taking the control rather than the window.

    Split so the refusal path does not have to re-find the window from inside a
    thread that may already have been asked to give up.
    """
    try:
        _ensure_foreground(_main_window() or 0)
    except Exception:  # noqa: BLE001 - focus is best effort for a clear
        pass
    return _clear_composer(composer)


async def press_send() -> None:
    """Send what is in the message box (Enter, the locale-independent act)."""
    await _run(_send_sync)


async def delivery_seen(text: str) -> bool:
    """Whether the text is in the conversation, read live for a few seconds."""
    return await _run(_delivery_poll_sync, text)
