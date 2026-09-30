"""Thinking levels are a per-model fact, not a constant.

A voice session that fails its first LLM request produces no audio and no obvious
error, so an unsupported thinking level has to be caught before the request is
sent. Google rejects one with a 400, and the accepted sets are not nested:
`gemini-3.8-flash` takes low/medium/high but not minimal.

The Live API is not a single set either, which is the point this file had wrong.
Google's table at https://ai.google.dev/gemini-api/docs/live-api/thinking puts
`gemini-3.8-live` at "thinking_level not supported" and
`gemini-3.8-live-extended-thinking` at low/medium/high with minimal unsupported.
Collapsing both into one "Live API" set sent `minimal` to a model that refuses
the parameter, which failed at session setup with a 1007 and no audio -- the
regression these tests exist to prevent.
"""

import unittest
from unittest import mock

from agent.settings.config_store import resolve_profile
from agent.pipeline.pipeline_factory import _build_llm, _build_realtime
from agent.settings.providers import PROVIDERS, get_model
from agent.settings.validation import validate_document

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

    def test_the_standard_live_model_takes_no_level(self):
        # The reported failure. `gemini-3.8-live` is not configured by level at
        # all, so it must declare none: an empty set makes the factory omit
        # `thinking_config` entirely rather than send one Google refuses.
        model = get_model("google", "gemini-3.8-live", "realtime")
        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model.thinking_levels, ())
        self.assertFalse(model.accepts("thinking_level"))

    def test_the_live_thinking_model_stops_at_low(self):
        # The one Live model that does take a level. Google's table excludes
        # minimal, so the family's `minimal` default was wrong here too.
        model = get_model("google", "gemini-3.8-live-extended-thinking", "realtime")
        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model.thinking_levels, ("low", "medium", "high"))
        self.assertFalse(model.supports_thinking_level("minimal"))
        self.assertTrue(model.supports_thinking_level("low"))
        self.assertTrue(model.supports_thinking_level("high"))

    def test_a_model_not_configured_by_level_says_so(self):
        # Gemini 2.5 uses a token budget, and the plugin only translates a level
        # for the Gemini 3 family. Empty means "send nothing", not "send minimal".
        model = get_model("google", "gemini-3.1-flash-lite", "llm")
        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model.thinking_levels, ())
        self.assertFalse(model.accepts("thinking_level"))

    def test_no_model_ships_a_default_level(self):
        """Nothing in the catalog picks a level on the user's behalf.

        There used to be a `default_thinking_level()` that answered "the lowest
        level this model accepts", and it was the reason a profile with no
        thinking setting still sent one. Omitting the level is both valid for
        every model and faster than any level we could name, so there is no
        default to compute. A model either takes the levels it declares or takes
        none.
        """
        for provider in PROVIDERS.values():
            for model in provider.models:
                if not model.thinking_levels:
                    continue
                with self.subTest(model=model.id):
                    self.assertFalse(hasattr(model, "default_thinking_level"))

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
        with mock.patch("agent.pipeline.pipeline_factory.require_module") as require:
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
        with mock.patch("agent.pipeline.pipeline_factory.require_module") as require:
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
        with mock.patch("agent.pipeline.pipeline_factory.require_module") as require:
            module = require.return_value
            import asyncio

            asyncio.run(_build_llm(resolved.llm))
        config = module.LLM.call_args.kwargs["thinking_config"]
        self.assertEqual(config["thinking_level"], "low")


class RealtimeThinkingLevelTests(unittest.TestCase):
    """The realtime path, tested through the builder that talks to the plugin.

    These used to assert the behaviour of a helper that nothing called, while the
    real path went through `build_options`. That is how a rule nobody enforced --
    "default to the lowest level the model accepts" -- sat in the codebase looking
    tested. Every assertion here therefore goes through `_build_realtime` and
    reads the kwargs the plugin would actually receive.
    """

    def _plugin_kwargs(self, profile: dict) -> dict:
        resolved = resolve_profile(profile, interruption_mode="barge_in")
        with mock.patch("agent.pipeline.pipeline_factory.require_module") as require:
            module = require.return_value
            import asyncio

            asyncio.run(_build_realtime(resolved.realtime, resolved.interruption_mode))
        return module.realtime.RealtimeModel.call_args.kwargs

    def test_the_standard_live_model_sends_no_thinking_config(self):
        """The reported failure, asserted end to end.

        A saved realtime profile with no level must reach the plugin with no
        `thinking_config`. It used to arrive with `{"thinking_level": "minimal"}`,
        which `gemini-3.8-live` rejects at setup with a 1007, so the greeting never
        played and the session closed with no audio.
        """
        kwargs = self._plugin_kwargs(realtime_profile())
        self.assertNotIn("thinking_config", kwargs)
        self.assertEqual(kwargs["model"], "gemini-3.8-live")

    def test_a_stale_level_on_the_standard_model_is_not_sent(self):
        # A hand-edited or previously saved profile can hold a level this model
        # does not take. The session must still start, and validation names it.
        profile = realtime_profile(thinking_level="minimal")
        self.assertNotIn("thinking_config", self._plugin_kwargs(profile))
        self.assertIn("llm.thinking_level_unsupported", codes(as_document(profile)))

    def test_the_thinking_model_sends_nothing_when_no_level_is_saved(self):
        kwargs = self._plugin_kwargs(
            realtime_profile(model="gemini-3.8-live-extended-thinking")
        )
        self.assertNotIn("thinking_config", kwargs)

    def test_an_explicit_level_reaches_the_thinking_model(self):
        kwargs = self._plugin_kwargs(
            realtime_profile(
                model="gemini-3.8-live-extended-thinking", thinking_level="high"
            )
        )
        self.assertEqual(kwargs["thinking_config"]["thinking_level"], "high")

    def test_a_stale_minimal_is_downgraded_on_the_thinking_model(self):
        # `minimal` is the level Google's table excludes. It must be corrected
        # rather than forwarded, or the thinking model fails exactly as the
        # standard one did.
        profile = realtime_profile(
            model="gemini-3.8-live-extended-thinking", thinking_level="minimal"
        )
        kwargs = self._plugin_kwargs(profile)
        self.assertEqual(kwargs["thinking_config"]["thinking_level"], "low")
        self.assertIn("llm.thinking_level_unsupported", codes(as_document(profile)))


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
