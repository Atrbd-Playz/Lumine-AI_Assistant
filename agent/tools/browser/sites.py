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

No entry constructs a search URL. The whole point is that the search runs through
the page's own UI, so what comes back is what a person would have got.

## The fallback

:func:`search_candidates` returns, in order: the site's entry, then the semantic
fallback (a search box, then a field whose label mentions searching), then
nothing. A caller gets a list and decides.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    ),
    SiteSearch(
        name="google",
        hosts=("google.com", "google.co.uk", "google.de", "google.com.bd"),
        input='textarea[name="q"], input[name="q"]',
        # Google has no search button: Enter is the only submit there is.
        results="q=",
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
    ),
    SiteSearch(
        name="reddit",
        hosts=("reddit.com",),
        input='input[name="q"], input[placeholder*="Search" i]',
        submit='button[type="submit"]',
        results="/search",
    ),
    SiteSearch(
        name="github",
        hosts=("github.com",),
        input='input[name="q"], input[data-target*="search"]',
        results="?q=",
    ),
    SiteSearch(
        name="wikipedia",
        hosts=("wikipedia.org",),
        input='#searchInput, input[name="search"]',
        submit='button[type="submit"]',
        results="?search=",
    ),
    SiteSearch(
        name="x",
        hosts=("x.com", "twitter.com"),
        input='input[data-testid="SearchBox_Search_Input"], input[placeholder*="Search" i]',
        submit='button[data-testid="SearchBox_Search_Button"]',
    ),
)


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