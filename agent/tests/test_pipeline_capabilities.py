"""Tests for the capability-driven pipeline factory.

These cover the point of Phase 2: STT, LLM, TTS, and VAD can each be selected
independently, and realtime can be paired with a separate TTS when — and only
when — the model supports it.

The environment entry point (`build_pipeline`) is covered by
`test_pipeline_runtime.py`, which guards the behaviour that must not change.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.settings.config_store import ResolvedProfile, ResolvedStage, resolve_profile
from agent.pipeline.pipeline_factory import (
    ConfigurationRejected,
    build_resolved,
    provider_module,
    require_module,
)


def stage(provider: str, model: str, **options) -> ResolvedStage:
    return ResolvedStage(provider=provider, model=model, options=dict(options))


def pipeline_profile(**stages) -> dict:
    resolved = {
        "stt": stage("groq", "whisper-large-v3-turbo"),
        "llm": stage("groq", "openai/gpt-oss-20b", max_completion_tokens=300, parallel_tool_calls=False),
        "tts": stage("cartesia", "sonic-3", language="en", speed=0.95, voice="f786b574-daa5-4673-aa0c-cbe3e8534c02"),
        "vad": stage("silero", "silero", min_speech_duration=0.4),
    }
    resolved.update(stages)
    return ResolvedProfile(
        kind="pipeline",
        name="Test pipeline",
        profile_id="test-pipeline",
        interruption_mode="barge_in",
        **resolved,
    )


class ProviderModuleTests(unittest.TestCase):
    def test_every_catalogued_provider_resolves_to_a_module_or_none(self):
        for provider_id in ("google", "groq", "cartesia", "openai", "silero"):
            with self.subTest(provider=provider_id):
                # Either installed or explicitly None; never a lookup error.
                provider_module(provider_id)

    def test_an_unknown_provider_has_no_module(self):
        self.assertIsNone(provider_module("acme"))

    def test_a_missing_plugin_produces_an_actionable_error(self):
        with mock.patch("agent.pipeline.pipeline_factory.provider_module", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                require_module("acme", "speech")
        self.assertIn("livekit-plugins-acme", str(caught.exception))

    def test_a_missing_plugin_names_the_capability(self):
        with mock.patch("agent.pipeline.pipeline_factory.provider_module", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                require_module("acme", "speech-to-text")
        self.assertIn("speech-to-text", str(caught.exception))


class IndependentStageSelectionTests(unittest.TestCase):
    """The whole point of the refactor: stages are chosen, not hardcoded."""

    def _fakes(self, recorded):
        def stt(**kwargs):
            recorded["stt"] = kwargs
            return "stt-instance"

        def llm(**kwargs):
            recorded["llm"] = kwargs
            return "llm-instance"

        def tts(**kwargs):
            recorded["tts"] = kwargs
            return "tts-instance"

        class VAD:
            @staticmethod
            def load(**kwargs):
                recorded["vad"] = kwargs
                return "vad-instance"

        return (
            SimpleNamespace(STT=stt, LLM=llm),
            SimpleNamespace(TTS=tts),
            SimpleNamespace(VAD=VAD),
        )

    def _build(self, resolved):
        recorded: dict = {}
        groq, cartesia, silero = self._fakes(recorded)
        google = SimpleNamespace(
            STT=lambda **kw: recorded.setdefault("stt", kw) or "google-stt-instance"
        )
        installed = {"groq": groq, "cartesia": cartesia, "silero": silero, "google": google}
        with mock.patch("agent.pipeline.pipeline_factory._groq", groq), mock.patch(
            "agent.pipeline.pipeline_factory._cartesia", cartesia
        ), mock.patch("agent.pipeline.pipeline_factory._silero", silero), mock.patch(
            "agent.pipeline.pipeline_factory.provider_module", side_effect=lambda pid: installed.get(pid)
        ):
            components, _ = asyncio.run(build_resolved(resolved, validate=False))
        return components, recorded

    def test_each_stage_reaches_its_own_provider(self):
        components, recorded = self._build(pipeline_profile())
        self.assertEqual(recorded["stt"]["model"], "whisper-large-v3-turbo")
        self.assertEqual(recorded["llm"]["model"], "openai/gpt-oss-20b")
        self.assertEqual(recorded["tts"]["model"], "sonic-3")
        self.assertEqual(recorded["vad"]["min_speech_duration"], 0.4)
        self.assertEqual(components.stt, "stt-instance")
        self.assertEqual(components.tts, "tts-instance")
        self.assertEqual(components.vad, "vad-instance")

    def test_the_stt_provider_can_change_without_touching_the_others(self):
        # Google STT needs a VAD; here it is a swap of one stage only.
        resolved = pipeline_profile(stt=stage("google", "gemini-2.5-flash-lite", language="en"))
        _, recorded = self._build(resolved)
        self.assertEqual(recorded["stt"]["model"], "gemini-2.5-flash-lite")
        self.assertEqual(recorded["llm"]["model"], "openai/gpt-oss-20b")
        self.assertEqual(recorded["tts"]["model"], "sonic-3")

    def test_the_llm_can_change_without_touching_the_others(self):
        resolved = pipeline_profile(llm=stage("groq", "llama-3.3-70b-versatile", max_completion_tokens=300))
        _, recorded = self._build(resolved)
        self.assertEqual(recorded["llm"]["model"], "llama-3.3-70b-versatile")
        self.assertEqual(recorded["stt"]["model"], "whisper-large-v3-turbo")
        self.assertEqual(recorded["tts"]["model"], "sonic-3")

    def test_the_voice_can_change_without_touching_the_others(self):
        resolved = pipeline_profile(
            tts=stage("cartesia", "sonic-3.5", language="en", speed=0.95, voice="a167e0f3-df7e-4d52-a9c3-f949145efdab")
        )
        _, recorded = self._build(resolved)
        self.assertEqual(recorded["tts"]["model"], "sonic-3.5")
        self.assertEqual(recorded["tts"]["voice"], "a167e0f3-df7e-4d52-a9c3-f949145efdab")
        self.assertEqual(recorded["stt"]["model"], "whisper-large-v3-turbo")

    def test_a_profile_without_a_vad_omits_it(self):
        resolved = pipeline_profile(vad=None)
        components, recorded = self._build(resolved)
        self.assertNotIn("vad", recorded)
        self.assertIsNone(components.vad)
        self.assertIsNone(components.session_kwargs["vad"])

    def test_session_options_still_follow_the_interruption_mode(self):
        components, _ = self._build(pipeline_profile())
        self.assertTrue(components.session_kwargs["turn_handling"]["interruption"]["enabled"])
        self.assertEqual(components.session_kwargs["min_endpointing_delay"], 0.4)
        self.assertEqual(components.session_kwargs["max_tool_steps"], 3)


class RealtimeAssemblyTests(unittest.TestCase):
    def _realtime(self, **overrides) -> ResolvedProfile:
        base = {
            "kind": "realtime",
            "name": "Test realtime",
            "profile_id": "test-realtime",
            "interruption_mode": "barge_in",
            "realtime": stage(
                "google",
                "gemini-3.8-live",
                voice="Sulafat",
                language="en",
                max_output_tokens=1024,
                connect_max_retry=0,
                connect_timeout=10.0,
                silence_duration_ms=700,
                thinking_level="minimal",
                include_thoughts=False,
            ),
            "output_mode": "model_voice",
            "max_tool_steps": 1,
        }
        base.update(overrides)
        return ResolvedProfile(**base)

    def test_a_plain_realtime_profile_has_no_external_tts(self):
        fake_google = SimpleNamespace(realtime=SimpleNamespace(RealtimeModel=lambda **kw: SimpleNamespace(**kw)))
        with mock.patch("agent.pipeline.pipeline_factory._google", fake_google), mock.patch(
            "agent.pipeline.pipeline_factory.provider_module", return_value=fake_google
        ):
            components, _ = asyncio.run(build_resolved(self._realtime(), validate=False))
        self.assertIsNone(components.tts)
        self.assertNotIn("tts", components.session_kwargs)
        self.assertEqual(components.session_kwargs["vad"], None)
        self.assertEqual(components.session_kwargs["turn_handling"]["turn_detection"], "realtime_llm")

    def test_half_cascade_attaches_a_separate_tts(self):
        fake_google = SimpleNamespace(realtime=SimpleNamespace(RealtimeModel=lambda **kw: SimpleNamespace(**kw)))
        fake_cartesia = SimpleNamespace(TTS=lambda **kw: SimpleNamespace(**kw))
        resolved = self._realtime(
            output_mode="custom_tts",
            tts=stage("cartesia", "sonic-3", voice="f786b574-daa5-4673-aa0c-cbe3e8534c02", language="en"),
        )
        with mock.patch("agent.pipeline.pipeline_factory._google", fake_google), mock.patch(
            "agent.pipeline.pipeline_factory._cartesia", fake_cartesia
        ), mock.patch(
            "agent.pipeline.pipeline_factory.provider_module",
            side_effect=lambda pid: {"google": fake_google, "cartesia": fake_cartesia}.get(pid),
        ):
            components, _ = asyncio.run(build_resolved(resolved, validate=False))
        self.assertIsNotNone(components.tts)
        self.assertIn("tts", components.session_kwargs)
        # The realtime model still owns turn detection in a half-cascade.
        self.assertEqual(components.session_kwargs["vad"], None)

    def test_half_cascade_is_refused_for_a_native_audio_model(self):
        # This is the rule that matters most, and it must be enforced by the
        # factory too, not only by the settings UI.
        resolved = self._realtime(
            output_mode="custom_tts",
            tts=stage("cartesia", "sonic-3", voice="f786b574-daa5-4673-aa0c-cbe3e8534c02", language="en"),
        )
        with self.assertRaises(ConfigurationRejected) as caught:
            asyncio.run(build_resolved(resolved, validate=True))
        codes = [d.code for d in caught.exception.diagnostics]
        self.assertIn("realtime.custom_tts_unsupported", codes)

    def test_finish_response_maps_to_no_interruption(self):
        captured = {}

        class FakeActivity:
            NO_INTERRUPTION = "no-interruption"
            START_OF_ACTIVITY_INTERRUPTS = "interrupts"

        fake_google = SimpleNamespace(
            realtime=SimpleNamespace(RealtimeModel=lambda **kw: captured.update(kw) or SimpleNamespace(**kw))
        )
        with mock.patch("agent.pipeline.pipeline_factory._google", fake_google), mock.patch(
            "agent.pipeline.pipeline_factory.provider_module", return_value=fake_google
        ), mock.patch("google.genai"):
            asyncio.run(build_resolved(self._realtime(interruption_mode="finish_response"), validate=False))
        # Realtime owns turn detection, so "finish reply" is expressed on the
        # model, never by disabling interruptions at the session level.
        self.assertIn("realtime_input_config", captured)

    def test_a_realtime_provider_without_the_module_says_so(self):
        """The builder resolves the plugin from the stage, not from a hardcoded name.

        A provider with no realtime module is refused with a message naming it,
        rather than a session that cannot be interrupted correctly.
        """
        resolved = self._realtime(realtime=stage("openai", "gpt-realtime", voice="ash"))
        with mock.patch("agent.pipeline.pipeline_factory.require_module") as require:
            # A plugin that exposes no `realtime` attribute.
            del require.return_value.realtime
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(build_resolved(resolved, validate=False))
        self.assertIn("no realtime module", str(caught.exception))
        self.assertIn("openai", str(caught.exception))

    def test_a_realtime_failure_names_the_provider(self):
        # openai does ship a realtime module, so this exercises the other
        # failure: a model the plugin cannot build. The message has to say which
        # provider, because the catalog is no longer the only place that knows.
        resolved = self._realtime(realtime=stage("openai", "gpt-realtime", voice="ash"))
        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(build_resolved(resolved, validate=False))
        self.assertIn("openai", str(caught.exception))


class ResolveProfileTests(unittest.TestCase):
    def test_a_stored_pipeline_profile_fills_every_stage(self):
        profile = {
            "id": "custom",
            "name": "Custom",
            "kind": "pipeline",
            "pipeline": {
                "stt": {"provider": "groq", "model": "whisper-large-v3"},
                "llm": {"provider": "groq", "model": "llama-3.3-70b-versatile"},
                "tts": {"provider": "cartesia", "model": "sonic-3.5", "voice": "abc", "language": "fr"},
                "vad": {"provider": "silero", "model": "silero"},
            },
        }
        resolved = resolve_profile(profile)
        self.assertEqual(resolved.profile_id, "custom")
        self.assertEqual(resolved.stt.model, "whisper-large-v3")
        self.assertEqual(resolved.llm.model, "llama-3.3-70b-versatile")
        self.assertEqual(resolved.tts.model, "sonic-3.5")
        self.assertEqual(resolved.tts.voice, "abc")
        self.assertEqual(resolved.tts.options["language"], "fr")
        self.assertEqual(resolved.vad.provider, "silero")

    def test_profile_overrides_win_over_environment_defaults(self):
        import os
        from unittest import mock

        profile = {
            "id": "custom",
            "kind": "pipeline",
            "pipeline": {
                "stt": {"provider": "groq", "model": "whisper-large-v3"},
                "llm": {"provider": "groq", "model": "custom-llm"},
                "tts": {"provider": "cartesia", "model": "sonic-3", "voice": "v"},
            },
        }
        with mock.patch.dict(os.environ, {"GROQ_MODEL": "env-llm", "CARTESIA_TTS_MODEL": "env-tts"}, clear=True):
            resolved = resolve_profile(profile)
        self.assertEqual(resolved.llm.model, "custom-llm")
        self.assertEqual(resolved.tts.model, "sonic-3")

    def test_the_groq_budget_guards_are_never_dropped(self):
        resolved = resolve_profile({
            "id": "p",
            "kind": "pipeline",
            "pipeline": {
                "stt": {"provider": "groq", "model": "whisper-large-v3-turbo"},
                "llm": {"provider": "groq", "model": "llama-3.3-70b-versatile"},
                "tts": {"provider": "cartesia", "model": "sonic-3", "voice": "v"},
            },
        })
        self.assertIn("max_completion_tokens", resolved.llm.options)
        self.assertFalse(resolved.llm.options["parallel_tool_calls"])

    def test_providers_lists_every_needed_provider(self):
        resolved = resolve_profile({
            "id": "p",
            "kind": "pipeline",
            "pipeline": {
                "stt": {"provider": "groq", "model": "whisper-large-v3-turbo"},
                "llm": {"provider": "groq", "model": "llama-3.3-70b-versatile"},
                "tts": {"provider": "cartesia", "model": "sonic-3", "voice": "v"},
                "vad": {"provider": "silero", "model": "silero"},
            },
        })
        self.assertEqual(set(resolved.providers()), {"groq", "cartesia", "silero"})

    def test_realtime_resolution_carries_the_turn_silence_window(self):
        finished = resolve_profile({
            "id": "r",
            "kind": "realtime",
            "realtime": {"provider": "google", "model": "gemini-3.8-live",
                         "voice": "Sulafat", "turnHandling": {"interruptionMode": "finish_response"}},
        })
        barging = resolve_profile({
            "id": "r",
            "kind": "realtime",
            "realtime": {"provider": "google", "model": "gemini-3.8-live",
                         "voice": "Sulafat", "turnHandling": {"interruptionMode": "barge_in"}},
        })
        self.assertEqual(finished.interruption_mode, "finish_response")
        self.assertGreater(
            finished.realtime.options["silence_duration_ms"],
            barging.realtime.options["silence_duration_ms"],
        )


if __name__ == "__main__":
    unittest.main()
