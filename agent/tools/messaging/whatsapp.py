"""WhatsApp: one tool, two modes, and the human's finger on the send key.

The design in one paragraph, because every decision below falls out of it:
WhatsApp is the user's own account on the user's own machine, and a message
sent in their name is irreversible. So there are exactly two ways this tool
may operate, both gated, and neither may ever send twice.

- **deeplink** (the default) builds ``whatsapp://send?phone=...&text=...``
  and hands it to the OS. The desktop app opens with the recipient and the
  text *filled in* -- and the user presses send themselves. The tool
  prepares; it never sends. This needs no login, no automation, and no
  network call of ours, which is why it is the default.
- **automation** drives WhatsApp itself, types the message, and sends --
  *after* the approval card shows the recipient and the exact wording. It
  has two paths behind one seam: WhatsApp **Desktop** on Windows (the
  machine's own app, driven through UI Automation -- no browser to start)
  and WhatsApp **Web** in the isolated browser (the same profile the
  `browse` tool uses) underneath for everything else. One path is chosen
  before anything is resolved and used whole -- it is never switched
  mid-send -- and when neither is ready the refusal names what was tried
  and why each said no. It is opt-in because it is the mode where a bug
  could really send something.

Both modes obey the same three rules:

1. **Configuration can only turn things off.** The saved document's
   ``messaging.whatsapp.mode`` wins over ``LUMINE_WHATSAPP_MODE``, an
   unrecognised value is treated as ``off`` rather than downgraded to a
   mode that sends, and the policy floor for the call site is
   ``sensitive``/``destructive`` -- never something a settings file can
   lower to automatic.
2. **One message, once.** A `SendLedger` remembers what was sent for a
   dedup window (two identical calls a moment apart are one message, not
   two) and remembers what could *not be confirmed* for much longer --
   because replying to "I don't know whether it sent" by trying again is
   precisely the double-send this exists to prevent. Failures before the
   send attempt are retryable; failures after it are not. And a send is
   only ever *confirmed* by a reading taken after the attempt -- an
   observation older than the send cannot vouch for it, because a stale
   "it's there" passed off as current is the same lie in slow motion.
3. **Existing conversations only, resolved before the ask.** A name is
   searched against the account's own chat list; if several match, the
   tool returns the candidates and sends *nothing* -- the user picks, the
   second call resolves, and only then does an approval card appear, naming
   the person and quoting the text.

Nothing here types a password (WhatsApp Web login is a QR code the *user*
scans, and WhatsApp Desktop signs in on the user's own phone), and nothing
reads message history: contact names are the only thing that leaves the
account, capped and clipped like every other tool result.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import quote

try:
    from ...runtime.permission_gate import require_approval
    from ...runtime.progress import step as progress_step
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime.permission_gate import require_approval
    from runtime.progress import step as progress_step

from . import whatsapp_ops as ops
from . import whatsapp_refs as refs
from . import whatsapp_web as web
from .whatsapp_files import describe as describe_attachment
from .whatsapp_files import resolve_attachment

#: Sibling module, always reachable by relative name because `messaging` is a
#: package in both import shapes. Imported at module scope for the same reason
#: every other sibling is: a function-level relative import breaks the script path
#: the worker actually runs under.
from .whatsapp_flow import ChatCandidate, choose as choose_contact, kind_for_row

#: The one `open_chat`. Aliased on purpose: this module has a wrapper of the same
#: name that delegates to it, and a bare ``web.open_chat`` at the call site would
#: read as two implementations rather than one name bound twice.
#:
#: It is imported by value at module scope, like every other sibling. A
#: function-level import would break the script path the worker actually runs
#: under -- and the import-shape test exists because that is not a hypothetical.
_web_open_chat = web.open_chat

try:
    from ...settings.config_store import effective_document
except ImportError:  # running as a top-level script with agent/ on sys.path
    from settings.config_store import effective_document

try:
    from ..tools_compat import RunContext, ToolError, function_tool
    from ..tool_results import as_payload, wrap_untrusted
    from ..tools_text import clip

    # The *dedicated* WhatsApp tab, never `working_page`. This module and
    # `whatsapp_web.py` are one path split across two files, and for a while they
    # were pointed at two different pages: this one at the browse tab and that one
    # at the WhatsApp tab. The symptom was maddening to diagnose -- searching a
    # contact worked, and then selecting it did nothing, because the search ran on
    # one page and the click on another. If you are adding a Web operation here,
    # it must be `whatsapp_page()`.
    from ..browser.session import whatsapp_page
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_compat import RunContext, ToolError, function_tool
    from tool_results import as_payload, wrap_untrusted
    from tools_text import clip
    from browser.session import whatsapp_page

try:
    from . import whatsapp_desktop as desktop
except ImportError:  # running as a top-level script with agent/ on sys.path
    import whatsapp_desktop as desktop

logger = logging.getLogger("lumine")

#: The three modes, mildest first. `deeplink` prepares a draft;
#: `automation` sends. `off` hides the tool entirely.
MODES = ("off", "deeplink", "automation")

#: Saved document key, then the environment underneath it -- the same
#: precedence every other setting in this product follows.
CONFIG_PATH = ("messaging", "whatsapp", "mode")
MODE_VAR = "LUMINE_WHATSAPP_MODE"

#: An identical message inside this window is not prepared or sent twice.
DEDUP_VAR = "LUMINE_WHATSAPP_DEDUP_SECONDS"
DEFAULT_DEDUP_SECONDS = 10.0

#: How long an *unconfirmed* send blocks a retry. Longer than the dedup
#: window on purpose: the user needs time to look at WhatsApp and see
#: whether it went out before the tool is willing to risk saying yes.
UNCERTAIN_HOLD_SECONDS = 300.0

#: Whitespace collapses before anything else happens, so the ledger keys,
#: the approval card, and WhatsApp's own Enter-to-send behaviour all see
#: the same single-line text the user approved.
MESSAGE_MAX = 1000
RECIPIENT_MAX = 60

#: Digits only after normalization: ``str.isdigit`` would happily accept
#: Arabic-Indic or fullwidth digits and hand them to the OS as a URL.
PHONE_RE = re.compile(r"^[0-9]{6,15}$")

#: Which paths messaging may run on. **Web only.**
#:
#: This was `("web", "desktop")`, and the fallback was a genuine attempt to help:
#: a working desktop path starts nothing -- no browser, no profile, no QR code --
#: so it was worth trying where Web was not ready. It produced three separate
#: faults, all of them visible to the user rather than theoretical:
#:
#: - **Two applications for one sentence.** A send whose Web tab had not finished
#:   loading fell through to Desktop, so the desktop app opened, searched its own
#:   chat list, and the user watched two applications answer the same request.
#:   Measured in the worker log: the resolve step took 9.1 seconds -- UI Automation
#:   speed -- while Web was sitting right there.
#: - **A resolve that could not be followed by an open.** The name resolved on one
#:   path and the chat was then opened on another, so a contact the account has
#:   came back as "I couldn't read your chat list -- nothing matched". That is the
#:   Desktop/Web contradiction this module was supposed to have eliminated: the fix
#:   was to pick one path and use it whole, and the only way to *guarantee* that is
#:   to have one path.
#: - **A silent authority question.** Which application a message goes out of is not
#:   something the user should have to reason about mid-sentence, and neither is
#:   which one a reply lands in.
#:
#: So the messaging surface is Web, full stop. ``whatsapp_desktop.py`` stays in the
#: tree as the record of what the adapter could do and is no longer reachable from
#: any tool -- see :func:`choose_backend`, which says so out loud rather than
#: quietly skipping it.
#:
#: **The installed WhatsApp Desktop app is still launchable**, and that is a
#: different thing from a messaging path: "open WhatsApp Desktop" is a request to
#: start a program, and ``router.py`` sends it to ``open_app`` like any other. It
#: is only ever a *window*, never the thing a message is sent through.
PATH_PREFERENCE: tuple[str, ...] = ("web",)

#: Set to ``True`` by a caller that has explicitly asked for the installed
#: application. There is currently no such caller, and the flag exists so that one
#: cannot be added by accident: it is read in exactly one place, and adding a
#: second route to the desktop adapter would have to go through here.
PATH_PREFERENCE_IS_FIXED = True

#: How many steps a send narrates. Fixed so the "step 3 of 6" position means the
#: same thing every time; the sequence below is the same sequence every time.
#: How many steps a send narrates. Was six, and the sixth was the send itself.
#:
#: The list now ends at the approval question, because the answer to that question
#: *is* the send, and it is announced by the tool's return value. A step line for
#: it was not progress -- pressing Enter and reading the bubble takes a few hundred
#: milliseconds -- it was a second statement that the message had been sent, and
#: step lines reach the model. See the note at the send itself.
SEND_STEPS = 5

# Model-facing refusals. Each names what did *not* happen, because "denied"
# alone invites the model to rephrase the same request.
MSG_OFF = "WhatsApp messaging is switched off in configuration. Nothing was prepared."
MSG_NEED_NUMBER = (
    "I'm in draft mode, where WhatsApp only takes a phone number -- so I can't "
    "look anybody up by name. Give me the number in international form like "
    "+15551234567, or switch messaging to automation mode in Settings and I can "
    "find Afia by name and type the message in WhatsApp for you to confirm. "
    "Nothing was prepared."
)
MSG_BAD_RECIPIENT = "That isn't a phone number or a contact name. Nothing was prepared."
MSG_EMPTY = "There is no message text to send. Nothing was prepared."
MSG_TOO_LONG = "That message is too long for one send. Nothing was prepared."
MSG_NO_BODY = "WhatsApp is not open on this machine. Nothing was prepared."
MSG_NO_COMPOSER = "I can't see the message box on this chat. Nothing was typed."
MSG_NOT_LOGGED_IN = (
    "WhatsApp Web is not signed in on this machine yet. Scan the QR code once in "
    "Lumine's browser window and ask me again. Nothing was sent."
)
#: Distinct from the above, and the distinction is the whole point. WhatsApp Web
#: takes several seconds to show a chat list after the page loads; a refusal that
#: says "scan the QR code" during that window sends the user to re-scan a code
#: they have already scanned, and -- because messaging is Web-only now -- there is
#: no second path to quietly try instead. So "still starting" says so.
MSG_STILL_LOADING = (
    "WhatsApp is still loading in Lumine's browser window. Give it a moment and "
    "ask me again -- nothing was sent."
)
MSG_NO_CONTACTS = "I couldn't read your chat list -- nothing matched, and nothing was sent."
MSG_NOT_CONTACT = "That number isn't one of your WhatsApp chats. Nothing was sent."
MSG_UNCERTAIN = (
    "I could not confirm that WhatsApp accepted the message, so I have not tried "
    "again. Check WhatsApp; if it did not go out, ask me once more."
)
MSG_RETRY_UNCERTAIN = (
    "My last attempt at this exact message could not be confirmed, so I will not "
    "risk sending it twice. Check WhatsApp first. Nothing new was sent."
)
MSG_ALREADY_SENT = (
    "I already sent that exact message a moment ago -- not sending it twice. "
    "Nothing new was sent."
)
MSG_ALREADY_PREPARED = (
    "I already opened that message for you a moment ago -- it should be waiting in "
    "WhatsApp. Nothing new was prepared."
)


# ---------------------------------------------------------------------------
# Contact search by name
# ---------------------------------------------------------------------------
#
# The old path opened a chat by looking for a *title* among the rows WhatsApp
# happened to have rendered. On a real account that list is the last few dozen
# conversations, so "find Afia" worked only when Afia was already on screen --
# which is the reported bug, and the reason people were asked for a phone number
# instead.
#
# WhatsApp has its own search box, and that is what a person uses. So: type the
# name into it, wait for the list to actually change, read the candidates, clear
# the box. The box is cleared in a `finally` because a filter left behind narrows
# every later lookup, and a tool that only works once is a tool that lies.
#
# Every name below comes from the interface the user's own signed-in account
# shows. Nothing here reads a contact database, and nothing needs a number.


async def _search_contact_web(query: str) -> list:
    """Chat rows matching ``query``, through WhatsApp Web's own search box."""
    page = await whatsapp_page()
    focused = await page.evaluate(_JS_FOCUS_SEARCH)
    if not focused:
        raise ToolError(MSG_NO_SEARCH)
    try:
        # Real keystrokes, for the same reason `search_number` uses them:
        # WhatsApp's search is driven by its own key handling, and a scripted
        # value write types into the DOM without ever filtering the list.
        await page.keyboard.type(query)
        # Poll rather than assume a frame is enough: the filter arrives on a network
        # round trip, and a read that landed before it returns the *unfiltered*
        # list, whose first row is merely the most recent chat.
        for _ in range(8):
            await asyncio.sleep(0.35)
            rows = await page.evaluate(_JS_KINDS)
            matching = [row for row in rows if query.casefold() in str(row.get("title") or "").casefold()]
            if matching:
                return matching
        # No filter arrived. Reporting the whole chat list as a match for a name
        # would be worse than reporting none: the caller would ask the user to
        # choose from their entire history.
        return []
    finally:
        # Cleared on the way out. A filter left behind narrows every later lookup,
        # and a tool that only works once is a tool that lies on the second call.
        try:
            await page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001 - best effort; nothing depends on it
            pass


async def search_contact(backend: Any, query: str) -> list:
    """Chat candidates for ``query``, read on the path that will send.

    The path is a *parameter*, not a decision this function makes. It used to walk
    ``PATH_PREFERENCE`` itself, and that was how the two paths came to contradict
    each other: the lookup could succeed against Desktop's chat list while the
    message went to a Web tab that had never heard of the contact, or vice versa.
    Now whoever chose the path also reads the name from it, so the chat the name
    was found in is the chat the message goes to.

    A path that cannot search falls through to its own rendered list rather than
    reporting "no contacts" -- which would be a false answer about a person who is
    right there -- and a name that resolves to nothing is refused with a sentence
    that says so.
    """
    try:
        rows = await backend.search_name(query)
    except Exception as exc:  # noqa: BLE001 - a search that cannot run is a fallback
        logger.debug("[whatsapp] %s could not search by name: %s", backend.name, exc)
        rows = []
    if not rows:
        try:
            rows = [
                {"title": title}
                for title in await backend.list_contacts(query)
            ]
        except Exception:  # noqa: BLE001 - neither read worked; that is a refusal
            rows = []
    return [
        ChatCandidate(
            title=str(row.get("title") or ""),
            kind=str(row.get("kind") or "contact"),
            participants=tuple(row.get("participants") or ()),
        )
        for row in rows
        if str(row.get("title") or "").strip()
        ]


def _desktop_can_search() -> bool:
    """Whether the Desktop path has both a driver and a search box, right now.

    Synchronous on purpose: it is a Toolhelp snapshot plus a window probe, and
    making it a coroutine would let the caller ``await`` it inside the loop where
    a COM-touching call would be wrong.
    """
    capability = desktop.desktop_capability()
    return bool(capability["available"]) and desktop.has_search()


async def _web_can_search() -> bool:
    return await whatsapp_state() == "ready"


async def _web_contact_search(query: str) -> list:
    rows = await web.search_chats(query)
    out = []
    for row in rows:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        out.append(
            {
                "title": title,
                "kind": "group" if row.get("group") else "contact",
                "participants": (),
                "unread": int(row.get("unread") or 0),
            }
        )
    return out


async def _desktop_contact_search(query: str) -> list:
    rows = await desktop.search_contacts(query)
    return [
        {
            "title": str(row.get("title") or ""),
            "kind": str(row.get("kind") or "contact"),
            "participants": tuple(row.get("participants") or ()),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


def normalize_number(raw: object) -> str | None:
    """A bare international number, or ``None`` if this is not one.

    Formatting characters people actually type (spaces, dashes, parens, a
    leading ``+``) are removed; everything else must already be digits, and
    the length must be plausible for E.164. Not a single character is
    allowed through on a maybe -- this value becomes part of a URL the OS
    will open.
    """
    text = str(raw or "").strip()
    stripped = re.sub(r"[\s()\-.]", "", text)
    if stripped.startswith("+"):
        stripped = stripped[1:]
    if not PHONE_RE.match(stripped):
        return None
    return stripped


def collapse_text(raw: object) -> str:
    """Single line, trimmed. WhatsApp sends on Enter, so newlines are risk."""
    return " ".join(str(raw or "").split())


def build_deeplink(number: str, message: str) -> str:
    """``whatsapp://send?phone=...&text=...`` with nothing smuggled in.

    The number is digits (checked by `normalize_number` before this is
    called) and the text is percent-encoded with an empty safe set, so
    neither ``&`` in a message nor a stray ``#`` can change what the URL
    addresses.
    """
    url = f"whatsapp://send?phone={number}"
    if message:
        url += f"&text={quote(message, safe='')}"
    return url


def open_deeplink(url: str) -> None:
    """Hand a ``whatsapp://`` URL to the OS -- no shell, anywhere.

    Windows goes through ``os.startfile`` (a ShellExecute of one string:
    there is no command line to inject into), macOS and Linux through an
    argv pair. An unregistered scheme raises ``OSError`` here, which the
    tool turns into a sentence about WhatsApp not being installed rather
    than a silent nothing.
    """
    if sys.platform == "win32":
        if not hasattr(os, "startfile"):
            raise OSError("this platform has no way to open a link")
        os.startfile(url)  # type: ignore[arg-type]
        return
    opener = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
    try:
        result = subprocess.run(opener, capture_output=True, timeout=5, check=False)
    except FileNotFoundError as exc:
        raise OSError(f"{opener[0]} is not installed") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(str(exc)) from exc
    if result.returncode != 0:
        raise OSError(f"{opener[0]} exited {result.returncode}")


def resolve_mode() -> str:
    """The effective mode: saved document, then environment, then default.

    Mirrors `permission_gate.load_policy`: the document wins *per key*, the
    environment is the base underneath, and an unrecognised value is **off**
    -- a typo in a line meant to disable sending must not produce a mode
    that sends.
    """
    raw: object = None
    document, _source, _diagnostics = effective_document()
    section = document.get(CONFIG_PATH[0]) if isinstance(document, Mapping) else None
    if isinstance(section, Mapping):
        inner = section.get(CONFIG_PATH[1])
        if isinstance(inner, Mapping) and inner.get(CONFIG_PATH[2]) is not None:
            raw = inner.get(CONFIG_PATH[2])
    if raw is None:
        raw = os.getenv(MODE_VAR, "")
    text = str(raw or "deeplink").strip().lower()
    return text if text in MODES else "off"


def whatsapp_disabled_reason() -> str:
    """Why the tool is unavailable, or ``""`` when it is -- for the card."""
    if resolve_mode() == "off":
        return f"Off: set {MODE_VAR} or messaging.whatsapp.mode to use it."
    return ""


def dedup_window() -> float:
    """Seconds an identical send is suppressed, ``0`` to disable."""
    raw = os.getenv(DEDUP_VAR, "") or ""
    try:
        seconds = float(raw)
    except ValueError:
        seconds = DEFAULT_DEDUP_SECONDS
    return max(0.0, seconds)


def _digest(recipient: str, message: str) -> str:
    """What two calls must agree on to count as "the same message"."""
    return hashlib.sha256(f"{recipient}\n{message}".encode("utf-8")).hexdigest()[:32]


class SendLedger:
    """Process-local memory of what was sent, and what could not be confirmed.

    Two kinds of entry with two different lifetimes, because they answer two
    different questions:

    - ``sent`` -- held for the dedup window. "Did I just do that?" A second
      identical call inside the window is a repeated utterance or a
      parallel tool call, and one message is the right number.
    - ``uncertain`` -- held for minutes, because a send whose result we do
      not know must not be answered with a retry. The user checks WhatsApp
      first; only then may they ask again.

    Locked because launches and messages can arrive from different tasks in
    the same process; the cost of the lock is nothing next to a duplicate
    message in the user's name.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, float]] = {}

    def _prune(self, now: float) -> None:
        horizon = max(dedup_window(), 0.0)
        for key, (kind, at) in list(self._entries.items()):
            hold = UNCERTAIN_HOLD_SECONDS if kind == "uncertain" else horizon
            if now - at > hold:
                del self._entries[key]

    def check(self, recipient: str, message: str, now: float | None = None) -> str | None:
        """``"sent"``, ``"uncertain"``, or ``None`` for "not known"."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._prune(moment)
            entry = self._entries.get(_digest(recipient, message))
            return entry[0] if entry else None

    def mark(self, recipient: str, message: str, kind: str, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._entries[_digest(recipient, message)] = (kind, moment)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()


#: One ledger for the process, like the launch dedup it is modelled on.
ledger = SendLedger()


# ---------------------------------------------------------------------------
# WhatsApp Web, through the isolated browser
# ---------------------------------------------------------------------------
#
# These selectors live in WhatsApp's own markup and will drift when it
# changes; that fragility is contained here and surfaced as a spoken
# failure, which is a fair trade for a mode the user opts into. What is
# *not* negotiable is that every one of these runs inside the same route
# guard and the same profile as `browse` -- this is a page, and a page gets
# the treatment pages get.

#: Removed. It was a *second* reader of "is WhatsApp signed in", and the second
#: copy is always the one without the waiting: this one looked at `#pane-side`
#: exactly once and answered `"logged_out"` for a splash screen, while
#: `whatsapp_web.state` polls and reports `loading` as its own answer. The symptom
#: was a signed-in account being told to scan a QR code it had already scanned.
#: `whatsapp_state()` delegates now, so there is one reader. Kept as a note rather
#: than deleted silently, because "we removed the naive one" is the useful fact.
_JS_STATE_REMOVED = """
() => {
  const qr = document.querySelector('[data-testid="qrcode"]');
  const chats = document.querySelector('#pane-side');
  return { ready: !qr && !!chats };
}
"""

_JS_TITLES = """
(query) => {
  const q = String(query || '').trim().toLowerCase();
  const seen = new Set();
  const out = [];
  for (const el of document.querySelectorAll('#pane-side span[title]')) {
    const title = (el.getAttribute('title') || '').trim();
    if (!title || seen.has(title)) continue;
    seen.add(title);
    if (!q || title.toLowerCase().includes(q)) out.push(title);
    if (out.length >= 12) break;
  }
  return out;
}
"""

#: Group rows carry the participant count in a title attribute on the same element
#: ("‎Afia, Rahim +2"), so kind is read from the DOM rather than guessed from the
#: name -- which is the only way to tell "Afia" from "Afia, Rahim +2" reliably
#: and in any locale.
_JS_KINDS = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('#pane-side span[title]')) {
    const title = (el.getAttribute('title') || '').trim();
    if (!title) continue;
    const group = el.closest('[data-id*="@g.us"]') || el.closest('[data-id*="@c.us"]');
    const isGroup = !!(el.closest('[data-id*="@g.us"]') || /\\+\\d+\\s*$/.test(title));
    out.push({ title: title, kind: isGroup ? 'group' : 'contact' });
    if (out.length >= 40) break;
  }
  return out;
}
"""

_JS_FOCUS_SEARCH = """
() => {
  const box = document.querySelector(
    '#side [contenteditable="true"], #side input[type="text"], [data-testid="chat-list-search"] [contenteditable="true"]'
  );
  if (!box) return false;
  box.focus();
  return true;
}
"""

#: The attribute the row marker sets, and the selector that clicks it.
#:
#: **Both now live in `whatsapp_web`, and nothing here sets them.** This module
#: used to carry its own `_JS_MARK_CHAT_ROW` -- a second implementation of "find a
#: chat row by name and mark it", beside `whatsapp_web._JS_MARK_ROW`. Two copies
#: drifted: the Web copy falls back to the search box when the chat is not
#: rendered and this one did not, so the send tool could resolve a contact and then
#: fail to open it. Measured in the worker log: `find` returned `chat: "Me2"` and
#: 400 ms later `open_chat("Me2")` refused with "I couldn't read your chat list --
#: nothing matched".
#:
#: The names are kept as aliases rather than deleted, because a test that asserts
#: the marker is shared between the two modules is a test that will fail loudly if
#: somebody reintroduces a second copy -- which is the mistake being guarded
#: against, and the reason the aliases exist.
_ROW_MARK = web._ROW_MARK
_ROW_SELECTOR = web._ROW_SELECTOR

_JS_FOCUS_COMPOSER = """
() => {
  const composer = document.querySelector('footer [contenteditable="true"]');
  if (!composer) return false;
  composer.focus();
  return true;
}
"""

_JS_CLICK_SEND = """
() => {
  // The icon's data attribute is locale-independent; an aria-label is not.
  const icon = document.querySelector('[data-icon="send"]');
  const target = (icon && icon.closest('button')) ||
    document.querySelector('button[aria-label="Send"]') ||
    document.querySelector('[data-testid="send"]');
  if (!target) return false;
  target.click();
  return true;
}
"""

#: Is this exact text now an outgoing message in the conversation?
#:
#: **Found by geometry, not by class name**, and that is not a shortcut. The
#: probe read the live DOM of a real conversation and every message row came back
#: with no class, no `data-id` and no `data-testid` at all:
#:
#:     {"cls": "", "data_id": "", "testid": "", "role": "row", "text": "..."}
#:
#: So the selector this used to rely on -- `.message-out .selectable-text` --
#: cannot match on this build, ever. Which made `delivery_seen` always answer
#: "not delivered" for a message that had in fact been delivered, and that is the
#: whole of "she cannot send even after I said yes": the send worked, the
#: confirmation could not see it, and the ledger then held the retry for five
#: minutes.
#:
#: What *is* stable is layout, and it took a live DOM to learn the shape:
#:
#: - ``[role="row"]`` is a **full-width container** (920px inside a 932px pane), not
#:   a bubble. So a row's own width says nothing about the message.
#: - ``[data-testid="msg-container"]`` is the bubble. It is the one stable hook
#:   left, because the class names are hashed -- ``x1liijdw xu342n7 xelbjmh`` and
#:   no more.
#: - Direction is **bubble centre against pane centre**. The conversation pane
#:   begins at x=491 of a 1422px window, so a midpoint of the *window* puts every
#:   message in the left column -- a measured 200px bubble at 553..753 against a
#:   pane centre of 957 is left-aligned, and the window-midpoint rule said
#:   otherwise.
#:
#: This is the same geometric reasoning `whatsapp_desktop.py` is built on: where
#: there is no identity in the DOM, position is the identity.
#:
#: The whole string is compared, because a bubble carries its clock too and a
#: substring test would accept the wrong message.
_JS_DELIVERY = """
(text) => {
  const wanted = String(text || '').replace(/\\s+/g, ' ').trim();
  if (!wanted) return false;
  const main = document.querySelector('#main');
  if (!main) return false;
  const pane = main.getBoundingClientRect();
  const centre = pane.left + pane.width / 2;
  const composer = main.querySelector("footer [contenteditable='true']");
  const floor = composer ? composer.getBoundingClientRect().top : 0;
  for (const row of main.querySelectorAll('[role="row"]')) {
    if (floor && row.getBoundingClientRect().top > floor) continue;  // unsent draft
    const bubble = row.querySelector('[data-testid="msg-container"]');
    if (!bubble) continue;                       // a notice, not a message
    const rect = bubble.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 8) continue;
    // A bubble is narrow (200px in a 932px pane, measured); a notice wraps across
    // the column. A notice that reached here would sit right of centre and be
    // confirmed as a delivery of something nobody sent.
    if (rect.width > pane.width * 0.85) continue;
    if (rect.left + rect.width / 2 < centre) continue;   // theirs, not ours
    const raw = (bubble.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!raw) continue;
    const body = raw.replace(/\\b\\d{1,2}:\\d{2}(\\s?[AP]M)?\\b\\s*$/i, '').trim();
    if (body === wanted || raw === wanted) return true;
  }
  return false;
}
"""

#: Which chat WhatsApp is *showing*, read from the header rather than remembered.
#: The row that was clicked may have moved since, and a reconnect can land
#: anywhere, so this is re-read immediately before anything is typed into it.
_JS_ACTIVE_CHAT = """
() => {
  const header =
    document.querySelector("header [data-testid='conversation-info-header']") ||
    document.querySelector('#main header span[title]') ||
    document.querySelector("div[data-testid='conversation-info-header'] span[title]");
  const title = ((header && (header.getAttribute('title') || header.textContent)) || '').trim();
  return { title: title };
}
"""


async def whatsapp_state() -> str:
    """``"ready"``, ``"logged_out"`` or ``"loading"`` -- checked before anything is typed.

    Delegates to :func:`whatsapp_web.state`, and that is the whole point of the
    function existing.

    It used to be its own reader: **one** look at ``#pane-side``, ``"logged_out"``
    for anything else, no polling. WhatsApp Web is a single-page application with
    several seconds of splash between the document loading and the chat list being
    in the DOM, so on a cold tab that single look almost always lands during the
    splash -- and the answer was *"WhatsApp Web is not signed in on this machine
    yet. Scan the QR code once"*, for an account that was signed in and sitting
    there. The user had to scan a code that was never the problem.

    Two readers of one question is the fault this repository keeps documenting, and
    the second copy is always the one without the waiting. One reader now.
    """
    return await web.state()


async def list_contacts(query: str) -> list[str]:
    """Chat-list names matching ``query`` (all names when it is empty)."""
    page = await whatsapp_page()
    titles = await page.evaluate(_JS_TITLES, query)
    return [str(title) for title in (titles or [])]


async def search_number(number: str) -> str | None:
    """Type the number into search and return the chat it opened on, or none.

    The search box takes real keystrokes rather than a scripted value:
    WhatsApp's composer is a contenteditable driven by its own key handling,
    and synthetic ``value`` writes are the classic way to type a message
    that exists only in the DOM.
    """
    page = await whatsapp_page()
    focused = await page.evaluate(_JS_FOCUS_SEARCH)
    if not focused:
        raise ToolError(MSG_NO_BODY)
    await page.keyboard.type(number)
    # Search results arrive on a network round trip; poll rather than
    # assume a frame is enough.
    for _ in range(10):
        await asyncio.sleep(0.3)
        titles = await list_contacts("")
        if titles:
            return titles[0]
    return None


async def open_chat(name: str) -> bool:
    """Open a chat by exact title -- by delegating to the one implementation.

    This function used to carry its own body: mark the row, press it, poll the
    header twelve times, refuse on a mismatch. ``whatsapp_web`` has a *different*
    ``open_chat`` with the same job, and the two had drifted into answering
    differently -- which is the same class of fault this module keeps hitting, in
    the place where it hurts most:

    - the Web copy falls back to the **search box** when the chat is not rendered,
      because the chat list is virtualised and shows a handful of conversations;
      this copy had no fallback, so a contact the account has but the window does
      not show was unopenable, and the send refused with "I couldn't read your
      chat list -- nothing matched" for a chat the previous step had just found.
    - the Web copy waits for the header and applies the *refuse-on-mismatch,
      proceed-on-unreadable* rule; this copy re-implemented that rule separately,
      so a change to one was not a change to the other.

    Measured in the worker log: ``find`` returned ``chat: "Me2"`` and 400 ms later
    ``open_chat("Me2")`` refused -- same name, same page, two implementations.

    There is one ``open_chat`` now. Delegating rather than deleting is deliberate:
    this name is imported by ``web_backend`` and by tests, and keeping the symbol
    means nothing has to change to read the intent.
    """
    return await _web_open_chat(str(name or ""))


async def draft_message(text: str) -> bool:
    """Put exactly ``text`` in the message box and verify it is there.

    Returns whether the composer now holds precisely what was asked for, and
    clears the box when it does not. The verification is the point of this
    operation rather than of the keystrokes that precede it: the user is about to
    be asked to confirm a draft they can see, and a draft that silently lost its
    last few characters produces a confident "I typed I love you" followed by a
    message saying something else.

    Refuses rather than overwriting when the box already holds something. A draft
    is the user's words, and typing over one sends them without anyone approving
    them.

    This is also the check that stands in for an unreadable chat header. A
    composer only exists when *some* chat is open, and it only exists for that
    chat -- so "there is an empty composer here" is evidence that a conversation
    is open even when the header could not be read, and typing here is what
    ``open_chat`` is trusting when it cannot verify a title.
    """
    state = await web.composer()
    if not state.get("found"):
        # No composer means no conversation is open, which after a click means
        # the click did not land. This is the refusal `open_chat` cannot make.
        raise ToolError(MSG_NO_COMPOSER)
    if not state.get("empty", True):
        raise ToolError(
            "There's already something written in WhatsApp's message box, so I "
            "didn't add to it. Send or clear that first, then ask me again. "
            "Nothing was sent."
        )
    page = await whatsapp_page()
    focused = await page.evaluate(_JS_FOCUS_COMPOSER)
    if not focused:
        raise ToolError("I can't see the message box on this chat. Nothing was typed.")
    await page.keyboard.type(text)
    typed = str((await web.composer()).get("text") or "")
    # WhatsApp marks its own messages with directional marks. They are rendering,
    # not content, and comparing them would fail every single message.
    cleaned = typed.replace("\u200e", "").replace("\u200f", "").strip()
    if cleaned != " ".join(str(text or "").split()):
        await web._clear_composer()
        return False
    return True


async def discard_draft() -> bool:
    """Empty the composer. Best-effort, and never raises.

    Called after a *refusal*, and that is the only reason it exists. Leaving the
    text would be defensible -- it is visible, and the user could send it
    themselves -- but it would also mean every later send refuses with "there is
    already something written there", so a single "no" would break messaging until
    they cleared the box by hand. A refused action should cost the user nothing.
    """
    try:
        return await web._clear_composer()
    except Exception:  # noqa: BLE001 - cleanup must not raise into the refusal
        return False


async def press_send() -> None:
    """Click the send control; fall back to Enter, which WhatsApp treats as send."""
    page = await whatsapp_page()
    clicked = await page.evaluate(_JS_CLICK_SEND)
    if not clicked:
        focused = await page.evaluate(_JS_FOCUS_COMPOSER)
        if not focused:
            raise ToolError("I can't see the message box on this chat. Nothing was sent.")
        await page.keyboard.press("Enter")


async def delivery_seen(text: str) -> bool:
    """Poll for the message among outgoing bubbles, a few seconds at most."""
    page = await whatsapp_page()
    for _ in range(12):
        if await page.evaluate(_JS_DELIVERY, text):
            return True
        await asyncio.sleep(0.25)
    return False


# ---------------------------------------------------------------------------
# The automation seam: one send sequence, two paths behind it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """What a backend saw, and when it saw it.

    The timestamp is the point. `automation_send` accepts a confirmation
    only when it was observed *after* the send attempt, so a backend that
    answers from an earlier reading -- a cached tree, a page state from
    before Enter was pressed -- cannot confirm a send it never watched.
    """

    delivered: bool
    observed_at: float


@dataclass(frozen=True)
class Backend:
    """One WhatsApp path, as the send sequence sees it.

    Desktop and Web differ in every operation but not in the *shape* of
    the sequence, which is what lets the ordering rules -- resolve before
    gate, gate before typing, uncertain before retry -- live in one place
    instead of being enforced twice and believed once. `check` returns
    ``None`` when the path is ready, or its refusal sentence when not;
    `delivery_seen` returns an `Observation`, never a bare verdict.
    """

    name: str
    check: Callable[[], Awaitable[str | None]]
    list_contacts: Callable[[str], Awaitable[list[str]]]
    search_number: Callable[[str], Awaitable[str | None]]
    #: Search chats by a spoken *name*, through WhatsApp's own search box.
    #:
    #: This member exists so that the path is chosen exactly once. It used to be a
    #: module-level function that decided for itself -- Web first, then Desktop --
    #: which meant a send could resolve the name on Desktop and then send through
    #: Web, or the reverse. Two applications, two chat lists, two sets of answers,
    #: and a user being told the paths contradicted each other. With the operation
    #: on the backend, the name is looked up on the same path that will type into
    #: it, and the two cannot disagree.
    search_name: Callable[[str], Awaitable[list[dict[str, Any]]]]
    open_chat: Callable[[str], Awaitable[bool]]
    draft: Callable[[str], Awaitable[bool]]
    discard_draft: Callable[[], Awaitable[bool]]
    press_send: Callable[[], Awaitable[None]]
    delivery_seen: Callable[[str], Awaitable[Observation]]


def web_backend() -> Backend:
    """WhatsApp Web in the isolated browser.

    The operations are read from this module's namespace when the
    backend is *built*, so whichever functions are in place -- live ones
    or test doubles -- are the ones that run. Nothing is bound at import
    time for the same reason nothing is cached at import time.
    """

    async def check() -> str | None:
        """Whether the Web path can be used, and a reason when it cannot.

        The three states get three answers. They used to get one: anything that
        was not `"ready"` became *"scan the QR code"*, so a splash screen --
        several seconds of it on every cold tab, which is what a pre-warm is
        for -- was reported to a signed-in user as a signed-out account.

        Each message also names the remedy for *its own* state, because the whole
        cost of guessing wrong here is a user sent to do something that was never
        the problem.
        """
        verdict = await whatsapp_state()
        if verdict == "ready":
            return None
        if verdict == "loading":
            return MSG_STILL_LOADING
        return MSG_NOT_LOGGED_IN

    async def observe(text: str) -> Observation:
        return Observation(await delivery_seen(text), time.monotonic())

    return Backend(
        name="WhatsApp Web",
        check=check,
        list_contacts=list_contacts,
        search_number=search_number,
        search_name=_web_contact_search,
        open_chat=open_chat,
        draft=draft_message,
        discard_draft=discard_draft,
        press_send=press_send,
        delivery_seen=observe,
    )


def desktop_backend() -> Backend:
    """WhatsApp Desktop through UI Automation -- see `whatsapp_desktop`.

    The capability is checked first and the state read second, because
    they answer different questions: whether the app *can* be driven
    (platform, driver, window) and whether it currently *has* a chat list
    to drive. A capable path with nothing to show still falls through to
    Web, with both answers available to the refusal.
    """

    async def check() -> str | None:
        capability = desktop.desktop_capability()
        if not capability["available"]:
            return str(capability["reason"])
        token = await desktop.state()
        return None if token == "ready" else desktop.state_reason(token)

    async def observe(text: str) -> Observation:
        return Observation(await desktop.delivery_seen(text), time.monotonic())

    async def draft(text: str) -> bool:
        # No composer read on this path: the desktop adapter already refuses when
        # a draft is present, and re-walking the tree to confirm the text arrived
        # would cost a second full traversal for a check the type already implies.
        await desktop.type_message(text)
        return True

    async def drop() -> bool:
        return await desktop.clear_composer()

    return Backend(
        name="WhatsApp Desktop",
        check=check,
        list_contacts=desktop.list_contacts,
        search_number=desktop.search_number,
        search_name=_desktop_contact_search,
        open_chat=desktop.open_chat,
        draft=draft,
        discard_draft=drop,
        press_send=desktop.press_send,
        delivery_seen=observe,
    )


def _refusal(reasons: list[str]) -> str:
    """Every path that said no, in one sentence that says "nothing sent" once."""
    bodies = [reason.removesuffix(" Nothing was sent.") for reason in reasons]
    return f"{' '.join(bodies)} Nothing was sent."


async def choose_backend(verb: str = "send_text") -> Backend:
    """WhatsApp Web, and only WhatsApp Web.

    There used to be a second candidate. The Desktop adapter was a fallback for
    ``send_text`` and ``prepare_draft`` on the reasoning that a working desktop
    path starts nothing -- no browser, no profile, no QR code. In practice it
    meant a send whose Web tab was still loading silently went out through the
    desktop app instead, so the user watched two applications answer one sentence,
    and a name resolved on one path could not be opened on the other.

    ``verb`` is still checked against the operation table first. That check is not
    about choosing between paths any more -- there is one -- but it is what keeps
    an operation the Web path genuinely cannot do from being attempted and
    reported as something else.

    When Web is not ready, the refusal says *that*, in one sentence naming Web,
    rather than naming two applications and letting the user work out which one
    she meant.
    """
    reasons: list[str] = []
    for candidate in backend_candidates():
        path = _path_of(candidate)
        if path != "web":
            # Belt and braces. `backend_candidates()` reads `PATH_PREFERENCE`,
            # which is one entry, so this cannot fire today -- and a guard that
            # cannot fire is still the cheapest place to catch a second path being
            # added, because it fails here rather than in front of a user.
            logger.warning(
                "[whatsapp] %r asked for a path this build does not use", path
            )
            continue
        if not ops.supported_by(verb, path):
            reasons.append(ops.refusal_for(verb, path))
            continue
        why = await candidate.check()
        if why is None:
            return candidate
        reasons.append(why)
    raise ToolError(_refusal(reasons))


def _path_of(backend: Any) -> str:
    """``"web"`` or ``"desktop"``, from a backend's own name."""
    return "web" if "Web" in str(getattr(backend, "name", "")) else "desktop"


def backend_candidates() -> tuple[Backend, ...]:
    """The messaging paths, in order. One of them.

    Built on each call rather than once, because each backend reads its
    operations from this module's namespace when it is *built* -- so whichever
    functions are in place, live ones or test doubles, are the ones that run.
    Nothing is bound at import time, for the same reason nothing is cached at
    import time.

    ``desktop_backend`` is deliberately **not** in ``factories``. It still exists
    as a function and ``whatsapp_desktop`` still exists as a module, because
    deleting the record of what the adapter could do loses the reason it was not
    used; but nothing a tool can call reaches either. That is the whole of "remove
    every action regarding WhatsApp Desktop" -- an unreachable adapter and a
    launchable application are different states, and only one of them is gone.
    """
    factories = {"web": web_backend}
    return tuple(factories[name]() for name in PATH_PREFERENCE if name in factories)





# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def _choose_payload(for_whom: str, candidates: list[str]) -> str:
    names = [clip(name, 30) for name in candidates[:5]]
    return wrap_untrusted(
        as_payload(
            {
                "status": "choose",
                "for": clip(for_whom, 40),
                "candidates": names,
                "next": "Ask the user which one, then call again with that exact name.",
            }
        )
    )


async def _resolve_contact(backend: Any, name: str):
    """Resolve a contact name on the path that will send, or refuse.

    The backend is passed rather than chosen. It used to call ``choose_backend``
    itself, which meant the name could be resolved against one WhatsApp and the
    message sent through the other -- and when the two disagreed the user was
    told so, which is the correct thing for a tool to do and a useless thing to
    have to say.

    WhatsApp's own search box, not the rendered list: the list is the last few
    dozen conversations, so a name resolves only when that chat happens to be on
    screen. Falls back to the path's own list read when the search is
    unavailable, so a path that can read chats but has no search box still gets a
    genuine attempt rather than an immediate refusal.
    """
    rows = await search_contact(backend, name)
    if rows:
        return choose_contact(name, rows, allow_group=True)
    titles = await backend.list_contacts(name)
    return choose_contact(name, [ChatCandidate(title=title) for title in titles], allow_group=True)


async def _number_for_name(context: RunContext, name: str) -> str | None:
    """The number behind a contact name, or ``None`` when there isn't one.

    Draft mode exists so the *user* presses send, which is a reason to prepare a
    ``whatsapp://`` link -- not a reason to require the user to read a phone
    number aloud. Asking for one was the original reported bug: the same name
    that WhatsApp's own search box had just matched a moment earlier could not be
    turned into anything, so the conversation dead-ended on "give me her number".

    So the name is resolved through the account's own search box and the chat's
    JID, which *is* the number for a one-to-one chat. Three refusals stay
    refusals: a group has no number, a chat the account does not have is not
    invented, and a name that matches several chats asks rather than picks. The
    lookup is a read, so it is gated like every other read -- asking for approval
    must not be a side door around the tools page.
    """
    await require_approval(
        context,
        tool=ops.SEND_TOOL,
        action="resolve",
        action_class="read",
        summary=f"Look up WhatsApp contacts matching {clip(name, 30)}",
    )
    try:
        rows = await web.search_chats(name)
    except ToolError:
        return None
    except Exception:  # noqa: BLE001 - an unreachable page is a refusal, not a crash
        return None
    candidates = [
        ChatCandidate(
            title=str(row.get("title") or "").strip(),
            kind=kind_for_row(row),
        )
        for row in rows
        if str(row.get("title") or "").strip()
    ]
    choice = choose_contact(name, candidates)
    if not choice.ok or choice.chosen is None:
        return None
    chosen = choice.chosen.title
    for row in rows:
        if str(row.get("title") or "").strip() == chosen:
            return web.number_from_jid(row.get("jid")) or None
    return None


@function_tool()
async def send_whatsapp(
    context: RunContext,
    recipient: str,
    message: str,
    attachment: str = "",
    reply_to: str = "",
) -> str:
    """Message someone on WhatsApp from the user's own account, after approval.

    Give a number in international form (+15551234567). In the default mode a
    draft opens with the text filled in and the *user* presses send. With
    automation on, a contact name works too; if several match, the call returns
    them to choose from -- ask, then call again with that exact name.

    In automation mode the text is typed into the chat first and the user is asked
    to confirm while they can see it, so the question is about something they have
    read rather than something they must take on trust.

    Never guess a number from a name, never message anyone the user did not name,
    and never retry a send that reported failure after the attempt -- report it
    and let them decide. Never use this to check whether something arrived.

    There is no ``action`` argument: the operation follows from the mode and from
    which arguments are filled in. ``send_text`` (automation, plain text),
    ``prepare_draft`` (default mode), ``reply`` (``reply_to`` given, so it quotes),
    ``attach`` (``attachment`` given), ``typing`` (a hint while drafting).

    Args:
        recipient: A number in international form, or a contact name when
            automation is on.
        message: The text to send, worded as the user wants it.
        attachment: Optional file on this computer to send with it.
        reply_to: Optional message id from a read; the reply quotes it.
    """
    mode = resolve_mode()
    if mode == "off":
        raise ToolError(MSG_OFF)

    target = str(recipient or "").strip()
    if not target or len(target) > RECIPIENT_MAX:
        raise ToolError(MSG_BAD_RECIPIENT)
    text = collapse_text(message)
    caption = ""
    if not text and attachment:
        caption = ""
    elif not text:
        raise ToolError(MSG_EMPTY)
    if len(text) > MESSAGE_MAX:
        raise ToolError(MSG_TOO_LONG)
    file_path = resolve_attachment(attachment) if attachment else ""

    number = normalize_number(target)

    # -- draft mode: prepare, never send -----------------------------------
    if mode == "deeplink":
        if number is None:
            # A name, not a number. Resolved rather than refused, because the
            # default mode is this one and refusing every name made it unable to
            # do the one thing it exists for. Still only a *draft*: the user
            # presses send, exactly as before.
            with progress_step(
                context, ops.SEND_TOOL, "find", f"Looking for {clip(target, 24)}", index=1, total=2
            ):
                number = await _number_for_name(context, target)
            if number is None:
                raise ToolError(MSG_NEED_NUMBER)
        previous = ledger.check(number, text)
        if previous == "sent":
            # Not an error the user needs to hear twice: it is the dedup
            # working. Returned as the sentence it would have returned.
            return MSG_ALREADY_PREPARED
        await require_approval(
            context,
            tool=ops.SEND_TOOL,
            action="prepare",
            action_class="sensitive",
            summary=f"WhatsApp draft to +{number}: {clip(text, 40)}",
            target=f"+{number}",
        )
        try:
            open_deeplink(build_deeplink(number, text))
        except OSError as exc:
            raise ToolError(
                f"I couldn't open WhatsApp: {clip(str(exc), 80)}. Nothing was prepared."
            ) from None
        ledger.mark(number, text, "sent")
        return f"WhatsApp is open with a message to +{number} -- press send there and it goes."

    # -- automation mode ---------------------------------------------------
    # Everything below happens in ONE tool call, deliberately. The sequence
    # reads like five operations and it is one, because each extra tool call is
    # another completion round against a per-minute token budget and another
    # second of silence before the user hears anything. On the free Groq tier,
    # where the budget is 8,000 tokens a minute, five calls to do one message is
    # the difference between working and rate-limited.
    with progress_step(
        context, ops.SEND_TOOL, "open", "Opening WhatsApp", index=1, total=SEND_STEPS
    ):
        backend = await choose_backend("send_text")

    # -- resolve the recipient ---------------------------------------------
    if number is None:
        # A *read*, gated like every other read so a settings file that
        # tightened this tool can actually stop the lookup too.
        await require_approval(
            context,
            tool=ops.SEND_TOOL,
            action="resolve",
            action_class="read",
            summary=f"Look up WhatsApp contacts matching {clip(target, 30)}",
        )
        with progress_step(
            context,
            ops.SEND_TOOL,
            "find",
            f"Looking for {clip(target, 24)} in your contacts",
            index=2,
            total=SEND_STEPS,
        ) as found_step:
            candidates = await _resolve_contact(backend, target)
            if candidates.ok and candidates.chosen is not None:
                found_step["chat"] = clip(candidates.chosen.title, 40)
        if not candidates.ok:
            if not candidates.candidates:
                raise ToolError(MSG_NO_CONTACTS)
            # Ambiguous, or a group where a person was named: nothing is sent and
            # nothing is asked for approval. The user picks; the second call names
            # them exactly.
            return _choose_payload(target, [item.title for item in candidates.candidates])
        who = candidates.chosen.title
    else:
        who = f"+{number}"

    previous = ledger.check(who, text or file_path)
    if previous == "uncertain":
        # Before the gate as well as before the send: an approval the user
        # gave last time is not an approval to gamble again.
        raise ToolError(MSG_RETRY_UNCERTAIN)
    if previous == "sent":
        return MSG_ALREADY_SENT

    # -- open the chat and confirm it is the right one ------------------------
    with progress_step(
        context,
        ops.SEND_TOOL,
        "open_chat",
        f"Opening the chat with {clip(who, 24)}",
        index=3,
        total=SEND_STEPS,
    ):
        if number is not None:
            found = await backend.search_number(number)
            if not found:
                raise ToolError(MSG_NOT_CONTACT)
            if not await backend.open_chat(found):
                raise ToolError(MSG_NOT_CONTACT)
        elif not await backend.open_chat(who):
            raise ToolError(MSG_NO_CONTACTS)

    # A reply quotes the message it answers, which is the whole difference
    # between "reply to that one" and "send into whichever chat is open". So a
    # `reply_to` the Web path cannot honour is *refused* rather than quietly
    # dropped: a parameter that silently does nothing is worse than no parameter,
    # because the model will report a reply where the user received a message.
    quote_index = refs.parse_handle(reply_to) if reply_to else 0
    if quote_index and _path_of(backend) != "web":
        raise ToolError(
            "Replying to a specific message needs WhatsApp Web -- WhatsApp Desktop "
            "can send, but not quote which message it answers. Nothing was sent."
        )
    # The order here is the whole point of the flow. Ask first and the user
    # approves a sentence they cannot see; type first and the approval is about
    # something that is physically on their screen, in their own WhatsApp.
    if file_path:
        # Attachment first, text second. The order matters and getting it wrong
        # is silent: an attachment accompanied by a caption used to be drafted as
        # text and the file quietly dropped, which is the worst possible outcome
        # for a send -- the user sees a plausible message and a file that never
        # went anywhere. So the file is what the step is about, and the text
        # becomes its caption.
        with progress_step(
            context,
            ops.SEND_TOOL,
            "attach",
            f"Attaching {clip(os.path.basename(file_path), 24)} for {clip(who, 16)}",
            index=4,
            total=SEND_STEPS,
        ):
            # Attachments are Web-only, and the refusal says which path could have
            # done it rather than reporting a failure with no explanation. The
            # desktop attach button opens a *native* file dialog, which UI
            # Automation cannot type a path into.
            if _path_of(backend) != "web":
                raise ToolError(web.MSG_NO_ATTACH)
            if not await web.attach(file_path, caption=text):
                raise ToolError(web.MSG_NO_ATTACH)
    elif text:
        with progress_step(
            context,
            ops.SEND_TOOL,
            "draft",
            f"Typing the message for {clip(who, 20)}",
            index=4,
            total=SEND_STEPS,
        ):
            if quote_index:
                # The quote is attached *before* the text is typed, because
                # WhatsApp's reply bar is part of the composer: typing first and
                # quoting afterwards is not a thing the interface offers. The
                # check that the message is still the one we meant comes before
                # either, so a stale reference is refused rather than quoted.
                if not await web.quote_message(quote_index):
                    raise ToolError(
                        f"I couldn't attach the reply to that message, so nothing "
                        "was sent. Read the chat again and I'll try that."
                    )
            try:
                drafted = await backend.draft(text)
            except desktop.DirtyText:
                # Nothing went out, but the composer has leftovers a blind retry
                # would type on top of -- so the ledger holds it exactly like an
                # unconfirmed send, because from the user's side that is what it is.
                ledger.mark(who, text or file_path, "uncertain")
                raise ToolError(MSG_UNCERTAIN) from None
            if not drafted:
                raise ToolError(
                    "WhatsApp didn't take the message exactly as I typed it, so I "
                    "cleared the box and sent nothing. Try again and I'll retype it."
                )

    # -- the question -------------------------------------------------------
    with progress_step(
        context,
        ops.SEND_TOOL,
        "confirm",
        "Draft is ready -- asking you to confirm it",
        index=5,
        total=SEND_STEPS,
    ):
        try:
            await require_approval(
                context,
                tool=ops.SEND_TOOL,
                action="send",
                # The strongest class: real, irreversible send. The card quotes the
                # recipient and the wording -- the two things a person needs to see
                # to answer yes to *this* message rather than the one they said out
                # loud three seconds ago.
                action_class="destructive",
                summary=_send_summary(who, text, file_path),
                target=clip(who, 40),
            )
        except ToolError:
            # Refused, timed out, or duplicated. The draft is still sitting in
            # WhatsApp, and leaving it there would mean every later send refuses
            # with "there is already something written there" -- so one "no" would
            # break messaging until they cleared the box by hand.
            await backend.discard_draft()
            raise

    # -- send it, and confirm it left ---------------------------------------
    #
    # **No progress step here, and that is the fix.**
    #
    # There used to be a sixth step, "Sending to X", completing with
    # `verified="the message is in the conversation"`. Removing the `verified`
    # detail was not enough, and that is the part worth keeping: a step *message*
    # is not a private note. Every step line reaches the model, and the model is
    # what talks to the user. So the turn contained two statements that the message
    # had been sent -- the step's line and the return value -- and the model, being
    # asked to report on the turn, said both. The user heard the send confirmed
    # twice, in voice, which is the only channel that cannot be scrolled past.
    #
    # Nothing in this codebase speaks after a send: `permission_gate._announce`
    # speaks the *question* before the action, and the failure apology is a
    # different thing entirely. So a duplicate heard in the voice can only be the
    # model reporting two facts it was given. The fix is to give it one.
    #
    # The step is not lost, it is *the result*. `SEND_STEPS` drops to 5 because
    # the list now ends at the approval question and the answer is the line that
    # follows it -- which was always the honest shape, since pressing Enter and
    # observing the bubble is fast enough that a progress line for it was showing
    # off rather than informing.
    attempted_at = time.monotonic()
    try:
        await backend.press_send()
        observation = await backend.delivery_seen(text)
    except ToolError as exc:
        # The attempt is over and its result is not ours. This includes a
        # refusal that *sounds* pre-send -- "could not take the keyboard" is
        # exactly what a send looks like when Enter landed but the focus
        # check ran late. Passing the original sentence through would tell the
        # user nothing was sent, which is the one thing we do not know.
        logger.info("[whatsapp] send outcome unknown: %s", exc)
        ledger.mark(who, text or file_path, "uncertain")
        raise ToolError(MSG_UNCERTAIN) from None
    except desktop.DirtyText:
        ledger.mark(who, text or file_path, "uncertain")
        raise ToolError(MSG_UNCERTAIN) from None
    except Exception as exc:  # noqa: BLE001 - after the attempt, this is uncertain
        logger.warning("[whatsapp] send outcome unknown: %s", exc, exc_info=True)
        ledger.mark(who, text or file_path, "uncertain")
        raise ToolError(MSG_UNCERTAIN) from None
    if not (observation.delivered and observation.observed_at >= attempted_at):
        ledger.mark(who, text or file_path, "uncertain")
        raise ToolError(MSG_UNCERTAIN)

    # The confirmation is **spoken once, from here**, at the moment delivery is
    # observed rather than reported -- and the model is told it has been said, so
    # its own reply adds nothing. See `web.announce_sent` for why the speaker is
    # the product and not the model: both spoken lines the user heard came from
    # the model, one per fact, and asking it to report on a turn containing two
    # facts about one event produces two sentences.
    channel = await web.announce_sent(context, who)
    ledger.mark(who, text or file_path, "sent")
    if channel == SILENT:
        # Nothing outside the model can speak on this profile -- a native-audio
        # one raises on `say()` -- so its sentence is the confirmation and it is
        # the only one. Said plainly rather than left to be inferred.
        return f"Sent to {who} on WhatsApp. Say this once, in your own words."
    return (
        f"Sent to {who} on WhatsApp. You have already said this out loud, so do "
        f"not repeat it -- a short acknowledgement is enough."
    )


def _send_summary(who: str, text: str, file_path: str) -> str:
    """The one sentence the approval card and Lumine's voice both carry.

    It quotes the *actual* wording, because this is the last thing standing
    between the model and an irreversible message in the user's name. A summary
    that said "message Afia" instead would let someone approve one message and
    believe they had agreed to another -- which is the failure this whole flow
    exists to prevent.
    """
    if file_path and not text:
        return (
            f"Send {describe_attachment(file_path)} to {clip(who, 24)} on WhatsApp"
        )
    return f"Send to {clip(who, 24)} on WhatsApp: {clip(text, 60)}"