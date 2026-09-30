"""Tests for the Phase 3 configuration wiring in the worker.

The contract being protected: a saved configuration governs the session when it
is valid, and the environment takes over when it is not. Losing a voice session
to a bad settings file would be a much worse outcome than ignoring it.
"""

import asyncio
import json
import unittest
from pathlib import Path
from unittest import mock

from agent import agent as agent_module
from agent.settings.config_store import SOURCE_UI
from agent.validate_config import describe, main, validate_text
from agent.settings.validation import CONFIG_VERSION

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class RecordingPublisher:
    def __init__(self):
        self.records: list[dict] = []

    def emit(self, event_type, **payload):
        record = {"type": event_type, **payload}
        self.records.append(record)
        return record

    def of(self, event_type: str) -> list[dict]:
        return [record for record in self.records if record["type"] == event_type]


class DescribeTests(unittest.TestCase):
    def test_describe_reports_the_environment_source(self):
        payload = describe()
        self.assertIn(payload["source"], {"env", "ui"})
        self.assertEqual(payload["document"]["version"], CONFIG_VERSION)

    def test_describe_never_exposes_credentials(self):
        blob = json.dumps(describe()).lower()
        for marker in ("sk-", "bearer ", "eyj"):
            self.assertNotIn(marker, blob)

    def test_describe_reports_the_selected_pipeline(self):
        # Point the lookup at a path that does not exist, so this exercises the
        # environment profile it is about. Without it the command reads whatever
        # the developer has saved, which is exactly the behaviour fixed by the
        # desktop-directory lookup -- and made this test depend on the machine.
        with mock.patch.dict("os.environ", {"LUMINE_PIPELINE": "legacy_cascade"}, clear=False), mock.patch.dict(
            "os.environ", {"LUMINE_CONFIG_PATH": str(FIXTURES / "does-not-exist.json")}, clear=False
        ):
            payload = describe()
        kinds = {profile["kind"] for profile in payload["document"]["profiles"]}
        self.assertEqual(kinds, {"pipeline"})


class ValidateCliTests(unittest.TestCase):
    def test_a_valid_document_passes(self):
        raw = (FIXTURES / "lumine.config.example.json").read_text(encoding="utf-8")
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
            "agent.validate_config.env_credential_status",
            return_value={"google": True, "groq": True, "cartesia": True, "livekit": True},
        ):
            result = validate_text(raw)
        self.assertTrue(result["ok"])
        self.assertEqual(result["errorCount"], 0)

    def test_malformed_json_is_reported_not_raised(self):
        result = validate_text("{not json")
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"][0]["code"], "document.unreadable")

    def test_the_result_is_json_serializable(self):
        json.dumps(validate_text("{}"))

    def test_describe_flag_emits_json(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(main(["--describe"]), 0)
        payload = json.loads(buffer.getvalue())
        self.assertIn("source", payload)
        self.assertIn("document", payload)


class SessionConfigWiringTests(unittest.TestCase):
    """`build_session_components` decides which layer governs a session."""

    def _build(self, document, interruption_mode="barge_in"):
        publisher = RecordingPublisher()
        with mock.patch.object(agent_module, "effective_document", return_value=(document, SOURCE_UI, [])), \
             mock.patch.object(agent_module, "build_pipeline") as env_build, \
             mock.patch.object(agent_module, "build_resolved") as ui_build:
            ui_build.return_value = (_components("from-ui"), [])
            env_build.return_value = _components("from-env")
            result = asyncio.run(
                agent_module.build_session_components(publisher, "room-1", interruption_mode)
            )
        return result, publisher, env_build, ui_build

    def test_a_valid_saved_profile_governs_the_session(self):
        document = json.loads((FIXTURES / "lumine.config.example.json").read_text(encoding="utf-8"))
        result, publisher, env_build, ui_build = self._build(document)
        self.assertEqual(result.profile, "from-ui")
        ui_build.assert_called_once()
        env_build.assert_not_called()
        self.assertEqual(publisher.of("config_applied")[0]["source"], "ui")

    def test_a_rejected_saved_profile_falls_back_to_the_environment(self):
        # A saved document that cannot build must not cost the user their voice.
        from agent.pipeline.pipeline_factory import ConfigurationRejected
        from agent.settings.validation import diagnostic

        document = json.loads((FIXTURES / "lumine.config.example.json").read_text(encoding="utf-8"))
        publisher = RecordingPublisher()
        rejected = ConfigurationRejected([diagnostic("error", "realtime.custom_tts_unsupported", "realtime.output.mode", "nope")])
        with mock.patch.object(agent_module, "effective_document", return_value=(document, SOURCE_UI, [])), \
             mock.patch.object(agent_module, "build_pipeline", return_value=_components("from-env")) as env_build, \
             mock.patch.object(agent_module, "build_resolved", side_effect=rejected):
            result = asyncio.run(agent_module.build_session_components(publisher, "room-1", "barge_in"))
        self.assertEqual(result.profile, "from-env")
        env_build.assert_called_once()
        self.assertTrue(publisher.of("config_rejected"))

    def test_a_dangling_profile_reference_falls_back(self):
        document = {"version": 1, "activeProfileId": "ghost", "providers": {}, "profiles": []}
        result, publisher, env_build, ui_build = self._build(document)
        self.assertEqual(result.profile, "from-env")
        env_build.assert_called_once()
        ui_build.assert_not_called()
        self.assertTrue(publisher.of("config_rejected"))

    def test_the_environment_path_does_not_consult_the_saved_document(self):
        publisher = RecordingPublisher()
        with mock.patch.object(agent_module, "effective_document", return_value=({"version": 1}, "env", [])), \
             mock.patch.object(agent_module, "build_pipeline", return_value=_components("from-env")):
            result = asyncio.run(agent_module.build_session_components(publisher, "room-1", "barge_in"))
        self.assertEqual(result.profile, "from-env")
        applied = publisher.of("config_applied")
        self.assertEqual(applied[0]["source"], "env")

    def test_the_interruption_mode_is_passed_through_to_the_resolver(self):
        document = json.loads((FIXTURES / "lumine.config.example.json").read_text(encoding="utf-8"))
        publisher = RecordingPublisher()
        with mock.patch.object(agent_module, "effective_document", return_value=(document, SOURCE_UI, [])), \
             mock.patch.object(agent_module, "build_resolved", return_value=(_components("ui"), [])) as ui_build:
            asyncio.run(agent_module.build_session_components(publisher, "room-1", "finish_response"))
        resolved = ui_build.call_args.args[0]
        self.assertEqual(resolved.interruption_mode, "finish_response")


class JobMetadataWiringTests(unittest.TestCase):
    def test_the_entrypoint_reads_the_profile_reference_from_metadata(self):
        from agent.settings.session_preferences import job_preferences_from_metadata

        preferences = job_preferences_from_metadata(json.dumps({"profile_id": "custom", "interruption_mode": "barge_in"}))
        self.assertEqual(preferences.profile_id, "custom")

    def test_metadata_without_a_profile_still_resolves(self):
        from agent.settings.session_preferences import interruption_mode_from_metadata

        self.assertEqual(interruption_mode_from_metadata(json.dumps({"interruption_mode": "barge_in"})), "barge_in")


def _components(profile: str):
    from agent.pipeline.pipeline_factory import PipelineComponents

    return PipelineComponents(
        profile=profile,
        model_name="test-model",
        interruption_mode="barge_in",
        llm=object(),
        session_kwargs={},
    )


if __name__ == "__main__":
    unittest.main()
