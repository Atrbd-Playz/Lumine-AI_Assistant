"""News headlines from Google News RSS: keyless and headline-only."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .http_client import shared_client
from . import tool_results
from .tools_compat import RunContext, ToolError, function_tool
from .tools_text import clip

SEARCH_URL = "https://news.google.com/rss/search"
TOP_STORIES_URL = "https://news.google.com/rss"
LOCALE = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
TIMEOUT = httpx.Timeout(10.0)
#: Read from the feed, so a busy news day does not cost the model more context.
MAX_HEADLINES = 8
HEADLINE_LIMIT = 110


def _age(published: str) -> str:
    try:
        moment = parsedate_to_datetime(published)
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def parse_headlines(feed: str) -> list[dict[str, str]]:
    """Take headlines, source, and age from an RSS feed.

    Article bodies are deliberately never fetched, so a busy news day costs
    the model the same context as a quiet one.
    """
    try:
        channel = ET.fromstring(feed).find("channel")
    except ET.ParseError as exc:
        raise ToolError("News is unavailable right now, try again shortly.") from exc

    headlines: list[dict[str, str]] = []
    for item in channel.findall("item") if channel is not None else []:
        if len(headlines) >= MAX_HEADLINES:
            break
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        # Google News repeats the publisher at the end of every title.
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].strip()
        if not title:
            continue
        headlines.append(
            {
                "title": clip(title, HEADLINE_LIMIT),
                "source": clip(source, 40),
                "age": _age(item.findtext("pubDate") or ""),
            }
        )
    return headlines


def format_headlines(headlines: list[dict[str, str]]) -> str:
    """The compact record the model receives.

    Titles only, and only the first few. The source and the age were formatted
    into every line before, which is roughly a doubling of the payload for detail
    that changes no reply -- and it is paid on every later turn, because a tool
    result stays in the conversation. The full feed is still available in the
    desktop app, where reading it costs nothing.
    """
    return tool_results.headlines([headline["title"] for headline in headlines])


@function_tool()
async def get_news(context: RunContext, topic: str = "") -> str:
    """Get recent news headlines, optionally about a specific topic.

    Returns headlines only, never article text. Mention the one or two items
    that answer the question and offer to go deeper, rather than reciting the
    whole list.

    Args:
        topic: Subject to look up, such as "AI" or "Premier League". Leave it
            empty for today's top stories.
    """
    subject = clip(topic, 120).strip()
    params = dict(LOCALE)
    url = TOP_STORIES_URL
    if subject:
        params["q"] = subject
        url = SEARCH_URL

    try:
        async with shared_client() as client:
            response = await client.get(
                url, params=params, follow_redirects=True, timeout=TIMEOUT
            )
    except httpx.HTTPError as exc:
        raise ToolError("News is unavailable right now, try again shortly.") from exc

    if response.status_code != 200:
        raise ToolError("News is unavailable right now, try again shortly.")

    headlines = parse_headlines(response.text)
    if not headlines:
        if subject:
            raise ToolError(f"I couldn't find recent news about '{clip(subject, 60)}'.")
        raise ToolError("I couldn't fetch the top stories right now.")

    return tool_results.wrap_untrusted(format_headlines(headlines))
