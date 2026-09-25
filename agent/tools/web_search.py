"""Web search through DuckDuckGo's HTML endpoint: no API key required."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from .http_client import shared_client
from .tools_compat import RunContext, ToolError, function_tool
from .tools_text import clip

SEARCH_URL = "https://html.duckduckgo.com/html/"
TIMEOUT = httpx.Timeout(10.0)
MAX_RESULTS = 5
SNIPPET_LIMIT = 160
TITLE_LIMIT = 100
URL_LIMIT = 110
MAX_OUTPUT_CHARS = 1600
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


@function_tool()
async def search_web(context: RunContext, query: str) -> str:
    """Search the internet and return a few short results.

    Use this whenever you need current facts, prices, versions, dates, or
    anything you are not certain about. Summarise the findings in your own
    words instead of reading the list out line by line.

    Args:
        query: What to look up, such as "current USD to BDT rate".
    """
    search = clip(query, 200).strip()
    if not search:
        raise ToolError("Tell me what to search for.")

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

    lines = []
    for index, (title, snippet, url) in enumerate(results, start=1):
        body = f" - {clip(snippet, SNIPPET_LIMIT)}" if snippet else ""
        lines.append(f"{index}. {clip(title, TITLE_LIMIT)}{body} ({short_url(url)})")
    return clip("\n".join(lines), MAX_OUTPUT_CHARS)
