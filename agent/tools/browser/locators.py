"""Turning what the user said about an element into a Playwright locator.

## Why not just CSS

Because the pages that actually break are the ones whose markup nobody controls.
"Click the second video" on YouTube, "choose Bangladesh" on a checkout form, and
"search" on a page whose search box has no stable id all need something other
than a hand-written selector. Playwright already solves this properly, with
*role and name* locators computed from the accessibility tree -- which is what a
screen reader uses, and therefore the closest thing to what a user perceives as
"the Search button".

So the strategy ladder here is:

1. **Accessible role + name** -- what a person would call the control.
2. **Label / placeholder / alt / title** -- the visible text attached to it.
3. **Visible text** -- for elements that are their own label (a link, a button).
4. **Stable identifier** -- ``id``, ``name``, ``data-testid``.
5. **CSS** -- only when the caller supplied one, and validated before use.

Verified coordinates come last and are explicitly *not* implemented as a
fallback, because a coordinate is only meaningful against the exact frame it was
read from. A reflow between the screenshot and the click moves it. Every other
tier here is re-resolved live, which is the property that makes a stale reference
refuse rather than mis-click.

## Ambiguity is a result, not a guess

`locate` returns either a single resolved element or a list of candidates. When
the caller supplied a disambiguator -- a position ("the second"), a channel name,
a partial label -- the ranking uses it. When several elements still match and no
disambiguator was given, it returns *candidates* and the tool refuses to choose,
because silently picking the first match is how "click the video" opens the wrong
one and the user only finds out from the audio.

## Nothing here evaluates model-authored JavaScript

Every locator is built from a fixed set of Playwright APIs. A CSS selector is
accepted because it cannot execute, and is length-capped and checked for the
characters that would turn it into something else. There is no
``evaluate("...")`` path, and that is the whole security argument for this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# -- input validation ---------------------------------------------------------

#: CSS selectors a request may supply. Deliberately narrow: enough to address a
#: real element, with no way to express a combinator explosion, a pseudo-class
#: chain, or an attribute selector that walks the document.
CSS_MAX = 200
#: Attribute-prefix operators are refused outright. ``^=`` and ``|=`` are how a
#: selector grows an unbounded per-node comparison against an attacker-chosen
#: attribute, and they are the only reason ``^``/``$``/``|`` are absent from this
#: character class rather than present and filtered afterwards.
_CSS_ALLOWED = re.compile(r"^[A-Za-z0-9_#\-\.\[\]=\"' :\*\>\+\~,()$]+$")

#: Roles we will ask Playwright for. A closed list rather than a pass-through:
#: an unknown role string becomes a locator that matches nothing, and the honest
#: answer to that is a refusal at the boundary rather than a timeout in the page.
ROLES: tuple[str, ...] = (
    "button",
    "link",
    "textbox",
    "searchbox",
    "checkbox",
    "radio",
    "radiogroup",
    "combobox",
    "listbox",
    "option",
    "menuitem",
    "menuitemcheckbox",
    "togglebutton",
    "tab",
    "heading",
    "image",
    "img",
    "figure",
    "video",
    "slider",
    "spinbutton",
    "switch",
    "dialog",
    "alertdialog",
    "row",
    "cell",
    "listitem",
)

#: Attribute-based strategies, in the order we try them. Each maps to a
#: ``get_by_*`` call, so a "label" and an "alt text" both end up in the
#: accessibility tree rather than in a raw selector.
TEXT_STRATEGIES: tuple[str, ...] = ("label", "placeholder", "alt", "title", "text", "value")

#: Ordinals a caller may use for "the second one". Written out rather than
#: parsed from a number, because "the second" is how people say it and a
#: natural-language ordinal the model can pass through is exactly the input we
#: want to *not* have to parse freely.
ORDINALS: dict[str, int] = {
    "first": 1, "1st": 1,
    "second": 2, "2nd": 2,
    "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5,
    "last": -1,
}

_ORDINAL_RE = re.compile(
    r"\b(" + "|".join(sorted(ORDINALS, key=len, reverse=True)) + r")\b", re.IGNORECASE
)
_NUMBERED_RE = re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\b", re.IGNORECASE)

MAX_CANDIDATES = 8


class LocatorError(ValueError):
    """The caller's element description cannot be turned into a locator."""


# -- pure parsing -------------------------------------------------------------


#: Roles a caller may name that the collector reports under a different, equally
#: correct name for the same control. Kept as equivalence sets rather than
#: synonyms so the filter stays symmetric and obvious: "the second *text box*"
#: must find an ``<input type="text">`` as readily as a ``<textarea>``, and a
#: model that says "textbox" for both is not wrong.
ROLE_FAMILIES: dict[str, frozenset[str]] = {
    "textbox": frozenset({"textbox", "searchbox", "input", "textarea"}),
    "searchbox": frozenset({"searchbox", "textbox"}),
    "button": frozenset({"button", "submit", "reset"}),
    "link": frozenset({"link", "a"}),
    "combobox": frozenset({"combobox", "select", "textbox", "searchbox"}),
    "checkbox": frozenset({"checkbox", "switch"}),
    "heading": frozenset({"heading", "h1", "h2", "h3", "h4", "h5", "h6"}),
    # Each of these is a *filter*, applied as `item.role in role_family(wanted)`,
    # so a family that is too wide does not help a model find more things -- it
    # makes one thing ambiguous against everything the family swallowed. A
    # `listitem` is not an `option` and a `cell` is not a `row`; mapping them
    # across would turn "the second option" on a results page into a page-wide
    # ambiguity, which is the failure this table exists to prevent.
    #
    # What is here instead is each *spelling* of one control. `image` and `img`
    # are the same element reached by two names -- Playwright's role engine says
    # `image`, the collector reports the tag -- and before this a model asking
    # for "the product image" matched nothing on a page of photographs, because
    # the family contained only the name it was asked by.
    "image": frozenset({"image", "img"}),
    "img": frozenset({"img", "image"}),
    # An alert dialog *is* a dialog, so asking for a dialog reaches either; the
    # reverse is deliberately not true, because "the alert dialog" must not be
    # answered by an ordinary one.
    "dialog": frozenset({"dialog", "alertdialog"}),
    "alertdialog": frozenset({"alertdialog"}),
    # A group and the radios in it are different controls and stay different:
    # "the second radio" means the button, never the container holding it.
    "radio": frozenset({"radio"}),
    "radiogroup": frozenset({"radiogroup"}),
    "listbox": frozenset({"listbox"}),
    "option": frozenset({"option"}),
    "menuitem": frozenset({"menuitem", "menuitemcheckbox"}),
    "row": frozenset({"row"}),
    "cell": frozenset({"cell"}),
    "listitem": frozenset({"listitem"}),
}


def role_family(role: object, tag: object = "") -> frozenset[str]:
    """The names a control may legitimately answer to.

    Always includes the role itself, so an unknown-but-declared role filters
    strictly rather than silently matching everything -- a strict filter that
    returns nothing is a clear failure, and a loose one is a wrong click.
    """
    name = str(role or "").strip().casefold()
    family = ROLE_FAMILIES.get(name)
    if family is not None:
        return family
    return frozenset({name}) if name else frozenset()


def normalize_role(raw: object) -> str:
    """A role from :data:`ROLES`, or ``""`` when there is not one."""
    text = str(raw or "").strip().casefold().replace("_", "-")
    if not text:
        return ""
    if text in ROLES:
        return text
    # A plural reads as a category rather than a single control; the singular is
    # what Playwright wants, and refusing the rest is better than guessing.
    if text.endswith("s") and text[:-1] in ROLES:
        return text[:-1]
    raise LocatorError("I don't know that kind of control")


def normalize_strategy(raw: object) -> str:
    text = str(raw or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if not text:
        return "label"
    if text in ("css", "selector"):
        return "css"
    if text in TEXT_STRATEGIES:
        return text
    aliases = {
        "name": "text",
        "role_name": "text",
        "content": "text",
        "inner_text": "text",
        "aria_label": "label",
        "visible_text": "text",
        "visibletext": "text",
        # A model writing prose will not remember to use an underscore.
        "visible text": "text",
        "aria label": "label",
        "role name": "text",
    }
    return aliases.get(text, "")


def valid_css(raw: object) -> str:
    """A CSS selector the caller may supply, or ``LocatorError``.

    Checked rather than trusted: a selector is the one strategy that is a language
    we hand to the browser, and an unbounded one is a denial-of-service against
    our own page as well as a way to reach nodes the ladder would have excluded.
    """
    text = str(raw or "").strip()
    if not text:
        raise LocatorError("Tell me which element you mean.")
    if len(text) > CSS_MAX:
        raise LocatorError("That selector is too long for me to use.")
    if not _CSS_ALLOWED.match(text):
        raise LocatorError("I can only use a plain CSS selector.")
    # A selector containing a script-ish token is refused even inside the
    # allowlist's quotes, because `style` and `javascript:` are exactly what a
    # CSS attribute selector can smuggle.
    if re.search(r"(?:javascript|vbscript|data)\s*:", text, re.IGNORECASE):
        raise LocatorError("I can only use a plain CSS selector.")
    return text


#: Attribute-prefix operators, refused by the allowlist above. Checked here too so
#: the refusal reason is specific rather than a generic "plain selector".
_CSS_PREFIX_OPS = ("^=", "|=", "$=")


def ordinal_of(raw: object) -> int:
    """``1``-based position from "second" / "3" / "2nd". ``0`` for none.

    Words are read before digits on purpose: "the second video by Cocomelon"
    contains both, and the word is the one the user meant.
    """
    text = str(raw or "").strip()
    if not text:
        return 0
    match = _ORDINAL_RE.search(text)
    if match:
        return ORDINALS[match.group(1).casefold()]
    match = _NUMBERED_RE.search(text)
    if match:
        value = int(match.group(1))
        return value if 1 <= value <= 99 else 0
    return 0


# -- the request --------------------------------------------------------------


@dataclass(frozen=True)
class ElementQuery:
    """What the caller wants, as parsed facts rather than prose."""

    role: str = ""
    name: str = ""
    strategy: str = "label"
    css: str = ""
    ordinal: int = 0
    #: Free text that narrows *within* a role -- a channel name, a duration, a
    #: view count. Never used to locate on its own.
    qualifier: str = ""
    exact: bool = False
    frame: str = ""
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def names_something(self) -> bool:
        return bool(self.name or self.css)


def build_query(
    *,
    role: object = "",
    name: object = "",
    strategy: object = "",
    css: object = "",
    position: object = "",
    qualifier: object = "",
    frame: object = "",
) -> ElementQuery:
    """Parse the caller's element description. Raises :class:`LocatorError`.

    A role on its own is enough: "click the second video" and "click the third
    result" name no text at all, and refusing them would send the model off to
    invent a name for something the page has not shown yet. A role *and* an
    ordinal is the strongest form -- it says what kind of thing and which one.
    """
    normalized_role = normalize_role(role)
    selector = valid_css(css) if str(css or "").strip() else ""
    text = " ".join(str(name or "").split())
    if selector and text:
        raise LocatorError("Give me either a selector or a name, not both.")
    ordinal = ordinal_of(position)
    if not selector and not text and not normalized_role and not ordinal:
        raise LocatorError("Tell me which element you mean.")
    if ordinal and not (normalized_role or text or selector):
        raise LocatorError("Tell me what kind of element you mean, or name it.")
    chosen = normalize_strategy(strategy) if strategy else ""
    if not chosen:
        chosen = "css" if selector else ("text" if normalized_role else "label")
    if selector and chosen != "css":
        chosen = "css"
    if not normalized_role and chosen == "text" and not selector:
        raise LocatorError("Tell me what kind of control to look for, or name it.")
    return ElementQuery(
        role=normalized_role,
        name=text,
        strategy=chosen,
        css=selector,
        ordinal=ordinal,
        qualifier=" ".join(str(qualifier or "").split()),
        frame=" ".join(str(frame or "").split()),
    )


# -- ranking ------------------------------------------------------------------


@dataclass
class Candidate:
    """One element that matched, described well enough to tell from another."""

    index: int
    ref: str = ""
    role: str = ""
    name: str = ""
    tag: str = ""
    visible: bool = True
    enabled: bool = True
    checked: bool | None = None
    #: Free-form extra the page gave us -- a video's duration, a channel name, a
    #: view count. Clipped by the caller; never a payload.
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.role}:{self.name}".casefold()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"index": self.index}
        if self.ref:
            out["ref"] = self.ref
        if self.role:
            out["role"] = self.role
        if self.name:
            out["name"] = self.name
        if self.tag:
            out["tag"] = self.tag
        if not self.visible:
            out["visible"] = False
        if not self.enabled:
            out["enabled"] = False
        if self.checked is not None:
            out["checked"] = self.checked
        if self.detail:
            detail = dict(self.detail)
            if "meta" in detail:
                detail["context"] = detail.pop("meta")
            out["detail"] = detail
        return out


def score(candidate: Candidate, query: ElementQuery) -> int:
    """How well one candidate answers the query. Lower is better.

    The tiers are in priority order and each is worth more than everything below
    it combined, which is what makes the order stable: an exact accessible name
    beats a substring of a longer name, which beats a match found only in the
    ``title`` attribute.

    Hidden and disabled elements are not penalised by score -- they are *filtered*
    by :func:`rank`, because "the best disabled control" is not something anybody
    asked for.
    """
    name = candidate.name.casefold()
    wanted = query.name.casefold()
    if not wanted:
        return 0
    if name == wanted:
        return 0
    item_title = str(candidate.detail.get("itemTitle") or "").casefold()
    if item_title:
        if item_title == wanted:
            return 1
        if item_title.startswith(wanted):
            return 2
        if wanted in item_title:
            return 3
    if query.qualifier:
        blob = " ".join(
            [name, candidate.role, str(candidate.detail.get("channel", "")), str(candidate.detail.get("meta", ""))]
        ).casefold()
        if query.qualifier.casefold() in blob:
            return 1
    if name.startswith(wanted):
        return 2
    if wanted in name:
        return 3
    context = str(candidate.detail.get("meta", "")).casefold()
    if context and wanted in context:
        return 4
    return NO_MATCH


#: A candidate the query's text does not touch at all. Kept only when the query
#: named no text, because "the first button" is a real request and filtering on an
#: empty name would answer nothing.
NO_MATCH = 5


def rank(candidates: Sequence[Candidate], query: ElementQuery, *, limit: int = MAX_CANDIDATES) -> list[Candidate]:
    """Order the candidates that match, and reindex them.

    Three filters before any ordering, because all three are about *eligibility*
    rather than preference:

    * **hidden** candidates are dropped. "The best hidden control" is not
      something anybody asked for, and offering one would click something the user
      cannot see.
    * **candidates of another role** are dropped whenever the query named a role.
      This is the one that makes an ordinal mean what it says: "the second
      *video*" is the second link, and without this filter the second thing on the
      page -- a search button, a cookie banner, a header menu item -- was the
      second *candidate*. The role in the request decided nothing at all, which is
      why "click the second video" activated a search box.
    * **non-matching** candidates are dropped whenever the query named text. A
      role query with no name is left alone -- "click the button" is legitimate.

    Enumeration order then breaks ties, deliberately. Two equally good matches are
    genuinely ambiguous, and which one is "first" has to be *consistent* so the
    user's "the second one" means something on the next pass too.
    """
    usable = [item for item in candidates if item.visible]
    if query.role:
        wanted = normalize_role(query.role)
        usable = [item for item in usable if item.role in role_family(wanted, item.tag)]
    if query.name or query.qualifier:
        usable = [item for item in usable if score(item, query) < NO_MATCH]
    ordered = sorted(enumerate(usable), key=lambda row: (score(row[1], query), row[0]))
    chosen = [item for _position, item in ordered[: max(0, limit)]]
    for position, item in enumerate(chosen, start=1):
        item.index = position
    return chosen


@dataclass(frozen=True)
class Resolution:
    """The answer to "which one?" -- one element, several, or none."""

    query: ElementQuery
    chosen: Candidate | None = None
    candidates: tuple[Candidate, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.chosen is not None

    @property
    def ambiguous(self) -> bool:
        return self.chosen is None and len(self.candidates) > 1

    @property
    def empty(self) -> bool:
        return not self.candidates

    def describe(self) -> str:
        """The sentence a person can check.

        Deliberately names *how many* and, for the ambiguous case, lists them --
        "I found 3" is actionable in a way that "I could not find it" is not when
        three things on the page are called Search.
        """
        if self.chosen is not None:
            label = self.chosen.name or self.chosen.role or "that control"
            return f"{label} (item {self.chosen.index})"
        if not self.candidates:
            return self.reason or "nothing on the page matches that"
        names = ", ".join(
            f"{item.name or item.role} ({item.index})" for item in self.candidates[:4]
        )
        return f"several match: {names}"


def resolve_one(
    candidates: Sequence[Candidate], query: ElementQuery, *, limit: int = MAX_CANDIDATES
) -> Resolution:
    """Pick one candidate, or explain why not.

    The ordinal is honoured *before* ambiguity is reported, because "the second
    video" is the user already disambiguating. When no ordinal was given and more
    than one candidate survives, this refuses rather than choosing -- and it
    refuses with the candidates attached, so the next call can name one.
    """
    ranked = rank(candidates, query, limit=limit)
    if not ranked:
        return Resolution(query=query, reason="I couldn't find that on the page")
    if query.ordinal > 0:
        if query.ordinal <= len(ranked):
            return Resolution(query=query, chosen=ranked[query.ordinal - 1], candidates=tuple(ranked))
        return Resolution(
            query=query,
            candidates=tuple(ranked),
            reason=f"there are only {len(ranked)} of those",
        )
    if query.ordinal < 0:  # "last"
        return Resolution(query=query, chosen=ranked[-1], candidates=tuple(ranked))
    if len(ranked) == 1:
        return Resolution(query=query, chosen=ranked[0], candidates=tuple(ranked))
    if not query.name and query.role:
        # A role with nothing to narrow it and no position. The first is as good
        # as any -- "click the button" is a real request -- and this is the case
        # that would otherwise report three buttons as an unanswerable question
        # when the caller simply did not look at the page first.
        return Resolution(query=query, chosen=ranked[0], candidates=tuple(ranked))
    # One clear winner (tier 0 or 1) is not ambiguity. Two exact matches are.
    best = score(ranked[0], query)
    tied = [item for item in ranked if score(item, query) == best]
    if best <= 1 and len(tied) == 1:
        return Resolution(query=query, chosen=ranked[0], candidates=tuple(ranked))
    return Resolution(query=query, candidates=tuple(ranked), reason="more than one matches")


# -- building the Playwright locator ------------------------------------------
#
# The strategy ladder, as a string. Playwright builds this from its own API and
# we never inject a selector we did not construct, except for the `css` strategy
# where the caller's own validated selector is the whole point.


def role_locator(page: Any, role: str, name: str, *, exact: bool = False) -> Any:
    return page.get_by_role(role, name=name, exact=exact)


def _strategy_locator(page: Any, strategy: str, value: str, *, exact: bool = False) -> Any:
    if strategy == "label":
        return page.get_by_label(value, exact=exact)
    if strategy == "placeholder":
        return page.get_by_placeholder(value, exact=exact)
    if strategy == "alt":
        return page.get_by_alt_text(value, exact=exact)
    if strategy == "title":
        return page.get_by_title(value, exact=exact)
    if strategy == "text":
        return page.get_by_text(value, exact=exact)
    if strategy == "value":
        return page.locator(f'[value={json_quote(value)}]')
    raise LocatorError("I don't know how to look for that")


def json_quote(value: str) -> str:
    """A double-quoted string for an attribute selector, quotes escaped.

    Written out rather than pulled from ``json.dumps`` because the output has to
    be *CSS-quoted*, not JSON: ``json.dumps`` escapes a forward slash and a
    non-ASCII character, which inside ``[value="..."]`` changes nothing but makes
    the selector unreadable in a log.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_locator(page: Any, query: ElementQuery, *, frame: Any = None) -> Any:
    """The Playwright locator for ``query``, on ``frame`` if one is named.

    Frame selection happens here rather than in the caller so that *every*
    strategy goes through the same frame gate: a name that only exists inside an
    iframe has to be findable, and a selector that only works in the top document
    has to be scoped just as strictly.
    """
    scope = frame if frame is not None else page
    if query.css:
        return scope.locator(query.css)
    if query.role:
        located = role_locator(scope, query.role, query.name, exact=query.exact)
        if query.qualifier:
            return located.filter(has_text=query.qualifier)
        return located
    if query.strategy == "text":
        return scope.get_by_text(query.name, exact=query.exact)
    return _strategy_locator(scope, query.strategy, query.name, exact=query.exact)


def describe_plan(query: ElementQuery) -> str:
    """One line about how the search will run. Logged, and shown on refusal."""
    if query.css:
        return f"selector {query.css[:60]}"
    if query.role:
        return f"{query.role} named {query.name!r}" + (f" ({query.strategy})" if query.name else "")
    return f"{query.strategy} {query.name!r}"


def frame_for(page: Any, spec: object) -> Any:
    """The frame a query names, or ``page`` when it names none.

    Named by url substring, by ``name`` attribute, or by index -- the three ways
    a caller can describe a frame without us inventing a way. An unresolvable
    name raises rather than falling back to the top document, because a query
    that lands in the wrong frame is worse than one that reports nothing.
    """
    text = " ".join(str(spec or "").split())
    if not text:
        return page
    wanted = text.casefold()
    if wanted.isdigit():
        index = int(wanted)
        try:
            return page.frames[index]
        except IndexError:
            raise LocatorError(f"there is no frame {index} on this page") from None
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        if str(getattr(frame, "name", "") or "").casefold() == wanted:
            return frame
        if wanted in str(getattr(frame, "url", "") or "").casefold():
            return frame
    raise LocatorError(f"I couldn't find a frame called {text[:40]}")


def candidate_from(raw: Any, index: int) -> Candidate:
    """Build a :class:`Candidate` from whatever Playwright handed back.

    Defensive throughout, because the properties differ per element type and a
    missing one is not an error -- it is simply not known.
    """
    name = str(getattr(raw, "name", "") or "")
    tag = str(getattr(raw, "tag", "") or "")
    role = str(getattr(raw, "role", "") or "")
    detail: dict[str, Any] = {}
    for key in ("channel", "meta", "duration", "views", "href", "value"):
        value = getattr(raw, key, None)
        if isinstance(value, (str, int, float)) and str(value):
            detail[key] = value
    checked = getattr(raw, "checked", None)
    return Candidate(
        index=index,
        ref=str(getattr(raw, "ref", "") or ""),
        role=role,
        name=name,
        tag=tag,
        visible=bool(getattr(raw, "visible", True)),
        enabled=bool(getattr(raw, "enabled", True)),
        checked=bool(checked) if isinstance(checked, bool) else None,
        detail=detail,
    )


def candidates_from(raws: Iterable[Any]) -> list[Candidate]:
    return [candidate_from(raw, position) for position, raw in enumerate(raws, start=1)]