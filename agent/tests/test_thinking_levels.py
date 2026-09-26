"""Thinking levels are a per-model fact, not a constant.

A voice session that fails its first LLM request produces no audio and no obvious
error, so an unsupported thinking level has to be caught before the request is
sent. Google rejects one with a 400, and the accepted sets are not nested:
`gemini-3.8-flash` takes low/medium/high but not minimal, while the Live API
takes minimal too.
"""

import unittest
from unittest import mock

from agent.config_store import resolve_profile
from agent.pipeline_factory import _build_llm, _realtime_thinking_level
from agent.providers import PROVIDERS, get_model
from agent.validation import validate_document

ALL_PRESENT = {provider_id: True for provider_id in PROVIDERS}


def pipeline_profile(llm: dict | None = None) -> dict:
    return {
        "id": "p",
        "name": "P",
        "kind": "pipeline",
        "pipeline": {
            "stt": {"provider": "groq", "model": "whisper-large-v3-turbo"},
            "llm": llm or {"provider": "google", "model": "gemini-3.8-flash"},
            "tts": {
                "provider": "cartesia",
                "model": "sonic-3",
                "voice": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
            },
        },
    }


def realtime_profile(**realtime: object) -> dict:
    return {
        "id": "r",
        "name": "R",
        "kind": "realtime",
        "realtime": {
            "provider": "google",
            "model": "gemini-3.8-live",
            "voice": "Sulafat",
            "output": {"mode": "model_voice"},
            **realtime,
        },
    }


def as_document(profile: dict) -> dict:
    return {
        "version": 1,
        "activeProfileId": profile["id"],
        "providers": {},
        "profiles": [profile],
    }


def codes(document: dict) -> list[str]:
    return [d.code for d in validate_document(document, ALL_PRESENT)]


class ThinkingLevelCatalogTests(unittest.TestCase):
    def test_gemini_38_flash_rejects_minimal(self):
        """The value that broke the session. Google's own table lists three levels."""
        model = get_model("google", "gemini-3.8-flash", "llm")
        self.assertIsNotNone(model)
        assert model is not None
        self.assertFalse(model.supports_thinking_level("minimal"))
        self.assertTrue(model.supports_thinking_level("low"))
        self.assertTrue(model.supports_thinking_level("high"))
        self.assertEqual(model.thinking_levels, ("low", "medium", "high"))

    def test_the_level_sets_are_not_nested(self):
        # A single family-wide default is exactly the bug: 3.6 accepts minimal,
        # 3.8 does not. This asserts the distinction so it cannot be flattened.
        six = get_model("google", "gemini-3.6-flash", "llm")
        eight = get_model("google", "gemini-3.8-flash", "llm")
        self.assertIsNotNone(six)
        self.assertIsNotNone(eight)
        assert six is not None and eight is not None
        self.assertTrue(six.supports_thinking_level("minimal"))
        self.assertFalse(eight.supports_thinking_level("minimal"))

    def test_the_live_api_still_accepts_minimal(self):
        # The Live plugin documents minimal as its lowest-latency default, so the
        # realtime path must keep working after the pipeline path was fixed.
        for model_id in ("gemini-3.8-live", "gemini-3.1-flash-live-preview"):
            model = get_model("google", model_id, "realtime")
            self.assertIsNotNone(model, model_id)
            assert model is not None
            with self.subTest(model=model_id):
                self.assertTrue(model.supports_thinking_level("minimal"))

    def test_a_model_not_configured_by_level_says_so(self):
        # Gemini 2.5 uses a token budget, and the plugin only translates a level
        # for the Gemini 3 family. Empty means "send nothing", not "send minimal".
        model = get_model("google", "gemini-3.1-flash-lite", "llm")
        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model.thinking_levels, ())
        self.assertIsNone(model.default_thinking_level())

    def test_the_default_is_the_lowest_level_the_model_accepts(self):
        model = get_model("google", "gemini-3.8-flash", "llm")
        assert model is not None
        self.assertEqual(model.default_thinking_level(), "low")

    def test_a_level_is_accepted_case_insensitively(self):
        model = get_model("google", "gemini-3.8-flash", "llm")
        assert model is not None
        self.assertTrue(model.supports_thinking_level("HIGH"))
        self.assertTrue(model.supports_thinking_level("  High  "))


class ThinkingLevelValidationTests(unittest.TestCase):
    def test_an_unsupported_level_blocks(self):
        document = as_document(
            pipeline_profile(
                {"provider": "google", "model": "gemini-3.8-flash", "thinking_level": "minimal"}
            )
        )
        self.assertIn("llm.thinking_level_unsupported", codes(document))

    def test_a_supported_level_is_accepted(self):
        document = as_document(
            pipeline_profile(
                {"provider": "google", "model": "gemini-3.8-flash", "thinking_level": "low"}
            )
        )
        self.assertNotIn("llm.thinking_level_unsupported", codes(document))

    def test_the_diagnostic_names_the_supported_levels(self):
        document = as_document(
            pipeline_profile(
                {"provider": "google", "model": "gemini-3.8-flash", "thinking_level": "minimal"}
            )
        )
        diagnostic = next(
            d for d in validate_document(document, ALL_PRESENT)
            if d.code == "llm.thinking_level_unsupported"
        )
        self.assertEqual(diagnostic.severity, "error")
        self.assertEqual(diagnostic.path, "pipeline.llm.thinking_level")
        self.assertIn("low, medium, high", diagnostic.hint or "")

    def test_a_level_on_a_google_model_that_takes_none_blocks(self):
        # gemini-3.1-flash-lite is catalogued without a level set: Google's table
        # does not list the plain model, so the worker must not invent one.
        document = as_document(
            pipeline_profile(
                {"provider": "google", "model": "gemini-3.1-flash-lite", "thinking_level": "low"}
            )
        )
        self.assertIn("llm.thinking_level_unsupported", codes(document))

    def test_no_level_is_fine(self):
        self.assertNotIn(
            "llm.thinking_level_unsupported", codes(as_document(pipeline_profile()))
        )

    def test_a_level_on_a_non_google_model_blocks(self):
        """A stage that takes no level must say so rather than ignore the value.

        Previously this was skipped for non-Google providers, on the reasoning
        that a stray key was meaningless. It is better to block it: the setting
        would be dropped at build time, and a user who set it deserves to know.
        """
        document = as_document(
            pipeline_profile(
                {"provider": "groq", "model": "openai/gpt-oss-20b", "thinking_level": "high"}
            )
        )
        self.assertIn("llm.thinking_level_unsupported", codes(document))


class ThinkingLevelFactoryTests(unittest.TestCase):
    def test_no_level_is_invented_for_the_pipeline_llm(self):
        """The regression: a saved profile with no level must send no level.

        The factory used to default to `minimal`, which is a 400 for
        gemini-3.8-flash and produces a silent session with no audio.
        """
        resolved = resolve_profile(pipeline_profile(), interruption_mode="barge_in")
        self.assertNotIn("thinking_level", resolved.llm.options)
        with mock.patch("agent.pipeline_factory.require_module") as require:
            module = require.return_value
            import asyncio

            asyncio.run(_build_llm(resolved.llm))
        options = module.LLM.call_args.kwargs
        self.assertNotIn("thinking_config", options)
        self.assertEqual(options["model"], "gemini-3.8-flash")

    def test_an_explicit_supported_level_is_forwarded(self):
        resolved = resolve_profile(
            pipeline_profile(
                {"provider": "google", "model": "gemini-3.8-flash", "thinking_level": "high"}
            ),
            interruption_mode="barge_in",
        )
        with mock.patch("agent.pipeline_factory.require_module") as require:
            module = require.return_value
            import asyncio

            asyncio.run(_build_llm(resolved.llm))
        config = module.LLM.call_args.kwargs["thinking_config"]
        self.assertEqual(config["thinking_level"], "high")

    def test_a_stale_level_is_downgraded_rather_than_sent(self):
        # A hand-edited or previously saved profile can hold a level the model no
        # longer takes. The session must still start.
        resolved = resolve_profile(
            pipeline_profile(
                {"provider": "google", "model": "gemini-3.8-flash", "thinking_level": "minimal"}
            ),
            interruption_mode="barge_in",
        )
        with mock.patch("agent.pipeline_factory.require_module") as require:
            module = require.return_value
            import asyncio

            asyncio.run(_build_llm(resolved.llm))
        config = module.LLM.call_args.kwargs["thinking_config"]
        self.assertEqual(config["thinking_level"], "low")


class RealtimeThinkingLevelTests(unittest.TestCase):
    def test_the_realtime_path_keeps_a_level(self):
        resolved = resolve_profile(realtime_profile(), interruption_mode="barge_in")
        self.assertEqual(resolved.realtime.options.get("thinking_level"), "minimal")

    def test_a_valid_realtime_level_is_kept(self):
        resolved = resolve_profile(
            realtime_profile(thinking_level="high"), interruption_mode="barge_in"
        )
        self.assertEqual(resolved.realtime.options.get("thinking_level"), "high")

    def test_the_realtime_default_is_catalogued_not_hardcoded(self):
        model = get_model("google", "gemini-3.8-live", "realtime")
        self.assertEqual(_realtime_thinking_level(model, None), "minimal")
        self.assertEqual(_realtime_thinking_level(model, "medium"), "medium")
        # An uncatalogued model yields nothing rather than a guess. Inventing a
        # level here is the bug this whole change exists to remove.
        self.assertIsNone(_realtime_thinking_level(None, None))

    def test_a_realtime_model_without_levels_yields_nothing(self):
        model = get_model("google", "gemini-2.5-flash-native-audio-preview-12-2025", "realtime")
        self.assertIsNotNone(model)
        assert model is not None
        # This one does take levels, so the point is the guard: a model with an
        # empty set must not be handed one.
        stripped = type(model)(**{**model.__dict__, "thinking_levels": ()})
        self.assertIsNone(_realtime_thinking_level(stripped, None))


class ProviderOptionIsolationTests(unittest.TestCase):
    """Groq's clamps are Groq's, not generic LLM settings.

    They existed to keep a voice turn inside Groq's rate limit. Applying them to
    whichever provider happened to be selected would silently cap another
    vendor's token budget and reuse Groq's retry policy.
    """

    def test_a_google_llm_inherits_no_groq_clamps(self):
        resolved = resolve_profile(pipeline_profile(), interruption_mode="barge_in")
        self.assertEqual(resolved.llm.provider, "google")
        self.assertNotIn("max_completion_tokens", resolved.llm.options)
        self.assertNotIn("max_retries", resolved.llm.options)
        self.assertNotIn("parallel_tool_calls", resolved.llm.options)

    def test_groq_still_gets_its_clamps(self):
        resolved = resolve_profile(
            pipeline_profile({"provider": "groq", "model": "openai/gpt-oss-20b"}),
            interruption_mode="barge_in",
        )
        self.assertEqual(resolved.llm.provider, "groq")
        self.assertIn("max_completion_tokens", resolved.llm.options)
        self.assertIn("max_retries", resolved.llm.options)

    def test_a_profile_override_still_wins(self):
        resolved = resolve_profile(
            pipeline_profile(
                {"provider": "groq", "model": "openai/gpt-oss-20b", "temperature": 0.2}
            ),
            interruption_mode="barge_in",
        )
        self.assertEqual(resolved.llm.options["temperature"], 0.2)


if __name__ == "__main__":
    unittest.main()
