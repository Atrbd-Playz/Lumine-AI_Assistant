"""Tests for configuration precedence, the environment profile, and job metadata.

These cover the Phase 1 promise: with no saved document, resolution falls through
to the environment and behaves exactly as before.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import config_store
from agent.config_store import (
    APP_IDENTIFIER,
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_FILENAME,
    SOURCE_ENV,
    SOURCE_UI,
    active_profile,
    config_path,
    config_path_candidates,
    config_search_summary,
    effective_document,
    env_document,
    env_profile,
    load_document,
)
from agent.pipeline_config import (
    DEFAULT_CARTESIA_TTS_MODEL,
    cartesia_tts_settings,
    gemini_settings,
)
from agent.providers import get_model, get_provider
from agent.session_preferences import (
    JobPreferences,
    interruption_mode_from_metadata,
    job_preferences_from_metadata,
    normalize_profile_id,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = FIXTURES.parent.parent.parent


class CartesiaTtsSettingsTests(unittest.TestCase):
    def test_default_model_is_no_longer_the_retiring_sonic_2(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cartesia_tts_settings()["model"], "sonic-3")
        self.assertNotEqual(DEFAULT_CARTESIA_TTS_MODEL, "sonic-2")

    def test_lumine_voice_is_preserved_across_the_model_change(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            settings = cartesia_tts_settings()
        self.assertEqual(settings["voice"], "002622d8-19d0-4567-a16a-f99c7397c062")
        self.assertEqual(settings["language"], "en")
        self.assertAlmostEqual(settings["speed"], 0.95)

    def test_every_field_is_environment_overridable(self):
        env = {
            "CARTESIA_TTS_MODEL": "sonic-3.5",
            "CARTESIA_TTS_VOICE": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
            "CARTESIA_TTS_LANGUAGE": "fr",
            "CARTESIA_TTS_SPEED": "1.2",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            settings = cartesia_tts_settings()
        self.assertEqual(settings["model"], "sonic-3.5")
        self.assertEqual(settings["voice"], "f786b574-daa5-4673-aa0c-cbe3e8534c02")
        self.assertEqual(settings["language"], "fr")
        self.assertAlmostEqual(settings["speed"], 1.2)

    def test_out_of_range_speed_degrades_the_way_the_parsers_already_do(self):
        # _float_env is deliberately asymmetric and shared with the existing
        # retry/temperature helpers: above the maximum clamps to it, below the
        # minimum falls back to the default. A mistyped value degrades rather
        # than breaking speech, which is the behaviour we want here.
        with mock.patch.dict(os.environ, {"CARTESIA_TTS_SPEED": "99"}, clear=True):
            self.assertAlmostEqual(cartesia_tts_settings()["speed"], 2.0)
        with mock.patch.dict(os.environ, {"CARTESIA_TTS_SPEED": "0.01"}, clear=True):
            self.assertAlmostEqual(cartesia_tts_settings()["speed"], 0.95)

    def test_unparseable_speed_falls_back_to_the_default(self):
        with mock.patch.dict(os.environ, {"CARTESIA_TTS_SPEED": "fast"}, clear=True):
            self.assertAlmostEqual(cartesia_tts_settings()["speed"], 0.95)

    def test_blank_model_falls_back_to_the_default(self):
        with mock.patch.dict(os.environ, {"CARTESIA_TTS_MODEL": "  "}, clear=True):
            self.assertEqual(cartesia_tts_settings()["model"], "sonic-3")


class RuntimeDefaultsStayCataloguedTests(unittest.TestCase):
    """A runtime default must never point at a model the provider has withdrawn.

    This is the guard that keeps the Sonic 2 retirement from coming back.
    """

    def test_cartesia_default_model_is_available_in_the_catalog(self):
        model = get_model("cartesia", DEFAULT_CARTESIA_TTS_MODEL, "tts")
        self.assertIsNotNone(model, "the default TTS model is not in the catalog")
        self.assertEqual(model.status, "available")

    def test_gemini_default_model_is_available_in_the_catalog(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            model_id = gemini_settings()["model"]
        model = get_model("google", model_id, "realtime")
        self.assertIsNotNone(model, f"the default realtime model {model_id} is not catalogued")
        self.assertEqual(model.status, "available")

    def test_default_gemini_voice_is_in_the_voice_catalog(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            voice_id = gemini_settings()["voice"]
        google = get_provider("google")
        self.assertIsNotNone(
            google.get_voice(voice_id),
            f"the default Gemini voice {voice_id!r} is not in the voice catalog",
        )

    def test_default_cartesia_voice_is_in_the_voice_catalog(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            voice_id = cartesia_tts_settings()["voice"]
        cartesia = get_provider("cartesia")
        self.assertIsNotNone(cartesia.get_voice(voice_id))


class EnvProfileTests(unittest.TestCase):
    def test_env_profile_reports_the_realtime_stack_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            profile = env_profile()
        self.assertEqual(profile["kind"], "realtime")
        self.assertEqual(profile["realtime"]["provider"], "google")

    def test_env_profile_reports_the_pipeline_stack_when_selected(self):
        with mock.patch.dict(os.environ, {"LUMINE_PIPELINE": "legacy_cascade"}, clear=True):
            profile = env_profile()
        self.assertEqual(profile["kind"], "pipeline")
        self.assertEqual(profile["pipeline"]["stt"]["provider"], "groq")
        self.assertEqual(profile["pipeline"]["tts"]["provider"], "cartesia")

    def test_env_profile_is_read_only_by_construction(self):
        # The ids mark the profile as derived, so the UI can present it as
        # "managed by agent/.env" and refuse to save over it.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(env_profile()["id"].startswith("env-"))

    def test_env_document_validates_clean(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            document = env_document()
        from agent.validation import validate_document

        self.assertEqual(validate_document(document), [])

    def test_env_document_is_a_valid_document_at_the_current_version(self):
        from agent.validation import CONFIG_VERSION

        self.assertEqual(env_document()["version"], CONFIG_VERSION)


class PrecedenceTests(unittest.TestCase):
    def test_config_path_honours_the_environment_override(self):
        with mock.patch.dict(os.environ, {CONFIG_PATH_ENV: "custom/where.json"}, clear=True):
            self.assertEqual(str(config_path()), "custom/where.json".replace("/", os.sep))

    def test_config_path_defaults_next_to_the_agent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config_path().name, "lumine.config.json")

    def test_the_override_is_the_only_candidate(self):
        # An explicit path must not fall through to another location, or a test
        # pointed at a fixture could read the developer's real settings.
        with mock.patch.dict(os.environ, {CONFIG_PATH_ENV: "custom/where.json"}, clear=True):
            candidates = [str(p) for p in config_path_candidates()]
        self.assertEqual(candidates, [str(Path("custom") / "where.json")])

    def test_the_desktop_app_location_is_searched_before_the_agent_directory(self):
        """The two halves have to agree without an environment variable.

        The desktop app writes to its local application data directory. A worker
        started outside Tauri — the documented ``lk agent dev`` workflow — gets
        no ``LUMINE_CONFIG_PATH``, so if that directory is not searched the
        worker falls back to the environment and the user's saved settings look
        like they were ignored. That is the bug this ordering fixes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, self._platform_data_env(tmp), clear=True):
                candidates = config_path_candidates()
                data_dir = config_store.app_local_data_dir()

        self.assertIsNotNone(data_dir, "the desktop location could not be determined")
        self.assertEqual(candidates[0], data_dir / DEFAULT_CONFIG_FILENAME)
        self.assertEqual(candidates[-1], config_store.AGENT_DIR / DEFAULT_CONFIG_FILENAME)
        self.assertGreaterEqual(len(candidates), 2)
        self.assertEqual(candidates[0].parent.name, APP_IDENTIFIER)

    @staticmethod
    def _platform_data_env(root: str) -> dict[str, str]:
        """Just the variable this platform's app data directory comes from."""
        if sys.platform == "win32":
            return {"LOCALAPPDATA": root}
        if sys.platform == "darwin":
            return {"HOME": root}
        return {"XDG_DATA_HOME": root}

    def test_the_first_existing_candidate_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            first.mkdir()
            second.mkdir()
            (second / DEFAULT_CONFIG_FILENAME).write_text("{}", encoding="utf-8")
            candidates = [first / DEFAULT_CONFIG_FILENAME, second / DEFAULT_CONFIG_FILENAME]
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
                "agent.config_store.config_path_candidates", return_value=candidates
            ):
                self.assertEqual(config_path(), second / DEFAULT_CONFIG_FILENAME)

    def test_the_primary_candidate_is_reported_when_none_exist(self):
        # An error message pointing at a skipped candidate is worse than one
        # pointing at where the file would be created.
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope" / DEFAULT_CONFIG_FILENAME
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
                "agent.config_store.config_path_candidates", return_value=[missing]
            ):
                self.assertEqual(config_path(), missing)

    def test_the_search_summary_names_the_location(self):
        with mock.patch.dict(os.environ, {CONFIG_PATH_ENV: "somewhere/else.json"}, clear=True):
            summary = config_search_summary()
        self.assertIn("somewhere", summary)
        self.assertIn(CONFIG_PATH_ENV, summary)

    def test_the_app_identifier_matches_the_tauri_bundle(self):
        """The worker can only find the file if it computes Tauri's directory.

        The identifier lives in ``tauri.conf.json``. A rename there without a
        matching change here splits the two sides silently, so this reads the
        manifest rather than trusting the constant.
        """
        manifest = REPO_ROOT / "lumine-ui" / "src-tauri" / "tauri.conf.json"
        self.assertTrue(manifest.exists(), f"expected {manifest}")
        config = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(config.get("identifier"), APP_IDENTIFIER)

    def test_the_search_survives_a_stripped_environment(self):
        """A missing home directory must not stop the worker from starting.

        It only means the desktop location cannot be searched; the agent
        directory is still a candidate.
        """
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(Path, "home", side_effect=RuntimeError("no home")):
                candidates = config_path_candidates()
        self.assertEqual([c.name for c in candidates], [DEFAULT_CONFIG_FILENAME])
        self.assertEqual(len(candidates), 1, "the agent directory is still a fallback")

    def test_absent_document_falls_back_to_the_environment(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            document, source, diagnostics = effective_document(Path("does-not-exist.json"))
        self.assertEqual(source, SOURCE_ENV)
        self.assertEqual(diagnostics, [])
        self.assertTrue(document["activeProfileId"].startswith("env-"))

    def test_a_valid_document_wins_over_the_environment(self):
        path = FIXTURES / "lumine.config.example.json"
        with mock.patch.dict(os.environ, {}, clear=True):
            document, source, diagnostics = effective_document(path)
        self.assertEqual(source, SOURCE_UI)
        self.assertEqual([d.to_dict() for d in diagnostics], [])
        self.assertEqual(document["activeProfileId"], "gemini-live")

    def test_corrupt_json_falls_back_instead_of_raising(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "lumine.config.json"
            broken.write_text("{not json", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                document, source, diagnostics = effective_document(broken)

        self.assertEqual(source, SOURCE_ENV, "a corrupt file must not stop the worker")
        self.assertEqual([d.code for d in diagnostics], ["document.unreadable"])

    def test_a_document_with_blocking_errors_falls_back(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "lumine.config.json"
            broken.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "activeProfileId": "ghost",
                        "providers": {},
                        "profiles": [{"id": "real", "name": "Real", "kind": "pipeline",
                                      "pipeline": {}}],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                document, source, diagnostics = effective_document(broken)

        self.assertEqual(source, SOURCE_ENV)
        self.assertIn("document.unknown_active_profile", [d.code for d in diagnostics])

    def test_unsupported_version_falls_back(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            future = Path(tmp) / "lumine.config.json"
            future.write_text(
                json.dumps({"version": 99, "activeProfileId": "x",
                            "providers": {}, "profiles": []}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                _, source, diagnostics = effective_document(future)

        self.assertEqual(source, SOURCE_ENV)
        self.assertIn("document.unsupported_version", [d.code for d in diagnostics])

    def test_missing_file_is_not_an_error(self):
        loaded, diagnostics = load_document(Path("definitely-absent.json"))
        self.assertIsNone(loaded)
        self.assertEqual(diagnostics, [])

    def test_active_profile_resolves_the_referenced_profile(self):
        document = json.loads((FIXTURES / "lumine.config.example.json").read_text(encoding="utf-8"))
        profile = active_profile(document)
        self.assertIsNotNone(profile)
        self.assertEqual(profile["id"], "gemini-live")

    def test_active_profile_returns_none_for_a_dangling_reference(self):
        self.assertIsNone(active_profile({"activeProfileId": "nope", "profiles": []}))


class JobPreferencesTests(unittest.TestCase):
    def test_legacy_metadata_still_yields_the_interruption_mode_only(self):
        metadata = json.dumps({"interruption_mode": "finish_response"})
        with mock.patch.dict(os.environ, {}, clear=True):
            result = job_preferences_from_metadata(metadata)
        self.assertIsInstance(result, JobPreferences)
        self.assertEqual(result.interruption_mode, "finish_response")
        self.assertIsNone(result.profile_id)
        self.assertTrue(result.uses_default_profile)

    def test_profile_id_is_read_when_present(self):
        metadata = json.dumps({"interruption_mode": "barge_in", "profile_id": "lumine-default"})
        with mock.patch.dict(os.environ, {}, clear=True):
            result = job_preferences_from_metadata(metadata)
        self.assertEqual(result.profile_id, "lumine-default")
        self.assertFalse(result.uses_default_profile)

    def test_malformed_metadata_falls_back_completely(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = job_preferences_from_metadata("not-json")
        self.assertEqual(result.interruption_mode, "barge_in")
        self.assertIsNone(result.profile_id)

    def test_non_object_metadata_falls_back_completely(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = job_preferences_from_metadata("[1, 2, 3]")
        self.assertEqual(result.interruption_mode, "barge_in")
        self.assertIsNone(result.profile_id)

    def test_absent_metadata_falls_back_completely(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = job_preferences_from_metadata(None)
        self.assertIsNone(result.profile_id)

    def test_job_preferences_agree_with_the_legacy_helper(self):
        for metadata in (
            None,
            "",
            "garbage",
            json.dumps({}),
            json.dumps({"interruption_mode": "barge_in"}),
            json.dumps({"interruption_mode": "finish_response"}),
            json.dumps({"interruption_mode": "nonsense"}),
            json.dumps({"interruption_mode": "finish_response", "profile_id": "p1"}),
        ):
            with self.subTest(metadata=metadata), mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    job_preferences_from_metadata(metadata).interruption_mode,
                    interruption_mode_from_metadata(metadata),
                )

    def test_profile_ids_are_validated(self):
        self.assertEqual(normalize_profile_id("lumine-default"), "lumine-default")
        self.assertEqual(normalize_profile_id("a.b_c-1"), "a.b_c-1")
        for bad in ("", "  ", "-leading", "has space", "x" * 65, "../../etc/passwd", "semi;colon"):
            with self.subTest(value=bad):
                self.assertIsNone(normalize_profile_id(bad))

    def test_a_hostile_profile_id_is_dropped_not_forwarded(self):
        metadata = json.dumps({"profile_id": "../../etc/passwd"})
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(job_preferences_from_metadata(metadata).profile_id)

    def test_environment_fallback_is_honoured(self):
        env = {"LUMINE_INTERRUPTION_MODE": "finish_response"}
        with mock.patch.dict(os.environ, env, clear=True):
            result = job_preferences_from_metadata(json.dumps({"profile_id": "p"}))
        self.assertEqual(result.interruption_mode, "finish_response")
        self.assertEqual(result.profile_id, "p")


class ConfigStoreModuleTests(unittest.TestCase):
    def test_precedence_sources_are_distinct_strings(self):
        self.assertNotEqual(config_store.SOURCE_UI, config_store.SOURCE_ENV)

    def test_module_does_not_read_credentials(self):
        """The config document layer must never touch key material."""
        source = Path(config_store.__file__).read_text(encoding="utf-8")
        for forbidden in ("API_KEY", "api_key", "SECRET", "get_password"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
