import json
import os
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from agent.tools import http_client, tools_permission, tools_registry
from agent.tools.apps import candidates, validate_app_name
from agent.tools.messaging import whatsapp
from agent.tools.news import SEARCH_URL as NEWS_SEARCH_URL
from agent.tools.news import format_headlines, get_news, parse_headlines
from agent.tools.tool_results import MAX_RESULT_CHARS, UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from agent.tools.tools_compat import ToolError
from agent.tools.tools_permission import filter_permissions, tool_id
from agent.tools.tools_text import clip
from agent.tools.weather import FORECAST_URL, GEOCODE_URL
from agent.tools.weather import WEATHER_CODES, format_weather, get_weather
from agent.tools.web_search import (
    SEARCH_PAYLOAD_CHARS,
    SEARCH_URL,
    SNIPPET_LIMIT,
    clean_url,
    parse_results,
    search_web,
    short_url,
)

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
    def setUp(self):
        # Two switches read the *saved document* -- WhatsApp's mode and the
        # per-tool on/off row -- and document-over-environment is the contract
        # for both. A machine's real configuration must not decide which tools
        # these tests expect to see, so both are stubbed to "no document", which
        # leaves the environment as the base.
        for module in (whatsapp, tools_permission):
            patcher = patch.object(module, "effective_document", return_value=({}, "test", []))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_all_tools_are_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                tools_registry.tool_ids(tools_registry.get_tools()),
                ["get_weather", "get_news", "search_web", "recall_persona", "find_app", "open_app", "manage_window", "browse", "run_task", "web_control", "cancel_task", "play_media", "whatsapp_read", "whatsapp_chats", "whatsapp_account", "send_whatsapp"],
            )

    def test_disabled_tools_env_hides_individual_tools(self):
        with patch.dict(os.environ, {"LUMINE_DISABLED_TOOLS": "get_news, search_web"}, clear=True):
            self.assertEqual(
                tools_registry.tool_ids(tools_registry.get_tools()),
                ["get_weather", "recall_persona", "find_app", "open_app", "manage_window", "browse", "run_task", "web_control", "cancel_task", "play_media", "whatsapp_read", "whatsapp_chats", "whatsapp_account", "send_whatsapp"],
            )

    def test_app_launch_toggle_hides_only_the_launcher(self):
        # The toggle is about *launching a program*. `find_app` opens nothing,
        # `manage_window` only arranges what is already running, and
        # `run_task` is handed the enabled tool list so it routes around
        # launching rather than starting one anyway -- so all three stay
        # available to someone who switched launches off.
        with patch.dict(os.environ, {"LUMINE_ENABLE_APP_LAUNCH": "false"}, clear=True):
            self.assertEqual(
                tools_registry.tool_ids(tools_registry.get_tools()),
                ["get_weather", "get_news", "search_web", "recall_persona", "find_app", "manage_window", "browse", "run_task", "web_control", "cancel_task", "play_media", "whatsapp_read", "whatsapp_chats", "whatsapp_account", "send_whatsapp"],
            )

    def test_browser_toggle_hides_the_browser_tools(self):
        # The browser is its own switch because its setup is its own thing:
        # someone who installed no browser engine still wants search, and
        # someone who wants no browsing still wants the desktop tools. Both
        # browser tools go with it -- leaving `web_control` on would hand the
        # model a way to drive a browser through the back door.
        with patch.dict(os.environ, {"LUMINE_ENABLE_BROWSER": "false"}, clear=True):
            self.assertEqual(
                tools_registry.tool_ids(tools_registry.get_tools()),
                ["get_weather", "get_news", "search_web", "recall_persona", "find_app", "open_app", "manage_window", "run_task", "cancel_task", "play_media", "whatsapp_read", "whatsapp_chats", "whatsapp_account", "send_whatsapp"],
            )

    def test_whatsapp_off_hides_only_whats_app(self):
        # Its own switch again, this time from the environment form. The three
        # read-only WhatsApp tools go with it: they need the account they would
        # be sending from, so leaving them on would offer a question the mode
        # switch has already answered.
        with patch.dict(os.environ, {"LUMINE_WHATSAPP_MODE": "off"}, clear=True):
            self.assertEqual(
                tools_registry.tool_ids(tools_registry.get_tools()),
                ["get_weather", "get_news", "search_web", "recall_persona", "find_app", "open_app", "manage_window", "browse", "run_task", "web_control", "cancel_task", "play_media"],
            )

    def test_whatsapp_off_in_the_saved_document_beats_the_environment(self):
        # Document over environment, per key -- the same precedence as the
        # permission policy. Here the document says off while the
        # environment optimistically says automation: off wins, because
        # this is the one toggle whose off switch belongs to the settings
        # screen rather than to a file somebody has to find.
        with patch.object(
            whatsapp,
            "effective_document",
            return_value=({"messaging": {"whatsapp": {"mode": "off"}}}, "test", []),
        ), patch.dict(os.environ, {"LUMINE_WHATSAPP_MODE": "automation"}, clear=True):
            self.assertEqual(
                tools_registry.tool_ids(tools_registry.get_tools()),
                ["get_weather", "get_news", "search_web", "recall_persona", "find_app", "open_app", "manage_window", "browse", "run_task", "web_control", "cancel_task", "play_media"],
            )

    def test_tool_id_reads_both_function_tools_and_plain_functions(self):
        self.assertEqual(tool_id(tools_registry.ALL_TOOLS[0]), "get_weather")
        self.assertEqual(tool_id(lambda: None), "<lambda>")

    # -- the per-tool switch in the saved document ----------------------------

    def _document(self, section):
        """Run with `tools` set to `section` in the saved document."""
        return patch.object(
            tools_permission,
            "effective_document",
            return_value=({"tools": section}, "test", []),
        )

    def test_a_document_switch_turns_one_tool_off(self):
        with self._document({"get_news": "off"}), patch.dict(os.environ, {}, clear=True):
            kept = tools_registry.tool_ids(tools_registry.get_tools())
            self.assertNotIn("get_news", kept)
            self.assertIn("get_weather", kept)
            self.assertEqual(len(kept), 15)

    def test_the_document_beats_the_environment_variable(self):
        # Per-key precedence, the same rule the WhatsApp mode uses. A switch
        # flipped in Settings must win over a line left in `.env`, or the two
        # quietly disagree and the one the user just touched is the one that
        # does nothing.
        with self._document({"send_whatsapp": "on"}), patch.dict(
            os.environ, {"LUMINE_DISABLED_TOOLS": "send_whatsapp"}, clear=True
        ):
            # `on` is not a way to *force* a tool on: the environment is still
            # consulted underneath, and a document that says nothing about a
            # tool leaves that tool exactly as the environment had it.
            self.assertNotIn("send_whatsapp", tools_registry.tool_ids(tools_registry.get_tools()))

    def test_an_off_switch_wins_over_the_environment_saying_on(self):
        with self._document({"browse": "off"}), patch.dict(
            os.environ, {"LUMINE_ENABLE_BROWSER": "true"}, clear=True
        ):
            self.assertNotIn("browse", tools_registry.tool_ids(tools_registry.get_tools()))

    def test_an_unrecognised_value_is_read_as_on(self):
        # This key exists to *reduce* what Lumine can do, so the only safe
        # reading of an instruction we did not understand is to ignore it. The
        # opposite holds for `messaging.whatsapp.mode`, which fails closed.
        with self._document({"get_news": "maybe"}), patch.dict(os.environ, {}, clear=True):
            self.assertIn("get_news", tools_registry.tool_ids(tools_registry.get_tools()))

    def test_a_switch_for_a_tool_that_does_not_exist_hides_nothing(self):
        # Inventing a switch for a tool that is not registered would be a
        # setting that appears to work. `tool_catalog.py --check` reports it.
        with self._document({"not_a_tool": "off"}), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(len(tools_registry.tool_ids(tools_registry.get_tools())), 16)

    def test_the_reason_names_the_settings_row_not_a_variable(self):
        # "You turned this off" and "an environment variable says so" call for
        # different responses, and only one of them is fixable from this screen.
        with self._document({"browse": "off"}), patch.dict(os.environ, {}, clear=True):
            reason = tools_permission.disabled_reason("browse")
            self.assertIn("Settings", reason)
            self.assertIn("tools.browse", reason)

    def test_every_card_publishes_the_key_the_page_writes(self):
        # A switch that saves to the wrong place looks perfect and does nothing,
        # which is the worst shape a settings control can have. So the key is
        # published by the worker and asserted here against the section the
        # runtime actually reads.
        for card in tools_registry.tool_cards():
            self.assertTrue(card["switchable"], card["id"])
            self.assertEqual(card["configKey"], f"{tools_permission.TOOLS_SECTION}.{card['id']}")

    def test_filter_permissions_preserves_order(self):
        with patch.dict(os.environ, {"LUMINE_DISABLED_TOOLS": "open_app"}, clear=True):
            kept = filter_permissions(tools_registry.ALL_TOOLS)
            self.assertEqual(
                [tool_id(tool) for tool in kept],
                ["get_weather", "get_news", "search_web", "recall_persona", "find_app", "manage_window", "browse", "run_task", "web_control", "cancel_task", "play_media", "whatsapp_read", "whatsapp_chats", "whatsapp_account", "send_whatsapp"],
            )


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
            "find_app": ["app_name"],
            "open_app": ["app_name"],
            "manage_window": ["action", "window"],
            # `desktop_control` drives the real desktop (clicks, keys, text at a
            # point) and arrived alongside the WhatsApp split; it is listed here so
            # the schema assertion covers it rather than raising a KeyError, which
            # is what an unlisted tool produces.
            "desktop_control": ["action", "target", "text", "window", "x", "y"],
            "browse": ["action", "key", "ref", "text", "url"],
            "run_task": ["goal", "request"],
            "cancel_task": [],
            "web_control": [
                "action", "css", "for_model", "frame", "index", "key", "name",
                "position", "role", "strategy", "text", "url",
            ],
            "whatsapp_read": ["action", "chat", "query"],
            # The music library. `index` is how an ambiguity the model reported gets
            # resolved without asking again, `folder` is how "play my vocal songs"
            # reaches a playlist instead of matching one filename out of four
            # thousand, and `all_tracks` is the difference between "play a vocal
            # song" and "play all of them".
            "play_media": ["action", "all_tracks", "folder", "index", "query"],
            # `emoji` is for `react` and `to` is for `forward` -- two actions on a
            # chat that need one more fact each, and neither is a second tool.
            "whatsapp_chats": ["action", "chat", "emoji", "message", "to"],
            "whatsapp_account": ["action", "call_type", "who"],
            # `draft_only` is the difference between Lumine writing a message for
            # the user to send and Lumine sending it, which is the whole consent
            # boundary of the send flow.
            "send_whatsapp": [
                "attachment", "draft_only", "message", "recipient", "reply_to",
            ],
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
        # Search's own ceiling, not the shared 800 + 200 slack: results now
        # carry a snippet each, and the payload is sized to hold all four
        # without `as_payload` halving the list. A search that silently
        # dropped its second half would be the same bug as the one that
        # dropped its snippets.
        self.assertLessEqual(len(result), SEARCH_PAYLOAD_CHARS)

    async def test_search_results_carry_a_snippet(self):
        # The reported failure: "she obviously browses the websites but doesn't
        # have context about the website content at all." A title says the page
        # exists; only the snippet can answer the constraints the request
        # actually carried -- "under 1K", the named retailers.
        FakeClient.routes = {SEARCH_URL: FakeResponse(text=long_results_html())}
        result = await search_web(None, "python release")
        # The wrapper is a marker around the payload, not part of it -- strip
        # it the way the model sees past it, rather than asserting on the
        # delimiters.
        body = result.split(UNTRUSTED_OPEN, 1)[1].split(UNTRUSTED_CLOSE, 1)[0]
        payload = json.loads(body.split("\n", 2)[2])
        rows = payload["results"]
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(title=row.get("title")):
                self.assertIn("snippet", row)
                self.assertLessEqual(len(row["snippet"]), SNIPPET_LIMIT)

    async def test_news_output_stays_within_budget(self):
        FakeClient.routes = {NEWS_SEARCH_URL: FakeResponse(text=long_news_feed())}
        result = await get_news(None, "AI")
        self.assertLessEqual(result.count("\n") + 1, 6)
        self.assertLessEqual(len(result), MAX_RESULT_CHARS + 200)


if __name__ == "__main__":
    unittest.main()
