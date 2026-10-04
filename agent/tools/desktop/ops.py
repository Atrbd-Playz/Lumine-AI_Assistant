"""Every desktop operation Lumine can perform, declared once.

This table is the answer to a question that otherwise gets asked separately in
four places: *what may she do to this machine, how much does it cost the user,
and which addressing tier is good enough?* The registry
(``tools_registry.TOOL_ACTION_CLASS``), the Tools-page card, the permission
floor, and the tool itself all read it here, because a badge that can disagree
with the behaviour is not a badge.

Four rules live in this file rather than in ``control.py``, and each is a
decision that would be easy to get wrong one call site at a time.

**1. Which window she is allowed to drive.** :data:`INTEGRATED_WINDOWS`. Any
window with a dedicated Lumine integration is refused by name and redirected
to that tool. This is what stops ``desktop_control`` from becoming a way to
send a WhatsApp message without the two questions ``send_whatsapp`` asks, or
to click through a page without the checks ``web_control`` makes. A blind click
is *worse* than a selector, so the tool that has a selector should win; this
table is where that preference is written down.

**2. Which class each verb costs.** ``action_class`` is what the call site
passes to the gate, and for the verbs that act on an *element* it is computed
from the element rather than fixed -- clicking "Send" is a different act from
clicking "Next", and a table that cannot tell them apart would have to pick
the strict one for both. ``test_desktop_ops.py`` pins this table against
``permission_gate.ACTION_CLASSES``.

**3. That a verdict is never inferred from a stale screen.** :func:`check_generation`
implements the whole of it, and it is the reason :mod:`capture` mints a token
at all. "Click 7" is only meaningful relative to a picture of the screen; a
minute later, element 7 may be a different control entirely, and on a scrolled
list it is usually one *below* where the model meant. So every act names the
generation it was planned against and a mismatch is a refusal, not a warning.

**4. That a click which may have landed is never retried.** ``hold_seconds``
mirrors ``whatsapp_ops``: an outcome we cannot interpret leaves something
outside this process, and repeating it is how a person gets two messages.
``verification.may_have_happened`` decides, not the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from . import capture
except ImportError:  # running as a top-level script with agent/tools on sys.path
    import capture  # type: ignore[no-redef]

#: The action classes, mildest first. Mirrors ``permission_gate.ACTION_CLASSES``;
#: ``test_desktop_ops.py`` pins the two together, so a class invented here and
#: not there fails rather than quietly becoming "sensitive" at the gate.
ACTION_CLASSES = ("read", "act", "sensitive", "destructive")

#: Model-facing tool id. Declared here rather than in the registry so this
#: table can name a tool without importing it -- the registry imports *this*,
#: and a cycle resolved by import order is a cycle nobody can debug.
TOOL = "desktop_control"


@dataclass(frozen=True)
class Operation:
    """One operation, and everything the runtime needs to be honest about it."""

    #: The verb, as the model writes it and as a policy key names it
    #: (``desktop_control.click=ask``). Stable: a permission key is something a
    #: person typed into a settings file, so renaming one silently disarms a
    #: decision they made.
    verb: str
    #: The permission class at the call site, for a verb whose class does not
    #: depend on what it hits.
    action_class: str
    #: Whether the class is computed from the element rather than fixed. A
    #: submit is a submission whether it arrived by click, by Enter, or by the
    #: space bar activating a focused button.
    element_dependent: bool = False
    #: Whether this operation reads message history, i.e. shows the user text
    #: another person wrote. Screen text is that too, which is why
    #: ``observe`` carries the same weight.
    reads_third_party_text: bool = False
    #: Which addressing tiers this verb may use, weakest last. A raw
    #: coordinate is ``("element", "coordinate")``; anything that needs to know
    #: *which* control it is pressing cannot use one.
    tiers: tuple[str, ...] = ("element", "coordinate")
    #: How a completed operation proves it happened, in one short clause.
    verify: str = ""
    #: Whether an unconfirmed outcome blocks a retry, and for how long.
    #: ``0`` means a retry is safe, because nothing irreversible left here.
    hold_seconds: float = 0.0
    #: A short, model-facing description of what the verb does.
    summary: str = ""

    @property
    def irreversible(self) -> bool:
        """Whether an unconfirmed outcome may exist outside this process."""
        return self.hold_seconds > 0

    def allows_tier(self, tier: str) -> bool:
        return str(tier or "").strip().lower() in self.tiers

    def summary_text(self) -> str:
        """The row's one-line description, for the catalog and the probe."""
        return self.summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "verb": self.verb,
            "action_class": self.action_class,
            "tiers": list(self.tiers),
            "verify": self.verify,
            "reads_third_party_text": self.reads_third_party_text,
            "hold_seconds": self.hold_seconds,
            "summary": self.summary,
        }


#: How long an unconfirmed click holds a retry. Ten seconds, matching
#: ``apps.LAUNCH_DEDUP_SECONDS``: long enough that a model retrying inside the
#: same confused moment is refused, short enough that a genuine second attempt
#: a minute later is not blocked by a stale entry.
CLICK_HOLD_SECONDS = 10.0

#: Typed text is the one operation whose unconfirmed outcome is *visible*: the
#: characters are in the field whether or not we can read them back, so a
#: retry could produce "hellohello" in a message the user is about to send.
#: Longer than a click, because the recovery is the user looking at the field
#: rather than looking at a button that may or may not have been pressed.
TYPE_HOLD_SECONDS = 30.0


#: The surface. Five verbs, and that is the point.
#:
#: This was nine, and the extra four were a cost paid on every request. The
#: model has to choose between tools on a voice turn's budget of attention, and
#: `double_click` / `right_click` / `drag` / `scroll` are four ways to ask for
#: one of two intentions -- "press that" and "look further down". Every extra
#: verb is a coin flip the model can lose, and the user-facing report was that
#: she could not click anything, which was never this tool's problem but is
#: what an ambiguous surface feels like from the outside.
#:
#: `input.py` still implements the mechanics -- a right button, a drag, a wheel
#: notch -- because they are tested and cheap. They are simply not verbs. Adding
#: one back is one row here and one branch in `control._do_pointer`.
OPERATIONS: tuple[Operation, ...] = (
    Operation(
        verb="observe",
        action_class="read",
        tiers=(),
        reads_third_party_text=True,
        verify="the elements the application reports right now",
        summary="Look at the screen and list what can be clicked on it.",
    ),
    Operation(
        verb="move",
        action_class="act",
        tiers=("element", "coordinate"),
        verify="where the pointer actually ended up",
        summary="Move the mouse to something without clicking it.",
    ),
    Operation(
        verb="click",
        action_class="act",
        tiers=("element", "coordinate"),
        element_dependent=True,
        verify="the control's own reported state afterwards",
        hold_seconds=CLICK_HOLD_SECONDS,
        summary="Click something, by the name on it.",
    ),
    Operation(
        # `act`, which is the product decision: Lumine has hands. The two guards
        # that make it safe are structural rather than advisory -- a password
        # field is refused outright, before the gate, so no approval can unlock
        # it -- and `classify_element` still raises this to a question whenever
        # the field's own name reads as a submit.
        #
        # What this costs is stated plainly rather than buried: typing into a
        # chat box and pressing Enter sends a message with nobody asked, because
        # `type` runs and `press` asks. That is the shape of the decision, not an
        # oversight in it.
        verb="type",
        action_class="act",
        tiers=("element", "coordinate"),
        element_dependent=True,
        verify="the field's text afterwards",
        hold_seconds=TYPE_HOLD_SECONDS,
        summary="Type text into whatever has the keyboard focus.",
    ),
    Operation(
        # `sensitive`, and the only verb that asks. Enter is a submit in a chat
        # box, a search with autosubmit, and most settings dialogs, so this is
        # where "deleting or similar" becomes true without needing a name to
        # match against.
        verb="press",
        action_class="sensitive",
        tiers=(),
        verify="that the application reacted, where it reported that",
        summary="Press a named key or a combination like Ctrl+S.",
    ),
)

_BY_VERB: dict[str, Operation] = {op.verb: op for op in OPERATIONS}

#: The verbs, in the order the tool's schema lists them: reading first, then
#: pointing, then the two that change what is on the machine.
VERBS: tuple[str, ...] = tuple(op.verb for op in OPERATIONS)

#: A coordinate is the fallback for everything that exposes no elements --
#: canvases, games, remote desktops, terminal emulators. Never the default,
#: because it is the only tier where nothing but the model's arithmetic stands
#: between the request and a click.
COORDINATE_TIER = "coordinate"

#: A named, observable control. The default, and the only tier available to
#: verbs whose class depends on what they hit.
ELEMENT_TIER = "element"


def operation(verb: object) -> Operation | None:
    """The row for a verb, or ``None`` when there is not one."""
    return _BY_VERB.get(str(verb or "").strip().lower())


def action_class(verb: object) -> str:
    """The class for a verb, fail-closed.

    Unknown means ``sensitive`` rather than ``act``: mislabelling an action as
    milder is the dangerous direction, and a verb that slipped past validation
    should cost a question rather than not.
    """
    row = operation(verb)
    return row.action_class if row is not None else "sensitive"


def holds_on_uncertainty(verb: object) -> float:
    """Seconds an unconfirmed outcome blocks a retry. ``0`` means safe."""
    row = operation(verb)
    return row.hold_seconds if row is not None else CLICK_HOLD_SECONDS


def tool_action_class() -> str:
    """The strictest class any verb reaches -- what the Tools-page card says.

    Computed from the rows rather than written down, for the reason the
    messaging table documents: ~9 verbs means restating the strictest one in a
    second place would be another thing to keep in step, and a card that
    promises less than the gate enforces is a security bug rather than a typo.
    """
    return max((op.action_class for op in OPERATIONS), key=ACTION_CLASSES.index)


# ---------------------------------------------------------------------------
# Which window may be driven
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Integration:
    """A window Lumine has a dedicated tool for, and where to send the model."""

    #: Executable names, lowercase and without ``.exe``. Matched **exactly**,
    #: which is not a style choice. A prefix match on ``msedge`` also matches
    #: ``msedgewebview2``, which is the WebView2 renderer that lives inside
    #: every Tauri, WinUI and Electron application -- so a prefix would report
    #: Lumine's own window, and every desktop app built on a webview, as a
    #: browser, and refuse to drive all of them. The renderer is a child
    #: process; a browser *window* is owned by the browser itself.
    processes: tuple[str, ...]
    #: The tool that should handle a request about this window instead.
    tool: str
    #: What that tool does that this one cannot.
    reason: str


#: Windows that have a dedicated Lumine integration.
#:
#: **This is a routing hint, not a refusal.** It was a hard refusal, and that
#: was wrong: it made `desktop_control` refuse a browser and a WhatsApp window
#: outright, so a user asking for something the dedicated tool could not phrase
#: for got a dead end with no route forward. A tool that can drive the machine
#: and then refuses half the desktop is not a fallback, it is a wall.
#:
#: What it does now is *steer*. `desktop_control` will drive any window it can
#: see, and the router still prefers the dedicated tool for a request that is
#: clearly about one -- because `web_control` finds a control by name after the
#: page moves, and `send_whatsapp` shows the message before it goes. The
#: preference lives in the router, where a *routing* decision belongs, rather
#: than here, where it was a policy decision nobody asked for.
#:
#: The list is kept for two things: the router's preference, and `desktop_probe`
#: telling a user which windows have a better tool behind them.
INTEGRATED_WINDOWS: tuple[Integration, ...] = (
    Integration(
        processes=("whatsapp", "whatsappweb"),
        tool="send_whatsapp",
        reason=(
            "WhatsApp has its own tools, which show you the message before it "
            "goes and never send the same text twice"
        ),
    ),
    Integration(
        processes=(
            "chrome",
            "msedge",
            "firefox",
            "firefoxdeveloperedition",
            "brave",
            "opera",
            "vivaldi",
            "safari",
        ),
        tool="web_control",
        reason=(
            "the browser has its own tool, which can find a control by name "
            "even after the page moves underneath it"
        ),
    ),
)

#: Renderer and helper processes that must never be mistaken for their host
#: application. None of these owns a top-level window a user would call a
#: browser, and all of them are named after one, so they are excluded
#: explicitly rather than relying on every entry being spelled exactly.
RENDERER_SUFFIXES: tuple[str, ...] = (
    "webview2",
    "webview",
    "renderer",
    "gpu-process",
    "utility",
    "crashpad",
    "minibrowser",
)

#: Human-readable, keyed by tool id, for one refusal sentence naming all of
#: them. Built once so the message cannot list a tool that is not in the table.
_INTEGRATION_TOOLS: tuple[str, ...] = tuple(
    dict.fromkeys(entry.tool for entry in INTEGRATED_WINDOWS)
)


def integration_for(process_names: Any) -> Integration | None:
    """The integration owning any of ``process_names``, or ``None``.

    Matched on the *window's own* process, not on its title and not on its
    children. A browser tab titled "WhatsApp Web" is a browser window, and one
    titled "Gmail" is not the user's mail client -- the executable is the only
    thing here that cannot be chosen by whoever built the page. Children are
    excluded because a renderer is never the window: ``msedgewebview2.exe``
    belongs to a Tauri app, not to a browser, and treating it as one would
    refuse every webview-based desktop application.
    """
    for name in process_names or ():
        stem = _process_stem(name)
        if not stem or _is_renderer(stem):
            continue
        for entry in INTEGRATED_WINDOWS:
            if stem in entry.processes:
                return entry
    return None


def _process_stem(name: object) -> str:
    """``"Chrome.EXE"`` -> ``"chrome"``. Empty for anything unreadable."""
    lowered = str(name or "").strip().lower()
    if not lowered:
        return ""
    return lowered[:-4] if lowered.endswith(".exe") else lowered


def _is_renderer(stem: str) -> bool:
    """Whether this is a renderer or helper rather than an application."""
    return any(stem.endswith(suffix) for suffix in RENDERER_SUFFIXES)


def integrated_tool_names() -> tuple[str, ...]:
    """The tools a hint can point at, for the probe and the tool's schema."""
    return _INTEGRATION_TOOLS


def prefers(hwnd: int) -> Integration | None:
    """The tool that should handle this window *by preference*, or ``None``.

    Returned as advice, never as a veto. The caller decides what to do with it:
    the router uses it to choose, and `desktop_control` uses it only to put a
    sentence on the approval card -- so a user who deliberately asked for the
    desktop can see which tool would have been steadier, without being stopped.
    """
    return integration_for(capture.process_names_for(hwnd))


def refusal_for(entry: Integration) -> str:
    """One sentence naming the steadier tool -- a note, not a veto.

    Used on the approval card and by the probe. Deliberately *not* a refusal:
    the caller has already decided to proceed, and saying "I won't" there would
    be a lie about something that is about to happen.
    """
    return (
        f"{entry.tool} would be steadier here, because {entry.reason}."
    )


# ---------------------------------------------------------------------------
# Element classing
# ---------------------------------------------------------------------------

#: Words that make a click consequential on the far side of it. Matched
#: case-insensitively against a control's accessible name, whole-word where it
#: can be: this list is the difference between "Next" running automatic and
#: "Send" asking, and a substring match on "send" would catch "Send a message"
#: (fine) and "Sender" (not fine, but harmless) as well as "Resend" (very
#: much fine).
SUBMIT_WORDS: tuple[str, ...] = (
    "send",
    "submit",
    "publish",
    "post",
    "share",
    "confirm",
    "complete",
    "continue",
    "buy",
    "pay",
    "checkout",
    "place order",
    "transfer",
    "sign",
    "install",
    "allow",
    "accept",
    "agree",
    "grant",
    "upgrade",
    "subscribe",
    "donate",
    "withdraw",
    "delete",
    "remove",
    "erase",
    "uninstall",
    "reset",
    "format",
    "wipe",
    "empty",
    "clear all",
    "revoke",
    "deactivate",
    "terminate",
    "end call",
    "leave",
    "log out",
    "sign out",
    "shut down",
    "restart now",
    "empty trash",
    "discard",
    "discard changes",
    "don't save",
    "do not save",
)

#: Words that make it irreversible rather than merely consequential. One rung
#: above ``SUBMIT_WORDS`` and treated separately because "cancel" and "delete"
#: can be ordinary housekeeping -- the model clicked them on purpose and the
#: question is a formality -- while these cannot be undone by the user at all.
IRREVERSIBLE_WORDS: tuple[str, ...] = (
    "delete",
    "erase",
    "wipe",
    "empty trash",
    "uninstall",
    "format",
    "reset",
    "terminate",
    "delete account",
    "close account",
    "deactivate",
    "revoke",
)


def _name_has(name: str, words: tuple[str, ...]) -> bool:
    """Whether any word in ``words`` appears in ``name`` as a phrase.

    Phrase matching, not substring: "send" must not match "Sender" and
    "delete" must not match "Deleted items (3)". Multi-word entries are
    checked as phrases for the same reason. This is intentionally a little
    conservative -- a control this misses is an ordinary click that runs
    automatic, which is the cheaper of the two mistakes.
    """
    lowered = " ".join(str(name or "").lower().split())
    if not lowered:
        return False
    padded = f" {lowered} "
    return any(f" {word} " in padded or padded.strip().startswith(f"{word} ") for word in words)


def classify_element(name: str, *, role: str = "", enabled: bool = True) -> str:
    """The class for acting on one control, computed from what it says.

    The mirror of ``browser.actions.classify_click``, which does the same for a
    DOM element, and for the same reason: the blast radius is a property of the
    *target*, not of the verb. Clicking a link that navigates and clicking the
    button that sends a message are the same act from here.

    ``disabled`` is ``act`` rather than ``read``: a control that cannot be
    pressed does nothing, and asking about a click that provably cannot land is
    a question about noise. It is checked before the name, so a disabled
    "Delete" does not ask about an action that cannot occur.
    """
    if not enabled:
        return "act"
    if _name_has(name, IRREVERSIBLE_WORDS):
        return "destructive"
    if _name_has(name, SUBMIT_WORDS):
        return "sensitive"
    return "act"


def classify(verb: object, element: Any = None) -> str:
    """The class for one call: the verb's, unless the element raises it.

    Only ever *raises*. A control called "Next" cannot make a ``destructive``
    verb safe, and lowering a class because an element's name looks harmless
    would make the element -- a string off the screen, which a web page chose
    -- the thing that decides how much the user is asked.
    """
    base = action_class(verb)
    if element is None:
        return base
    row = operation(verb)
    if row is None or not row.element_dependent:
        return base
    element_class = classify_element(
        getattr(element, "name", ""),
        role=str(getattr(element, "role", "") or ""),
        enabled=bool(getattr(element, "enabled", True)),
    )
    if ACTION_CLASSES.index(element_class) > ACTION_CLASSES.index(base):
        return element_class
    return base


def tool_summary() -> str:
    """The one-line description the tool catalog publishes."""
    return (
        "Clicks, types and presses keys on this computer's screen, in any "
        "window. Windows with their own Lumine tools are handled by those."
    )
