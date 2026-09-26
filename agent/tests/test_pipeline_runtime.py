import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.pipeline_config import gemini_settings, pipeline_name
import agent.pipeline_factory as pipeline_factory
from agent.pipeline_factory import build_pipeline
from agent.runtime_events import ToolEventBridge
from agent.session_preferences import interruption_mode_from_metadata, normalize_interruption_mode
from agent.tools import apps
from agent.tools.tools_compat import ToolError


class RecordingPublisher:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.topics: list[str] = []

    def emit_and_publish(self, topic: str, record: dict[str, object]) -> dict[str, object]:
        self.records.append(record)
        self.topics.append(topic)
        return record


class PipelineConfigTests(unittest.TestCase):
    def test_gemini_is_the_default_profile(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pipeline_name(), "gemini_live")

    def test_legacy_alias_is_preserved(self):
        with patch.dict(os.environ, {"LUMINE_PIPELINE": "legacy"}, clear=True):
            self.assertEqual(pipeline_name(), "legacy_cascade")

    def test_unknown_profile_fails_fast(self):
        with patch.dict(os.environ, {"LUMINE_PIPELINE": "cloud-only"}, clear=True):
            with self.assertRaises(ValueError):
                pipeline_name()

    def test_gemini_defaults_prioritize_low_latency(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = gemini_settings()
        self.assertEqual(settings["model"], "gemini-3.8-live")
        self.assertEqual(settings["max_output_tokens"], 1024)
        self.assertEqual(settings["thinking_config"]["thinking_level"], "minimal")
        self.assertFalse(settings["thinking_config"]["include_thoughts"])
        self.assertEqual(settings["connect_max_retry"], 0)
        self.assertEqual(settings["max_tool_steps"], 1)

    def test_gemini_environment_overrides_are_bounded(self):
        env = {
            "GEMINI_MODEL": "gemini-3.8-live",
            "GEMINI_MAX_OUTPUT_TOKENS": "200",
            "GEMINI_CONNECT_MAX_RETRY": "99",
            "GEMINI_CONNECT_TIMEOUT": "0.1",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = gemini_settings()
        self.assertEqual(settings["max_output_tokens"], 200)
        self.assertEqual(settings["connect_max_retry"], 2)
        self.assertEqual(settings["connect_timeout"], 10.0)


class InterruptionPreferenceTests(unittest.TestCase):
    def test_metadata_selects_finish_response_mode(self):
        self.assertEqual(
            interruption_mode_from_metadata('{"interruption_mode":"finish_response"}'),
            "finish_response",
        )

    def test_invalid_metadata_falls_back_to_environment_default(self):
        with patch.dict(os.environ, {"LUMINE_INTERRUPTION_MODE": "barge_in"}, clear=False):
            self.assertEqual(interruption_mode_from_metadata("not-json"), "barge_in")
            self.assertEqual(
                interruption_mode_from_metadata('{"interruption_mode":"unknown"}'),
                "barge_in",
            )

    def test_mode_normalization_rejects_unknown_values(self):
        self.assertEqual(normalize_interruption_mode("FINISH_RESPONSE"), "finish_response")
        with self.assertRaises(ValueError):
            normalize_interruption_mode("unknown", default="unknown")


class GeminiFactoryTests(unittest.TestCase):
    def test_factory_builds_native_realtime_model_without_external_stt_or_tts(self):
        try:
            from livekit.plugins import google  # noqa: F401
        except ImportError:
            self.skipTest("livekit-plugins-google is not installed")
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "unit-test-placeholder", "GOOGLE_GENAI_USE_VERTEXAI": "0"}, clear=False):
            components = asyncio.run(build_pipeline("gemini_live"))
        try:
            self.assertEqual(components.profile, "gemini_live")
            self.assertEqual(components.model_name, "gemini-3.8-live")
            self.assertEqual(components.interruption_mode, "barge_in")
            self.assertEqual(components.response_token_limit, 1024)
            from google.genai import types
            self.assertEqual(
                components.llm._opts.realtime_input_config.activity_handling,
                types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            )
            self.assertFalse(components.llm._opts.realtime_input_config.automatic_activity_detection.disabled)
            self.assertIsNone(components.stt)
            self.assertIsNone(components.tts)
            self.assertIsNone(components.vad)
            self.assertIn("llm", components.session_kwargs)
            self.assertIsNone(components.session_kwargs["vad"])
            self.assertEqual(components.session_kwargs["turn_handling"]["turn_detection"], "realtime_llm")
            self.assertNotIn("stt", components.session_kwargs)
            self.assertNotIn("tts", components.session_kwargs)
        finally:
            asyncio.run(components.llm.aclose())

    def test_finish_response_mode_disables_gemini_interruption(self):
        try:
            from google.genai import types
            from livekit.plugins import google  # noqa: F401
        except ImportError:
            self.skipTest("livekit-plugins-google is not installed")
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "unit-test-placeholder", "GOOGLE_GENAI_USE_VERTEXAI": "0"}, clear=False):
            components = asyncio.run(build_pipeline("gemini_live", interruption_mode="finish_response"))
        try:
            self.assertEqual(components.interruption_mode, "finish_response")
            self.assertEqual(components.response_token_limit, 1024)
            self.assertEqual(
                components.llm._opts.realtime_input_config.activity_handling,
                types.ActivityHandling.NO_INTERRUPTION,
            )
            self.assertFalse(components.llm._opts.realtime_input_config.automatic_activity_detection.disabled)
        finally:
            asyncio.run(components.llm.aclose())


class LegacyFactoryTests(unittest.TestCase):
    def test_interruption_mode_is_applied_without_provider_network_calls(self):
        recorded: dict[str, dict] = {}

        class FakeSilero:
            class VAD:
                @staticmethod
                def load(**kwargs):
                    recorded["vad"] = kwargs
                    return object()

        def fake_stt(**kwargs):
            recorded["stt"] = kwargs
            return object()

        def fake_llm(**kwargs):
            recorded["llm"] = kwargs
            return object()

        def fake_tts(**kwargs):
            recorded["tts"] = kwargs
            return object()

        fake_groq = SimpleNamespace(STT=fake_stt, LLM=fake_llm)
        fake_cartesia = SimpleNamespace(TTS=fake_tts)

        for mode, enabled in (("finish_response", False), ("barge_in", True)):
            with patch.object(pipeline_factory, "_silero", FakeSilero), patch.object(
                pipeline_factory, "_groq", fake_groq
            ), patch.object(pipeline_factory, "_cartesia", fake_cartesia):
                components = asyncio.run(build_pipeline("legacy_cascade", interruption_mode=mode))
            self.assertEqual(components.interruption_mode, mode)
            self.assertEqual(
                components.session_kwargs["turn_handling"]["interruption"]["enabled"],
                enabled,
            )

        # Stage builders now forward the resolved model instead of relying on
        # each plugin's own default, so the picks are explicit and assertable.
        self.assertEqual(recorded["stt"]["model"], "whisper-large-v3-turbo")
        self.assertEqual(recorded["tts"]["model"], "sonic-3")
        self.assertEqual(recorded["tts"]["voice"], "002622d8-19d0-4567-a16a-f99c7397c062")
        # The VAD threshold is part of the existing turn-taking behaviour.
        self.assertEqual(recorded["vad"]["min_speech_duration"], 0.4)
        # The Groq budget guards must survive the refactor.
        self.assertIn("max_completion_tokens", recorded["llm"])
        self.assertFalse(recorded["llm"]["parallel_tool_calls"])


class ToolEventBridgeTests(unittest.TestCase):
    def test_open_app_start_and_success_are_normalized(self):
        publisher = RecordingPublisher()
        bridge = ToolEventBridge(publisher)  # type: ignore[arg-type]
        call = SimpleNamespace(call_id="call-1", name="open_app", arguments='{"app_name":"Notepad"}')
        bridge.handle(SimpleNamespace(update=SimpleNamespace(type="tool_call_started", function_call=call)))
        bridge.handle(
            SimpleNamespace(
                update=SimpleNamespace(
                    type="tool_call_ended",
                    call_id="call-1",
                    status="done",
                    message="Opened Notepad.",
                )
            )
        )

        self.assertEqual(publisher.topics, ["lumine.tool", "lumine.tool"])
        self.assertEqual([record["status"] for record in publisher.records], ["started", "completed"])
        self.assertEqual(publisher.records[1]["name"], "open_app")
        self.assertIn("Opening Notepad", str(publisher.records[0]["message"]))
        self.assertEqual(publisher.records[1]["message"], "Opened Notepad.")

    def test_failed_tool_is_not_a_session_error(self):
        publisher = RecordingPublisher()
        bridge = ToolEventBridge(publisher)  # type: ignore[arg-type]
        call = SimpleNamespace(call_id="call-2", name="open_app", arguments='{"app_name":"Missing"}')
        bridge.handle(SimpleNamespace(update=SimpleNamespace(type="tool_call_started", function_call=call)))
        bridge.handle(
            SimpleNamespace(
                update=SimpleNamespace(
                    type="tool_call_ended",
                    call_id="call-2",
                    status="error",
                    message="I couldn't find an app called 'Missing'.",
                )
            )
        )
        self.assertEqual(publisher.records[-1]["status"], "failed")
        self.assertIn("couldn't find", str(publisher.records[-1]["message"]))


class AppLauncherTests(unittest.TestCase):
    def test_path_launch_reports_success_without_waiting(self):
        with patch.object(apps.shutil, "which", return_value="C:/apps/notepad.exe"), patch.object(apps, "spawn") as spawn:
            self.assertEqual(apps.launch("notepad"), "Opened notepad.")
        spawn.assert_called_once_with(["C:/apps/notepad.exe"])

    def test_missing_app_has_a_specific_error(self):
        with patch.object(apps.shutil, "which", return_value=None), patch.object(apps.sys, "platform", "win32"), patch.object(apps.os, "startfile", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ToolError, "couldn't find an app"):
                apps.launch("missing-app")

    def test_found_app_failure_is_distinct_from_missing_app(self):
        with patch.object(apps.shutil, "which", return_value="C:/apps/notepad.exe"), patch.object(apps.sys, "platform", "linux"), patch.object(apps, "spawn", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(ToolError, "found notepad"):
                apps.launch("notepad")

    def test_open_app_disallows_interruptions_and_runs_in_worker_thread(self):
        context = SimpleNamespace(disallow_interruptions=lambda: setattr(context, "called", True), called=False)
        with patch.object(apps.asyncio, "to_thread", wraps=asyncio.to_thread) as to_thread, patch.object(apps, "launch", return_value="Opened notepad.") as launch_mock:
            result = asyncio.run(apps.open_app(context, "notepad"))
        self.assertEqual(result, "Opened notepad.")
        self.assertTrue(context.called)
        to_thread.assert_called_once_with(launch_mock, "notepad")


if __name__ == "__main__":
    unittest.main()
