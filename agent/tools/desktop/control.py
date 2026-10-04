"""``desktop_control``: the mouse and keyboard, with the gate in front.

## What it is for

Anything on this computer. The user asks for something in Notepad, in Explorer,
in a settings dialog, in a terminal, in a browser, in an application nobody has
heard of -- and it happens. That is the whole point: the alternative is one
adapter per application forever, and an application list is a list that is never
finished.

## Five verbs, and that is deliberate

``observe``, ``move``, ``click``, ``type``, ``press``. This was nine. The four
that went -- ``double_click``, ``right_click``, ``drag``, ``scroll`` -- are four
ways to ask for one of two intentions, "press that" and "look further down",
and every extra verb is a coin flip the model can lose on a voice turn's budget
of attention. The report that prompted the cut was that she could not click
anything, which was never this tool's fault and is exactly what an ambiguous
surface feels like from the outside.

The mechanics are still in :mod:`input` -- a right button, a drag, a wheel notch
-- because they are tested and cheap. They are simply not verbs.

## What it refuses, and why that is the feature

Two refusals, each load-bearing:

**A reference from a screen that has moved.** :func:`capture.confirm_target`.
The whole reason ``observe`` returns a number: the tree is re-walked and the
target re-found by role and name before anything irreversible happens.

**A password field.** Not a question -- a refusal. A credential written into a
field by a voice agent is a credential in a transcript, and no approval prompt
makes that acceptable.

And **its own window**, because an agent that can click anything can click the
Tools page, switch this tool off mid-turn, and keep using it.

A window with a dedicated integration used to be refused too, and that was
wrong: it made the "fallback" a wall, and anything the dedicated tools could not
phrase for became unreachable. It is now a *note* on the approval card --
"``web_control`` would be steadier here" -- which is advice the user can see
and ignore, rather than a refusal they have to argue with.

## Why ``type`` is ``act`` and everything around it is not

A row in :mod:`ops` argues this at length; the short form is that the two
guards which make typing safe are structural rather than advisory. The window
check is a refusal, the password check is a refusal, and neither can be talked
out of by anything the model says. A user asked about every keystroke learns to
answer without reading, and then the questions that matter are the ones they
skip -- so the deliberate submit stays ``sensitive`` while the typing does not.

## Ordering inside one call

Gate, then act, then *read the result afterwards*. The read is what separates
``observed`` from ``dispatched``, and a click that returns without any check
is indistinguishable from a click that hit the wrong thing -- which, given that
this tool can aim at any pixel, is not a theoretical concern.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    from ...runtime import progress, verification
    from ...runtime.permission_gate import require_approval
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime import progress, verification  # type: ignore[no-redef]
    from runtime.permission_gate import require_approval  # type: ignore[no-redef]

try:
    from ..tool_results import as_payload, wrap_untrusted
    from ..tools_compat import RunContext, ToolError, function_tool
    from ..tools_text import clip
    from . import capture, input as desktop_input, ops, uia
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tool_results import as_payload, wrap_untrusted  # type: ignore[no-redef]
    from tools_compat import RunContext, ToolError, function_tool  # type: ignore[no-redef]
    from tools_text import clip  # type: ignore[no-redef]
    from desktop import capture, input as desktop_input, ops, uia  # type: ignore[no-redef]

logger = logging.getLogger("lumine")

#: Where the desktop preview travels. Separate from `lumine.tool` for the same
#: reason `browser.preview.TOPIC` is: a picture is not transient status text,
#: and a dropped preview must never delay a step.
TOPIC = "lumine.screen"

#: The record type the app switches on.
RECORD_TYPE = "screen_preview"

MAX_PREVIEW_CHARS = 1_400_000

#: Sentences the model cannot rephrase its way past.
MSG_BAD_VERB = "Tell me one of: " + ", ".join(ops.VERBS) + "."
MSG_NO_TARGET = (
    "Tell me what to click -- the number from the screen I looked at, or what "
    "it says on it. Nothing was done."
)
MSG_NO_TEXT = "Tell me what to type. Nothing was typed."
MSG_NO_COORD = "Tell me where on the screen: an x and a y. Nothing was done."
MSG_AMBIGUOUS = "more than one thing on screen matches"
MSG_UNSUPPORTED = (
    "I can only control the mouse and keyboard on Windows, and this isn't one. "
    "Nothing was done."
)
MSG_NO_DRIVER = (
    "The UI Automation driver this needs isn't installed (the `uiautomation` "
    "package). Nothing was done."
)
MSG_PASSWORD = "I won't type into a password field. Nothing was typed."
MSG_NOTHING_ADDRESSABLE = (
    "Nothing on this screen says what it is, so I can't click it by name. "
    "Nothing was done -- tell me which application to work in, or give me an "
    "x and a y and I'll point at a position."
)
MSG_STALE_SCREEN = (
    "the screen moved since I looked at it, so that number would point at "
    "something else now. Nothing was done -- observe again."
)

#: How long a fresh observation is reused before re-walking the tree.
REFRESH_SECONDS = 8.0


# ---------------------------------------------------------------------------
# Engine seam
# ---------------------------------------------------------------------------


class _Desktop:
    """One attribute, so a test swaps the whole machine without patching six
    module globals. Every blocking call goes through here."""

    input: Any = None
    capture: Any = None
    uia: Any = None

    @classmethod
    def get(cls) -> "_Desktop":
        return cls

    @classmethod
    def preflight(cls) -> None:
        """Refuse before anything is measured, with the actionable reason.

        A classmethod rather than an instance method: every fact it checks is
        machine-level rather than per-engine, and calling it as
        ``_Desktop.preflight()`` -- which is how the tool calls it -- silently
        raised a ``TypeError`` instead of doing the check.
        """
        if desktop_input.sys.platform != "win32":
            raise ToolError(MSG_UNSUPPORTED)
        if not uia.supported():
            raise ToolError(MSG_NO_DRIVER)


def desktop() -> _Desktop:
    return _Desktop.get()


def set_engine(value: Any) -> None:
    """Install an engine. Tests and nothing else."""
    _Desktop.input = getattr(value, "input", None)
    _Desktop.capture = getattr(value, "capture", None)
    _Desktop.uia = getattr(value, "uia", None)


def _input() -> Any:
    return _Desktop.input or desktop_input


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _check_window(hwnd: int) -> None:
    """Note that a steadier tool exists, and carry on.

    **This used to refuse**, and it was the wrong call: refusing every browser
    and WhatsApp window turned "the fallback" into a wall, and a user asking for
    something the dedicated tools could not phrase for got nowhere at all. The
    preference belongs to the router, which is where a routing decision lives.

    Kept as a function because the *note* is still worth having: it reaches the
    approval card, so a user deliberately asking for the desktop sees which tool
    would have been steadier -- and, more usefully, sees it at the moment they
    are deciding whether to approve.
    """
    return None


def _preferred_tool_hint(hwnd: int) -> str:
    """The steadier-tool note for an approval card, or ``""``."""
    entry = ops.integration_for(capture.process_names_for(hwnd))
    return f" ({ops.refusal_for(entry)})" if entry is not None else ""


def _check_own_window(hwnd: int) -> None:
    """Refuse to click Lumine's own windows. Still a hard refusal.

    An agent that can click anywhere can click the Tools page, switch
    ``desktop_control`` off mid-turn, and keep using it. Every other window is
    drivable now; this one is not, because a self-defeating switch is not a
    permission the user granted.

    It also covers the *desktop shell*, not just the worker: the shell is a
    separate process, so the earlier pid-only test never matched the window that
    actually matters, and the agent read and aimed at its own interface.
    """
    if uia.is_our_own(hwnd):
        raise ToolError(
            "I won't click my own window. Nothing was done -- switch to the "
            "application you want me to work in first."
        )


#: Verbs where a stale position costs nothing, so the extra tree walk is pure
#: cost. ``move`` only aims the pointer: pointing at where a control *was* does
#: nothing, and no click follows from it inside this call.
UNVERIFIED_VERBS = frozenset({"move"})


def _resolve_target(
    *,
    ref: object = None,
    name: str = "",
    x: object = None,
    y: object = None,
    verb: str = "click",
    verify_screen: bool | None = None,
) -> tuple[tuple[int, int], capture.Element | None, str]:
    """Where to aim, and which element (if any) justified it.

    The tree is re-read before an act returns, and that re-read is the only
    thing that can detect movement since the observation â€” a generation token
    cannot, because the tool supplies that number itself. ``move`` skips it via
    :data:`UNVERIFIED_VERBS`, decided *here* rather than by the caller: a rule
    that lives at the call site is a rule the next caller forgets.

    Three ways in, deliberately in that order of preference:

    * a ``ref`` from the current observation -- exact, and the only tier whose
      staleness is fully checked;
    * a ``name`` -- resolved against the current observation, and refused as
      ambiguous rather than guessed, because two buttons called Send and only
      one of them is the one meant;
    * an ``x``/``y`` pair -- the fallback for screens that expose nothing, and
      the only tier with no element behind it, so the caller classifies it as
      ``sensitive``.

    Returns ``(point, element, tier)``. The element is what the class is
    computed from; ``None`` means the coordinate tier, which is why the caller
    has to handle its absence rather than defaulting to a class.
    """
    row = ops.operation(verb)
    tiers = row.tiers if row is not None else (ops.COORDINATE_TIER,)
    if verify_screen is None:
        verify_screen = verb not in UNVERIFIED_VERBS

    if ref is not None and str(ref).strip() != "":
        # The current observation, or a refusal to look first. Deliberately *not*
        # a generation comparison: the tool supplies that number itself, so it
        # would always match. The freshness question is answered below, by
        # re-reading the tree.
        observation = capture.require_observation()
        element = observation.by_ref(ref)
        if element is None:
            raise ToolError(
                "there's no control with that number on the screen I looked "
                "at. Nothing was done -- observe again and use a number it "
                "gives you."
            )
        if not element.enabled:
            # Not a refusal about permissions -- the control cannot be pressed,
            # so there is no act to ask about.
            raise ToolError(
                "that control is disabled right now, so clicking it would do "
                "nothing. Nothing was done."
            )
        if verify_screen:
            capture.confirm_target(observation, element)
        return element.center, element, ops.ELEMENT_TIER

    if str(name or "").strip():
        observation = capture.require_observation()
        matches = capture.find_by_name(observation, name)
        if not matches:
            raise ToolError(
                f"nothing on the screen says '{clip(name, 40)}'. Nothing was "
                f"done -- observe again, or give me an x and a y."
            )
        if len(matches) > 1:
            listed = ", ".join(str(item.ref) for item in matches[:4])
            raise ToolError(
                f"{MSG_AMBIGUOUS} ({listed}). Nothing was done -- observe "
                f"again and use the number you meant."
            )
        if verify_screen:
            capture.confirm_target(observation, matches[0])
        return matches[0].center, matches[0], ops.ELEMENT_TIER

    if x is not None and y is not None:
        # Asked of the verb's own tiers rather than derived: `press` declares
        # *no* tiers, so a test of "is this verb element-dependent" would be
        # false and a coordinate would sail through -- a key goes to whatever
        # has focus, so a coordinate says nothing about where it lands.
        if not ops.COORDINATE_TIER in tiers:
            raise ToolError("that action needs a control, not a position. Nothing was done.")
        point = (int(x), int(y))
        if not desktop_input.contains(point):
            raise ToolError("that position isn't on the screen. Nothing was done.")
        return point, None, ops.COORDINATE_TIER

    raise ToolError(MSG_NO_TARGET if ops.ELEMENT_TIER in tiers else MSG_NO_COORD)


def _claimed_generation() -> object:
    """The generation the caller is acting against, for the *plan-level* check.

    Kept because ``run_task``'s desktop steps genuinely compare a generation
    across steps -- but nothing in this module calls it, and that is the point
    rather than an oversight: a single tool call reads the generation from
    ``_LAST`` and would be comparing a value against itself, which always
    passes. Freshness here is answered by :func:`capture.confirm_target`, which
    takes a second reading of the tree instead of trusting a number.
    """
    current = capture.last_observation()
    return current.generation if current is not None else None


def _focused_element(observation: capture.Observation) -> capture.Element | None:
    """The element that currently holds keyboard focus, if it is listed.

    Read from the tree rather than tracked, because focus can move without us:
    the user clicks, an application autofocuses a field on open, a previous
    step left the caret somewhere. Typing goes to whatever has focus *now*.
    """
    try:
        root = uia.driver().ControlFromHandle(observation.hwnd)
        focused: list[Any] = []

        def visit(node: Any, _depth: int) -> None:
            if focused:
                return
            try:
                if node.HasKeyboardFocus:
                    focused.append(node)
            except Exception:  # noqa: BLE001
                pass

        uia.walk(root, visit, depth=0)
        if not focused:
            return None
        rect = uia.rect_of(focused[0])
        if rect is None:
            return None
        for element in observation.elements:
            if element.rect == rect:
                return element
        return None
    except Exception:  # noqa: BLE001 -- no answer is not an error here
        return None


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


def _payload(**fields: Any) -> str:
    return wrap_untrusted(as_payload(fields))


def _preview_record(observation: capture.Observation) -> dict[str, Any]:
    """The desktop state, for the app.

    Metadata only. No pixels: this tool reads the accessibility tree, so there
    is no picture to send, and a synthetic placeholder image would be worse
    than none -- it would look like a screenshot of something.
    """
    return {
        "type": RECORD_TYPE,
        "generation": observation.generation,
        "window": clip(observation.title, 90),
        "process": observation.process,
        "addressable": observation.addressable,
        "dpiAligned": observation.dpi_aligned,
        "elements": len(observation.elements),
        "captured_at": observation.observed_at,
    }


def _publish(context: Any, record: dict[str, Any]) -> bool:
    """Send the desktop state to the app. Never raises.

    A tool must not end because a telemetry channel was closed -- the same
    argument ``runtime_events.write_event_line`` is built on.
    """
    try:
        publisher = progress.publisher_for(context)
        if publisher is None:
            return False
        publisher.emit_and_publish(TOPIC, record)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[Desktop] preview could not be published", exc_info=True)
        return False


async def _fresh_observation(hwnd: int | None = None) -> capture.Observation:
    """Observe, reusing a recent index rather than re-walking the tree.

    The reuse window is short (:data:`REFRESH_SECONDS`) and applies only to
    *reads*. Anything that acts goes through
    :func:`capture.check_generation`, which compares generations exactly and
    is unaffected by how old the observation is -- so this is a saving on
    repeated questions, never a licence to act on a stale screen.
    """
    current = capture.last_observation()
    if current is not None and not capture.is_stale(current, seconds=REFRESH_SECONDS):
        target = int(hwnd or 0) or current.hwnd
        if target == current.hwnd:
            return current
    return await asyncio.to_thread(capture.observe, hwnd)


def _after_click_evidence(before: capture.Observation, hwnd: int) -> str:
    """What changed after a click, read fresh.

    Synchronous, and called through ``asyncio.to_thread`` because it walks
    COM. The comparison is structural (title, then the element list) rather
    than pixel or fuzzy, because the only honest claim available here is "the
    controls changed", and inventing a finer one would be claiming a precision
    this cannot deliver.
    """
    return _read_change(before, hwnd)


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@function_tool()
async def desktop_control(
    context: RunContext,
    action: str,
    target: str = "",
    text: str = "",
    window: str = "",
    x: int = 0,
    y: int = 0,
) -> str:
    """Look at the screen, and click, type, or press keys on this computer.

    This is Lumine's hands. Use it for anything on this machine that has no
    tool of its own: Notepad, Explorer, a settings dialog, a terminal, a
    browser window, an application nobody has built an adapter for.

    Five things, and the order is fixed: **observe, then act on a number it
    gave you.**

    - `observe` -- list what can be clicked, numbered.
    - `move` -- put the mouse on something without pressing it.
    - `click` -- press something.
    - `type` -- type into whatever has the keyboard focus.
    - `press` -- one named key or combination, like "enter" or "ctrl+s".

    Observe again after anything moves. The numbers belong to the screen as it
    was, so acting on a number from an older look is refused rather than
    followed -- which is what stops a scrolled list turning "click 7" into a
    click on Send.

    A control whose own label reads as a submission or a deletion -- Send, Pay,
    Delete, Empty trash -- asks you first, and so does `press`. Everything else
    runs. Lumine never types into a password field, and never drives her own
    window.

    Args:
        action: What to do: "observe", "move", "click", "type", or "press".
        target: For "click" or "move": the number from `observe`, or part of the
            text on the control. Not needed for "type" or "press".
        text: For "type", the text to type. For "press", the key or
            combination, such as "enter" or "ctrl+shift+s".
        window: Part of a window's title, to work in that window rather than
            whichever one she picks. Worth saying when more than one application
            is open.
        x: An x position on screen, only for a screen that exposes no controls
            at all. Rarely needed -- prefer a number from `observe`.
        y: The y position to go with x.
    """
    verb = str(action or "").strip().lower()
    if verb not in ops.VERBS:
        raise ToolError(MSG_BAD_VERB)

    desktop().preflight()

    # Interruption is latched once something is going to move the user's
    # pointer, and not before: a read is safe to interrupt, a keystroke is not.
    if verb != "observe":
        latch = getattr(context, "disallow_interruptions", None)
        if callable(latch):
            latch()

    # An action aimed at a named window acts on *that* window -- otherwise
    # "type into the notes window" would type into whatever was in front while
    # reporting that it had used the notes window.
    target_hwnd = 0
    if str(window or "").strip():
        target_hwnd, _title = capture.resolve_window(window)

    if verb == "observe":
        return await _do_observe(context, window=window)
    if verb == "type":
        return await _do_type(context, text, hwnd=target_hwnd)
    if verb == "press":
        return await _do_press(context, text, hwnd=target_hwnd)
    return await _do_pointer(context, verb, target=target, x=x, y=y, hwnd=target_hwnd)


def _target_window(hwnd: int) -> int:
    """The window to act on, brought forward if it is not already.

    With no window named, this is :func:`capture.best_window` rather than the
    foreground handle -- see that function for why "the window I am looking at"
    is not the foreground window while you are talking to Lumine.

    A click carries its own position, so it lands correctly in a background
    window. Keystrokes do not: they go to the foreground, so acting on a window
    the user is not looking at and reporting success would be a claim about
    whichever window *had* focus. Bringing it forward is what makes "type into
    the notes window" mean that window.
    """
    if not hwnd:
        try:
            hwnd, _title = capture.best_window()
        except ToolError:
            return uia.foreground_window()
    if not hwnd:
        return uia.foreground_window()
    if int(hwnd) != int(uia.foreground_window() or 0):
        # Best effort on purpose: a click carries its own position and lands in a
        # background window anyway, so refusing to click because focus could not
        # be taken would be a refusal of an act that would have worked. Typing is
        # the strict case and uses `uia.ensure_foreground` instead, which does
        # refuse. `bring_to_front` works around the focus lock rather than
        # asking politely and being ignored.
        uia.bring_to_front(int(hwnd))
    return int(hwnd)


async def _do_observe(context: RunContext, *, window: str = "") -> str:
    if not str(window or "").strip() and not uia.foreground_window():
        raise ToolError("I can't tell which window is in front. Nothing was done.")
    observation = await asyncio.to_thread(capture.observe, window=window)
    _publish(context, _preview_record(observation))

    # Read through the same gate as everything else, so a policy entry for
    # `desktop_control.observe` can tighten it. Under the default policy this
    # returns immediately.
    await require_approval(
        context,
        tool=ops.TOOL,
        action="observe",
        action_class=ops.action_class("observe"),
        summary="Look at the screen and list what I can click",
        target=clip(observation.title, 80),
    )

    if not observation.addressable:
        # Never a dead end. The old version said "give me an x and a y" and
        # stopped; naming the windows she *can* see turns it into a question
        # with an answer, which is the difference between "she cannot do
        # anything" and "which one did you mean".
        visible = [clip(title, 40) for _h, title in capture.candidate_windows()[:6]]
        return _payload(
            ok=False,
            generation=observation.generation,
            window=clip(observation.title, 80),
            addressable=False,
            reason=MSG_NOTHING_ADDRESSABLE,
            windowsOpen=visible,
        )
    if not observation.dpi_aligned:
        # Reported rather than hidden: clicks will land at the scale factor's
        # distance from where they were aimed, and a user watching that happen
        # needs to know why before they assume Lumine is broken.
        logger.warning("[Desktop] process is not DPI aware; clicks may be offset")
    return _payload(
        ok=True,
        generation=observation.generation,
        window=clip(observation.title, 80),
        process=observation.process,
        addressable=True,
        # The coordinate space, so a negative x reads as a second monitor rather
        # than as an impossible position.
        desktop=list(observation.desktop),
        foreground=observation.foreground,
        elements=[element.to_dict() for element in observation.elements],
        lines=observation.describe(),
    )


async def _do_pointer(
    context: RunContext,
    verb: str,
    *,
    target: str,
    x: int,
    y: int,
    hwnd: int = 0,
) -> str:
    hwnd = _target_window(hwnd)
    if not hwnd:
        raise ToolError("I can't tell which window is in front. Nothing was done.")
    _check_own_window(hwnd)
    _check_window(hwnd)

    before = capture.last_observation()
    name = str(target or "")
    ref: object = None
    # A numeric target is a reference, not a name. A model asked to "click 7"
    # means the seventh control on the screen it looked at, never a control
    # whose name happens to be a number.
    if name.strip().isdigit():
        ref = int(name.strip())
        name = ""

    point, element, tier = _resolve_target(ref=ref, name=name, x=x, y=y, verb=verb)

    # The class comes from the element when there is one and from the coordinate
    # tier when there is not. The second case is why `sensitive` is not
    # negotiable: a click at a bare coordinate is a click at whatever happens
    # to be there.
    action_class = ops.classify(verb, element) if element is not None else "sensitive"
    await require_approval(
        context,
        tool=ops.TOOL,
        action=verb,
        action_class=action_class,
        summary=_pointer_summary(verb, element, point) + _preferred_tool_hint(hwnd),
        target=_pointer_target(element, point),
    )

    engine = _input()
    try:
        if verb == "move":
            await asyncio.to_thread(engine.move_to, point)
        else:
            await asyncio.to_thread(engine.click, point)
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"That didn't work: {clip(exc, 120)}. Nothing was done.") from None

    evidence = ""
    if before is not None and verb != "move":
        evidence = await asyncio.to_thread(_read_change, before, hwnd)
    outcome = _outcome_for(evidence, verb, element)
    return _payload(**outcome.to_dict(), point=list(point), tier=tier)


async def _do_type(context: RunContext, text: str, *, hwnd: int = 0) -> str:
    if not str(text or ""):
        raise ToolError(MSG_NO_TEXT)
    hwnd = _target_window(hwnd)
    if not hwnd:
        raise ToolError("I can't tell which window is in front. Nothing was typed.")
    _check_own_window(hwnd)
    _check_window(hwnd)

    observation = await _fresh_observation(hwnd)
    focused = _focused_element(observation)

    # The one refusal here that is not about permission. A credential typed by
    # a voice agent is a credential in a transcript, and no approval makes that
    # acceptable -- so this is structural, and it is checked before the gate.
    if focused is not None and focused.is_password:
        raise ToolError(MSG_PASSWORD)

    # `act` -- see the note on the `type` row in `ops.py`. The summary still
    # carries the literal text, because the day this is turned into `ask` the
    # card must already have something to show, and because a user reading the
    # audit log later needs to know what was typed where.
    await require_approval(
        context,
        tool=ops.TOOL,
        action="type",
        action_class=ops.action_class("type"),
        summary=f"Type into {clip(observation.title, 50)}: {clip(text, 60)}",
        target=clip(focused.describe() if focused else observation.title, 80),
    )

    # After the gate, never before it: this may steal the user's focus, and a
    # question asked while a window is being yanked forward is a question about
    # something that may not happen. Strict rather than best-effort -- this is
    # the whole "where would these keystrokes go?" answer, and a refusal here
    # is better than text landing in whichever window had focus instead.
    try:
        await asyncio.to_thread(uia.ensure_foreground, hwnd)
    except ToolError as exc:
        # Re-worded rather than propagated. `ensure_foreground` is shared with
        # WhatsApp and says what *it* could not do; every refusal in this tool
        # has to also say what did *not* happen here, so the model cannot
        # rephrase its way past one into a second attempt that types somewhere.
        raise ToolError(
            f"{str(exc).split('.')[0].strip()}. Nothing was typed."
        ) from None
    typed = await asyncio.to_thread(_type_sync, text)
    try:
        uia.release_foreground()
    except Exception:  # noqa: BLE001 -- restoring focus is best effort
        pass
    return _payload(
        **verification.accepted(
            "type", f"typed {len(text)} characters into {clip(observation.title, 40)}"
        ).to_dict(),
        events=typed,
        into=clip(focused.describe() if focused else "whatever had focus", 80),
    )


def _type_sync(text: str) -> int:
    return _input().type_text(text)


async def _do_press(context: RunContext, spec: str, *, hwnd: int = 0) -> str:
    if not str(spec or ""):
        raise ToolError("Tell me which key, such as 'enter' or 'ctrl+s'. Nothing was pressed.")
    hwnd = _target_window(hwnd)
    if hwnd:
        _check_own_window(hwnd)
        _check_window(hwnd)

    # `press` is `sensitive` at the verb, and stays that way for every key.
    # Enter is the reason: in a chat composer, a search box with autosubmit, or
    # a settings dialog, Enter *is* the submit, and that is the act the user
    # wants to be asked about.
    await require_approval(
        context,
        tool=ops.TOOL,
        action="press",
        action_class=ops.action_class("press"),
        summary=f"Press {clip(spec, 30)}",
        target=clip(uia.window_title(hwnd), 80) if hwnd else "whatever has focus",
    )
    await asyncio.to_thread(_input().press_key, spec)
    return _payload(**verification.dispatched("press", f"pressed {clip(spec, 20)}").to_dict())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scroll_amount(spec: object) -> int:
    """Parse ``"down 3"`` / ``"up 5"`` / ``"-2"`` into a signed notch count.

    Returns ``0`` for anything unparseable, which every caller turns into a
    refusal. A scroll of zero notches is not a no-op to be shrugged at: it
    means the model did not say what it meant, and guessing a direction on its
    behalf would scroll a page the user did not ask about.
    """
    text = " ".join(str(spec or "").split()).lower()
    if not text:
        return 0
    direction = 1
    for word, sign in (("down", 1), ("up", -1), ("back", 1), ("forward", -1)):
        if word in text:
            direction = sign
            break
    digits = ""
    for char in text:
        if char.isdigit():
            digits += char
        elif char == "-" and not digits:
            digits = "-"
    if not digits:
        # "scroll down" with no number means "some", and three notches is
        # enough to see movement without overshooting a short list.
        return direction * 3 if any(w in text for w in ("down", "up", "back", "forward")) else 0
    try:
        amount = int(digits)
    except ValueError:
        return 0
    if amount == 0:
        return 0
    # The direction is applied here rather than read off the count: an explicit
    # negative count already carries one, and multiplying it by the direction
    # would flip it back the other way.
    if amount < 0:
        return amount
    return amount * direction


def _pointer_summary(verb: str, element: capture.Element | None, point: tuple[int, int]) -> str:
    """What the approval card says, in the user's words rather than the model's."""
    words = {"click": "Click", "move": "Move the pointer to"}.get(
        verb, verb.replace("_", " ").capitalize()
    )
    if element is not None:
        return f"{words} {clip(element.describe(), 60)}"
    return f"{words} a point on the screen, at {point[0]}, {point[1]}"


def _pointer_target(element: capture.Element | None, point: tuple[int, int] | None) -> str:
    """What the card shows as the thing being acted on.

    An element names itself. A coordinate gets its window and its position,
    because a bare "420, 300" on a card is not enough for a user to decide.
    """
    if element is not None:
        return clip(element.describe(), 90)
    if point is None:
        return "the screen"
    return f"a point at {point[0]}, {point[1]}"


def _read_change(before: capture.Observation, hwnd: int) -> str:
    """Re-read the window after a click and describe what differs.

    Synchronous, and called through ``asyncio.to_thread`` -- it walks COM. The
    comparison is structural (title, then the element list) rather than pixel
    or fuzzy, because the only honest claim available here is "the controls
    changed", and inventing a finer one would be claiming a precision this
    cannot deliver.
    """
    try:
        after = capture.observe(hwnd)
    except Exception:  # noqa: BLE001 -- an unreadable after-state is not a failure
        return ""
    if after.title != before.title:
        return f"the window title is now {clip(after.title, 50)}"
    before_refs = [(e.role, e.name, e.rect) for e in before.elements]
    after_refs = [(e.role, e.name, e.rect) for e in after.elements]
    if before_refs != after_refs:
        return "the controls on screen changed"
    return ""


def _outcome_for(evidence: str, verb: str, element: capture.Element | None) -> "verification.ActionOutcome":
    """A click's verification verdict.

    ``observed`` only when something was actually read afterwards. Otherwise
    ``dispatched``, which is the honest floor: the input went to the window,
    and this tool has no way to know what the application did with it. The
    gap between those two words is exactly what ``verification.py`` exists to
    keep from being papered over.
    """
    what = clip(element.describe(), 50) if element is not None else "that point"
    verb_words = {"move": "moved the pointer to"}.get(verb, "clicked")
    if evidence:
        return verification.observed(
            verb, f"{verb_words} {what}, and {evidence}", evidence=evidence
        )
    # `note` rather than `evidence`, and deliberately: `verification.dispatched`
    # takes no evidence because a dispatched outcome has none by definition,
    # so passing that keyword would land in `data` under a name that reads like
    # the field it is not. The model needs the sentence -- it is what stops it
    # reporting a click that was never confirmed as one that worked.
    return verification.dispatched(
        verb,
        f"{verb_words} {what}",
        note=(
            "Windows accepted the input; this tool cannot read what the "
            "application did with it. Say that rather than claiming it worked."
        ),
    )


def capability() -> dict[str, Any]:
    """Can this be driven right now? Live, never cached.

    The same shape ``whatsapp_desktop.desktop_capability`` returns, for the
    same reason: "no driver" and "not Windows" and "this screen exposes
    nothing" are three different problems and a single "unavailable" helps
    none of them.
    """
    if desktop_input.sys.platform != "win32":
        return {"available": False, "reason": MSG_UNSUPPORTED}
    if not uia.supported():
        return {"available": False, "reason": MSG_NO_DRIVER}
    observation = capture.last_observation()
    if observation is None:
        return {"available": True, "reason": "", "state": "not_observed"}
    return {
        "available": True,
        "reason": "",
        "state": "addressable" if observation.addressable else "coordinate_only",
        "generation": observation.generation,
    }


def operations_table() -> list[dict[str, Any]]:
    """The operation table as the catalog publishes it."""
    return [op.to_dict() for op in ops.OPERATIONS]
