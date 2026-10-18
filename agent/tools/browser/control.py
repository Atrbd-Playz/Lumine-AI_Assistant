"""`web_control`: the practical browser surface, with the permission gate in front.

## Why a second browser tool

`browse` exists and works, and it is kept. It is also small by design -- seven
verbs over its own ``r1``/``r2`` references -- which is the right shape for a
model that has to choose between tools on a voice turn's budget of attention.
What it cannot do is *find* an element by meaning, which is the whole difficulty
on a page nobody here wrote: "the second video", "the search box", "the size
dropdown". Minted references are positional, so using them means observing first,
counting, and hoping the page did not re-render in between.

This tool is that missing half, and it is deliberately *not* a replacement:

* ``browse`` -- cheap, ref-based, seven verbs, the right default.
* ``web_control`` -- semantic, snapshot-scoped, verifies after every action, for
  when the target has to be found rather than counted.

Two tools that both fetch a page is one more thing for the model to reconcile, so
the division is by *difficulty*, not by feature, and the tool policy says which to
reach for. Where they overlap -- navigation, back/forward -- both work and the
policy tells the model to prefer `browse`.

## What every action returns

A structured result with a :mod:`agent.runtime.verification` status, the element
that was acted on, and the page state read *afterwards*. "Clicked Search" and
"the page moved to /results" are separate fields on purpose: a click that
returned success and a URL that did not change is the specific failure this tool
exists to stop reporting as a success.

## Security, inherited rather than restated

* Route guard, isolated profile, no downloads: `session`, unchanged.
* **No model-authored JavaScript.** The only scripts are the literals in
  `state`. Element descriptions are turned into Playwright locators by
  `locators`, whose CSS strategy is validated before use.
* **Password fields are refused**, before the question and again in the page.
* **Every form submission is ``sensitive``**, computed from DOM facts in
  `actions`, and the read-back carries the control's label and the host.
"""

from __future__ import annotations

import json
import logging
from typing import Any

try:
    from ...runtime import verification
    from ...runtime.permission_gate import require_approval
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime import verification
    from runtime.permission_gate import require_approval

try:
    from ..tool_results import MAX_PAGE_READ_CHARS, as_payload, wrap_untrusted
    from ..tools_compat import RunContext, ToolError, function_tool
    from ..tools_text import clip
    from . import locators, preview, search, session as browser_session, state
    from .actions import (
        parse_tab_request,
        TAB_ACTION_CLASS,
        TAB_ASK_VERB,
        classify_click,
        classify_key,
        fill_deny_reason,
        hostname_of,
        is_search_control,
        normalize_key,
        normalize_scroll,
        normalize_url,
        observe_payload,
        unknown_action_message,
    )
    from .engine import EngineAction, EngineError, GenerationGate, build_engine
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tool_results import MAX_PAGE_READ_CHARS, as_payload, wrap_untrusted  # type: ignore[no-redef]
    from tools_compat import RunContext, ToolError, function_tool
    from tools_text import clip
    from browser import locators, preview, search, session as browser_session, state
    from browser.actions import (
        parse_tab_request,
        TAB_ACTION_CLASS,
        TAB_ASK_VERB,
        classify_click,
        classify_key,
        fill_deny_reason,
        hostname_of,
        is_search_control,
        normalize_key,
        normalize_scroll,
        normalize_url,
        observe_payload,
        unknown_action_message,
    )
    from browser.engine import EngineAction, EngineError, GenerationGate, build_engine

logger = logging.getLogger("lumine")

#: The verbs. Closed, because an unknown verb must be refused by name rather
#: than passed through to a page.
ACTIONS: tuple[str, ...] = (
    "open",       # navigate
    "read",       # observe + return candidates
    "find",       # resolve a query, return what matched
    "search",     # type into this page's own search box and submit it
    "click",
    "double_click",
    "right_click",
    "hover",
    "focus",
    "fill",
    "clear",
    "type",
    "press",
    "select",
    "check",
    "uncheck",
    "drag",
    "scroll",     # move the viewport; no control, no reference
    "back",
    "forward",
    "reload",
    "tab",        # the browser's own tabs: list, new, switch, close
    "pagetabs",   # the tab strip *on the page* (was "tabs")
    "state",      # url, title, viewport, form values
    "screenshot",
    "close",
)

#: Spellings that used to mean something else and still work.
#:
#: `tabs` was the page's own tab strip, and it was also the obvious word for the
#: browser's tabs -- so one verb had to mean whichever thing the caller meant.
#: `tab` now covers the browser's, and `tabs` is kept as an alias for the page's
#: rather than silently becoming the other one: a caller that still says "tabs"
#: gets what it used to get, and the collision is gone rather than moved.
#:
#: The rest are words a model reaches for before it reaches for ours, and every
#: one of them is a *refusal* it has to recover from on a voice turn. "goto" and
#: "navigate" are `open`; "look" and "inspect" are `read`; "choose" and "pick"
#: are `select`. Aliases rather than removals, so a verb nobody has to learn
#: still lands on the one that exists.
VERB_ALIASES: dict[str, str] = {
    "tabs": "pagetabs",
    "goto": "open",
    "navigate": "open",
    "visit": "open",
    "go_to": "open",
    "extract": "read",
    "get": "read",
    "look": "read",
    "inspect": "read",
    "choose": "select",
    "pick": "select",
}

#: Verbs that only read. Everything here changes no state on the page, so it is
#: ``read`` by the same argument `browse` uses for `open`. `scroll` belongs here
#: for the same reason `back` and `forward` do: it moves the *viewport*, which is
#: the reader's own position, and nothing on the page changes.
READ_ACTIONS = frozenset({"open", "read", "find", "scroll", "back", "forward", "reload", "tab", "pagetabs", "state", "screenshot"})

#: `search` is absent from `READ_ACTIONS` on purpose, and it is the only verb
#: whose class is computed rather than fixed: typing and submitting a query moves
#: the page, so it is classified from the DOM facts of the field it found -- the
#: same `classify_click` that decides whether an ordinary click submits anything.

#: Verbs that change nothing about the page but do end the browser.
LIFECYCLE_ACTIONS = frozenset({"close"})

#: `tab` decides its own class per operation rather than being fixed: listing,
#: switching and opening are reads, and the two that destroy a page are acts.
#: `TAB_ACTION_CLASS` and `TAB_ASK_VERB` are imported from `actions`, so the two
#: browser tools cannot disagree about what a tab operation costs the user.

#: Verbs whose class is decided by the *element*, not by the verb. A submit is a
#: submission whichever of these is used.
ELEMENT_ACTIONS = frozenset({"click", "double_click", "right_click", "press", "select", "check", "uncheck"})

#: A screenshot is an image the model will read on every later turn. Capped, and
#: returned as a description plus the bytes -- never as an unbounded payload.
SCREENSHOT_CHARS = 120

#: How a `read` splits its one budget between the page's text and its controls.
#: Both are here rather than inline because the two numbers are not independent:
#: they have to add up under `MAX_PAGE_READ_CHARS`, and two call sites that each
#: chose "a reasonable amount" were how the payload arrived cut through the
#: middle of the control list. The text half leads because it is where the
#: region read puts the thing the user actually asked about -- the price -- and
#: the control half is what the model needs to *act* on the page afterwards.
#: 1,200 + 20 controls at ~60 characters is comfortably inside the ceiling, so
#: `as_payload` has to cut neither.
MAX_READ_CONTROLS = 20
READ_TEXT_CHARS = 1200

MSG_BAD_ACTION = "Tell me one of: " + ", ".join(ACTIONS) + "."
MSG_BAD_SCROLL = "Tell me where to scroll: down, up, top, or bottom. Nothing moved."
MSG_NO_URL = "Tell me which page to open."
MSG_BAD_URL = "I can only open web addresses (http or https) -- I won't open that. Nothing was shown."
MSG_BAD_QUERY = "Tell me which control: its name, or a role and a name."
MSG_NO_QUERY = "Tell me what to search for."
MSG_AMBIGUOUS = "more than one thing on the page matches"
MSG_STALE = "the page changed since I looked at it"
MSG_NOT_FILLABLE = "That control doesn't take text. Nothing was typed."
MSG_NO_SUCH_OPTION = "That dropdown doesn't offer that choice. Nothing was chosen."
MSG_BAD_KEY = "I can only press Enter, Escape, Tab, Space, Backspace, Delete, Home, End, PageUp, PageDown, or an arrow key. Nothing was pressed."
MSG_FILL_PASSWORD = "I won't type into a password field. Nothing was typed."
MSG_BAD_TAB = (
    "I did not understand that. Say 'tab' to list them, 'new' to open one, a tab "
    "number or 'first'/'last'/'next' to go to one, or 'close' (with a number) to "
    "close one. Nothing was done."
)
MSG_NO_PAGE = "No page is open yet -- open one first."


def _tab_sentence(op: str, data: dict, rows: list[dict]) -> str:
    """One sentence saying what a tab operation did, in the user's terms.

    Reported rather than assumed because two of the operations change what is on
    screen, and "I opened tab 2" is not the interesting half of a switch -- the
    page she is now looking at is.
    """
    if op == "new":
        where = clip(str(data.get("title") or data.get("url") or ""), 60)
        said = f"Opened tab {data.get('tab')}"
        if where and where != "about:blank":
            said += f" on {where}"
        else:
            said += ". It is blank"
        return said + "."
    if op == "switch":
        where = clip(str(data.get("title") or data.get("url") or ""), 60)
        if where == "about:blank":
            where = ""
        return (
            f"On tab {data.get('tab')} now" + (f": {where}" if where else "")
            + ". Read it before acting -- earlier references belong to the tab you left."
        )
    count = int(data.get("count") or 0)
    said = f"Closed tab {data.get('closed')}. {count} tab{'s' if count != 1 else ''} open"
    if not count:
        return said + " -- a blank tab is there now."
    active = next((row for row in rows if row.get("active")), None)
    if active:
        said += f", on {clip(str(active.get('title') or active.get('url') or ''), 50)} now"
    return said + "."


# -- result shaping -----------------------------------------------------------


def _payload(**fields: Any) -> str:
    """A page read, under the *page* ceiling rather than the global one.

    Every result here is a read of a page, and the global 800-character ceiling
    was cutting them mid-structure: `read` alone asks for text plus a control
    list, and `as_payload` answered by halving the list, so the model received
    four controls and no way to know there were twenty. The larger ceiling is
    still a ceiling -- a read joins the context permanently and is re-billed on
    every later turn -- so it applies to this path and nothing else.
    """
    return wrap_untrusted(as_payload(fields, max_chars=MAX_PAGE_READ_CHARS))


def _failure(action: str, detail: str) -> ToolError:
    return ToolError(detail)


class _Engine:
    """Indirection so a test can swap the engine without patching module globals
    six times. One attribute, assigned in `setUp`."""

    engine: Any = None

    @classmethod
    def get(cls) -> Any:
        if cls.engine is not None:
            return cls.engine
        return build_engine()


def engine() -> Any:
    return _Engine.get()


def set_engine(value: Any) -> None:
    """Install an engine. Tests and nothing else."""
    _Engine.engine = value


# -- helpers ------------------------------------------------------------------


def _query(
    *,
    name: str = "",
    role: str = "",
    strategy: str = "",
    css: str = "",
    position: str = "",
    frame: str = "",
) -> locators.ElementQuery:
    try:
        return locators.build_query(
            name=name,
            role=role,
            strategy=strategy,
            css=css,
            position=position,
            frame=frame,
        )
    except locators.LocatorError as exc:
        raise ToolError(str(exc)) from None


def _is_password(candidate: locators.Candidate) -> bool:
    """Whether the collected facts say this is a password field.

    Read from the *name*, which for a password field is the only honest signal
    available without a second page call: an ``aria-label``, a placeholder, a
    ``name``, or an id the page chose. It errs toward refusing, which is the
    correct direction for the one field this product never types into. The refusal
    is unconditional and is made *before* the question, so there is no
    "allow once" that would put a password into a page.
    """
    text = f"{candidate.name} {candidate.detail.get('value', '')}".casefold()
    return any(word in text for word in ("password", "passwd", "pass code", "pin"))


def _stamp(payload: Any, gate: GenerationGate | None) -> state.Snapshot:
    """The snapshot for a read that already happened, and its generation.

    Split out from :func:`_observe` so a verb that needs the *raw* payload -- the
    one carrying ``forms`` and ``viewport``, which is collected on every read and
    was being thrown away -- stamps the same generation it would have got anyway.
    One read, one generation, whichever caller asked for it.
    """
    store = gate
    if store is None:
        return state.snapshot_from_script(1, payload)
    return store.stamp(payload)


async def _observe(engine_obj: Any, *, gate: GenerationGate | None = None) -> state.Snapshot:
    payload = await engine_obj.observe()
    store = getattr(engine_obj, "gate", None) or gate
    return _stamp(payload, store)


async def _tabs_note(engine_obj: Any) -> dict:
    """Which tab a read was of, so no read has to be *about* tabs to be safe.

    The same reasoning as `browse`'s, and the same fix: a model told which tab it
    is looking at cannot act on the wrong one, and asking for that costs a turn
    she usually does not have. Best-effort -- it is awareness, and awareness must
    never be the reason a read fails.
    """
    getter = getattr(engine_obj, "tab_context", None)
    if getter is None:
        return {}
    try:
        context = await getter()
    except Exception:  # noqa: BLE001 - see the docstring
        return {}
    return context if isinstance(context, dict) else {}


def _element_summary(chosen: locators.Candidate) -> str:
    label = clip(chosen.name or chosen.role or "control", 40)
    host = ""
    return f"{label}{host}"


# -- the tool -----------------------------------------------------------------


@function_tool()
async def web_control(
    context: RunContext,
    action: str,
    url: str = "",
    name: str = "",
    role: str = "",
    text: str = "",
    key: str = "",
    strategy: str = "",
    css: str = "",
    index: int = 0,
    frame: str = "",
    position: str = "",
    for_model: bool = False,
) -> str:
    """Drive a page in Lumine's own browser by what the controls *are*.

    Use this when a control has to be *found* rather than counted -- "the search
    box", "the second video", "the size dropdown". Use `browse` instead when you
    can see the control in an `observe` result, and `search_web` for a plain
    question: this starts a real browser window.

    Actions: open, read, find, search, click, double_click, right_click, hover,
    focus, fill, clear, type, press, select, check, uncheck, drag, scroll, back,
    forward, reload, tab, pagetabs, state, screenshot, close.

    "tab" is the browser's own tabs, same as `browse`: say nothing to list them,
    "new" to open one (give the address in url), a tab's number to switch to it,
    or "close" to close one. "pagetabs" is the tab strip *on the page* -- a form's
    own sections, read out of the document. They were both called "tabs" once,
    which meant one word for two things; "tabs" still works and still means
    "pagetabs".

    **"open" reuses the tab in use; "tab new" adds one.** When the user wants to
    keep what is on screen, use "tab new" with the url -- "open" navigates the
    current tab and throws the page away.

    **"read" shows the part of the page in view, not the top of it**, and its
    payload's `scroll` says how far down you are and how much page is left. To
    read further down: scroll, then read again. A read whose text *and*
    `scroll.y` match the last one means nothing moved -- scroll further rather
    than reading it again; what the user asked about (a price, a spec, a date)
    is usually below the fold.

    Every read says which tab it read, so you never have to ask.

    Every action checks what it did and reports it. A click that does not move the
    page says so rather than claiming it worked. Submitting a form always asks
    first, and password fields are refused -- Lumine never types passwords.
    References go stale after any action; `read` again before the next one.

    Args:
        action: What to do. See the list above.
        url: The page to open, for action "open".
        name: What to find, for the element actions -- the visible text,
            accessible name, label, placeholder, alt text, or a partial match.
        role: What kind of control: button, link, textbox, searchbox, checkbox,
            radio, combobox, listbox, option, tab, menuitem, heading, image,
            video, slider.
        text: What to search for on action "search", the text to type, fill,
            choose, or drag direction ("down 300"), or where to scroll on action
            "scroll" -- "down", "up", "top", or "bottom".
        key: The key to press, for action "press".
        strategy: How to read `name`: label, placeholder, alt, title, text,
            value, or css. Defaults to label.
        css: A CSS selector, as the last resort instead of name/role.
        index: Which of several matches, 1-based. Omit when you want the one
            unambiguous match.
        frame: Which iframe, by name, url fragment, or index, when the control
            is inside one.
        position: Which match you mean when you say it in words -- "second",
            "third", "2". Redundant with index; either is fine.
        for_model: On action "screenshot", also show the picture to yourself, not
            just the user. Off by default -- a picture is paid for again on every
            later turn for the rest of the conversation, so ask for it only when
            the text of the page cannot tell you what you need, such as a canvas, a
            map, or a control drawn as an image.
    """
    normalized = str(action or "").strip().lower().replace(" ", "_").replace("-", "_")
    normalized = VERB_ALIASES.get(normalized, normalized)
    if normalized not in ACTIONS:
        # The dynamic sentence rather than the constant: it names the likely
        # verb first (`scoll` is `scroll`), which is the half a model can act on
        # during a voice turn. `MSG_BAD_ACTION` stays the module's own record of
        # the surface and is still what the tool documents.
        raise ToolError(unknown_action_message(action, ACTIONS))

    engine_obj = engine()
    gate = getattr(engine_obj, "gate", None)

    # -- reads ---------------------------------------------------------------
    if normalized == "open":
        raw = str(url or "").strip()
        if not raw:
            raise ToolError(MSG_NO_URL)
        try:
            target = normalize_url(raw)
        except ValueError:
            raise ToolError(MSG_BAD_URL) from None
        await require_approval(
            context,
            tool="web_control",
            action="open",
            action_class="read",
            summary=f"Open {hostname_of(target)}",
            target=clip(target, 100),
        )
        try:
            page = await engine_obj.navigate(target)
        except EngineError as exc:
            raise ToolError(f"{exc}") from None
        snapshot = await _observe(engine_obj, gate=gate)
        # Same honesty as `browse`: `open` reuses the tab in use, so when other
        # tabs exist the model is told it replaced one and that `tab new` is the
        # way to keep it. Without this the page is simply gone and nothing said.
        payload = {
            "title": clip(str(page.get("title") or ""), 70),
            "url": clip(str(page.get("url") or ""), 90),
            "generation": snapshot.generation,
            "status": "opened",
        }
        tabs = await _tabs_note(engine_obj)
        if tabs.get("tab") is not None:
            payload["tab"] = tabs["tab"]
            if int(tabs.get("tabCount") or 0) > 1:
                payload["note"] = (
                    f"This replaced the page in tab {tabs['tab']}. Use action 'tab' "
                    "with 'new' to open another instead."
                )
        return _payload(**payload)

    if normalized in ("read", "find"):
        action_class = "read"
        await require_approval(
            context,
            tool="web_control",
            action=normalized,
            action_class=action_class,
            summary="Read the current page" if normalized == "read" else "Find a control on the page",
        )
        raw = await engine_obj.observe()
        snapshot = _stamp(raw, gate)
        if normalized == "read":
            # Text and controls are budgeted *together*, on purpose. The read
            # ceiling is one number for the whole payload, so clipping the text
            # to 300 and the controls to 8 was two independently-reasonable
            # choices that summed to a page nobody could act on: the region read
            # put the price in the text and the clip threw it away, and eight
            # controls was a page's header. These two numbers add up under the
            # ceiling, so `as_payload` has to cut neither.
            return _payload(
                title=clip(snapshot.title, 70),
                url=clip(snapshot.url, 90),
                text=clip(snapshot.text, READ_TEXT_CHARS),
                generation=snapshot.generation,
                controls=[item.to_dict() for item in snapshot.candidates[:MAX_READ_CONTROLS]],
                count=len(snapshot.candidates),
                # The per-site extraction, when the page's own map has one. A
                # price under a stable name rather than a number inside a
                # paragraph -- see `site_map`.
                **({"fields": snapshot.fields} if snapshot.fields else {}),
                # Where the windowed text was read from. `read` is the same
                # windowed read `browse observe` makes, and a text with no
                # position beside it cannot be followed up with "scroll and
                # read again" -- the model has no number to check moved.
                **(
                    {"scroll": raw["scroll"]}
                    if isinstance(raw.get("scroll"), dict)
                    else {}
                ),
                **(await _tabs_note(engine_obj)),
            )
        query = _query(
            name=name,
            role=role,
            strategy=strategy,
            css=css,
            position=position,
            frame=frame,
        )
        resolution = state.locate(snapshot, query)
        # `state.report` is the sentence a person would use -- "matched X, or
        # here are the three things it could have been". The raw candidate list
        # was already in the payload and the model still guessed, because a list
        # of objects does not say that the choice is *unmade*.
        return _payload(
            matched=bool(resolution.ok),
            generation=snapshot.generation,
            chosen=resolution.chosen.to_dict() if resolution.chosen else {},
            candidates=[item.to_dict() for item in resolution.candidates[:6]],
            note=state.report(snapshot, resolution),
        )

    if normalized in ("back", "forward", "reload"):
        await require_approval(
            context,
            tool="web_control",
            action=normalized,
            action_class="read",
            summary=f"Go {normalized} in the browser",
        )
        try:
            if normalized == "reload":
                page = await engine_obj.reload()
            else:
                page = await engine_obj.history(normalized)
        except EngineError as exc:
            raise ToolError(f"{exc}") from None
        snapshot = await _observe(engine_obj, gate=gate)
        return _payload(
            moved=bool(page.get("moved", True)),
            title=clip(str(page.get("title") or ""), 70),
            url=clip(str(page.get("url") or ""), 90),
            generation=snapshot.generation,
        )

    # -- scroll ---------------------------------------------------------------
    #
    # A read by the rule that makes `back` and `forward` reads: it moves the
    # *viewport*, which is the reader's own position, and nothing on the page
    # changes. It belongs here rather than in `browse` only because a model
    # already inside `web_control` had no way to move down a page at all -- it
    # reached for `open` (destroying the page) or re-read the same viewport and
    # got the same answer twice, which is the reported failure.
    if normalized == "scroll":
        try:
            direction = normalize_scroll(text)
        except ValueError:
            raise ToolError(MSG_BAD_SCROLL) from None
        await require_approval(
            context,
            tool="web_control",
            action="scroll",
            action_class="read",
            summary=f"Scroll the page {direction}",
        )
        try:
            data = await browser_session.scroll_page(direction)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - translated by the tool
            raise EngineError(f"I could not scroll: {str(exc)[:120]}") from None
        if not data.get("scrolled"):
            # Honest about a page that has nothing below it. "Scrolled" about a
            # page that never moved is a claim with nothing behind it, and the
            # model would re-ask instead of reading what is already on screen.
            return _payload(scrolled=False, note="That page fits on screen -- there is nothing to scroll.")
        landed = int(data.get("y") or 0)
        limit = int(data.get("max") or 0)
        note = {
            "top": "Scrolled to the top of the page.",
            "bottom": "Scrolled to the bottom of the page.",
            "up": "Scrolled up." + (" That is the top of the page." if landed <= 0 else ""),
        }.get(direction)
        if note is None:
            note = "Scrolled down." + (" That is the bottom of the page." if landed >= limit else "")
        # The generation is *not* bumped and no new snapshot is taken: the
        # document is unchanged, so every reference the model already holds is
        # still valid. That is the whole reason this is its own verb rather than
        # something the model fakes with a re-read.
        return _payload(scrolled=True, at=int(landed), max=int(limit), note=note)

    if normalized == "pagetabs":
        await require_approval(
            context,
            tool="web_control",
            action="pagetabs",
            action_class="read",
            summary="List the tabs on this page",
        )
        try:
            data = await engine_obj.tabs()
        except EngineError as exc:
            raise ToolError(f"{exc}") from None
        return _payload(tabs=data.get("tabs") or [], url=clip(str(data.get("url") or ""), 90))

    # -- browser tabs -------------------------------------------------------
    #
    # The same four operations as `browse`'s `tab`, from the same parser, because
    # both tools drive one browser and a model must not find that one of them
    # cannot open a tab. What it does without this is `open` the page in the tab
    # in use, which throws away whatever the user had there.
    if normalized == "tab":
        try:
            op, arg = parse_tab_request(text)
        except ValueError:
            raise ToolError(MSG_BAD_TAB) from None
        target = (arg or url) if op == "new" else ""
        action_class = TAB_ACTION_CLASS[op]

        if op == "list":
            await require_approval(
                context,
                tool="web_control",
                action=TAB_ASK_VERB,
                action_class=action_class,
                summary="List the browser tabs",
            )
            try:
                data = await engine_obj.tab_list()
            except EngineError as exc:
                raise ToolError(f"{exc}") from None
            rows = [row for row in data.get("tabs") or [] if isinstance(row, dict)]
            listed = "; ".join(
                f"tab {row.get('tab')}: {clip(str(row.get('title') or row.get('url') or 'a blank page'), 50)}"
                + (" (the one you're on)" if row.get("active") else "")
                for row in rows
            )
            return _payload(tabs=rows, active=data.get("active"), count=len(rows), said=listed)

        if op == "close_others":
            await require_approval(
                context,
                tool="web_control",
                action=TAB_ASK_VERB,
                action_class=action_class,
                summary="Close every browser tab except this one",
            )
            try:
                data = await engine_obj.close_other_tabs()
            except EngineError as exc:
                raise ToolError(f"{exc}") from None
            rows = [row for row in data.get("tabs") or [] if isinstance(row, dict)]
            gone = data.get("closed") or []
            return _payload(
                said=(
                    f"Closed {len(gone) if isinstance(gone, list) else 1} other tab(s); "
                    f"{int(data.get('count') or 0)} left, and it is the one you were on."
                ),
                count=int(data.get("count") or 0),
                tabs=rows,
            )

        calls = {
            "new": lambda: engine_obj.new_tab(target),
            "switch": lambda: engine_obj.switch_tab(arg),
            "close": lambda: engine_obj.close_tab(arg),
        }
        await require_approval(
            context,
            tool="web_control",
            action=TAB_ASK_VERB,
            action_class=action_class,
            summary=(
                "Open a new browser tab"
                if op == "new"
                else f"{'Close' if op == 'close' else 'Switch to'} browser tab {arg}".strip()
            ),
            target=clip(target or arg, 100),
        )
        try:
            data = await calls[op]()
        except EngineError as exc:
            raise ToolError(f"{exc}") from None
        # `close_tab` reports the whole list; the other two report the tab they
        # touched. Both shapes end up here, so the list is simply whatever came
        # back -- a missing key is an empty list, not a guess.
        rows = [row for row in data.get("tabs") or [] if isinstance(row, dict)]
        return _payload(
            said=_tab_sentence(op, data, rows),
            tab=data.get("tab", data.get("closed")),
            count=int(data.get("count") or 0),
            tabs=rows,
        )

    if normalized == "state":
        await require_approval(
            context,
            tool="web_control",
            action="state",
            action_class="read",
            summary="Read this page's state",
        )
        raw = await engine_obj.observe()
        snapshot = _stamp(raw, gate)
        tabs = await _tabs_note(engine_obj)
        # `_JS_FORM` already ran on this read and had its password fields redacted
        # *in the page*, so returning it costs nothing extra and is the only way
        # the model can check what it actually put into a form. Dropping it is why
        # "fill this in and confirm" was three guesses instead of one reading.
        viewport = (raw.get("viewport") if isinstance(raw, dict) else None) or {}
        title = clip(snapshot.title, 70)
        url = clip(snapshot.url, 90)
        # Everything except the fields, rendered once so `form_values_payload` can
        # measure against it. Building this dict and passing it -- rather than
        # passing a character count -- is what stops the two halves being budgeted
        # separately and the payload arriving cut through the middle of a field.
        around = {
            "title": title,
            "url": url,
            "generation": snapshot.generation,
            "controls": len(snapshot.candidates),
            "ready": clip(snapshot.ready, 12),
            "atBottom": bool(viewport.get("atBottom")),
            "tab": tabs.get("tab", 0),
            "tabCount": tabs.get("tabCount", 0),
            # The largest this key can render, since it is only present when there
            # is something left over and that is the case being measured for.
            "moreFields": 99999,
        }
        forms = state.form_values_payload(
            raw.get("forms") if isinstance(raw, dict) else None, around=around
        )
        # Only when there were more to give. A `more` key on a payload that had
        # everything is a claim the model will act on -- it would go looking for
        # fields that do not exist.
        more = 0
        if isinstance(raw, dict) and isinstance(raw.get("forms"), list):
            more = max(0, len(raw["forms"]) - len(forms))
        return _payload(
            title=title,
            url=url,
            generation=snapshot.generation,
            controls=len(snapshot.candidates),
            ready=clip(snapshot.ready, 12),
            forms=forms,
            atBottom=bool(viewport.get("atBottom")),
            # Budgeted against by `around` above, so the keys it adds are counted
            # rather than arriving free on top of the forms.
            **({"tab": tabs["tab"], "tabCount": tabs.get("tabCount")} if tabs.get("tab") is not None else {}),
            **({"moreFields": more} if more else {}),
        )

    if normalized == "screenshot":
        await require_approval(
            context,
            tool="web_control",
            action="screenshot",
            action_class="read",
            summary="Take a screenshot of the page",
        )
        try:
            data = await engine_obj.screenshot(
                max_edge=state.PREVIEW_MAX_EDGE, quality=state.PREVIEW_QUALITY
            )
        except EngineError as exc:
            raise ToolError(f"{exc}") from None
        raw = await engine_obj.observe()
        where = clip(str((raw or {}).get("title") or ""), 70)
        url = clip(str((raw or {}).get("url") or ""), 90)
        sent = preview.publish_preview(
            context,
            preview.preview_record(data, url=url, title=where, reason="screenshot"),
        )
        if for_model:
            # A tool cannot return an image -- LiveKit accepts only scalars and
            # containers of them -- so the picture goes into the chat context and
            # the tool says what it did. See `preview.show_to_model`.
            return preview.show_to_model(context, data, caption=where or url)
        # The default: the user sees it, and the model is told it exists. A picture
        # in the model's context is re-sent on every later turn for the rest of
        # the session, so it is something to ask for rather than something to pay
        # for on every screenshot.
        return _payload(
            status="captured",
            bytes=len(data),
            shownToUser=sent,
            page=where or url,
            note=(
                "the user can see this screenshot; ask again with for_model=true "
                "if you need to see it yourself"
            ),
        )

    if normalized == "close":
        await require_approval(
            context,
            tool="web_control",
            action="close",
            action_class="act",
            summary="Close the browser",
        )
        try:
            await engine_obj.close()
        except EngineError as exc:
            raise ToolError(f"{exc}") from None
        return "The browser is closed."

    # -- search ---------------------------------------------------------------
    #
    # Ahead of the element actions because it is the one verb that finds its own
    # target: `name`/`role` describe nothing here, the page's own search box does.
    if normalized == "search":
        query = search.normalize_query(text or name)
        if not query:
            raise ToolError(MSG_NO_QUERY)
        raw = await engine_obj.observe()
        snapshot = _stamp(raw, gate)

        async def approve(*, action_class: str, summary: str, target: str) -> None:
            await require_approval(
                context,
                tool="web_control",
                action="search",
                action_class=action_class,
                summary=summary,
                target=target,
            )

        outcome = await search.run_search(
            engine_obj, snapshot, query, approve=approve, context=context
        )
        # One sentence for the user and a payload for the model. They are kept
        # apart because the model needs the *status* -- `accepted` means "I typed
        # it, the page has not confirmed" -- while the user needs the short one.
        said = {
            verification.OBSERVED: f"Searched for {clip(query, 30)}.",
            verification.ACCEPTED: (
                f"I typed {clip(query, 30)} into the search box, "
                "but the page has not shown results yet."
            ),
            verification.FAILED: str(outcome.detail),
        }.get(outcome.status, str(outcome.detail or "That search did not go through."))
        payload = {
            "status": outcome.status,
            "detail": said,
            "url": clip(str(outcome.data.get("url") or snapshot.url), 90),
            "generation": snapshot.generation,
        }
        fields = outcome.data.get("fields")
        if fields:
            # A refusal that names something is retryable; one that says "there is
            # nothing here" is a dead end the model can only answer by guessing.
            payload["fields"] = fields
        return _payload(**payload)

    # -- element actions -----------------------------------------------------
    # `position` is honoured here as well as in `find`: "click the third result"
    # is an action, and a positional argument that only worked on one verb would
    # send the model back to `find` + `index` for no reason.
    query = _query(name=name, role=role, strategy=strategy, css=css, position=position, frame=frame)
    return await _element_action(
        context,
        engine_obj,
        gate,
        normalized,
        query,
        text=text,
        key=key,
        index=int(index or 0),
        frame=frame,
    )


async def _element_action(
    context: RunContext,
    engine_obj: Any,
    gate: GenerationGate | None,
    verb: str,
    query: locators.ElementQuery,
    *,
    text: str = "",
    key: str = "",
    index: int = 0,
    frame: str = "",
) -> str:
    """The shared body of every verb that operates one element.

    Facts first, then the question, then the action, then the check -- in that
    order, and the order is the security property. The gate is passed the control's
    *facts* and never its label, so a page cannot talk its way out of a
    classification by renaming a button.
    """
    snapshot = await _observe(engine_obj, gate=gate)
    resolution = state.locate(snapshot, query)
    chosen = _choose_or_refuse(resolution, index)
    if chosen is None:
        raise ToolError(MSG_AMBIGUOUS)

    label = clip(chosen.name or chosen.role or "control", 40)
    host = hostname_of(snapshot.url)
    on_host = f" on {host}" if host else ""
    target = clip(snapshot.url, 100)

    action_class, verb_for_gate = _classify(verb, chosen, text=clip(text, 200), key=key)

    if verb in ("fill", "type") and _is_password(chosen):
        # Before the question, and without one. A card the user answers "yes" to
        # is not consent to have their password typed anywhere, so this is a
        # refusal rather than a permission class.
        raise ToolError(MSG_FILL_PASSWORD)

    which = ""
    if key.strip():
        try:
            which = normalize_key(key)
        except ValueError:
            raise ToolError(MSG_BAD_KEY) from None

    await require_approval(
        context,
        tool="web_control",
        action=verb_for_gate,
        action_class=action_class,
        summary=f"{_verb_phrase(verb, label, on_host, text=clip(text, 60), key=which)}",
        target=target,
    )

    step = EngineAction(
        kind=_engine_kind(verb),
        query=query,
        index=chosen.index,
        text="" if verb in ("check", "uncheck") else clip(text, 600),
        key=which,
        generation=snapshot.generation,
        expected={
            "name": chosen.name,
            "role": chosen.role,
            "href": str(chosen.detail.get("href") or ""),
        },
        frame=frame,
    )
    try:
        result = await engine_obj.act(step)
    except EngineError as exc:
        raise ToolError(str(exc)) from None
    except locators.LocatorError as exc:
        raise ToolError(str(exc)) from None

    after = result.get("after") or {}
    return _payload(
        status=verification.OBSERVED,
        actedOn=chosen.to_dict(),
        url=clip(str(after.get("url") or ""), 90),
        title=clip(str(after.get("title") or ""), 70),
        note=state.change_report(str(result.get("before", {}).get("url") or ""), str(after.get("url") or "")),
        generation=snapshot.generation,
    )


def _choose_or_refuse(resolution: locators.Resolution, index: int) -> locators.Candidate | None:
    if index and index > 0:
        for candidate in resolution.candidates:
            if candidate.index == index:
                return candidate
        return None
    return resolution.chosen


def _classify(verb: str, chosen: locators.Candidate, *, text: str, key: str) -> tuple[str, str]:
    """The action class, and the verb name the gate records.

    The class comes from DOM facts wherever the verb's class is not fixed:
    a submitter is a submission whichever way it was activated, and an ``Enter``
    inside a form is HTML's implicit submit. Facts only -- never the label,
    which is text the page chose.

    ``type_`` reads ``detail["type"]`` and used to read ``detail["value"]``, which
    is the element's *value* attribute: `<input type="submit" value="Go">` was
    therefore classified an ordinary ``act`` and a form submission went through
    without a question, while ``browse`` -- whose own facts script returns the
    real attribute -- classified the same button ``sensitive``. Two tools, one
    button, two answers, and the wrong one in the direction that never asks.
    """
    in_form = bool(chosen.detail.get("inForm"))
    type_ = str(chosen.detail.get("type") or "")
    search = is_search_control(
        tag=chosen.tag,
        type_=type_,
        role=chosen.role,
        in_form=in_form,
        form_role=chosen.detail.get("formRole"),
        sole_field=bool(chosen.detail.get("soleField")),
    )
    if verb in ELEMENT_ACTIONS:
        action_class = classify_click(
            tag=chosen.tag, type_=type_, in_form=in_form, search=search
        )
        if verb == "press":
            action_class = classify_key(
                normalize_key(key) if key.strip() else "Enter",
                tag=chosen.tag,
                type_=type_,
                in_form=in_form,
                search=search,
            )
        return action_class, verb
    if verb in ("fill", "type", "clear"):
        return "act", verb
    if verb == "select":
        return "act", verb
    if verb in ("hover", "focus", "drag"):
        return "act", verb
    return "act", verb


def _engine_kind(verb: str) -> str:
    return {
        "double_click": "dblclick",
        "right_click": "rightclick",
    }.get(verb, verb)


def _verb_phrase(verb: str, label: str, on_host: str, *, text: str = "", key: str = "") -> str:
    shown = {" " : "Space"}.get(key, key)
    table = {
        "click": f"Click {label}{on_host}",
        "double_click": f"Double-click {label}{on_host}",
        "right_click": f"Right-click {label}{on_host}",
        "hover": f"Hover over {label}{on_host}",
        "focus": f"Focus {label}{on_host}",
        "fill": f"Type into {label}{on_host}",
        "clear": f"Clear {label}{on_host}",
        "type": f"Type into {label}{on_host}",
        "press": f"Press {shown or 'a key'} on {label}{on_host}",
        "select": f"Choose {text} in {label}{on_host}",
        "check": f"Tick {label}{on_host}",
        "uncheck": f"Untick {label}{on_host}",
        "drag": f"Drag {label}{on_host}",
    }
    return table.get(verb, f"{verb} {label}{on_host}")