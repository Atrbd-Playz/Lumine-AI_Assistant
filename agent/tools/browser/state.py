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
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import locators

# The ceiling every tool result is held to, imported here rather than passed in.
# `state` is the module that decides how much of a page fits in a payload, and it
# needs the number to measure against -- the same try/except pair as everywhere
# else, because `python agent/agent.py` loads this as a top-level module and a
# relative import inside a function body passes every other test and then crashes
# the first real session.
try:
    from ..tool_results import MAX_RESULT_CHARS
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tool_results import MAX_RESULT_CHARS  # type: ignore[no-redef]

# -- bounds -------------------------------------------------------------------

MAX_TEXT = 1500
MAX_CANDIDATES = locators.MAX_CANDIDATES
MAX_TABS = 12

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
    tab_count: int = 0
    ready: str = "loading"
    observed_at: float = field(default_factory=time.time)

    def candidate_list(self) -> list[locators.Candidate]:
        return list(self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "url": self.url,
            "title": self.title,
            "ready": self.ready,
            "text": self.text,
            "candidates": [item.to_dict() for item in self.candidates],
        }


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

_JS_COLLECT = """
() => {
  // Stale markers from a previous collection are cleared first. Leaving them
  // behind would mean an action that presents an old reference resolves against
  // a node this run never looked at -- which is exactly the "clicked something
  // else" failure the generation check exists to prevent.
  for (const old of document.querySelectorAll('[data-lumine-ref]')) {
    old.removeAttribute('data-lumine-ref');
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
    'a[href]', 'button', 'input', 'select', 'textarea', 'video', 'summary',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[role="option"]', '[role="checkbox"]', '[role="switch"]', '[contenteditable="true"]',
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
    const name = labelFor(node);
    if (!name) continue;
    index += 1;
    // A marker, minted on the node the ranking chose. This is what makes the
    // action hit *this* element rather than the nth match of a selector we
    // rebuilt after the page had already changed underneath us.
    const ref = 'r' + index;
    node.setAttribute('data-lumine-ref', ref);
    const role = roleOf(node);
    const detail = {};
    const href = node.getAttribute('href') || '';
    if (href && role === 'link') detail.href = href.slice(0, 200);
    const type = (node.getAttribute('type') || '').toLowerCase();
    if (type === 'checkbox' || type === 'radio') detail.checked = node.checked === true;
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
    // form from a comment box: YouTube, Google and Amazon all ship
    // <form role="search">, and no checkout or sign-in form does. Read here
    // with `inForm` because it answers the same question -- "does submitting
    // this send the user anything?" -- and it has to be a DOM fact rather than
    // something derived later from a field we did not collect.
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
      rowText: (node.closest('li, [role="listitem"], article, ytd-video-renderer, tr') || {}).textContent
        ? (node.closest('li, [role="listitem"], article, ytd-video-renderer, tr').textContent || '')
            .replace(/\\s+/g, ' ').trim().slice(0, 300)
        : ''
    });
  }
  return {
    title: document.title || '',
    url: location.href,
    text: ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').trim().slice(0, 1500),
    ready: document.readyState,
    candidates: out
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
    return Snapshot(
        generation=generation,
        url=str(data.get("url") or ""),
        title=str(data.get("title") or ""),
        text=str(data.get("text") or "")[:MAX_TEXT],
        candidates=tuple(candidates_from_script(data)),
        # Counted leniently on purpose. This is the one field whose argument can
        # arrive as a list rather than a number -- `tabs` holds a list everywhere
        # in this module's own vocabulary -- and it used to raise here, taking
        # every read on a multi-frame page down with it.
        tab_count=_as_count(count),
        ready=str(data.get("ready") or "loading"),
    )


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
    """The result ceiling, read once at module import."""
    return int(MAX_RESULT_CHARS)


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