"""Reading a page into structured facts: candidates, tabs, state, screenshots.

Everything here is *observation*. It changes nothing, so the permission class is
``read`` for all of it, and none of it is allowed to accept model-authored
JavaScript: each call is either a Playwright API we chose or a fixed script we
wrote. The scripts below are literal strings, evaluated by the page's own engine,
which is the same arrangement the existing `session` module uses and for the same
reason -- a model cannot reach them.

Two properties every read here keeps:

* **Bounded.** Text, control counts, candidate counts and screenshots are all
  capped. A read joins the model's context permanently, and an uncapped read of a
  single-page app is how a voice turn eats a token budget.
* **Snapshot-scoped.** Each observation stamps a generation. Anything that later
  acts on what it read must present that generation back, and a mismatch is a
  refusal -- not a re-resolve against a page that has moved on. This is the
  staleness rule, expressed as data rather than as a rule someone has to remember.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import locators

logger = logging.getLogger("lumine")

# The ceiling every tool result is held to, imported here rather than passed in.
# `state` is the module that decides how much of a page fits in a payload, and it
# needs the number to measure against -- the same try/except pair as everywhere
# else, because `python agent/agent.py` loads this as a top-level module and a
# relative import inside a function body passes every other test and then crashes
# the first real session.
try:
    from ..tool_results import MAX_PAGE_READ_CHARS
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tool_results import MAX_PAGE_READ_CHARS  # type: ignore[no-redef]

# The per-site selector map comes in the same way, and for the same reason. It
# used to be imported *inside* `read_region`, which passes every other test and
# then fails the first real session -- `test_import_shapes` flags a function-level
# relative import precisely because `python agent/agent.py` loads this module
# through a different package path than the tests do.
try:
    from . import site_map
except ImportError:  # running as a top-level script with agent/ on sys.path
    import site_map  # type: ignore[no-redef]

# -- bounds -------------------------------------------------------------------

#: How much of a page's *text* one read carries. Deliberately the same number as
#: the browser path's result ceiling (`MAX_PAGE_READ_CHARS`) rather than the
#: global one: a read that stops at 800 characters ends mid-sentence on the very
#: thing the read was for -- on a product page that is the price, which is why
#: the reported failure was "she read the page and could not tell me what it
#: costs". This is still a ceiling, not a target: it is re-billed on every later
#: turn, so it is raised only for the browser path and never globally.
MAX_TEXT = MAX_PAGE_READ_CHARS
MAX_CANDIDATES = locators.MAX_CANDIDATES
MAX_TABS = 12
MAX_PAGE_ITEMS = 20

#: How many controls one collection reports. Raised alongside `MAX_TEXT` for the
#: same reason: 8 was a page's header, and a model asked to find "the second
#: result" was choosing among the eight controls a marketing banner happened to
#: own. Still bounded -- a single-page app can render thousands.
MAX_REPORTED_CONTROLS = 60

#: One screenshot, as PNG bytes. Capped because a screenshot is the largest thing
#: this layer can produce and a model reads it as an image every time.
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024

#: What the *app* is sent, which is a different budget from what the model reads.
#: The preview crosses a data channel and lands in the DOM, so it is capped well
#: below the capture cap: a 4 MB PNG is 5.4 MB of base64 on the wire and a
#: decode stall in the renderer. 900x600 JPEG at quality 70 is a legible
#: screenshot of a page and roughly 60 KB, which is the whole point.
MAX_PREVIEW_BYTES = 900 * 1024

#: Longest edge of a preview, in pixels. Playwright can scale the capture for us,
#: so this costs nothing extra and bounds the decoded bitmap as well as the bytes.
PREVIEW_MAX_EDGE = 900

#: What the *model* is sent when it asks to look. Deliberately much smaller than
#: the preview: this one joins the chat context permanently, so it is re-billed on
#: every later turn for the rest of the session. 512px is enough to read a layout
#: and spot a control that has no text in it, which is the entire reason to look.
MAX_MODEL_IMAGE_BYTES = 320 * 1024
MODEL_IMAGE_MAX_EDGE = 512

#: JPEG quality for the model's copy, and the app's. 70 is the point where
#: screenshot text stays crisp; above it the bytes buy nothing a person can see.
PREVIEW_QUALITY = 70
MODEL_IMAGE_QUALITY = 70


@dataclass(frozen=True)
class PageItem:
    """A visible result/product card grounded in its current page snapshot."""

    index: int
    kind: str
    title: str
    text: str
    ref: str = ""
    href: str = ""
    price: str = ""

    def to_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "index": self.index,
            "kind": self.kind,
            "title": self.title,
            "text": self.text,
        }
        if self.ref:
            item["ref"] = self.ref
        if self.href:
            item["href"] = self.href
        if self.price:
            item["price"] = self.price
        return item


@dataclass(frozen=True)
class Snapshot:
    """One observation, with the generation that identifies it.

    ``generation`` increases on every read. A caller that acts must pass the
    generation it read; the action refuses if the page has been re-read since,
    because a reference minted from a snapshot the caller has not seen is a
    reference into a page state nobody reasoned about.
    """

    generation: int
    url: str = ""
    title: str = ""
    text: str = ""
    candidates: tuple[locators.Candidate, ...] = ()
    items: tuple[PageItem, ...] = ()
    tab_count: int = 0
    ready: str = "loading"
    #: Named fields pulled out of the page's *content* region by the site map
    #: (`site_map.yaml`). A page read that only returns prose still cannot answer
    #: "what does this cost?" without the model re-reading a paragraph hoping the
    #: number is in it; a field is the same fact under a stable name, so the
    #: result can say `price` instead of a sentence containing a currency glyph.
    #: Empty for a page no entry claims, which is the honest state -- there is no
    #: schema to guess at.
    fields: dict[str, str] = field(default_factory=dict)
    observed_at: float = field(default_factory=time.time)

    def candidate_list(self) -> list[locators.Candidate]:
        return list(self.candidates)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "generation": self.generation,
            "url": self.url,
            "title": self.title,
            "ready": self.ready,
            "text": self.text,
            "candidates": [item.to_dict() for item in self.candidates],
            "items": [item.to_dict() for item in self.items],
        }
        if self.fields:
            out["fields"] = dict(self.fields)
        return out


@dataclass
class SnapshotStore:
    """The last few snapshots, and the generation counter.

    Kept so an "observe, act, observe, act" sequence can prove each act followed
    an observation *the caller made*, and so a re-read that invalidated a
    reference can say so concretely.
    """

    generations: int = 0
    current: Snapshot | None = None
    history: list[Snapshot] = field(default_factory=list)

    def next_generation(self) -> int:
        self.generations += 1
        return self.generations

    def store(self, snapshot: Snapshot, *, keep: int = 4) -> Snapshot:
        self.current = snapshot
        self.history.append(snapshot)
        if len(self.history) > keep:
            del self.history[: len(self.history) - keep]
        return snapshot

    def stale(self, generation: object) -> bool:
        """Whether ``generation`` is no longer the live one."""
        wanted = int(generation or 0)
        return self.current is None or self.current.generation != wanted


# -- the scripts --------------------------------------------------------------
#
# Literal JavaScript, evaluated in the page. Each returns plain JSON-able data.
# `_JS_COLLECT` is the important one: it walks the accessibility-ish structure of
# the page and emits every candidate it can see, so `locators.rank` can choose
# among them in Python rather than by a CSS selector we guessed.

# -- what the reader is actually looking at -----------------------------------
#
# Every read here used to return the **head** of the document or of the content
# region, whatever the reader had scrolled to, so scrolling changed nothing about
# what came back. Measured on one product page: six `observe` calls and four
# scrolls, all returning the same 127 words of header text, and a turn that ended
# "I couldn't find the current price". The price was not in the head, and no
# budget fixes that -- a bigger budget reads *more header*.
#
# So a read is the text whose box meets the viewport, expanded by half a screen
# above and a screen below so it is not cut at the fold. Two rules carry it:
#
# * **It returns `''` rather than guessing** whenever this environment cannot
#   answer -- a document with no layout yet, a detached frame, the stub DOM the
#   suite executes the scripts against. The caller then keeps the head it read
#   before this existed, which is still a read rather than an error.
# * **Geometry is a second reading, never the whole answer.** The walk is in
#   document order and capped, so a page with ten thousand nodes costs a bounded
#   number of layout reads; and a text node's *line boxes* are what decide, not
#   its ancestor's box, because a `<div>` spanning the whole page would
#   otherwise qualify at every scroll position -- which is the bug in a new hat.
#
#: Inlined into `_JS_COLLECT`, `_JS_REGION_TEMPLATE` and `session._JS_OBSERVE`,
#: because all three read a page for the model and none of them may answer the
#: question differently. It is a fragment: it declares, it never returns at the
#: top level, and it is spliced into an arrow body -- see `js_script_shape`.
_JS_VIEWPORT_FN = """
const onScreenText = (root, cap) => {
  try {
    if (!root || typeof window === 'undefined' || typeof document === 'undefined') return '';
    const height = window.innerHeight || 0;
    if (!height) return '';
    if (typeof document.createTreeWalker !== 'function') return '';
    const top = (window.scrollY || document.documentElement.scrollTop || 0) - height * 0.5;
    const bottom = top + height * 2;
    const walker = document.createTreeWalker(root, 4); // NodeFilter.SHOW_TEXT
    const range = typeof document.createRange === 'function' ? document.createRange() : null;
    const parts = [];
    let chars = 0;
    let scanned = 0;
    let node = walker.nextNode();
    while (node && scanned < 3000) {
      scanned += 1;
      const value = (node.nodeValue || '').replace(/\\s+/g, ' ').trim();
      if (!value) { node = walker.nextNode(); continue; }
      let rects = null;
      if (range) {
        try {
          range.selectNode(node);
          rects = range.getClientRects();
        } catch (err) {
          rects = null;
        }
      }
      let hit = false;
      if (rects && rects.length) {
        for (let i = 0; i < rects.length; i += 1) {
          if (rects[i].bottom > top && rects[i].top < bottom) { hit = true; break; }
        }
      } else {
        const el = node.parentElement;
        const box = el && el.getBoundingClientRect ? el.getBoundingClientRect() : null;
        hit = !!(box && box.height > 0 && box.bottom > top && box.top < bottom);
      }
      // The walk advances here, after this node has been judged -- advancing
      // at the top of the loop made the parent-element fallback read the
      // *next* node, and the last one read `null`, so the helper threw and
      // answered '' on every page.
      node = walker.nextNode();
      if (!hit) continue;
      parts.push(value);
      chars += value.length + 1;
      if (cap && chars >= cap) break;
    }
    return parts.join(' ');
  } catch (err) {
    return '';
  }
};
const scrollPosition = () => {
  try {
    if (typeof window === 'undefined' || typeof document === 'undefined') return null;
    const root = document.scrollingElement || document.documentElement;
    const max = Math.max(0, (root.scrollHeight || 0) - window.innerHeight);
    return { y: Math.round(window.scrollY || root.scrollTop || 0), max: Math.round(max) };
  } catch (err) {
    return null;
  }
};
"""

# Nearby item text lets a control keep the identity of the card it belongs to:
# a thumbnail link may only be labelled "Play", while its video title lives in
# the surrounding renderer; a product button may only say "Add to cart".
_JS_ITEM_CONTEXT_FN = """
const itemContext = (el) => {
  const isItem = (node) => {
    if (!node || !node.tagName) return false;
    const tag = node.tagName.toLowerCase();
    const role = (node.getAttribute('role') || '').toLowerCase();
    if (['article', 'li', 'tr', 'ytd-video-renderer', 'ytd-rich-item-renderer'].includes(tag)
        || role === 'listitem') return true;
    const markers = [
      node.getAttribute('data-testid') || '',
      node.getAttribute('data-component-type') || '',
      node.getAttribute('class') || ''
    ].join(' ');
    return /(^|[\\s_-])(product|item|result|card|video)([\\s_-]|$)/i.test(markers);
  };
  for (let node = el && el.parentElement, depth = 0;
       node && node !== document.body && depth < 8;
       node = node.parentElement, depth += 1) {
    if (!isItem(node)) continue;
    const text = String(node.innerText || node.textContent || '')
      .replace(/\\s+/g, ' ').trim();
    if (text.length >= 12 && text.length <= 800) return text.slice(0, 180);
  }
  return '';
};
"""

_JS_PAGE_ITEMS_FN = r"""
const pageItems = () => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = typeof window === 'undefined' ? null : window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 &&
      (!style || (style.display !== 'none' && style.visibility !== 'hidden'));
  };
  // Site-identified cards must be collected before generic list items: large
  // shops commonly put dozens of navigation `<li>` nodes before their products.
  const selectors = [
    'ytd-video-renderer', 'ytd-rich-item-renderer',
    '[data-component-type="s-search-result"]',
    '[data-testid*="product" i]', '[class*="product-card" i]',
    '[class*="video-card" i]', 'article', '[role="listitem"]', 'li'
  ];
  const nodes = new Set();
  for (const selector of selectors) {
    try {
      for (const node of document.querySelectorAll(selector)) nodes.add(node);
    } catch (err) {
      // A browser-supported selector failure must not break the page read.
    }
  }
  const money = /(?:[$€£¥₹৳]\s?\d[\d,.]*(?:\s?(?:USD|EUR|GBP|BDT|INR))?|\b(?:USD|EUR|GBP|BDT|INR)\s?\d[\d,.]*)/i;
  const seen = new Set();
  const out = [];
  for (const node of nodes) {
    if (out.length >= 20 || !visible(node)) continue;
    const text = clean(node.innerText || node.textContent);
    if (text.length < 12) continue;
    const titleNode = Array.from(node.querySelectorAll(
      '[itemprop="name"], [id*="video-title"], [data-testid*="title" i], h1, h2, h3, h4, a[title]'
    )).find(visible);
    const links = Array.from(node.querySelectorAll('a[href]')).filter(visible);
    const title = clean(
      (titleNode && (titleNode.getAttribute('content') || titleNode.getAttribute('title') ||
        titleNode.innerText || titleNode.textContent)) ||
      (links[0] && (links[0].getAttribute('title') || links[0].getAttribute('aria-label') ||
        links[0].innerText || links[0].textContent)) ||
      text.split(/[.!?]\s/)[0]
    ).slice(0, 140);
    if (title.length < 3) continue;
    const link = links.find((entry) => {
      const label = clean(entry.getAttribute('title') || entry.getAttribute('aria-label') ||
        entry.innerText || entry.textContent);
      return label.toLowerCase().includes(title.toLowerCase()) ||
        title.toLowerCase().includes(label.toLowerCase());
    }) || links[0];
    const href = link ? String(link.href || '').slice(0, 240) : '';
    const identity = title.toLowerCase() + '|' + href;
    if (seen.has(identity)) continue;
    seen.add(identity);
    const priceNode = Array.from(node.querySelectorAll(
      '[itemprop="price"], meta[itemprop="price"], [class*="price" i], [data-testid*="price" i]'
    )).find(visible);
    const candidatePrice = priceNode
      ? clean(priceNode.getAttribute('content') || priceNode.innerText || priceNode.textContent)
      : '';
    const price = (candidatePrice.match(money) || text.match(money) || [])[0] || '';
    const isVideo = /ytd-video|video-card/i.test(node.tagName + ' ' + (node.className || '')) ||
      /\/watch\?/.test(href);
    const isProduct = !!price || /product|s-search-result/i.test(
      node.tagName + ' ' + (node.className || '') + ' ' + (node.getAttribute('data-testid') || '')
    );
    const target = link && link.getAttribute('data-lumine-ref')
      ? link.getAttribute('data-lumine-ref')
      : '';
    out.push({
      index: out.length + 1,
      kind: isVideo ? 'video' : (isProduct ? 'product' : 'result'),
      title: title,
      text: text.slice(0, 300),
      ref: target,
      href: href,
      price: price.slice(0, 60)
    });
  }
  return out;
};
"""

_JS_COLLECT = """(generation) => {
""" + _JS_VIEWPORT_FN + """
""" + _JS_ITEM_CONTEXT_FN + """
""" + _JS_PAGE_ITEMS_FN + """
  // Stale markers from a previous collection are cleared first. Leaving them
  // behind would mean an action that presents an old reference resolves against
  // a node this run never looked at -- which is exactly the "clicked something
  // else" failure the generation check exists to prevent.
  for (const old of document.querySelectorAll('[data-lumine-ref]')) {
    old.removeAttribute('data-lumine-ref');
  }
  if (generation !== undefined && generation !== null && document.documentElement) {
    document.documentElement.setAttribute('data-lumine-gen', String(generation));
  }
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  const labelFor = (el) => {
    const parts = [
      el.getAttribute('aria-label'),
      el.getAttribute('placeholder'),
      el.getAttribute('alt'),
      el.getAttribute('title')
    ];
    for (const part of parts) {
      if (part && part.trim()) return part.trim();
    }
    // A <label for> whose target is this control. Before `name`, because a name
    // is an implementation detail and a label is what a person would call it --
    // YouTube's search box is `name="search_query"` with no aria-label, and a
    // label check finds nothing, but a checkout input has both.
    if (el.id) {
      const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (label && label.textContent) return label.textContent.trim().slice(0, 120);
    }
    const wrapper = el.closest('label');
    if (wrapper && wrapper.textContent) return wrapper.textContent.trim().slice(0, 120);
    // Only now the identifier-ish attributes. A placeholder-less input with
    // nothing else is addressable by `name`, which is better than not being
    // addressable at all.
    const fallback = el.getAttribute('name') || el.getAttribute('placeholder') || '';
    if (fallback.trim()) return fallback.trim().slice(0, 120);
    return (el.textContent || '').trim().slice(0, 120);
  };
  const roleOf = (el) => {
    const explicit = (el.getAttribute('role') || '').trim().toLowerCase();
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
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
    return tag;
  };
  const selectors = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'video', 'summary', 'img',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[role="option"]', '[role="checkbox"]', '[role="switch"]', '[contenteditable="true"]',
    '[role="radiogroup"]', '[role="radio"]', '[role="combobox"]', '[role="listbox"]',
    '[role="slider"]', '[role="spinbutton"]', '[role="dialog"]', '[role="alertdialog"]',
    '[role="searchbox"]', '[role="textbox"]', '[role="togglebutton"]',
    '[role="menuitemcheckbox"]', '[role="row"]', '[role="cell"]', '[role="heading"]',
    '[role="figure"]', '[role="listitem"]',
    'details > summary'
  ];
  // A control that is the *only* place you can type inside a form. Search boxes
  // are almost always shaped like this, and Google's is: `name="q"`, no
  // aria-label, no placeholder, nothing in its label that says "search". The
  // shape is the only honest signal left on such a field.
  const soleFieldOfForm = (el) => {
    if (el.tagName.toLowerCase() !== 'input' && el.tagName.toLowerCase() !== 'textarea') return false;
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (!['text', 'search', 'email', 'tel', 'url', ''].includes(type)) return false;
    const form = el.closest('form');
    if (!form) return false;
    const typed = Array.from(form.querySelectorAll('input, textarea')).filter((other) => {
      const otherType = (other.getAttribute('type') || 'text').toLowerCase();
      return ['text', 'search', 'email', 'tel', 'url', ''].includes(otherType);
    });
    return typed.length === 1 && typed[0] === el;
  };
  const seen = new Set();
  const out = [];
  let index = 0;
  for (const node of document.querySelectorAll(selectors.join(','))) {
    if (out.length >= 400) break;
    if (seen.has(node)) continue;
    seen.add(node);
    if (!visible(node)) continue;
    const role = roleOf(node);
    index += 1;
    const raw = labelFor(node);
    // A control with no name at all is still a control. The rule here used to be
    // `if (!name) continue`, so a page of icon buttons and unlabelled headings
    // reported *no controls at all* -- a state indistinguishable, to the model,
    // from "this site has nothing on it", and the reason an image-heavy product
    // page offered nothing to click. It is given the role and its position
    // instead, and flagged, so a caller can tell an invented name from one the
    // page itself chose rather than having to guess.
    const name = raw || (role + ' ' + index);
    // A marker, minted on the node the ranking chose. This is what makes the
    // action hit *this* element rather than the nth match of a selector we
    // rebuilt after the page had already changed underneath us.
    const ref = 'r' + index;
    node.setAttribute('data-lumine-ref', ref);
    const detail = {};
    if (!raw) detail.synthesizedName = true;
    const href = node.getAttribute('href') || '';
    if (href && role === 'link') detail.href = href.slice(0, 200);
    const type = (node.getAttribute('type') || '').toLowerCase();
    if (type === 'checkbox' || type === 'radio') detail.checked = node.checked === true;
    if (node.tagName.toLowerCase() === 'select') {
      detail.options = Array.from(node.options)
        .map((option) => (option.textContent || '').replace(/\\s+/g, ' ').trim())
        .filter(Boolean)
        .slice(0, 20);
      if (node.selectedIndex >= 0) {
        detail.selectedOption = (node.options[node.selectedIndex].textContent || '')
          .replace(/\\s+/g, ' ').trim().slice(0, 80);
      }
    }
    const value = node.getAttribute('value');
    if (value) detail.value = value.slice(0, 80);
    if (soleFieldOfForm(node)) detail.soleField = true;
    // Whether this control sits inside a <form>. Read here because it is a DOM
    // fact and belongs with the other DOM facts, not computed later in Python from
    // something we did not collect.
    //
    // It is load-bearing for the permission class: `actions.classify_click` treats
    // a <button> inside a form as `sensitive` and one outside as `act`, and
    // `classify_key` treats Enter in a form as a submission. Omitting this made
    // `web_control`'s click path read it as False for *every* control, so a
    // submit button was classified `act` and never asked. `browse` was never
    // affected -- its `_JS_FACTS` script computed it independently -- which is
    // exactly why two tools could disagree about the same button.
    if (node.closest('form')) detail.inForm = true;
    // The *form's* own role, which is the one signal that separates a search
    // form from a comment box: Google and Amazon ship <form role="search">, and
    // no checkout or sign-in form does. Read here with `inForm` because it
    // answers the same question -- "does submitting this send the user
    // anything?" -- and it has to be a DOM fact rather than something derived
    // later from a field we did not collect. YouTube no longer carries the
    // role at all (measured 2026), which is what `detail.field` below is for.
    if (detail.inForm) {
      const form = node.closest('form');
      const formRole = form && (form.getAttribute('role') || '').trim().toLowerCase();
      if (formRole) detail.formRole = formRole;
    }
    // The element's `type`, kept under its own name. It used to reach the
    // permission classifiers as `detail["value"]` -- the *value* attribute --
    // which meant `<input type="submit" value="Go">` was classified as an
    // ordinary act and a form submission never asked. A fact that decides
    // whether to interrupt the user cannot be read off the wrong attribute.
    if (type) detail.type = type;
    // The element's `name` attribute, which is the site table's half of the
    // search classification (`actions.is_search_control` reads it as `field`).
    // An identifier the page's author chose, never text a user reads -- which
    // is what makes it usable here, and it only ever decides a class on a host
    // this tool already knows, because a host that is not in the table gets
    // nothing from it.
    const fieldName = node.getAttribute('name') || '';
    if (fieldName) detail.field = fieldName.slice(0, 60);
    out.push({
      index: index,
      ref: ref,
      role: role,
      name: name.slice(0, 120),
      tag: node.tagName.toLowerCase(),
      type: type,
      visible: true,
      enabled: !(node.disabled === true || node.getAttribute('aria-disabled') === 'true'),
      checked: (type === 'checkbox' || type === 'radio') ? (node.checked === true) : null,
      detail: detail,
      // The nearest row-ish ancestor, so a caller can describe "the third result".
      rowText: itemContext(node)
    });
  }
  return {
    title: document.title || '',
    url: location.href,
    // The whole body, clipped in-page so a single-page app with a million nodes
    // does not ship a megabyte to Python for Python to throw away. The number
    // matches `MAX_TEXT`; Python clips again, and the second clip is the one
    // that is allowed to change -- this is a wire bound, not a policy.
    //
    // The *viewport window* leads and the head is the fallback, because a head
    // slice answers nothing on the pages people ask about: it is the cookie
    // banner and the navigation, and scrolling changed nothing about it. The
    // fallback is kept rather than an empty string so a page with no layout yet
    // still reads as a page instead of as an error.
    text: (onScreenText(document.body, 2400) ||
      ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').trim()).slice(0, 2400),
    // Where this read was taken from, so "scroll down and read again" is a
    // sentence the model can act on and a repeat read at the same position can
    // be recognised for what it is.
    scroll: scrollPosition(),
    ready: document.readyState,
    candidates: out,
    items: pageItems()
  };
}
"""

#: Form values, with the sensitive fields removed *in the page* rather than
#: filtered afterwards. A password that left the page would already be in the
#: return value, and a filter in Python is a filter that can be forgotten.
_JS_FORM = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'password') {
      out.push({ name: el.name || '', type: type, redacted: true, value: '' });
      continue;
    }
    if (['hidden', 'submit', 'button', 'image', 'reset', 'file'].includes(type)) continue;
    if (el.tagName.toLowerCase() === 'select') {
      out.push({
        name: el.name || '', type: 'select', value: el.value || '',
        options: Array.from(el.options).map((o) => (o.textContent || '').trim()).slice(0, 30)
      });
      continue;
    }
    out.push({ name: el.name || '', type: type || 'text', value: (el.value || '').slice(0, 200) });
    if (out.length >= 40) break;
  }
  return out;
}
"""

_JS_TABS = """
() => Array.from(document.querySelectorAll('[role="tab"]'))
  .filter((el) => el.getBoundingClientRect().width > 0)
  .map((el) => ({
    name: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 80),
    selected: el.getAttribute('aria-selected') === 'true'
  }))
  .slice(0, 12)
"""

# -- the content region -------------------------------------------------------
#
# A page read that returns the whole body is a read that answers nothing on the
# pages people actually ask about: a product page's body text is a cookie
# banner, a navigation drawer and a recommendation rail, and the price is
# somewhere after all of it. Reading the *content region* is the difference
# between "the page loaded" and "it costs $49".
#
# The ladder is ordered from most specific to least. Each selector is tried in
# turn and the first that yields enough text wins; a selector that matches
# nothing is skipped, which is what makes it safe to lead with a site-specific
# entry that may have gone stale after a redesign.

#: Content-region selectors, most specific first. The tail of this ladder is
#: deliberately `body`: a page whose content none of the above claims is a page
#: we know nothing about, and reading all of it is still better than refusing --
#: but `region` is reported as empty for that case, so a caller can tell "this
#: is the whole page" from "this is the part that mattered".
REGION_LADDER: tuple[str, ...] = (
    "main",
    '[role="main"]',
    "article",
    '[role="article"]',
    "#content",
    "#main-content",
    "#main",
    ".main",
    ".content",
    '[itemprop="articleBody"]',
    "body",
)

#: The least text a region must carry to be believed as *the* content. Below
#: this it is a wrapper that happened to match -- an empty `<main>` around a
#: canvas, or a landmark a framework renders before its children mount -- and
#: believing it would return a blank read for a page that has plenty to say.
#: 80 characters is two short sentences: not much, but clearly not nothing.
REGION_MIN_CHARS = 80

#: Longest a single field's value may be, before the payload's own ceiling is
#: applied on top. A field is one fact, and a fact that needs two hundred
#: characters is a paragraph that has not been recognised as one.
MAX_FIELD_VALUE_CHARS = 200
MAX_REGION_FIELDS = 12


#: ``options`` is an array of CSS selectors, resolved in Python and passed in by
#: :func:`read_region`. The page never chooses them, and no model-authored
#: JavaScript ever reaches this call -- the selectors are validated in
#: :mod:`.site_map` before they get here, and a selector the page's own engine
#: rejects is skipped rather than raised, because a read that fails on one bad
#: selector is worse than a read that skips it.
_JS_REGION_TEMPLATE = """([selectors, fieldMap, charBudget]) => {
""" + _JS_VIEWPORT_FN + """
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const visibleEnough = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    // A region may be taller than the viewport -- that is normal -- but one with
    // no box at all is display:none, and its text is still in innerText on some
    // engines while being invisible on the screen.
    return rect.height > 0 || el === document.body;
  };
  let region = null;
  let regionSelector = '';
  // Named `charBudget`, never `budget`: the parameter and a `const` of the same
  // name in one arrow-function scope is a SyntaxError, and Playwright reports it
  // as a failed evaluate rather than a broken script. Measured -- this read threw
  // on *every* call and the caller swallowed it into "no region", so the per-site
  // map was silently doing nothing on every page. `ScriptShapeTests` now checks
  // every browser script for the collision.
  const budget = Math.max(200, Number(charBudget) || 2400);
  for (const selector of selectors || []) {
    let found = null;
    try {
      found = document.querySelector(selector);
    } catch (err) {
      // A selector the page's engine rejects is a selector we should skip, not
      // a reason to fail the whole read.
      found = null;
    }
    if (!found || !visibleEnough(found)) continue;
    const text = clean(found.innerText || found.textContent || '');
    // `body` is the last entry on the ladder and is allowed to win with less
    // text than the others, because on a genuinely sparse page it is the only
    // region there is. Anything else has to earn the claim.
    const isBody = found === document.body || String(selector).toLowerCase() === 'body';
    if (!isBody && text.length < 80) continue;
    region = found;
    regionSelector = String(selector);
    break;
  }
  if (!region) {
    return { text: '', fields: {}, region: '' };
  }
  const raw = clean(region.innerText || region.textContent || '');
  const fields = {};
  const entries = Object.entries(fieldMap || {});
  for (const [name, selector] of entries.slice(0, 12)) {
    let node = null;
    try {
      node = region.querySelector(selector);
    } catch (err) {
      node = null;
    }
    if (!node) {
      // Fall back to the document, so an absolute selector in the map still
      // works. A relative one is the norm, but refusing an absolute one would
      // make the map less expressive for no safety gained -- the selector was
      // validated in Python before it got here.
      try {
        node = document.querySelector(selector);
      } catch (err) {
        node = null;
      }
    }
    if (!node) continue;
    const value = clean(node.innerText || node.textContent || node.getAttribute('content') || '');
    if (!value) continue;
    fields[String(name)] = value.slice(0, 200);
  }
  return {
    // The region's *on-screen* text leads, and the whole region is the
    // fallback. A region taller than the viewport is the normal case for an
    // article or a product page, and its head is the same header the body read
    // already returned -- so a windowed read is what makes "scroll and read
    // again" produce different text. `''` means this environment cannot answer
    // (no layout, the suite's stub DOM), and the caller keeps the old answer
    // rather than an error.
    text: (onScreenText(region, budget) || raw).slice(0, budget),
    fields: fields,
    region: regionSelector
  };
}
"""


async def read_region(page: Any, *, url: object = "", budget: int = MAX_TEXT) -> dict[str, Any]:
    """The page's content region: its text, its named fields, and which selector won.

    Returns ``{"text", "fields", "region"}`` and never raises on a page that has
    none of the above -- ``region`` is ``""`` in that case, which is the honest
    answer and lets a caller say so rather than implying a region was found. The
    failure this replaces was a body-text read that handed the model eight
    hundred characters of navigation and no price.

    The site map is asked once and its entry is used for *both* halves -- which
    selectors to try, and which fields to extract -- because two lookups would
    be two answers if the file changed between them, and the field selectors are
    relative to the content region, so they only mean anything together.
    """
    entry = site_map.match(url)
    if entry is not None and entry.content:
        options: tuple[str, ...] = (entry.content,) + tuple(
            item for item in REGION_LADDER if item != entry.content
        )
        field_map: dict[str, str] = dict(entry.fields)
    else:
        # No entry, or one that only names fields. The ladder leads, and the
        # fields still apply -- a map may know where a site keeps its price
        # without knowing better than `main` where its prose is.
        options = REGION_LADDER
        field_map = dict(entry.fields) if entry is not None else {}
    try:
        payload = await page.evaluate(
            _JS_REGION_TEMPLATE,
            [list(options), field_map, int(budget)],
        )
    except Exception as exc:  # noqa: BLE001 -- a read must not take the tool down
        logger.warning("[browser] region read failed: %s", exc)
        return {"text": "", "fields": {}, "region": ""}
    if not isinstance(payload, dict):
        return {"text": "", "fields": {}, "region": ""}
    return {
        "text": str(payload.get("text") or ""),
        "fields": _bounded_fields(payload.get("fields")),
        "region": str(payload.get("region") or ""),
    }


def _bounded_fields(raw: Any) -> dict[str, str]:
    """Named fields from the page, bounded to something a payload can carry."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()[:60]
        text = " ".join(str(value or "").split())
        if name and text:
            out[name] = text[:MAX_FIELD_VALUE_CHARS]
        if len(out) >= MAX_REGION_FIELDS:
            break
    return out


def apply_region(payload: Any, region: Any) -> Any:
    """Merge a region read into a payload, without losing what it already had.

    The region's text *replaces* the body text when it has something to say and
    leaves it alone when it does not, because a caller that collected controls
    from the full document should not lose them to a region that turned out to
    be empty. ``fields`` merges rather than replaces, and a field the region did
    not find is left exactly as it was.
    """
    if not isinstance(payload, dict) or not isinstance(region, dict):
        return payload
    text = str(region.get("text") or "")
    if len(text.strip()) >= REGION_MIN_CHARS:
        payload["text"] = text[:MAX_TEXT]
        payload["region"] = str(region.get("region") or "")
    fields = region.get("fields")
    if isinstance(fields, dict) and fields:
        merged = dict(payload.get("fields") or {})
        merged.update(_bounded_fields(fields))
        payload["fields"] = merged
    return payload


#: What the collection script reports for a control inside a ``<form>``. Asserted
#: against a real page in ``test_browser_search`` rather than here, because the
#: script is JavaScript and the only honest way to test it is against a browser.
#: This constant exists so Python has one name for the fact.
DETAIL_IN_FORM = "inForm"


def candidates_from_script(payload: Any) -> list[locators.Candidate]:
    """Turn the collection script's output into :class:`Candidate` objects.

    Built here rather than in JavaScript so the ranking, the caps and the
    candidate shape are all testable in Python with a fixture -- the same
    property the whole locators module has.

    The ``ref`` survives into the candidate, and that is the point of the whole
    function: the marker is minted on the node the page rendered, so an action
    can address that node directly rather than rebuilding a selector and hoping it
    still means the same thing.
    """
    rows = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[locators.Candidate] = []
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        detail = dict(row.get("detail") or {})
        row_text = str(row.get("rowText") or "")
        if row_text:
            detail["meta"] = row_text
        out.append(
            locators.Candidate(
                index=int(row.get("index") or position),
                ref=str(row.get("ref") or ""),
                role=str(row.get("role") or ""),
                name=str(row.get("name") or ""),
                tag=str(row.get("tag") or ""),
                visible=bool(row.get("visible", True)),
                enabled=bool(row.get("enabled", True)),
                checked=row.get("checked") if isinstance(row.get("checked"), bool) else None,
                detail=detail,
            )
        )
        if len(out) >= MAX_CANDIDATES * 4:
            break
    return out


def page_items_from_script(payload: Any) -> tuple[PageItem, ...]:
    """Parse bounded, visible result cards without trusting page field types."""
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return ()
    out: list[PageItem] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = " ".join(str(row.get("title") or "").split())[:140]
        text = " ".join(str(row.get("text") or "").split())[:300]
        if len(title) < 3 or not text:
            continue
        href = str(row.get("href") or "").strip()[:240]
        if not href.startswith(("http://", "https://")):
            href = ""
        ref = str(row.get("ref") or "")
        if not re.fullmatch(r"r\d{1,3}", ref):
            ref = ""
        identity = (title.casefold(), href)
        if identity in seen:
            continue
        seen.add(identity)
        kind = str(row.get("kind") or "result")
        if kind not in {"video", "product", "result"}:
            kind = "result"
        out.append(
            PageItem(
                index=len(out) + 1,
                kind=kind,
                title=title,
                text=text,
                ref=ref,
                href=href,
                price=" ".join(str(row.get("price") or "").split())[:60],
            )
        )
        if len(out) >= MAX_PAGE_ITEMS:
            break
    return tuple(out)


def ground_candidates(
    candidates: Sequence[locators.Candidate], items: Sequence[PageItem]
) -> list[locators.Candidate]:
    """Attach a result's title to its primary link for semantic resolution."""
    item_titles = {item.ref: item.title for item in items if item.ref}
    for candidate in candidates:
        title = item_titles.get(candidate.ref)
        if title:
            candidate.detail["itemTitle"] = title
    return list(candidates)


def snapshot_from_script(
    generation: int, payload: Any, *, tab_count: Any = None
) -> Snapshot:
    """Build a snapshot from a script's payload.

    ``tab_count`` defaults to *derived from the payload*, not to zero. The bug
    this replaces was a caller reading ``payload["tabs"]`` as a number and calling
    ``int()`` on a list; fixing only that call site left the trap one refactor
    away. Deriving it here means the field is counted the same way whoever built
    the snapshot, and a caller that *does* pass a count is overriding rather than
    remembering. ``None`` is therefore meaningful and ``0`` is a real zero.
    """
    data = payload if isinstance(payload, dict) else {}
    count = data.get("tabs") if tab_count is None else tab_count
    candidates = candidates_from_script(data)
    items = page_items_from_script(data)
    candidates = ground_candidates(candidates, items)
    return Snapshot(
        generation=generation,
        url=str(data.get("url") or ""),
        title=str(data.get("title") or ""),
        text=str(data.get("text") or "")[:MAX_TEXT],
        candidates=tuple(candidates),
        items=items,
        # Counted leniently on purpose. This is the one field whose argument can
        # arrive as a list rather than a number -- `tabs` holds a list everywhere
        # in this module's own vocabulary -- and it used to raise here, taking
        # every read on a multi-frame page down with it.
        tab_count=_as_count(count),
        ready=str(data.get("ready") or "loading"),
        fields=fields_from_script(data),
    )


def fields_from_script(payload: Any) -> dict[str, str]:
    """Named fields from a script payload, bounded and always a plain dict.

    Kept as its own function rather than inlined in :func:`snapshot_from_script`
    so the region reader and the collector can both feed a snapshot without
    either of them reaching into the other's output shape. A non-dict or a
    non-string value is dropped rather than coerced: a field the page did not
    actually state is worse than a field that is absent, because the model will
    quote it back as a fact.
    """
    raw = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()[:60]
        text = " ".join(str(value or "").split())
        if name and text:
            out[name] = text[:MAX_FIELD_CHARS]
        if len(out) >= MAX_PAGE_FIELDS:
            break
    return out


#: Longest one named field may be, and how many a single read reports. Both are
#: here rather than in `tool_results` because they are the shape of *this*
#: payload and nothing else uses them: a field is one fact ("$49.00"), and a page
#: with more than twelve named facts is a page whose region selector is wrong.
MAX_FIELD_CHARS = 200
MAX_PAGE_FIELDS = 12


def _as_count(value: Any) -> int:
    """A count from whatever was handed over, never an exception.

    ``tabs`` is a list of descriptors in every other part of this module's
    vocabulary, so a count has to accept one. Doing that here rather than at the
    call site is the difference between a field this function tolerates and a
    trap one refactor away: the caller that read the list as a number raised
    ``TypeError`` on every snapshot of a multi-frame page, and took
    ``web_control`` down with it.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, (list, tuple)):
        return len(value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


#: The longest a field's name or value may be. Both exist to keep the JSON
#: *parseable*: `tool_results.as_payload` enforces its ceiling by cutting the
#: rendered text, and a cut object is not an object -- the model would receive
#: `{"forms":[{"name":"address_line_one_address_line_...` and spend the tokens
#: anyway on something it cannot read.
MAX_FORM_NAME_CHARS = 40
MAX_FORM_VALUE_CHARS = 80

#: The most form rows a read will return, before :func:`form_values_payload`
#: starts dropping rows to fit. A real checkout form has more than two fields, so
#: this is a ceiling rather than a target -- the fitting pass decides.
MAX_FORM_ROWS = 12


def _form_budget_chars() -> int:
    """The result ceiling a form read is fitted to.

    The browser path reads *more* than the global ceiling, so a form read has to
    be measured against the budget that path actually has. Measuring against the
    global one fitted a two-field form into 800 characters while the payload it
    was being placed inside was allowed 2400, so the rows were dropped for no
    reason the caller could see.
    """
    return int(MAX_PAGE_READ_CHARS)


def _payload_fits(rows: list[dict[str, Any]], extra: int) -> bool:
    """Whether the caller's *whole* payload fits, not just the forms half.

    ``extra`` is the rendered JSON of everything the caller will put around these
    rows, not an estimate of its length. Measuring the parts separately is how this
    got it wrong the first time: the fields were budgeted to fit and the keys around
    them were not counted at all, so the payload still arrived cut in half.

    The caller's job is to render its own keys once and hand them in. That is a
    little more work than passing a character count, and it is the difference
    between "fits" and "usually fits, unless the title is long".
    """
    try:
        body = json.dumps({"forms": rows, "around": extra}, separators=(",", ":"))
    except (TypeError, ValueError):
        # Unrenderable surroundings. Assume the worst rather than crashing a read.
        return False
    return len(body) <= _form_budget_chars()


def _is_redacted(row: Any) -> bool:
    """Whether a collected form row is a password field.

    Read from the page's own redaction flag *and*, as a backstop, from the type --
    because the flag is the only thing standing between a page's password and the
    model's context, and it is not the sort of thing a single missed key should be
    able to undo. `_JS_FORM` redacts in the page precisely so a filter here can be
    forgotten; this is that filter, and it errs toward redacting.
    """
    if not isinstance(row, dict):
        return False
    if row.get("redacted"):
        return True
    return str(row.get("type") or "").casefold() in ("password", "passwd", "pin", "pass code")


def form_values_payload(
    rows: Any, *, limit: int = MAX_FORM_ROWS, around: Any = None
) -> list[dict[str, Any]]:
    """Form values, bounded, with redaction preserved rather than dropped.

    A redacted entry is *kept* rather than filtered: the model should be able to
    see that a password field exists and is empty, which is the honest reading,
    instead of concluding the page has no such field.

    Rows are dropped from the end until the caller's whole payload *fits*.
    ``around`` is the rendered JSON of the other keys this read returns -- see
    :func:`_payload_fits` for why it is the real thing and not a length. One row is
    always kept when there is one, so a single-field form can report its field
    rather than nothing.
    """
    surroundings = around if isinstance(around, str) else json.dumps(around or {}, separators=(",", ":"))
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        entry = {
            "name": str(row.get("name") or "")[:MAX_FORM_NAME_CHARS],
            "type": str(row.get("type") or "")[:20],
        }
        if _is_redacted(row):
            entry["value"] = ""
            entry["redacted"] = True
        else:
            entry["value"] = str(row.get("value") or "")[:MAX_FORM_VALUE_CHARS]
            if row.get("options"):
                entry["options"] = [str(item)[:40] for item in row["options"]][:20]
        out.append(entry)
        if len(out) >= max(1, int(limit)):
            break

    if surroundings:
        while len(out) > 1 and not _payload_fits(out, surroundings):
            out.pop()
    return out





def tabs_payload(rows: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "name": str(row.get("name") or "")[:80],
                "selected": bool(row.get("selected")),
            }
        )
        if len(out) >= MAX_TABS:
            break
    return out


def locate(snapshot: Snapshot, query: locators.ElementQuery) -> locators.Resolution:
    """Choose among a snapshot's candidates. Thin, so it is one obvious place."""
    return locators.resolve_one(snapshot.candidate_list(), query)


def report(snapshot: Snapshot, resolution: locators.Resolution, *, limit: int = 6) -> str:
    """A short, checkable report for the model.

    Says which element was chosen *and* what else was available, because the
    ambiguity case is only resolvable if the model can see the options.
    """
    if resolution.ok and resolution.chosen is not None:
        chosen = resolution.chosen
        parts = [f"Matched {chosen.name[:60]!r} ({chosen.role}, item {chosen.index})."]
        others = [item for item in resolution.candidates if item.index != chosen.index]
        if others:
            parts.append(
                "Others on the page: "
                + ", ".join(f"{item.name[:30]!r} ({item.index})" for item in others[:3])
            )
        return " ".join(parts)
    if resolution.ambiguous:
        options = ", ".join(
            f"{item.index}={item.name[:40]!r}" for item in resolution.candidates[:limit]
        )
        return f"{resolution.reason}: {options}"
    return resolution.describe()


def change_report(before: str, after: str) -> str:
    """Whether the page actually changed, stated plainly.

    A click that returned ``ok`` and a URL that did not move are different
    events, and the second is the one that tells you the click missed. Reported
    rather than assumed, because "the click worked" and "the page navigated" are
    separate claims.
    """
    if before and after and before != after:
        return f"the page moved to {after[:80]}"
    if before == after:
        return "the page did not move, though the control was activated"
    return "the page state was re-read"