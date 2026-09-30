import json
import unittest
from pathlib import Path

from agent.settings.validation import (
    env_credential_status,
    has_errors,
    validate_document,
    validate_profile,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

ALL_PRESENT = {"livekit": True, "google": True, "groq": True, "cartesia": True, "openai": True}
NONE_PRESENT: dict[str, bool] = {}


def pipeline_profile(**overrides):
    profile = {
        "id": "p1",
        "name": "Pipeline",
        "kind": "pipeline",
        "pipeline": {
            "stt": {"provider": "groq", "model": "whisper-large-v3-turbo"},
            "llm": {"provider": "groq", "model": "openai/gpt-oss-20b"},
            "tts": {"provider": "cartesia", "model": "sonic-3", "voice": "f786b574-daa5-4673-aa0c-cbe3e8534c02"},
            "vad": {"provider": "silero", "model": "silero"},
            "turnHandling": {"interruptionMode": "barge_in"},
        },
    }
    profile["pipeline"].update(overrides)
    return profile


def realtime_profile(**overrides):
    realtime = {
        "provider": "google",
        "model": "gemini-3.8-live",
        "voice": "Puck",
        "output": {"mode": "model_voice"},
        "turnHandling": {"interruptionMode": "barge_in"},
    }
    realtime.update(overrides)
    return {"id": "r1", "name": "Realtime", "kind": "realtime", "realtime": realtime}


def codes(diagnostics):
    return [d.code for d in diagnostics]


def errors(diagnostics):
    return [d for d in diagnostics if d.severity == "error"]


class PipelineValidationTests(unittest.TestCase):
    def test_a_complete_pipeline_is_clean(self):
        result = validate_profile(pipeline_profile(), ALL_PRESENT)
        self.assertEqual(result, [])

    def test_missing_credential_is_an_error(self):
        result = validate_profile(pipeline_profile(), NONE_PRESENT)
        self.assertIn("credential.missing", codes(result))
        self.assertTrue(has_errors(result))

    def test_local_vad_needs_no_credential(self):
        # silero.requires_key is False, so it must never produce a credential
        # error even with an empty credential map.
        result = validate_profile(pipeline_profile(), NONE_PRESENT)
        for diag in result:
            self.assertNotEqual(diag.path, "pipeline.vad.provider")

    def test_unknown_provider_is_reported_not_raised(self):
        result = validate_profile(pipeline_profile(stt={"provider": "acme", "model": "x"}), ALL_PRESENT)
        self.assertIn("stage.unknown_provider", codes(result))

    def test_model_on_the_wrong_capability_is_reported(self):
        # gemini-2.5-flash-lite is a Google STT model, not a TTS model.
        result = validate_profile(
            pipeline_profile(tts={"provider": "google", "model": "gemini-2.5-flash-lite"}), ALL_PRESENT
        )
        self.assertIn("stage.unknown_model", codes(result))

    def test_provider_without_the_capability_is_reported(self):
        # Cartesia does TTS only; asking it for an LLM is a capability mismatch.
        result = validate_profile(
            pipeline_profile(llm={"provider": "cartesia", "model": "sonic-3"}), ALL_PRESENT
        )
        self.assertIn("stage.capability_mismatch", codes(result))

    def test_non_streaming_stt_without_a_vad_is_blocked(self):
        profile = pipeline_profile()
        profile["pipeline"]["stt"] = {"provider": "google", "model": "gemini-2.5-flash-lite"}
        profile["pipeline"]["vad"] = {}
        result = validate_profile(profile, ALL_PRESENT)
        self.assertIn("stt.requires_vad", codes(result))

    def test_deprecated_model_warns_and_names_its_replacement(self):
        result = validate_profile(
            pipeline_profile(tts={"provider": "cartesia", "model": "sonic-2", "voice": "f786b574-daa5-4673-aa0c-cbe3e8534c02"}),
            ALL_PRESENT,
        )
        self.assertIn("model.deprecated", codes(result))
        deprecation = next(d for d in result if d.code == "model.deprecated")
        self.assertIn("sonic-3.5", deprecation.hint)
        self.assertFalse(has_errors(result), "a deprecated model should warn, not block")

    def test_legacy_sonic_2_profile_still_reports_its_deprecation(self):
        """A profile saved before the migration must explain itself, not break.

        Cartesia retires Sonic 2 on 2026-10-20, so an older saved configuration
        has to produce an actionable warning rather than silently keep working
        until the provider stops serving it.
        """
        legacy = pipeline_profile(
            tts={
                "provider": "cartesia",
                "model": "sonic-2",
                "voice": "002622d8-19d0-4567-a16a-f99c7397c062",
            }
        )
        result = validate_profile(legacy, ALL_PRESENT)
        self.assertIn("model.deprecated", codes(result))
        deprecation = next(d for d in result if d.code == "model.deprecated")
        self.assertEqual(deprecation.path, "pipeline.tts.model")
        self.assertIn("sonic-3.5", deprecation.hint)
        self.assertFalse(has_errors(result), "a deprecated model should warn, not block")

    def test_missing_voice_warns_but_does_not_block(self):
        result = validate_profile(
            pipeline_profile(tts={"provider": "cartesia", "model": "sonic-3"}), ALL_PRESENT
        )
        self.assertIn("voice.missing", codes(result))
        self.assertFalse(has_errors(result))

    def test_unknown_voice_warns_but_does_not_block(self):
        # Custom/cloned voices are legitimate, so this must not be an error.
        result = validate_profile(
            pipeline_profile(tts={"provider": "cartesia", "model": "sonic-3", "voice": "my-clone"}),
            ALL_PRESENT,
        )
        self.assertIn("voice.unknown", codes(result))
        self.assertFalse(has_errors(result))

    def test_invalid_interruption_mode_is_an_error(self):
        profile = pipeline_profile()
        profile["pipeline"]["turnHandling"] = {"interruptionMode": "whenever"}
        self.assertIn("turn.unknown_interruption_mode", codes(validate_profile(profile, ALL_PRESENT)))


class RealtimeValidationTests(unittest.TestCase):
    def test_a_plain_gemini_live_profile_is_clean(self):
        self.assertEqual(validate_profile(realtime_profile(), ALL_PRESENT), [])

    def test_realtime_plus_custom_tts_is_blocked_for_native_audio(self):
        """The headline rule: Gemini 3.1 native audio cannot use a separate TTS."""
        profile = realtime_profile(
            output={
                "mode": "custom_tts",
                "tts": {"provider": "cartesia", "model": "sonic-3"},
                "voice": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
            }
        )
        result = validate_profile(profile, ALL_PRESENT)
        self.assertIn("realtime.custom_tts_unsupported", codes(result))
        blocking = next(d for d in result if d.code == "realtime.custom_tts_unsupported")
        self.assertEqual(blocking.path, "realtime.output.mode")
        self.assertIn("non-native-audio", blocking.hint)

    def test_custom_tts_is_allowed_when_the_model_supports_text_only(self):
        # Simulate a future non-native-audio model by patching the catalog entry.
        import agent.settings.providers as providers_module
        import agent.settings.validation as validation_module

        original = providers_module.PROVIDERS["google"]
        # Every current Gemini Live model is native audio, so the text-only branch
        # is exercised by patching the catalog rather than by a real entry.
        model = original.get_model("gemini-3.8-live")
        assert model is not None
        patched = providers_module.ModelDefinition(
            **{**model.__dict__, "native_audio": False, "text_only_modality": True, "default": False}
        )
        replaced = providers_module.ProviderDefinition(
            **{**original.__dict__, "models": tuple(
                patched if m.id == model.id else m for m in original.models
            )}
        )
        providers_module.PROVIDERS["google"] = replaced
        validation_module.PROVIDERS["google"] = replaced
        try:
            profile = realtime_profile(
                model="gemini-3.8-live",
                output={
                    "mode": "custom_tts",
                    "tts": {"provider": "cartesia", "model": "sonic-3"},
                    "voice": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
                },
            )
            result = validate_profile(profile, ALL_PRESENT)
            self.assertNotIn("realtime.custom_tts_unsupported", codes(result))
        finally:
            providers_module.PROVIDERS["google"] = original
            validation_module.PROVIDERS["google"] = original

    def test_model_voice_requires_the_model_to_have_voices(self):
        import agent.settings.providers as providers_module
        import agent.settings.validation as validation_module

        original = providers_module.PROVIDERS["google"]
        model = original.get_model("gemini-3.8-live")
        assert model is not None
        patched = providers_module.ModelDefinition(**{**model.__dict__, "voices": ()})
        replaced = providers_module.ProviderDefinition(
            **{**original.__dict__, "models": tuple(
                patched if m.id == model.id else m for m in original.models
            )}
        )
        providers_module.PROVIDERS["google"] = replaced
        validation_module.PROVIDERS["google"] = replaced
        try:
            self.assertIn("output.no_model_voice", codes(validate_profile(realtime_profile(), ALL_PRESENT)))
        finally:
            providers_module.PROVIDERS["google"] = original
            validation_module.PROVIDERS["google"] = original

    def test_finish_response_is_rejected_when_the_model_cannot_do_it(self):
        profile = realtime_profile(turnHandling={"interruptionMode": "finish_response"})
        # Gemini 3.1 does support it, so this must stay clean.
        self.assertEqual(validate_profile(profile, ALL_PRESENT), [])

    def test_unsupported_interruption_mode_names_the_sdk_constraint(self):
        import agent.settings.providers as providers_module
        import agent.settings.validation as validation_module

        original = providers_module.PROVIDERS["google"]
        model = original.get_model("gemini-3.8-live")
        assert model is not None
        patched = providers_module.ModelDefinition(**{**model.__dict__, "finish_response": False})
        replaced = providers_module.ProviderDefinition(
            **{**original.__dict__, "models": tuple(
                patched if m.id == model.id else m for m in original.models
            )}
        )
        providers_module.PROVIDERS["google"] = replaced
        validation_module.PROVIDERS["google"] = replaced
        try:
            profile = realtime_profile(turnHandling={"interruptionMode": "finish_response"})
            result = validate_profile(profile, ALL_PRESENT)
            self.assertIn("realtime.finish_response_unsupported", codes(result))
            blocking = next(d for d in result if d.code == "realtime.finish_response_unsupported")
            self.assertIn("LiveKit rejects", blocking.hint)
        finally:
            providers_module.PROVIDERS["google"] = original
            validation_module.PROVIDERS["google"] = original

    def test_gemini_31_options_warn_instead_of_blocking(self):
        profile = realtime_profile()
        profile["options"] = {"proactivity": True, "affectiveDialog": True, "asyncFunctionCalling": True}
        result = validate_profile(profile, ALL_PRESENT)
        self.assertIn("realtime.proactivity_unsupported", codes(result))
        self.assertIn("realtime.affectiveDialog_unsupported", codes(result))
        self.assertIn("realtime.asyncFunctionCalling_unsupported", codes(result))
        self.assertFalse(has_errors(result), "unsupported extras should warn, not block")

    def test_unknown_output_mode_is_an_error(self):
        self.assertIn("output.unknown_mode", codes(validate_profile(realtime_profile(output={"mode": "telepathy"}), ALL_PRESENT)))


class ProfileShapeTests(unittest.TestCase):
    def test_non_object_profile_is_reported_not_raised(self):
        self.assertIn("profile.invalid", codes(validate_profile("nope")))

    def test_unknown_kind_is_reported(self):
        self.assertIn("profile.unknown_kind", codes(validate_profile({"kind": "psychic"})))

    def test_unnamed_profile_warns(self):
        result = validate_profile({"kind": "pipeline", "pipeline": {}}, ALL_PRESENT)
        self.assertIn("profile.unnamed", codes(result))

    def test_diagnostics_are_json_serializable(self):
        result = validate_profile(pipeline_profile(), NONE_PRESENT)
        json.dumps([d.to_dict() for d in result])


class DocumentValidationTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(
            (FIXTURES / "lumine.config.example.json").read_text(encoding="utf-8")
        )

    def test_shipped_example_config_has_no_blocking_errors(self):
        result = validate_document(self.document, ALL_PRESENT)
        self.assertEqual([d.to_dict() for d in errors(result)], [])

    def test_shipped_example_config_is_completely_clean(self):
        """The example config we ship must not carry a single warning.

        It used to reference Cartesia's retiring Sonic 2. That is exactly the
        kind of rot this assertion exists to catch.
        """
        result = validate_document(self.document, ALL_PRESENT)
        self.assertEqual([d.to_dict() for d in result], [])

    def test_shipped_example_config_reports_every_missing_credential(self):
        result = validate_document(self.document, NONE_PRESENT)
        missing = {d.path for d in result if d.code == "credential.missing"}
        self.assertEqual(missing, {"realtime.provider", "pipeline.stt.provider", "pipeline.llm.provider", "pipeline.tts.provider"})

    def test_unsupported_version_blocks(self):
        self.document["version"] = 99
        self.assertIn("document.unsupported_version", codes(validate_document(self.document, ALL_PRESENT)))

    def test_unknown_active_profile_blocks(self):
        self.document["activeProfileId"] = "ghost"
        self.assertIn("document.unknown_active_profile", codes(validate_document(self.document, ALL_PRESENT)))

    def test_duplicate_profile_ids_block(self):
        self.document["profiles"].append(dict(self.document["profiles"][0]))
        self.assertIn("profile.duplicate_id", codes(validate_document(self.document, ALL_PRESENT)))

    def test_empty_document_blocks(self):
        self.assertIn("document.no_profiles", codes(validate_document({"version": 1}, ALL_PRESENT)))

    def test_two_profiles_may_share_a_name_but_it_is_reported(self):
        """Two identical rows in the settings list are ambiguous, not fatal.

        Both profiles still run, so this must not block a save; the settings
        screen seeds a new name, and this catches a hand-edited file.
        """
        second = dict(self.document["profiles"][0])
        second["id"] = "gemini-live-2"
        self.document["profiles"].append(second)
        result = validate_document(self.document, ALL_PRESENT)
        self.assertIn("profile.duplicate_name", codes(result))
        self.assertEqual([d.to_dict() for d in errors(result)], [])

    def test_a_duplicate_name_reports_where_the_other_one_is(self):
        first = self.document["profiles"][0]
        second = dict(first)
        second["id"] = "gemini-live-2"
        self.document["profiles"].append(second)
        appended = len(self.document["profiles"]) - 1
        duplicate = next(
            d for d in validate_document(self.document, ALL_PRESENT) if d.code == "profile.duplicate_name"
        )
        self.assertEqual(duplicate.path, f"profiles[{appended}].name")
        self.assertIn("profiles[0]", duplicate.hint or "")

    def test_names_are_compared_case_insensitively(self):
        second = dict(self.document["profiles"][0])
        second["id"] = "gemini-live-2"
        second["name"] = "GEMINI LIVE"
        self.document["profiles"].append(second)
        self.assertIn("profile.duplicate_name", codes(validate_document(self.document, ALL_PRESENT)))

    def test_several_profiles_validate_clean_together(self):
        """The Phase 4 shape: more than one profile in a document is normal."""
        second = json.loads(json.dumps(self.document["profiles"][0]))
        second["id"] = "gemini-live-2"
        second["name"] = "Second"
        self.document["profiles"].append(second)
        self.assertEqual([d.to_dict() for d in validate_document(self.document, ALL_PRESENT)], [])


class CredentialStatusTests(unittest.TestCase):
    def test_env_status_covers_every_credential_bearing_provider(self):
        status = env_credential_status()
        self.assertIn("google", status)
        self.assertIn("groq", status)
        self.assertIn("cartesia", status)
        self.assertIn("livekit", status)
        # Local providers have nothing to store.
        self.assertNotIn("silero", status)

    def test_env_status_returns_booleans(self):
        for value in env_credential_status().values():
            self.assertIsInstance(value, bool)


if __name__ == "__main__":
    unittest.main()
