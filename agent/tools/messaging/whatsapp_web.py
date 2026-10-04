"""WhatsApp Web: the path that can actually read, and the one most verbs run on.

WhatsApp Desktop through UI Automation is the better path when it works -- nothing
to start, no login -- and it is still tried first for sending. It cannot do
anything else, though, and the reason is structural rather than a matter of
effort: every operation on the desktop path is a geometry heuristic against a
WebView2 accessibility tree, and twenty more of them is twenty more ways for
WhatsApp to ship a layout change and have Lumine confidently do the wrong thing to
somebody's chat. So the division is not "both paths, partially": the desktop
adapter keeps the two operations it is genuinely good at (search a contact, type
and send), and this module carries the rest.

## The three rules that shape every function here

**One evaluate per logical read.** A chat list read that walked twelve rows with a
`page.evaluate` each would pay twelve round trips into the browser for one
question, and on a warm page that is most of the latency. Every read below is a
single script that returns everything it needs in one go. This is the difference
between the flow feeling instant and feeling like it is thinking.

**Position plus text, never position alone.** WhatsApp exposes no stable id on a
message bubble in the DOM -- the identity lives in React state we cannot read. So a
message is addressed by counting from the newest and checked by what it says, and
`whatsapp_refs` turns that pair into something a caller can hold. An operation
that cannot find its target *refuses*. It never proceeds on "close enough": the
whole point of addressing a message is that it is the one you meant.

**A miss is a sentence, not a guess.** Selectors live in :data:`TARGETS` with
several candidates each, because WhatsApp renames things and a module with one
selector per idea breaks on the day they do. When every candidate misses, the
refusal names the *target* ("I couldn't find the message menu") rather than the
selectors, because a person cannot act on a CSS selector and can act on being told
which control went missing. :func:`probe` exists so the remaining gaps can be found
against the real page instead of guessed at -- see ``agent/whatsapp_probe.py``.

## Locale

Nothing here matches on a visible label. WhatsApp ships in many languages and an
`aria-label="Send"` test works only in English; the existing send path already
learned this and reaches for `data-icon` and `data-testid`, which are the same in
every build. Where a menu item genuinely has no such hook, :func:`_click_menu_item`
falls back to text and says so in the refusal, rather than silently picking the
first thing that looks close.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Sequence

try:
    from ..tools_compat import ToolError
    from ..tools_text import clip
    from ..browser.session import whatsapp_page
    from . import whatsapp_desktop as desktop
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_compat import ToolError
    from tools_text import clip
    from browser.session import whatsapp_page
    import whatsapp_desktop as desktop

logger = logging.getLogger("lumine")

#: Digits only, and ASCII only: ``str.isdigit`` would accept Arabic-Indic digits
#: and hand them to the OS as a URL. Declared here rather than imported from
#: `whatsapp` because that module imports this one, and the cycle would buy
#: nothing -- the two checks are about different strings anyway (a JID's numeric
#: half, and whatever a person typed).
PHONE_RE = re.compile(r"^[0-9]{6,15}$")


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------
#
# Each entry is one logical target and every selector that has been observed to
# name it. Ordered best-first. The list is deliberately long: WhatsApp renames
# these across releases and between A/B variants, and a path that only knows one
# spelling is a path that works until a Tuesday. `probe()` reports which one hit.

TARGETS: dict[str, tuple[str, ...]] = {
    "chat_list": ("#pane-side",),
    "chat_row": (
        # Measured on the live account: rows carry `data-testid="list-item-N"` and no
        # `data-id`, so this is the selector that identifies one. The `role="row"`
        # spelling stays, because it is what a build without the test id exposes --
        # and `role="row"` is also what the *conversation* uses for messages, so the
        # two must be told apart by the `#pane-side` prefix rather than by role.
        "#pane-side [data-testid^='list-item']",
        "#pane-side [data-id*='@c.us']",
        '[data-id*="@c.us"]',
        '[data-id*="@g.us"]',
        "#pane-side [role='listitem']",
        "#pane-side [role='row']",
    ),
    "search_box": (
        "#side [contenteditable='true']",
        "[data-testid='chat-list-search'] [contenteditable='true']",
        "[data-testid='search-input'] [contenteditable='true']",
        "#side input[type='text']",
        "div[title='Search'] [contenteditable='true']",
    ),
    "chat_header": (
        "header [data-testid='conversation-info-header']",
        "#main header span[title]",
        "div[data-testid='conversation-info-header'] span[title]",
    ),
    "conversation": ("#main",),
    # Measured against a live conversation and reported missing, correctly: this
    # build renders message rows with no class, no `data-id` and no
    # `data-testid` at all, so none of these can match. They are kept because a
    # future build may bring them back -- but nothing depends on them any more.
    # `lumineRows` reads direction from which column a `[role="row"]` sits in,
    # which is what actually holds. See `_JS_ROWS_FN`.
    "message_in": (
        "div.message-in",
        "[data-id^='true_']",
        "div[data-testid='msg-container'] div.message-in",
    ),
    "message_out": (
        "div.message-out",
        "[data-testid='msg-container'] div.message-out",
    ),
    "composer": (
        "footer div[contenteditable='true'][data-tab='10']",
        "footer [contenteditable='true']",
        "div[contenteditable='true'][data-tab='10']",
    ),
    "send_button": (
        # Measured: with a draft in the composer, `button[data-testid='send']`
        # and `[data-icon='send']` both miss, so `hasSend` was reported False and
        # *every* send took the Enter fallback. That fallback is fine -- Enter is
        # WhatsApp's own send gesture and is locale-independent -- but it meant the
        # button we can verify was live was never actually used, so the "clicked a
        # live control" property the module claims was not being exercised.
        "button[data-testid='send']",
        "[data-testid='send']",
        "span[data-icon='send']",
        "[data-icon='send']",
        "button[aria-label='Send']",
        "span[aria-label='Send']",
    ),
    "attach_button": (
        "div[data-testid='clip']",
        "button[data-testid='attach']",
        "[data-icon='attach']",
        "span[data-icon='attach']",
        "[aria-label='Attach']",
    ),
    "file_input": ("input[type='file']",),
    "message_menu": (
        "div[data-testid='message-menu']",
        "button[data-testid='msg-menu']",
        "[data-testid='msg-menu']",
        "[aria-label='Menu']",
        "div.message-in [role='button']",
        "div.message-out [role='button']",
    ),
    "row_menu": (
        "button[data-testid='row-menu']",
        "[data-testid='row-menu']",
        "div[data-testid='chat-list-item'] [data-icon='menu']",
        "[data-icon='menu-more-vertical']",
    ),
    "menu_surface": ("div[role='menu']", "div[role='dialog']", "[data-testid='popup']"),
    "chat_info": (
        "div[data-testid='chat-info']",
        "div[role='dialog']",
        "[data-testid='contact-info-drawer']",
    ),
    "settings_menu": ("span[data-icon='menu']", "[data-icon='settings']", "[aria-label='Menu']"),
    "more_menu": ("[data-icon='menu-more-vertical']", "[data-icon='more-vertical']"),
}

#: Menu entries addressed by the icon they render rather than the words they show.
#: Locale-independent, which is why these are preferred; see the module docstring.
MENU_ICONS: dict[str, tuple[str, ...]] = {
    "pin": ("pin", "pin-filled", "unpin"),
    "unpin": ("unpin", "pin"),
    "mute": ("mute-notifications", "notification-off"),
    "unmute": ("unmute-notifications", "notification"),
    "archive": ("archive", "archive-filled"),
    "unarchive": ("unarchive", "archive"),
    "mark_read": ("mark-as-read", "check-double"),
    "mark_unread": ("mark-unread", "circle-unread"),
    "delete": ("delete", "trash"),
    "forward": ("forward", "forward-message"),
    "star": ("star", "star-outline"),
    "unstar": ("star-filled", "star"),
    "edit": ("edit", "pencil"),
    "react": ("emoji", "sticker"),
    "reply": ("reply",),
    "block": ("block", "block-filled", "ban"),
    "contact": ("profile-info", "contact-info"),
    "clear": ("clear", "close", "delete"),
    "voice_call": ("call", "phone"),
    "video_call": ("video", "video-call"),
    "unlink": ("logout", "log-out", "trash"),
}


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

MSG_NOT_READY = (
    "WhatsApp Web is not signed in yet. Scan the QR code in Lumine's WhatsApp "
    "window once, then ask me again. Nothing was sent."
)
MSG_NOT_OPEN = "WhatsApp is not open on this machine. Nothing was sent."
MSG_NO_SEARCH = "I can't find WhatsApp's search box right now. Nothing was typed."
MSG_NO_HEADER = (
    "I can't see which chat WhatsApp has open. Nothing was typed -- I won't guess "
    "who would receive this."
)
MSG_NO_COMPOSER = "I can't see the message box on this chat. Nothing was typed."
MSG_DIRTY = (
    "I started typing and couldn't clear the message box afterwards. Open "
    "WhatsApp and clear that box before asking me again -- nothing was sent."
)
MSG_NOT_CHAT = "that chat is not in your WhatsApp, so nothing was sent"
MSG_NO_TARGET = "WhatsApp's layout has changed and I couldn't find the {target}. Nothing was sent."
#: Distinct from :data:`MSG_DIRTY` on purpose, because the two call for different
#: next steps. Dirty means *the box may still hold something* and the user should
#: look. This one means the box was **cleaned** and nothing was typed -- so there is
#: nothing to fix, and the honest report is that WhatsApp would not accept the text
#: this time. Saying "clear your message box" here would send them to clear an
#: empty one.
MSG_NOT_TYPED = (
    "WhatsApp did not keep the text I typed, so I've left the message box empty "
    "rather than send you something half-written. Nothing was sent -- ask me "
    "again in a moment."
)


def _missing(target: str) -> ToolError:
    return ToolError(MSG_NO_TARGET.format(target=target))


# ---------------------------------------------------------------------------
# Page scripts
# ---------------------------------------------------------------------------
#
# Literal strings, never model-authored, for the reason `browser/state.py` gives:
# anything evaluated in the page is something the model could not have written.
#
# Every read is bounded. A chat list, a conversation and a menu are all unbounded
# on WhatsApp's side, and an unbounded read into a voice turn's token budget is
# the failure this whole module is written against.

#: One pass over the chat list. Returns everything the reader needs per row --
#: title, the JID (WhatsApp's own stable identity for a chat), unread count,
#: preview, and the pinned/muted markers -- so a list of twenty chats is one
#: round trip rather than twenty.
#: One chat-list row, read from the test ids this build actually renders.
#:
#: The previous version read `span[title]`, `data-id` and `[data-testid="last-msg"]`,
#: and a live dump showed **none of them exist**. A row on the current build is::
#:
#:     <div role="row" data-testid="list-item-0" class="x10l6tqk xh8yej3 x1g42fcv">
#:       <div data-testid="cell-frame-container">
#:         <div data-testid="cell-frame-title">Dipto</div>
#:         <div data-testid="cell-frame-primary-detail">9/10/2026</div>
#:         <div data-testid="cell-frame-secondary">Khali mone hoy meye</div>
#:       <span data-testid="last-msg-status">Khali mone hoy meye</span>
#:
#: Hashed classes again, and **no `data-id` on any row** -- which is why every chat
#: came back with an empty `jid`, an empty `preview`, `group: false` and no
#: unread count. Nothing about the account was wrong; four of the five fields were
#: being read from selectors this build does not have.
#:
#: `group` is the one that cannot be recovered. It was derived from the JID, and a
#: group is now indistinguishable from a contact in the list. It is therefore
#: reported as **three-valued** -- true, false, or `null` for "cannot tell" -- and
#: `null` is treated as *possibly a group* downstream, so the rule that a group is
#: never silently substituted for a person still holds. Reporting `false` because
#: we could not look would have quietly switched that safety off.
#: The contact's name, with the phone number WhatsApp hangs off it removed.
#:
#: A **contact search result** -- somebody saved on the account who has never been
#: messaged -- is rendered differently from a chat row, and the difference is
#: load-bearing. Measured against the live account:
#:
#: ```text
#: chat row      cell-frame-title             = 'Me2'
#:               cell-frame-primary-detail    = '14:32'      <- the timestamp
#: contact row   cell-frame-title             = 'Jubayed Reza (22-23)'
#:               cell-frame-primary-detail    = ''
#: ```
#:
#: The name and the number share **one text run**. Walking the title cell found five
#: nested elements each carrying the whole string, and no element boundary between
#: "Jubayed Reza" and "(22-23)" -- so this cannot be read structurally, and the
#: reader has to strip it.
#:
#: Why that breaks three separate things, all reported by one user:
#:
#: - **The row cannot be clicked.** `_JS_MARK_ROW` compared the raw cell against the
#:   name, so "Jubayed Reza" never matched "Jubayed Reza (22-23)".
#: - **The open chat looks like the wrong chat.** `open_chat` verifies against the
#:   conversation header, which shows the *name*. Asking for "Jubayed Reza" and
#:   comparing it to the row's "Jubayed Reza (22-23)" refuses -- on the chat that
#:   had opened correctly.
#: - **The digits read as a group size.** `participants` looks for a small element
#:   whose whole text is 1-4 digits, and "22" and "23" are exactly that.
#:
#: ## Why this is narrow on purpose
#:
#: It only removes a **trailing** parenthesised run that is *entirely* a phone
#: number: digits plus the separators a phone number uses, at least four digits,
#: and either a separator or seven digits. So `'Jubayed Reza (22-23)'` becomes
#: `'Jubayed Reza'`, while a chat genuinely called `'Team (2024)'` keeps its
#: brackets -- four digits, no separator. Getting this wrong in that direction is
#: safe: `open_chat` then compares "Team" against a header reading "Team (2024)"
#: and **refuses**. The alternative error -- inventing a name -- is not available.
#:
#: One definition, used by both the reader and the row marker, because a reader
#: that strips the number and a marker that does not is precisely the pair that
#: made this bug invisible.
_JS_CONTACT_NAME_FN = """
function lumineContactName(raw) {
  const text = String(raw || '').replace(/\\s+/g, ' ').trim();
  const m = text.match(/\\s*\\(([^()]*)\\)\\s*$/);
  if (!m) return text;
  const inner = m[1];
  if (!/^[+\\d][\\d\\s()+.-]*$/.test(inner)) return text;
  const digits = (inner.match(/\\d/g) || []).length;
  const separated = /[+\\-\\s]/.test(inner.slice(1));
  if (digits < 4 || !(separated || digits >= 7)) return text;
  const cut = text.slice(0, m.index).trim();
  return cut || text;
}
"""

_JS_CHAT_ROW_FN = _JS_CONTACT_NAME_FN + """
function lumineChatRow(el) {
  // A row from the **Messages** section of a search result is not a chat.
  //
  // WhatsApp searches message text as well as names, and those rows are shaped
  // like chat rows -- they carry a `cell-frame-title`, so everything else here
  // reads them happily and hands back the *matched message* as though it were a
  // contact's name. Measured for the query "Me":
  //
  //   title='Ok, I won't send you any other messages'   <- a sentence, not a person
  //   title='this is me from another me.'              <- ditto
  //
  // Which is worse than a missed contact. A bogus candidate makes `choose` report
  // "more than one chat matches that name" for a name that has exactly one
  // contact, and `open_chat` would try to click a message row. The discriminator
  // is structural -- a message result carries a `chatlist-message-<hash>` cell,
  // and nothing else does:
  //
  //   chat row      cell-frame-container, cell-frame-title, cell-frame-primary-detail
  //   message row   chatlist-message-3EB0BE8E693D9F711AB8BF, cell-frame-title, ...
  //
  // Returning null here rather than in the caller means the plain chat-list read
  // and the search read cannot disagree about what a row is.
  if (el.querySelector('[data-testid^="chatlist-message-"]')) return null;

  const text = (node) => ((node && node.textContent) || '').replace(/\\s+/g, ' ').trim();
  const squashed = (node) =>
    ((node && node.childNodes) ? node : null) &&
    Array.from(node.childNodes)
      .filter((n) => !(n.nodeType === 1 && /^\\d{1,4}$/.test(text(n))))
      .map((n) => (n.textContent || ''))
      .join(' ')
      .replace(/\\s+/g, ' ')
      .trim();
  const pick = (tid) => el.querySelector('[data-testid="' + tid + '"]');

  const titleCell = pick('cell-frame-title');
  const titleNode = titleCell || el.querySelector('span[title]');

  // A group's participant count is its own element inside the title cell, so it is
  // found by *looking for a child whose whole text is a number* -- never by
  // regexing the title.
  //
  // That distinction is the whole of it. An earlier version read
  // ``title.match(/(\\d+)$/)`` and so called the contact "Me2" a two-person group,
  // which would have refused to message a person because their name ended in a
  // digit. A contact really named "Me2" puts the 2 *inside the name text*; a group
  // puts it *in an element of its own*. Same visible characters, and the element
  // test is the only thing that tells them apart.
  // The *raw* cell text first, then the name. Order matters: the number has to be
  // taken off before anything counts digits, or "22" from "(22-23)" reads as a
  // twenty-two-person group and a person is refused as a group.
  const rawTitle = titleCell ? (squashed(titleCell) || text(titleCell)) : text(titleNode);
  if (!rawTitle) return null;
  const title = lumineContactName(rawTitle);
  const isContact = title !== rawTitle;

  let participants = 0;
  // Skipped entirely for a contact row. Those digits are a phone number, and the
  // `\\d{1,4}` test cannot tell "(22)" from a participant count -- so a contact
  // saved on a number ending in a small digit would be reported as a group, which
  // is the "Me2" bug wearing a different hat.
  if (titleCell && !isContact) {
    for (const child of titleCell.querySelectorAll('span, div')) {
      if (child.querySelector('span, div')) continue;
      const value = text(child);
      if (!/^\\d{1,4}$/.test(value)) continue;
      participants = parseInt(value, 10);
      break;
    }
  }

  const stampNode = pick('cell-frame-primary-detail');
  const previewNode = pick('cell-frame-secondary') || pick('last-msg-status');
  const preview = text(previewNode);

  // A title can carry the group's participant count in this build; where it does,
  // it is the only signal there is. Read as a count rather than trusted as text.
  // A title can carry the group's participant count in this build; where it does,
  // it is the only signal there is. Read as a count rather than trusted as text.
  // The unread badge, found by test id and then *structurally*. It matters that
  // both exist: with only the test ids, a build that renames them reads every chat
  // as "no unread", and "which chats have I not answered?" answers "none" -- a
  // confident, wrong answer about the thing the user actually asked.
  //
  // The structural test is a small element whose entire text is a number. That is
  // what an unread badge is, and it cannot be confused with the timestamp (which
  // contains slashes) or the preview (which is prose).
  let badgeText = text(el.querySelector(
    '[data-testid="unread-count"], [data-testid="unread-badge"], ' +
    'span[data-icon="unread-count"], span[aria-label*="unread" i]'
  ));
  if (!badgeText) {
    for (const small of el.querySelectorAll('span, div')) {
      const value = text(small);
      if (!/^\\d{1,3}$/.test(value)) continue;
      const rect = small.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      if (rect.width > 40 || rect.height > 40) continue;
      if (small.contains(small.closest('[data-testid="cell-frame-secondary"]'))) continue;
      badgeText = value;
      break;
    }
  }

  let group = null;
  if (participants > 1) group = true;
  else if (/@g\\.us/.test(el.getAttribute('data-id') || '')) group = true;
  else if (el.querySelector('[data-icon="group"], [data-testid="group-icon"]')) group = true;

  return {
    title: title,
    // Untouched, so a caller that wants the number -- "which of these is +8801...?"
    // -- still has it, and so a strip that is wrong can be seen rather than hidden.
    raw: rawTitle,
    // A row WhatsApp rendered as a *contact* rather than a chat. Reported rather
    // than inferred downstream, because the distinction is the reason the title
    // needed stripping at all.
    contact: isContact,
    jid: '',
    group: group,
    unread: badgeText ? (parseInt(badgeText.replace(/\\D/g, ''), 10) || 1) : 0,
    hasUnreadBadge: !!badgeText,
    time: text(stampNode),
    preview: preview.slice(0, 120),
    pinned: !!el.querySelector('[data-icon="pin"], [data-testid="pinned"]'),
    muted: !!el.querySelector('[data-icon="mute-notifications"]'),
    participants: participants,
    text: (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200)
  };
}
"""


def _with_contact_name(param: str, body: str) -> str:
    """``body`` with :data:`_JS_CONTACT_NAME_FN` declared *inside* it.

    The same single-expression rule as `_with_rows`, over the smaller helper. The
    chat header needs the strip and needs nothing else, so pulling in the whole
    chat-row reader to get it would mean evaluating a large script on every header
    read -- and the header is read in a polling loop.

    As with `_with_rows`, the body must close the brace the wrapper opens.
    """
    return f"({param}) => {{\n{_JS_CONTACT_NAME_FN}{body}"


def _with_chat_row(param: str, body: str) -> str:
    """:data:`_JS_CHAT_ROW_FN` declared inside `body`, not concatenated before it.

    Playwright evaluates a string argument as a single expression, so
    ``function f(e){...}`` followed by ``(x) => {...}`` parses as
    ``f(e){...}(x) => {...}`` and the page answers::

        ReferenceError: lumineChatRow is not defined

    Which it did, on the first run of this code. The same trap as
    :func:`_with_rows`, reached a second time because the fix was not written down
    anywhere a new reader would look -- hence this being a function with a docstring
    rather than a concatenation at the call site.
    """
    return f"({param}) => {{\n{_JS_CHAT_ROW_FN}{body}"


#: Every chat the list currently renders, newest first.
#:
#: **The list is virtualised.** Only four rows existed in the DOM against an account
#: with many more chats, so a `cap` of 8 cannot return 8 rows -- it returns what is
#: on screen. That is reported as `rendered` alongside the rows rather than papered
#: over, because a list that quietly returned four of thirty chats is what makes
#: "who have I not replied to?" answer wrongly. `search_chats` reaches the whole
#: account; this answers "what is in front of me", and says so.
_JS_READ_CHATS = _with_chat_row("limits", """
  const cap = Math.max(1, limits.cap || 25);
  const root = document.querySelector('#pane-side');
  if (!root) return { found: false, rows: [], rendered: 0 };
  const rows = [];
  const seen = new Set();
  let rendered = 0;
  for (const el of root.querySelectorAll(
    '[data-testid^="list-item"], [role="listitem"], [role="row"]'
  )) {
    rendered++;
    const row = lumineChatRow(el);
    if (!row) continue;
    if (seen.has(row.title)) continue;
    seen.add(row.title);
    rows.push(row);
    if (rows.length >= cap) break;
  }
  return { found: true, rows: rows, rendered: rendered };
}
""")

#: The one definition of "a message in the open conversation", injected into every
#: script that has to address one.
#:
#: **Class names are gone.** The live DOM of a real conversation was probed with
#: ``agent/whatsapp_message_probe.py``, and a message row came back as::
#:
#:     {"cls": "x1liijdw xu342n7 xelbjmh x16pgt24", "role": "row", "text": "..."}
#:
#: Hashed, meaningless class names. So ``div.message-in, div.message-out`` -- which
#: six scripts here relied on -- matches **nothing** on the current build. The
#: consequences were all silent, which is why none of them was noticed:
#:
#: - ``delivery_seen`` could never see a sent message, so every send ended "I could
#:   not confirm", and the ledger held the retry for five minutes. That is the
#:   whole of "she cannot send even after I said yes".
#: - ``read_messages`` reported zero messages from a chat full of them.
#: - Anything addressing a message *by position* -- react, star, delete, forward --
#:   resolved index -N against an empty list and refused.
#:
#: Three measured facts decide what replaced it:
#:
#: 1. ``[role="row"]`` is a **full-width container** -- 920px inside a 932px pane --
#:    not a bubble. So a row's own width tells you nothing about the message.
#: 2. ``[data-testid="msg-container"]`` **is** the bubble: 200px, inside the row.
#:    That attribute is the one stable hook left.
#: 3. Direction is **bubble centre against pane centre**, not against the viewport.
#:    The conversation pane begins at x=491 of a 1422px window, so a viewport
#:    midpoint puts every message in the left column. Measured: a 200px bubble at
#:    553..753 against a pane centre of 957 is left-aligned, and it reads as yours
#:    either way unless the comparison uses the pane.
#:
#: A row is a message **only if it carries a bubble.** There was a structural
#: fallback here -- "the narrowest descendant carrying the row's whole text" -- and
#: it was measurably wrong: the encryption banner has no ``msg-container``, the
#: fallback matched *some* element holding its text, and that element came out
#: right of centre. The banner was then reported as a message of yours, with
#: ``delivery_seen`` confirming a delivery against it. A chat with no messages in
#: it would have reported one delivered. So the fallback is gone rather than
#: tuned: if a future build drops the attribute, the honest answer is an **empty
#: read** -- a refusal the user can see -- not a plausible wrong one.
#:
#: **Anything below the composer's top edge is not a message.** An unsent draft
#: sits in the composer verbatim, and a text match would otherwise confirm a send
#: that never happened.
#:
#: Each entry keeps both the row and the bubble, so a caller that needs to open a
#: menu on a message can reach it.
_JS_ROWS_FN = """
function lumineRows(main) {
  const out = [];
  if (!main) return out;
  const pane = main.getBoundingClientRect();
  const centre = pane.left + pane.width / 2;
  const composer = main.querySelector("footer [contenteditable='true']");
  const floor = composer ? composer.getBoundingClientRect().top : 0;
  for (const row of main.querySelectorAll('[role="row"]')) {
    if (floor && row.getBoundingClientRect().top > floor) continue;  // the composer
    const bubble = row.querySelector('[data-testid="msg-container"]');
    if (!bubble) continue;              // a notice, a divider, a call log
    const rect = bubble.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 8) continue;
    if (rect.width > pane.width * 0.85) continue;
    const raw = (bubble.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!raw) continue;
    const clock = (raw.match(/\\b\\d{1,2}:\\d{2}(\\s?[AP]M)?\\b\\s*$/i) || [''])[0].trim();
    out.push({
      row: row,
      bubble: bubble,
      raw: raw,
      body: raw.replace(/\\b\\d{1,2}:\\d{2}(\\s?[AP]M)?\\b\\s*$/i, '').trim(),
      time: clock,
      mine: rect.left + rect.width / 2 >= centre
    });
  }
  return out;
}
"""


def _with_rows(param: str, body: str) -> str:
    """`body`, wrapped in one arrow function that can see :data:`_JS_ROWS_FN`.

    The helper is declared **inside** the body rather than concatenated in front,
    and that is not a style preference. Playwright evaluates a string argument as a
    single expression: it wraps it, and if the result is a function, calls that with
    the argument. So ``function f(m){...}`` followed by ``(x) => {...}`` is parsed
    as ``f(m){...}(x) => {...}``, and the page answers::

        SyntaxError: Malformed arrow function parameter list

    Declared inside the body, it hoists and works. The alternative -- giving each
    script its own copy of the geometry -- is six copies of the one piece of
    knowledge that must not drift, because when it drifts the failure is silent:
    a delivered message stops being seen.

    `body` supplies its own closing brace, because it is the same text that was a
    complete script before this existed. Only the opening is added.
    """
    return f"({param}) => {{\n{_JS_ROWS_FN}{body}"


#: One pass over the open conversation's bubbles, newest last. ``indexFromEnd`` is
#: computed here rather than in Python so the count and the order cannot disagree
#: -- the reference is minted from the same list that was read.
_JS_READ_MESSAGES = _with_rows("limits", """
  const cap = Math.max(1, limits.cap || 20);
  const main = document.querySelector('#main');
  if (!main) return { found: false, rows: [] };
  const all = lumineRows(main);
  const rows = [];
  for (const item of all) {
    const el = item.row;
    const raw = item.body;
    const img = el.querySelector('img');
    const doc = el.querySelector('a[download], [data-testid="document-attachment"]');
    const audio = el.querySelector('audio, [data-testid="audio-player"]');
    let kind = 'text';
    if (!raw && doc) kind = 'document';
    else if (!raw && audio) kind = 'audio';
    else if (!raw && img) kind = 'image';
    else if (raw === '‎\u200e\u200f') kind = 'sticker';
    rows.push({
      text: raw.slice(0, 400),
      mine: item.mine,
      kind: kind,
      time: item.time
    });
  }
  const total = rows.length;
  return {
    found: true,
    rows: rows.slice(Math.max(0, total - cap)).map(function (row, index, kept) {
      row.indexFromEnd = kept.length - index;
      row.total = total;
      return row;
    })
  };
}
""")

#: The characters WhatsApp pads a `contenteditable` with: left/right-to-left marks,
#: a zero-width space, a word joiner, a soft hyphen and a BOM. Written as a JS
#: literal rather than a regex so it and :data:`_INVISIBLE` are provably the same
#: set -- see the note below.
#: Zero-width direction marks, a zero-width space, a word joiner, a soft hyphen and
#: a BOM -- the characters WhatsApp pads a `contenteditable` with.
#:
#: Written as real characters on purpose. An earlier version held the literal
#: two-character sequence ``\u200e`` inside a `str.replace`, which is not the mark
#: WhatsApp inserts, so it stripped nothing: a draft arrived intact and was still
#: reported as a mismatch. The same escaping mistake on the JavaScript side is why
#: an *empty* composer read as holding a draft, and that one refused every send.
_INVISIBLE = "\u200e\u200f\u200b\u2060\u00ad\ufeff"
_NBSP = "\xa0"

#: The characters WhatsApp pads a `contenteditable` with, as JS escape sequences --
#: left/right-to-left marks, a zero-width space, a word joiner, a soft hyphen, a BOM.
#:
#: **Emitted from `_INVISIBLE` rather than written out again.** It has to be: the
#: JavaScript emptiness test and the Python comparison had drifted apart once
#: already, and the drift is invisible in both languages at once -- a draft that
#: arrived intact read as a mismatch, and an *empty* box read as holding a draft,
#: which refused every send. One list, used by both, is the only arrangement that
#: cannot disagree.
_JS_INVISIBLE = "".join(f"\\u{ord(c):04x}" for c in _INVISIBLE)

#: The composer, its text, and whether a send button is live. Read together
#: because the decision "may I type here" needs all three at once.
#:
#: **The `empty` test is the whole of "she can select the chat but cannot type".**
#: It stripped only `\u200e` and `\u200f`, so a composer holding *nothing but* a
#: zero-width space read as "there is already something written in WhatsApp's
#: message box" -- and `type_message` refused on that, every single time, on every
#: chat. Measured on a real account: ``text=''`` while ``empty=False``. The user
#: saw a refusal about a draft they had never written, and could not get past it,
#: because Ctrl+A and Backspace left the same invisible residue and the clear
#: could not confirm success either.
#:
#: So the emptiness test uses the *same* character set as `_comparable`, and the
#: read reports both halves so the disagreement is visible rather than inferred.
_JS_COMPOSER = """
() => {
  const el =
    document.querySelector("footer div[contenteditable='true'][data-tab='10']") ||
    document.querySelector('footer [contenteditable="true"]') ||
    document.querySelector("div[contenteditable='true'][data-tab='10']");
  if (!el) return { found: false };
  const send = document.querySelector(
    "button[data-testid='send'], [data-testid='send'], [data-icon='send'], " +
    "span[data-icon='send'], button[aria-label='Send'], span[aria-label='Send']"
  );
  const raw = el.textContent || '';
  const visible = raw.replace(/[INVISIBLE]/g, '').replace(/\\s+/g, ' ').trim();
  return {
    found: true,
    text: visible,
    rawLength: raw.length,
    hasSend: !!send,
    empty: visible.length === 0
  };
}
""".replace("[INVISIBLE]", _JS_INVISIBLE)

#: Which chat is actually open. Read from the header rather than remembered: the
#: row that was clicked may have been moved by a new message, and a reconnect can
#: land anywhere.
#:
#: The header holds **two** things, and reading it as one produced a title that
#: matches no chat on earth. Measured against a live header:
#:
#: ```text
#: div[data-testid='conversation-info-header']
#:   ├─ div                                             -> 'Me2'   <- the name
#:   └─ div[data-testid='chat-subtitle'] > span[title]  -> 'click here for contact info'
#: ```
#:
#: `header.textContent` is *both*, so `active_chat()` returned
#: `'Me2click here for contact info'` for a chat named `Me2`. The open-chat check
#: refuses a title that mismatches -- deliberately, because a mismatch means we
#: would be typing into somebody else's conversation -- and a title that can never
#: match turns that check into one that **always refuses**. Which is what happened:
#: the chat was open, `open_chat` returned ``False``, and the send died on "I
#: couldn't read your chat list" for a chat the previous step had just found.
#:
#: The name is the first child that is not the subtitle. Naming the exclusion is
#: the point; stripping the string afterwards would be a guess about wording.
_JS_ACTIVE_CHAT = _with_contact_name("", """
  const header =
    document.querySelector("header [data-testid='conversation-info-header']") ||
    document.querySelector('#main header span[title]') ||
    document.querySelector("div[data-testid='conversation-info-header'] span[title]");
  if (!header) return { title: '', raw: '', open: false };
  const named =
    header.querySelector(':scope > *:not([data-testid="chat-subtitle"])');
  const raw = named
    ? (named.getAttribute('title') || named.textContent || '')
    : (header.getAttribute('title') || header.textContent || '');
  const flat = String(raw).replace(/\\s+/g, ' ').trim();
  // The *same* strip the chat rows get, because the header has the same habit: a
  // contact who has never been messaged renders its header as
  // 'Jubayed Reza (22-23)'. Comparing that against the name we asked for is a
  // mismatch on a chat that is exactly right -- and a mismatch is refused, so the
  // user watches the conversation open and is told it did not.
  //
  // `raw` is reported alongside so the caller can accept *either* spelling, which
  // keeps a chat genuinely named 'Team (2024)' working without re-implementing
  // this rule in Python. One definition, two forms, no second guess.
  const title = lumineContactName(flat);
  return {
    title: title,
    raw: flat,
    open: !!document.querySelector('#main') && !!title
  };
}
""")

#: Search the chat list. The query is typed with real keys by the caller; this
#: only reads what the filter left behind, and the caller compares it against a
#: pre-search baseline -- a read that landed before the filter applied returns
#: whichever chat sorts first, which is how "message Afia" becomes "message
#: whoever messaged last".
#:
#: Shares :data:`_JS_CHAT_ROW` with the plain list read rather than repeating it.
#: This one used to hard-code `unread: 0, preview: '', pinned: false, muted:
#: false` -- so a search result was a strictly poorer version of the same row, and
#: "has Afia replied?" resolved the name and then came back with nothing to say.
#: One row reader means search and browse cannot disagree about a chat.
_JS_FILTERED_ROWS = _with_chat_row("wanted", """
  const root = document.querySelector('#pane-side');
  if (!root) return { found: false, applied: false, rows: [] };
  const rows = [];
  const seen = new Set();
  // "Did the filter arrive?" answered structurally rather than by inference.
  //
  // A search result is grouped under section headers -- 'Chats', 'Contacts',
  // 'Groups' -- and a header is a `list-item-*` row with **no** title cell at all.
  // Measured: `list-item-0` was 'Contacts' with `cells: []`. Those rows never exist
  // in the unfiltered list, so one is proof the filter is in.
  //
  // This replaced a `title.includes(query)` test, and that test was the whole of
  // "she searched, the results appeared, and she said she found nobody": the
  // account has 'Jubayed Reza', speech recognition heard 'Jubayat', and
  // `'jubayed reza'.includes('jubayat')` is false. One letter from a match, and a
  // person who is demonstrably on the screen was reported as absent.
  let applied = false;
  for (const el of root.querySelectorAll(
    '[data-testid^="list-item"], [role="listitem"], [role="row"]'
  )) {
    if (!el.querySelector('[data-testid="cell-frame-title"]') &&
        !el.querySelector('span[title]')) {
      applied = true;
      continue;
    }
    const row = lumineChatRow(el);
    if (!row) continue;
    if (seen.has(row.title)) continue;
    seen.add(row.title);
    rows.push(row);
    // The same cap as `list_chats`, deliberately. A smaller cap here made the
    // "the list changed" check unreliable in the *permissive* direction: twelve of
    // an unchanged forty-row list is a strict subset, so a filter that had not
    // applied still looked like it had.
    if (rows.length >= 40) break;
  }
  return { found: true, applied: applied, rows: rows };
}
""")

#: Open a context menu on a message or a chat row, by position from the end.
#: Counts backwards for the same reason `whatsapp_refs` does: the newest end is
#: the one that moves when somebody is spoken to.
#:
#: It **marks** the trigger and does not click it. This carries the same lesson as
#: `_JS_MARK_CHAT_ROW`: `trigger.click()` inside the page dispatches an untrusted
#: event, which runs the page's listeners and none of the browser's defaults --
#: and WhatsApp's menus do not open on that. The click is done by Playwright,
#: through the real input pipeline, in `open_message_menu`.
#:
#: A menu that cannot be opened is a *refusal*, so this reports the failure
#: honestly rather than pretending the message was reached.
_JS_OPEN_MESSAGE_MENU = _with_rows("spec", """
  const main = document.querySelector('#main');
  if (!main) return { ok: false, reason: 'no conversation' };
  const all = lumineRows(main);
  const wanted = Number(spec.indexFromEnd || 1);
  const index = all.length - wanted;
  if (index < 0) return { ok: false, reason: 'no such message' };
  const row = all[index].row;
  const seen = all[index].body;
  if (spec.expect && seen !== String(spec.expect).replace(/\\s+/g, ' ').trim()) {
    return { ok: false, reason: 'different message', seen: seen };
  }
  let trigger =
    row.querySelector("button[data-testid='msg-menu']") ||
    row.querySelector("div[data-testid='message-menu']") ||
    row.querySelector("[data-testid='msg-menu']") ||
    row.querySelector("[aria-label='Menu']") ||
    row.querySelector("div[role='button']");
  if (!trigger) return { ok: false, reason: 'no menu trigger' };
  trigger.scrollIntoView({ block: 'center' });
  // Clear first: reacting to a second message would otherwise press the *first*
  // one's menu trigger, and the confirmation would then check the wrong bubble --
  // which is how a reaction ends up on the wrong message without anything failing.
  for (const old of document.querySelectorAll('[data-lumine-wa-menu]')) {
    old.removeAttribute('data-lumine-wa-menu');
  }
  trigger.setAttribute('data-lumine-wa-menu', '1');
  return { ok: true, text: seen };
}
""")

#: The attribute `_JS_OPEN_MESSAGE_MENU` sets, and the selector that clicks it.
#: Held next to the script so the two cannot drift apart -- which is how a marker
#: ends up set under one name and looked for under another, and every menu simply
#: fails to open with nothing logged.
_MENU_MARK = "data-lumine-wa-menu"

#: Delay between keystrokes, in milliseconds.
#:
#: WhatsApp's composer is a React-controlled ``contenteditable``: every keystroke
#: re-renders the whole message list around it. Typed as fast as Playwright will
#: send, roughly one character in three is dropped, and the draft that reaches the
#: approval card is silently truncated -- so the user agrees to send text that is
#: not the text they were shown.
#:
#: The verification after typing is what makes this safe rather than lucky, and the
#: two belong together: a delay is an improvement, the check is the guarantee. It
#: costs about 60ms on a 25-character message, against a voice turn that already
#: spent seconds opening the tab.
KEY_DELAY_MS = 12

#: Markers for the two clicks that go through Playwright rather than the page.
#: Declared together with `_MENU_MARK` because all three share one reason for
#: existing, and a fourth added later should be added here rather than invented.
_FOCUS_MARK = "data-lumine-wa-focus"
_CLICK_MARK = "data-lumine-wa-click"
_TYPING_MARK = "data-lumine-wa-typing"
_ROW_MENU_MARK = "data-lumine-wa-rowmenu"

#: The composer's own selector, held as data because `set_typing` needs it without
#: going through `TARGETS` -- it must not be able to *change* what it focuses, only
#: mark whatever the composer already is.
_COMPOSER_PRIMARY = "footer div[contenteditable='true'][data-tab='10']"

#: Mark a chat row's menu trigger, by exact title.
#:
#: It marks rather than clicks, for the same reason as `_JS_MARK_ROW`: the hover
#: above is not a real hover and the click is not a real click, so pin, mute and
#: archive all reported success and changed nothing. Playwright presses the mark in
#: `open_row_menu`, and its own hover is what makes the trigger exist at all -- it is
#: hidden until the row is hovered on some builds, which is why this has to be a
#: real press rather than a dispatched `mouseover`.
_JS_MARK_ROW_MENU = """
(title) => {
  const wanted = String(title || '').trim().toLowerCase();
  const root = document.querySelector('#pane-side');
  if (!root) return { ok: false, reason: 'no chat list' };
  for (const old of document.querySelectorAll('[data-lumine-wa-rowmenu]')) {
    old.removeAttribute('data-lumine-wa-rowmenu');
  }
  for (const el of root.querySelectorAll(
    '[data-id*="@c.us"], [data-id*="@g.us"], [role="listitem"], [role="row"]'
  )) {
    const titleEl = el.querySelector('span[title]') || el.querySelector('div[title]');
    const value = ((titleEl && (titleEl.getAttribute('title') || titleEl.textContent)) || '')
      .trim().toLowerCase();
    if (value !== wanted) continue;
    const trigger =
      el.querySelector("button[data-testid='row-menu']") ||
      el.querySelector("[data-testid='row-menu']") ||
      el.querySelector("[data-icon='menu']") ||
      el.querySelector("[data-icon='menu-more-vertical']");
    if (!trigger) return { ok: false, reason: 'no row menu' };
    trigger.setAttribute('data-lumine-wa-rowmenu', '1');
    el.scrollIntoView({ block: 'center' });
    return { ok: true };
  }
  return { ok: false, reason: 'no such chat row' };
}
"""

#: Read an open menu. Both the text and the icon of every entry, because the icon
#: is what we match on and the text is what we fall back to -- and because a
#: diagnostic that shows both is the only way to find out what a WhatsApp build
#: actually offers.
_JS_READ_MENU = """
() => {
  const surface =
    document.querySelector("div[role='menu']") ||
    document.querySelector("div[role='dialog']") ||
    document.querySelector("[data-testid='popup']");
  if (!surface) return { found: false, items: [] };
  const items = [];
  for (const el of surface.querySelectorAll(
    "div[role='menuitem'], button[role='menuitem'], [data-testid='menu-item'], li"
  )) {
    const icon = el.querySelector('[data-icon]');
    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!text && !icon) continue;
    items.push({
      text: text.slice(0, 40),
      icon: (icon && icon.getAttribute('data-icon')) || '',
      testid: el.getAttribute('data-testid') || ''
    });
  }
  return { found: true, items: items };
}
"""

#: Mark the menu entry for `wanted`: by icon first, by exact text only as a
#: fallback. It marks rather than clicks.
#:
#: An in-page `el.click()` on a menu item is the one place this was left alone, on
#: the argument that a menu item is a plain DOM element. That argument turned out
#: to be the wrong way round: the click fired the page's handler, the handler
#: changed React state, and the menu closed -- while the operation went on to look
#: for the marker it expected and never found it. So "I clicked Delete" was
#: reported as a failure and "I pinned it" as success at the same time.
#:
#: A real press, in the order the other clicks use: mark, then `page.click`.
#: Playwright's auto-wait is exactly right for a portal that animates open and
#: disappears on the first state change.
_JS_MARK_MENU_ITEM = """
(wanted) => {
  const icons = Array.isArray(wanted.icons) ? wanted.icons : [];
  const words = String(wanted.text || '').trim().toLowerCase();
  // One item marked at a time. Two, and the press lands on whichever the menu
  // happens to list first rather than the one that was matched here.
  for (const old of document.querySelectorAll('[data-lumine-wa-item]')) {
    old.removeAttribute('data-lumine-wa-item');
  }
  const surface =
    document.querySelector("div[role='menu']") ||
    document.querySelector("div[role='dialog']") ||
    document.querySelector("[data-testid='popup']");
  if (!surface) return { ok: false, reason: 'menu closed' };
  const items = surface.querySelectorAll(
    "div[role='menuitem'], button[role='menuitem'], [data-testid='menu-item'], li"
  );
  for (const el of items) {
    const icon = el.querySelector('[data-icon]');
    const value = (icon && icon.getAttribute('data-icon')) || '';
    if (icons.includes(value)) {
      el.setAttribute('data-lumine-wa-item', '1');
      return { ok: true, via: 'icon', value: value };
    }
  }
  for (const el of items) {
    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    if (text && words && text === words) {
      el.setAttribute('data-lumine-wa-item', '1');
      return { ok: true, via: 'text', value: text };
    }
  }
  return { ok: false, reason: 'no such item' };
}
"""

_ITEM_MARK = "data-lumine-wa-item"
_CONFIRM_MARK = "data-lumine-wa-confirm"
_EMOJI_MARK = "data-lumine-wa-emoji"
_BLOCK_MARK = "data-lumine-wa-block"
_UNLINK_MARK = "data-lumine-wa-unlink"
_CALL_MARK = "data-lumine-wa-call"

#: Mark the confirming button of a modal -- "Delete for everyone?" is a dialog with
#: two buttons and picking the wrong one is the whole operation.
#:
#: Exact text match, no icon and no "first button" shortcut: this is the one click
#: in the module where being nearly right deletes something irreversibly. An empty
#: `words` therefore marks nothing rather than defaulting to the affirmative.
_JS_MARK_CONFIRM = """
(words) => {
  const wanted = String(words || '').trim().toLowerCase();
  if (!wanted) return { ok: false, reason: 'no confirmation text given' };
  // One button marked at a time: a leftover mark from a previous dialog would put
  // this press on the wrong control, and this is the click that deletes.
  for (const old of document.querySelectorAll('[data-lumine-wa-confirm]')) {
    old.removeAttribute('data-lumine-wa-confirm');
  }
  const dialog =
    document.querySelector("div[role='dialog']") || document.querySelector("div[role='alertdialog']");
  if (!dialog) return { ok: false, reason: 'no dialog' };
  for (const el of dialog.querySelectorAll('button, [role="button"]')) {
    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    if (text === wanted) {
      el.setAttribute('data-lumine-wa-confirm', '1');
      return { ok: true };
    }
  }
  return { ok: false, reason: 'no such button' };
}
"""

#: A message bubble addressed by position from the end, with the text it holds.
#: Used for the read that must happen *immediately before* an action: it is what
#: turns "the message at position 2" into "the message at position 2, which says
#: this", and a mismatch refuses.
_JS_MESSAGE_AT = _with_rows("spec", """
  const main = document.querySelector('#main');
  if (!main) return { ok: false, reason: 'no conversation' };
  const all = lumineRows(main);
  const index = all.length - Number(spec.indexFromEnd || 1);
  if (index < 0) return { ok: false, reason: 'no such message', total: all.length };
  return {
    ok: true,
    total: all.length,
    text: all[index].body.slice(0, 400),
    mine: all[index].mine
  };
}
""")

#: Whether this exact text is present as an *outgoing* bubble in the conversation
#: pane. Excludes the composer and the left column, both of which contain the text
#: of a message that did not go anywhere: an unsent draft sits in the composer
#: verbatim, and the chat list previews the last message, so a previous send would
#: match forever.
#: Is this exact text now an outgoing message?
#:
#: **Found by geometry.** The live DOM of a real conversation was probed and every
#: message row came back with no class, no `data-id` and no `data-testid` -- so
#: `.message-out`, which this used to rely on, cannot match on the current build.
#: That made a delivered message read as undelivered, which is what produced "she
#: cannot send even after I said yes" and a five-minute retry hold on a send that
#: had in fact worked.
#:
#: What is stable is layout: WhatsApp puts your own messages flush right and
#: theirs flush left, inside `[role="row"]` elements. Same geometric reasoning as
#: `whatsapp_desktop.py`. The whole string is compared, because a row carries its
#: timestamp too and a substring test would accept the wrong message.
#:
#: It reuses `_JS_ROWS_FN` rather than restating the geometry, because six scripts
#: independently re-deriving "which column is mine" is six places for the answer to
#: drift -- and when they drift, delivery is the one that fails *silently*, turning a
#: sent message into "could not confirm".
_JS_DELIVERED = _with_rows("text", """
  const wanted = String(text || '').replace(/\\s+/g, ' ').trim();
  if (!wanted) return false;
  const main = document.querySelector('#main');
  if (!main) return false;
  for (const item of lumineRows(main)) {
    if (!item.mine) continue;
    if (item.body === wanted || item.raw === wanted) return true;
  }
  return false;
}
""")

#: Whether a reaction badge is on a specific message.
_JS_REACTED = _with_rows("spec", """
  const main = document.querySelector('#main');
  if (!main) return { ok: false, reason: 'no conversation' };
  const all = lumineRows(main);
  const index = all.length - Number(spec.indexFromEnd || 1);
  if (index < 0) return { ok: false, reason: 'no such message' };
  const badge = all[index].row.querySelector('[data-testid="reaction"], span[data-icon="reaction"]');
  return { ok: true, reacted: !!badge, reaction: ((badge && badge.textContent) || '').trim() };
}
""")

#: Whether a message is still there *and* replaced by the tombstone WhatsApp
#: renders in its place. Absence is not the evidence: a conversation that scrolled
#: past the message produces the same absence as a successful delete.
_JS_DELETED = _with_rows("spec", """
  const main = document.querySelector('#main');
  if (!main) return { ok: false, reason: 'no conversation' };
  const all = lumineRows(main);
  const index = all.length - Number(spec.indexFromEnd || 1);
  if (index < 0) return { ok: false, reason: 'no such message' };
  const tomb = all[index].row.querySelector("[data-testid='revoked'], .revoked, [data-icon='revoke']");
  return { ok: true, revoked: !!tomb };
}
""")

#: Whether a chat row still carries a given marker -- pinned, muted, unread, or
#: starred. The single shape four operations verify themselves with, because
#: "the row looks different" is not a thing you can check.
_JS_ROW_FLAG = """
(spec) => {
  const wanted = String(spec.title || '').trim().toLowerCase();
  const root = document.querySelector('#pane-side');
  if (!root) return { found: false };
  for (const el of root.querySelectorAll(
    '[data-id*="@c.us"], [data-id*="@g.us"], [role="listitem"], [role="row"]'
  )) {
    const titleEl = el.querySelector('span[title]') || el.querySelector('div[title]');
    const value = ((titleEl && (titleEl.getAttribute('title') || titleEl.textContent)) || '')
      .trim().toLowerCase();
    if (value !== wanted) continue;
    const flag = String(spec.flag || '');
    const badge = el.querySelector("[data-testid='unread-count']");
    return {
      found: true,
      present: flag === 'pinned'
        ? !!el.querySelector('[data-icon="pin"], [data-testid="pinned"]')
        : flag === 'muted'
        ? !!el.querySelector('[data-icon="mute-notifications"]')
        : flag === 'unread'
        ? !!badge
        : !!el.querySelector(`[data-icon="${flag}"]`)
    };
  }
  return { found: false };
}
"""

#: A message-level marker, for the star.
_JS_MESSAGE_FLAG = _with_rows("spec", """
  const main = document.querySelector('#main');
  if (!main) return { ok: false };
  const all = lumineRows(main);
  const index = all.length - Number(spec.indexFromEnd || 1);
  if (index < 0) return { ok: false };
  const star = all[index].row.querySelector("[data-icon='star'], [data-icon='star-filled']");
  return { ok: true, present: !!star };
}
""")

#: The chat's own info drawer: about text, and the last-seen line. Both come from
#: the same surface, so one open answers "who is this and are they around".
_JS_CHAT_INFO = """
() => {
  const panel =
    document.querySelector("div[data-testid='chat-info']") ||
    document.querySelector("[data-testid='contact-info-drawer']") ||
    document.querySelector("div[role='dialog']");
  if (!panel) return { found: false };
  const pick = (selector) => {
    const el = panel.querySelector(selector);
    return ((el && el.textContent) || '').replace(/\\s+/g, ' ').trim();
  };
  return {
    found: true,
    about: pick("[data-testid='about-entry'], .about-text, span[title]"),
    presence: pick("[data-testid='presence'], .presence, [data-testid='sub-header']"),
    phone: pick("[data-testid='cell'] , span[dir='ltr']"),
    name: pick("[data-testid='chat-info-header-title'], header span[title]")
  };
}
"""

#: The linked-devices list.
_JS_DEVICES = """
() => {
  const body = document.body;
  const text = (body.innerText || '');
  const rows = [];
  for (const el of body.querySelectorAll("[data-testid='device-row'], div[role='listitem']")) {
    const line = (el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (line.length > 3 && line.length < 160) rows.push({ text: line.slice(0, 120) });
    if (rows.length >= 12) break;
  }
  return { found: rows.length > 0 || /linked device/i.test(text), rows: rows };
}
"""

#: A one-line description of what this build's DOM actually offers. The engine
#: behind ``agent/whatsapp_probe.py``: it answers "which selector is live" without
#: anyone having to guess, which is the only way the candidate lists above stay
#: honest as WhatsApp ships changes.
_JS_PROBE = """
(names) => {
  const out = {};
  for (const pair of names) {
    const key = pair[0];
    const selectors = pair[1];
    let hit = '';
    let count = 0;
    for (const selector of selectors) {
      let found = null;
      try { found = document.querySelectorAll(selector); } catch (e) { found = null; }
      if (found && found.length) { hit = selector; count = found.length; break; }
    }
    out[key] = { hit: hit, count: count };
  }
  const menus = [];
  for (const el of document.querySelectorAll('[data-icon]')) {
    const icon = el.getAttribute('data-icon');
    if (icon && menus.indexOf(icon) === -1) menus.push(icon);
  }
  return { targets: out, icons: menus.slice(0, 120) };
}
"""

#: Targets that only exist once a conversation is open or a menu is showing.
#:
#: Probing the home screen reported eleven of these as missing, which read as
#: "half the selectors are stale" and would have sent somebody to rewrite working
#: selectors. They are not stale: `#main`'s composer, the chat header, the send
#: button and the message bubbles are not in the DOM at all until a chat is open.
#:
#: So the probe opens the first chat and probes *again*, and reports the second
#: reading -- the one that matches the state the operations actually run in. This
#: is the same rule the module applies everywhere else: read the thing where the
#: thing exists.
_CONTEXTUAL: frozenset[str] = frozenset(
    {
        "chat_header",
        "conversation",
        "composer",
        "send_button",
        "attach_button",
        "chat_info",
        "message_in",
        "message_out",
        "message_menu",
        "menu_surface",
        "row_menu",
        "more_menu",
    }
)

#: Mark the first chat in the list and return its title. Marking only -- the click
#: is a real Playwright click, for the reason in `whatsapp._JS_MARK_CHAT_ROW`.
#:
#: Returns the *title*, not a boolean. It returned `true` before, and the report
#: then printed `Chat: True`, which reads as a chat named "True" and tells nobody
#: which conversation was measured.
_JS_MARK_FIRST_CHAT = """
() => {
  const root = document.querySelector('#pane-side');
  if (!root) return '';
  const rows = Array.from(
    root.querySelectorAll('[role="row"], [role="listitem"], [data-id]')
  );
  if (!rows.length) return '';
  // Prefer a *contact* chat.
  //
  // The first row is very often WhatsApp's own official account, and that chat
  // has no message box at all -- it is a notice, not a conversation. Probing it
  // reports the composer and the send button as missing, which reads as two stale
  // selectors when in fact it is the one chat where they cannot exist. So a row
  // with a `@c.us` id is preferred, and an unread badge beats neither.
  const score = (row) => {
    const id = row.getAttribute('data-id') || '';
    if (id.includes('@c.us')) return 3;
    if (id.includes('@g.us')) return 2;
    const title = row.querySelector('span[title], div[title]');
    const name = ((title && title.getAttribute('title')) || '').trim();
    if (/^whatsapp$/i.test(name)) return 0;   // the official account
    return 1;
  };
  let best = rows[0];
  let bestScore = score(best);
  for (const row of rows) {
    const value = score(row);
    if (value > bestScore) { best = row; bestScore = value; }
    if (bestScore === 3) break;
  }
  const title = best.querySelector('span[title], div[title]');
  const name = ((title && (title.getAttribute('title') || title.textContent)) || '').trim();
  best.setAttribute('data-lumine-wa-row', '1');
  best.scrollIntoView({ block: 'center' });
  return name;
}
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------



def _probe_targets() -> list[list[Any]]:
    return [[name, list(selectors)] for name, selectors in TARGETS.items()]


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _poll(read, *, attempts: int, delay: float, predicate=None) -> Any:
    """Poll `read` until it returns something acceptable, or give up.

    WhatsApp answers in two hops: the click lands immediately and the DOM catches
    up a frame or two later. Polling rather than sleeping a fixed guess is both
    faster in the common case (returns the first time it is true) and slower only
    when the thing genuinely is not going to happen.

    ``predicate`` exists because "truthy" is not always the question being asked.
    Reading WhatsApp's search results wants the whole answer, not just its rows:
    an **unfiltered** chat list is non-empty too, so `if value:` accepted the very
    thing the caller was trying to rule out. Every other caller keeps the original
    meaning, because the default *is* truthiness.
    """
    for attempt in range(max(1, attempts)):
        value = await read()
        if (predicate(value) if predicate is not None else value):
            return value
        if attempt + 1 < attempts:
            await _sleep(delay)
    return None


#: How long to wait for WhatsApp to finish loading before deciding anything, and
#: how often to look while waiting.
#:
#: This exists because of a bug that looked exactly like a wrong diagnosis. The
#: state check read the page **once**, immediately after `domcontentloaded`, and
#: called anything without a chat list "signed out". WhatsApp Web is a
#: single-page application: between the document loading and the chat list being
#: in the DOM there are several seconds of splash screen. So a fully signed-in
#: account was reported as signed out, every target was reported missing, and the
#: tool refused to do anything -- while the same profile, open in another tab, was
#: sitting there logged in.
#:
#: So a belief about sign-in state now *waits for evidence*, which is the same
#: rule this module already applies to the chat-list filter ("a result is believed
#: only once the list stops looking like the pre-search baseline"). Three answers,
#: not two, because "still loading" and "signed out" are different problems and
#: telling a signed-in person they are signed out sends them off to re-scan a
#: QR code that is not the problem.
#: Ninety seconds, because the first load of a conversation is genuinely slow.
#:
#: A warm tab is ready in about a second. A *cold* one -- the first message of a
#: session, or a profile whose Chromium has just been closed -- re-syncs the whole
#: account, and measured against a real account that took longer than thirty
#: seconds. Thirty was chosen by feel and produced a "WhatsApp is still starting"
#: refusal on a tab that was about to work.
#:
#: This is paid once per session rather than per call, and it is the difference
#: between the first message working and the first message not.
READY_TIMEOUT_SECONDS = 90.0
READY_INTERVAL_SECONDS = 0.5

#: The readiness read, in three states rather than two. ``side`` and ``qr`` are
#: reported separately so "loading" is distinguishable from "signed out": a chat
#: list means signed in, a QR code means signed out, and *neither* means the page
#: has not got there yet.
_JS_READY = """
() => {
  const qr = document.querySelector('[data-testid="qrcode"], canvas[aria-label*="scan" i]');
  const side = document.querySelector('#pane-side');
  const app = document.querySelector('#app');
  return { qr: !!qr, side: !!side, app: !!app };
}
"""


async def _readiness(page: Any) -> str:
    """``"ready"``, ``"logged_out"``, or ``"loading"`` -- one look, no waiting."""
    try:
        data = await page.evaluate(_JS_READY)
    except Exception:  # noqa: BLE001 - a page mid-navigation is loading, not signed out
        return "loading"
    data = data or {}
    if data.get("side") and not data.get("qr"):
        return "ready"
    if data.get("qr"):
        return "logged_out"
    return "loading"


async def state(*, wait: float = READY_TIMEOUT_SECONDS) -> str:
    """``"ready"``, ``"logged_out"``, or ``"loading"``, having actually looked.

    Polls until the page has an opinion. The first look is worthless on a cold
    tab, and treating the worthless first look as the answer is what made a signed
    -in account look signed out. Returns ``"loading"`` rather than guessing once
    the wait is spent, so a caller can say "WhatsApp is still starting" instead
    of sending somebody to re-scan a code that was never the problem.
    """
    try:
        page = await whatsapp_page()
    except ToolError as exc:
        if "WhatsApp" in str(exc):
            raise ToolError(MSG_NOT_READY) from None
        raise
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        verdict = await _readiness(page)
        if verdict != "loading" or time.monotonic() >= deadline:
            return verdict
        await _sleep(READY_INTERVAL_SECONDS)


async def require_ready() -> None:
    """Refuse before anything is typed, never after.

    Waits for the page to finish loading first, because refusing a *cold* tab is
    indistinguishable from refusing a *broken* one -- and the tab is always cold
    on the first message of a conversation.
    """
    token = await state()
    if token == "loading":
        raise ToolError(
            "WhatsApp is still starting up in my browser window -- give it a "
            "moment and ask me again. Nothing was sent."
        )
    if token != "ready":
        raise ToolError(MSG_NOT_READY)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def read_chat_list(
    *, cap: int = 25, unread_only: bool = False
) -> dict[str, Any]:
    """The chat list plus what it could not tell us.

    Returns a mapping rather than a list because two of the honest answers are not
    lists:

    - ``partial``: WhatsApp **virtualises** the chat list, so only the rows in view
      exist in the DOM. A `cap` of 8 against four rendered rows returns four, and
      saying so is the difference between "these are your recent chats" and a list
      that looks complete and is not.
    - ``unreadReadable``: whether any row exposed an unread badge at all. When no
      row does, ``unread_only`` returning nothing means "I could not tell", not
      "you have nothing unread" -- and those are very different answers to a user
      asking which messages they have not replied to.
    """
    await require_ready()
    page = await whatsapp_page()
    data = await page.evaluate(_JS_READ_CHATS, {"cap": int(cap)})
    rows = list((data or {}).get("rows") or [])
    rendered = int((data or {}).get("rendered") or len(rows))
    unread_seen = any(row.get("hasUnreadBadge") or int(row.get("unread") or 0) > 0 for row in rows)
    if unread_only:
        rows = [row for row in rows if int(row.get("unread") or 0) > 0]
    return {
        "rows": rows,
        "rendered": rendered,
        "partial": rendered < int(cap),
        "unreadReadable": unread_seen,
    }


async def list_chats(*, cap: int = 25, unread_only: bool = False) -> list[dict[str, Any]]:
    """The chat list, in one pass."""
    return list(
        (await read_chat_list(cap=cap, unread_only=unread_only)).get("rows") or []
    )


async def read_messages(*, cap: int = 20) -> list[dict[str, Any]]:
    """The open conversation's messages, oldest first, newest last."""
    await require_ready()
    page = await whatsapp_page()
    data = await page.evaluate(_JS_READ_MESSAGES, {"cap": int(cap)})
    if not (data or {}).get("found"):
        raise ToolError(MSG_NO_HEADER)
    return list((data or {}).get("rows") or [])


#: How long the chat header is given to become readable on its own before the
#: composer is believed instead.
#:
#: Not a tuning constant so much as a statement about what a page does: a press
#: changes the DOM over a few frames, and in that window the *previous*
#: conversation is still fully present. Reading "a composer exists" during those
#: frames says nothing about whether the right conversation opened.
#:
#: Long enough to cover the swap (a new chat's header renders as soon as its
#: conversation mounts), short enough that the never-messaged-contact case -- which
#: will never have a header -- still returns quickly rather than sitting out the
#: full timeout.
_HEADER_GRACE_SECONDS = 1.2


async def _composer_is_ready() -> bool:
    """Whether there is a message box to type into. Never raises.

    Read through the same script the draft step uses, so "somewhere safe to type"
    and "where the text will actually go" cannot be two different opinions. Kept
    separate from :func:`composer` because this one answers a yes/no and must not
    fail the open step by raising: a script that changed shape is exactly the
    situation in which the answer should be *no* and the caller should fall back
    to waiting, not an exception that aborts a send.
    """
    try:
        page = await whatsapp_page()
        data = await page.evaluate(_JS_COMPOSER) or {}
    except Exception:  # noqa: BLE001 - an unreadable page is "not ready", not a crash
        return False
    return bool((data or {}).get("found"))


async def active_chat() -> str:
    """The chat WhatsApp is actually showing. ``""`` when none is open.

    The contact's **name**, with the phone number WhatsApp puts beside it stripped
    off -- see :data:`_JS_CONTACT_NAME_FN`.
    """
    title, _raw = await active_chat_names()
    return title


async def active_chat_names() -> tuple[str, str]:
    """``(name, as displayed)`` for the open chat. Both empty when none is open.

    Two forms because they legitimately differ: a contact who has never been
    messaged shows as `'Jubayed Reza (22-23)'` while being *named* 'Jubayed
    Reza'. :func:`open_chat` accepts either, so a chat genuinely called
    'Team (2024)' still matches on the displayed form without the strip rule
    having to exist in Python as well.
    """
    page = await whatsapp_page()
    data = await page.evaluate(_JS_ACTIVE_CHAT) or {}
    return (
        str(data.get("title") or "").strip(),
        str(data.get("raw") or "").strip(),
    )


def number_from_jid(jid: object) -> str:
    """The dialable number inside a chat's JID, or ``""``.

    WhatsApp's own identity for a one-to-one chat is the number, suffixed --
    ``15551234567@c.us`` -- so a name the account already knows can be turned into
    a number without anybody reading out a phone number aloud.

    A group is ``@g.us`` and has no number at all, and a contact saved on another
    device is suffixed ``:12`` rather than being a different person. Both return
    ``""``: a deeplink built from a guessed number opens a stranger's chat, and
    "I couldn't find a number" costs the user one sentence while a wrong one costs
    them a message sent to somebody who never agreed to receive it.
    """
    text = str(jid or "").strip()
    if "@" not in text:
        return ""
    number, _, domain = text.partition("@")
    if not domain.startswith("c.us"):
        return ""
    # A per-device suffix is not part of the number.
    number = number.split(":", 1)[0]
    return number if PHONE_RE.fullmatch(number) else ""


async def search_chats(query: str) -> list[dict[str, Any]]:
    """Chats matching `query`, through WhatsApp's own search box.

    ## It waits for the page before it types

    This used to go straight to the search box. `whatsapp_page()` returns a tab
    whether or not WhatsApp has finished *loading in it*, and the search box is the
    last thing WhatsApp renders -- so on a tab still coming up, `_focus` finds
    nothing, or finds an input the single-page application is about to replace, and
    the keystrokes go into a control that no longer exists. The filter never
    applies, WhatsApp matches nothing, and the report is "I couldn't read your
    chat list -- nothing matched" for a contact that is right there.

    Retried, it works. Which is the whole of the reported symptom: *it takes
    several attempts to find the contact*, and the first attempt or two were being
    spent typing into a page that was not ready. `open_chat` and `require_ready`
    already waited; the search did not, and the search is what a send does first.

    Four rules, all learned from the Desktop adapter and all still true here:

    1. **Search, then read.** The rendered list is the last few dozen
       conversations; the search box reaches the account's whole history.
    2. **The filter must arrive.** WhatsApp's own results are grouped under
       section headers that never exist in the unfiltered list, so their presence
       is the signal. A read that landed before the filter applied returns
       whichever chat sorts first, which is how "message Afia" becomes "message
       whoever wrote last".
    3. **The box is cleared on the way out**, in a ``finally``. A filter left
       behind narrows every later lookup for the rest of the session, and a tool
       that only works once is a tool that lies on the second call.

    ## The box is also cleared on the way *in*

    Clearing afterwards is not enough, and the gap was found by measurement rather
    than reasoning. The box arrives dirty in ways the ``finally`` cannot cover: a
    person typed into it, or a previous search was interrupted and never reached
    its own cleanup. Then this search *appends*, the box holds
    ``"Jubayed" + "Jubayat"``, WhatsApp matches nothing, and the tool reports a
    contact absent while the earlier result is still on screen behind it.

    So it is emptied and **verified empty** before the query goes in. Verified,
    because the alternative -- assuming the keystrokes worked -- is the same class
    of assumption that made an empty composer read as holding a draft.

    ## This returns what WhatsApp rendered; it does not decide what matches

    It used to drop any row whose title did not contain the query verbatim. That
    is a spelling test, and speech is not spelled: the account holds
    'Jubayed Reza', the request said 'Jubayat', and one letter was enough to
    report a contact who was on the screen as absent. Matching is
    `whatsapp_flow.match`'s job -- it ranks tiers and asks when two rows tie -- and
    a pre-filter here would have quietly removed the rows that fuzzy matching
    exists to rescue.
    """
    page = await whatsapp_page()
    await require_ready()
    baseline = frozenset(
        str(row.get("title") or "").casefold() for row in await list_chats(cap=40)
    )
    try:
        box = await _focus(page, "search_box")
        if box is None:
            raise _missing("search box")
        if not await _empty_search_box(page):
            raise _missing("search box")
        await page.keyboard.type(str(query or ""))
        # Real keys, not a value write: WhatsApp's search is driven by its own key
        # handling, and a scripted value types into the DOM without ever filtering
        # the list.
        answer = await _poll(
            lambda: _filtered_rows(page, query),
            attempts=8,
            delay=0.35,
            predicate=lambda answer: _filter_has_landed(answer, baseline),
        )
        if not answer:
            return []
        # `_poll` returns what `read` returned, and that is now the *pair* -- the
        # "did the filter land" flag travels with the rows so the predicate could
        # see it. Unwrap here. Returning the pair would hand every caller a tuple
        # where a list of chats belongs, and `[row.title for row in result]` would
        # fail on the boolean.
        _applied, rows = answer
        return rows
    finally:
        try:
            await page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001 - best effort; nothing depends on it
            pass


def _filter_has_landed(
    answer: tuple[bool, list[dict[str, Any]]], baseline: frozenset[str]
) -> bool:
    """Whether WhatsApp's results are actually on screen.

    **Both** signals, and the second one is what makes the first trustworthy.

    - The section header is the structural evidence that a filter is in. It is not
      sufficient alone: while the box is being *cleared*, WhatsApp still has those
      headers up over the unfiltered list for a frame or two, so ``applied`` can be
      true before a single character of the query has been matched. Measured: a
      deliberately dirtied box produced seven baseline chats reported as the
      results for "Jubayat".
    - The rows differing from the pre-search baseline is what says a filter
      actually *narrowed* something. This alone was once untrustworthy, because the
      result read was capped at 12 while the baseline was 40 -- twelve rows of an
      unchanged forty is a strict subset, so an unfiltered list passed. Both reads
      are capped at 40 now, which is what makes the comparison mean something.

    Together they cannot be fooled by the ordinary cases: an unchanged list has no
    headers once the reset finishes, and a real result set is never the whole chat
    history.
    """
    applied, rows = answer
    if not (applied and rows):
        return False
    titles = frozenset(str(row.get("title") or "").casefold() for row in rows)
    return bool(titles) and titles != baseline


#: Select-all, then delete. WhatsApp's search is a plain text input, so this is the
#: ordinary gesture rather than anything WhatsApp-specific.
_JS_SEARCH_BOX_EMPTY = """
() => {
  const input = document.querySelector('#side input');
  if (!input) return false;
  const now = input.value || '';
  if (now) {
    input.focus();
    return false;
  }
  return true;
}
"""


#: What the search box currently holds, or ``""``. Never raises.
#:
#: Compared casefolded and whitespace-collapsed against the wanted name, because
#: WhatsApp normalises what it is given and this is asking the same question the
#: row marker asks. One definition of "the same name" is the only way two readers
#: cannot disagree about it.
_JS_SEARCH_BOX_VALUE = """
() => {
  const input = document.querySelector('#side input');
  return (input && input.value) || '';
}
"""


async def _search_box_holds(page: Any, wanted: str) -> bool:
    """Whether the search box already contains exactly ``wanted``."""
    try:
        current = await page.evaluate(_JS_SEARCH_BOX_VALUE)
    except Exception:  # noqa: BLE001 - an unreadable box is "do not assume"
        return False
    return " ".join(str(current or "").split()).casefold() == " ".join(
        str(wanted or "").split()
    ).casefold()


#: Spoken once after a send is *verified*, by the product rather than by the model.
#:
#: This line existed as a progress step and was removed, and that was wrong -- the
#: user wants a spoken confirmation, not a silent one. What was actually wrong was
#: that it was the *model* saying it, twice, from two facts:
#:
#:   1. the step line "Sending to X" reaching the model, and
#:   2. the return value "Sent to X on WhatsApp."
#:
#: Two facts about one event, and a model asked to report on a turn will say both.
#: So the fix is not "one fact" -- it is **one speaker**. The confirmation is now
#: spoken here, from code, the moment delivery is observed rather than reported, and
#: the tool result then tells the model it has been said, so its own reply adds
#: nothing. The step list still ends at the approval question, because the answer to
#: that question is the send.
#:
#: The channel is reported rather than assumed. `voice_line.speak_line` answers
#: `SAID` when a synthesiser carried the line, `GENERATED` when the realtime model
#: spoke for itself, and `SILENT` when the profile can do neither -- which is the
#: case on a native-audio profile such as `gemini-3.8-live`, where `say()` raises
#: and the model's own sentence is the only voice there is. Saying so in the result
#: is what stops the model talking over a confirmation the product already gave.
async def announce_sent(context: Any, who: str) -> str:
    """Speak the send confirmation once. Never raises; answers the channel used."""
    try:
        from ...runtime.progress import _session_of as session_of
        from ...runtime.voice_line import SILENT, speak_line
    except ImportError:  # running as a top-level script with agent/ on sys.path
        try:
            from runtime.progress import _session_of as session_of
            from runtime.voice_line import SILENT, speak_line
        except ImportError:
            return "silent"
    session = session_of(context)
    if session is None:
        return "silent"
    line = f"The message has been sent to {who}."
    try:
        # `add_to_chat_ctx` is not a `say()` argument -- this is a synthetic
        # conversation item so the model does not read its own confirmation back
        # as content to discuss, which is the other half of saying it once.
        channel = await speak_line(session, line, allow_interruptions=True)
    except Exception:  # noqa: BLE001 - a confirmation that cannot be spoken is not a failure
        logger.info("[whatsapp] could not speak the send confirmation", exc_info=True)
        return SILENT
    logger.info("[whatsapp] send confirmation spoken on channel %s", channel)
    return channel


async def _empty_search_box(page: Any) -> bool:
    """Empty the search box, and confirm it is empty. ``True`` when it is.

    Three rounds rather than one, because this runs on a page the agent does not
    own: the first pass finds what is there, one keystroke gesture clears it, and
    the last pass is the only one that counts. A helper that reported success
    without re-reading is how a "cleared" box that still held a filter survived
    long enough to make four searches in a row find nothing.
    """
    for _ in range(3):
        if await page.evaluate(_JS_SEARCH_BOX_EMPTY):
            return True
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
        except Exception:  # noqa: BLE001 - try the next round
            continue
        await _sleep(0.1)
    return bool(await page.evaluate(_JS_SEARCH_BOX_EMPTY))


async def _filtered_rows(page: Any, query: str) -> tuple[bool, list[dict[str, Any]]]:
    data = await page.evaluate(_JS_FILTERED_ROWS, query)
    return bool((data or {}).get("applied")), list((data or {}).get("rows") or [])


#: Mark exactly one element, and only that one, for a Playwright press.
#:
#: **Why the clearing is there and not optional.** The marker used to be set and
#: never removed, so the attribute accumulated. `page.click("[data-lumine-wa-
#: focus='1']")` resolves through `querySelector`, which returns the *first* match
#: in document order -- so after the search box had been focused once (during the
#: name lookup), marking the composer left **two** elements carrying the attribute
#: and every subsequent press landed on the search box, which sits above it.
#:
#: Measured consequence: `composer()` read the box successfully and `type_message`
#: then refused with "WhatsApp's layout has changed and I couldn't find the message
#: box" -- on every chat, every time, 200 ms in. The chat list was a red herring;
#: the press was going to the wrong element the whole time.
#:
#: A unique value per call would work too, but clearing first needs no state and no
#: counter, so it cannot go stale across a page navigation or a code path that
## forgets.
_JS_MARK_ONE = """
(spec) => {
  const { selector, mark } = spec;
  for (const old of document.querySelectorAll('[' + mark + ']')) {
    old.removeAttribute(mark);
  }
  const el = document.querySelector(selector);
  if (!el) return false;
  el.setAttribute(mark, '1');
  el.scrollIntoView({ block: 'center' });
  return true;
}
"""


async def _focus(page: Any, target: str) -> Any:
    """Focus the first live selector for `target`. ``None`` when none is live.

    **Two ways to land focus, and one way to know it worked.**

    A real Playwright press is tried first, because `element.focus()` alone runs
    the page's focus handler and dispatches no real input -- no caret, nothing
    dismissed, no editing mode -- and keystrokes then go to an element that exists
    but is not being edited.

    But measured against the live account, the press **is not always possible**: the
    composer sits behind an overlay that Playwright's actionability check -- visible,
    stable, *receives events*, enabled -- refuses, and it times out rather than
    clicking something else. A layout-change refusal for a focus problem is the
    wrong sentence as well as the wrong outcome, so `el.focus()` is the fallback.

    The confirmation is what decides, not the method. A `contenteditable` can absorb
    a press and still not be the active element, and typing into that sends the
    message to the search box, a menu, or the page body.

    Only **one** element is ever marked: see :data:`_JS_MARK_ONE`, because a
    leftover marker silently redirects the press to whatever was focused before.
    """
    for selector in TARGETS.get(target, ()):
        try:
            marked = await page.evaluate(
                _JS_MARK_ONE, {"selector": selector, "mark": _FOCUS_MARK}
            )
        except Exception:  # noqa: BLE001 - try the next spelling
            continue
        if not marked:
            continue

        pressed = False
        try:
            await page.click(f"[{_FOCUS_MARK}='1']", timeout=2500)
            pressed = True
        except Exception:  # noqa: BLE001 - the overlay case, handled below
            logger.info(
                "[whatsapp] %s could not be pressed; falling back to focus()", target
            )

        if not pressed:
            try:
                await page.evaluate(
                    "(selector) => { const el = document.querySelector(selector); "
                    "if (!el) return false; el.focus(); return true; }",
                    selector,
                )
            except Exception:  # noqa: BLE001 - try the next spelling
                continue

        # The check that decides, whichever route was taken.
        active = False
        try:
            active = bool(
                await page.evaluate(
                    "(selector) => { const el = document.querySelector(selector); "
                    "if (!el) return false; "
                    "return el === document.activeElement "
                    "  || !!el.querySelector(':focus'); }",
                    selector,
                )
            )
        except Exception:  # noqa: BLE001
            active = False
        if active:
            return selector
    return None


async def _click_target(page: Any, target: str) -> bool:
    """Click a target through the real input pipeline. ``True`` when it landed.

    One element marked at a time, for the reason in :data:`_JS_MARK_ONE` -- a
    leftover `data-lumine-wa-click` from an earlier press would send this one
    somewhere else in the page entirely.
    """
    for selector in TARGETS.get(target, ()):
        try:
            marked = await page.evaluate(
                _JS_MARK_ONE, {"selector": selector, "mark": _CLICK_MARK}
            )
        except Exception:  # noqa: BLE001 - try the next spelling
            continue
        if not marked:
            continue
        try:
            await page.click(f"[{_CLICK_MARK}='1']", timeout=4000)
        except Exception:  # noqa: BLE001 - try the next spelling
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Chat navigation
# ---------------------------------------------------------------------------


#: Mark the chat row titled exactly `wanted`, and report whether one was found.
#:
#: It only *marks*. Same reason as `_JS_MARK_CHAT_ROW`: an untrusted `el.click()`
#: inside the page does not open WhatsApp's chat list, and the hover event above
#: it does not survive either. Playwright performs the real click afterwards, in
#: `_click_row`.
#:
#: Exact match, never a prefix: "Afia" must not open "Afia Rahman" by accident,
#: and the caller has already resolved which chat it means.
#: Wrapped in `_with_chat_row` rather than prefixed with the helper directly.
#:
#: That is not tidiness, it is the bug this module already documents once:
#: Playwright evaluates a string argument as a **single expression**, so
#: `function f(){...}` immediately followed by `(x) => {...}` parses as
#: `f(){...}(x) => {...}` and the page answers `SyntaxError`. Concatenating the
#: helper in front of the arrow function produced exactly that -- and every
#: `_JS_MARK_ROW` call failed to parse, so `_click_row` found nothing and
#: `open_chat` refused on chats that opened perfectly well by hand.
#:
#: `_with_chat_row` wraps the declarations *inside* the arrow function's body,
#: which is the only spelling that parses. It also means
#: :data:`_JS_CONTACT_NAME_FN` is declared exactly once, in
#: :data:`_JS_CHAT_ROW_FN`, and both readers reach the same definition.
_JS_MARK_ROW = _with_chat_row("wanted", """
  const root = document.querySelector('#pane-side');
  if (!root) return false;
  // Clear first: this marker is also set by the *send* path's own row marker, and
  // two marked rows means the press lands on whichever is first in the document
  // rather than the one that was just chosen.
  for (const old of document.querySelectorAll('[data-lumine-wa-row]')) {
    old.removeAttribute('data-lumine-wa-row');
  }
  for (const el of root.querySelectorAll(
    '[data-testid^="list-item"], [data-id*="@c.us"], [data-id*="@g.us"], ' +
    '[role="listitem"], [role="row"]'
  )) {
    const titleNode = el.querySelector('[data-testid="cell-frame-title"]')
      || el.querySelector('span[title]') || el.querySelector('div[title]');
    const value = ((titleNode &&
      (titleNode.getAttribute('title') || titleNode.textContent)) || '')
      .trim().toLowerCase();
    // Both spellings are accepted. The raw cell carries the phone number on a
    // contact row and the name alone on a chat row, and the caller only ever has
    // the name -- so comparing against the raw text meant a saved contact could be
    // resolved and then never clicked, and `open_chat` reported "nothing matched"
    // for a contact the search had just found on screen.
    if (value !== wanted && lumineContactName(value) !== wanted) continue;
    el.setAttribute('data-lumine-wa-row', '1');
    el.scrollIntoView({ block: 'center' });
    return true;
  }
  return false;
}
""")

_ROW_MARK = "data-lumine-wa-row"

#: The selector that presses a marked row. Derived once, here, rather than written
#: out in each caller -- a second spelling would be a press aimed at something the
#: marker never set, which fails as "could not click" rather than as the mismatch
#: it is.
_ROW_SELECTOR = f"[{_ROW_MARK}='1']"


async def _click_row(page: Any, wanted: str) -> bool:
    """Click the chat row whose title is exactly `wanted`, through a real click.

    The mark-then-click split is load-bearing and is the same one
    `_JS_MARK_CHAT_ROW` exists for on the send path. An in-page `el.click()`
    reports success and opens nothing, which is what this function did before --
    so the open step fell through to the search box on chats that were plainly
    visible, and reported "I couldn't find that" for a contact on screen.
    """
    try:
        marked = await page.evaluate(_JS_MARK_ROW, wanted)
    except Exception:  # noqa: BLE001
        return False
    if not marked:
        return False
    try:
        # A real press. Playwright hovers first, which is what reveals the row's
        # menu trigger, and waits for the element to be actionable -- so this also
        # answers "is it actually clickable" rather than assuming it.
        await page.click(f"[{_ROW_MARK}='1']", timeout=4000)
    except Exception:  # noqa: BLE001
        logger.info("[whatsapp] the row for %r could not be clicked", wanted)
        return False
    return True


async def _search_then_click(page: Any, wanted: str, *, attempts: int = 8, delay: float = 0.35) -> bool:
    """Reach a chat that is not in the rendered list, through the search box.

    The rendered list is the last few dozen conversations and nothing more, so
    "find Afia" succeeds for a chat the open step cannot then reach. That was a
    live dead end: `whatsapp_read` resolved a name the account's own search box
    had just found, and then refused with "I couldn't find that in your
    WhatsApp" -- resolving and opening disagreed about what exists.

    So the open step searches too, and waits for the filter to arrive before
    clicking. The box is cleared in a `finally` for the reason `search_chats`
    clears it: a filter left behind narrows every later lookup, and the next
    read of the chat list would return the *filtered* rows and look like a
    plausible wrong answer.

    ## The box is emptied before the name goes in

    This is the *second* search of the same name in one send. `send_whatsapp`
    resolves the recipient with `search_chats`, which types the name, and then
    `open_chat` searches for it again -- so a contact who is not in the rendered
    list has their name typed into the search bar twice, visibly, in one turn.
    The user described watching it happen.

    Emptying first turns that into one clean pass over whatever was already
    there, so the two searches cannot concatenate into `JubayedJubayat` and find
    nothing -- which is the same append hazard `_empty_search_box` was written for
    on the resolve side.
    """
    try:
        if await _focus(page, "search_box") is None:
            return False
        # Only type when the box does not already hold the name. This is not a
        # micro-optimisation: the visible symptom was Lumine typing the same name
        # into the search bar over and over, because every retry of a send re-ran
        # the search and the keystrokes are what the user watches. When the name is
        # already there -- an interrupted search, or the filter the resolve step
        # left behind -- retyping is not "being sure", it is the third search.
        if not await _search_box_holds(page, wanted):
            if not await _empty_search_box(page):
                return False
            # Real keys for the same reason `search_chats` uses them: WhatsApp's
            # search is driven by its own key handling, and a scripted value types
            # into the DOM without ever filtering the list.
            await page.keyboard.type(wanted)
        return bool(
            await _poll(
                lambda: _click_row(page, wanted),
                attempts=attempts,
                delay=delay,
            )
        )
    except Exception:  # noqa: BLE001 - a failed search is a refusal, never a crash
        return False
    finally:
        try:
            await page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001 - best effort; nothing depends on it
            pass


async def open_chat(title: str, *, timeout: float = 4.0) -> bool:
    """Open a chat by exact title, and confirm the header agrees.

    Confirmation is the point. "I clicked the row" is not "the right chat is
    open", and the second is the one that has to gate a send: the row may have
    moved between the search and the click, a reconnect may have landed
    elsewhere, and a click on the wrong row means the message goes to somebody
    who never agreed to receive it.

    Two ways in, in order. The rendered list first, because it is one round
    trip and covers the chats somebody is actually talking about. The search box
    second, for a chat old enough not to be rendered -- without it, every read
    and every send of a chat outside the visible window fails while the very
    same name resolves a moment earlier through the search.

    ## Refuse on a *mismatch*, proceed on *unreadable*

    Two situations produce no confirmation, and only one of them is a failure:

    - **The header names a different chat.** A real mismatch. Refuse: something
      moved and we would be typing into the wrong conversation, in front of
      somebody who never agreed to receive it.
    - **The header cannot be read at all** -- a selector WhatsApp renamed, a chat
      that has not rendered yet. The press landed; WhatsApp opened *something*.
      Refusing here turns a WhatsApp layout change into "Lumine cannot pick
      contacts", which is a capability the user needs and which no layout change
      justifies removing.

    Proceeding is safe *because of what happens next*, not on its own account:
    nothing is typed until the composer has been read, nothing is sent until the
    user has approved a draft they can read on their own screen, and the
    composer's own check is the next line of defence. Typing into a composer that
    is not the intended chat is what would actually misfire.

    This rule was written for the send path and lost when the two copies of this
    function were merged -- the Web copy simply returned ``False`` whenever the
    title did not match within the window, which made an unreadable header
    indistinguishable from a wrong chat. Both copies being "the open step" was
    itself the bug: the send tool ran the one with the rule, every read ran the
    one without it, and they answered differently about the same page.

    ## An unreadable header no longer costs the whole window

    A contact who has **never been messaged** has no conversation header to read,
    so the loop used to run to the deadline -- four seconds of silence -- before
    proceeding on the rule above. The composer is polled in the same breath, and it
    is a *positive* signal rather than an absence: a message box is somewhere safe
    to type, which is exactly what the rule was leaning on when it proceeded.

    ## A mismatch is only evidence once the page has settled

    This is the bug the user finally localised for us, and it only shows when
    **another chat is already open**. Ask for a contact while a conversation is on
    screen and the sequence is: press B's row, read the header, and the header
    still says **A** -- because the previous conversation has not been swapped out
    yet. A mismatch read in that instant is not evidence of the wrong chat, it is
    evidence that we have not looked yet. Refusing on it throws away our own
    successful click.

    The report was exact: *"she searched it, then opened it, saying she can't open
    that; then when I say to do it again, when the chat is already opened, she does
    it perfectly."* The second attempt works because by then the page has settled
    on the right chat. So the first attempt was never wrong about the chat -- it
    was wrong about the timing.

    Hence :data:`_HEADER_GRACE_SECONDS` gates the **refusal** as well as the
    composer shortcut. A header that is readable, mismatched, and has had time to
    become a different chat is still refused immediately. What changes is only that
    a mismatch is no longer treated as final while the swap is still in flight.
    """
    await require_ready()
    page = await whatsapp_page()
    wanted = str(title or "").strip().casefold()
    if not wanted:
        return False

    if not await _click_row(page, wanted) and not await _search_then_click(page, wanted):
        return False

    deadline = time.monotonic() + max(0.5, timeout)
    started = time.monotonic()
    saw_a_header = False
    while time.monotonic() < deadline:
        current, shown = await active_chat_names()
        current, shown = current.casefold(), shown.casefold()
        if current or shown:
            saw_a_header = True
            # Either spelling is this chat. `shown` is what WhatsApp renders and
            # `current` is the same thing with a contact's phone number removed --
            # so a chat named 'Team (2024)' matches on the first and a contact
            # shown as 'Jubayed Reza (22-23)' matches on the second. Requiring both
            # would refuse one of them; requiring the stripped form only would
            # refuse the other.
            if current == wanted or shown == wanted:
                return True
            # A different chat -- but the *previous* one, until the swap lands.
            # Refusing here would be refusing our own successful click, which is
            # what the user saw: ask for a second contact while one is open, and
            # she opens it and then reports that she could not.
            if time.monotonic() - started >= _HEADER_GRACE_SECONDS:
                return False
        # No header at all. A composer is the positive evidence that somewhere safe
        # to type now exists, which is the assumption the rule below proceeds on --
        # so waiting out the whole window for a header this chat will never have
        # is four seconds of silence for nothing.
        #
        # **But only after a settle period**, and that word is doing real work. The
        # instant after the press, the *previous* conversation's composer is still
        # on screen. Accepting it immediately reports "opened" while the send is
        # about to type into the wrong chat -- which is the one mistake this whole
        # function exists to prevent, introduced by the optimisation meant to speed
        # it up. Measured as the user saw it: the first attempt after a different
        # chat reported failure or success inconsistently, and the second attempt
        # worked, because by then the DOM had settled.
        #
        # So the header gets a short grace period to become readable on its own --
        # which is the better answer when it does -- and the composer is only
        # believed once the page has had time to swap.
        if time.monotonic() - started >= _HEADER_GRACE_SECONDS and await _composer_is_ready():
            logger.info(
                "[whatsapp] no chat header for %r after %.1fs, but the composer is "
                "there; proceeding on the press",
                title,
                _HEADER_GRACE_SECONDS,
            )
            return True
        await _sleep(0.15)
    if saw_a_header:
        # It settled on something, and it was not the chat we asked for.
        return False
    # Never readable. The press landed; let the composer's own read decide whether
    # there is somewhere safe to type.
    logger.info(
        "[whatsapp] could not read the chat header for %r; proceeding on the press "
        "and relying on the composer check",
        title,
    )
    return True


# ---------------------------------------------------------------------------
# The composer
# ---------------------------------------------------------------------------


async def composer() -> dict[str, Any]:
    """The composer, its text, and whether a send button is live."""
    page = await whatsapp_page()
    return dict(await page.evaluate(_JS_COMPOSER) or {})


async def type_message(text: str) -> None:
    """Put exactly `text` in the message box -- or leave the box clean.

    Four ways out, and one that a retry cannot survive. No composer: nothing typed.
    A draft already there: nothing typed, and the user's words untouched -- typing
    over one would send words nobody approved. Typed but the box does not hold it:
    the box is **cleared again** and the refusal says so, because the alternative
    was asking the user to approve a draft that was never fully there. Typed, and
    the clear failed: :class:`DirtyText`, which the send sequence holds exactly
    like an unconfirmed send, because from the user's side that is what it is.

    The verification is the point of this function. "I pressed the keys" is not
    "the text is in WhatsApp", and for a React-controlled ``contenteditable`` the
    gap between them is where a silent truncation used to live.

    It also waits for the page, because this is a step that *acts*. The composer
    read below is a fair question to ask of a page that is up and a useless one to
    ask of a splash screen: WhatsApp renders the search box and the composer last,
    so a tab still mounting answers "no composer" and the send refuses with "I
    can't see the message box" for a chat that has a message box. Waiting here is
    the difference between one slow send and a retry the user has to ask for.
    """
    await require_ready()
    state_ = await composer()
    if not state_.get("found"):
        raise ToolError(MSG_NO_COMPOSER)
    if not state_.get("empty", True):
        raise ToolError(
            "There's already something written in WhatsApp's message box, so I "
            "didn't add to it. Send or clear that first, then ask me again. "
            "Nothing was sent."
        )
    page = await whatsapp_page()
    box = await _focus(page, "composer")
    if box is None:
        raise _missing("message box")
    try:
        await page.keyboard.type(str(text), delay=KEY_DELAY_MS)
    except Exception:
        if await _clear_composer():
            raise
        raise desktop.DirtyText(MSG_DIRTY) from None
    # Verify the box now holds the text, and clear it if it does not.
    #
    # Without this the sequence went on to ask for approval for a draft that was
    # never typed -- so the user was shown "send I love you to Afia?", agreed, and
    # watched nothing happen. The refusal names the real problem instead: the text
    # did not reach WhatsApp.
    #
    # A draft that lost its *tail* counts as a failure, not a near miss. A partial
    # draft that goes on to be sent is a message the user did not approve.
    if not await _composer_holds(str(text)):
        await _clear_composer()
        raise ToolError(MSG_NOT_TYPED)


async def _clear_composer() -> bool:
    """Empty the composer, and verify it is actually empty.

    Select-all then Backspace is the gesture a person uses, and it is enough. The
    residue that used to defeat it -- a zero-width space the caret could not sit
    after -- is invisible to the same test that decides whether the box is empty,
    so "cleared" now means the box really is empty rather than "I pressed two keys".

    Tries Backspace once more when the first pass left something behind, because a
    single stubborn character is common and a second pass is free. It does not
    loop: a box that refuses to empty is a refusal, not something to keep hitting
    with keystrokes while the user watches.
    """
    page = await whatsapp_page()
    try:
        await _focus(page, "composer")
    except Exception:  # noqa: BLE001
        return False
    for _ in range(2):
        try:
            await page.keyboard.press("ControlOrMeta+a")
            await page.keyboard.press("Backspace")
        except Exception:  # noqa: BLE001
            return False
        if (await composer()).get("empty"):
            return True
    return False


async def _composer_holds(expected: str) -> bool:
    """Whether the composer holds exactly `expected` -- the whole of it.

    Exact, and after normalising whitespace and the invisible marks WhatsApp puts
    at the edges of a contenteditable. Two reasons for the strictness:
    """
    state_ = await composer()
    if not state_.get("found"):
        return False
    seen = _comparable(str(state_.get("text") or ""))
    return seen == _comparable(expected)


#: Zero-width direction marks, a zero-width space, a word joiner, a soft hyphen and
#: a BOM -- the characters WhatsApp pads a `contenteditable` with.
#:
#: Written as real characters on purpose. An earlier version held the literal
#: two-character sequence ``\u200e`` inside a `str.replace`, which is not the mark
#: WhatsApp inserts, so it stripped nothing: a draft arrived intact and was still
#: reported as a mismatch. The same escaping mistake on the JavaScript side is why
#: an *empty* composer read as holding a draft, and that one refused every send.
_INVISIBLE = "\u200e\u200f\u200b\u2060\u00ad\ufeff"
_NBSP = "\xa0"


def _comparable(text: str) -> str:
    """Normalise a message for comparison.

    WhatsApp's composer pads a ``contenteditable`` with zero-width direction marks
    and non-breaking spaces. Those are part of the rendering rather than part of
    what the user asked to send, so a faithful draft must not be reported as a
    mismatch and cleared out from under them.

    Everything else is compared exactly, because a partial draft that goes on to be
    sent is a message nobody approved.
    """
    cleaned = text.translate({ord(c): None for c in _INVISIBLE})
    cleaned = cleaned.replace(_NBSP, " ")
    return " ".join(cleaned.split()).strip()


async def press_send() -> None:
    """Click the send control, falling back to Enter.

    Enter is WhatsApp's own send gesture and is locale-independent, so it is the
    fallback rather than the primary: a button whose icon we can find is a button
    we can also verify is live.

    Waits for the page first, like every step that *acts* rather than looks. The
    Enter fallback types into whatever currently holds focus, so on a page that is
    still mounting that is the search box -- a send gesture aimed at a search field
    is worse than no send at all, because it looks like it worked.
    """
    await require_ready()
    page = await whatsapp_page()
    if not await _click_target(page, "send_button"):
        await _focus(page, "composer")
        await page.keyboard.press("Enter")


async def delivery_seen(text: str, *, timeout: float = 4.0) -> bool:
    """Is this exact text an outgoing bubble in the conversation *now*?

    Polls for a few seconds, because the send is acknowledged a beat after the
    click. Read after the attempt and never before -- see the module docstring on
    why a stale "it's there" is the same lie in slow motion.
    """
    page = await whatsapp_page()
    wanted = str(text or "").replace("\n", " ").strip()
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        try:
            if await page.evaluate(_JS_DELIVERED, wanted):
                return True
        except Exception:  # noqa: BLE001 - a re-render mid-evaluate is a retry
            pass
        await _sleep(0.25)
    return False


async def set_typing(active: bool = True) -> bool:
    """Best-effort "typing…" indicator. A hint, so it never raises.

    Dispatching an ``input`` event from inside the page was the wrong tool, and the
    test above is what says so. It fires the page's listener and nothing else --
    WhatsApp's indicator is driven by real keystrokes reaching a focused composer,
    so the event achieved nothing while reporting that it had. The fix is one real
    character, typed and deleted, which is the same thing a person does.

    Deliberately not routed through :func:`_focus`: this is cosmetic, and it must
    not be able to steal focus from the search box or a menu on the strength of
    being a hint.
    """
    del active
    page = await whatsapp_page()
    try:
        el = await page.evaluate(
            _JS_MARK_ONE,
            {"selector": _COMPOSER_PRIMARY, "mark": _TYPING_MARK},
        )
        if not el:
            return False
        await page.click(f"[{_TYPING_MARK}='1']", timeout=2000)
        await page.keyboard.type("\u200b", delay=1)
        await page.keyboard.press("Backspace", delay=1)
        return True
    except Exception:  # noqa: BLE001 - a hint is never worth a refusal
        return False


# ---------------------------------------------------------------------------
# Message-level operations
# ---------------------------------------------------------------------------


async def message_at(index_from_end: int, *, expect: str = "") -> dict[str, Any]:
    """Re-read the message a reference points at, immediately before acting.

    This is the check that makes a position safe. The reference said "third from
    the bottom, saying *this*"; if what is actually there says something else --
    because the conversation moved since the read -- the answer is a refusal, not
    an action on whatever now occupies the slot.
    """
    page = await whatsapp_page()
    data = await page.evaluate(_JS_MESSAGE_AT, {"indexFromEnd": int(index_from_end)})
    if not (data or {}).get("ok"):
        raise ToolError(MSG_NOT_CHAT)
    text = str((data or {}).get("text") or "")
    if expect and text != str(expect).replace("\n", " ").strip():
        raise ToolError(
            "That message is not where it was when I read it -- read the chat "
            "again and I'll try that. Nothing was sent."
        )
    return dict(data or {})


async def _open_menu(spec: dict[str, Any]) -> bool:
    page = await whatsapp_page()
    try:
        result = await page.evaluate(_JS_OPEN_MESSAGE_MENU, spec)
    except Exception:  # noqa: BLE001
        return False
    if not (result or {}).get("ok"):
        return False
    # The real click, here rather than inside the page. An untrusted
    # `trigger.click()` runs the page's own listeners and none of the browser's
    # defaults, and WhatsApp's message menu does not open on that -- which is the
    # same defect `_JS_MARK_CHAT_ROW` exists to prevent for chat rows. Refusing
    # here is the honest outcome: a menu that did not open cannot be read, so
    # there is nothing to click in it.
    try:
        await page.click(f"[{_MENU_MARK}='1']", timeout=5000)
    except Exception:  # noqa: BLE001 - see the docstring on the script
        logger.info("[whatsapp] could not click the message menu trigger")
        return False
    return bool(await _poll(lambda: _menu_items(page), attempts=6, delay=0.2))


async def _menu_items(page: Any) -> list[dict[str, Any]]:
    data = await page.evaluate(_JS_READ_MENU)
    items = list((data or {}).get("items") or [])
    return items or None


async def _click_menu(verb: str, *, fallback_text: str = "") -> bool:
    """Click a menu entry, by icon first and by text only as a fallback.

    Mark, then a real press -- see :data:`_JS_MARK_MENU_ITEM` for why the press
    cannot come from inside the page. A mark that Playwright then refuses to click
    is a refusal, not a silent no-op: a menu that closed one frame earlier is
    exactly the case that used to be reported as having worked.
    """
    page = await whatsapp_page()
    icons = list(MENU_ICONS.get(verb, ()))
    try:
        marked = await page.evaluate(
            _JS_MARK_MENU_ITEM, {"icons": icons, "text": fallback_text}
        )
    except Exception:  # noqa: BLE001
        return False
    if not (marked or {}).get("ok"):
        return False
    try:
        await page.click(f"[{_ITEM_MARK}='1']", timeout=4000)
    except Exception:  # noqa: BLE001
        return False
    return True


async def react(index_from_end: int, emoji: str, *, expect: str = "") -> bool:
    """React to a specific message. Confirmed by the badge that appears on it."""
    await message_at(index_from_end, expect=expect)
    if not await _open_menu({"indexFromEnd": int(index_from_end), "expect": ""}):
        raise _missing("message menu")
    if not await _click_menu("react"):
        raise _missing("react option")
    page = await whatsapp_page()
    if not await _click_emoji(page, emoji):
        raise _missing("that emoji")
    seen = await _poll(
        lambda: _reacted(page, index_from_end),
        attempts=8,
        delay=0.25,
    )
    return bool(seen)


async def _click_emoji(page: Any, emoji: str) -> bool:
    """Mark an emoji in the open picker, then press it. ``True`` when it landed.

    Exact text equality, and no icon fallback: this is the one place a
    near-enough match picks the wrong face, and a reaction on the wrong message is
    something the recipient sees.
    """
    try:
        marked = await page.evaluate(
            """
            (emoji) => {
              const panels = Array.from(
                document.querySelectorAll("div[role='dialog'], div[aria-label]")
              );
              for (const old of document.querySelectorAll('[data-lumine-wa-emoji]')) {
                old.removeAttribute('data-lumine-wa-emoji');
              }
              for (const panel of panels) {
                for (const el of panel.querySelectorAll('span, div[role="button"]')) {
                  if ((el.textContent || '').trim() === emoji) {
                    el.setAttribute('data-lumine-wa-emoji', '1');
                    return true;
                  }
                }
              }
              return false;
            }
            """,
            emoji,
        )
        if not marked:
            return False
        await page.click(f"[{_EMOJI_MARK}='1']", timeout=4000)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _reacted(page: Any, index_from_end: int) -> bool:
    try:
        data = await page.evaluate(_JS_REACTED, {"indexFromEnd": int(index_from_end)})
    except Exception:  # noqa: BLE001
        return None
    return True if (data or {}).get("reacted") else None


async def delete_message(index_from_end: int, *, expect: str = "") -> bool:
    """Delete one message for everyone.

    Verified by the *tombstone*, not by the message's absence. A conversation
    that scrolled past the message and one where the delete succeeded look
    identical from the outside, and only the replacement WhatsApp renders proves
    the second.
    """
    await message_at(index_from_end, expect=expect)
    if not await _open_menu({"indexFromEnd": int(index_from_end), "expect": ""}):
        raise _missing("message menu")
    if not await _click_menu("delete"):
        raise _missing("delete option")
    page = await whatsapp_page()
    # The confirmation is a two-button dialog and choosing wrong *is* the
    # operation, so the primary label is what we match and nothing is inferred
    # from position.
    if not await _confirm(page, "delete"):
        raise _missing("delete confirmation")
    seen = await _poll(
        lambda: _deleted(page, index_from_end),
        attempts=8,
        delay=0.3,
    )
    return bool(seen)


async def _deleted(page: Any, index_from_end: int) -> bool:
    try:
        data = await page.evaluate(_JS_DELETED, {"indexFromEnd": int(index_from_end)})
    except Exception:  # noqa: BLE001
        return None
    return True if (data or {}).get("revoked") else None


#: How each verb's confirmation button reads in English. Passed through unchanged
#: when there is no entry, so a caller may name the button itself.
_CONFIRM_WORDS = {
    "delete": "delete",
    "block": "block",
    "unlink": "log out",
    "leave": "leave",
}


async def _confirm(page: Any, words: str) -> bool:
    """Press the one button that says what we asked for. ``True`` when it landed.

    The verbs map to their button labels in English, because WhatsApp Web's
    confirmations are not localised by the model but by the browser and a Bengali
    page shows Bengali text. An unmapped verb falls through as itself, so a caller
    can name the button; what it cannot do is *guess*, and the script refuses an
    empty label rather than taking the affirmative by position.

    A real press, and the mark-and-press pair, because a delete that reports
    success without deleting is worse than one that refuses: the tombstone check
    below would catch it, but only after the user has been told the wrong thing.
    """
    wanted = _CONFIRM_WORDS.get(words, words)
    if not str(wanted).strip():
        return False
    try:
        marked = await page.evaluate(_JS_MARK_CONFIRM, str(wanted).strip().lower())
    except Exception:  # noqa: BLE001
        return False
    if not (marked or {}).get("ok"):
        return False
    try:
        await page.click(f"[{_CONFIRM_MARK}='1']", timeout=4000)
    except Exception:  # noqa: BLE001
        return False
    return True


async def star_message(index_from_end: int, *, star: bool = True, expect: str = "") -> bool:
    """Star or unstar one message, verified by the marker on that message."""
    await message_at(index_from_end, expect=expect)
    if not await _open_menu({"indexFromEnd": int(index_from_end), "expect": ""}):
        raise _missing("message menu")
    if not await _click_menu("star" if star else "unstar"):
        raise _missing("star option" if star else "unstar option")
    page = await whatsapp_page()
    seen = await _poll(
        lambda: _message_flag(page, index_from_end, "star"),
        attempts=6,
        delay=0.25,
    )
    return bool(seen)


async def _message_flag(page: Any, index_from_end: int, flag: str) -> bool:
    try:
        data = await page.evaluate(
            _JS_MESSAGE_FLAG, {"indexFromEnd": int(index_from_end), "flag": flag}
        )
    except Exception:  # noqa: BLE001
        return None
    return True if (data or {}).get("present") else None


async def quote_message(index_from_end: int, *, expect: str = "") -> bool:
    """Put the composer into reply mode, quoting the message at that position.

    *Only* the quote. Typing is the caller's business, through the ordinary draft
    operation, and that split is deliberate: this function opening a menu and
    then typing would mean the text was written twice on two different paths, and
    two ways to type a draft is two ways to disagree about whether it arrived.

    Quoting is what makes "reply to *that one*" mean something. Without it a
    reply is a send into whichever chat is open, which is a different operation
    wearing this one's name.

    The position is checked against what is actually there first, because a
    reference minted before the conversation moved would otherwise quote a
    different message than the user meant.
    """
    await message_at(index_from_end, expect=expect)
    if not await _open_menu({"indexFromEnd": int(index_from_end), "expect": ""}):
        raise _missing("message menu")
    if not await _click_menu("reply"):
        raise _missing("reply option")
    page = await whatsapp_page()
    if not await _poll(lambda: _quoted(page), attempts=6, delay=0.2):
        raise _missing("the quoted message bar")
    return True


async def _quoted(page: Any) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                () => !!document.querySelector(
                  "[data-testid='quoted-message'], div[data-testid='reply-bar']"
                )
                """
            )
        )
    except Exception:  # noqa: BLE001
        return None


async def forward(index_from_end: int, *, destination: str, expect: str = "") -> bool:
    """Forward one message to another chat.

    The destination is resolved *first* and the menu is opened second, on purpose:
    WhatsApp's forward dialog is a full-screen takeover, so discovering halfway
    through that the destination does not exist means leaving the user stranded
    inside a dialog nobody is going to close.
    """
    await message_at(index_from_end, expect=expect)
    if not await _open_menu({"indexFromEnd": int(index_from_end), "expect": ""}):
        raise _missing("message menu")
    if not await _click_menu("forward"):
        raise _missing("forward option")
    page = await whatsapp_page()
    if not await _poll(lambda: _forward_dialog(page), attempts=8, delay=0.25):
        raise _missing("the forward dialog")
    if not await search_chats(destination):
        raise ToolError(f"{destination} isn't in your WhatsApp, so nothing was forwarded.")
    if not await open_chat(destination):
        raise ToolError(f"I couldn't open {destination}'s chat, so nothing was forwarded.")
    await page.keyboard.press("Enter")
    return await _poll(lambda: _forward_dialog(page), attempts=6, delay=0.25) is None


async def _forward_dialog(page: Any) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                () => !!document.querySelector(
                  "[data-testid='forward-dialog'], div[role='dialog'] [data-testid='chat-list']"
                )
                """
            )
        )
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

MSG_NO_ATTACH = (
    "I can't attach a file from this computer on WhatsApp Web right now -- send "
    "it from your phone, or give me the text. Nothing was sent."
)


async def attach(path: str, *, caption: str = "") -> bool:
    """Send a file through WhatsApp's own hidden file input.

    The input is what the attach button drives, so setting it directly is the same
    act the UI performs -- without the native file dialog, which no browser
    automation can type a path into and which would also hand the user's whole
    filesystem to a modal we cannot see.
    """
    await require_ready()
    page = await whatsapp_page()
    if not await _click_target(page, "attach_button"):
        return False
    seen = await _poll(
        lambda: _has_file_input(page),
        attempts=6,
        delay=0.2,
    )
    if not seen:
        return False
    try:
        await page.set_input_files(
            "input[type='file']", str(path), timeout=5000
        )
    except Exception:  # noqa: BLE001 - a rejected file is a refusal, not a crash
        return False
    if caption:
        await _poll(lambda: _composer_present(page), attempts=8, delay=0.25)
        await type_message(caption)
    return True


async def _has_file_input(page: Any) -> bool:
    try:
        return bool(
            await page.evaluate(
                "() => { const el = document.querySelector(\"input[type='file']\"); "
                "return !!el && !el.disabled && !el.files; }"
            )
        )
    except Exception:  # noqa: BLE001
        return None


async def _composer_present(page: Any) -> bool:
    try:
        return bool(await page.evaluate("() => !!document.querySelector(\"footer [contenteditable='true']\")"))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Chat management
# ---------------------------------------------------------------------------


async def row_action(title: str, verb: str, *, fallback_text: str = "") -> bool:
    """Open a chat row's menu and click one entry.

    Verified by the marker the action sets, not by the click landing. That is why
    every caller passes what it expects to be able to see afterwards: a menu item
    that moved in a build change is a silent no-op otherwise, and "I pinned it"
    for a chat that is still sitting at the bottom is worse than a refusal.
    """
    page = await whatsapp_page()
    opened = await page.evaluate(_JS_MARK_ROW_MENU, str(title or "").strip().casefold())
    if not (opened or {}).get("ok"):
        raise _missing(f"menu for {title}")
    # The real press, so the hover that reveals the trigger is a real hover and
    # the click reaches a menu that actually opened.
    try:
        await page.click(f"[{_ROW_MENU_MARK}='1']", timeout=4000)
    except Exception:  # noqa: BLE001
        raise _missing(f"menu for {title}") from None
    if not await _poll(lambda: _menu_items(page), attempts=6, delay=0.2):
        raise _missing("that chat's menu")
    if not await _click_menu(verb, fallback_text=fallback_text):
        raise _missing(f"the {verb} option")


async def row_flag(title: str, flag: str) -> bool | None:
    """Is `flag` set on a chat row? ``None`` when the row is not there."""
    page = await whatsapp_page()
    try:
        data = await page.evaluate(_JS_ROW_FLAG, {"title": title, "flag": flag})
    except Exception:  # noqa: BLE001
        return None
    if not (data or {}).get("found"):
        return None
    return bool((data or {}).get("present"))


async def set_flag(title: str, flag: str, verb: str, *, want: bool, **kwargs: Any) -> bool:
    """Put a row marker where it should be, and confirm it got there.

    Reads the marker *first*, so an action that is already in the desired state is
    a no-op rather than a toggle applied twice -- "pin it" twice, because the
    first click's result had not rendered yet, unpinning it.
    """
    current = await row_flag(title, flag)
    if current is None:
        raise ToolError(f"{title} isn't in your chat list, so nothing was changed.")
    if current == want:
        return True
    await row_action(title, verb, **kwargs)
    page = await whatsapp_page()
    seen = await _poll(
        lambda: _flag_is(page, title, flag, want),
        attempts=8,
        delay=0.25,
    )
    return bool(seen)


async def _flag_is(page: Any, title: str, flag: str, want: bool) -> bool:
    try:
        data = await page.evaluate(_JS_ROW_FLAG, {"title": title, "flag": flag})
    except Exception:  # noqa: BLE001
        return None
    if not (data or {}).get("found"):
        return None
    return True if bool((data or {}).get("present")) == want else None


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


async def chat_info() -> dict[str, Any]:
    """The open chat's info drawer: about text, presence, number."""
    page = await whatsapp_page()
    try:
        return dict(await page.evaluate(_JS_CHAT_INFO) or {})
    except Exception:  # noqa: BLE001
        return {}


async def block_contact(*, block: bool = True) -> bool:
    """Block or unblock whoever's chat is open.

    Reached through the contact's own settings drawer, which is where WhatsApp
    puts it, and confirmed by the block state read back from that same drawer.
    The confirmation matters more than usual here: "I blocked her" reported for a
    block that did not happen is a person who believes she is unreachable and is
    not.
    """
    page = await whatsapp_page()
    if not await _has_info(page):
        await open_chat_info("")
    if not await _poll(lambda: _has_info(page), attempts=8, delay=0.25):
        return False
    # Mark, then press -- the same arrangement as every other click here. The
    # icon is tried before the text for the ordinary reason that labels are
    # localised and `data-icon` is not, but both are matched exactly: the drawer
    # holds "Block contact" and "Report contact", and the second one is not a
    # near miss for the first.
    marked = await page.evaluate(
        """
        (want) => {
          const panel =
            document.querySelector("div[data-testid='chat-info']") ||
            document.querySelector("[data-testid='contact-info-drawer']") ||
            document.querySelector("div[role='dialog']");
          if (!panel) return false;
          for (const old of document.querySelectorAll('[data-lumine-wa-block]')) {
            old.removeAttribute('data-lumine-wa-block');
          }
          const icons = want ? ['block', 'block-filled', 'ban'] : ['unblock'];
          for (const el of panel.querySelectorAll('div[role="button"], button, li, div[tabindex]')) {
            const icon = el.querySelector('[data-icon]');
            const value = (icon && icon.getAttribute('data-icon')) || '';
            if (icons.includes(value)) {
              el.setAttribute('data-lumine-wa-block', '1');
              return true;
            }
          }
          const words = want ? ['block contact', 'block'] : ['unblock contact', 'unblock'];
          for (const el of panel.querySelectorAll('div[role="button"], button, li, div[tabindex]')) {
            const text = (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            if (words.includes(text)) {
              el.setAttribute('data-lumine-wa-block', '1');
              return true;
            }
          }
          return false;
        }
        """,
        bool(block),
    )
    if not marked:
        return False
    try:
        await page.click(f"[{_BLOCK_MARK}='1']", timeout=4000)
    except Exception:  # noqa: BLE001
        return False
    await _sleep(0.3)
    # The confirmation, where there is one. A build with no dialog has already
    # done it, so a missing dialog is not itself a failure.
    await _confirm(page, "block" if block else "unblock")
    await _sleep(0.3)
    seen = await _poll(
        lambda: _block_state(page, want=bool(block)),
        attempts=6,
        delay=0.25,
    )
    if seen is not None:
        return bool(seen)
    # No readable state: the click landed and no dialog is arguing about it, which
    # is the most a DOM-only path can honestly claim. Reported as done rather than
    # refused, and the ledger holds it so a blind retry cannot stack blocks.
    return True


async def _block_state(page: Any, *, want: bool) -> bool | None:
    try:
        data = await page.evaluate(
            """
            (want) => {
              const panel =
                document.querySelector("div[data-testid='chat-info']") ||
                document.querySelector("[data-testid='contact-info-drawer']") ||
                document.querySelector("div[role='dialog']");
              if (!panel) return { found: false };
              const text = (panel.textContent || '').toLowerCase();
              return { found: true, blocked: want ? /unblock/.test(text) : /block contact/.test(text) && !/unblock/.test(text) };
            }
            """,
            bool(want),
        )
    except Exception:  # noqa: BLE001
        return None
    if not (data or {}).get("found"):
        return None
    return bool((data or {}).get("blocked"))


async def open_chat_info(title: str = "") -> bool:
    """Open a chat's info drawer.

    With a title, through the row menu. Without one, reports whether it is
    already open -- which is what ``block_contact`` needs, because blocking is
    reached from a drawer the caller did not open.
    """
    if not str(title or "").strip():
        page = await whatsapp_page()
        return bool(await _has_info(page))
    await row_action(title, "contact")
    page = await whatsapp_page()
    return bool(await _poll(lambda: _has_info(page), attempts=8, delay=0.25))


async def _has_info(page: Any) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                () => !!document.querySelector(
                  "div[data-testid='chat-info'], [data-testid='contact-info-drawer']"
                )
                """
            )
        )
    except Exception:  # noqa: BLE001
        return None


async def devices() -> list[dict[str, Any]]:
    """The linked-devices list, reached through the account menu."""
    page = await whatsapp_page()
    await _click_target(page, "settings_menu")
    await _sleep(0.3)
    if not await _click_menu("menu", fallback_text="linked devices"):
        if not await _click_target(page, "more_menu"):
            return []
        await _sleep(0.3)
        if not await _click_menu("menu", fallback_text="linked devices"):
            return []
    await _sleep(0.4)
    data = await page.evaluate(_JS_DEVICES)
    return list((data or {}).get("rows") or [])


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


async def unlink_device(name: str) -> bool:
    """Sign one other device out of the account.

    Matched on the name as WhatsApp lists it and confirmed by its *absence*
    afterwards -- which is sound here even though absence was rejected as evidence
    for a delete. The difference is the size of the list: a linked-device list is
    a handful of rows, so a row that was there and is not has been removed, rather
    than a conversation that may simply have scrolled.
    """
    page = await whatsapp_page()
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return False
    if not await open_devices():
        return False

    before = await _device_rows(page)
    if not any(wanted in str(row.get("text") or "").casefold() for row in before):
        return False
    marked = await page.evaluate(
        """
        (wanted) => {
          for (const old of document.querySelectorAll('[data-lumine-wa-unlink]')) {
            old.removeAttribute('data-lumine-wa-unlink');
          }
          for (const el of document.querySelectorAll(
            "[data-testid='device-row'], div[role='listitem'], div[role='button']"
          )) {
            const line = (el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            if (!line.includes(wanted)) continue;
            const icon = el.querySelector(
              "[data-icon='logout'], [data-icon='log-out'], [data-icon='trash'], [data-icon='delete']"
            );
            const target = icon || el;
            target.setAttribute('data-lumine-wa-unlink', '1');
            return true;
          }
          return false;
        }
        """,
        wanted,
    )
    if not marked:
        return False
    # A real press. Signing a device out has to *happen*, and the confirmation
    # dialog afterwards is what the user is asked to agree to -- so a click that
    # reported success and left the device listed would tell them they had signed
    # out a phone that is still signed in.
    try:
        await page.click(f"[{_UNLINK_MARK}='1']", timeout=4000)
    except Exception:  # noqa: BLE001
        return False
    await _sleep(0.3)
    await _confirm(page, "log out")
    await _sleep(0.4)
    gone = await _poll(
        lambda: _device_gone(page, wanted),
        attempts=8,
        delay=0.3,
    )
    return bool(gone)


async def _device_rows(page: Any) -> list[dict[str, Any]]:
    try:
        data = await page.evaluate(_JS_DEVICES)
    except Exception:  # noqa: BLE001
        return []
    return list((data or {}).get("rows") or [])


async def _device_gone(page: Any, wanted: str) -> bool:
    rows = await _device_rows(page)
    if not rows:
        return None
    return None if any(wanted in str(r.get("text") or "").casefold() for r in rows) else True


async def open_devices() -> bool:
    """Open the linked-devices list through the account menu."""
    page = await whatsapp_page()
    await _click_target(page, "settings_menu")
    await _sleep(0.3)
    if not await _click_menu("menu", fallback_text="linked devices"):
        if not await _click_target(page, "more_menu"):
            return False
        await _sleep(0.3)
        if not await _click_menu("menu", fallback_text="linked devices"):
            return False
    await _sleep(0.4)
    return bool(await _device_rows(page))


async def start_call(title: str, *, video: bool = False) -> bool:
    """Start a voice or video call to whoever's chat is open.

    Verified by the call surface appearing, which is evidence the call *started*
    and nothing more. Whether anybody answered is not knowable from this side, so
    the caller says so rather than reporting a connected call.
    """
    page = await whatsapp_page()
    if not await _click_target(page, "video_call" if video else "voice_call"):
        # The header call buttons carry icons rather than a stable test id on some
        # builds, so a second attempt marks the header's own controls and presses
        # them for real -- an in-page click here rang nobody, which the call
        # surface check then correctly reported as no call, so it was caught rather
        # than told to the user as a dialled call.
        marked = await page.evaluate(
            """
            (want) => {
              const header = document.querySelector('#main header') ||
                document.querySelector("header");
              if (!header) return false;
              for (const old of document.querySelectorAll('[data-lumine-wa-call]')) {
                old.removeAttribute('data-lumine-wa-call');
              }
              const icons = want ? ['video', 'video-call'] : ['call', 'phone'];
              for (const el of header.querySelectorAll('[data-icon]')) {
                if (icons.includes(el.getAttribute('data-icon'))) {
                  el.setAttribute('data-lumine-wa-call', '1');
                  return true;
                }
              }
              return false;
            }
            """,
            bool(video),
        )
        if not marked:
            return False
        try:
            await page.click(f"[{_CALL_MARK}='1']", timeout=4000)
        except Exception:  # noqa: BLE001
            return False
    seen = await _poll(lambda: _in_call(page), attempts=8, delay=0.3)
    return bool(seen)


async def _in_call(page: Any) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                () => !!document.querySelector(
                  "[data-testid='call-overlay'], div[data-testid='outgoing-call']"
                ) || /\\b(Calling|Calling\\.\\.\\.|Ringing)\\b/.test(document.body.innerText || '')
                """
            )
        )
    except Exception:  # noqa: BLE001
        return None


async def _read_targets(page: Any) -> dict[str, Any]:
    try:
        data = await page.evaluate(_JS_PROBE, _probe_targets())
    except Exception as exc:  # noqa: BLE001 - a probe that fails is a finding
        return {"error": clip(str(exc), 160)}
    targets = (data or {}).get("targets") or {}
    return {
        "hit": {
            name: value["hit"]
            for name, value in targets.items()
            if value.get("hit")
        },
        "missing": sorted(
            name for name, value in targets.items() if not value.get("hit")
        ),
        "icons": list((data or {}).get("icons") or []),
    }


async def probe() -> dict[str, Any]:
    """What this build's DOM actually offers, read where the DOM actually offers it.

    The engine behind ``agent/whatsapp_probe.py``. Every target is checked against
    every known spelling and the ones that hit are reported, so the candidate lists
    in :data:`TARGETS` get maintained from evidence rather than from somebody's
    recollection of what WhatsApp used to look like.

    Opens a chat before the second reading, and reports *that* one. The first run
    of this tool reported the composer, the chat header, the send button and the
    message bubbles as missing -- on a home screen, where none of them exist in
    any build. It read the page in a state the operations never run in, and called
    the result a diagnosis.
    """
    try:
        page = await whatsapp_page()
    except ToolError as exc:
        return {"error": str(exc)}
    # Wait for the page to finish loading before reading anything. Probing a
    # splash screen reports every target missing and the account signed out, which
    # is a diagnosis invented entirely by asking too early -- and it is the one
    # this tool exists to prevent anybody having to make.
    ready = await state()
    if ready == "loading":
        return {
            "url": getattr(page, "url", ""),
            "ready": "loading",
            "hit": {},
            "missing": sorted(TARGETS),
            "icons": [],
            "note": (
                "WhatsApp had not finished loading after "
                f"{READY_TIMEOUT_SECONDS:.0f}s, so nothing was read. This is not a "
                "sign-in problem -- re-run it once the tab has settled."
            ),
        }

    home = await _read_targets(page)

    opened = False
    title = ""
    try:
        title = str(await page.evaluate(_JS_MARK_FIRST_CHAT) or "").strip()
        if title:
            # A real click, through the browser's input pipeline. An untrusted
            # `row.click()` inside the page is what made this report "opened:
            # False" against a perfectly healthy chat list.
            await page.click(_ROW_SELECTOR, timeout=5000)
            opened = True
    except Exception:  # noqa: BLE001 - a probe that cannot click reports what it has
        opened = False
    if opened:
        # Wait for the conversation to render, which is the same "the filter must
        # arrive" rule as everywhere else in this module.
        seen = await _poll(lambda: _conversation_present(page), attempts=10, delay=0.3)
        in_chat = await _read_targets(page)
        if "error" in in_chat:
            # The chat click navigated somewhere the probe could not read. The
            # home reading is still a real reading, so it is returned rather than
            # discarded -- and the reason is carried with it.
            return {**home, "chat": title, "opened": bool(seen), "error": in_chat["error"]}
        return {
            "url": getattr(page, "url", ""),
            "ready": ready,
            "chat": title,
            "opened": bool(seen),
            "hit": in_chat["hit"],
            "missing": in_chat["missing"],
            "home_only": sorted(
                set(home["hit"]) - set(in_chat["hit"]) - _CONTEXTUAL
            ),
            "icons": in_chat["icons"],
        }

    return {
        "url": getattr(page, "url", ""),
        "ready": ready,
        "hit": home["hit"],
        "missing": sorted(set(home["missing"]) - _CONTEXTUAL),
        "not_measured": sorted(_CONTEXTUAL),
        "icons": home["icons"],
        "note": (
            "No chat could be opened, so the conversation-dependent targets were "
            "not measured. They are listed separately rather than as missing: "
            "they do not exist on the home screen in any build."
        ),
    }


async def _conversation_present(page: Any) -> bool:
    try:
        return bool(
            await page.evaluate(
                "() => !!document.querySelector(\"footer [contenteditable='true']\")"
            )
        )
    except Exception:  # noqa: BLE001
        return None