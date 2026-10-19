"""How to search on the sites people actually ask to be searched on.

## Why this is a table and not a heuristic

"Search for a song on YouTube" and "Search for a price on Amazon" are the same
request shape, and every one of those sites spells its search box differently:
YouTube's is a `name="search_query"` input inside a `<form>` with a `button`
that has no text; Google's is an input whose accessible name changes between
locale, country and experiment; Maps' is inside a `role="combobox"` that is not a
combobox in the HTML sense.

A heuristic that guesses which is which fails on the site it has not seen. A
small explicit table fails *loudly* on one it has not seen, which is the better
failure: the tool says "I could not find a search box on this page" and the model
can name the field itself with `web_control`, rather than a search silently going
into a filter box that was never a search box.

## What every entry provides

* ``input`` -- a selector for the field, tried first.
* ``submit`` -- a selector for the control that runs the search, tried second.
* ``results`` -- a substring or host fragment the *results* URL is known to
  carry, used only to confirm the search actually went somewhere.
* ``query_param`` -- the URL parameter that identifies a search already showing,
  so an identical request can reuse the live results page.
* ``fields`` -- the ``name`` attributes of the site's search field, used by
  :func:`is_named_search_field` when a site's markup is too plain for structure
  to say that submitting it is a search rather than a submission.

No entry constructs a search URL. The whole point is that the search runs through
the page's own UI, so what comes back is what a person would have got.

## The fallback

:func:`search_candidates` returns, in order: the site's entry, then the semantic
fallback (a search box, then a field whose label mentions searching), then
nothing. A caller gets a list and decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

#: Markers in a control's label that mean "this is where you search". Ordered by
#: strength: an exact match is a search box, and a word inside a longer label is
#: a field that might be one.
SEARCH_LABEL_WORDS: tuple[str, ...] = (
    "search", "find", "query", "look for", "what are you looking for",
)


@dataclass(frozen=True)
class SiteSearch:
    """How one site is searched, and how to tell it worked."""

    name: str
    hosts: tuple[str, ...]
    input: str = ""
    submit: str = ""
    #: A fragment the results URL is known to contain. Empty means "the URL
    #: changed, which is all we will claim".
    results: str = ""
    query_param: str = ""
    #: The ``name`` attributes that identify *this site's* search field. This is
    #: the permission classifier's half of the entry -- see
    #: :func:`is_named_search_field` -- and it is a table lookup rather than a
    #: guess for the same reason ``input`` is: a site's own markup is the only
    #: authority on where a search goes.
    fields: tuple[str, ...] = ()


SITES: tuple[SiteSearch, ...] = (
    SiteSearch(
        name="youtube",
        hosts=("youtube.com", "youtu.be"),
        # YouTube's search field has no aria-label and no placeholder in most
        # locales; the name is the only stable handle, and the submit button is
        # the sibling of that name rather than anything text-shaped.
        input='input[name="search_query"]',
        submit='button[aria-label*="Search" i], button[title*="Search" i], form[role="search"] button',
        results="/results",
        query_param="search_query",
        # Measured on the live site (2026): the form that holds this field is
        # `<form action="/results" class="ytSearchboxComponentSearchForm">` with
        # **no `role`** -- the `role="search"` this table used to rely on is
        # gone, and the field is `type="text"` with `role="combobox"`. So none of
        # the structural signals fire, and pressing Enter in it was classified a
        # form submission and asked permission. This is the signal left, and it
        # is a table entry rather than a heuristic for the same reason `input` is.
        fields=("search_query",),
    ),
    SiteSearch(
        name="google",
        hosts=("google.com", "google.co.uk", "google.de", "google.com.bd"),
        input='textarea[name="q"], input[name="q"]',
        # Google has no search button: Enter is the only submit there is.
        results="q=",
        query_param="q",
        fields=("q",),
    ),
    SiteSearch(
        name="maps",
        hosts=("google.com/maps", "maps.google.com", "maps.app.goo.gl"),
        # Maps' search box is a combobox by role and an input by tag, and it only
        # exists once the page has finished loading.
        input='input[id="searchboxinput"], input[aria-label*="Search" i]',
        submit='button[aria-label*="Search" i]',
        results="/maps/search",
    ),
    SiteSearch(
        name="amazon",
        hosts=("amazon.com", "amazon.co.uk", "amazon.de", "amazon.in"),
        input='#twotabsearchtextbox, input[name="field-keywords"]',
        submit='#nav-search-submit-button, input[name="go"]',
        results="/s?",
        query_param="k",
        fields=("field-keywords",),
    ),
    SiteSearch(
        name="reddit",
        hosts=("reddit.com",),
        input='input[name="q"], input[placeholder*="Search" i]',
        submit='button[type="submit"]',
        results="/search",
        query_param="q",
        fields=("q",),
    ),
    SiteSearch(
        name="github",
        hosts=("github.com",),
        input='input[name="q"], input[data-target*="search"]',
        results="?q=",
        query_param="q",
        fields=("q",),
    ),
    SiteSearch(
        name="wikipedia",
        hosts=("wikipedia.org",),
        input='#searchInput, input[name="search"]',
        submit='button[type="submit"]',
        results="?search=",
        query_param="search",
        fields=("search",),
    ),
    SiteSearch(
        name="x",
        hosts=("x.com", "twitter.com"),
        input='input[data-testid="SearchBox_Search_Input"], input[placeholder*="Search" i]',
        submit='button[data-testid="SearchBox_Search_Button"]',
        query_param="q",
        fields=("q",),
    ),
    SiteSearch(
        name="duckduckgo",
        hosts=("duckduckgo.com",),
        # Measured on the live site (2026): the box is a `<textarea name="q">`
        # -- not an input -- inside `<form role="search" id="searchbox_homepage">`,
        # with hidden `ia`/`origin`/`t` fields beside it. The `role` on the form
        # is what makes submitting it an ordinary act rather than a question.
        input='textarea[name="q"], input[name="q"]',
        submit='button[type="submit"], input[type="submit"]',
        results="?q=",
        query_param="q",
        fields=("q",),
    ),
)

#: The search page a request that names no site lands on -- what a browser's
#: address bar does with nothing typed in it. `search_web` already answers
#: *lookups* through DuckDuckGo's HTML endpoint, so this is the same engine on
#: the other side of the same line: the browser's own searches should not be a
#: different company's by accident of which page happened to be open.
DEFAULT_SEARCH_URL = "https://duckduckgo.com/"

#: The engine named by :data:`DEFAULT_SEARCH_URL`, for messages a person reads.
DEFAULT_ENGINE = "duckduckgo"


def site_for(url: object) -> SiteSearch | None:
    """The entry for a page's address, or ``None`` when it is not one we know.

    Longest host fragment wins, so ``google.com/maps`` beats the bare
    ``google.com`` entry. Order in :data:`SITES` cannot decide this: Maps is a
    path on Google's domain, so whichever entry is written first would claim it,
    and typing ``#searchboxinput`` into Google's own homepage is a search that
    silently does nothing.
    """
    text = str(url or "").casefold()
    if not text:
        return None
    best: tuple[int, SiteSearch] | None = None
    for entry in SITES:
        for host in entry.hosts:
            if host in text and (best is None or len(host) > best[0]):
                best = (len(host), entry)
    return best[1] if best is not None else None


def is_named_search_field(url: object, field: object) -> bool:
    """Whether ``field`` is *this site's* search field, per the table above.

    The permission classifier's answer to a case structure cannot settle, and
    the case is measured rather than imagined. YouTube's masthead search box is
    ``<input name="search_query" type="text" role="combobox">`` inside
    ``<form action="/results">`` -- a form with **no ``role``** in the current
    build. So none of ``is_search_control``'s three structural signals fire:
    the form does not claim to be a search form, the field is not
    ``type="search"``, and its role is the suggestions combobox every
    autocomplete shares. Pressing Enter in it was therefore classified as a
    form submission and asked permission, which made the single most ordinary
    act on the site this browser is most used for into an interruption.

    The honest structural answer is "we cannot tell": a one-field GET form is
    also the shape of a newsletter sign-up, and no amount of DOM shape
    separates "search results" from "subscribe" -- the difference is semantic.
    So it is answered the way every other question about where a site searches
    is answered: by a table that names the field, keyed on the host. A page
    that is not in the table gets nothing, which keeps this bounded exactly as
    ``form_role`` is.
    """
    entry = site_for(url)
    if entry is None or not entry.fields:
        return False
    name = str(field or "").strip().casefold()
    if not name:
        return False
    return name in entry.fields


def looks_like_search_label(label: object) -> int:
    """How strongly a control's label says "search", or ``0``.

    Returned as a number rather than a bool so a caller can prefer an exact
    ``"search"`` over a ``"search filters"`` sort control, without having to
    re-derive the rule.
    """
    text = " ".join(str(label or "").split()).casefold()
    if not text:
        return 0
    for strength, word in enumerate(SEARCH_LABEL_WORDS, start=1):
        if text == word:
            return 3
        if word in text:
            return strength
    return 0


def site_suggestions(url: object) -> list[str]:
    """Sites worth offering when the current page is not one we know.

    Purely advisory: a suggestion is never acted on without the user choosing it,
    because suggesting "YouTube" in response to a recipe search is worse than
    saying nothing.
    """
    del url
    return [entry.name for entry in SITES]


def results_moved(url: object, before_url: object, entry: SiteSearch | None) -> bool:
    """Whether a search plausibly went somewhere.

    Three ways to be true, and no fourth: the URL changed, the URL now carries
    the site's own results marker, or there was no URL to begin with (a
    single-page app that swapped its content without touching the address bar).
    A marker alone is enough, because "the URL contains /results" is the site's
    own statement that it is showing results.
    """
    after = str(url or "")
    was = str(before_url or "")
    if entry is not None and entry.results and entry.results in after:
        return True
    if was and after and was != after:
        return True
    return not was and bool(after)


def current_query(url: object) -> str:
    """Read the search query from a known site's results URL, if available."""
    text = str(url or "")
    entry = site_for(text)
    if entry is None or not entry.query_param:
        return ""
    try:
        values = parse_qs(urlsplit(text).query).get(entry.query_param, ())
    except ValueError:
        return ""
    return " ".join(str(values[0]).split()) if values else ""