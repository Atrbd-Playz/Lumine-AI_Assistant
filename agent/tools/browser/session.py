"""One isolated browser, started on demand, closable on demand.

Design rules worth keeping, because each one is a security or lifetime
decision rather than a style:

- **Isolated profile, never the user's browser.** A separate profile
  directory with its own cookies, so nothing the model browses can read the
  sessions the user keeps in their own browser. The profile persists on disk
  between conversations -- so a login the *user* completes by typing it
  themselves survives -- and is never seeded from the real profile.
- **Playwright is imported lazily.** The module must import on machines
  without it: the tool reports a setup step instead of the worker refusing
  to start.
- **Every request crosses a guard.** A context-wide route handler allows
  only public http(s). Page JS, redirects, subresources, iframes -- all of
  it goes through the same check, so a hostile page cannot probe the user's
  router or a localhost service through the browser the assistant opened.
  This is the pivot attack: something the user was told to trust aiming the
  browser inward.
- **Bound to the loop it started on, and guarded across threads.** Playwright's
  objects belong to an event loop. A call arriving from a *different* loop fails
  closed with a sentence instead of touching half-alive state from a dead loop.
  The mutual exclusion that enforces "one browser, one owner" is a
  :class:`threading.Lock` held for the microseconds of an ownership decision --
  never across an ``await`` -- because ``asyncio.Lock`` is not thread-safe and
  this process is genuinely multi-threaded: LiveKit runs each dispatched room
  in its own thread with its own loop (see ``_OwnerLock``).
- **The password refusal lives inside the page, at the moment of typing.**
  The tool refuses password fields before asking anything (see
  `actions.fill_deny_reason`), and the script below refuses them again
  inside the DOM -- so a page that swaps elements under a reference between
  two calls still cannot receive a keystroke in a password field.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

try:
    from ..tools_compat import ToolError
    from ..tools_text import clip
    from .actions import REF_RE, host_verdict, hostname_of, normalize_url, TITLE_CHARS, URL_CHARS
    from . import adblock, state as browser_state
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_compat import ToolError
    from tools_text import clip
    from browser.actions import REF_RE, host_verdict, hostname_of, normalize_url, TITLE_CHARS, URL_CHARS
    from browser import adblock, state as browser_state  # type: ignore[no-redef]

logger = logging.getLogger("lumine")

#: Where the isolated profile lives. One conversation's cookies are the next
#: conversation's cookies only for pages the *user* chose to log into.
PROFILE_VAR = "LUMINE_BROWSER_PROFILE"

#: The documented escape hatch for people who want their assistant to reach
#: a NAS or a local dev server. Off by default; see `actions.host_verdict`.
ALLOW_LOCAL_VAR = "LUMINE_BROWSER_ALLOW_LOCAL"

#: Run without showing the window. Default shown: a person watching the
#: screen see exactly what the assistant does, which is worth more than the
#: tidiness of an invisible window.
HEADLESS_VAR = "LUMINE_BROWSER_HEADLESS"

#: How long a navigation may take before we report failure instead of
#: claiming a page we never saw.
NAV_TIMEOUT_MS = 15000

#: How long a key press waits for its control to become pressable. Far
#: shorter than Playwright's 30-second default: this is a voice turn, and a
#: control that will not take a key should be reported in a sentence the
#: user hears now, not in half a minute.
PRESS_TIMEOUT_MS = 3000

#: Addresses are cached this long, so a page full of images does not become
#: a page full of DNS lookups -- but a host that moves between two networks
#: does not stay wrong for long either.
DNS_CACHE_SECONDS = 60.0

#: How long a *failed* resolution is remembered. Deliberately far shorter than
#: the success TTL above, and this is the fix for a video that played for ~49
#: seconds and then stopped with "Something went wrong. Refresh or try again
#: later."
#:
#: The mechanism: YouTube serves its media from rotating segment hosts
#: (``rr4---sn-abc.googlevideo.com``), and it rotates to a *new* one partway
#: through a stream. A new host means a fresh lookup, one transient miss on that
#: lookup is cached, and ``host_verdict`` refuses anything that did not resolve
#: -- so the guard started aborting every segment from that host for a full
#: minute, which is exactly a player that stops mid-video. A failure is the
#: thing least worth caching: it is the state most likely to be transient, and
#: the cost of a wrong retry is one more lookup.
DNS_FAIL_CACHE_SECONDS = 2.0

#: How long one lookup may take. Before this there was no bound at all, so a
#: resolver that answered slowly held the guard -- and therefore the request --
#: for however long the OS felt like waiting. A page makes many requests; each
#: one waiting on DNS is the difference between a stream that buffers and one
#: that stalls.
DNS_TIMEOUT_SECONDS = 5.0

#: Where the WhatsApp automation path drives. Held here rather than in the
#: messaging package because this module owns the browser it runs in, and the URL
#: is part of "which page is this tab on" -- the check that lets a warm call skip
#: reloading a four-megabyte application.
WHATSAPP_URL = "https://web.whatsapp.com/"

#: Is this tab already on WhatsApp? Host comparison rather than prefix
#: comparison, so a path change upstream does not send us reloading on every call,
#: and a lookalike host cannot talk us into staying put.
WHATSAPP_HOST = "web.whatsapp.com"

#: The hosts a YouTube watch page can arrive on. All three render the same player,
#: and a redirect between them is normal, so the check that decides "is this a
#: video page" has to include the ones YouTube itself sends you to.
YOUTUBE_HOSTS = ("www.youtube.com", "m.youtube.com", "youtube.com")

#: A YouTube video id. Fixed at eleven characters of a URL-safe alphabet, which is
#: exactly why an invented one is accepted without complaint: it is the right
#: *shape*, so nothing about the request itself reveals that nobody looked it up.
VIDEO_ID_RE = r"^[A-Za-z0-9_-]{11}$"

#: How many tabs the model may hold open at once.
#:
#: A cap, and the reason it is a *refusal* rather than a silent eviction is that
#: the model asked for a tab and did not get one: a tool that quietly does
#: something else is the failure mode this whole file is built against. Six is
#: more than a voice conversation needs -- two or three is a working habit -- and
#: it bounds what a page can open behind our back, since every popup becomes a
#: tab too (see :meth:`_Browser._on_page`).
MAX_WORK_TABS = 6

#: How many *other* tabs a read names. A read is re-billed on every later turn
#: for the rest of the conversation, so the list a read carries is capped well
#: below the number of tabs that can exist -- enough to answer "what else did I
#: leave open", not enough to become the payload.
MAX_REPORTED_TABS = 4


def video_id_of(url: object) -> str:
    """The `v=` id in a watch URL, or an empty string."""
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if parsed.hostname not in YOUTUBE_HOSTS or not parsed.path.startswith("/watch"):
        return ""
    return parse_qs(parsed.query).get("v", [""])[0].strip()[:32]


#: Wording that means the video is *there* and merely not for this viewer.
#:
#: This is the difference between "Lumine guessed the address" and "this is a
#: private video", and the two need opposite replies. A guessed id gets "open the
#: results and click it"; a private one gets "this link needs the owner's
#: permission", and telling someone to go and search for a video they just handed
#: us is worse than saying nothing at all.
#:
#: Matched case-insensitively over the whole reason string. Over-matching is
#: harmless here -- the cost of calling a genuinely absent video "restricted" is
#: one sentence of advice, while the cost of the reverse is Lumine insisting she
#: searched when she never did.
RESTRICTED_VIDEO_WORDS = (
    "private",
    "removed by the uploader",
    "deleted",
    "age-restricted",
    "age restricted",
    "confirm your age",
    "sign in",
    "members-only",
    "members only",
    "join this channel",
    "not available in your country",
    "unavailable in your country",
    "blocked it in your country",
    "copyright",
)


def _video_exists_anyway(reason: str, seen_on_a_page: bool) -> bool:
    """Whether an unplayable video still exists, and so was not invented.

    Two independent pieces of evidence, either of which is enough. The wording
    YouTube chose names the reason, and a private or region-blocked video names
    itself; a bare "Video unavailable" does not, because that is what a made-up
    id produces. And an id we previously read off a results page cannot have been
    invented, whatever happened to it since -- most likely the owner removed it.
    """
    text = (reason or "").casefold()
    if any(word in text for word in RESTRICTED_VIDEO_WORDS):
        return True
    return bool(seen_on_a_page)


def _is_whatsapp(url: object) -> bool:
    try:
        return (urlsplit(str(url or "")).hostname or "").casefold() == WHATSAPP_HOST
    except ValueError:
        return False


#: Whether the WhatsApp tab is signed in and showing a chat list. ``ready`` is the
#: absence of a QR code *and* the presence of the side pane: either alone is not
#: enough, because a page still loading shows neither, and "signed in" must never
#: be inferred from anything but both facts.
_WA_STATE = """
() => {
  const qr = document.querySelector('[data-testid="qrcode"]');
  const side = document.querySelector('#pane-side');
  const grid = document.querySelector('#app');
  return { ready: !!side && !qr && !!grid };
}
"""

MSG_NOT_INSTALLED = (
    "The browser engine is not installed yet, so nothing was opened. "
    "It is installed once with: python -m playwright install chromium"
)
MSG_OTHER_LOOP = "The browser is busy with another conversation. Nothing was done."
MSG_PROFILE_LOCKED = (
    "A Lumine browser window is already open, and this profile can only be used "
    "by one at a time. Close that window -- or quit the Lumine app if it is the "
    "one holding it -- and ask me again. Nothing was sent."
)
MSG_NO_PAGE = "No page is open yet -- open one first."
MSG_UNKNOWN_REF = "I don't have that control -- observe the page again. Nothing was done."
MSG_CHANGED = "That control changed on the page -- observe it again. Nothing was done."
MSG_NO_TAB = "I don't have that tab -- ask which tabs are open. Nothing was done."
MSG_NO_OTHER_TABS = (
    "There is only one tab open, so there are no others to close. Nothing was closed."
)
MSG_TOO_MANY_TABS = (
    f"I already have {MAX_WORK_TABS} tabs open. Close one first, then ask for another. "
    "Nothing was opened."
)
MSG_BAD_URL = (
    "I can only open web addresses (http or https) -- I won't open that. "
    "Nothing was shown."
)


# ---------------------------------------------------------------------------
# Page scripts
# ---------------------------------------------------------------------------
#
# Each is one atomic step. `inspect` reads the facts a classification needs;
# `click` re-reads them *and* clicks in the same evaluation, so there is no
# window in which a page could swap a harmless-looking element under the
# reference between our decision and our action. `fill` repeats the password
# refusal at the point of typing for the same reason.

_JS_OBSERVE = """(gen) => {
""" + browser_state._JS_VIEWPORT_FN + """
""" + browser_state._JS_ITEM_CONTEXT_FN + """
""" + browser_state._JS_PAGE_ITEMS_FN + """
  for (const el of document.querySelectorAll('[data-lumine-ref]')) {
    el.removeAttribute('data-lumine-ref');
  }
  // The document is stamped with the read that produced these references. Every
  // later action checks it, so a reference cannot survive the page it was read
  // from -- see `_JS_STALE`.
  if (document.documentElement) {
    document.documentElement.setAttribute('data-lumine-gen', String(gen));
  }
  // Chrome, skipped. Not a site-specific list: a control in the page's own
  // furniture is furniture on *every* site, and spending a whole report on it is
  // how a results page ends up offering a model eight navigation links and not
  // one result. It used to be exactly that on YouTube -- the masthead and the
  // guide come first in document order, so the only two of the eight that went
  // anywhere led to the home page.
  //
  // Both the element forms and the ARIA landmark roles are checked, because no
  // single spelling covers every site: a masthead is `<header>` on one site,
  // `role="banner"` on another, and a custom element on the third. The landmark
  // roles are the reliable ones and the element names are kept because plenty of
  // pages never set a role at all.
  //
  // One element legitimately means two different things depending on where it
  // sits: a `<footer>` at page level is Terms and Privacy, never the thing the
  // user came for, while a `<footer>` inside an `<article>` is the byline and the
  // tags, which is exactly what they came for. So the walk keeps going past a
  // footer and lets a content landmark above it settle the question.
  //
  // `nav`, `header` and the landmark roles get no such second chance, and
  // deliberately so: none of them is ever the content a person asked for, even
  // when a page misuses the role and puts a banner inside its main region --
  // which is what an injected advertisement looks like. Rescuing those would put
  // the ads back into a results report.
  //
  // `section` is *not* treated as a content landmark, for the same reason in the
  // other direction: it wraps content, but so does the whole navigation drawer,
  // and honouring it would let every nav on the page through.
  const CHROME_ROLES = ['navigation', 'banner', 'contentinfo'];
  const isChrome = (el) => {
    let pageFooter = false;
    for (let node = el; node && node !== document.body; node = node.parentElement) {
      if (!node.tagName) continue;
      const tag = node.tagName.toLowerCase();
      const role = node.getAttribute ? (node.getAttribute('role') || '').toLowerCase() : '';
      if (tag === 'article' || tag === 'main' || role === 'article' || role === 'main') return false;
      if (tag === 'nav' || tag === 'header') return true;
      if (CHROME_ROLES.indexOf(role) !== -1) return true;
      if (tag === 'footer') pageFooter = true;
    }
    return pageFooter;
  };
  // A name for a control that has none. Ordered by what a *person* would call
  // it, and the fallbacks at the end are the whole point: the old rule was
  // `if (!label) continue`, so a page's headings, images and unlabelled buttons
  // were collected into nothing at all and the model was told the page had no
  // controls on it. An icon button with no accessible name is still a button,
  // and "button 14" is a worse label than no label but a far better one than a
  // control that does not exist as far as the tool is concerned.
  //
  // Written against `getAttribute`, `textContent`, a `parentElement` walk and
  // one precomputed `label[for]` map -- no `closest`, no `querySelector` -- so
  // the same script still runs against the stub DOM in `test_observe_controls`,
  // which is the only honest test of what it collects.
  const labelsById = {};
  for (const label of document.querySelectorAll('label')) {
    const forId = label.getAttribute ? label.getAttribute('for') : null;
    const text = label && label.textContent ? String(label.textContent).trim() : '';
    if (forId && text && !labelsById[forId]) labelsById[forId] = text.slice(0, 120);
  }
  const nameOf = (el, role) => {
    const attrs = ['aria-label', 'placeholder', 'alt', 'title', 'name'];
    for (const attr of attrs) {
      const value = el.getAttribute(attr);
      if (value && String(value).trim()) return String(value).trim().slice(0, 120);
    }
    if (el.id && labelsById[el.id]) return labelsById[el.id];
    // A <label> that *wraps* its control has no `for`, so the map above cannot
    // hold it. Walk up instead of calling `closest`, which the stub does not
    // implement and which would silently make this half of the naming dead in
    // the only test that exercises it.
    for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
      if (node.tagName && node.tagName.toLowerCase() === 'label' && node.textContent) {
        const text = String(node.textContent).trim();
        if (text) return text.slice(0, 120);
      }
    }
    const own = el.textContent ? String(el.textContent).trim() : '';
    if (own) return own.slice(0, 120);
    // An image's file name, which is what an alt-less icon usually is. Only the
    // leaf: a sprite sheet's URL names every icon on the site at once.
    const src = el.getAttribute('src');
    if (src) {
      const leaf = String(src).split(/[?#]/)[0].split('/').pop();
      if (leaf && leaf !== '.' && leaf !== '..') return leaf.slice(0, 120);
    }
    return el.tagName.toLowerCase() + ' ' + role;
  };
  const roleOf = (el) => {
    const explicit = (el.getAttribute('role') || '').trim().toLowerCase();
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'a' && el.getAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'submit' || type === 'button' || type === 'image' || type === 'reset') return 'button';
      if (type === 'search') return 'searchbox';
      if (type === 'range') return 'slider';
      if (type === 'number') return 'spinbutton';
      if (type === 'password') return 'textbox';
      return 'textbox';
    }
    if (tag === 'img') return 'img';
    if (tag === 'video') return 'video';
    if (tag === 'h1' || tag === 'h2' || tag === 'h3') return 'heading';
    if (tag === 'h4' || tag === 'h5' || tag === 'h6') return 'heading';
    return tag;
  };
  // Every element the report may offer. The ARIA roles are here for the same
  // reason `locators.ROLES` holds them: a `role="radiogroup"` filter, a
  // `role="dialog"` and a `role="heading"` are all things a person points at,
  // and none of them is reachable by any HTML tag of its own.
  const nodes = document.querySelectorAll([
    'a[href]', 'button', 'input', 'select', 'textarea', 'video', 'summary', 'img',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[role="option"]', '[role="checkbox"]', '[role="switch"]', '[contenteditable="true"]',
    '[role="radiogroup"]', '[role="radio"]', '[role="combobox"]', '[role="listbox"]',
    '[role="slider"]', '[role="spinbutton"]', '[role="dialog"]', '[role="alertdialog"]',
    '[role="searchbox"]', '[role="textbox"]', '[role="togglebutton"]',
    '[role="menuitemcheckbox"]', '[role="row"]', '[role="cell"]', '[role="heading"]',
    '[role="figure"]', '[role="listitem"]', '[onclick]'
  ].join(','));
  let minted = 0;
  const controls = [];
  // How many of each role one report offers. Without it a results page is sixty
  // `listitem` rows and the search box that produced them is nowhere in the
  // report -- the cap is per role so the *mix* survives, which is the property a
  // model actually needs to act.
  //
  // Enforced *before* the ref is minted, not after. Minting first and then
  // skipping leaves a hole in the sequence (`r1`, `r3`), and the contiguity of
  // that sequence is a stated property: a model told to act on `r2` must be
  // able to see what `r2` is.
  const MAX_PER_ROLE = 12;
  const perRole = {};
  for (const el of nodes) {
    if (minted >= 300) break;
    if (controls.length >= 60) break;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    if (isChrome(el)) continue;
    const role = roleOf(el);
    if ((perRole[role] || 0) >= MAX_PER_ROLE) continue;
    const ref = 'r' + (minted + 1);
    el.setAttribute('data-lumine-ref', ref);
    perRole[role] = (perRole[role] || 0) + 1;
    minted += 1;
    const label = nameOf(el, role);
    const context = itemContext(el);
    controls.push({
      ref: ref,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      href: (el.getAttribute('href') || '').slice(0, 200),
      label: label,
      context: context,
      role: role,
      // Set when the label was invented rather than read off the page, so a
      // caller can tell "the button named Save" from "the third unnamed button"
      // without guessing.
      synthesised: label === el.tagName.toLowerCase() + ' ' + role
    });
  }
  const body = document.body;
  return {
    title: document.title || '',
    url: location.href,
    // The viewport window leads, the head is the fallback. `browse observe` used
    // to slice the body from character zero on every call, so six observes and
    // four scrolls on one product page all returned the same 127 words of
    // header and the turn ended "I couldn't find the price" -- the price was
    // below the fold and the read never went there. `''` from the helper means
    // this environment cannot answer (no layout yet, the suite's stub DOM), and
    // the head keeps the read a read.
    text: (onScreenText(body, 4000) || (body && body.innerText || '')).slice(0, 4000),
    // Where the read was taken from. Without it a model cannot tell "I read the
    // same text twice" from "the page moved", and `scroll down and read again`
    // is a sentence it has no way to confirm it followed.
    scroll: scrollPosition(),
    controls: controls,
    items: pageItems()
  };
}
"""

#: Inlined into the action scripts. A reference is only meaningful for the
#: document that minted it, and the only cheap way to know which document that
#: is -- across a navigation, a back, a reload, or a single-page app replacing
#: its whole body -- is to ask the document itself.
_JS_STALE = """
  const gen = document.documentElement
    ? document.documentElement.getAttribute('data-lumine-gen')
    : null;
  if (gen !== String(expected)) return { ok: false, reason: 'stale' };
"""

_JS_FACTS = """
([ref, expected]) => {
  const el = document.querySelector('[data-lumine-ref="' + ref + '"]');
  if (!el) return { found: false, url: location.href };
  if (!el.isConnected) return { found: false, url: location.href };
""" + _JS_STALE + """
  const tag = el.tagName.toLowerCase();
  const type = el.getAttribute('type') || '';
  let node = el;
  let inForm = false;
  let form = null;
  while (node) {
    if (node.tagName && node.tagName.toLowerCase() === 'form') { inForm = true; form = node; break; }
    node = node.parentElement;
  }
  // `role`, `formRole` and `soleField` are here for one reason: `web_control`
  // classifies the same control from `state._JS_COLLECT`, and the two tools
  // must reach the same answer. When `web_control` could only see `inForm`,
  // `browse` computed it independently in this script and the two drifted --
  // `state.py` documents the incident. These three close the remaining gap,
  // and none of them is in `sig`, which stays `tag|type|inForm` so every
  // reference already in the wild still verifies.
  const role = (el.getAttribute('role') || '').trim().toLowerCase() || '';
  const formRole = (form && (form.getAttribute('role') || '').trim().toLowerCase()) || '';
  const soleField = (() => {
    if (tag !== 'input' && tag !== 'textarea') return false;
    const kind = (el.getAttribute('type') || 'text').toLowerCase();
    if (!['text', 'search', 'email', 'tel', 'url', ''].includes(kind)) return false;
    if (!form) return false;
    const typed = Array.from(form.querySelectorAll('input, textarea')).filter((other) => {
      const otherType = (other.getAttribute('type') || 'text').toLowerCase();
      return ['text', 'search', 'email', 'tel', 'url', ''].includes(otherType);
    });
    return typed.length === 1 && typed[0] === el;
  })();
  return {
    found: true,
    tag: tag,
    type: type,
    inForm: inForm,
    role: role,
    formRole: formRole,
    soleField: soleField,
    // The `name` attribute -- an identifier the page's author chose, never
    // text a user sees -- because a site too plain to declare `role="search"`
    // still has to be answerable, and the answer is a table keyed on the host.
    field: (el.getAttribute('name') || '').slice(0, 60),
    sig: tag + '|' + type + '|' + (inForm ? '1' : '0'),
    url: location.href,
    label: (
      el.getAttribute('aria-label') ||
      el.getAttribute('placeholder') ||
      el.getAttribute('name') ||
      (el.textContent || '').trim()
    ).slice(0, 80)
  };
}
"""

#: Ask the player whether the page it is sitting on actually has a video.
#:
#: YouTube serves its unavailable page with HTTP 200 and a normal-looking
#: document, so "did the navigation succeed" cannot answer this -- only the
#: player's own verdict can. `playabilityStatus.status` is the authoritative field
#: and it is deliberately read before any text: the unavailable page is localised
#: and the string on it changes, whereas the status does not.
#:
#: Reads nothing and triggers nothing. It runs on a page we have already navigated
#: to, and it is bounded to three fields.
_JS_PLAYABILITY = """
() => {
  const response = window.ytInitialPlayerResponse;
  const status = response && response.playabilityStatus;
  const details = response && response.videoDetails;
  return {
    status: status && status.status ? String(status.status) : '',
    reason: status && status.reason ? String(status.reason) : '',
    videoId: details && details.videoId ? String(details.videoId) : '',
    title: details && details.title ? String(details.title) : ''
  };
}
"""

_JS_CLICK = """
([ref, sig, expected]) => {
  const el = document.querySelector('[data-lumine-ref="' + ref + '"]');
  if (!el) return { ok: false, reason: 'gone' };
""" + _JS_STALE + """
  const tag = el.tagName.toLowerCase();
  const type = el.getAttribute('type') || '';
  let node = el;
  let inForm = false;
  while (node) {
    if (node.tagName && node.tagName.toLowerCase() === 'form') { inForm = true; break; }
    node = node.parentElement;
  }
  const now = tag + '|' + type + '|' + (inForm ? '1' : '0');
  if (now !== sig) return { ok: false, reason: 'changed' };
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return { ok: false, reason: 'not visible' };
  el.scrollIntoView({ block: 'center' });
  el.click();
  return { ok: true };
}
"""

_JS_FILL = """
([ref, value, expected]) => {
  const el = document.querySelector('[data-lumine-ref="' + ref + '"]');
  if (!el) return { ok: false, reason: 'gone' };
""" + _JS_STALE + """
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  // The refusal, at the moment of typing -- see the module docstring.
  if (tag === 'input' && type === 'password') return { ok: false, reason: 'password' };
  if (tag !== 'input' && tag !== 'textarea' && !el.isContentEditable) {
    return { ok: false, reason: 'not fillable' };
  }
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return { ok: false, reason: 'not visible' };
  el.focus();
  if (el.isContentEditable && el.tagName.toLowerCase() !== 'input' && el.tagName.toLowerCase() !== 'textarea') {
    el.textContent = value;
    el.dispatchEvent(new InputEvent('input', { bubbles: true }));
    return { ok: true };
  }
  // The native setter, not `el.value = ...`: a framework like React tracks
  // the property it defines, and a direct assignment is invisible to its
  // change detection, which types the text and sends nothing.
  const proto = tag === 'textarea' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
  if (descriptor && descriptor.set) {
    descriptor.set.call(el, value);
  } else {
    el.value = value;
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: true };
}
"""


_JS_SELECT = """
([ref, sig, wanted, expected]) => {
  const el = document.querySelector('[data-lumine-ref="' + ref + '"]');
  if (!el) return { ok: false, reason: 'gone' };
""" + _JS_STALE + """
  const tag = el.tagName.toLowerCase();
  const type = el.getAttribute('type') || '';
  let node = el;
  let inForm = false;
  while (node) {
    if (node.tagName && node.tagName.toLowerCase() === 'form') { inForm = true; break; }
    node = node.parentElement;
  }
  const now = tag + '|' + type + '|' + (inForm ? '1' : '0');
  if (now !== sig) return { ok: false, reason: 'changed' };
  if (tag !== 'select') return { ok: false, reason: 'not selectable' };
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return { ok: false, reason: 'not visible' };
  const wantedText = String(wanted || '').trim();
  const wantedLower = wantedText.toLowerCase();
  let chosen = null;
  for (const option of el.options) {
    const label = (option.textContent || '').trim();
    if (label === wantedText || label.toLowerCase() === wantedLower || (option.value || '') === wantedText) {
      chosen = option;
      break;
    }
  }
  if (!chosen) return { ok: false, reason: 'no such option' };
  el.focus();
  // The native setter, not `el.value = ...`: the reason `_JS_FILL` does it
  // this way. A framework tracking the property must see the assignment,
  // or the value changes on screen and the page's own state never hears.
  const descriptor = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value');
  if (descriptor && descriptor.set) {
    descriptor.set.call(el, chosen.value);
  } else {
    el.value = chosen.value;
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: true, chosen: (chosen.textContent || '').trim() };
}
"""

_JS_SCROLL = """
(direction) => {
  const root = document.scrollingElement || document.documentElement;
  const max = Math.max(0, (root.scrollHeight || 0) - window.innerHeight);
  let y = window.scrollY || root.scrollTop || 0;
  const step = Math.max(200, Math.round(window.innerHeight * 0.8));
  if (direction === 'top') y = 0;
  else if (direction === 'bottom') y = max;
  else if (direction === 'up') y = Math.max(0, y - step);
  else y = Math.min(max, y + step);
  window.scrollTo(0, y);
  const now = Math.round(window.scrollY || root.scrollTop || 0);
  return { scrolled: max > 0, y: now, max: Math.round(max) };
}
"""


def profile_dir() -> Path:
    """The isolated profile directory.

    An override wins, then the platform's per-user data directory. Nothing
    here points at the user's real browser profile: the whole point of this
    path is that it is somewhere else.
    """
    raw = (os.getenv(PROFILE_VAR, "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "lumine" / "browser-profile"


async def _joined_start_page(context: Any) -> Any:
    """Use an existing blank tab or create one without changing joined pages."""
    pages = list(getattr(context, "pages", []) or [])
    for page in pages:
        try:
            if str(page.url or "") == "about:blank":
                return page
        except Exception:  # noqa: BLE001 -- an unreadable page is not ours to navigate
            continue
    try:
        return await context.new_page()
    except Exception as exc:  # noqa: BLE001 -- do not close a shared context
        raise ToolError(
            f"I couldn't open a browser tab: {clip(str(exc), 100)}"
        ) from None


def headless_default() -> bool:
    return (os.getenv(HEADLESS_VAR, "") or "").strip().lower() in ("1", "true", "yes", "on")


def allow_local() -> bool:
    return (os.getenv(ALLOW_LOCAL_VAR, "") or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Sharing one browser between processes
# ---------------------------------------------------------------------------
#
# Chromium takes an **exclusive lock** on a user-data directory. Two Lumine
# processes therefore could not use one profile: the second failed with "Opening in
# existing browser session", which reads like a product fault and whose real fix is
# "somebody else has it". That is a hard ceiling on a *persistent* profile -- and
# persistent is not optional here, because being signed into WhatsApp is the whole
# reason the browser exists.
#
# So the lock is kept and worked with rather than avoided. The browser is launched
# with a DevTools endpoint, and the endpoint is published next to the profile. Any
# later process **connects to the running browser instead of launching one**, so
# one Chromium serves the worker, the Tauri app, every dispatched room and any
# diagnostic CLI the developer is running.
#
# What this deliberately does *not* do is serialise access. Two processes will
# genuinely drive the same window, which is what "multi-agent" means and also its
# hazard -- so the writes that could collide are still guarded, at the level where
# the collision would matter:
#
# - the WhatsApp send ledger (`whatsapp.py`) already holds a key for a confirmed
#   send and five minutes for an uncertain one, so two rooms cannot both send;
# - `permission_gate` asks per session, so one room's approval is not another's;
# - a *read* is idempotent, which is most of what several agents want at once.
#
# Two processes on the same page can still fight over focus. That is inherent to
# sharing a window and is why `choose_backend` picks one path per operation rather
# than interleaving two.


def _endpoint_file() -> Path:
    """Where the running browser's DevTools endpoint is published."""
    return profile_dir().parent / "browser-cdp.txt"


#: How long `_close_remote` waits for the endpoint it just closed to stop
#: answering. Chromium is still tearing down when `context.close()` returns, and
#: a socket mid-shutdown still accepts a connect -- so one instant probe is not
#: evidence that the browser survived. 2.5 s is far longer than any real teardown
#: (measured: the port refuses immediately once the process is gone, which makes
#: this loop exit on its first check in the normal case) and far shorter than a
#: user would notice as a hang in release.
_ENDPOINT_DEATH_SECONDS = 2.5

#: Spacing between those probes. Small enough that a slow teardown is caught
#: within the deadline rather than being slept past.
_ENDPOINT_POLL_SECONDS = 0.1


#: Launch attempts for a profile lock, including the first one. A lock that is on
#: its way out -- the browser this session just closed -- clears within this; a
#: lock somebody else holds does not, and the refusal arrives as before.
_LAUNCH_ATTEMPTS = 3

#: Delay between those attempts. Short enough that a genuinely held lock costs the
#: user well under two seconds, long enough for a closing Chromium to let go.
_LAUNCH_RETRY_SECONDS = 0.7


def _profile_locked(message: str) -> bool:
    """Chromium's two spellings of "this profile is already in use".

    Lower-cased on both sides: the message comes from the browser, and matching it
    case-sensitively meant a capitalised variant slipped past and became the
    generic "I couldn't start the browser" instead of the sentence that tells the
    user which window to close.
    """
    lowered = str(message or "").lower()
    return "existing browser session" in lowered or "user data directory is already in use" in lowered


async def _launch_persistent(playwright: Any, directory: Path) -> Any:
    """Launch the profile's browser, retrying a lock that is on its way out.

    **A lock that is *being* released looks exactly like one somebody holds**, and
    this is the path that has to reopen the browser in the same session: `release`
    has just closed the previous one, and the teardown it schedules onto the owning
    loop is not awaited by the caller -- so the next `ensure` can arrive while
    Chromium is still letting go of the directory. One attempt would translate that
    into `MSG_PROFILE_LOCKED`, which tells the user to go and close a window that is
    already closing, and the reopen would fail for a reason that is no longer true.

    Only the locked outcome is retried, and only a bounded number of times. A lock
    genuinely held by another window does not clear, and every extra attempt is time
    the user spends waiting for a refusal that was correct on the first try -- so
    `_LAUNCH_ATTEMPTS` and `_LAUNCH_RETRY_SECONDS` keep that under two seconds, and
    anything else (a missing executable, a broken profile) is raised on the first
    attempt exactly as before.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            return await playwright.chromium.launch_persistent_context(
                str(directory),
                headless=headless_default(),
                # Downloads are cancelled at the source: a browser the
                # assistant drives is not a browser that drops files on the
                # user's disk without a conversation about it.
                accept_downloads=False,
                # There is no synthetic viewport: the browser window is the
                # viewport, and its default appearance is explicitly dark.
                no_viewport=True,
                color_scheme="dark",
                args=_window_args()
                # A DevTools endpoint, so another process can *join* this
                # browser instead of failing against its lock. Port 0 lets the
                # OS choose it, because two processes launching at once would
                # otherwise both ask for the same fixed one -- and the endpoint
                # is published from `DevToolsActivePort` once it is known.
                + ["--remote-debugging-port=0"],
            )
        except Exception as exc:  # noqa: BLE001 - re-raised unless locked
            if attempts >= _LAUNCH_ATTEMPTS or not _profile_locked(str(exc)):
                raise
            logger.info(
                "[Browser] the profile is still busy; launch attempt %d/%d in %.1fs",
                attempts + 1,
                _LAUNCH_ATTEMPTS,
                _LAUNCH_RETRY_SECONDS,
            )
            await asyncio.sleep(_LAUNCH_RETRY_SECONDS)


def _publish_endpoint(port: int) -> None:
    target = _endpoint_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"http://127.0.0.1:{port}\n", encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - sharing is an optimisation, not a requirement
        logger.info("[Browser] could not publish the DevTools endpoint: %s", exc)


def _read_endpoint() -> str:
    """The published endpoint, or ``""``.

    Read rather than written by a lock file, and validated against the socket
    rather than trusted: a crashed browser leaves the file behind, and connecting
    to a dead port has to be indistinguishable from there having never been one.
    """
    try:
        text = _endpoint_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text.startswith("http://127.0.0.1:"):
        return ""
    port = text.rsplit(":", 1)[-1].strip()
    if not port.isdigit():
        return ""
    return f"http://127.0.0.1:{port}"


def _endpoint_alive(endpoint: str) -> bool:
    """Whether something is actually listening on the published port."""
    port = str(endpoint or "").rsplit(":", 1)[-1].strip()
    if not port.isdigit():
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.4)
    try:
        probe.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            probe.close()
        except OSError:  # noqa: BLE001
            pass


def _forget_endpoint() -> None:
    try:
        _endpoint_file().unlink()
    except OSError:
        pass


async def _close_page_safe(page: Any, what: str = "a browser tab") -> None:
    """Close one page. Best effort, never raises, and **never under `_lock`**.

    It is a module-level coroutine rather than a method because the caller that
    matters is `release`, and `release` must not hold `_lock` while it awaits.
    `_lock` is a `threading.Lock`, so a second coroutine reaching
    `with self._lock` on the same thread blocks the *event loop* instead of
    waiting its turn -- and the close the first one is awaiting can then never
    finish. Teardown would deadlock against itself, which is the worst possible
    shape for the one path that is not allowed to fail.

    `what` is only ever used in the log line, so a tab that refuses to close
    names itself instead of being an anonymous failure.
    """
    try:
        if page is not None and not page.is_closed():
            await page.close()
    except Exception as exc:  # noqa: BLE001 - a tab that will not close is not fatal
        logger.info("[browser] could not close %s: %s", what, exc)


def sharing_endpoint() -> tuple[str, bool]:
    """The published DevTools endpoint, and whether anything is listening on it.

    **Public, and public deliberately.** A diagnostic CLI at ``agent/`` root imports
    this as ``tools.browser.session``, which under ``PYTHONPATH=agent`` is a
    *different module object* from the ``agent.tools.browser.session`` its tests
    patch. Two copies of the same module means patching one and calling into the
    other, and the test passes or fails for a reason that has nothing to do with
    the code. One function, imported by name, cannot drift like that.

    Returns ``("", False)`` for every unhappy case: no file, nonsense in the file,
    a dead port. Callers only ever need "can I join a browser right now".
    """
    endpoint = _read_endpoint()
    if not endpoint:
        return "", False
    return endpoint, _endpoint_alive(endpoint)


def _discover_devtools_port(profile: Path, attempts: int = 40) -> int:
    """The port Chromium actually bound, from ``DevToolsActivePort``.

    Asked for port ``0`` so the OS picks a free one -- two Lumine processes racing
    to launch must not both try 9222 -- and Chromium writes the real one here.
    Reading the file rather than guessing is what makes "whichever agent got there
    first owns the endpoint" work without a coordinator.
    """
    marker = profile / "DevToolsActivePort"
    for _ in range(attempts):
        try:
            first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
            if first.isdigit():
                return int(first)
        except (OSError, IndexError):
            pass
        time.sleep(0.25)
    return 0


# ---------------------------------------------------------------------------
# How the window looks
# ---------------------------------------------------------------------------
#
# Two things were wrong with the window, both reported as "it looks kinda odd".
#
# **It overflowed the screen.** With no viewport and no window geometry,
# `launch_persistent_context` sizes the window itself, and the result is a window
# that can be wider than the display and offset by whatever was on screen before.
# The fix is to size it to the *screen*, from the screen, and to turn off Playwright
# viewport emulation so the page sees the real window size rather than a
# substituted one.
#
# **It changed the theme.** Playwright's default for `color_scheme` is `light`, so
# the browser rendered every site in light regardless of what the machine is set to
# -- which is why WhatsApp looked unlike the user's own browser. Playwright accepts
# `None` for "emulate nothing", which is the honest setting: the browser follows the
# OS like any other window the user opened.
#
# `device_scale_factor` is left alone deliberately. Forcing 1.0 makes everything
# small on a HiDPI display; leaving it alone makes the window agree with the rest of
# the desktop.


#: Measured once per process. `tkinter.Tk()` opens a real window object, which is
#: slow, and two LiveKit rooms reaching it at once is two windows fighting over the
def _window_args() -> list[str]:
    """Chromium flags for the default browser size, dark theme and playback.
    Playwright already passes `--disable-background-timer-throttling`,
    `--disable-backgrounding-occluded-windows` and `--disable-renderer-backgrounding`
    (read out of the driver's own switch list). The media-suspend flag prevents
    Chromium from suspending audio/video when its window loses focus.
    """
    return [
        "--window-size=960,680",
        "--window-position=0,0",
        "--disable-features=CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-media-suspend",
        "--force-dark-mode",
    ]


class _OwnerLock:
    """Thread-safe mutual exclusion for *ownership decisions* only.

    Why this exists, because the obvious `asyncio.Lock` is a deadlock here.
    This worker is one process serving many rooms, and LiveKit 1.8.3 runs each
    dispatched room in **its own thread with its own event loop**
    (`_JobProc`/`ProcClient.run` calls `asyncio.new_event_loop()` per job). The
    browser, meanwhile, is deliberately a single process-wide singleton -- one
    window the user can see, one profile on disk.

    So two rooms can reach `ensure()` at once, from two threads, on two loops.
    An `asyncio.Lock` cannot arbitrate that:

    - CPython's uncontended `acquire()` takes a fast path that does **not** bind
      the lock to a loop, so whichever thread wins the race owns a lock that
      records no loop at all;
    - the loser then binds that same lock to *its own* loop and parks on a
      future belonging to that loop;
    - when the winner releases, `Future.set_result` is called **directly on the
      loser's future from the winner's thread**. `asyncio` only wakes another
      loop's callback queue through `call_soon_threadsafe`, which this path does
      not use, so the loser's selector never wakes again.

    The waiter then never returns. That is the reported bug: Lumine's browser
    "keeps crashing when she tried to open" -- in truth the opening call hangs
    forever, holding the room, so nothing further is ever heard.

    A `threading.Lock` is correct here for one reason: it is only ever held for
    the few statements that read or write the ownership fields, never across an
    `await`. The slow path (actually starting Chromium) runs *outside* it, and
    the ownership token it stamps is what keeps two rooms from launching two
    browsers on one profile directory.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self) -> "_OwnerLock":
        self._lock.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


def _is_gone(page: Any) -> bool:
    """Whether a page is finished with, treating "cannot ask" as finished.

    A page object that has lost its connection raises rather than answering
    ``is_closed()``, and a page we have already torn down answers ``True``. Both
    mean the same thing to every caller here -- this tab is not available -- and
    treating an exception as "still open" would leave the registry pointing at a
    page that cannot be driven.
    """
    if page is None:
        return True
    try:
        return bool(page.is_closed())
    except Exception:  # noqa: BLE001 - a page we cannot ask about is one we cannot use
        return True


class _Tab:
    """One tab the model may look at, and the document generation it holds.

    ``generation`` is **per tab**, which is the whole reason this class exists.

    References are stamped into the document that minted them
    (``data-lumine-gen``) and every later action compares against that stamp, so
    a counter shared by the browser has a failure mode tabs introduce: observe
    tab 1, open tab 2, and the counter has moved -- so tab 1's perfectly good
    references now read as stale and every action on it is refused. The model
    would be told to observe a page it had just observed.

    One counter per tab, with every number still unique across the browser,
    keeps the stale check exactly as strict as it was with a single page while
    letting a tab keep its references while the model works in another one. The
    DOM check is untouched and still does the real work: it compares against the
    stamp in the document itself, so a navigation, a back, or a single-page app
    replacing its body all invalidate the same way they always did.
    """

    __slots__ = ("key", "page", "generation", "last_read")

    def __init__(self, key: int, page: Any, generation: int) -> None:
        self.key = key
        self.page = page
        self.generation = generation
        # A digest of the last read this tab produced -- url, scroll position and
        # text -- so a second observe that returns the *same* page can say so.
        # Six observes and four scrolls returning identical header text was the
        # measured failure; a model that is not told it is re-reading the same
        # thing keeps re-reading it.
        self.last_read: str | None = None

    def url(self) -> str:
        """The tab's address, or ``""`` -- a page mid-navigation reads as none."""
        try:
            return str(self.page.url or "")
        except Exception:  # noqa: BLE001 - see `_is_gone`
            return ""


class _Browser:
    """The one browser: started on first use, torn down by `release`.

    **Tabs, not one page.** ``_tabs`` holds every page the model may look at and
    ``_active`` names the one it is looking at; ``_page`` is that tab's page,
    mirrored here because it is the handle every operation on the page is made
    through. The WhatsApp tab is deliberately *not* in the list: it belongs to
    the messaging tools, and a tab list the model can see is a promise it can
    drive, which would be a false one for that page.

    This started as two pages -- one for ``browse`` and one for WhatsApp -- and
    the second was the only reason multi-tab existed at all. The first one is the
    interesting half, because the old rule for a page the browser opened was to
    adopt it and *close* the previous one: a popup was a replacement rather than a
    second page, so opening one was a way to lose what you had. "Open this in a
    new tab", "compare these two pages", "keep the search results while I go back"
    were all impossible, and the workaround -- reading one page, writing down
    what mattered, navigating -- is what makes a multi-step browse feel unreliable.

    Every page now becomes a tab. Nothing the browser opens is invisible, and
    closing one is a decision the model makes by name.
    """

    def __init__(self) -> None:
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None
        self._wa_page: Any = None
        #: Every page the model may act on, and which one it is looking at.
        #: See the class docstring; ``_page`` mirrors ``_tabs[_active]``.
        self._tabs: list[_Tab] = []
        self._active: int | None = None
        self._next_key = 0
        #: Whether **this process** launched the browser *and* put it in the
        #: taskbar. It is the answer to "may I hide this window?", and it exists
        #: because the first version of the warm-up asked the wrong question: it
        #: hid whatever window it found. Measured, with a video playing in it --
        #: `whatsapp_page(focus=False)` on a browser that was already open and
        #: visible left the window MINIMIZED, so a background load moved the
        #: user's own window out from under them mid-call. A warm-up is allowed
        #: to keep *its own* window quiet; it is never allowed to hide a window
        #: that was already on screen.
        self._hid_window = False
        #: Whether this process *joined* a browser somebody else launched.
        #:
        #: It decides three things, all of them about not doing damage to another
        #: agent: `release` detaches instead of closing, `release` leaves the
        #: endpoint published so the next process can still find the browser, and
        #: the WhatsApp tab and every other page are left alone because they are
        #: somebody else's. A process that launched the browser owns it; a process
        #: that joined it is a guest, and a guest does not tidy up the house.
        self._shared = False
        #: The `Browser` handle from `connect_over_cdp`, when we joined one. Not the
        #: context everything else works with -- kept so `_close_remote` can tell
        #: "detach from the browser" from "close the context", which for a CDP
        #: connection are very different operations.
        self._joined_browser: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Guards the ownership fields above. A `threading.Lock`, *not* an
        #: `asyncio.Lock` -- see `_OwnerLock` for why an asyncio lock deadlocks
        #: when two rooms reach this at once. Never held across an `await`.
        self._lock = _OwnerLock()
        #: The loop that owns `_start_lock` / `_wa_lock`. Those two are ordinary
        #: `asyncio.Lock`s because within a single loop they are correct and do
        #: what they are for -- stopping two coroutines on that loop from both
        #: launching a browser. They are only ever awaited on the loop recorded
        #: here, and are rebuilt whenever ownership moves, which is what keeps
        #: them from ever being shared across threads.
        self._owner_token: object = None
        self._start_lock: asyncio.Lock | None = None
        self._wa_lock: asyncio.Lock | None = None
        self._dns: dict[str, tuple[float, list[str]]] = {}
        #: Watch URLs this session has actually been shown, gathered from the
        #: pages themselves rather than from anything the model said. See
        #: :meth:`_watch_verdict` -- this is the memory that lets that check say
        #: "assembled rather than read" instead of only "does not work", and it is
        #: a set of ids rather than of full URLs because the id is the whole of
        #: the claim.
        self._seen_video_ids: set[str] = set()
        #: Which document each tab's references belong to. A single monotonic
        #: counter that never resets -- a fresh number is the point -- but handed
        #: out *per tab*, so a tab keeps its own references while the model works
        #: in another. See :class:`_Tab` for why sharing one number broke that.
        self._generation = 0

    # -- the tab registry ---------------------------------------------------

    def _mint(self) -> int:
        """The next document number. Unique across every tab in this browser."""
        self._generation += 1
        return self._generation

    def _new_document(self) -> int:
        """Invalidate the active tab's outstanding references; return the number.

        Called after a navigation *we* performed, which is the one moment we know
        the document changed for certain. Only the active tab is stamped: the
        tabs behind it did not move, and invalidating them would refuse correct
        work on a page nobody touched.
        """
        number = self._mint()
        tab = self._active_tab()
        if tab is not None:
            tab.generation = number
        return number

    def _active_generation(self) -> int:
        """The number the *active* tab's references were minted with.

        Falls back to the counter when no tab is registered -- a page set
        directly on the object, or a browser taken over from a dead loop -- so
        this is always a number that has been issued and never one that has not.
        """
        tab = self._active_tab()
        return tab.generation if tab is not None else self._generation

    def _tab_of(self, page: Any) -> _Tab | None:
        """The tab holding ``page``, matched by identity."""
        for tab in self._tabs:
            if tab.page is page:
                return tab
        return None

    def _active_tab(self) -> _Tab | None:
        if self._active is None:
            return None
        for tab in self._tabs:
            if tab.key == self._active:
                return tab
        return None

    def _tab_by_key(self, key: object) -> _Tab | None:
        """The tab the model named, by number.

        Digits only, and no fallback to "the only one open": a target that
        resolves by guessing is a click landing on a page nobody chose.
        """
        text = str(key if key is not None else "").strip()
        if not text.isdigit():
            return None
        wanted = int(text)
        for tab in self._tabs:
            if tab.key == wanted:
                return tab
        return None

    def _activate(self, tab: _Tab) -> None:
        """Make ``tab`` the one every page operation works on."""
        self._active = tab.key
        self._page = tab.page

    def _step(self, offset: int) -> "_Tab | None":
        """The tab ``offset`` places away from the one in use, wrapping round.

        Wrapping because that is what a browser does and what "the next tab"
        means when you are on the last one: there is no such thing as no next
        tab, so refusing would be inventing a rule the user did not ask for.
        """
        if not self._tabs:
            return None
        index = next(
            (i for i, tab in enumerate(self._tabs) if tab.key == self._active), 0
        )
        return self._tabs[(index + offset) % len(self._tabs)]

    def _resolve_target(self, which: object) -> "_Tab | None":
        """The tab a request names, however it named it.

        Four spellings, because the request arrives as a sentence rather than a
        number: nothing at all means the tab in use, ``+1``/``-1`` mean next and
        previous, ``last`` means the last one opened, and digits mean that tab.
        ``last`` is its own marker and not ``-1`` because "the last tab" is a
        *specific* tab and "the previous one" is a direction.
        """
        self._prune()
        text = str(which if which is not None else "").strip()
        if not text:
            return self._active_tab()
        if text == "+1":
            return self._step(1)
        if text == "-1":
            return self._step(-1)
        if text == "last":
            return self._tabs[-1] if self._tabs else None
        return self._tab_by_key(text)

    def _register(self, page: Any, *, activate: bool = True) -> _Tab:
        """Put a page in the registry, or return the tab that already holds it.

        A fresh tab starts on its own document number, so a reference minted on
        it can never be mistaken for one minted elsewhere.
        """
        existing = self._tab_of(page)
        if existing is not None:
            if activate:
                self._activate(existing)
            return existing
        self._next_key += 1
        tab = _Tab(self._next_key, page, self._mint())
        self._tabs.append(tab)
        if activate or self._active is None:
            self._activate(tab)
        return tab

    def _drop(self, tab: _Tab) -> None:
        """Forget a tab and close its page, leaving a real one active."""
        self._tabs = [other for other in self._tabs if other.key != tab.key]
        if self._active == tab.key:
            self._active = None
            self._page = None
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.create_task(self._close_quietly(tab.page))
            except RuntimeError:  # pragma: no cover - loop closed between the checks
                pass

    def _prune(self) -> _Tab | None:
        """Forget tabs whose page has gone, and make sure the active one is real.

        Returns the active tab, or ``None`` when the browser is left with none.
        A tab dies whenever the user closes it in the window themselves -- the
        window is theirs, not ours -- so this is not bookkeeping, it is how the
        registry notices that the tab it is pointing at no longer exists.
        """
        alive = [tab for tab in self._tabs if not _is_gone(tab.page)]
        if len(alive) != len(self._tabs):
            self._tabs = alive
        if not self._tabs:
            # `_page` can outlive the list: a browser taken over from a dead loop
            # keeps whatever it had, and a page set on the object directly was
            # never registered. Adopt it rather than dropping a working page.
            if not _is_gone(self._page):
                return self._register(self._page, activate=True)
            self._active = None
            self._page = None
            return None
        active = self._tab_of(self._page)
        if active is None:
            # Whatever `_page` points at is gone; the last tab we still hold is
            # the one the model was most recently working in.
            self._activate(alive[-1])
            return alive[-1]
        if active.key != self._active:
            self._active = active.key
        return active

    def _reserve(self, page: Any) -> None:
        """Take a page out of the model's tabs and hand it to the messaging path.

        Called *after* the page object exists rather than around the call that
        creates it, and that ordering is the point. ``context.new_page()`` raises
        Playwright's ``page`` event before it returns, so a flag set around the
        call is a guess about when the event lands; measured, it fires while the
        ``await`` is still outstanding. Reclassifying the page afterwards cannot
        miss, and it costs one list removal.

        Without this the WhatsApp tab was adopted as the working page -- it
        arrived as an unregistered page, so `_on_page` treated it like a popup --
        and the page the user had open was closed behind them. Measured indirectly
        and directly: every messaging call site creates the tab this way.
        """
        tab = self._tab_of(page)
        if tab is not None:
            self._tabs = [other for other in self._tabs if other.key != tab.key]
            if self._active == tab.key:
                self._active = None
                self._page = None
                self._prune()

    # -- the guard ---------------------------------------------------------

    async def _resolve(self, host: str) -> list[str]:
        """Addresses for a host, cached. Literal IPs short-circuit.

        A **failure** is cached for :data:`DNS_FAIL_CACHE_SECONDS` rather than
        :data:`DNS_CACHE_SECONDS`. Both facts matter and neither is decoration:

        - YouTube's media arrives from rotating segment hosts, so a stream
          meets a *new* host partway through. One transient miss there used to
          be remembered for a minute, and every segment from that host was
          refused for it -- the reported ~49-second playback stop.
        - The lookup itself is bounded by :data:`DNS_TIMEOUT_SECONDS`, because
          an unbounded ``getaddrinfo`` on the request path is a page that waits
          on the resolver rather than on the network.
        """
        host = host.strip("[]")
        try:
            import ipaddress

            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        now = time.monotonic()
        cached = self._dns.get(host)
        if cached:
            age = now - cached[0]
            if not cached[1]:
                # Empty answer: a failure. Short window only.
                if age < DNS_FAIL_CACHE_SECONDS:
                    return []
            elif age < DNS_CACHE_SECONDS:
                return cached[1]
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, None, type=socket.SOCK_STREAM),
                timeout=DNS_TIMEOUT_SECONDS,
            )
            addresses = list(dict.fromkeys(info[4][0] for info in infos))
        except (socket.gaierror, OSError, asyncio.TimeoutError):
            addresses = []
        self._dns[host] = (now, addresses)
        return addresses

    async def allowed(self, url: str) -> tuple[bool, str]:
        """``(may this URL be fetched, why not)`` -- the same check for the
        tool's own navigation and for every request the page makes."""
        try:
            parts = urlsplit(str(url or ""))
        except ValueError:
            return False, "it is not a readable address"
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False, "only web pages (http or https) are opened"
        host = (parts.hostname or "").casefold()
        if not host:
            return False, "it has no host"
        addresses = await self._resolve(host)
        return host_verdict(host, addresses, allow_local=allow_local())

    async def _guard(self, route: Any, request: Any) -> None:
        """The route handler: one door for navigation, images, XHR, iframes."""
        url = request.url
        scheme = (urlsplit(url).scheme or "").lower()
        if scheme in ("data", "blob", "about"):
            # Inline content makes no request to anywhere. As a *navigation*
            # target it is never what we were asked to open.
            if request.is_navigation_request():
                await route.abort()
            else:
                await route.continue_()
            return
        # Ads and trackers go first, and *before* the DNS check: a request that
        # is blocked here never leaves the machine, which is the only version of
        # "blocking" that a tracker cannot observe around. Everything else takes
        # the same host check it always did.
        if adblock.should_block(url):
            await route.abort()
            return
        ok, _why = await self.allowed(url)
        if ok:
            await route.continue_()
        else:
            await route.abort()

    # -- lifetime ----------------------------------------------------------

    def _on_page(self, page: Any) -> None:
        """A page the browser opened becomes a tab of its own.

        The old rule was the opposite: adopt the newest page and **close** the
        previous one, so a popup was a replacement rather than a second page. The
        justification -- "popups would mean pages the model cannot observe
        holding state it cannot see" -- was right and the remedy did not follow
        from it. Keeping the page *and* listing the popup answers it properly, and
        it is what makes "open this in a new tab" possible at all.

        A popup becomes the active tab rather than merely joining the list,
        because it is what the model's own click produced and the reference it
        holds next will be against it. It is in the list, so nothing is hidden.

        The WhatsApp page is the exception and still has to be: WhatsApp opens
        windows of its own for things like "open in WhatsApp Desktop", and those
        belong to the messaging tools rather than to `browse`. `_reserve` takes
        our own WhatsApp tab out of the list once it exists, so the identity check
        here and the reclassification there agree whichever order they land in.

        At the ceiling, the oldest tab that is *not* the one in use goes: a site
        that opens a window per click cannot grow the registry without bound, and
        the tab being worked in is the one whose references we must not break.
        """
        if page is self._wa_page or self._tab_of(page) is not None:
            return
        if len(self._tabs) >= MAX_WORK_TABS:
            for tab in self._tabs:
                if tab.key != self._active:
                    self._drop(tab)
                    break
        self._register(page, activate=True)

    @staticmethod
    async def _close_quietly(page: Any) -> None:
        try:
            await page.close()
        except Exception:  # noqa: BLE001 -- teardown of a page we are discarding
            pass

    def _adoptable_blank_page(self) -> Any:
        """A blank page we made ourselves and nobody is using, or ``None``.

        Starting the browser leaves exactly one page, blanked to `about:blank` on
        purpose ("a persistent profile must not mean persistent tabs"). When the
        *first* thing anybody asks for is WhatsApp, that blank page is dead weight
        for the whole conversation -- and the user sees a window with an empty tab
        beside WhatsApp, which reads as two windows when it is one.

        So the messaging tab adopts it rather than opening a second one. The
        conditions, and why each is enough:

        - **Blank, and blank is why this is safe.** A blank page has no document,
          so there is nothing in it that could carry a `data-lumine-ref`. Adopting
          it cannot invalidate a reference the model is holding -- which is the
          reason the obvious version of this check ("is it a registered tab?")
          refused, since `_start` registers that first page like any other. There
          is nothing in an `about:blank` to have referenced.
        - **Only when it is the only tab.** Taking a blank page out of a set the
          model is working with would renumber everything else.
        - **We launched this browser.** A *joined* browser's pages belong to
          somebody else. This is the same rule as not closing their tabs and not
          hiding their window: a blank page in a browser we joined may be the new
          tab they just opened.
        """
        if self._shared:
            return None
        page = self._page
        if page is None:
            return None
        try:
            if page.is_closed():
                return None
            url = (page.url or "").strip().lower()
        except Exception:  # noqa: BLE001 - a page we cannot read is not adoptable
            return None
        if url not in ("", "about:blank"):
            return None
        if len(list(getattr(self, "_tabs", ()) or ())) > 1:
            return None
        return page

    def _forget_tab(self, page: Any) -> None:
        """Drop ``page`` from the tab registry. Best effort, never raises.

        Called only when the page is being adopted for something else. It has to
        be dropped rather than left: `_reserve` treats an already-registered page
        as done, so without this the adopted page would stay *tab 1* -- blank, and
        listed -- while also being the WhatsApp tab. The model would be told about
        an empty tab it never asked for.
        """
        try:
            tabs = getattr(self, "_tabs", None)
            if tabs is None:
                return
            for tab in [t for t in list(tabs) if getattr(t, "page", None) is page]:
                tabs.remove(tab)
            if getattr(self, "_active", None) is not None and (
                getattr(self._active, "page", None) is page
            ):
                self._active = None
        except Exception:  # noqa: BLE001 - the registry is bookkeeping, not the work
            logger.debug("[browser] could not drop an adopted tab", exc_info=True)

    async def _page_on(self, context: Any) -> Any:
        """One more page on a context we already hold, or a sentence."""
        try:
            return await context.new_page()
        except Exception as exc:  # noqa: BLE001 -- translated into a sentence
            raise ToolError(f"I couldn't open a browser tab: {clip(str(exc), 100)}") from None

    def _existing_whatsapp_tab(self, context: Any) -> Any:
        """The WhatsApp tab **already open** on this context, or ``None``.

        Creating one without looking first is how a persistent worker ends up with
        one WhatsApp tab per conversation.

        The worker is long-lived and `_wa_page` is a *reference*, not an
        ownership record. Anything that drops the reference without closing the
        tab -- a release, a prune, a room that died, or simply joining a browser
        somebody else already had open -- leaves a WhatsApp tab behind that the
        next conversation cannot see and will not reuse. So it makes another, and
        the next one after that.

        Measured shape of the complaint: the tabs multiply until several copies of
        WhatsApp are open at once, the search runs against one of them while the
        press lands in another, and the assistant's own reaction to the failure is
        to open yet another tab. Three symptoms, one leak.

        So adoption is the primary defence -- asking first costs one cheap read of
        ``context.pages`` -- and :meth:`release` closing what it made is the
        backstop. Either alone leaves a way to accumulate.
        """
        try:
            pages = list(getattr(context, "pages", None) or [])
        except Exception:  # noqa: BLE001 - a context that cannot be listed is "none"
            return None
        for page in pages:
            try:
                if not page.is_closed() and _is_whatsapp(page.url):
                    return page
            except Exception:  # noqa: BLE001 - a dead page is simply not it
                continue
        return None

    async def ensure(self, *, background: bool = False) -> Any:
        """The active page, starting the browser if this is its first use.

        ``background`` is the difference between a browser the user asked for and
        one a warm-up asked for. It only changes the window: a background start
        comes up minimized, so a pre-load never throws a window over whatever the
        user is doing mid-sentence. It is not about permission -- both paths get
        a real browser -- and it is ignored when we *joined* a running one,
        because that window belongs to somebody else.

        Ownership is decided under `_lock` (a `threading.Lock`, so it is correct
        across the threads LiveKit gives each room) and the launch happens
        outside it, because `_start` is `await`ed many times over and holding a
        thread lock across an `await` would park every other room behind a
        browser start.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is not None and self._loop is not loop:
                # Playwright's objects belong to the loop that made them, so a
                # call from a different loop must not reach into them. But
                # "different" is not the same as "refuse forever":
                #
                # - the owning loop is still *running* -> a real conversation
                #   is using this browser right now, and MSG_OTHER_LOOP is the
                #   honest answer;
                # - the owning loop is closed or stopped -> that room is gone
                #   and nothing will ever release its browser. Refusing here is
                #   what turned one crashed room into a worker that could never
                #   browse again, so the abandoned references are dropped and
                #   this room takes over. The old Chromium died with its loop.
                if self._loop.is_running() and not self._loop.is_closed():
                    raise ToolError(MSG_OTHER_LOOP)
                self._context = self._page = self._pw = None
                self._wa_page = None
                self._tabs = []
                self._active = None
                self._loop = None
                self._owner_token = None
                self._start_lock = None
                self._wa_lock = None
            # The page we would hand out is settled here rather than at every
            # call site: the window is the user's, so a tab can be closed from
            # its toolbar at any moment, and a registry entry for a page that no
            # longer exists would otherwise sit there counting against the tab
            # ceiling and get listed as something the model can switch to.
            active = self._prune()
            if active is not None:
                return active.page
            # Rebuild the in-loop locks if ownership moved, so a lock created
            # on a dead loop is never awaited on this one.
            if self._owner_token is not loop:
                self._owner_token = loop
                self._start_lock = asyncio.Lock()
                self._wa_lock = asyncio.Lock()
        # Two coroutines *on this loop* can still race to start it; that is what
        # the asyncio lock is for, and it is safe because it was just built for
        # this loop. One holds it across the start, the other waits and then
        # finds the page already there.
        start_lock = self._start_lock
        if start_lock is None:  # pragma: no cover - set above, read back defensively
            start_lock = self._start_lock = asyncio.Lock()
        async with start_lock:
            active = self._prune()
            if active is not None:
                return active.page
            if self._context is not None:
                # The browser is running; it is the *tab* that ran out, which is
                # what closing the last one looks like. Going back to `_start`
                # here would launch a second Chromium against a profile the first
                # one still holds, and fail with "existing browser session" -- the
                # exact sentence `MSG_PROFILE_LOCKED` exists to translate.
                page = await self._page_on(self._context)
                return self._register(page, activate=True).page
            return await self._start(loop, background=background)

    async def _start(self, loop: asyncio.AbstractEventLoop, *, background: bool = False) -> Any:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ToolError(MSG_NOT_INSTALLED) from None

        directory = profile_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(
                f"I can't use {clip(str(directory), 60)} for the browser profile: {clip(str(exc), 80)}"
            ) from None

        playwright = await async_playwright().start()
        shared = False
        try:
            # Share the browser if one is already running for this profile.
            #
            # Chromium holds an exclusive lock on the profile directory, so a
            # second `launch_persistent_context` cannot succeed -- and it failed
            # with Chromium's own paragraph about sessions, which reads like a
            # product fault. Connecting to the running one instead is what makes
            # the browser usable by the worker, the desktop app, every dispatched
            # room and a diagnostic CLI at the same time. See `_endpoint_file`.
            existing = _read_endpoint()
            if existing and _endpoint_alive(existing):
                try:
                    # `connect_over_cdp` returns a **Browser**, not a
                    # BrowserContext -- and everything below this line is written
                    # against a context (`route`, `on("page")`, `pages`).
                    #
                    # Asking the joined browser for a context and handing that back
                    # keeps one shape for the rest of the function. It also makes
                    # the mistake visible: with the Browser passed straight
                    # through, the very next line raises
                    # `AttributeError: 'Browser' object has no attribute 'route'`
                    # -- and the failure handler then stops Playwright, which tears
                    # down a browser belonging to *another process*. Verified by
                    # doing it: the holder's nine Chromium processes went to zero.
                    joined = await playwright.chromium.connect_over_cdp(existing)
                    contexts = list(getattr(joined, "contexts", []) or [])
                    if not contexts:
                        raise ToolError(
                            "The Lumine browser I found is not serving any window I "
                            "can use. Nothing was sent."
                        )
                    context = contexts[0]
                    self._joined_browser = joined
                    shared = True
                    logger.info("[Browser] joined the running browser at %s", existing)
                except ToolError:
                    raise
                except Exception as exc:  # noqa: BLE001 - fall through and launch
                    logger.info("[Browser] could not join %s (%s); launching", existing, exc)
                    _forget_endpoint()
                    context = None
            else:
                if existing:
                    # A crashed browser leaves its endpoint behind; treating that
                    # as "none" is the difference between recovering and refusing
                    # forever.
                    _forget_endpoint()
                context = None

            if context is None:
                context = await _launch_persistent(playwright, directory)
                port = _discover_devtools_port(directory)
                if port:
                    _publish_endpoint(port)
                    logger.info("[Browser] published a DevTools endpoint on %s", port)
        except Exception as exc:  # noqa: BLE001 -- any launch failure becomes a sentence
            # Detach, do not shut down, when we joined somebody else's browser.
            #
            # `playwright.stop()` on a CDP connection closes the *remote* browser.
            # Doing that in a failure handler -- which is where every unexpected
            # shape arrives -- means the first error in a joining process kills the
            # browser the agent that owns it is using. Measured: a holder with nine
            # Chromium processes was at zero the moment a second process failed to
            # attach. A launch failure is ours; the browser is not.
            if not shared:
                try:
                    await playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
            message = str(exc)
            if "Executable" in message or "executable" in message:
                raise ToolError(MSG_NOT_INSTALLED) from None
            if _profile_locked(message):
                # The profile is locked, which means a Lumine browser window is
                # already open somewhere -- usually the Tauri app, or a probe that
                # did not exit cleanly. Chromium's own wording here is a paragraph
                # about profiles and about "Opening in existing browser session",
                # and the actionable sentence is one line long.
                raise ToolError(MSG_PROFILE_LOCKED) from None
            raise ToolError(
                f"I couldn't start the browser: {clip(message, 120)}"
            ) from None

        # Start on a blank page, and *reuse* a page the browser already opened
        # rather than closing everything and immediately asking for a new tab.
        #
        # The old shape closed every existing page and then called `new_page()`.
        # On a persistent context, closing the last page starts Chromium tearing
        # the window down, and the `new_page()` that follows races that teardown:
        # it fails with "Protocol error (Target.createTarget): Failed to open a
        # new tab". That is not a test artefact -- it is the *browser refusing to
        # start at all*, intermittently, which on a voice turn reads as "she can't
        # access the browser".
        #
        # Reusing the first page and blanking it gets the same guarantee -- a
        # persistent profile must not mean persistent *tabs* -- without ever
        # leaving the context with zero pages, which is the state Chromium does
        # not like.
        pages = list(getattr(context, "pages", []) or [])
        if shared:
            # A joined context belongs to another process and may contain the
            # user's active page. Reuse only an existing blank tab; otherwise
            # create a Lumine tab instead of navigating somebody else's page to
            # about:blank.
            try:
                page = await _joined_start_page(context)
            except ToolError:
                await self._close_remote(context, playwright, shared=True)
                raise
        elif pages:
            page = pages[0]
            for extra in pages[1:]:
                try:
                    await extra.close()
                except Exception:  # noqa: BLE001 -- best effort
                    pass
            try:
                await page.goto("about:blank")
            except Exception:  # noqa: BLE001 -- a fresh tab is still usable
                pass
        else:
            try:
                page = await context.new_page()
            except Exception as exc:  # noqa: BLE001 -- translated into a sentence
                try:
                    await context.close()
                    await playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                raise ToolError(
                    f"I couldn't open a browser tab: {clip(str(exc), 100)}"
                ) from None

        await context.route("**/*", self._guard)
        context.on("page", self._on_page)
        # Cosmetic hiding, best effort. A stylesheet that fails to install
        # leaves a slightly uglier page; one that *raises* here would leave no
        # browser at all, which is a far worse trade than the ads it hides.
        if adblock.enabled():
            try:
                await context.add_init_script(adblock.COSMETIC_INIT_JS)
            except Exception:  # noqa: BLE001 -- cosmetic only, never fatal
                logger.debug("[adblock] cosmetic stylesheet not installed")

        self._pw = playwright
        self._context = context
        self._loop = loop
        self._shared = shared
        # Registered rather than assigned, so the first tab is a tab like any
        # other and holds a document number of its own.
        self._register(page, activate=True)
        self._hid_window = False
        if background and not shared:
            await self._minimize_window()
        return page

    async def _minimize_window(self) -> None:
        """Put the window in the taskbar instead of over the user's work.

        **Chromium's own ``--start-minimized`` does nothing here.** Measured on
        this machine against a headed launch with that flag: the window came up
        ``normal`` every time, with and without ``--window-position``. Playwright
        owns the window geometry on a persistent context, so the flag is inert --
        which is worth writing down, because it is the obvious thing to reach for
        and building on it would have shipped a popup that still pops.

        So the window is minimized after the fact, over CDP: ``getWindowForTarget``
        to find the window this page is in, then ``setWindowBounds``. A protocol
        method rather than a guess about window geometry or a scan for a window
        title, and it only ever touches the window the page we launched it into
        is in.

        Two things it deliberately does not do. It does nothing when headless --
        there is no window to move. And it does nothing to a browser we *joined*:
        that window is somebody else's, and a background load is not licence to
        hide a window the user or another agent is using. Both measured; see
        :meth:`ensure`.

        Best-effort. A window we failed to hide is the old behaviour, which is a
        cosmetic problem -- never a refusal, because the page loaded either way.

        **Only for a window this process started.** `_hid_window` is the guard, and
        it is not about permission -- the caller asked for no focus either way. It
        is that a background load must not move a window the user already has on
        screen. See `_Browser._hid_window` for what that looked like when it was
        missing.
        """
        context, page = self._context, self._page
        if context is None or page is None or headless_default():
            return
        try:
            session = await context.new_cdp_session(page)
            try:
                info = await session.send("Browser.getWindowForTarget")
                window_id = int(info["windowId"])
                await session.send(
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "minimized"}},
                )
                self._hid_window = True
                logger.info("[Browser] started in the background; window minimized")
            finally:
                await session.detach()
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.info("[Browser] could not minimize the background window: %s", exc)

    async def _reveal(self, page: Any) -> None:
        """Bring a tab forward because the user asked to *see* something.

        The counterpart to :meth:`_minimize_window`, and it is what makes that
        safe: a background start leaves the window in the taskbar, so the first
        thing anybody does with it has to bring it back. ``browse open`` does,
        and so does switching tabs -- both are requests to look at a page, which
        is the one thing a minimized window cannot deliver.

        Measured, and load-bearing: ``bring_to_front`` on a minimized Chromium
        window restores it to ``normal``. That is why nothing else has to restore
        it, and why the WhatsApp send flow -- which has always focused its tab
        before asking the user to approve a draft -- keeps working against a
        window a warm-up hid.

        Best-effort, like `_focus_whatsapp`: a window manager that refuses must
        not turn a working open into a refusal.
        """
        if page is None:
            return
        # The window is on screen now, so it stops being ours to hide. Without
        # this a warm-up that ran *after* the user had been shown something would
        # minimize it again on its next tab, which is the same bug one step later.
        self._hid_window = False
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001 - see the docstring
            logger.debug("[Browser] could not bring the tab forward", exc_info=True)

    # -- the WhatsApp page -------------------------------------------------
    #
    # Separate tab, same profile, so a QR login survives between conversations
    # exactly as it does for anything else in this browser. What it must not be
    # is the page `browse` drives -- see `_on_page`.

    async def whatsapp_page(self, *, navigate: bool = True, focus: bool = True) -> Any:
        """The dedicated WhatsApp tab, opened on web.whatsapp.com.

        Idempotent in the way the user experiences: the first call pays for
        starting Chromium and loading WhatsApp, and every call after that returns
        the tab that is already there. A WhatsApp popup does not become this page
        -- WhatsApp opens its own window for several actions, and adopting one
        would mean the next call drove a window the user cannot see.

        Navigation is skipped when the tab is already on WhatsApp, which is the
        common case and the difference between a warm call costing nothing and a
        warm call reloading a 4 MB application.

        ``focus=False`` is what a **background** caller asks for, and it is the
        fix for a window appearing over the user mid-sentence. The pre-warm runs
        on every conversation while the greeting is still being spoken, and it
        used to call straight into this: a headed Chromium launched, WhatsApp
        started loading, and ``bring_to_front`` pulled the whole thing to the
        front of the screen. Every call, on every machine, whether or not the
        person on the line ever sends a message -- and it happened *while they
        were mid-sentence*, which is the worst possible moment for a 900x700
        window to appear.

        So a caller that does not want the window passes ``focus=False``, and
        two things follow from it: nothing is brought forward, and a browser that
        has to be *started* comes up minimized (see :meth:`ensure`). The tab is
        still opened, still navigated, still logged in -- it is only the window
        that stays out of the way. And the focus on a *real* request is
        untouched: the send flow still brings the tab forward before asking the
        user to approve a draft, and ``bring_to_front`` restores a minimized
        window, so this costs the send path nothing.
        """
        loop = asyncio.get_running_loop()
        warm: Any = None
        with self._lock:
            if self._loop is not None and self._loop is not loop:
                raise ToolError(MSG_OTHER_LOOP)
            if self._wa_page is not None and not self._wa_page.is_closed():
                if not navigate or _is_whatsapp(self._wa_page.url):
                    # Decided here, acted on below. This path was returning without
                    # focusing at all -- the two focus calls the old test counted
                    # were both further down, inside the WA lock -- so the cheapest
                    # path, and the one every send takes after the first, handed
                    # over a tab sitting behind another one. The user was then
                    # asked to approve a message they could not see.
                    #
                    # Only the *decision* happens under the owner lock. The focus
                    # itself must not: `_OwnerLock` is a `threading.Lock` held
                    # strictly between field reads and writes and never across an
                    # `await`, and awaiting `bring_to_front` while holding it
                    # reintroduces exactly the two-room deadlock that rule exists
                    # to prevent -- a `threading.Lock` blocks the whole thread, so
                    # the other room's coroutine cannot even be scheduled to
                    # release it.
                    warm = self._wa_page
        if warm is not None:
            if focus:
                await self._focus_whatsapp()
            return warm
        with self._lock:
            if self._wa_lock is None:
                # Built here, on this loop, and only ever awaited on it. A lock
                # inherited from a previous room's loop would park this coroutine
                # on a future nothing will ever wake -- see `_OwnerLock`.
                self._wa_lock = asyncio.Lock()
            wa_lock = self._wa_lock
        async with wa_lock:
            # Re-checked after the wait: another call may have opened it while we
            # queued behind the lock.
            if self._wa_page is not None and not self._wa_page.is_closed():
                if not navigate or _is_whatsapp(self._wa_page.url):
                    if focus:
                        await self._focus_whatsapp()
                    return self._wa_page
            # `ensure` starts the browser if this is its first use, and creates the
            # main page alongside it -- so this reuses one context rather than
            # paying for a second browser. Not asking to focus *is* asking for a
            # background start, so the window is minimized rather than opened over
            # the user; `ensure` ignores that for a browser we merely joined.
            await self.ensure(background=not focus)
            context = self._context
            if context is None:
                raise ToolError(MSG_NO_PAGE)
            page = self._wa_page
            if page is None or page.is_closed():
                # Adopted before created. `_wa_page` is a reference, not a record
                # of what exists, so it can be gone while the tab is very much
                # open -- and one leaked tab per conversation is what a persistent
                # worker produces if the only answer is "make another".
                page = self._existing_whatsapp_tab(context) or self._adoptable_blank_page()
                adopted = page is not None
                if page is None:
                    page = await self._page_on(context)
                elif page is self._page:
                    # The blank page we adopted *was* the working page and the
                    # first registered tab, so it stops being both: it is the
                    # WhatsApp tab now, and `_reserve` below re-registers it as
                    # one. Left in `_tabs` it would still be listed as tab 1 --
                    # blank, and something the model never asked for.
                    self._page = None
                    self._forget_tab(page)
                # Assigned *before* the tab is reserved, and reserved before
                # anything awaits again: `new_page` raises Playwright's `page`
                # event while it is still outstanding, and `_on_page` reads an
                # unregistered page as a popup. Without this the messaging tab was
                # adopted as the working page and the user's own tab was closed
                # behind them -- see `_reserve`.
                self._wa_page = page
                self._reserve(page)
                if adopted:
                    logger.info(
                        "[browser] reused the WhatsApp tab already open rather than "
                        "opening another one"
                    )
                # Creating a tab raises the window, so a background start has to
                # hide it again *here* -- measured, and it is the whole remaining
                # half of the startup popup. The sequence is: `ensure`
                # minimizes the freshly launched window, creating this tab brings
                # it straight back up, and only then does WhatsApp start loading.
                # Minimizing after the navigation instead would also work, but it
                # would run on the warm path too, where the window may be one the
                # user is looking at. This runs exactly when we caused the raise.
                #
                # And only if *we* were the ones who hid it. On a browser that was
                # already open, creating this tab raises a window the user may be
                # watching -- and putting that back is not ours to do.
                if not focus and self._hid_window:
                    await self._minimize_window()
            ok, why = await self.allowed(WHATSAPP_URL)
            if not ok:
                raise ToolError(f"I can't open WhatsApp -- {why}. Nothing was sent.")
            try:
                await page.goto(WHATSAPP_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001 - timeout, abort by the guard
                raise ToolError(f"I couldn't open WhatsApp: {clip(str(exc), 120)}") from None
            if focus:
                await self._focus_whatsapp()
            return page

    async def _focus_whatsapp(self) -> None:
        """Bring the WhatsApp tab to the front of its window.

        Done on *every* hand-out, warm or cold, and that is not cosmetic. The
        whole send flow is "type the draft, then ask the user to confirm it while
        they can see it on their own screen" -- so if this tab is behind the blank
        browse tab, the design does not merely look worse, it does not work: the
        user is asked to approve a message they cannot see. It is also the only
        way a QR code can be scanned, since a tab nobody is looking at cannot be
        scanned.

        Best-effort throughout. Focus is a courtesy, and a window manager that
        refuses it must not turn a working send into a refusal.
        """
        page = self._wa_page
        if page is None:
            return
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001 - see the docstring
            logger.debug("[Browser] could not bring the WhatsApp tab forward", exc_info=True)

    async def whatsapp_open(self) -> bool:
        """Whether the WhatsApp tab is usable right now -- true or a sentence."""
        page = await self.whatsapp_page()
        try:
            state = await page.evaluate(_WA_STATE)
        except Exception:  # noqa: BLE001 - a page mid-navigation reads as nothing
            return False
        return bool((state or {}).get("ready"))

    async def release(self) -> None:
        """Close the browser if we have one. Never raises: this runs in
        teardown paths where an exception would cost more than a browser.

        **Ownership is dropped even when this cannot close anything.** The old
        version returned early on a cross-loop call *before* clearing
        `_loop`/`_context`, on the reasoning that leaving them referenced avoided
        orphaning something mid-flight. In practice that made the singleton
        permanently unusable: `close_session` awaits this from whatever loop the
        shutdown callback lands on, which for a room whose browser was started
        elsewhere is not the owning loop. The singleton kept pointing at a dead
        loop, so `ensure` refused every later room with `MSG_OTHER_LOOP` -- and
        because the browser is a process-wide singleton, "later rooms" meant
        every room for the remaining life of the worker. Lumine's browser was
        permanently dead after one untidy teardown, with no way back.

        So: take the references under the lock and clear them unconditionally.
        If the owning loop is alive we close through it; if it is gone we cannot
        close, and a browser whose loop has died goes with it.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            context, playwright = self._context, self._pw
            owner = self._loop
            shared = self._shared
            self._context = self._page = self._pw = None
            self._shared = False
            # We are no longer attached to that browser, so nothing here may keep
            # a handle to it. Leaving `_joined_browser` set meant a later read of
            # "am I a guest?" answered yes for a browser we had already left.
            self._joined_browser = None
            # The WhatsApp tab goes with everything else. Its *login* survives on disk
            # in the profile, which is the part worth keeping; the open chat, the
            # half-typed draft, and the search box somebody left filtered are all
            # state a new conversation must not inherit.
            #
            # The tab is ours -- `whatsapp_page` created it -- so letting it outlive
            # the conversation that used it means a persistent worker leaves one
            # behind per conversation. That is not untidy, it is *broken*: the next
            # conversation cannot see the old tab, so `whatsapp_page` opens another,
            # and after a few rooms there are several copies of WhatsApp open at
            # once. The search then runs against one while the press lands in
            # another, which reads as "she can't open the chat" -- and the reaction
            # to that failure is to open yet another tab.
            #
            # Best effort, and deliberately so: release runs on the way out of a
            # room and must not be the thing that fails. A close that does not land
            # is picked up by adoption on the next session rather than added to.
            #
            # **Unless the browser is shared**, in which case that tab is another
            # agent's and this process does not get to decide it should go.
            #
            # The *page* is taken out here and closed after the lock is released.
            # `_OwnerLock`'s contract is that it is held only for the statements
            # that read or write the ownership fields and **never across an
            # `await`** -- and awaiting the tab's close inside it broke that in the
            # worst available way: `with self._lock` is a `threading.Lock`, so a
            # second coroutine reaching it blocks the *event loop* rather than
            # waiting its turn, and the close the first one is waiting on can then
            # never finish. Teardown is the one path that is not allowed to fail,
            # and it was failing against its own lock. See `_close_page_safe`.
            wa_page = None
            if not shared:
                wa_page = self._wa_page
                self._wa_page = None
                self._wa_lock = None
            # Every tab goes with them, for the same reason: a page somebody was
            # halfway through reading is not state the next conversation should
            # inherit, and the numbers minted for it mean nothing to a browser
            # that no longer exists. Dropping the list *always* -- a guest's tabs
            # are somebody else's, but they are still not ours to keep a handle
            # on after we detach.
            self._tabs = []
            self._active = None
            self._hid_window = False
            self._loop = None
            self._owner_token = None
            self._start_lock = None

        if wa_page is not None:
            if owner is not None and owner is not loop and not owner.is_closed() and owner.is_running():
                # The tab belongs to the loop that made it. Hand it back there
                # rather than awaiting it from a loop that cannot service it.
                try:
                    asyncio.run_coroutine_threadsafe(
                        _close_page_safe(wa_page, "the WhatsApp tab"), owner
                    )
                except RuntimeError:  # pragma: no cover - loop died mid-handoff
                    pass
            else:
                await _close_page_safe(wa_page, "the WhatsApp tab")

        if owner is not None and owner is not loop:
            # Somebody else made these objects. Close them on *their* loop if it
            # is still running, which is the only place awaiting them is legal.
            if not owner.is_closed() and owner.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._close_remote(context, playwright, shared=shared), owner
                    )
                except RuntimeError:  # pragma: no cover - loop died mid-handoff
                    pass
            return
        await self._close_remote(context, playwright, shared=shared)

    @staticmethod
    async def _close_remote(context: Any, playwright: Any, *, shared: bool = False) -> None:
        """Detach from a context and stop Playwright, ignoring every failure.

        **A shared browser is detached from, not closed.** This is the difference
        between one agent leaving and the browser dying under everybody else.
        `connect_over_cdp` hands back a context whose `close()` tears down the whole
        browser -- including the WhatsApp tab another room is mid-read on, and the
        window the user is looking at. So when we joined rather than launched, the
        only thing we close is our own Playwright client.

        It also does not unpublish the endpoint, for the same reason: the browser is
        still running and the next process should still find it.
        """
        if shared:
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
            return
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:  # noqa: BLE001
                pass
        if context is None:
            # We had no browser of our own to close, so the published endpoint may
            # still be somebody else's: drop it only once nothing answers on it.
            if not _endpoint_alive(_read_endpoint()):
                _forget_endpoint()
            return
        # We closed the browser, so its endpoint is no longer a way in.
        #
        # **The check has to outlive the close, and the forget is not conditional
        # on it.** `context.close()` returns as Chromium is tearing down, and a
        # socket that is mid-shutdown still accepts a connect -- so one instant
        # probe read "alive" for a browser that was already gone and left the file
        # behind. The next `_start` in this same session then *joined* that dying
        # browser, got a context for a window that no longer existed, and failed
        # against it -- which is the whole of "close it, then ask me to open it
        # again and it will not". So: poll until the port stops answering (bounded,
        # because teardown must not become the slow part of a teardown), then drop
        # the file either way. A surviving file costs one no-op unlink; a surviving
        # *file for a dead browser* costs a whole session.
        deadline = time.monotonic() + _ENDPOINT_DEATH_SECONDS
        while time.monotonic() < deadline:
            if not _endpoint_alive(_read_endpoint()):
                break
            await asyncio.sleep(_ENDPOINT_POLL_SECONDS)
        _forget_endpoint()

    # -- page operations ---------------------------------------------------
    #
    # Mechanics only: no gate lives here. The tool decides what to ask and
    # when; this module only knows how to do it.

    async def _watch_verdict(self, url: str) -> dict[str, Any]:
        """What YouTube says about the video we just landed on.

        This is where the fabricated-address failure gets named, and it is worth
        being explicit about why it is *here* rather than before the navigation.
        A pre-flight refusal cannot tell a guessed id from one the user read out
        loud -- both are a watch URL we have not seen, so refusing would break the
        perfectly ordinary case of "play this link I just sent you". After the
        navigation the question has a real answer: a video that exists renders,
        and one that does not says so in the page's own words. So nothing is
        blocked, and the dead end is diagnosed instead of being navigated into.

        The distinction that matters for the user is not "did the page load" --
        YouTube serves its unavailable page with a success status, which is
        precisely why a fabricated id can look like a working one. It is whether
        the page reports a *playable* video.
        """
        video_id = video_id_of(url)
        if not video_id:
            return {"verdict": "other"}
        page = await self.ensure()
        try:
            facts = await page.evaluate(_JS_PLAYABILITY)
        except Exception:  # noqa: BLE001 -- a page we cannot read is not a failure
            return {"verdict": "unknown", "videoId": video_id}
        status = str(facts.get("status") or "")
        reason = str(facts.get("reason") or "").strip()
        if status == "OK":
            self._remember_video_id(url)
            return {"verdict": "playable", "videoId": video_id, "title": facts.get("title")}
        if not status:
            # No player response at all. That is not evidence of anything -- the
            # document may still be booting -- so it says so rather than guessing
            # either way.
            return {"verdict": "unknown", "videoId": video_id}
        return {
            "verdict": "unavailable",
            "videoId": video_id,
            "reason": reason or "YouTube did not report this as a playable video.",
            "seen": video_id in self._seen_video_ids,
            "invented": not _video_exists_anyway(reason, video_id in self._seen_video_ids),
        }

    def _remember_video_id(self, url: str) -> None:
        """Note a watch id that a page has now confirmed by showing us itself.

        Learned from two places, and only two: the ids on links an ``observe``
        read, and the id we have navigated to. The second is the stronger of the
        two -- a video page that rendered is proof the id exists, which an href on
        a results page only claims. Nothing the model says can add to this set,
        because the set's whole purpose is to tell its claims from the evidence.
        """
        video_id = video_id_of(url)
        if video_id:
            self._seen_video_ids.add(video_id)

    async def current_url(self) -> str:
        """The address already open, or ``""``.

        Read by the tool layer to answer "you are already here" *without* a round
        trip through the model. The reported failure was a browser opened to
        youtube.com three times in one turn, each call discarding the results page
        the previous one had just read -- so the model searched again every time
        and never got any further. A tool that can say "you are already on that
        page, here is what it says" ends the loop in a single call.
        """
        page = self._page
        if page is None:
            return ""
        try:
            if page.is_closed():
                return ""
            return str(page.url or "")
        except Exception:  # noqa: BLE001 - a page we cannot read is one we do not have
            return ""

    async def open_page(self, url: str) -> dict[str, Any]:
        target = normalize_url(url)  # ValueError -> the tool translates
        # Verdict before launch: a refused address must not even start a
        # browser, and the pre-check is the same verdict the route guard
        # applies to everything the page fetches afterwards.
        ok, why = await self.allowed(target)
        if not ok:
            raise ToolError(
                f"I can't open {clip(hostname_of(target), 60)} -- {why}. Nothing was shown."
            )
        # Already there? Navigate anyway rather than refuse, but *say* it, so the
        # model learns the page survived rather than re-issuing the same open on
        # the next turn. Refusing outright would break the legitimate "go there
        # again in case it changed" case, and silently doing nothing is what made
        # this read as a loop.
        already = ""
        try:
            here = await self.current_url()
            already = here if here.rstrip("/") == target.rstrip("/") else ""
        except Exception:  # noqa: BLE001 - the hint is never worth failing an open
            already = ""
        page = await self.ensure()
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            # A new document invalidates every reference the model is holding,
            # and it is the one moment we know that for certain.
            self._new_document()
        except Exception as exc:  # noqa: BLE001 -- timeout, abort by the guard, network
            raise ToolError(
                f"I couldn't open {clip(hostname_of(target), 60)}: {clip(str(exc), 120)}"
            ) from None
        # The user asked to see this page, so the window comes forward. Required
        # rather than decorative since a background warm-up can leave the browser
        # minimized, and a page nobody can see is not an open. Measured:
        # `bring_to_front` restores a minimized window, so this is also the only
        # thing anywhere that has to un-hide it.
        await self._reveal(page)
        # The guard already blocked a redirect inward; this is the sentence
        # for it, rather than a browser error page nobody asked to see.
        ok, why = await self.allowed(page.url)
        if not ok:
            raise ToolError(f"I stopped at {clip(page.url, 80)} -- {why}. Nothing was shown.")
        title = await self._title(page)
        result = {"title": title, "url": page.url}
        verdict = await self._watch_verdict(page.url)
        if verdict.get("verdict") == "playable":
            result["video"] = verdict
        elif verdict.get("verdict") == "unavailable":
            # Named rather than reported as a working page. A watch URL for a
            # video that does not exist is either an address the user gave us for
            # a private or removed video, or one the model assembled itself -- and
            # the second is the case worth catching, because the page loaded
            # successfully and looks like a fine result from the outside.
            result["video"] = verdict
            result["notice"] = (
                "This address is not a playable video -- "
                f"{clip(str(verdict.get('reason') or ''), 90)}."
                + (
                    (
                        " I have not seen that id on any page, so it was probably"
                        " assembled rather than read: open the search results and"
                        " click the video instead."
                    )
                    if verdict.get("invented")
                    else (
                        " The video itself exists, so this is not a broken link to"
                        " guess at -- it is a viewing restriction."
                    )
                )
            )
        if already:
            result["alreadyOpen"] = True
            result["notice"] = (
                "That page was already open -- I refreshed it rather than opening a"
                " new one. Scroll or observe it; do not open it again."
            )
        return result

    @staticmethod
    async def _title(page: Any) -> str:
        try:
            return await page.title()
        except Exception:  # noqa: BLE001 -- a title is nice to have, not a reason to fail
            return ""

    async def observe_page(self) -> dict[str, Any]:
        page = await self.ensure()
        if page.url == "about:blank":
            raise ToolError(MSG_NO_PAGE)
        raw = await page.evaluate(
            browser_state._JS_COLLECT,
            self._active_generation(),
        )
        rows = raw.get("candidates") if isinstance(raw, dict) else []
        items = browser_state.page_items_from_script(raw)
        item_titles = {item.ref: item.title for item in items if item.ref}
        controls = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
            control = {
                "ref": str(row.get("ref") or ""),
                "tag": str(row.get("tag") or ""),
                "type": str(row.get("type") or ""),
                "href": str(detail.get("href") or ""),
                "label": str(row.get("name") or ""),
                "context": str(row.get("rowText") or ""),
                "role": str(row.get("role") or ""),
                "synthesised": bool(detail.get("synthesizedName")),
            }
            if control["ref"] in item_titles:
                control["itemTitle"] = item_titles[control["ref"]]
            if isinstance(detail.get("options"), list):
                control["options"] = detail["options"][:20]
            if detail.get("selectedOption"):
                control["selectedOption"] = str(detail["selectedOption"])[:80]
            controls.append(control)
        result = {
            "title": str(raw.get("title") or ""),
            "url": str(raw.get("url") or page.url),
            "text": str(raw.get("text") or ""),
            "scroll": raw.get("scroll"),
            "ready": str(raw.get("ready") or "loading"),
            "controls": controls,
            "items": [item.to_dict() for item in items],
        }
        # Every video the page just showed us is an id worth remembering. A
        # results page is the only place this happens, which is why it has to be
        # collected here: `observe` is the one call that reads the whole document
        # and hands back every link on it.
        for control in result.get("controls") or []:
            if isinstance(control, dict):
                self._remember_video_id(str(control.get("href") or ""))
        for item in items:
            self._remember_video_id(item.href)
        # The content region, read separately from the shared control walk.
        # returns the *body* text, which on a product page is a cookie banner, a
        # nav drawer and a recommendation rail before anything the user asked
        # about -- the reported failure was a read that could not say what the
        # thing cost. The region read replaces that text with the part that
        # matters and adds the site's named fields, and is deliberately best
        # effort: a page with no region answers with nothing and keeps the body
        # text, which is still a read rather than an error.
        result = browser_state.apply_region(
            result, await browser_state.read_region(page, url=page.url)
        )
        result.update(await self.tab_context())
        self._flag_repeat_read(result)
        return result

    def _flag_repeat_read(self, result: dict[str, Any]) -> None:
        """Say so when a read returns the text this tab already returned.

        The measured failure was six observes and four scrolls on one product
        page, every one returning the same 127 words of header, with nothing
        telling the model it was re-reading the same window. A scroll that
        does not move (a page with no height to give, a wheel event the page
        swallows) looks exactly like success from the outside, so the model
        kept asking and kept getting the same text.

        The digest is url + scroll position + text: all three, because any one
        alone is ambiguous. The same url at a *different* position is a real
        second read and must not be flagged; the same text at a different url
        is a different page that happens to share its header. The notice lands
        in the payload as `notice`, next to the text it is about.
        """
        tab = self._tab_by_key(self._active)
        if tab is None:
            return
        scroll = result.get("scroll")
        position = scroll.get("y") if isinstance(scroll, dict) else None
        digest = "|".join(
            (
                str(result.get("url") or ""),
                str(position if position is not None else ""),
                str(result.get("text") or "")[:400],
            )
        )
        previous, tab.last_read = tab.last_read, digest
        if previous is not None and previous == digest:
            result["notice"] = (
                "This is the same text you just read from this page -- nothing"
                " moved. Scroll first (the payload's scroll.y / scroll.max say"
                " where you are and how much is left), then observe again; do"
                " not re-read the same window."
            )

    async def tab_context(self) -> dict[str, Any]:
        """Which tab this is, and what else is open.

        **Rides along with every read rather than being asked for.** The report
        was that Lumine did not know which tab she was on or what else was open --
        and the fix is not a better instruction, it is that `observe` and `state`
        now *say* it. A model that has to remember to ask "which tab am I on"
        asks on the turn after she has already acted on the wrong one, which is the
        turn where the answer is expensive.

        Cheap on purpose: one number for this tab, a count, and a short list of
        the others by title. Titles are clipped and the list is capped, because a
        tab list is exactly the sort of thing that grows without limit and is
        re-billed on every later turn.
        """
        self._prune()
        others = [
            {"tab": tab.key, "title": clip(await self._title(tab.page), TITLE_CHARS)}
            for tab in self._tabs
            if tab.key != self._active
        ][:MAX_REPORTED_TABS]
        return {
            "tab": self._active,
            "tabCount": len(self._tabs),
            "otherTabs": others,
        }

    async def inspect_ref(self, ref: str) -> dict[str, Any]:
        if not REF_RE.match(ref):
            return {"found": False}
        page = await self.ensure()
        return await page.evaluate(_JS_FACTS, [ref, self._active_generation()])

    async def click_ref(self, ref: str, sig: str) -> dict[str, Any]:
        """Click a reference, and report **where the page actually ended up**.

        "I clicked the row" is not "the right thing happened", and on this path
        the difference is not academic. Clicking a video result on YouTube and
        landing on the home page produced ``Clicked <title> on <host>.`` -- a
        success -- and the model then read whatever text was left on screen and
        answered from it. The click was the least interesting part of the claim.

        So the URL is read *before* the click and again after the page settles,
        and both are returned. A caller that only wants "did the click fire" can
        ignore them; a caller reporting to a person cannot, because a click that
        moved the page somewhere else is a different event from one that did
        what was wanted.
        """
        if not REF_RE.match(ref):
            return {"ok": False, "reason": "gone"}
        page = await self.ensure()
        before = {"url": page.url, "title": await self._title(page)}
        outcome = await page.evaluate(_JS_CLICK, [ref, sig, self._active_generation()])
        if not outcome.get("ok"):
            return outcome
        after = await self._settled(page)
        return {
            **outcome,
            "before": before,
            "after": after,
            # Same page and same document: the click was delivered but nothing
            # navigated. A single-page app that swapped content in place looks
            # identical from the URL, so this is "did not move", not "did not
            # work" -- the caller decides what that means.
            "moved": after["url"] != before["url"],
        }

    async def _settled(self, page: Any, *, attempts: int = 6, delay: float = 0.25) -> dict[str, str]:
        """Read the page once a click has had time to navigate.

        A click that opens a new document does not have its URL at the moment
        ``el.click()`` returns, so reading immediately reports the page the user
        was already on and every navigation looks like it failed. Polling for a
        *change* and settling briefly is what makes the report honest in both
        directions: a real navigation is caught, and a click that navigates
        nothing is not mistaken for a slow one.
        """
        url = page.url
        for attempt in range(max(1, attempts)):
            if page.url != url:
                url = page.url
                break
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
        return {"url": page.url, "title": await self._title(page)}

    async def fill_ref(self, ref: str, value: str) -> dict[str, Any]:
        if not REF_RE.match(ref):
            return {"ok": False, "reason": "gone"}
        page = await self.ensure()
        return await page.evaluate(_JS_FILL, [ref, value, self._active_generation()])

    async def select_ref(self, ref: str, sig: str, wanted: str) -> dict[str, Any]:
        """Choose one option of a `<select>` by its visible text or value."""
        if not REF_RE.match(ref):
            return {"ok": False, "reason": "gone"}
        page = await self.ensure()
        return await page.evaluate(_JS_SELECT, [ref, sig, wanted, self._active_generation()])

    async def press_ref(self, ref: str, sig: str, key: str) -> dict[str, Any]:
        """Press a key on a control, after re-reading its facts.

        Two steps on purpose. The facts are re-read first so a control that
        changed under the reference cannot be pressed at all -- the same
        rule the click follows -- and *then* Playwright's own locator press
        dispatches a real, trusted key through the browser's input pipeline.
        A synthetic `KeyboardEvent` would run the page's listeners and none
        of the browser's defaults, which is precisely the difference between
        "the page heard Enter" and "the form submitted".
        """
        if not REF_RE.match(ref):
            return {"ok": False, "reason": "gone"}
        page = await self.ensure()
        facts = await page.evaluate(_JS_FACTS, [ref, self._active_generation()])
        if not facts.get("found"):
            return {"ok": False, "reason": "gone"}
        now = f"{facts.get('tag', '')}|{facts.get('type', '')}|{'1' if facts.get('inForm') else '0'}"
        if now != sig:
            return {"ok": False, "reason": "changed"}
        try:
            await page.locator(f'[data-lumine-ref="{ref}"]').press(key, timeout=PRESS_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - not visible, not actionable, gone mid-press
            return {"ok": False, "reason": "not pressable"}
        return {"ok": True}

    async def scroll_page(self, direction: str) -> dict[str, Any]:
        """Move the viewport; returns where it landed relative to the page."""
        page = await self.ensure()
        if page.url == "about:blank":
            raise ToolError(MSG_NO_PAGE)
        return await page.evaluate(_JS_SCROLL, direction)

    async def go_history(self, direction: str) -> dict[str, Any]:
        """One step back or forward in this tab's own history."""
        page = await self.ensure()
        if page.url == "about:blank":
            raise ToolError(MSG_NO_PAGE)
        try:
            if direction == "back":
                response = await page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            else:
                response = await page.go_forward(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - timeout, blocked navigation, network
            raise ToolError(
                f"I couldn't go {direction}: {clip(str(exc), 120)}"
            ) from None
        if response is None:
            # Playwright answers `null` for "no entry" *and* for "cancelled"
            # (a form-resubmission confirmation, a page that refused to
            # unload). Saying which one it was would be a guess, so the
            # sentence does not make one.
            return {"moved": False, "url": page.url, "title": await self._title(page)}
        # A different document: the same reason `open_page` bumps it.
        self._new_document()
        # The guard already refused anything inward on the way here; this is
        # the sentence for it, the same one `open_page` gives.
        ok, why = await self.allowed(page.url)
        if not ok:
            raise ToolError(f"I stopped at {clip(page.url, 80)} -- {why}. Nothing was shown.")
        return {"moved": True, "url": page.url, "title": await self._title(page)}

    # -- tabs ---------------------------------------------------------------

    async def tab_list(self) -> dict[str, Any]:
        """Every tab the model may act on, and which one it is looking at.

        Read, so it is the answer to "what have I got open" without the model
        having to remember. The WhatsApp tab is not in it: it is not a tab
        `browse` drives, and listing it would offer a control that cannot work.
        """
        self._prune()
        rows: list[dict[str, Any]] = []
        for tab in self._tabs:
            rows.append(
                {
                    "tab": tab.key,
                    "title": clip(await self._title(tab.page), TITLE_CHARS),
                    "url": clip(tab.url(), URL_CHARS),
                    "active": tab.key == self._active,
                }
            )
        return {"tabs": rows, "active": self._active, "count": len(rows)}

    async def new_tab(self, url: str = "") -> dict[str, Any]:
        """Open another tab and make it the one in use.

        A blank tab when there is no address, because "open a new tab" on its own
        is a complete request and a blank tab is what a browser gives you for it.
        Navigating is a separate ``open`` the model already knows how to ask for.

        Refuses at :data:`MAX_WORK_TABS` rather than making one and closing
        something instead. The model asked for a tab; handing back a different set
        of tabs than it had would mean the references it is holding now point at
        the wrong documents, and it would have no way to know.
        """
        target = ""
        raw = str(url or "").strip()
        if raw:
            try:
                target = normalize_url(raw)
            except ValueError:
                raise ToolError(MSG_BAD_URL) from None
            ok, why = await self.allowed(target)
            if not ok:
                raise ToolError(
                    f"I can't open {clip(hostname_of(target), 60)} -- {why}. Nothing was shown."
                )
        self._prune()
        if len(self._tabs) >= MAX_WORK_TABS:
            raise ToolError(MSG_TOO_MANY_TABS)
        # `ensure` first, because it is what starts the browser when this is the
        # first thing anybody asked for.
        await self.ensure()
        context = self._context
        if context is None:  # pragma: no cover - ensure returns a page or raises
            raise ToolError(MSG_NO_PAGE)
        page = await self._page_on(context)
        # Registered before navigating, so a failure part-way through still
        # leaves a real, listed tab rather than a page nobody can see.
        tab = self._register(page, activate=True)
        if target:
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001 -- timeout, abort, network
                raise ToolError(
                    f"I couldn't open {clip(hostname_of(target), 60)}: {clip(str(exc), 120)}"
                ) from None
            # The same second look `open_page` takes: the pre-check judged the
            # address we asked for, and this one judges where it actually landed.
            # Discarding it would let a redirect inward into a new tab without a
            # word, which is the one thing the guard exists to be able to say.
            ok, why = await self.allowed(page.url)
            if not ok:
                raise ToolError(
                    f"I stopped at {clip(page.url, 80)} -- {why}. Nothing was shown."
                )
            self._new_document()
        # Always, blank tab or not: the user asked for a tab, and a tab behind a
        # window a warm-up minimized is not what "open a new tab" means.
        await self._reveal(page)
        return {
            "tab": tab.key,
            "title": clip(await self._title(page), TITLE_CHARS),
            "url": clip(page.url, URL_CHARS),
            "count": len(self._tabs),
        }

    async def switch_tab(self, which: object) -> dict[str, Any]:
        """Make one tab the one in use.

        **Does not mint a new document number, and that is the point.** Switching
        is not a navigation, so a tab the model read a moment ago still holds the
        references it read -- which is what makes "compare these two pages" work:
        read tab 1, switch to tab 2, switch back, act on what tab 1 showed. A
        counter that moved on every switch would refuse all of it.
        """
        tab = self._resolve_target(which)
        if tab is None:
            raise ToolError(MSG_NO_TAB)
        self._activate(tab)
        await self._reveal(tab.page)
        return {
            "tab": tab.key,
            "title": clip(await self._title(tab.page), TITLE_CHARS),
            "url": clip(tab.url(), URL_CHARS),
            "count": len(self._tabs),
        }

    async def close_tab(self, which: object = "") -> dict[str, Any]:
        """Close one tab -- by number, by ordinal, by position, or the one in use."""
        tab = self._resolve_target(which)
        if tab is None:
            raise ToolError(MSG_NO_TAB)
        key = tab.key
        was_active = tab.key == self._active
        self._drop(tab)
        if was_active:
            # Never leave the browser with no tab: the next operation would go
            # looking for one and, without the branch in `ensure`, try to launch
            # a second Chromium against a profile the first one still holds.
            if self._prune() is None:
                try:
                    await self.ensure()
                except ToolError:
                    # Reported as it is. A blank tab we could not open leaves the
                    # browser usable by the next call, which will try again.
                    pass
        listing = await self.tab_list()
        listing["closed"] = key
        return listing

    async def close_other_tabs(self) -> dict[str, Any]:
        """Close everything except the tab in use.

        Its own operation, not a target, because "close the other tabs" destroys
        several pages and "close tab 3" destroys one -- and the reported failure
        was a request for a *specific* tab landing on the one in front. Asking is
        only right when the user has to choose which pages to keep, and here they
        have already said: this one.
        """
        self._prune()
        keep = self._active
        doomed = [tab for tab in self._tabs if tab.key != keep]
        if not doomed:
            raise ToolError(MSG_NO_OTHER_TABS)
        for tab in doomed:
            self._drop(tab)
        listing = await self.tab_list()
        listing["closed"] = [tab.key for tab in doomed]
        return listing


_browser = _Browser()


# -- module-level surface, so the tool can patch one name in tests ----------


async def open_page(url: str) -> dict[str, str]:
    return await _browser.open_page(url)


async def current_url() -> str:
    """The address already open, or ``""``. See :meth:`_Browser.current_url`."""
    return await _browser.current_url()


async def observe_page() -> dict[str, Any]:
    return await _browser.observe_page()


async def inspect_ref(ref: str) -> dict[str, Any]:
    return await _browser.inspect_ref(ref)


async def click_ref(ref: str, sig: str) -> dict[str, Any]:
    return await _browser.click_ref(ref, sig)


async def fill_ref(ref: str, value: str) -> dict[str, Any]:
    return await _browser.fill_ref(ref, value)


async def select_ref(ref: str, sig: str, wanted: str) -> dict[str, Any]:
    return await _browser.select_ref(ref, sig, wanted)


async def press_ref(ref: str, sig: str, key: str) -> dict[str, Any]:
    return await _browser.press_ref(ref, sig, key)


async def scroll_page(direction: str) -> dict[str, Any]:
    return await _browser.scroll_page(direction)


async def go_history(direction: str) -> dict[str, Any]:
    return await _browser.go_history(direction)


async def release() -> None:
    await _browser.release()


async def tab_list() -> dict[str, Any]:
    """The open tabs and which one is in use. See :meth:`_Browser.tab_list`."""
    return await _browser.tab_list()


async def tab_context() -> dict[str, Any]:
    """Which tab this is, and what else is open. See :meth:`_Browser.tab_context`."""
    return await _browser.tab_context()


async def new_tab(url: str = "") -> dict[str, Any]:
    """Open another tab. See :meth:`_Browser.new_tab`."""
    return await _browser.new_tab(url)


async def switch_tab(which: object) -> dict[str, Any]:
    """Make one tab the one in use. See :meth:`_Browser.switch_tab`."""
    return await _browser.switch_tab(which)


async def close_tab(which: object = "") -> dict[str, Any]:
    """Close one tab. See :meth:`_Browser.close_tab`."""
    return await _browser.close_tab(which)


async def close_other_tabs() -> dict[str, Any]:
    """Close everything but the tab in use. See :meth:`_Browser.close_other_tabs`."""
    return await _browser.close_other_tabs()


async def working_page() -> Any:
    """The live page, for a tool that drives one known site directly.

    `browse` goes through refs and never touches this. The WhatsApp flow does: it
    searches a chat list and clicks controls whose identifiers live in WhatsApp's
    own markup, not in anything `observe` minted. The gate still sits in the tool
    layer -- this only answers "where would I type".
    """
    return await _browser.ensure()


async def whatsapp_page() -> Any:
    """The dedicated WhatsApp tab, opened and signed-in-or-not.

    Kept as a separate name rather than a parameter on :func:`working_page`
    because the two pages have different lifetimes and different owners: this one
    belongs to the messaging tools, and conflating them is how reading a chat
    ends up navigating the user's page.
    """
    return await _browser.whatsapp_page()


async def whatsapp_ready() -> bool:
    """Whether the WhatsApp tab is signed in right now. Never raises."""
    try:
        return await _browser.whatsapp_open()
    except Exception:  # noqa: BLE001 - a closed tab is "not ready", not an error
        return False


async def prewarm_whatsapp() -> bool:
    """Open the WhatsApp tab in the background so the first request is fast.

    The cost of the Web path is not the clicks -- it is starting Chromium and
    loading a single-page application, which is measured in seconds and happens on
    *every* first call of a conversation. Paying it while the user is still saying
    their first sentence is the difference between "she thought about it" and "she
    froze".

    **Fire-and-forget, and it does not put a window on the screen.** The second
    half is the whole reason this is worth stating: this runs while the greeting
    is still being generated, and it used to call `whatsapp_page()` with its
    default focus -- so a headed Chromium was launched, WhatsApp started loading,
    and `bring_to_front` pulled it over the front of the display. Every call, on
    every machine, whether or not the person on the line ever sends a message, and
    timed for while they were mid-sentence.

    So it asks for `focus=False`. The tab is still opened, still navigated, still
    logged in; nothing is brought forward, and a browser that has to be started
    for it comes up minimized (`_minimize_window`). The first *real* request
    brings the tab forward exactly as it always did -- and `bring_to_front`
    restores a minimized window, so the send flow still shows the user the draft
    they are approving. The latency win is kept; the popup is gone.

    Reported as ``False`` rather than raising, because a user who has not signed
    in yet will not have a tab, and that is not an error worth surfacing during
    startup.
    """
    try:
        await _browser.whatsapp_page(focus=False)
    except Exception:  # noqa: BLE001 - a warm-up that fails is not a failure
        return False
    return True
