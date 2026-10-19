"""`run_task`: one tool call that carries a whole objective to a verified end.

## What this is for

Every symptom this project started with was a coordination failure, not a tool
failure. Lumine opened Chrome and *then* opened her own browser. She opened Krita
twice. She typed a WhatsApp message before the chat was the right one. In every
case the individual tools worked; what was missing was a layer that decides
*which* environment, *in what order*, and whether each step actually happened.

That layer is three modules, and this tool is where they meet:

* :mod:`agent.runtime.router` -- which environment (pure, over a request plus
  facts about the machine);
* :mod:`agent.runtime.task_model` -- the ordered steps and their dependencies;
* :mod:`agent.runtime.task_engine` -- deterministic progression, retries,
  cancellation, verification.

The model still decides *what the user wants*. This decides how to get there
without re-deciding after every step -- which is the only way "open Chrome, go to
YouTube, search for a song" survives contact with a real browser.

## When the model should reach for it

For anything with two or more dependent steps, and for anything where a wrong
environment is visible to the user. For a single cheap lookup the ordinary tools
are better: this one pays for classification and a plan even when the answer is
one API call. The tool policy says so, and the routing table below follows it.

## What it returns

A structured result, not prose: the route chosen and why, each step's status and
verification status, and -- crucially -- what remains *uncertain*. The model is
told what is true rather than what went well, because a task report that says
"done" when a launch produced no window is worse than no report.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

try:
    from ..runtime import progress, router, task_engine, task_model, verification
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime import progress, router, task_engine, task_model, verification  # type: ignore[no-redef]

try:
    from ..runtime import latency as latency_module
    from ..runtime.frame_sampler import need_picture
    from ..runtime.latency import measure as latency_measure
    from ..runtime.permission_gate import require_approval
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime import latency as latency_module  # type: ignore[no-redef]
    from runtime.frame_sampler import need_picture
    from runtime.latency import measure as latency_measure
    from runtime.permission_gate import require_approval

try:
    from ..settings.config_store import effective_document
except ImportError:  # running as a top-level script with agent/ on sys.path
    from settings.config_store import effective_document

# Sibling modules, always reachable by relative name. The `browser` subpackage
# counts as a sibling here: `tools/browser` is a package, and `control`, `engine`,
# `locators` and `state` are reached through it.
from . import (
    app_index,
    app_resolver,
    apps,
    http_client,
    tool_results,
    tools_permission,
    web_search,
)
from .browser import (
    actions as browser_actions,
    control,
    engine as browser_engine,
    locators,
    search as browser_search,
    session as browser_session,
    sites,
    state,
)
from . import windows_control
from .desktop import capture as desktop_capture
from .desktop import control as desktop_control
from .desktop import input as desktop_input
from .desktop import ops as desktop_ops
from .tools_compat import RunContext, ToolError, function_tool
from .tools_text import clip

logger = logging.getLogger("lumine")

# -- how long the whole task may take ------------------------------------------

#: A whole task is bounded. A voice turn that holds the floor for two minutes
#: stops sounding like a conversation, and a user who has waited that long has
#: already given up on the result -- so the task stops, says what it stopped
#: after, and frees the turn.
TASK_BUDGET_SECONDS = 90.0

#: Per-step bound. Chosen above the slowest thing a step legitimately does -- a
#: cold app launch with a splash screen, a slow-tier application index probe -- and
#: below the point where a person would assume the app had hung.
STEP_BUDGET_SECONDS = 25.0

#: A task may not exceed this many steps. A voice request that needs more is a
#: request whose intent was not understood, and continuing would spend the user's
#: time discovering that.
MAX_STEPS = 8

#: Routing preferences, from the saved document and the environment underneath.
#: Declared here rather than in the router because this is the tool layer's
#: configuration, and the router stays a pure function of the request plus the
#: facts it is handed.
DEFAULT_BROWSER_VAR = "LUMINE_DEFAULT_BROWSER"
OWN_BROWSER_VAR = "LUMINE_PREFER_OWN_BROWSER"

#: The tool id whose call brackets a whole task, and the name every step record
#: is published under.
#:
#: Two different things share one constant on purpose. The desktop app keys the
#: step ledger's block off the `tool_status` record for *this* tool's call --
#: its header, its status and its duration come from that bracket -- while the
#: steps inside it carry `tool=TASK_TOOL` too so the app never has to decide
#: whether a stray step belongs to the block it is drawing. The steps
#: themselves are distinguished from the bracket by their `action`, exactly as
#: a WhatsApp send's steps are from its own call.
TASK_TOOL = "run_task"


# -- machine facts ------------------------------------------------------------


def installed_names() -> tuple[str, ...]:
    """Every app the index knows about, normalized. Read in a thread by the caller."""
    return tuple(app_index.normalize_key(app.name) for app in app_index.entries())


def window_titles() -> tuple[str, ...]:
    return tuple(title.casefold() for title, _handle, _program in app_resolver.window_facts())


async def machine_facts(
    *,
    include_apps: bool = True,
    include_windows: bool = True,
) -> router.MachineFacts:
    """What the router is allowed to know, read off the live machine.

    Both reads can be slow -- the app index is a directory walk, the window list a
    ctypes enum -- so they happen in threads and never on the event loop. A read
    that *fails* yields empty facts, which the router treats as "cannot tell"
    rather than "not installed": the difference decides whether "open Krita"
    launches Krita or asks the user which browser they meant.
    """
    installed: tuple[str, ...] = ()
    windows: tuple[str, ...] = ()
    if include_apps:
        try:
            installed = await asyncio.to_thread(installed_names)
        except Exception:  # noqa: BLE001 - no facts is a valid answer
            logger.debug("[task] app index read failed", exc_info=True)
    if include_windows:
        try:
            windows = await asyncio.to_thread(window_titles)
        except Exception:  # noqa: BLE001
            logger.debug("[task] window read failed", exc_info=True)
    document, _source, _diagnostics = effective_document()
    return router.MachineFacts.build(
        installed=installed,
        windows=windows,
        browser_open=_browser_has_page(),
        default_browser=_configured_browser(document),
        own_browser_default=bool(_own_browser_default(document)),
        whatsapp_ready=_whatsapp_ready(document),
    )


def _browser_has_page() -> bool:
    """Whether Lumine's isolated browser already has a page.

    Read from `session`'s own holder rather than by asking Playwright: this runs
    on a worker thread, where a Playwright object belongs to a different loop and
    touching it would raise. The only fact needed is whether one exists, and
    ``False`` is the safe answer -- "act on the current page" then reports that no
    page is open rather than acting on nothing.
    """
    try:
        page = browser_session._browser._page  # noqa: SLF001 - one holder, one reader
        return page is not None and not page.is_closed()
    except Exception:  # noqa: BLE001 - "cannot tell" is "no page, as far as we know"
        return False


def _section(document: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    current: Any = document
    for key in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _configured_browser(document: Mapping[str, Any]) -> str:
    """The user's default desktop browser, as a normalized app name.

    Read from the saved document first and the environment underneath, mirroring
    every other setting in this product. It is only consulted for a request that
    does *not* name a browser, and it is recorded in the route's notes so a wrong
    choice is visible in the log rather than invisible in the behaviour.

    ``desktopBrowser`` wins over ``default`` because they are synonyms and the
    settings screen uses the former; the precedence is stated in
    `validation._check_desktop` so it is not an accident.
    """
    section = _section(document, "desktop", "browsers")
    chosen = str(section.get("desktopBrowser") or "").strip()
    if not chosen:
        chosen = str(section.get("default") or "").strip()
    if not chosen:
        chosen = (os.getenv(DEFAULT_BROWSER_VAR, "") or "").strip()
    return chosen.casefold()


def _own_browser_default(document: Mapping[str, Any]) -> bool:
    """Whether an unnamed "open a website" should mean Lumine's own browser.

    Default ``False``. Asking the user is better than picking, and picking the
    *wrong* browser is the bug this whole layer exists to prevent. Both spellings
    are accepted because the two halves of this product describe the same idea in
    different words.
    """
    section = _section(document, "desktop", "browsers")
    for key in ("preferOwnBrowser", "ownBrowser", "isolate"):
        if key in section:
            return bool(section[key])
    raw = (os.getenv(OWN_BROWSER_VAR, "") or "").strip().casefold()
    return raw in ("1", "true", "yes", "on")


def _whatsapp_ready(document: Mapping[str, Any]) -> bool:
    section = _section(document, "messaging", "whatsapp")
    return str(section.get("mode") or "").strip().casefold() == "automation"


# -- step implementations ------------------------------------------------------
#
# One function per verb the orchestrator can perform, each returning a
# verification outcome. They are looked up through a table rather than called by
# name so a test can substitute any of them, and so the planner can validate a
# step against the set that actually exists.


@dataclass
class StepResult:
    """What one step did, in the vocabulary the task reports."""

    outcome: verification.ActionOutcome
    #: Facts later steps may read. Passed forward in ``task.outputs`` rather than
    #: through the model, because a value a step computed should not have to
    #: survive a round trip through prose to reach the next step.
    outputs: dict[str, Any] = field(default_factory=dict)


StepImpl = Callable[["StepContext"], Awaitable[StepResult]]


@dataclass
class StepContext:
    """Everything one step needs, and nothing it does not.

    The permission call is a *callback* rather than an import, so the same
    implementations run under a real gate in production and a recording stub in a
    test without the planner knowing the difference.
    """

    step: task_model.Step
    route: router.Route
    request: str
    outputs: Mapping[str, Any]
    approve: Callable[..., Awaitable[None]]
    #: The LiveKit session, when there is one. Used only to publish measurements;
    #: every step works without it, which is what keeps them testable.
    session: Any = None

    def output(self, key: str, default: Any = "") -> Any:
        value = self.outputs.get(key, default)
        return default if value is None or value == "" else value


# -- implementations -----------------------------------------------------------


async def _step_open_app(ctx: StepContext) -> StepResult:
    """Resolve a name to one target, then focus or launch it.

    This is the step where "open Krita" stops being a name and becomes an
    application. Three things happen in order and all three are load-bearing:
    resolution (which program), window check (is one already open), and a
    post-launch window check. The last is what turns "launched" into "opened".

    The launch goes through `apps.claim_launch`, the *same* dedup claim the
    `open_app` tool takes. That is what makes the orchestrator and the single-step
    tool one mechanism rather than two: a request that arrives twice -- as two
    steps of one task, or as a task and a tool call -- produces one window, and
    the second attempt reports as suppressed rather than as a second launch.
    """
    name = str(ctx.step.args.get("app_name") or ctx.route.target or ctx.request).strip()
    await ctx.approve(
        tool="run_task",
        action="open_app",
        action_class="act",
        summary=f"Open the app {clip(name, 40)}",
        target=clip(name, 60),
    )
    # Resolution and launch are measured separately because they have completely
    # different shapes: resolution is a directory walk plus a window enum (warm,
    # cheap, and the thing that grows with a large Start Menu), while launch is a
    # process start plus a wait for a window (unbounded, and the part the user
    # actually notices).
    resolve_started = time.monotonic()
    target = await app_resolver.resolve(name)
    resolve_ms = (time.monotonic() - resolve_started) * 1000
    _emit_latency(
        ctx.session,
        "app_resolve",
        resolve_ms,
        app=name[:40],
        resolved=target.resolved,
        kind=target.kind,
    )
    if not target.resolved:
        return StepResult(
            verification.failed(
                "open_app", f"I couldn't find an app called {clip(name, 40)}"
            )
        )
    key = apps.launch_key(name)
    if not apps.claim_launch(key):
        logger.info("[run_task] suppressed a repeat launch of %s", name)
        # Not a failure and not a launch: the app is already on its way up. Reported
        # as `observed` with the evidence being the suppression, so a task does not
        # retry it -- and so the user is told why there is only one window.
        return StepResult(
            verification.observed(
                "open_app",
                f"{clip(target.name, 40)} was already being opened",
                evidence="a launch of this app was already claimed",
                suppressed=True,
            ),
            {"app": target.name, "kind": target.kind},
        )
    outcome: verification.ActionOutcome | None = None
    launch_started = time.monotonic()
    try:
        outcome = await app_resolver.open_target(
            target, new_instance=bool(ctx.step.args.get("new_instance"))
        )
    finally:
        launch_ms = (time.monotonic() - launch_started) * 1000
        _emit_latency(
            ctx.session,
            "app_launch",
            launch_ms,
            app=name[:40],
            kind=target.kind,
            focused=bool(target.has_window),
            status=outcome.status if outcome is not None else "none",
        )
        # Released on failure so a retry is still possible; kept on success, so the
        # dedup window still suppresses an immediate repeat.
        if outcome is None or not outcome.ok:
            apps.release_launch(key)
    outputs: dict[str, Any] = {"app": target.name, "kind": target.kind}
    return StepResult(outcome, outputs)


async def _step_focus_window(ctx: StepContext) -> StepResult:
    """Bring an already-open window to the front."""
    name = str(ctx.step.args.get("window") or ctx.route.target or "").strip()
    matches = app_resolver.match_windows(name, app_resolver.window_facts())
    if not matches:
        return StepResult(verification.failed("focus", f"no window matches {clip(name, 40)}"))
    title, handle, _program = matches[0]
    await ctx.approve(
        tool="run_task",
        action="focus",
        action_class="act",
        summary=f"Bring the {clip(title, 40)} window to the front",
        target=clip(title, 60),
    )
    try:
        await asyncio.to_thread(app_resolver.focus_window, handle)
    except Exception as exc:  # noqa: BLE001 - a refused focus is a real answer
        return StepResult(verification.uncertain("focus", f"I couldn't bring {clip(title, 30)} forward: {clip(str(exc), 60)}"))
    return StepResult(
        verification.observed("focus", f"Brought {clip(title, 40)} forward", evidence="its window was already open")
    )


async def _step_search_web(ctx: StepContext) -> StepResult:
    """A background lookup. The cheapest thing that can answer a question.

    Reuses `web_search`'s own request and parsing rather than a second
    implementation: two DuckDuckGo scrapers would drift, and the one that drifts
    is the one whose result the model quotes to the user.
    """
    query = str(ctx.step.args.get("query") or _query_from_request(ctx.request)).strip()
    await ctx.approve(
        tool="run_task",
        action="search",
        action_class="read",
        summary=f"Search the web for {clip(query, 40)}",
        target=clip(query, 80),
    )
    try:
        results = await _search(query)
    except Exception as exc:  # noqa: BLE001 - a failed lookup is a sentence
        return StepResult(verification.failed("search_web", clip(str(exc), 120)))
    if not results:
        return StepResult(verification.failed("search_web", f"No results for {clip(query, 40)}"))
    return StepResult(
        verification.observed(
            "search_web",
            f"Found {len(results)} result(s) for {clip(query, 30)}",
            evidence="titles and links were returned",
            titles=[title[:60] for title, _url in results[:3]],
            urls=[url[:80] for _title, url in results[:3]],
        )
    )


#: Injected so a test drives the lookup without a network, and so this function is
#: the only place the search implementation is named.
_search_impl: Callable[[str], Awaitable[list[tuple[str, str]]]] | None = None


async def _search(query: str) -> list[tuple[str, str]]:
    """``(title, url)`` triples from DuckDuckGo, through `web_search`."""
    if _search_impl is not None:
        return await _search_impl(query)
    async with http_client.shared_client() as client:
        response = await client.get(
            web_search.SEARCH_URL,
            params={"q": query},
            headers={"User-Agent": web_search.USER_AGENT},
            follow_redirects=True,
            timeout=web_search.TIMEOUT,
        )
    if response.status_code != 200:
        raise RuntimeError("web search is unavailable right now")
    return [(title, url) for title, _snippet, url in web_search.parse_results(response.text)[: web_search.MAX_RESULTS]]


async def _step_browser_open(ctx: StepContext) -> StepResult:
    """Open a page in Lumine's isolated browser."""
    url = str(ctx.step.args.get("url") or ctx.route.url or "").strip()
    if not url:
        return StepResult(verification.failed("open_page", "no address was given"))
    await ctx.approve(
        tool="run_task",
        action="open_page",
        action_class="read",
        summary=f"Open {clip(url, 60)}",
        target=clip(url, 100),
    )
    engine_obj = control.engine()
    try:
        page = await engine_obj.navigate(url)
    except Exception as exc:  # noqa: BLE001 - a real Playkit failure is a sentence
        return StepResult(verification.failed("open_page", clip(str(exc), 120)))
    return StepResult(
        verification.observed(
            "open_page",
            f"Opened {clip(str(page.get('title') or url), 40)}",
            evidence="the page reported its title and address",
            url=str(page.get("url") or url),
            generation=1,
        )
    )


async def _step_browser_find(ctx: StepContext) -> StepResult:
    """Read the page and resolve one control by meaning.

    The read and the resolve are one step on purpose: a search is only meaningful
    against a snapshot, and taking the snapshot here means the generation this step
    observed is the one the next step's action is checked against.
    """
    engine_obj = control.engine()
    gate = getattr(engine_obj, "gate", None)
    try:
        payload = await engine_obj.observe()
    except Exception as exc:  # noqa: BLE001
        return StepResult(verification.failed("find", f"I couldn't read the page: {clip(str(exc), 80)}"))
    snapshot = gate.stamp(payload) if gate is not None else state.snapshot_from_script(1, payload)
    try:
        query = locators.build_query(
            name=str(ctx.step.args.get("name") or ""),
            role=str(ctx.step.args.get("role") or ""),
            strategy=str(ctx.step.args.get("strategy") or ""),
            css=str(ctx.step.args.get("css") or ""),
            position=str(ctx.step.args.get("position") or ""),
        )
    except locators.LocatorError as exc:
        return StepResult(verification.failed("find", str(exc)))
    resolution = state.locate(snapshot, query)
    if resolution.chosen is None:
        # What is *on the page*, not what matched. A refusal that says "there is
        # nothing here" when three similar controls exist is what sends the model
        # off inventing a different phrase, so the near-misses travel with it.
        #
        # `describe()` rather than `reason`, because `reason` for an ambiguous
        # page is the three words "more than one matches" and that is the whole
        # of what the model used to be told. Measured on a real session: the
        # step failed with exactly that and the task gave up, even though
        # `describe()` had the two candidates and their positions sitting right
        # there. The `candidates` field is still attached for anything reading
        # the structured outcome, but the note is what the model reads, so the
        # note has to be actionable on its own.
        return StepResult(
            verification.failed(
                "find",
                resolution.describe(),
                candidates=[item.name[:40] for item in snapshot.candidates[:6]],
            )
        )
    chosen = resolution.chosen
    return StepResult(
        verification.observed(
            "find",
            f"Found {clip(chosen.name, 40)}",
            evidence="the page reported it as a visible control",
            role=chosen.role,
            name=chosen.name[:60],
            index=chosen.index,
            generation=snapshot.generation,
        ),
        {"element": chosen.to_dict(), "generation": snapshot.generation},
    )


async def _step_browser_act(ctx: StepContext) -> StepResult:
    """Act on the control a previous step resolved, then verify.

    The element is carried forward from the `find` step rather than re-described
    here, and its generation is passed with it. If the page changed between the two
    steps the generation check refuses, which is the intended behaviour: the model
    described a control against a page that no longer looks like that.
    """
    element = ctx.output("element")
    if not isinstance(element, dict) or not element:
        return StepResult(verification.failed("act", "I haven't found the control yet"))
    verb = str(ctx.step.args.get("verb") or "click").strip().casefold()
    try:
        query = locators.build_query(
            name=str(element.get("name") or ""),
            role=str(element.get("role") or ""),
        )
    except locators.LocatorError as exc:
        return StepResult(verification.failed("act", str(exc)))

    label = clip(str(element.get("name") or "control"), 40)
    await ctx.approve(
        tool="run_task",
        action=verb,
        action_class=str(ctx.step.args.get("action_class") or "act"),
        summary=f"{verb.replace('_', ' ').title()} {label}",
        target=clip(str(ctx.output("url") or ""), 100),
    )
    engine_obj = control.engine()
    step = browser_engine.EngineAction(
        kind=verb,
        query=query,
        index=int(element.get("index") or 0),
        text=clip(str(ctx.step.args.get("text") or ""), 600),
        key=str(ctx.step.args.get("key") or ""),
        generation=int(ctx.output("generation") or 0),
        expected={
            "name": str(element.get("name") or ""),
            "role": str(element.get("role") or ""),
        },
    )
    try:
        result = await engine_obj.act(step)
    except browser_engine.EngineError as exc:
        return StepResult(verification.failed(verb, str(exc)))
    except locators.LocatorError as exc:
        return StepResult(verification.failed(verb, str(exc)))
    except Exception as exc:  # noqa: BLE001
        return StepResult(verification.failed(verb, clip(str(exc), 100)))
    after = result.get("after") or {}
    return StepResult(
        verification.observed(
            verb,
            f"{verb.replace('_', ' ').title()} {label}",
            evidence="the page was read again afterwards",
            url=str(after.get("url") or ""),
            title=str(after.get("title") or ""),
        ),
        {"url": str(after.get("url") or ""), "title": str(after.get("title") or "")},
    )


async def _step_browser_search(ctx: StepContext) -> StepResult:
    """Type a search into the page's own search control, submit it, and check.

    One step for all three, and that is the fix for the reported failure rather
    than a style choice. Splitting "fill" and "submit" into two steps meant the
    second one could be lost, be skipped by a re-plan, or never be planned at all
    -- so Lumine typed a search into YouTube and stopped, which reads to the user
    as "she cannot search".

    The work itself lives in :mod:`tools.browser.search`, because the
    `web_control` ``search`` verb does the same thing from a conversation. Two
    implementations of a search is one that will be the right one.
    """
    query = browser_search.normalize_query(ctx.step.args.get("query"))
    if not query:
        return StepResult(verification.failed("browser_search", "there was nothing to search for"))

    engine_obj = control.engine()
    gate = getattr(engine_obj, "gate", None)
    try:
        payload = await engine_obj.observe()
    except Exception as exc:  # noqa: BLE001
        return StepResult(verification.failed("browser_search", f"I couldn't read the page: {clip(str(exc), 80)}"))
    snapshot = gate.stamp(payload) if gate is not None else state.snapshot_from_script(1, payload)

    async def approve(*, action_class: str, summary: str, target: str) -> None:
        # The class is the search's own, computed from the field's DOM facts --
        # so a search box inside a submitting form asks, exactly as clicking that
        # form's button would.
        await ctx.approve(
            tool="run_task",
            action="search",
            action_class=action_class,
            summary=summary,
            target=target,
        )

    outcome = await browser_search.run_search(
        engine_obj,
        snapshot,
        query,
        approve=approve,
        tool="run_task",
        # The session, not a RunContext: this is what `progress.publisher_for`
        # resolves either spelling from, and a task step holds the session rather
        # than the context the tool was handed.
        context=ctx.session,
    )
    if outcome.status == verification.OBSERVED:
        try:
            result_payload = payload if outcome.data.get("reused") else await engine_obj.observe()
            result_snapshot = (
                gate.stamp(result_payload)
                if gate is not None
                else state.snapshot_from_script(snapshot.generation + 1, result_payload)
            )
            outcome = verification.observed(
                outcome.action,
                outcome.detail,
                evidence=outcome.evidence,
                **outcome.data,
                results=browser_search.result_items(result_snapshot),
            )
        except Exception as exc:  # noqa: BLE001 - the search itself remains verified
            logger.warning(
                "[Task] search succeeded but its result cards could not be read: %s",
                exc,
            )
            outcome = verification.observed(
                outcome.action,
                outcome.detail,
                evidence=outcome.evidence,
                **outcome.data,
                results_error=clip(str(exc), 100),
            )
    return StepResult(outcome, {"url": str(outcome.data.get("url") or "")})


async def _step_ask(ctx: StepContext) -> StepResult:
    """Pause and ask the user one question.

    A step rather than an exception, because ambiguity is not a failure: "which
    browser did you mean" is a *state* the task is in, and modelling it as a step
    means the engine can hold everything after it without pretending the task
    ended.
    """
    question = str(ctx.step.args.get("question") or "").strip()
    if not question:
        return StepResult(verification.failed("ask", "there was nothing to ask"))
    return StepResult(verification.uncertain("ask", question, question=question))


# -- desktop -------------------------------------------------------------------
#
# Two steps, and the split is the whole reason they are two. A screen has to be
# *read* before it can be *acted on*, and the thing read is a set of references
# that belongs to one moment: `desktop_capture` mints a generation for exactly
# this reason, and an act naming a different one is refused. So the observe is a
# step with an output, and the act depends on it -- which is what lets the
# engine report "the screen moved" as its own step instead of folding it into a
# click that went somewhere.


async def _step_desktop_observe(ctx: StepContext) -> StepResult:
    """Read the window in front of the user and record what can be clicked.

    A read, so it asks nothing and never runs the gate. It still refuses the
    same integrated windows the tool does, because a plan that observed a
    browser and then tried to click it would be reported as a desktop task that
    failed at the last step rather than as one that never should have started.
    """
    hwnd = desktop_capture.last_observation()
    target = str(ctx.step.args.get("window") or "").strip()
    if target:
        hwnd = _desktop_window_handle(target)
    if not hwnd:
        return StepResult(
            verification.failed("desktop_observe", "I can't tell which window is in front of you")
        )

    names = desktop_capture.process_names_for(hwnd)
    integration = desktop_ops.integration_for(names)
    if integration is not None:
        return StepResult(
            verification.failed(
                "desktop_observe",
                desktop_ops.refusal_for(integration),
                tool=integration.tool,
            )
        )

    observation = await asyncio.to_thread(desktop_capture.observe, hwnd)
    return StepResult(
        verification.observed(
            "desktop_observe",
            f"{len(observation.elements)} things I can click in "
            f"{observation.title or 'that window'}",
            evidence="the accessibility tree the application reported",
        ),
        {
            "generation": observation.generation,
            "window": observation.title,
            "addressable": observation.addressable,
        },
    )


def _desktop_window_handle(query: str) -> int | None:
    """The window whose title answers ``query``, or ``None``.

    Loose title matching, from `windows_control.match_window`, because that is
    the house style for "chrome" meaning a browser window. "Foreground window
    only" would have been simpler and would have made a second monitor useless.
    """
    try:
        matches = windows_control.match_window(query, windows_control.list_windows())
    except Exception:  # noqa: BLE001 -- "no windows" is a legitimate answer
        return None
    return matches[0].handle if matches else None


async def _step_desktop_act(ctx: StepContext) -> StepResult:
    """Act on something the observe step found.

    Every refusal `desktop_control` makes is made here too, and that is the
    point of routing through the tool's own helpers rather than re-deciding: the
    integrated-window check, the password check and the generation check would
    otherwise exist twice and drift, and a plan that could click a browser is
    exactly the bypass the whole design exists to prevent.

    The class comes from the element, as it does in the tool. A plan cannot
    claim a lighter class than the call site would enforce -- that is what
    ``STEP_ACTION_CLASS`` below is for.
    """
    verb = str(ctx.step.args.get("action") or "").strip().lower()
    row = desktop_ops.operation(verb)
    if row is None:
        return StepResult(
            verification.failed("desktop_act", f"there is no desktop action called '{verb}'")
        )

    generation = ctx.output("generation")
    if generation in (None, ""):
        return StepResult(
            verification.failed(
                "desktop_act", "I hadn't looked at the screen yet, so there was nothing to act on"
            )
        )
    try:
        observation = desktop_capture.check_generation(generation)
    except ToolError as exc:
        return StepResult(verification.failed("desktop_act", str(exc)))

    # A plan's act gets the same freshness a single tool call gets, and for the
    # same reason: the *tree* has just been re-read above, but the picture the
    # model chose this target from is whatever it was when the step was
    # written. `reason="step"` rather than `"action"` so the delivery record can
    # tell a planned act from an interactive one -- they have different honest
    # latencies, and a task engine tolerates more of it than a person does.
    # Not awaited: the refusal that matters is `check_generation`'s, above.
    need_picture(ctx, reason="step")

    target = str(ctx.step.args.get("target") or "").strip()
    element = None
    if target.isdigit():
        element = observation.by_ref(int(target))
        if element is None:
            return StepResult(
                verification.failed(
                    "desktop_act", f"there was nothing numbered {clip(target, 8)} on that screen"
                )
            )
    elif target:
        matches = desktop_capture.find_by_name(observation, target)
        if not matches:
            return StepResult(
                verification.failed("desktop_act", f"nothing on the screen says '{clip(target, 30)}'")
            )
        element = matches[0]

    action_class = desktop_ops.classify(verb, element) if element is not None else "sensitive"
    text = str(ctx.step.args.get("text") or "")
    await ctx.approve(
        tool="run_task",
        action=verb,
        action_class=action_class,
        summary=f"Click {clip(element.describe(), 40)}"
        if element is not None
        else f"Click a point at {ctx.step.args.get('x')}, {ctx.step.args.get('y')}",
        target=clip(element.describe() if element is not None else "the screen", 60),
    )

    engine = desktop_control.desktop().input or desktop_input
    try:
        if verb == "click" and element is not None:
            await asyncio.to_thread(engine.click, element.center)
        elif verb in ("double_click", "right_click") and element is not None:
            await asyncio.to_thread(
                engine.click,
                element.center,
                button="right" if verb == "right_click" else "left",
                count=2 if verb == "double_click" else 1,
            )
        elif verb == "type" and text:
            await asyncio.to_thread(engine.type_text, text)
        elif verb == "press" and text:
            await asyncio.to_thread(engine.press_key, text)
        elif verb == "scroll":
            await asyncio.to_thread(
                engine.scroll, amount=int(ctx.step.args.get("amount") or 3)
            )
        else:
            return StepResult(
                verification.failed(
                    "desktop_act", f"that action needs a control I could find, and I could not"
                )
            )
    except ToolError as exc:
        return StepResult(verification.failed(verb, str(exc)))
    except Exception as exc:  # noqa: BLE001
        return StepResult(verification.failed(verb, f"that did not work: {clip(str(exc), 80)}"))

    return StepResult(
        verification.dispatched(
            verb,
            f"clicked {clip(element.describe(), 40)}" if element is not None else verb,
            note="Windows accepted it; I did not read the application afterwards to confirm.",
        )
    )


#: The verbs a plan may name. A step whose implementation is missing fails
#: validation rather than at run time, which is the difference between "that plan
#: cannot run" and "the third step failed".
STEP_IMPLEMENTATIONS: dict[str, StepImpl] = {
    "open_app": _step_open_app,
    "focus_window": _step_focus_window,
    "search_web": _step_search_web,
    "open_page": _step_browser_open,
    "find_element": _step_browser_find,
    "browser_act": _step_browser_act,
    "browser_search": _step_browser_search,
    "desktop_observe": _step_desktop_observe,
    "desktop_act": _step_desktop_act,
    "ask": _step_ask,
}

#: What each verb's default action class is, for plan validation. Matches the
#: per-call-site class the tool itself declares, so a plan cannot claim a lower
#: class than the tool would enforce.
#:
#: `desktop_act` is `sensitive`, not `act`, and the reason is that its class is
#: computed from the *element* rather than fixed -- so any plan naming it has to
#: be treated as capable of clicking something that submits. Declaring the mild
#: class here would let a plan describe a Delete click as an ordinary one.
STEP_ACTION_CLASS: dict[str, str] = {
    "open_app": "act",
    "focus_window": "act",
    "search_web": "read",
    "open_page": "read",
    "find_element": "read",
    "browser_act": "act",
    "browser_search": "act",
    "desktop_observe": "read",
    "desktop_act": "sensitive",
    "ask": "read",
}

#: Steps that may be retried. Only reads: a launch may have produced a window, a
#: search may have navigated, and a second one is a duplicate.
_RETRYABLE = {"search_web", "find_element", "desktop_observe"}


#: What a user said they wanted clicked, stripped of the verb that asked.
#:
#: `"click the save button"` -> `"save"`. A plan cannot invent a target the
#: request did not contain: the act step matches it against the controls the
#: observe really found, and a target that matches nothing fails as a named
#: step rather than clicking whatever happens to be first.
_CLICK_TARGET = re.compile(
    r"\b(?:click|press|tap|hit|choose|select|push)\s+(?:on\s+)?(?:the\s+)?"
    r"(?:button\s+|link\s+|tab\s+|option\s+|menu\s+)?[\"']?(?P<target>[^\"'.,?!]+)",
    re.IGNORECASE,
)


def _requested_target(request: str) -> str:
    """The control the user named, or ``""`` for the model to resolve."""
    match = _CLICK_TARGET.search(str(request or ""))
    if not match:
        return ""
    return match.group("target").strip()


def _query_from_request(request: str) -> str:
    """The lookup query, when the caller did not name one.

    Taken as the request with the leading verb removed, which is enough for
    "find the current price of white t-shirts" and honest enough for the rest:
    the search engine handles the rest.
    """
    text = str(request or "").strip()
    text = re.sub(
        r"^\s*(?:please\s+)?(?:can you\s+)?(?:lumine,?\s*)?"
        r"(?:search (?:the web )?for|look up|find me|find|search for|search)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.rstrip("?.! ").strip() or str(request or "").strip()


# -- planning ------------------------------------------------------------------


@dataclass
class Plan:
    """A built plan, before it is turned into a task."""

    steps: list[task_model.Step] = field(default_factory=list)
    note: str = ""

    def __len__(self) -> int:
        return len(self.steps)


def _step(
    verb: str,
    description: str,
    args: Mapping[str, Any] | None = None,
    *,
    depends_on: tuple[str, ...] = (),
) -> task_model.Step:
    return task_model.build_step(
        description,
        verb,
        dict(args or {}),
        depends_on=depends_on,
        retry=task_model.READ_RETRY if verb in _RETRYABLE else task_model.NO_RETRY,
        action_class=STEP_ACTION_CLASS.get(verb, "sensitive"),
        timeout_seconds=STEP_BUDGET_SECONDS,
    )


def plan_for(route: router.Route, request: str, *, browser_open: bool = False) -> Plan:
    """Build the steps for a route. Deterministic, and the only place plans are made.

    Each branch is a *named scenario* from the requirements rather than a general
    system, and that is a deliberate limit: the failure mode of a general planner
    is a plan nobody can read, and the scenarios cover what people actually ask
    for. An unmatched route produces no steps and a note, which the caller turns
    into a question rather than a guess.
    """
    environment = route.environment

    if environment == router.MESSAGING:
        # Deliberately no steps, for either direction. Messaging has its own
        # tools, and those tools own the approval, the consent grant, the ledger
        # and the verification rules -- routing a send *or a read* through here
        # would mean two places that decide whether the user's messages are read
        # or something leaves the account, and one of them would be the less
        # careful one. Which of the two tools is named is the router's decision,
        # so this note hands back exactly the tool it routed to.
        if route.tool == "whatsapp_read":
            return Plan(note="use whatsapp_read: it owns the history consent")
        return Plan(note="use send_whatsapp: it owns the approval and the send ledger")

    if environment == router.BACKGROUND:
        return Plan(steps=[_step("search_web", "Look it up", {"query": _query_from_request(request)})])

    if environment == router.BROWSER:
        return _plan_browser(route, request, browser_open=browser_open)

    if environment == router.DESKTOP:
        # A screen the user is already looking at is a different shape from a
        # program to launch, and it is handled before the launch branch: these
        # two steps *depend* on each other in a way a launch never does, because
        # the act needs the reference the observe produced. Two dependent steps
        # is exactly what `run_task` is for, and it is why a desktop
        # interaction goes here rather than through a single tool call.
        if route.tool == "desktop_control":
            observed = _step("desktop_observe", "Look at the screen", {})
            # `depends_on` rather than "beside it": an act with no observation
            # behind it has no generation to check against, and would be refused
            # by the freshness guard -- which is the correct refusal, just
            # cheaper to never produce.
            return Plan(
                steps=[
                    observed,
                    _step(
                        "desktop_act",
                        "Act on it",
                        {
                            "action": "click",
                            "target": _requested_target(request),
                            "text": "",
                        },
                        depends_on=(observed.id,),
                    ),
                ],
                note="the act is verified against the generation the observe produced",
            )

        # A name the router could not pin down is *not* grounds for launching
        # whatever the first two content words happen to match. Asking costs one
        # short question; a wrong launch is a window the user has to close.
        target = route.target
        if not target:
            return Plan(note="which application?")
        opened = _step("open_app", "Open the application", {"app_name": target})
        # The focus step depends on the launch rather than running beside it: a
        # focus aimed at a window that does not exist yet is a refusal, and
        # running it first would make "open Krita" fail on a cold machine.
        return Plan(
            steps=[
                opened,
                _step(
                    "focus_window",
                    "Bring its window to the front",
                    {"window": target},
                    depends_on=(opened.id,),
                ),
            ],
            note="the launch already verified a window; the focus step is the visible half",
        )

    return Plan(note="nothing to do for that")


def _plan_browser(route: router.Route, request: str, *, browser_open: bool = False) -> Plan:
    """Browser plans, by what the request is asking to do there.

    Four shapes, in the order they are tried:

    1. **Search here or there.** "search for X on <site>" is one step that types
       and submits, because splitting those is how a search used to end with the
       text typed and nothing run.
    2. **Reach a page.** Open it, then act on a control there.
    3. **Act on the page already open.** "click the second video" names no
       address, and answering that with "no address was named" was the single
       reason the reported "she cannot click anything" happened: the planner had
       no shape for "use what is in front of her".
    4. Nothing else, which is reported rather than guessed at.
    """
    url = route.url or ""
    steps: list[task_model.Step] = []
    depends: tuple[str, ...] = ()

    if url:
        opened = _step("open_page", "Open the page", {"url": url})
        steps.append(opened)
        depends = (opened.id,)

    query = _search_query(request)
    if query:
        # Depends on the open when there was one, and on nothing otherwise --
        # "search for X" while a page is already open is a legitimate request.
        steps.append(
            _step("browser_search", "Search this page", {"query": query}, depends_on=depends)
        )
        return Plan(steps=steps)

    if not url and not browser_open:
        return Plan(note="no address was named and no page is open")

    target = _control_target(request)
    if not target:
        return Plan(steps=steps, note="opened the page" if url else "")

    found = _step("find_element", "Find the control", target, depends_on=depends)
    steps.append(found)
    if re.search(r"\b(?:find|locate)\b", request, flags=re.IGNORECASE) and not re.search(
        r"\b(?:open|click|press|choose|select|tap)\b",
        request,
        flags=re.IGNORECASE,
    ):
        return Plan(steps=steps, note="found the requested control on the current page")
    steps.append(
        _step(
            "browser_act",
            "Use it",
            {"verb": _act_verb(request), "text": _act_text(request)},
            depends_on=(found.id,),
        )
    )
    return Plan(steps=steps)


def _search_query(request: str) -> str:
    """The text a request wants searched for, or ``""``.

    Only for a request that is *about* a page -- one that names a site, or says
    "search" while a browser is open. "Find the price of white T-shirts" with no
    site is a lookup and never reaches here: it routes to ``search_web`` before
    the planner is consulted.
    """
    text = str(request or "")
    match = re.search(
        r"\b(?:search(?:\s+for)?|look\s+up|look\s+for|find)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    value = match.group(1).strip(" .?!\"'")
    explicit_search = re.search(
        r"\b(?:search\s+for|search\s+(?!box\b|field\b|bar\b|button\b|control\b|input\b)|look\s+up)\b",
        text,
        flags=re.IGNORECASE,
    )
    # "Find the search box" is a control lookup, while "search for a box" is a
    # submitted query. When the target parser resolves a control, keep it on-page.
    if _control_target(text) and not explicit_search:
        return ""
    # "search result(s), titled ..." refers to content already on the page, not
    # a query to submit into its search box.
    if re.match(
        r"^(?:the\s+)?results?(?:\s*[,;:]|\s+(?:titled|called|named)\b)",
        value,
        flags=re.IGNORECASE,
    ):
        return ""
    # A trailing "on <site>" belongs to the *route*, not to the query. Leaving it
    # in would search YouTube for "a song on youtube".
    value = re.sub(
        r"\s+(?:on|in|at|via|through)\s+(?:youtube|google|maps|amazon|reddit|github|wikipedia|twitter|x|duckduckgo)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip(" .?!\"'")
    return clip(value, 200)


def _act_verb(request: str) -> str:
    text = str(request or "").casefold()
    if "click" in text:
        return "click"
    if "type" in text or "fill" in text or "enter" in text:
        return "fill"
    if "choose" in text or "select" in text:
        return "select"
    return "click"


def _act_text(request: str) -> str:
    """The text a search request wants typed.

    A crude but honest extraction: everything after "search for" / "search" /
    "type" / "enter". It is a *default*, and the model overrides it by naming the
    control itself -- which is why the tool exposes both.
    """
    text = str(request or "")
    match = re.search(
        r"\b(?:search(?: the web)? for|search|find|look for|type|enter|click)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    value = match.group(1).strip(" .?!\"'")
    # "search for a song" -- the leading article is English filler, not part of
    # the query. Stripping one article is safer than stripping a real word.
    value = re.sub(r"^(?:a|an|the)\s+", "", value, flags=re.IGNORECASE)
    return clip(value, 200)


def _control_target(request: str) -> dict[str, Any]:
    """What control a browser request is aiming at, as arguments.

    Three shapes, all of them things a person actually says:

    * an **ordinal** -- "click the second video", "open the third result". A role
      plus a position, with no name, because the page has not shown the names yet
      and asking the model to invent one is how the wrong thing gets clicked.
    * a **quoted or named thing** -- "click 'Baby Shark'", "open the Krita file
      row". Text strategy, which matches the visible label.
    * a **named control** -- "click the Subscribe button", "choose the size". A
      role plus the words the control carries.

    Anything else is no plan, which the tool turns into a question. A target it
    guessed at would be worse than a question: every one of these guesses decides
    what happens on somebody's screen.
    """
    text = str(request or "").strip()
    lowered = text.casefold()

    quoted = re.search(r"[\"“”']([^\"“”']{2,80})[\"“”']", text)
    if quoted:
        return {"strategy": "text", "name": clip(quoted.group(1).strip(), 80)}

    titled = re.search(
        r"\b(?:title(?:d)?(?:\s+is)?|named|called)\s*[:,-]?\s*(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if titled:
        name = titled.group(1).strip(" .?!\"'“”")
        if name:
            return {"strategy": "text", "name": clip(name, 80)}

    ordinal = _ordinal_words(lowered)
    if ordinal:
        # "video", "result", "link", "item", "row" are what people call the
        # things in a results column. A link is the honest role: a video result
        # on every site this product drives is a link.
        noun = next(
            (word for word in ("video", "result", "link", "row", "item", "one") if word in lowered),
            None,
        )
        if noun:
            return {"role": "link", "position": ordinal}

    named = re.search(
        r"\b(?:click|press|choose|select|open|tap|find)\s+(?:on\s+)?(?:the\s+)?([A-Za-z][\w '-]{1,40}?)\s*(?:button|link|field|box|tab|option|menu)\b",
        text,
        flags=re.IGNORECASE,
    )
    if named:
        role = "button"
        if "link" in lowered:
            role = "link"
        elif "field" in lowered or "box" in lowered:
            role = "textbox"
        elif "tab" in lowered:
            role = "tab"
        elif "option" in lowered:
            role = "option"
        return {"role": role, "name": clip(named.group(1).strip(), 40)}

    return {}


def _ordinal_words(text: str) -> str:
    """``"second"`` from "click the second video", or ``""``."""
    for word in ("first", "second", "third", "fourth", "fifth", "last"):
        if re.search(rf"\b{word}\b", text):
            return word
    return ""


# -- the registry --------------------------------------------------------------

#: Tasks in flight, per session. Held so a cancellation has something to act on
#: and so a cancelled task cannot be resumed by a later turn.
_TASKS: dict[int, "LiveTask"] = {}


@dataclass
class LiveTask:
    """One task as it is running."""

    task: task_model.Task
    started_at: float = field(default_factory=time.monotonic)
    context: Any = None
    outputs: dict[str, Any] = field(default_factory=dict)
    narration: list[str] = field(default_factory=list)


def register(session: Any, live: LiveTask) -> None:
    if session is None:
        return
    try:
        _TASKS[id(session)] = live
    except Exception:  # noqa: BLE001 - a non-hashable stub must not break a task
        pass


def for_session(session: Any) -> LiveTask | None:
    return _TASKS.get(id(session)) if session is not None else None


def release(session: Any) -> None:
    """Drop a session's tasks, cancelling anything still running."""
    live = _TASKS.pop(id(session), None) if session is not None else None
    if live is not None:
        live.task.cancel("session released")


def cancel_active(session: Any, reason: str = "cancelled by the user") -> bool:
    live = for_session(session)
    if live is None:
        return False
    live.task.cancel(reason)
    return True


# -- execution -----------------------------------------------------------------


async def _run_step_impl(step: task_model.Step, ctx_factory: Callable[[task_model.Step], StepContext]) -> verification.ActionOutcome:
    implementation = STEP_IMPLEMENTATIONS.get(step.tool)
    if implementation is None:
        return verification.failed(step.tool, f"I don't know how to {step.tool}")
    result = await implementation(ctx_factory(step))
    return result.outcome


async def execute(
    task: task_model.Task,
    *,
    approve: Callable[..., Awaitable[None]],
    request: str,
    route: router.Route,
    session: Any = None,
    narration: list[str] | None = None,
) -> task_model.Task:
    """Run a validated task to completion, honouring the budget.

    ``approve`` is the permission call. Injected so the executor has no opinion
    about *who* is allowed to approve -- the gate already knows, and the tool only
    has to reach it.
    """
    outputs: dict[str, Any] = {}
    updates: dict[str, Any] = {}

    def context_for(step: task_model.Step) -> StepContext:
        # `outputs` is read live by the step that runs, so a step sees the values
        # its dependencies produced. The dict object is shared, not copied: a copy
        # would be a snapshot from before the dependency ran, which is exactly the
        # staleness this design is trying to avoid.
        return StepContext(
            step=step,
            route=route,
            request=request,
            outputs=outputs,
            approve=approve,
            session=session,
        )

    async def runner(step: task_model.Step) -> verification.ActionOutcome:
        if task.cancelled:
            return verification.failed(step.tool, task.cancel_reason or "cancelled")
        try:
            result = await asyncio.wait_for(
                _run_step_impl(step, context_for), timeout=step.timeout_seconds
            )
        except asyncio.TimeoutError:
            # The step may or may not have acted. Timed out is *not* success and
            # is not clean failure either, which is why it is its own status.
            return verification.timed_out(
                step.tool, f"{step.tool} took too long and I stopped waiting"
            )
        if isinstance(result, StepResult):
            outputs.update(result.outputs)
            updates[step.id] = result.outputs
            return result.outcome
        return result

    observer = _observer(narration, session)
    engine = task_engine.TaskEngine(runner, observer=observer)

    live = LiveTask(task=task, context=session, outputs=outputs)
    register(session, live)
    try:
        # Synchronous `measure`, not an `async with`: the tracker's span wraps a
        # coroutine call, and entering it synchronously records the whole task as
        # one duration rather than the event loop's own scheduling latency.
        with latency_measure(session, "task_execute", phase="tool", tool="run_task"):
            await engine.run(task)
    finally:
        if for_session(session) is live:
            _TASKS.pop(id(session), None)
    return task


def _observer(narration: list[str] | None, session: Any = None):
    """Collect a handful of progress lines, and publish every step as it moves.

    Brief on the narration side, because this is a voice turn: only steps that
    *acted* are narrated, and only the first few -- a person listening to a
    conversation does not want a running commentary on every verification read,
    and the tool result carries the detail for anyone who looks.

    The publish side is not filtered like that. The desktop app draws a step
    ledger on Lumine's stage while a task runs, and a ledger that showed three
    of its six rows would be a list the reader cannot trust -- so every step is
    sent, read steps included, with its position and the task it belongs to.
    `session` is what reaches `progress.publish_step`; with none registered
    there the publish is a no-op, which is the same guarantee every other tool's
    steps have.
    """

    async def observe(event: str, task: task_model.Task, step: task_model.Step | None) -> None:
        if step is not None:
            _publish_task_step(session, task, step, event)
        if narration is None or step is None:
            return
        if event == "task_started":
            narration.append(f"Starting: {len(task.steps)} step(s)")
        elif event == "step_started" and step.action_class != "read":
            narration.append(step.description or step.tool)
        elif event == "step_succeeded" and len(narration) < 6:
            detail = step.outcome.detail if step.outcome else ""
            if detail and step.action_class != "read":
                narration.append(detail)
        elif event == "step_failed" and step.error:
            narration.append(f"{step.description or step.tool}: {step.error}")

    return observe


#: Engine event -> the status the app's ledger understands.
#:
#: The engine's vocabulary is its own (`succeeded`, `cancelled`, `retry`) and
#: the wire's is `progress`'s -- `started` / `done` / `failed`, the same three a
#: tool call's steps already use, so one renderer reads a task's steps and a
#: WhatsApp send's steps without a second mapping. `step_retry` maps to
#: `started` rather than to its own status because a retry *is* the step running
#: again; giving it a fourth status would draw a row nothing else produces.
_TASK_STEP_STATUS: dict[str, str] = {
    "step_started": "started",
    "step_retry": "started",
    "step_succeeded": "done",
    "step_failed": "failed",
    "step_cancelled": "failed",
}


def _publish_task_step(
    session: Any, task: task_model.Task, step: task_model.Step, event: str
) -> None:
    """One step's movement, as a `tool_status` record on `lumine.tool`.

    Never raises: this is called from inside the engine's observer, and a
    telemetry line that could end a task is worse than no telemetry -- which is
    the argument `progress` itself is built on, and `_notify` wraps this call
    besides.

    Three fields are what make the ledger possible rather than merely
    possible-looking:

    - ``action`` is ``step<n>``, 1-based *by identity* against ``task.steps``.
      Steps are concurrent for reads, so the order records arrive in is not the
      order they were planned in, and a counter maintained here would credit the
      wrong row. The position is a fact about the plan; it is looked up, not
      counted.
    - ``total`` is the plan's whole length, so the app can draw the steps that
      have not started yet as pending instead of growing the list under the
      reader one row at a time.
    - ``task`` is the task's id, which is what groups the rows into one block
      and ties them to the `run_task` call bracketing the whole run.

    ``duration_ms`` is passed only once the step has really ended -- ``0.0`` is
    falsy and becomes ``None``, so a running step never claims to have taken no
    time, which would read as "instant" rather than as "not yet".
    """
    status = _TASK_STEP_STATUS.get(event)
    if status is None:
        # `task_started` / `task_finished` carry no step, so this is only ever
        # an event the table above deliberately does not draw.
        return
    index = 0
    for position, item in enumerate(task.steps, start=1):
        if item is step or item.id == step.id:
            index = position
            break
    if not index:
        # A step from some other plan. Drawing it without a position would put
        # an unnumbered row in a numbered list, so it is dropped instead.
        return
    label = step.description or step.tool
    if event == "step_retry":
        message = f"{label} -- retrying"
    elif event == "step_cancelled":
        message = f"{label} -- cancelled"
    elif event == "step_failed" and step.error:
        message = f"{label}: {step.error}"
    else:
        message = label
    progress.publish_step(
        session,
        tool=TASK_TOOL,
        action=f"step{index}",
        message=message,
        index=index,
        total=len(task.steps),
        status=status,
        duration_ms=step.duration_ms or None,
        task=task.id,
    )


# -- reporting -----------------------------------------------------------------


def _emit_latency(session: Any, stage: str, duration_ms: float, **details: Any) -> None:
    """Publish one measured phase of a task. Never raises.

    The phases are the ones a user can feel and cannot otherwise see: deciding
    where to work, resolving an application, and launching one. Routing in
    particular is invisible work -- tens of milliseconds spent reading a Start
    Menu and a window list -- and without a measurement it looks like a stall.
    """
    try:
        latency_module.emit_duration(session, stage, duration_ms, phase="tool", **details)
    except Exception:  # noqa: BLE001 - telemetry must never break a task
        logger.debug("[Task] latency emission failed for %s", stage, exc_info=True)


async def measure_route(session: Any, brief: str) -> tuple[router.Route, router.MachineFacts, float]:
    """Read the machine's facts and classify the request, timed.

    Returned as a triple rather than just the route because the cold and warm
    numbers mean different things: the first call on a worker pays for the app
    index, every later one does not, and averaging the two hides both.
    """
    started = time.monotonic()
    facts = await machine_facts()
    facts_ms = (time.monotonic() - started) * 1000
    route = router.classify(brief, facts, tools=set(tools_permission.enabled_tool_ids()))
    route = prefer_open_browser_for_control(route, facts, brief)
    total_ms = (time.monotonic() - started) * 1000
    _emit_latency(
        session,
        "route_classify",
        total_ms,
        environment=route.environment,
        ambiguous=route.ambiguous,
        facts_ms=round(facts_ms, 1),
    )
    return route, facts, total_ms


def prefer_open_browser_for_control(
    route: router.Route, facts: router.MachineFacts, request: str
) -> router.Route:
    """Keep a page-target request in the browser when its title trips window routing."""
    if (
        route.environment != router.DESKTOP
        or route.reason != "asked to arrange or close a window"
        or not facts.browser_open
        or not _control_target(request)
        or not re.search(r"\b(?:open|click|select|choose|tap|play|watch)\b", request, re.IGNORECASE)
        or re.search(
            r"\b(?:on|in)\s+(?:my|the|our)\s+(?:desktop|computer|laptop|pc|mac|machine)\b",
            request,
            flags=re.IGNORECASE,
        )
    ):
        return route

    enabled = set(tools_permission.enabled_tool_ids())
    browser_tool = next((tool for tool in ("browse", "web_control") if tool in enabled), "")
    if not browser_tool:
        return route

    return router.Route(
        router.BROWSER,
        tool=browser_tool,
        reason="the request targets a control on the page already open",
        alternatives=(router.DESKTOP,),
        notes={"reused_open_page": True},
    )


# -- reporting -----------------------------------------------------------------


def report(task: task_model.Task, route: router.Route) -> str:
    """The model-facing result. Says what happened, including the uncertain part."""
    counts: dict[str, int] = {}
    for step in task.steps:
        counts[step.status] = counts.get(step.status, 0) + 1
    payload: dict[str, Any] = {
        "status": task.status,
        "route": route.environment,
        "why": clip(route.reason, 80),
        "steps": [
            {
                "tool": step.tool,
                "status": step.status,
                "verified": step.outcome.status if step.outcome else "requested",
                **({"note": clip(step.error or step.outcome.detail, 80)} if (step.error or (step.outcome and step.outcome.detail)) else {}),
                **(
                    {
                        "results": step.outcome.data.get("results", []),
                        **(
                            {"results_error": clip(step.outcome.data["results_error"], 100)}
                            if step.outcome.data.get("results_error")
                            else {}
                        ),
                    }
                    if step.tool == "browser_search" and step.outcome is not None
                    else {}
                ),
            }
            for step in task.steps
        ],
    }
    uncertain = [
        step for step in task.steps
        if step.outcome is not None and verification.may_have_happened(step.outcome.status)
    ]
    if uncertain:
        payload["uncertain"] = [
            {"tool": step.tool, "detail": clip(step.outcome.detail or "", 80)}  # type: ignore[union-attr]
            for step in uncertain
        ]
    return tool_results.wrap_untrusted(tool_results.as_payload(payload))


# -- the tool ------------------------------------------------------------------


@function_tool()
async def run_task(context: RunContext, request: str, goal: str = "") -> str:
    """Carry out a multi-step request on this computer, and check each step.

    Use this for anything with **two or more dependent steps**, and for anything
    where picking the wrong environment would be visible to the user: opening a
    program and doing something in it, browsing to a page and using a control,
    finding a contact and doing something with the chat.

    Do NOT use it for a single question that `search_web`, `get_weather`, or
    `get_news` answers -- this is slower and heavier than a plain lookup, and one
    of those tools is the right tool.

    It decides for itself where each step happens: a background lookup, the
    isolated browser, or a desktop application. Naming the environment is respected
    -- "in your browser" means the isolated browser, "open Chrome" means the
    user's own browser, and neither ever starts the other. When a request is
    genuinely ambiguous it asks one short question rather than opening two
    things.

    It reports what it verified. A step that could not be confirmed is reported as
    uncertain rather than as done, and it never repeats an action that may already
    have taken effect.

    Args:
        request: What the user asked for, in their words. Used as the whole
            brief -- names of apps, sites, contacts and controls are taken from it.
        goal: Optional, a narrower statement of the same request. Leave it empty
            unless the request itself is long.
    """
    brief = " ".join(str(request or goal or "").split())
    if not brief:
        raise ToolError("Tell me what to do.")
    if len(brief) > 400:
        brief = brief[:400].rstrip()

    session = getattr(context, "session", None)

    async def approve(**kwargs: Any) -> None:
        await require_approval(context, **kwargs)

    # Which tools are on decides which routes exist. Passing it in is what makes
    # a disabled `open_app` mean "do not launch anything" rather than "launch it
    # anyway, the launch step happens not to use the tool".
    route, _facts, route_ms = await measure_route(session, brief)

    if route.ambiguous:
        return tool_results.wrap_untrusted(
            tool_results.as_payload(
                {
                    "status": "needs_clarification",
                    "question": clip(route.question or "Which one did you mean?", 120),
                    "route": route.environment,
                    "alternatives": list(route.alternatives),
                }
            )
        )

    plan = plan_for(route, brief, browser_open=_facts.browser_open)
    if not plan.steps:
        # No steps is one of three different situations, and the model has to be
        # able to tell them: it has a tool for the second, and needs to ask the
        # user about the first and third.
        note = plan.note
        payload: dict[str, Any] = {
            "status": "no_plan",
            "route": route.environment,
            "why": clip(route.reason, 80),
            "note": clip(note, 100),
        }
        if note == "which application?":
            payload["status"] = "needs_clarification"
            payload["question"] = "Which application should I open?"
        elif "send_whatsapp" in note:
            payload["hint"] = "use send_whatsapp"
        elif "whatsapp_read" in note:
            # A read is a single call too, and routing a "did she reply?" through
            # the orchestrator would spend a plan and a tool budget on a question
            # one read answers.
            payload["hint"] = "use whatsapp_read"
        else:
            payload["hint"] = "use a single-purpose tool instead"
        return tool_results.wrap_untrusted(tool_results.as_payload(payload))
    if len(plan.steps) > MAX_STEPS:
        plan.steps = plan.steps[:MAX_STEPS]

    task = task_model.make_task(brief, plan.steps, environment=route.environment)
    narration: list[str] = []
    await asyncio.wait_for(
        execute(task, approve=approve, request=brief, route=route, session=session, narration=narration),
        timeout=TASK_BUDGET_SECONDS,
    )
    return report(task, route)


@function_tool()
async def cancel_task(context: RunContext) -> str:
    """Stop whatever Lumine is currently doing on the computer.

    Use this when the user says to stop, cancel, or "never mind" while a task is
    running. It cancels the task and every step it had not finished, and it does
    not undo anything already done -- it stops the rest.
    """
    session = getattr(context, "session", None)
    live = for_session(session)
    if live is None:
        return "There was nothing running to stop."
    live.task.cancel("cancelled by the user")
    return f"Stopped. Nothing further will run for: {clip(live.task.request, 60)}"