"""Decides which tools Lumine exposes to the LLM.

Kept apart from the registry so the desktop layer can widen or narrow the tool
surface through environment variables instead of code changes.

This module also owns *how much* each tool may do without asking: the
:class:`~agent.runtime.permission_gate` enforces per-call, and the floor it
enforces comes from :data:`TOOL_ACTION_CLASS` here, so the card on the Tools
page and the gate in the tool cannot disagree about what a tool is.
"""

import os
from collections.abc import Mapping
from typing import Any, Iterable

try:
    from ..runtime.history_consent import MODE_VAR as HISTORY_VAR
    from ..runtime.history_consent import resolve_mode as resolve_history_mode
    from ..runtime.permission_gate import PERMISSIONS_VAR, effective_handling, env_permission_levels
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime.history_consent import MODE_VAR as HISTORY_VAR
    from runtime.history_consent import resolve_mode as resolve_history_mode
    from runtime.permission_gate import PERMISSIONS_VAR, effective_handling, env_permission_levels

try:
    from ..settings.config_store import effective_document
except ImportError:  # running as a top-level script with agent/ on sys.path
    from settings.config_store import effective_document

try:
    from .desktop import ops as desktop_ops
except ImportError:  # running as a top-level script with agent/ on sys.path
    from desktop import ops as desktop_ops  # type: ignore[no-redef]

from .messaging import whatsapp_ops
from .messaging.whatsapp import whatsapp_disabled_reason

#: Every messaging tool switches off together, on the one switch that already
#: exists. `messaging.whatsapp.mode = off` has always meant "Lumine does not touch
#: your WhatsApp", and it would be a strange product where that turned off
#: sending but left her reading the user's messages -- the capability that is
#: harder to notice and easier to regret.
MESSAGING_TOOLS = frozenset(whatsapp_ops.MESSAGING_TOOLS)

# Tools that act on the user's machine instead of only answering a question.
#: Tools that *start a program*. `run_task` and `cancel_task` are deliberately
#: not here: neither launches anything itself. `run_task` may decide to launch --
#: and when `LUMINE_ENABLE_APP_LAUNCH` is off it is told which tools are enabled
#: and routes around launching rather than silently starting a program the user
#: switched off -- while `cancel_task` only stops work. Hiding them would take
#: away the ability to search and browse from a session configured never to open
#: anything.
DESKTOP_TOOLS = frozenset({"open_app"})

#: The mouse and keyboard, behind a switch of its own rather than a member of
#: :data:`DESKTOP_TOOLS`.
#:
#: It is separated from launching because it is a different kind of authority.
#: `open_app` starts a program and does nothing else; `desktop_control` can
#: press any key and click any pixel on the machine, which includes the Tools
#: page that switches it off. That is why it also refuses to drive its own
#: window, and why it ships off -- a capability that can remove its own
#: switch should not be handed to a model nobody has opted in.
DESKTOP_INPUT_TOOL = "desktop_control"

#: Document key for the switch. Same shape as ``tools.<id>`` and the same
#: reasoning: it is one more authority a settings screen has to be able to
#: write *and revoke*, so it lives in the saved document rather than in an
#: environment variable nobody opens.
DESKTOP_INPUT_VAR = "LUMINE_ENABLE_DESKTOP_INPUT"
DESKTOP_INPUT_SECTION = "desktop"
DESKTOP_INPUT_KEY = "input"

# The tool whose normal use is reading a page but whose highest reach is a
# form submission. It has its own toggle because its setup step (the browser
# engine) is optional in a way nothing else here is.
BROWSER_TOOLS = frozenset({"browse", "web_control"})

DISABLED_TOOLS_VAR = "LUMINE_DISABLED_TOOLS"
APP_LAUNCH_VAR = "LUMINE_ENABLE_APP_LAUNCH"
BROWSER_VAR = "LUMINE_ENABLE_BROWSER"

#: The saved-document section the Settings screen writes per-tool switches into.
#: A single name rather than a nested path, because this is the one toggle that
#: applies to every tool and so has no natural parent section.
TOOLS_SECTION = "tools"

#: The strictest action class each tool can trigger, by tool id.
#:
#: This is the source of both the Tools-page badge ("asks before it acts") and
#: the gate's default handling when a tool declares its class at the call
#: site. It is written down here as well as in each tool because a card that
#: cannot say what a tool does is a card the reader has to take on trust --
#: and because a drift between the two is a security bug, not a typo, so
#: ``test_permission_gate.py`` pins them against each other.
#:
#: ``read`` never asks, ``act`` never asks by default, everything at or above
#: ``sensitive`` always does. Nothing a settings value or a model says can
#: lower a row here.
TOOL_ACTION_CLASS: dict[str, str] = {
    "get_weather": "read",
    "get_news": "read",
    "search_web": "read",
    "recall_persona": "read",
    "find_app": "read",
    "open_app": "act",
    # The *highest* class `manage_window` can reach: its `terminate` verb
    # force-kills a process that no dialog can interrupt, so the badge says
    # "asks" even though focus/minimize/restore -- and now an ordinary close,
    # which the target application still gets to veto for unsaved work -- run
    # automatic. The gate uses the class each call site passes, per verb, so
    # this row is about what the card promises, never about what the floor
    # allows.
    "manage_window": "destructive",
    # Likewise for `desktop_control`: it can click anything and type anywhere,
    # so the badge says "asks" even though looking at the screen and clicking
    # an ordinary control run without a question. The class each call site
    # passes is computed per verb *and* per element -- clicking a control
    # named "Send" is a submission and clicking one named "Next" is not, which
    # is the same reasoning as `browser`'s class coming from the DOM.
    #
    # Computed from `desktop_ops` rather than written here, for the reason the
    # messaging rows are: the table is the one place that knows what the verbs
    # do, and a badge that restates the strictest of nine rows is a ninth
    # thing to keep in step.
    "desktop_control": desktop_ops.tool_action_class(),
    # Likewise for `browse`: open/observe are reads, clicking and typing are
    # acts, and a control that submits a form is sensitive -- which is what
    # the card's "asks" badge is about. The class each call site passes is
    # computed from the DOM in `browser.actions`, and a model or a page can
    # only push it toward asking, never away.
    "browse": "sensitive",
    # `web_control` is the semantic sibling of `browse` and reaches the same
    # places: a form submission through either is a submission. Declared here so
    # the Tools-page badge and the gate agree, which is the same reasoning as
    # every other row in this table.
    "web_control": "sensitive",
    # The messaging tools declare their class in `messaging.whatsapp_ops`, one row
    # per operation, and these three rows are *computed* from that table by
    # `tool_action_class` rather than written here. The reason is the ordinary one
    # for the rest of this dictionary: a card that cannot say what a tool does is
    # a card the reader has to take on trust, and a drift between the badge and
    # the behaviour is a security bug rather than a typo. With ~20 verbs across
    # four tools, restating the strictest one in both places would be a fourth
    # thing to keep in step -- and `whatsapp_chats` is the case that proves it:
    # its worst verb is delete-for-everyone (`destructive`) and its mildest is
    # pinning (`act`), so the badge has to come from the worst, not the typical.
    "whatsapp_read": whatsapp_ops.tool_action_class("whatsapp_read"),
    "whatsapp_chats": whatsapp_ops.tool_action_class("whatsapp_chats"),
    "whatsapp_account": whatsapp_ops.tool_action_class("whatsapp_account"),
    # The *highest* class `send_whatsapp` reaches: in automation mode it
    # really sends, irreversibly, from the user's own account -- so the row
    # says destructive and the badge says "asks". The draft-only default
    # mode passes `sensitive` at its own call site; either way the card
    # promises a question, and neither can be talked out of one.
    "send_whatsapp": whatsapp_ops.tool_action_class("send_whatsapp"),
    # `run_task` orchestrates the others, so its own floor is the *strictest*
    # thing any step it might run can be. It declares `destructive` rather than
    # inheriting each step's class, because a plan the model wrote may contain a
    # step we have not read yet -- and the floor has to be safe for the plan that
    # arrives, not the one that was expected. In practice most steps are reads
    # and ordinary acts and run without asking; the gate is consulted per step at
    # the step's own declared class, so this row is about what the *card* can
    # promise, never about what a read is blocked from doing.
    "run_task": "destructive",
    # Cancelling never asks: the user is the one who asked. Stopping work is
    # strictly less than doing it, so a question here would be a question about
    # obeying the instruction they just gave.
    "cancel_task": "act",
    # Playing a file from the user's own library. `act` and not `read`, because it
    # opens a window and starts audio; `act` and not `sensitive`, because it
    # changes nothing on disk and nothing leaves the machine. That is the same
    # argument `open_app` makes, and it is why nobody is asked before each song.
    #
    # Deliberately *not* `destructive`. There is no verb here that deletes,
    # moves or renames anything, and a card that says "destructive" for a tool
    # whose only act is `os.startfile` teaches people to click through the ones
    # that are.
    "play_media": "act",
}

_TRUTHY = {"1", "true", "yes", "on"}


def tool_id(tool: Any) -> str:
    """Return a tool's id whether it is a FunctionTool or a plain function."""
    return str(getattr(tool, "id", None) or getattr(tool, "__name__", "") or tool)


def permission_floor(ident: str) -> str:
    """The handling this tool gets at default policy: ``auto`` or ``ask``.

    Used for the card's badge. It is the floor, so it is honest in both
    directions: a user who tightens the policy gets *more* asking than the
    badge says, never less, and the badge never promises an automatic a
    sensitive class cannot have.
    """
    action_class = TOOL_ACTION_CLASS.get(str(ident).lower(), "sensitive")
    return effective_handling(action_class, "default")


def _document_off_ids() -> set[str]:
    """Tools the *saved document* has switched off.

    The document is the only place a toggle can be made from the Settings
    screen, so it has to be readable here as well as writable there -- otherwise
    the switch on the card and the switch the model sees are two different
    settings, and the one the user just flipped is the one that does nothing.

    Shape is a per-tool map rather than a list, because a map records the
    *decision* (``"on"`` as well as ``"off"``) and a settings screen writing a
    complete answer is easier to reason about than one writing an omission:

    .. code-block:: json

        {"tools": {"browse": "off", "web_control": "off", "open_app": "on"}}

    Only ``off`` switches anything. An unrecognised value is treated as ``on``
    rather than as ``off``: this key *reduces* what Lumine can do, and a typo in
    a key that reduces authority must fail towards the more capable behaviour
    only where the default already is that -- which it is. Anything that must
    fail closed (``send_whatsapp``) has its own mode switch, which does the
    opposite on purpose.
    """
    document = _saved_document()
    section = document.get(TOOLS_SECTION) if isinstance(document, Mapping) else None
    if not isinstance(section, Mapping):
        return set()
    off: set[str] = set()
    for ident, value in section.items():
        key = str(ident or "").strip().lower()
        if not key:
            continue
        if str(value or "").strip().lower() == "off":
            off.add(key)
    return off


def _saved_document() -> dict:
    """The saved configuration, or ``{}`` when there is none to read.

    Read defensively on purpose. This module is imported by the registry, which
    the pipeline factory imports, which runs during worker startup -- a raised
    ``OSError`` from an unreadable config file here would take down every
    session rather than just the screen that wanted to know.
    """
    try:
        document, _source, _diagnostics = effective_document()
    except Exception:  # noqa: BLE001 - "cannot read it" means "no opinion"
        return {}
    return document if isinstance(document, dict) else {}


#: The three values ``desktop.input`` accepts. ``off`` is the default, and only
#: ``on`` enables anything.
DESKTOP_INPUT_MODES = ("on", "off")

#: Tools that contradict `desktop_control`, and are switched off while it is on.
#:
#: Two tools that both click a web page is the problem `browse` and
#: `web_control` were already split to solve -- *by difficulty*. With a third one
#: that can drive the browser by coordinate, the model has three ways to click
#: the same button and no principled way to choose, which is how a request ends
#: up in the weakest of the three.
#:
#: So this is an exclusion rather than a preference. It is also the honest
#: statement of what `desktop_control` now is: it can drive any window, the
#: browser included, and keeping a second and third way to do the same thing
#: helps nobody. Turn it off and the browser tools come straight back --
#: nothing is disabled permanently, and the reason each card shows names the
#: switch responsible.
SUPPRESSED_BY_DESKTOP_INPUT: tuple[str, ...] = ("browse", "web_control")

#: The music library, behind a switch of its own. **Ships on**, unlike
#: `desktop.input` beside it, and the reason is that the capability is bounded in a
#: way the mouse is not.
#:
#: It shipped off originally, on the reasoning that a song played in the user's own
#: player is still playing when the conversation ends and the worker holds no
#: handle to it. That reasoning is right about the *irreversibility* and wrong
#: about the *boundary*: the folder is a single allowlisted directory, resolved and
#: compared before anything is opened, and nothing here can move, rename or delete
#: a file. A capability that can only open one file inside one folder the user
#: chose is a much smaller thing than one that can press any key in any
#: application, and it is the kind of small thing a person should not have to go
#: and enable before their assistant will play them a song.
#:
#: So it defaults on, and it stays revocable: the switch is one click away and the
#: Tools page still shows it. What a fresh install does **not** get is a folder --
#: with no `media.library` the tool answers "choose one in Settings" rather than
#: reaching anything, which is the honest state and not an enabled capability.
#:
#: **It still fails closed.** An unrecognised *explicit* value reads as `off` at
#: every level, and only an **absent** value reaches the default. Reading a
#: misspelling as "fall through to the default" would mean
#: `LUMINE_ENABLE_MEDIA=enabled` turned the library on -- the exact inverse of what
#: the person who typed it meant -- which is the same bug `desktop.input` is
#: documented against, and the only thing that changed here is what the default is.
MEDIA_TOOL = "play_media"
MEDIA_VAR = "LUMINE_ENABLE_MEDIA"
MEDIA_SECTION = "media"
MEDIA_KEY = "enabled"


def _media_mode() -> str:
    """Whether the music library is switched on. On unless told otherwise.

    The same four-step precedence as :func:`_desktop_input_mode`, with the default
    at the other end: ``media.enabled`` is a direct answer and wins, then the
    Tools-page card, then the environment, then **on**.
    """
    document = _saved_document()
    section = document.get(MEDIA_SECTION) if isinstance(document, Mapping) else None
    if isinstance(section, Mapping):
        value = section.get(MEDIA_KEY)
        if value is not None:
            text = str(value).strip().lower()
            return text if text in DESKTOP_INPUT_MODES else "off"
    tools = document.get(TOOLS_SECTION) if isinstance(document, Mapping) else None
    if isinstance(tools, Mapping):
        card = tools.get(MEDIA_TOOL)
        if card is not None and str(card).strip().lower() in _TRUTHY:
            return "on"
        if card is not None:
            return "off"
    raw = str(os.getenv(MEDIA_VAR, "") or "").strip().lower()
    if raw in DESKTOP_INPUT_MODES:
        return raw
    if raw in _TRUTHY:
        return "on"
    if raw:
        # **An unrecognised value fails closed**, and that is the whole reason the
        # default is not consulted here. This key *grants* a capability, so a
        # misspelling has to leave it off; falling through to the (now `on`)
        # default would mean `LUMINE_ENABLE_MEDIA=enabled` turned the library on,
        # which is the exact inverse of what the person who typed it meant.
        # `desktop.input` fails closed for the same reason and reads it differently
        # only because its default happens to agree.
        return "off"
    # Ships on. `ask` is deliberately not honoured -- there is nothing to ask
    # about, and a machine configured by file that says "ask" meant "I am not sure",
    # which is answered by on-with-a-switch rather than by a capability that never
    # works.
    return "on"


def media_disabled_reason() -> str:
    """Why the music library is off, or ``""``. One switch, so one reason."""
    mode = _media_mode()
    if mode == "on":
        return ""
    if mode == "off":
        return f"Turned off by {MEDIA_SECTION}.{MEDIA_KEY}."
    return f"Turned off by {MEDIA_VAR}."


def _desktop_input_mode() -> str:
    """Whether the mouse and keyboard are switched on. Off unless told otherwise.

    **The Tools page card is the switch**, because that is what the product
    promised: one place to turn this on, next to every other capability. So a
    document that says ``tools.desktop_control: "on"`` and nothing else is
    *on*. There were two switches here at one point -- this key and the card --
    and the card did nothing while this one read ``off``, which is worse than
    either: a user flips a switch in the app and nothing happens, and concludes
    the feature is broken.

    Precedence, most explicit first:

    1. ``desktop.input`` -- a direct answer, and it *wins* over the card, so a
       hand-written document can pin this off regardless of what the card says.
    2. ``tools.desktop_control: "on"`` -- the card. Only an explicit ``on``
       enables: an absent key means the card was never touched, and a fresh
       install must not arrive with the mouse already under an agent's control.
    3. ``LUMINE_ENABLE_DESKTOP_INPUT`` -- for a machine configured by file.
    4. ``off``.

    An unrecognised value at any level reads as ``off`` -- the opposite of the
    plain ``tools.<id>`` switch, and for the same reason ``messaging.whatsapp.mode``
    also differs from it: this key *grants* authority rather than reducing it,
    so the direction that fails safe is the one that keeps the capability off.
    """
    document = _saved_document()
    desktop = document.get(DESKTOP_INPUT_SECTION) if isinstance(document, Mapping) else None
    if isinstance(desktop, Mapping):
        value = desktop.get(DESKTOP_INPUT_KEY)
        if value is not None:
            text = str(value).strip().lower()
            return text if text in DESKTOP_INPUT_MODES else "off"
    # 2. The Tools page card. Explicit `"on"` only -- an absent key means nobody
    #    has touched the card, and a fresh install must not arrive enabled.
    tools = document.get(TOOLS_SECTION) if isinstance(document, Mapping) else None
    if isinstance(tools, Mapping):
        card = tools.get(DESKTOP_INPUT_TOOL)
        if card is not None and str(card).strip().lower() in _TRUTHY:
            return "on"
        if card is not None:
            return "off"
    # 3. A machine configured by file rather than by screen.
    raw = str(os.getenv(DESKTOP_INPUT_VAR, "") or "").strip().lower()
    if raw in DESKTOP_INPUT_MODES:
        return raw
    if raw in _TRUTHY:
        return "on"
    # 4. Ships off.
    return "off"


def _disabled_ids() -> set[str]:
    raw = os.getenv(DISABLED_TOOLS_VAR, "") or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def tool_allowed(tool: Any) -> bool:
    """Check a single tool against the environment toggles."""
    return not disabled_reason(tool)


def disabled_reason(tool: Any) -> str:
    """Why a tool is switched off, or an empty string when it is available.

    The Tools page shows this next to a disabled card. "Not available" and "you
    turned this off" are different things to be told, and a card that only had a
    boolean would have to pick one -- the wrong one, since the common case is a
    deliberate choice someone made and then forgot they made.

    Returned rather than raised, and rather than a bool, because both callers need
    it: `tool_allowed` for the filter, this text for the screen.
    """
    ident = tool_id(tool).lower()
    # Document first, then the environment: the same per-key precedence as
    # `messaging.whatsapp.mode`, so a switch flipped on the Tools page wins over
    # a line in `.env` rather than the two quietly disagreeing.
    if ident in _document_off_ids():
        return f"Turned off in Settings ({TOOLS_SECTION}.{ident})."
    if ident in _disabled_ids():
        return f"Turned off by {DISABLED_TOOLS_VAR}."
    if ident in DESKTOP_TOOLS:
        enabled = (os.getenv(APP_LAUNCH_VAR, "true") or "true").strip().lower()
        if enabled not in _TRUTHY:
            return f"Turned off by {APP_LAUNCH_VAR}."
    if ident == DESKTOP_INPUT_TOOL:
        # Document first, then the environment, for the same per-key precedence
        # `messaging.whatsapp.mode` uses. Off unless something turns it *on*,
        # which is the opposite of every other toggle in this function and is
        # deliberate: the safe state has to be the one you get by doing nothing.
        mode = _desktop_input_mode()
        if mode != "on":
            return (
                f"Turned off by {DESKTOP_INPUT_SECTION}.{DESKTOP_INPUT_KEY} "
                f"(or {DESKTOP_INPUT_VAR})."
            )
    if ident in SUPPRESSED_BY_DESKTOP_INPUT and _desktop_input_mode() == "on":
        # The exclusion, and deliberately one-directional: turning the desktop
        # control on silences the browser tools, and turning it off brings them
        # straight back. Nothing here is disabled permanently -- the reason names
        # the switch, so the card says what to do about it rather than looking
        # broken.
        #
        # Gated on the tool actually being *live*, not merely on the switch being
        # set. The two are not the same, because the switch that grants authority
        # and the card that switches a tool off are separate keys: a document
        # carrying `desktop.input = on` alongside `tools.desktop_control = off`
        # reaches this line with the desktop tool already refused above. Reading
        # the switch alone there silenced two browser tools on the authority of a
        # capability nobody has, which is the one state in which this exclusion is
        # not merely unnecessary but actively harmful -- the user switched the
        # thing on and then turned it off again, and lost the tools they had.
        if disabled_reason(DESKTOP_INPUT_TOOL):
            return ""
        # Short on purpose. This is the one reason on the page that is a
        # *consequence of another switch* rather than a statement about this tool,
        # so it has to be read as "something else changed this" -- and it is
        # clamped to two lines on the card. A sentence that needed truncating
        # would leave the reader guessing at the half they cannot see, and the
        # remedy is one click away on a card they can see.
        return (
            f"While {DESKTOP_INPUT_SECTION}.{DESKTOP_INPUT_KEY} is on, "
            f"{DESKTOP_INPUT_TOOL} can drive a browser itself."
        )
    if ident in BROWSER_TOOLS:
        enabled = (os.getenv(BROWSER_VAR, "true") or "true").strip().lower()
        if enabled not in _TRUTHY:
            return f"Turned off by {BROWSER_VAR}."
    if ident == MEDIA_TOOL:
        # Checked *after* the per-id switches above, so a user who turned the card
        # off on the Tools page is told that, rather than being told about a
        # section they may not know exists.
        reason = media_disabled_reason()
        if reason:
            return reason
    if ident in MESSAGING_TOOLS:
        # The one toggle that is not environment-only: its off switch lives
        # in the saved document (`messaging.whatsapp.mode`) with the
        # environment underneath, because "this app may read and send in my
        # name" is a decision a settings screen should be able to make and
        # revoke, not one that only a file edit can reach. One switch for all
        # four tools, deliberately -- see `MESSAGING_TOOLS`.
        reason = whatsapp_disabled_reason()
        if reason:
            return reason
        if resolve_history_mode() == "off":
            # A narrower reason than the mode being off: the messaging tools
            # themselves are available and only *history* has been switched
            # off. Two different problems, and a card that reported the
            # wrong one would send the user looking in the wrong place.
            return f"Turned off by messaging.whatsapp.history ({HISTORY_VAR})."
    if env_permission_levels().get(ident) == "off":
        # The document-level equivalent is enforced by the gate at call time;
        # this keeps the card honest about the environment form, which is the
        # one this screen has always reflected.
        return f"Turned off by {PERMISSIONS_VAR}."
    return ""


def filter_permissions(tools: Iterable[Any]) -> list[Any]:
    """Drop every tool the current environment does not permit."""
    return [tool for tool in tools if tool_allowed(tool)]


def enabled_tool_ids() -> list[str]:
    """Every tool id the current configuration permits, in registry order.

    Computed from :data:`TOOL_ACTION_CLASS` rather than from ``ALL_TOOLS``, and
    that is deliberate: this function is called *by* the orchestrator, which the
    registry imports, so importing the registry here would be a cycle whose
    resolution depends on who imported first. The table it reads is the same table
    the gate enforces and the Tools page renders from, so the answer is the same
    one -- and ``test_tools.py`` asserts the two agree.
    """
    order = tuple(TOOL_ACTION_CLASS)
    probe = _IdentityTool
    return [
        ident
        for ident in order
        if ident in TOOL_ACTION_CLASS and not disabled_reason(probe(ident))
    ]


class _IdentityTool:
    """A stand-in that carries only an id, for the checks that need nothing else.

    ``disabled_reason`` reads an id and the environment. Substituting this for a
    live ``FunctionTool`` means the question "is this tool permitted?" can be
    answered without constructing the tool, which is what lets the orchestrator ask
    it without a registry import.
    """

    __slots__ = ("id",)

    def __init__(self, ident: str) -> None:
        self.id = ident

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_IdentityTool({self.id!r})"
