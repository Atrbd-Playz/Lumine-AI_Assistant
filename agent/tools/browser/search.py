"""Searching a page, by typing into the page's own search box.

One implementation, two callers. `task_runner`'s ``browser_search`` step was
already doing this correctly and was unreachable from a conversation; this module
is that code moved, so the tool verb and the task step cannot drift into being
two different searches.

## Why the search runs through the UI

`sites.py` holds a hand-written selector per site, and :func:`find_search_field`
prefers it over a heuristic. No entry constructs a search URL: the point is that
the results are what a person would have got, from the same box, with the same
suggestions and the same ranking.

## Why typing and submitting are one step

They were two, once. The second could be lost, skipped by a re-plan, or never
planned -- so Lumine typed a search into YouTube and stopped, which the user
experiences as "she cannot search". A single :func:`run_search` types, commits,
submits and checks, in that order.

## What "it worked" means

`run_search` returns a :class:`runtime.verification.ActionOutcome`, and the
distinction it preserves is the whole reason it does not return a bool. A query
that was typed and submitted but left the address bar alone is ``accepted`` --
the page may well be showing results by now -- and never ``observed``. Only a
page whose URL actually moved to a results view earns that, because a single-page
app that swapped its content without touching the address is indistinguishable
from one that silently failed, and only one of those is a success.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

try:
    from ...runtime import verification
    from ...runtime.progress import step as progress_step
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime import verification  # type: ignore[no-redef]
    from runtime.progress import step as progress_step  # type: ignore[no-redef]

# Module level on purpose, and specifically *not* a function-level import: the
# `try: from .x` / `except: from x` pair has to be the shape production runs,
# because `python agent/agent.py` loads these as top-level modules with `agent/`
# on sys.path. A relative import inside a function body is invisible to the rest
# of the suite and crashes the first real session -- `test_import_shapes` exists
# for exactly that. `engine` does not import this module, so there is no cycle.
try:
    from . import locators, sites, state
    from .actions import hostname_of
    from .engine import EngineAction
except ImportError:  # running as a top-level script with agent/ on sys.path
    from browser import locators, sites, state  # type: ignore[no-redef]
    from browser.actions import hostname_of  # type: ignore[no-redef]
    from browser.engine import EngineAction  # type: ignore[no-redef]

logger = logging.getLogger("lumine")

#: How long a search may take to go somewhere. Long enough for a real navigation
#: on a slow connection, short enough that a search which silently failed does not
#: hold a voice turn for a minute.
SEARCH_SETTLE_SECONDS = 8.0

#: How often the address bar is re-read while waiting. Frequent enough that a
#: fast page is noticed at once, sparse enough not to spin.
POLL_SECONDS = 0.4

#: The longest query we will type. Long enough for anything a person would say,
#: short enough that a runaway generation cannot fill a text field for a minute.
MAX_QUERY_CHARS = 200

#: How many steps a search narrates. Declared rather than counted at the call
#: sites so "step 3 of 3" cannot drift out of step with the number of steps
#: actually published -- the same reason `whatsapp.SEND_STEPS` exists.
SEARCH_STEPS = 3

#: The class every search is charged at.
#:
#: ``act`` and never ``read``: the query does go to somebody's server, so this
#: is not a pretence that nothing left the machine. ``act`` rather than
#: ``sensitive`` because submitting a search form sends a query and nothing the
#: user owns -- it is the same shape as the click that navigates a page, which
#: this tool has always treated as an ordinary act.
#:
#: Named rather than derived from a control's facts, because *being* the search
#: field is what :func:`find_search_field` just established: it is either the
#: site's own hand-written selector or a control whose label says "search". Both
#: are stronger evidence than any DOM fact gathered afterwards, and neither
#: depends on whether we happen to recognise the host.
SEARCH_ACTION_CLASS = "act"


@dataclass
class SearchField:
    """A search control on the page, and the query that finds it again.

    ``query`` is what the engine re-resolves at the moment it acts; ``expected``
    is the identity it had when it was read, so a page that swapped the element
    in between gets a refusal rather than a keystroke into whatever is there now.

    ``tag`` and ``in_form`` are the DOM facts the permission class is computed
    from. They are captured here because a site's CSS selector path finds the
    element without a candidate to read them off -- and "we typed into a field we
    cannot classify" has to mean ``sensitive``, which is the direction that errs
    toward asking rather than away from it.
    """

    query: locators.ElementQuery
    expected: dict[str, str] = field(default_factory=dict)
    frame: str = ""
    tag: str = "input"
    in_form: bool = False


def frame_spec(found: SearchField) -> str:
    """The frame the field was found in, or ``""`` for the top document."""
    return found.frame


def normalize_query(raw: object) -> str:
    """The text to search for, clipped and trimmed.

    An empty query is the caller's problem to report as a sentence: this returns
    ``""`` rather than raising, so the same shape handles "nothing was asked for"
    and "the user said something very long".
    """
    text = " ".join(str(raw or "").split())
    return text[:MAX_QUERY_CHARS]


def result_items(snapshot: state.Snapshot, *, limit: int = 8) -> list[dict[str, Any]]:
    """Return visible result titles needed to choose without searching again."""
    return [
        {
            "index": item.index,
            "kind": item.kind,
            "title": item.title[:140],
            **({"price": item.price[:60]} if item.price else {}),
            **({"url": item.href[:200]} if item.href else {}),
        }
        for item in snapshot.items[: max(0, limit)]
    ]


def find_search_field(
    snapshot: state.Snapshot, payload: Any = None
) -> SearchField | None:
    """The page's search control, by the site's own selector or semantically.

    The site's selector wins because it is the one somebody wrote after looking at
    that site. The semantic fallback exists for the long tail, and it ranks an
    exact ``"search"`` label above a field that merely mentions the word, so a
    sort control called "search filters" loses to the real one.
    """
    del payload  # kept for the caller's shape; the snapshot carries everything used

    entry = sites.site_for(snapshot.url)
    if entry is not None and entry.input:
        # Trusted *as the site's field*, because whoever wrote that selector had
        # the page in front of them. It is still re-validated by name once found.
        return SearchField(query=locators.ElementQuery(css=entry.input, strategy="css"))

    best: tuple[int, locators.Candidate] | None = None
    for candidate in snapshot.candidates:
        if candidate.role not in ("searchbox", "textbox", "combobox"):
            continue
        strength = sites.looks_like_search_label(candidate.name)
        if not strength and candidate.detail.get("soleField"):
            # The shape-only signal, ranked *below* any real label so it can never
            # beat one. It exists because Google's field is `name="q"` with
            # nothing in its label that says "search" -- without it, a search on
            # the most-used search engine in the world has no field to type into.
            strength = 1
        if strength and (best is None or strength > best[0]):
            best = (strength, candidate)
    if best is None:
        return None
    _strength, candidate = best
    return SearchField(
        query=locators.build_query(role=candidate.role, name=candidate.name),
        expected={"name": candidate.name, "role": candidate.role},
        tag=candidate.tag or "input",
        in_form=bool(candidate.detail.get("inForm")),
    )


def no_field_fields(snapshot: state.Snapshot, limit: int = 6) -> list[str]:
    """What the page *did* offer, for a refusal that names something actionable.

    "I could not find a search box" is a dead end; "there is no search box here,
    but there is a field called Q" lets the model try again on its own. The limit
    keeps a page with four hundred controls from turning a refusal into a
    paragraph.
    """
    return [str(item.name)[:40] for item in snapshot.candidates[:limit] if item.name]


async def wait_for_results(
    engine_obj: Any,
    before_url: str,
    entry: "sites.SiteSearch | None" = None,
    *,
    seconds: float = SEARCH_SETTLE_SECONDS,
) -> str | None:
    """The address a search landed on, or ``None`` if it stayed put.

    Polled rather than slept once, and the wait is bounded. An SPA that swaps
    content without changing the address is deliberately **not** reported as a
    result: a search that produced nothing and one that has not navigated yet look
    identical from here, and only one of them worked.
    """
    try:
        page = await engine_obj._page()  # noqa: SLF001 - the engine's own page
    except Exception:  # noqa: BLE001 - an engine with no page cannot have searched
        return None

    def moved(url: str) -> bool:
        return sites.results_moved(url, before_url, entry)

    try:
        url = page.url
    except Exception:  # noqa: BLE001
        return None
    if moved(url):
        return url

    deadline = time.monotonic() + max(0.1, float(seconds))
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        try:
            url = page.url
        except Exception:  # noqa: BLE001 - a page that closed is not a result
            return None
        if moved(url):
            return url
    return None


async def run_search(
    engine_obj: Any,
    snapshot: state.Snapshot,
    query: str,
    *,
    approve: Any = None,
    tool: str = "web_control",
    context: Any = None,
) -> verification.ActionOutcome:
    """Type ``query`` into this page's search box, submit it, and check.

    ``approve`` is awaited, once, *after* the field has been resolved and before
    anything is typed -- so the question names the field and the host, and a
    refusal costs one page read rather than a round trip through the UI.

    ``context`` is the ``RunContext``, used only to publish progress steps. It is
    optional and a step with no publisher registered is dropped silently, so every
    existing caller keeps working without it.

    Order, and why each part is there:

    1. **Find the field.** The site's selector, then the semantic fallback.
    2. **Ask.** Nothing is asked, and that is the fix rather than an omission.
       A search is :data:`SEARCH_ACTION_CLASS` because submitting a search form
       sends a query rather than anything the user owns. It was previously
       classified from the *key*, which made an Enter inside a ``<form>`` a
       submission -- true of the mechanism, false of the intent -- and every
       tabulated site wraps its search box in a form, so "search YouTube for X"
       asked for permission to look something up. The one control on the page
       where that classification still applies is a form that is *not* a search
       form, and this verb never lands on one: :func:`find_search_field` only
       returns a page's search field.
    3. **Type, then submit.** Two engine actions, because the field's value has to
       be committed before Enter means anything.
    4. **Check.** Typed-but-stayed-put is ``accepted`` with the address it stayed
       on. Never ``observed``.

    ## Why it narrates itself

    A search is the slowest thing these tools do: filling, submitting and then
    waiting for a results view is several seconds, and :func:`wait_for_results`
    will hold for :data:`SEARCH_SETTLE_SECONDS` before giving up. On a voice turn
    that is up to eight seconds of a companion who has gone quiet, which is
    indistinguishable from a broken microphone.

    So it publishes steps, the way a WhatsApp send already does
    (``messaging/whatsapp.py``). The steps are not narrated *aloud* -- five
    sentences would cost more in latency and annoyance than the silence they
    replaced. They are drawn in the app, and the only thing still spoken is the
    approval question, which the permission gate owns.
    """
    text = normalize_query(query)
    if not text:
        return verification.failed("search", "there was nothing to search for")

    where = hostname_of(snapshot.url) or "this page"
    entry = sites.site_for(snapshot.url)
    current = sites.current_query(snapshot.url)
    if (
        entry is not None
        and sites.results_moved(snapshot.url, "", entry)
        and current
        and " ".join(current.casefold().split()) == " ".join(text.casefold().split())
    ):
        return verification.observed(
            "search",
            f"Already showing results for {text[:30]}",
            evidence="the current results URL carries the same search query",
            url=snapshot.url[:120],
            typed=False,
            reused=True,
        )

    found = find_search_field(snapshot)
    if found is None:
        return verification.failed(
            "search",
            "I could not find a search box on this page",
            fields=no_field_fields(snapshot),
            url=snapshot.url[:90],
        )

    # A search is an ordinary act. It used to be classified by the *key*, which
    # asked whenever the field sat inside a `<form>` -- and it sat inside one on
    # every tabulated site, so "search YouTube for X" interrupted the user to ask
    # permission to look something up. The class is now the same on every site
    # rather than depending on whether the host is in `sites.SITES`, which is what
    # the site-selector branch's hard-coded `in_form=False` default used to decide.
    action_class = SEARCH_ACTION_CLASS

    with progress_step(
        context,
        tool,
        "find",
        f"Looking for the search box on {where}",
        index=1,
        total=SEARCH_STEPS,
    ) as found_step:
        found_step["field"] = (found.expected.get("name") or "the search box")[:40]

    if approve is not None:
        await approve(
            action_class=action_class,
            summary=f"Search {where} for {text[:30]}",
            target=snapshot.url[:100],
        )

    entry = sites.site_for(snapshot.url)
    before_url = snapshot.url
    try:
        with progress_step(
            context, tool, "type", f"Searching {where}", index=2, total=SEARCH_STEPS
        ):
            await engine_obj.act(
                EngineAction(
                    kind="fill",
                    query=found.query,
                    text=text,
                    generation=snapshot.generation,
                    expected=found.expected,
                    frame=frame_spec(found),
                )
            )
            # Submitted as its own action so the field's own value is committed
            # first, and so a page whose search needs a button still gets Enter.
            await engine_obj.act(
                EngineAction(
                    kind="press",
                    query=found.query,
                    key="Enter",
                    generation=snapshot.generation,
                    expected=found.expected,
                    frame=frame_spec(found),
                )
            )
    except Exception as exc:  # noqa: BLE001 - a real engine refusal is a sentence
        return verification.failed("search", str(exc)[:120])

    # The wait is the longest silent stretch of a voice turn, so it gets its own
    # step rather than being hidden inside the one that typed. It also says which
    # address it is waiting on, because "loading" with no subject is the least
    # reassuring thing a progress list can contain.
    with progress_step(
        context,
        tool,
        "wait",
        "Waiting for results",
        index=3,
        total=SEARCH_STEPS,
    ) as wait_step:
        after_url = await wait_for_results(engine_obj, before_url, entry)

    if after_url is None:
        return verification.accepted(
            "search",
            f"I typed {text[:30]} but the page did not go anywhere",
            typed=True,
            url=before_url[:90],
        )
    wait_step["url"] = after_url[:120]
    return verification.observed(
        "search",
        f"Searched for {text[:30]}",
        evidence="the page moved to a results view",
        url=after_url[:120],
        typed=True,
    )

