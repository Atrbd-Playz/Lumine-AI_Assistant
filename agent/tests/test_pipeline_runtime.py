import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.settings.pipeline_config import gemini_settings, pipeline_name
from agent.settings.validation import Diagnostic
import agent.pipeline.pipeline_factory as pipeline_factory
from agent.pipeline.pipeline_factory import build_pipeline
from agent.runtime.runtime_events import ToolEventBridge
from agent.settings.session_preferences import interruption_mode_from_metadata, normalize_interruption_mode
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


class RejectedProfileReportingTests(unittest.TestCase):
    """A profile the user chose must never be swapped for another one silently.

    The reported symptom was "the pipeline voice has some problem": a Pipeline
    profile was selected and saved, the worker rejected it, and the rejection was
    swallowed -- the session continued on the environment profile instead, which
    was a *different stack* (realtime rather than the Groq/Cartesia cascade).
    Nobody could tell, because the reason lived in `diagnostics` on an event the
    app rendered without reading them, and because nothing said a substitution
    had happened at all.

    The fallback itself is right. A working voice beats no voice, and refusing to
    start would turn a missing key into a mute button. What was wrong is that it
    was *silent* -- so these pin the two things that make it answerable: the event
    names the cause and the substitution, and the app is told in terms a person
    can act on.
    """

    def _diagnostics(self, *labels: str):
        from agent.settings.validation import Diagnostic

        return [
            Diagnostic(
                severity="error",
                code="credential.missing",
                path=f"profiles.p.pipeline.{label.lower()}",
                message=f"{label} has no credential available.",
                hint="",
            )
            for label in labels
        ]

    def test_a_missing_key_is_named_rather_than_reduced_to_a_generic_reason(self):
        from agent.agent import _missing_credential_labels

        self.assertEqual(
            _missing_credential_labels(self._diagnostics("Groq", "Cartesia")),
            ["Groq", "Cartesia"],
        )

    def test_a_rejection_that_is_not_a_missing_key_is_not_mislabelled_as_one(self):
        # Otherwise a typo in a profile is reported as "add your Groq key", which
        # sends somebody to the Providers page for a problem that is not there.
        from agent.agent import _missing_credential_labels
        from agent.settings.validation import Diagnostic

        self.assertEqual(
            _missing_credential_labels(
                [
                    Diagnostic(
                        severity="error",
                        code="model.unknown",
                        path="profiles.p.pipeline.llm.model",
                        message="No such model: groq/nope.",
                    )
                ]
            ),
            [],
        )

    def test_a_provider_named_twice_is_listed_once(self):
        from agent.agent import _missing_credential_labels

        self.assertEqual(
            _missing_credential_labels(self._diagnostics("Groq", "Groq")), ["Groq"]
        )

    def test_a_setup_notice_is_published_and_never_raises(self):
        from agent.agent import _publish_setup_notice

        publisher = RecordingPublisher()
        _publish_setup_notice(publisher, "Profile 'Pipeline' needs a Groq key", "Add it.")
        self.assertEqual(publisher.topics, ["lumine.notice"])
        self.assertEqual(publisher.records[0]["title"], "Profile 'Pipeline' needs a Groq key")

        class Broken:
            def emit_and_publish(self, *_args, **_kwargs):
                raise RuntimeError("the pipe is gone")

        # The failure path is the wrong place to add a second failure.
        _publish_setup_notice(Broken(), "t", "m")

    def test_an_empty_diagnostic_list_names_nothing(self):
        from agent.agent import _missing_credential_labels

        self.assertEqual(_missing_credential_labels([]), [])
        self.assertEqual(_missing_credential_labels(None), [])

    def test_a_rejection_reason_prefers_the_diagnostic_that_names_a_provider(self):
        from agent.agent import _rejection_reason

        class Rejected(Exception):
            diagnostics = [
                Diagnostic(
                    severity="error",
                    code="llm.unsupported",
                    path="p.llm",
                    message="That stage does not take a thinking level.",
                ),
                Diagnostic(
                    severity="error",
                    code="credential.missing",
                    path="p.tts",
                    message="Cartesia has no credential available.",
                ),
            ]

        self.assertEqual(
            _rejection_reason(Rejected()),
            "Cartesia has no credential available.",
        )

    def test_a_rejection_reason_falls_back_to_the_exception_text_and_never_to_nothing(self):
        from agent.agent import _rejection_reason

        # A plugin's own ValueError is more use than a generic sentence, and an
        # empty sentence is the one thing that must never reach a person.
        self.assertEqual(
            _rejection_reason(ValueError("speed must be a float for sonic-3")),
            "speed must be a float for sonic-3",
        )
        self.assertEqual(_rejection_reason(RuntimeError()), "RuntimeError while building the voice profile.")


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
        self.assertEqual(settings["connect_max_retry"], 0)
        self.assertEqual(settings["max_tool_steps"], 1)

    def test_no_thinking_level_is_invented_for_the_environment_profile(self):
        """The regression: an unset level must produce no level, not `minimal`.

        `minimal` used to be the hardcoded default, which is the lowest-latency
        *labelled* level and therefore looked like the safe choice. It is not:
        `gemini-3.8-live` rejects `thinking_config` outright, and
        `gemini-3.8-live-extended-thinking` rejects `minimal`. Sending nothing is
        both the fastest option and the only one valid for every current Live
        model, because the model then applies its own default.
        """
        with patch.dict(os.environ, {}, clear=True):
            settings = gemini_settings()
        self.assertNotIn("thinking_config", settings)

    def test_an_explicit_thinking_level_is_reported(self):
        env = {"GEMINI_THINKING_LEVEL": "high", "GEMINI_INCLUDE_THOUGHTS": "true"}
        with patch.dict(os.environ, env, clear=True):
            settings = gemini_settings()
        self.assertEqual(settings["thinking_config"]["thinking_level"], "high")
        self.assertTrue(settings["thinking_config"]["include_thoughts"])

    def test_include_thoughts_alone_reports_no_level(self):
        """The companion rides inside `thinking_config` and is meaningless alone.

        Emitting the group with a level the user never set would reintroduce the
        invented default through the back door.
        """
        with patch.dict(os.environ, {"GEMINI_INCLUDE_THOUGHTS": "true"}, clear=True):
            settings = gemini_settings()
        self.assertNotIn("thinking_config", settings)

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


class ToolResultDisclosureTests(unittest.TestCase):
    """What a tool result is allowed to show, and where.

    The transcript accumulates and the toast does not, so the two need different
    content. These tests exist because the failure is invisible: a payload shown
    inline still *looks* like it works, it just quietly puts retrieved text into
    the middle of a conversation, which is the shape a prompt injection wants.
    """

    @staticmethod
    def _run(name: str, result: str, status: str = "done") -> dict:
        publisher = RecordingPublisher()
        bridge = ToolEventBridge(publisher)  # type: ignore[arg-type]
        bridge.handle(
            SimpleNamespace(
                update=SimpleNamespace(
                    type="tool_call_started",
                    function_call=SimpleNamespace(call_id="c1", name=name, arguments="{}"),
                )
            )
        )
        bridge.handle(
            SimpleNamespace(
                update=SimpleNamespace(type="tool_call_ended", call_id="c1", status=status, message=result)
            )
        )
        return publisher.records[-1]

    def test_the_transcript_summary_contains_no_result_text(self):
        record = self._run("get_news", '{"headlines":["Big story lands","Second story"]}')
        summary = str(record["summary"])
        self.assertNotIn("Big story lands", summary)
        self.assertIn("headline", summary)

    def test_the_result_is_still_delivered_for_the_toast(self):
        record = self._run("get_news", '{"headlines":["Big story lands","Second story"]}')
        self.assertIn("Big story lands", str(record["result"]))

    def test_a_count_is_reported_rather_than_the_content(self):
        record = self._run("get_news", '{"headlines":["a","b","c"]}')
        self.assertIn("3 headlines", str(record["summary"]))

    def test_a_single_item_is_not_pluralised(self):
        record = self._run("get_news", '{"headlines":["only one"]}')
        self.assertIn("1 headline", str(record["summary"]))
        self.assertNotIn("1 headlines", str(record["summary"]))

    def test_weather_is_summarised_by_place(self):
        record = self._run("get_weather", '{"place":"Dhaka","now":"28C","today":"28-33"}')
        self.assertIn("Dhaka", str(record["summary"]))
        self.assertNotIn("28-33", str(record["summary"]))

    def test_unavailable_conditions_are_stated_plainly(self):
        record = self._run("get_weather", '{"place":"Nowhere","now":"unavailable"}')
        self.assertIn("No current conditions", str(record["summary"]))

    def test_unstructured_output_is_described_by_size_not_content(self):
        record = self._run("open_app", "a plain sentence of prose here")
        self.assertIn("words", str(record["summary"]))
        self.assertNotIn("prose", str(record["summary"]))

    def test_a_failed_call_carries_no_result(self):
        # A failure message is the tool explaining itself, which is worth
        # showing; it is not a payload.
        record = self._run("search_web", "Web search is unavailable right now.", status="error")
        self.assertEqual(record["status"], "failed")
        self.assertIn("unavailable", str(record["message"]))

    def test_an_empty_result_does_not_produce_a_silly_summary(self):
        record = self._run("search_web", "")
        self.assertTrue(str(record["summary"]).strip())


class AppLauncherTests(unittest.TestCase):
    # Each test patches the installed-app index out. These three assert the
    # legacy PATH/startfile chain on its own terms, and a developer's Start
    # Menu -- or a real PowerShell probe -- must never be what decides whether
    # they pass. The index branch is `test_app_index.py`'s subject.
    INDEX_OUT = patch.object(apps.app_index, "resolve", return_value=None)

    def test_path_launch_reports_success_without_waiting(self):
        with self.INDEX_OUT, patch.object(apps.shutil, "which", return_value="C:/apps/notepad.exe"), patch.object(apps, "spawn") as spawn:
            self.assertEqual(apps.launch("notepad"), "Opened notepad.")
        spawn.assert_called_once_with(["C:/apps/notepad.exe"])

    def test_missing_app_has_a_specific_error(self):
        with self.INDEX_OUT, patch.object(apps.shutil, "which", return_value=None), patch.object(apps.sys, "platform", "win32"), patch.object(apps.os, "startfile", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ToolError, "couldn't find an app"):
                apps.launch("missing-app")

    def test_found_app_failure_is_distinct_from_missing_app(self):
        with self.INDEX_OUT, patch.object(apps.shutil, "which", return_value="C:/apps/notepad.exe"), patch.object(apps.sys, "platform", "linux"), patch.object(apps, "spawn", side_effect=PermissionError("denied")):
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
