"""Web search through DuckDuckGo's HTML endpoint: no API key required."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from .http_client import shared_client
from . import tool_results
from .tools_compat import RunContext, ToolError, function_tool
from .tools_text import clip

try:
    from ..runtime.permission_gate import require_approval
except ImportError:  # running as a top-level script with agent/ on sys.path
    from runtime.permission_gate import require_approval

SEARCH_URL = "https://html.duckduckgo.com/html/"
TIMEOUT = httpx.Timeout(10.0)
MAX_RESULTS = 4
TITLE_LIMIT = 100
URL_LIMIT = 110
#: Each result's snippet, bounded. Four of these is the whole reason the
#: payload has its own ceiling: a title says the page *exists*, and the
#: reported failure was a model that could not decide between three retailers
#: because every result was a title and a link -- "under 1K" and "Ryan's,
#: StarTech or Techland" are constraints on *content*, and a list of titles
#: carries none. The snippet is also the part most likely to carry an
#: instruction rather than a fact, which is why it is short, kept inside the
#: untrusted wrapper, and never rendered into the transcript.
SNIPPET_LIMIT = 140
#: Search's own budget, above the shared 800-character ceiling and below the
#: page-read one. Worst case is 4 x (100 title + 110 url + 140 snippet + ~40
#: JSON) + ~90 wrapper = ~1651, so 1700 holds every result without `as_payload`
#: having to halve the list -- a search that silently dropped its second half
#: would be the same class of bug as one that dropped its snippets.
SEARCH_PAYLOAD_CHARS = 1700
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def clean_url(href: str) -> str:
    """Unwrap DuckDuckGo's redirect links back to the real destination."""
    url = (href or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if "uddg=" not in url:
        return url
    targets = parse_qs(urlparse(url).query).get("uddg")
    return targets[0] if targets else url


def short_url(url: str) -> str:
    """Drop the query string from a long link instead of truncating mid-path."""
    if len(url) <= URL_LIMIT:
        return url
    parsed = urlparse(url)
    trimmed = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return trimmed if len(trimmed) <= URL_LIMIT else clip(trimmed, URL_LIMIT)


def parse_results(html: str) -> list[tuple[str, str, str]]:
    """Pull (title, snippet, url) triples out of a DuckDuckGo results page."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[tuple[str, str, str]] = []
    for block in soup.select("div.result"):
        classes = " ".join(block.get("class") or [])
        if "result--ad" in classes or "result--no-result" in classes:
            continue
        anchor = block.select_one("a.result__a")
        if anchor is None:
            continue
        title = anchor.get_text(" ", strip=True)
        url = clean_url(anchor.get("href") or "")
        snippet_el = block.select_one(".result__snippet")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        if title and url:
            results.append((title, snippet, url))
    return results


def format_results(results: list[tuple[str, str, str]]) -> str:
    """The compact record the model receives: title, snippet, and where it came from.

    The snippet is bounded rather than dropped, and that was the fix for the
    reported failure: *"she obviously browses the websites but doesn't have
    context about the website content at all."* Titles answer "what exists on
    this topic"; they do not answer "is this one under 1K" or "does Ryan's
    have it", which are the constraints the request actually carried. A model
    deciding between three retailers from three titles can only guess, and a
    guess dressed as an answer is worse than "I don't know".

    It stays inside the untrusted wrapper and is clipped hard for the other
    reason: a snippet is scraped page text, and scraped page text is the part
    most likely to be carrying an instruction rather than a fact.
    """
    payload = [
        {
            "title": clip(title, TITLE_LIMIT),
            "snippet": clip(" ".join((snippet or "").split()), SNIPPET_LIMIT),
            "url": short_url(url),
        }
        for title, snippet, url in results
    ]
    body = tool_results.as_payload({"results": payload}, max_chars=SEARCH_PAYLOAD_CHARS)
    return tool_results.wrap_untrusted(body, max_chars=SEARCH_PAYLOAD_CHARS)


@function_tool()
async def search_web(context: RunContext, query: str) -> str:
    """Search the internet and return a few results with short snippets.

    Use this whenever you need current facts, prices, versions, dates, or
    anything you are not certain about. Each result carries a one-line
    snippet from the page itself -- compare the results against what the user
    asked for (a budget, a named shop, a region) using the snippets rather
    than opening every link. Summarise the findings in your own words instead
    of reading the list out line by line.

    Args:
        query: What to look up, such as "current USD to BDT rate".
    """
    search = clip(query, 200).strip()
    if not search:
        raise ToolError("Tell me what to search for.")

    # A read: automatic by default, but consulted so a settings entry for
    # `search_web` (ask, or off) actually governs the call instead of
    # decorating a card the tool then ignores.
    await require_approval(
        context,
        tool="search_web",
        action="read",
        action_class="read",
        summary=f"Search the web for {search}",
        target=search,
    )

    try:
        async with shared_client() as client:
            response = await client.get(
                SEARCH_URL,
                params={"q": search},
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
                timeout=TIMEOUT,
            )
    except httpx.HTTPError as exc:
        raise ToolError("Web search is unavailable right now, try again shortly.") from exc

    if response.status_code != 200:
        raise ToolError("Web search is unavailable right now, try again shortly.")

    results = parse_results(response.text)[:MAX_RESULTS]
    if not results:
        raise ToolError(f"I couldn't find results for '{clip(search, 60)}'.")

    return format_results(results)

