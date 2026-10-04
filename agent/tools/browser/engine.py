"""The mechanics the control tool needs, behind an interface a fake can replace.

Everything here is a *promise about shape*, not about Playwright. `control.py`
decides what is allowed and what the user is told; this module only knows how to
read a page and act on one. That split is why the decision logic in `control.py`
can be tested against a dictionary-shaped fake while the mechanics are tested
against a real Chromium.

Three properties the seam keeps:

* **Every read stamps a generation** (`state.SnapshotStore`). An action receives
  the generation it was planned against and refuses when it no longer matches, so
  "observe, act" cannot silently skip the observe.
* **Every action re-checks the target before acting**, not just after. A page
  that swaps an element between the read and the click gets a refusal, not a
  click on whatever is now there.
* **Every action returns what it read afterwards.** ``click`` reports the URL and
  title it landed on, so "clicked" and "the page moved" stay separate claims.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import locators, session as browser_session, state
from .actions import hostname_of

logger = logging.getLogger("lumine")

#: Bounds reused by every call so a fake and a real page are held to the same
#: limits, and a test cannot accidentally assert on an unbounded read.
NAV_TIMEOUT_MS = 15000
ACTION_TIMEOUT_MS = 6000


class EngineError(Exception):
    """A page operation could not be completed. Carries a spoken reason."""


class Engine(Protocol):
    """What the control tool needs from a browser. Implemented for real, and by
    a fake in the tests."""

    async def navigate(self, url: str) -> dict[str, Any]: ...
    async def observe(self) -> dict[str, Any]: ...
    async def act(self, step: "EngineAction") -> dict[str, Any]: ...
    async def history(self, direction: str) -> dict[str, Any]: ...
    async def reload(self) -> dict[str, Any]: ...
    async def tabs(self) -> dict[str, Any]: ...
    async def tab_list(self) -> dict[str, Any]:
        """The browser's own tabs. **Not** :meth:`tabs`, which is the tab strip
        *on the page* -- the two were named too similarly to leave to memory."""

        ...

    async def tab_context(self) -> dict[str, Any]: ...
    async def new_tab(self, url: str = "") -> dict[str, Any]: ...
    async def switch_tab(self, which: object) -> dict[str, Any]: ...
    async def close_tab(self, which: object = "") -> dict[str, Any]: ...
    async def close_other_tabs(self) -> dict[str, Any]: ...
    async def screenshot(self, *, max_edge: int = 0, quality: int = 0) -> bytes:
        """A capture of the viewport, downscaled to ``max_edge``.

        JPEG when ``quality`` is set and PNG otherwise, so the two callers can
        ask for the encoding their own budget wants: the preview crosses a data
        channel, the model's copy joins the chat context.
        """

        ...

    async def close(self) -> None: ...


#: One action, in the vocabulary `control.py` uses. A dataclass rather than
#: keyword arguments so the fake can pattern-match on it and a new action cannot
#: be added by quietly growing a signature.


@dataclass
class EngineAction:
    kind: str
    query: locators.ElementQuery | None = None
    #: Which candidate the caller chose from the last read, 1-based. ``0`` means
    #: "whatever the query resolves to", which is only allowed for an exact match.
    index: int = 0
    text: str = ""
    key: str = ""
    generation: int = 0
    #: The identity the target had when it was read. Re-checked before acting.
    expected: dict[str, str] = field(default_factory=dict)
    frame: str = ""
    #: Set by `resolve` steps that only need to *find*, never act.
    resolve_only: bool = False


def _tab_count(payload: Any) -> int:
    """How many tabs a script reported, from a field that holds a *list*.

    The name is the whole trap, and it is worth writing down because it cost a
    session: ``tabs`` is a list of tab descriptors everywhere else -- the
    ``tabs`` action returns one, ``_JS_TABS`` produces one, and the snapshot
    script's own failure path sets it to ``[]`` -- but ``GenerationGate.stamp``
    read it as a count and called ``int()`` on it. Every snapshot that actually
    contained tabs raised ``TypeError: int() argument must be a string, ... not
    'list'``, so ``web_control`` failed on essentially every call against a page
    with more than one frame, reporting a bare Python message with no address.

    Kept because it is the honest description of the field, and because
    ``snapshot_from_script`` derives the count through exactly this tolerance.
    """
    if not isinstance(payload, dict):
        return 0
    tabs = payload.get("tabs")
    if isinstance(tabs, (list, tuple)):
        return len(tabs)
    try:
        return int(tabs)  # a bare number, from a caller that already counted
    except (TypeError, ValueError):
        return 0


class GenerationGate:
    """Holds the last snapshot for one session and refuses stale work.

    A plain counter would be enough if the only failure were a re-read. It is not:
    the same generation on a *different* page -- after a navigation, say -- is
    just as wrong. So a match also requires the URL to be the one the action was
    planned against, which is what the ``expected`` field carries.
    """

    def __init__(self) -> None:
        self.store = state.SnapshotStore()

    def stamp(self, payload: dict[str, Any]) -> state.Snapshot:
        # No explicit count: `snapshot_from_script` derives it from the payload,
        # so a tab list cannot be misread as a number at a call site.
        snapshot = state.snapshot_from_script(self.store.next_generation(), payload)
        return self.store.store(snapshot)

    def current(self) -> state.Snapshot | None:
        return self.store.current

    def check(self, generation: int, expected_url: str = "") -> str:
        """``""`` when the action may proceed, else the sentence for refusing."""
        snapshot = self.store.current
        if snapshot is None:
            return "observe the page first -- I have not read it yet"
        if int(generation or 0) != snapshot.generation:
            return "the page changed since I looked at it -- observe it again"
        if expected_url and snapshot.url and snapshot.url != expected_url:
            return "the page moved since I looked at it -- observe it again"
        return ""

    def invalidate(self) -> None:
        self.store.current = None


def revalidate(expected: dict[str, str], candidate: locators.Candidate) -> str:
    """Whether a candidate is still the element the caller meant.

    Compares only what identifies an element, never its position: a re-render
    renumbers everything, and a re-check that fails on a renumbering would refuse
    correct work. Name, role and href are the identity; index is not.
    """
    if not expected:
        return ""
    if expected.get("name") and candidate.name != expected["name"]:
        return "that control changed on the page"
    if expected.get("role") and candidate.role and candidate.role != expected["role"]:
        return "that control changed on the page"
    href_expected = expected.get("href") or ""
    href_actual = str(candidate.detail.get("href") or "")
    if href_expected and href_actual and href_expected != href_actual:
        return "that control now points somewhere else"
    return ""


# -- the real engine ----------------------------------------------------------


class PlaywrightEngine:
    """Drives the isolated browser in `session`, and owns the generation gate.

    The only class here that knows Playwright exists. It imports it lazily --
    through `session`, which already does -- so a machine without the engine can
    still import the tool and be told what to install.
    """

    def __init__(self, gate: GenerationGate | None = None) -> None:
        self._gate = gate or GenerationGate()

    @property
    def gate(self) -> GenerationGate:
        return self._gate

    async def _page(self) -> Any:
        try:
            return await browser_session.working_page()
        except Exception as exc:  # noqa: BLE001 - translated by the tool
            raise EngineError(str(exc)) from None

    async def navigate(self, url: str) -> dict[str, Any]:
        try:
            return await browser_session.open_page(url)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(str(exc)) from None

    async def observe(self) -> dict[str, Any]:
        page = await self._page()
        try:
            payload = await page.evaluate(state._JS_COLLECT)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"I could not read that page: {str(exc)[:120]}") from None
        data = dict(payload or {})
        try:
            data["tabs"] = await page.evaluate(state._JS_TABS)
        except Exception:  # noqa: BLE001 - tabs are a nicety, not a requirement
            data["tabs"] = []
        try:
            data["forms"] = await page.evaluate(state._JS_FORM)
        except Exception:  # noqa: BLE001
            data["forms"] = []
        try:
            data["viewport"] = await page.evaluate(
                "() => ({w: window.innerWidth, h: window.innerHeight,"
                " dpr: window.devicePixelRatio, scrollY: window.scrollY})"
            )
        except Exception:  # noqa: BLE001
            data["viewport"] = {}
        return data

    async def history(self, direction: str) -> dict[str, Any]:
        try:
            return await browser_session.go_history(direction)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(str(exc)) from None

    async def reload(self) -> dict[str, Any]:
        page = await self._page()
        try:
            await page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return {"url": page.url, "title": await _title(page)}
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"I could not reload the page: {str(exc)[:120]}") from None

    async def tabs(self) -> dict[str, Any]:
        page = await self._page()
        rows = []
        try:
            rows = await page.evaluate(state._JS_TABS)
        except Exception:  # noqa: BLE001
            rows = []
        return {"tabs": state.tabs_payload(rows), "url": page.url}

    async def screenshot(self, *, max_edge: int = 0, quality: int = 0) -> bytes:
        """The viewport, scaled down on the way out.

        ``max_edge`` bounds the longest edge by clipping the capture to the top
        left of the viewport, which is where a page's own header, search field and
        first results live. Clipping is how Playwright downsamples -- there is no
        resize argument -- and it happens before encoding, so a wide page costs a
        small capture rather than a large one that is thrown away afterwards.

        ``quality`` selects JPEG. The two callers pay for an image differently:
        the preview is transient bytes crossing a data channel, while the model's
        copy joins the chat context and is re-sent on every later turn for the
        rest of the session.
        """
        page = await self._page()
        options: dict[str, Any] = {"full_page": False, "type": "jpeg" if quality else "png"}
        if quality:
            options["quality"] = int(quality)
        if max_edge:
            clip = _viewport_clip(page, max_edge)
            if clip:
                options.update(clip=clip, scale="css")
        try:
            data = await page.screenshot(**options)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"I could not take a screenshot: {str(exc)[:120]}") from None
        if not isinstance(data, bytes):
            return b""
        if len(data) > state.MAX_SCREENSHOT_BYTES:
            raise EngineError("that screenshot was too large to send")
        return data

    async def close(self) -> None:
        self._gate.invalidate()
        await browser_session.release()

    # -- browser tabs -------------------------------------------------------
    #
    # Thin passthroughs to `session`, like `navigate` and `history`. They exist
    # because a model that reaches for `web_control` and finds no way to open a
    # tab will do something worse: it will `open` the page in the tab in use and
    # throw away whatever the user had there. Two tools that both drive one
    # browser have to offer the same control of it, or which one she happens to
    # pick decides what she can do.

    async def tab_list(self) -> dict[str, Any]:
        try:
            return await browser_session.tab_list()
        except Exception as exc:  # noqa: BLE001
            raise EngineError(str(exc)) from None

    async def tab_context(self) -> dict[str, Any]:
        try:
            return await browser_session.tab_context()
        except Exception:  # noqa: BLE001 - awareness is never worth failing a read
            return {}

    async def new_tab(self, url: str = "") -> dict[str, Any]:
        try:
            return await browser_session.new_tab(url)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(str(exc)) from None

    async def switch_tab(self, which: object) -> dict[str, Any]:
        try:
            return await browser_session.switch_tab(which)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(str(exc)) from None

    async def close_tab(self, which: object = "") -> dict[str, Any]:
        try:
            return await browser_session.close_tab(which)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(str(exc)) from None

    async def close_other_tabs(self) -> dict[str, Any]:
        try:
            return await browser_session.close_other_tabs()
        except Exception as exc:  # noqa: BLE001
            raise EngineError(str(exc)) from None

    # -- acting -------------------------------------------------------------

    async def act(self, step: EngineAction) -> dict[str, Any]:
        """Do one thing on the page, and report the state afterwards.

        The shape is the same for every verb: re-read the page, find the element,
        re-validate it, act, then read again. Only the middle differs. Keeping the
        frame identical is what makes "verify after every action" true of every
        verb rather than of the two somebody remembered.

        **The element is addressed by its marker, not by a rebuilt selector.**
        The collection script stamps ``data-lumine-ref`` on every control it saw,
        and the action looks the node up by that marker. Rebuilding a locator from
        the *current* DOM instead would silently mean "the nth match of whatever
        this selector matches now", which on a page that changed between the read
        and the click is a different element -- and the marker is the only thing
        that pins the two together. The marker is re-checked through
        :func:`revalidate`, so a node that swapped under it still refuses.
        """
        page = await self._page()
        scope = _scope(page, step.frame)
        refusal = self._gate.check(step.generation)
        if refusal:
            raise EngineError(refusal)

        candidates = await _collect(page, step.query)
        resolution = locators.resolve_one(candidates, step.query or locators.ElementQuery())
        chosen = _choose(resolution, step.index)
        if chosen is None:
            # `describe()`, not `reason`. The reason for an ambiguous page is the
            # three words "more than one matches", which tells the model nothing
            # it can act on -- it cannot retry without knowing *what* matched.
            # `describe()` carries the candidates with their positions, so the
            # next call can name one. This was the difference between a model that
            # retries usefully and one that gives up and searches somewhere else.
            raise EngineError(resolution.describe())

        problem = revalidate(step.expected, chosen)
        if problem:
            raise EngineError(problem)

        locator = _locator_for(scope, chosen, step.query)

        before = {"url": page.url, "title": await _title(page)}
        try:
            await _perform(locator, step)
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001 - a Playwright failure is a sentence
            raise EngineError(f"that control did not respond: {str(exc)[:120]}") from None
        after = {"url": page.url, "title": await _title(page)}
        return {
            "ok": True,
            "before": before,
            "after": after,
            "element": chosen.to_dict(),
            "host": hostname_of(after["url"]),
        }


async def _title(page: Any) -> str:
    try:
        return str(await page.title())
    except Exception:  # noqa: BLE001
        return ""


def _viewport_clip(page: Any, max_edge: int) -> dict[str, int] | None:
    """The clip rectangle that keeps the longest edge at ``max_edge``.

    Pure, so it can be asserted without a browser, and it only ever *shrinks*: a
    viewport already inside the budget gets ``None`` and is captured whole rather
    than being blown up into a larger, blurrier image. The aspect ratio is kept,
    because a screenshot of a page with its proportions distorted is worse than a
    small one.
    """
    try:
        size = page.viewport_size or {}
        width = int(size.get("width") or 0)
        height = int(size.get("height") or 0)
    except Exception:  # noqa: BLE001 - an unknown viewport is captured whole
        return None
    if width <= 0 or height <= 0 or max_edge <= 0:
        return None
    longest = max(width, height)
    if longest <= max_edge:
        return None
    factor = max_edge / longest
    return {
        "x": 0,
        "y": 0,
        "width": max(1, round(width * factor)),
        "height": max(1, round(height * factor)),
    }


def _scope(page: Any, frame_spec: str) -> Any:
    if not frame_spec:
        return page
    try:
        return locators.frame_for(page, frame_spec)
    except locators.LocatorError as exc:
        raise EngineError(str(exc)) from None


async def _collect(page: Any, query: locators.ElementQuery | None) -> list[locators.Candidate]:
    """Every candidate the page offers.

    Reads the whole page rather than building a locator first, because that is
    what makes ambiguity *reportable*: a locator resolves to one element or
    throws, and a throw cannot say "there are three of those".

    **Awaited.** ``Page.evaluate`` is a coroutine function, so calling it without
    ``await`` hands :func:`candidates_from_script` a coroutine object instead of
    a payload. That returned an empty list -- silently -- and every action then
    reported "I couldn't find that on the page" no matter what was on screen.
    A caller cannot tell an empty page from an un-awaited read, which is why this
    reads as "the browser works but nothing can be clicked".
    """
    if query is None:
        raise EngineError("tell me what to act on")
    try:
        payload = await page.evaluate(state._JS_COLLECT)
    except Exception as exc:  # noqa: BLE001
        raise EngineError(f"I could not read that page: {str(exc)[:120]}") from None
    return state.candidates_from_script(payload)


def _choose(resolution: locators.Resolution, index: int) -> locators.Candidate | None:
    """Pick between "the query resolved" and "the caller meant candidate N"."""
    if index and index > 0:
        for candidate in resolution.candidates:
            if candidate.index == index:
                return candidate
        raise EngineError(
            f"there is no item {index} among those -- I found {len(resolution.candidates)}"
        )
    if resolution.chosen is None:
        raise EngineError(
            resolution.reason
            or "more than one thing on the page matches -- tell me which"
        )
    return resolution.chosen


def _locator_for(scope: Any, chosen: locators.Candidate, query: locators.ElementQuery | None) -> Any:
    """The locator for the element the ranking chose.

    By marker when the page gave us one, which is the normal case. The rebuilt
    selector is only a fallback for a caller that built its own candidate without
    one -- and even then the index is applied, because a ranked index and a
    selector's match order are the same order only when nothing was filtered out
    of the candidate list in between, which cannot be assumed.
    """
    if chosen.ref:
        return scope.locator(f'[data-lumine-ref="{chosen.ref}"]')
    locator = locators.build_locator(scope, query, frame=scope)
    return locator.nth(max(0, chosen.index - 1)) if chosen.index > 1 else locator


async def _perform(locator: Any, step: EngineAction) -> None:
    kind = step.kind
    if kind == "click":
        await locator.click(timeout=ACTION_TIMEOUT_MS)
        return
    if kind == "dblclick":
        await locator.dblclick(timeout=ACTION_TIMEOUT_MS)
        return
    if kind == "rightclick":
        await locator.click(button="right", timeout=ACTION_TIMEOUT_MS)
        return
    if kind == "hover":
        await locator.hover(timeout=ACTION_TIMEOUT_MS)
        return
    if kind == "focus":
        await locator.focus(timeout=ACTION_TIMEOUT_MS)
        return
    if kind == "fill":
        await locator.fill(step.text, timeout=ACTION_TIMEOUT_MS)
        return
    if kind == "clear":
        await locator.fill("", timeout=ACTION_TIMEOUT_MS)
        return
    if kind == "type":
        await locator.press_sequentially(step.text, timeout=ACTION_TIMEOUT_MS)
        return
    if kind == "press":
        await locator.press(step.key, timeout=ACTION_TIMEOUT_MS)
        return
    if kind == "select":
        await locator.select_option(label=step.text, timeout=ACTION_TIMEOUT_MS)
        return
    if kind in ("check", "uncheck"):
        # `set_checked` rather than `click`, because clicking a checkbox the page
        # thinks is already checked *unchecks* it -- the verb the caller asked for
        # is the state they asked for, not the click.
        await locator.set_checked(kind == "check", timeout=ACTION_TIMEOUT_MS)
        return
    if kind == "drag":
        await _drag(locator, step)
        return
    if kind == "scroll_into":
        await locator.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT_MS)
        return
    raise EngineError(f"I don't know how to {kind} anything")


async def _drag(locator: Any, step: EngineAction) -> None:
    """Drag by pixel offset, because a drag target is rarely a locator.

    ``drag_to`` needs a second element; a "drag down 300px" gesture has none. The
    offset comes from the caller and is bounded, so it cannot drive the viewport
    somewhere arbitrary or spin the event loop.
    """
    offset = _drag_offset(step.text)
    box = await locator.bounding_box()
    if box is None:
        raise EngineError("that element is not on screen to be dragged")
    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    await locator.hover(timeout=ACTION_TIMEOUT_MS)
    page = await locator.page()
    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    await page.mouse.move(start_x + offset[0], start_y + offset[1], steps=8)
    await page.mouse.up()


def _drag_offset(raw: object) -> tuple[int, int]:
    """``"down 300"`` -> ``(0, 300)``. Bounded, and total nonsense is refused."""
    import re

    text = str(raw or "").strip().casefold()
    match = re.match(r"^\s*(left|right|up|down)?\s*(-?\d{1,4})?\s*(?:px)?\s*$", text)
    if not match:
        raise EngineError("tell me how far to drag: down 300, right 200, and so on")
    direction = match.group(1) or "down"
    amount = int(match.group(2) or 200)
    amount = max(-2000, min(2000, amount))
    return {
        "down": (0, amount),
        "up": (0, -amount),
        "left": (-amount, 0),
        "right": (amount, 0),
    }[direction]


def build_engine() -> PlaywrightEngine:
    """The process-wide engine. One per worker, like the browser it drives.

    Named to match the module-level instance below it. These two used to disagree
    on case, and the result was a `NameError` raised inside the tool body on the
    first call that needed a real engine -- so `web_control` failed with a Python
    identifier as its user-facing message, which is the one thing a tool must
    never return.
    """
    return _engine


_engine = PlaywrightEngine()