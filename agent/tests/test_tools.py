import json
import os
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from agent.tools import http_client, tools_registry
from agent.tools.apps import candidates, validate_app_name
from agent.tools.news import SEARCH_URL as NEWS_SEARCH_URL
from agent.tools.news import format_headlines, get_news, parse_headlines
from agent.tools.tool_results import MAX_RESULT_CHARS
from agent.tools.tools_compat import ToolError
from agent.tools.tools_permission import filter_permissions, tool_id
from agent.tools.tools_text import clip
from agent.tools.weather import FORECAST_URL, GEOCODE_URL
from agent.tools.weather import WEATHER_CODES, format_weather, get_weather
from agent.tools.web_search import SEARCH_URL, clean_url, parse_results, search_web, short_url

DDG_HTML = """
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Futm_source%3Dddg&amp;rut=abc">
      Example <b>Title</b>
    </a>
  </h2>
  <a class="result__snippet">A short snippet with <b>bold</b> text.</a>
</div>
<div class="result result--ad">
  <h2 class="result__title">
    <a class="result__a" href="https://ads.example.com/">Buy ads here</a>
  </h2>
</div>
"""

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Top Stories</title>
    <item>
      <title>Big story lands - Reuters</title>
      <pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate>
      <source url="https://www.reuters.com/">Reuters</source>
    </item>
    <item>
      <title>Second story with a headline that is far too long to read out loud</title>
      <pubDate>not a date</pubDate>
      <source url="https://www.bbc.co.uk/">BBC News</source>
    </item>
  </channel>
</rss>
"""

WEATHER_PLACE = {
    "name": "Dhaka",
    "admin1": "Dhaka Division",
    "country": "Bangladesh",
    "latitude": 23.71,
    "longitude": 90.41,
}

WEATHER_FORECAST = {
    "current": {
        "temperature_2m": 31.2,
        "apparent_temperature": 34.8,
        "relative_humidity_2m": 74,
        "weather_code": 2,
        "wind_speed_10m": 12.4,
    },
    "daily": {
        "time": ["2026-09-24", "2026-09-25"],
        "temperature_2m_max": [33.1, 31.0],
        "temperature_2m_min": [28.2, 27.4],
        "precipitation_probability_max": [40, 60],
    },
}


class TextHelperTests(unittest.TestCase):
    def test_clip_collapses_whitespace(self):
        self.assertEqual(clip("  lots \n of   space "), "lots of space")

    def test_clip_caps_length_with_ellipsis(self):
        self.assertEqual(len(clip("x" * 500, limit=20)), 20)
        self.assertTrue(clip("x" * 500, limit=20).endswith("..."))

    def test_clip_handles_empty_values(self):
        self.assertEqual(clip(None), "")
        self.assertEqual(clip(0), "0")

    def test_clip_strips_emoji_and_symbols(self):
        self.assertEqual(clip(" rain \u26c8\ufe0f today "), "rain today")
        self.assertEqual(clip("\U0001f511 api key \u26a1"), "api key")

    def test_clip_keeps_supported_scripts(self):
        self.assertEqual(clip("  ভালো বৃষ্টি  "), "ভালো বৃষ্টি")
        self.assertEqual(clip("معطيات الطقس"), "معطيات الطقس")

    def test_clip_keeps_url_safe_characters(self):
        self.assertEqual(clip("https://example.com/a+b~c"), "https://example.com/a+b~c")

    def test_clip_keeps_meaningful_symbols(self):
        self.assertEqual(clip("31\u00b0C, 40% chance of rain"), "31\u00b0C, 40% chance of rain")
        self.assertEqual(clip("$1,299 VR glasses"), "$1,299 VR glasses")


class WeatherToolTests(unittest.TestCase):
    def test_format_weather_stays_short_and_readable(self):
        summary = format_weather(WEATHER_PLACE, WEATHER_FORECAST)
        record = json.loads(summary)
        # A keyed record, not a sentence: the model can take the one field it
        # needs without reading prose, and it costs fewer tokens.
        self.assertEqual(record["place"], "Dhaka, Dhaka Division, Bangladesh")
        self.assertEqual(record["now"], "31\u00b0C, feels 35\u00b0C, partly cloudy")
        self.assertEqual(record["today"], "28-33, 40% rain")
        self.assertEqual(record["tomorrow"], "27-31, 60% rain")
        self.assertLess(len(summary), 300)

    def test_format_weather_drops_missing_fields(self):
        summary = format_weather(
            {"name": "Nowhere"},
            {"current": {"temperature_2m": 10.4, "weather_code": 0}},
        )
        self.assertEqual(json.loads(summary)["now"], "10\u00b0C, clear sky")
        self.assertNotIn("None", summary)
        self.assertNotIn("chance of rain", summary)

    def test_format_weather_degrades_when_payload_is_empty(self):
        summary = format_weather({"name": "Nowhere"}, {})
        self.assertEqual(json.loads(summary)["now"], "unavailable")

    def test_weather_codes_are_spoken_friendly(self):
        self.assertEqual(WEATHER_CODES[95], "thunderstorm")
        self.assertNotIn("\n", " ".join(WEATHER_CODES.values()))


class WebSearchToolTests(unittest.TestCase):
    def test_parse_results_skips_ads_and_unwraps_links(self):
        results = parse_results(DDG_HTML)
        self.assertEqual(len(results), 1)
        title, snippet, url = results[0]
        self.assertEqual(title, "Example Title")
        self.assertEqual(snippet, "A short snippet with bold text.")
        self.assertEqual(url, "https://example.com/page?utm_source=ddg")

    def test_clean_url_passes_ordinary_links_through(self):
        self.assertEqual(clean_url("https://example.com/a"), "https://example.com/a")

    def test_short_url_drops_the_query_string_before_truncating(self):
        long_link = (
            "https://example.com/a/very/long/path/that/keeps/going/for/ages/forever"
            "?utm_source=ddg&utm_medium=search&utm_campaign=voice"
        )
        shortened = short_url(long_link)
        self.assertNotIn("?", shortened)
        self.assertLessEqual(len(shortened), 110)

    def test_search_results_are_bounded(self):
        # 5 results x (title + snippet + url) must stay a small slice of context.
        self.assertLessEqual(5 * (100 + 160 + 110), 2000)


class NewsToolTests(unittest.TestCase):
    def test_parse_headlines_strips_publisher_and_keeps_source(self):
        headlines = parse_headlines(RSS_FEED)
        self.assertEqual(len(headlines), 2)
        self.assertEqual(headlines[0]["title"], "Big story lands")
        self.assertEqual(headlines[0]["source"], "Reuters")
        self.assertRegex(headlines[0]["age"], r"^\d+d ago$")
        self.assertEqual(headlines[1]["age"], "")

    def test_parse_headlines_never_returns_body_text(self):
        for headline in parse_headlines(RSS_FEED):
            self.assertLessEqual(len(headline["title"]), 110)

    def test_format_headlines_sends_titles_only(self):
        # Titles, and nothing else. Source, age and link were dropped because a
        # tool result is re-sent on every later turn, and each of them cost tokens
        # without changing a reply.
        payload = json.loads(format_headlines(parse_headlines(RSS_FEED)))
        self.assertEqual(list(payload), ["headlines"])
        self.assertEqual(payload["headlines"][0], "Big story lands")
        self.assertEqual(len(payload["headlines"]), 2)
        self.assertNotIn("Reuters", format_headlines(parse_headlines(RSS_FEED)))

    def test_parse_headlines_rejects_broken_feed(self):
        with self.assertRaises(ToolError):
            parse_headlines("<rss><channel>")


class AppToolTests(unittest.TestCase):
    def test_validate_accepts_ordinary_app_names(self):
        self.assertEqual(validate_app_name("  Google   Chrome "), "Google Chrome")
        self.assertEqual(validate_app_name("notepad.exe"), "notepad.exe")

    def test_validate_rejects_shell_fragments(self):
        for dangerous in (
            "notepad & calc",
            "foo|bar",
            "../secret",
            "C:\\Windows\\System32",
            "$(whoami)",
            'app"; rm -rf /',
            "",
            "a" * 65,
        ):
            with self.subTest(app_name=dangerous):
                with self.assertRaises(ToolError):
                    validate_app_name(dangerous)

    def test_candidates_append_windows_extension_only_once(self):
        self.assertEqual(candidates("notepad"), ["notepad", "notepad.exe"])
        self.assertEqual(candidates("notepad.exe"), ["notepad.exe"])


class PermissionTests(unittest.TestCase):
    def test_all_tools_are_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                tools_registry.tool_ids(tools_registry.get_tools()),
                ["get_weather", "get_news", "search_web", "recall_persona", "open_app"],
            )

    def test_disabled_tools_env_hides_individual_tools(self):
        with patch.dict(os.environ, {"LUMINE_DISABLED_TOOLS": "get_news, search_web"}, clear=True):
            self.assertEqual(
                tools_registry.tool_ids(tools_registry.get_tools()),
                ["get_weather", "recall_persona", "open_app"],
            )

    def test_app_launch_toggle_hides_only_desktop_tools(self):
        with patch.dict(os.environ, {"LUMINE_ENABLE_APP_LAUNCH": "false"}, clear=True):
            self.assertEqual(
                tools_registry.tool_ids(tools_registry.get_tools()),
                ["get_weather", "get_news", "search_web", "recall_persona"],
            )

    def test_tool_id_reads_both_function_tools_and_plain_functions(self):
        self.assertEqual(tool_id(tools_registry.ALL_TOOLS[0]), "get_weather")
        self.assertEqual(tool_id(lambda: None), "<lambda>")

    def test_filter_permissions_preserves_order(self):
        with patch.dict(os.environ, {"LUMINE_DISABLED_TOOLS": "open_app"}, clear=True):
            kept = filter_permissions(tools_registry.ALL_TOOLS)
            self.assertEqual([tool_id(tool) for tool in kept], ["get_weather", "get_news", "search_web", "recall_persona"])


class RegistrationTests(unittest.TestCase):
    def test_every_tool_is_registered_with_livekit(self):
        try:
            from livekit.agents.llm.tool_context import FunctionTool
        except ImportError:
            self.skipTest("livekit-agents is not installed")
        for tool in tools_registry.ALL_TOOLS:
            with self.subTest(tool=tool_id(tool)):
                self.assertIsInstance(tool, FunctionTool)
                self.assertTrue(tool.info.description)

    def test_tool_docstrings_reach_the_model_as_schema(self):
        try:
            from livekit.agents.llm.utils import build_legacy_openai_schema
        except ImportError:
            self.skipTest("livekit-agents is not installed")

        expected_arguments = {
            "get_weather": ["location"],
            "search_web": ["query"],
            "get_news": ["topic"],
            "recall_persona": ["topic"],
            "open_app": ["app_name"],
        }
        for tool in tools_registry.ALL_TOOLS:
            with self.subTest(tool=tool_id(tool)):
                schema = build_legacy_openai_schema(tool)["function"]
                self.assertTrue(schema["description"].strip())
                properties = schema["parameters"]["properties"]
                self.assertEqual(sorted(properties), expected_arguments[tool_id(tool)])
                # The RunContext parameter must never be exposed to the model.
                self.assertNotIn("context", properties)
                # The Args: section becomes the per-argument descriptions.
                for name, argument in properties.items():
                    self.assertTrue(
                        argument.get("description", "").strip(),
                        f"{tool_id(tool)}.{name} has no description",
                    )


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200, json_data=None):
        self.text = text
        self.status_code = status_code
        self._json = {} if json_data is None else json_data

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


class FakeClient:
    """Stand-in for httpx.AsyncClient that records how it was used."""

    created = 0
    routes: dict = {}
    fail_with: Exception | None = None

    def __init__(self, **_kwargs):
        FakeClient.created += 1
        self.closed = False
        self.requests: list[tuple[str, dict]] = []

    @property
    def is_closed(self) -> bool:
        return self.closed

    async def aclose(self) -> None:
        self.closed = True

    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if FakeClient.fail_with is not None:
            raise FakeClient.fail_with
        return FakeClient.routes.get(url, FakeResponse())


def long_results_html(count: int = 5) -> str:
    blocks = []
    for index in range(count):
        blocks.append(
            f"""
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a class="result__a" href="https://example.com/page-{index}/very/long/path?utm_source=duckduckgo">
      Result {index} {'title ' * 30}
    </a>
  </h2>
  <a class="result__snippet">{'snippet ' * 40}</a>
</div>
"""
        )
    return "".join(blocks)


def long_news_feed(count: int = 6) -> str:
    items = "".join(
        f"""
  <item>
    <title>Headline {index} {'with a very long descriptive tail ' * 8}</title>
    <pubDate>not a date</pubDate>
    <source url="https://news.example.com/{index}">A Very Long Publisher Name {index}</source>
  </item>
"""
        for index in range(count)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'


class SharedHttpClientTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeClient.created = 0
        FakeClient.fail_with = None
        FakeClient.routes = {
            SEARCH_URL: FakeResponse(text=DDG_HTML),
            NEWS_SEARCH_URL: FakeResponse(text=RSS_FEED),
            GEOCODE_URL: FakeResponse(json_data={"results": [WEATHER_PLACE]}),
            FORECAST_URL: FakeResponse(json_data=WEATHER_FORECAST),
        }
        patcher = patch.object(http_client.httpx, "AsyncClient", FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addAsyncCleanup(http_client.close)

    async def test_one_client_is_created_and_reused_across_tool_calls(self):
        held = await http_client.acquire()  # session-lifetime reference
        await search_web(None, "python release")
        await search_web(None, "rust release")
        self.assertEqual(FakeClient.created, 1)
        self.assertEqual(len(held.requests), 2)
        self.assertTrue(all(url == SEARCH_URL for url, _ in held.requests))
        self.assertEqual(http_client.user_count(), 1)
        await http_client.release()

    async def test_client_is_released_when_no_session_holds_it(self):
        await search_web(None, "python release")
        self.assertFalse(http_client.is_shared())
        self.assertEqual(http_client.user_count(), 0)
        self.assertEqual(FakeClient.created, 1)

    async def test_last_release_closes_the_client(self):
        first = await http_client.acquire()
        second = await http_client.acquire()
        self.assertIs(first, second)

        await http_client.release()
        self.assertFalse(first.closed)
        self.assertTrue(http_client.is_shared())

        await http_client.release()
        self.assertTrue(first.closed)
        self.assertFalse(http_client.is_shared())
        self.assertEqual(http_client.user_count(), 0)

    async def test_close_is_safe_without_an_acquired_client(self):
        await http_client.close()
        await http_client.close()
        self.assertFalse(http_client.is_shared())
        self.assertEqual(http_client.user_count(), 0)

    async def test_failed_call_still_releases_its_reference(self):
        held = await http_client.acquire()
        FakeClient.fail_with = httpx.ConnectError("boom")
        with self.assertRaises(ToolError):
            await search_web(None, "python release")
        self.assertEqual(http_client.user_count(), 1)

        await http_client.release()
        self.assertTrue(held.closed)

    async def test_each_tool_keeps_its_timeout_and_redirect_behaviour(self):
        FakeClient.routes = {
            GEOCODE_URL: FakeResponse(json_data={"results": [WEATHER_PLACE]}),
            FORECAST_URL: FakeResponse(json_data=WEATHER_FORECAST),
            NEWS_SEARCH_URL: FakeResponse(text=long_news_feed()),
            SEARCH_URL: FakeResponse(text=long_results_html()),
        }
        held = await http_client.acquire()
        await get_weather(None, "Dhaka")
        await search_web(None, "python release")
        await get_news(None, "AI")

        sent = dict(held.requests)
        # Weather never followed redirects before and still does not.
        self.assertNotIn("follow_redirects", sent[GEOCODE_URL])
        # Search and news relied on redirect following.
        self.assertTrue(sent[SEARCH_URL]["follow_redirects"])
        self.assertTrue(sent[NEWS_SEARCH_URL]["follow_redirects"])
        for url in (GEOCODE_URL, FORECAST_URL, SEARCH_URL, NEWS_SEARCH_URL):
            with self.subTest(url=url):
                self.assertIn("timeout", sent[url])
        await http_client.release()

    async def test_weather_output_stays_within_budget(self):
        FakeClient.routes = {
            GEOCODE_URL: FakeResponse(json_data={"results": [WEATHER_PLACE]}),
            FORECAST_URL: FakeResponse(json_data=WEATHER_FORECAST),
        }
        result = await get_weather(None, "Dhaka")
        self.assertIn("Dhaka", result)
        self.assertLessEqual(len(result), MAX_RESULT_CHARS)

    async def test_search_output_stays_within_budget(self):
        FakeClient.routes = {SEARCH_URL: FakeResponse(text=long_results_html())}
        result = await search_web(None, "python release")
        self.assertLessEqual(result.count("\n") + 1, 5)
        self.assertLessEqual(len(result), MAX_RESULT_CHARS + 200)

    async def test_news_output_stays_within_budget(self):
        FakeClient.routes = {NEWS_SEARCH_URL: FakeResponse(text=long_news_feed())}
        result = await get_news(None, "AI")
        self.assertLessEqual(result.count("\n") + 1, 6)
        self.assertLessEqual(len(result), MAX_RESULT_CHARS + 200)


if __name__ == "__main__":
    unittest.main()
