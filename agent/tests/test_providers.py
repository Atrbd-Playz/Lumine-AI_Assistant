import json
import re
import unittest
from pathlib import Path

from agent.provider_catalog import main as catalog_main
from agent.providers import (
    CAPABILITIES,
    CATALOG_VERSION,
    PROVIDERS,
    RESERVED_PROVIDER_IDS,
    catalog_issues,
    get_model,
    get_provider,
    models_for,
    to_public_catalog,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = FIXTURES.parent.parent.parent
AI_CONFIG_CLIENT = REPO_ROOT / "lumine-ui" / "src" / "features" / "settings" / "aiConfigClient.ts"


class CatalogIntegrityTests(unittest.TestCase):
    def test_catalog_is_internally_coherent(self):
        self.assertEqual(catalog_issues(), [])

    def test_every_capability_is_represented(self):
        covered: set[str] = set()
        for provider in PROVIDERS.values():
            covered.update(provider.capabilities())
        self.assertEqual(set(CAPABILITIES), covered)

    def test_provider_ids_are_unique_and_sorted_on_output(self):
        ids = [provider["id"] for provider in to_public_catalog()["providers"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_reserved_ids_are_not_defined_yet(self):
        for reserved in RESERVED_PROVIDER_IDS:
            self.assertIsNone(get_provider(reserved))

    def test_local_providers_do_not_require_a_key(self):
        for provider in PROVIDERS.values():
            if provider.local:
                self.assertFalse(provider.requires_key, f"{provider.id} is local and needs no key")

    def test_voice_lists_have_no_duplicates(self):
        # Google draws on one named voice set for both TTS and Live, so the two
        # groups overlap. A duplicate would show up twice in the settings picker.
        for provider in PROVIDERS.values():
            ids = [voice.id for voice in provider.voices]
            with self.subTest(provider=provider.id):
                self.assertEqual(len(ids), len(set(ids)), f"duplicate voice in {provider.id}")

    def test_google_offers_more_than_one_voice(self):
        # A single curated voice made the picker look broken; both the TTS and the
        # Live groups have to survive the merge.
        google = PROVIDERS["google"]
        self.assertGreaterEqual(len(google.voices), 5)
        live_voices = {
            voice
            for model in google.models_for("realtime")
            for voice in model.voices
        }
        self.assertTrue(live_voices.issubset({voice.id for voice in google.voices}))

    def test_the_current_lumine_voice_is_still_catalogued(self):
        # Dropping it would silently change how Lumine sounds for anyone whose
        # saved profile still references it.
        cartesia = PROVIDERS["cartesia"]
        self.assertIsNotNone(cartesia.get_voice("002622d8-19d0-4567-a16a-f99c7397c062"))

    def test_each_pipeline_stage_has_one_preferred_provider(self):
        # The settings screen seeds a stage from these. A capability with no
        # preference, or two, means the seed follows catalog order and can put
        # the conversation on an unintended vendor.
        #
        # `realtime` is deliberately absent: a realtime stage is a single model,
        # and that choice comes from the model's own default flag. `transport` is
        # a reserved slot with no provider yet.
        for capability in ("stt", "llm", "tts", "vad"):
            preferred = [
                provider.id
                for provider in PROVIDERS.values()
                if capability in provider.preferred_for
            ]
            with self.subTest(capability=capability):
                self.assertEqual(len(preferred), 1, f"{capability}: {preferred}")

    def test_no_capability_has_two_preferred_providers(self):
        for capability in CAPABILITIES:
            preferred = [
                provider.id
                for provider in PROVIDERS.values()
                if capability in provider.preferred_for
            ]
            with self.subTest(capability=capability):
                self.assertLessEqual(len(preferred), 1, f"{capability}: {preferred}")

    def test_preferred_providers_are_the_ones_the_worker_uses(self):
        # Groq for speech and language, Cartesia for the voice, Silero locally.
        # If this drifts from agent/.env, switching stack type in the settings
        # screen silently moves Lumine to another vendor.
        self.assertEqual(PROVIDERS["groq"].preferred_for, ("stt", "llm"))
        self.assertEqual(PROVIDERS["cartesia"].preferred_for, ("tts",))
        self.assertEqual(PROVIDERS["silero"].preferred_for, ("vad",))

    def test_preferred_for_reaches_the_public_catalog(self):
        # The frontend seeds from this field; if it were dropped in the JSON the
        # settings screen would quietly fall back to catalog order.
        public = {provider["id"]: provider for provider in to_public_catalog()["providers"]}
        self.assertEqual(public["groq"]["preferredFor"], ["stt", "llm"])
        self.assertEqual(public["cartesia"]["preferredFor"], ["tts"])

    def test_realtime_models_exist_in_the_installed_plugin(self):
        """The plugin, not this catalog, decides what a session can connect to.

        `livekit.plugins.google.realtime` keeps a set of model ids it recognises
        and raises on a mismatch with the configured API. A catalog entry the
        installed plugin does not know is a dead end that no test here would
        otherwise catch, so this reads the plugin's own list.
        """
        try:
            from livekit.plugins.google.realtime.realtime_api import (
                KNOWN_GEMINI_API_MODELS,
            )
        except ImportError:  # pragma: no cover - plugin not installed
            self.skipTest("livekit-plugins-google is not installed")

        known = set(KNOWN_GEMINI_API_MODELS)
        for model in PROVIDERS["google"].models_for("realtime"):
            with self.subTest(model=model.id):
                self.assertIn(
                    model.id,
                    known,
                    f"{model.id} is not a Gemini API Live model this plugin knows; "
                    f"known: {sorted(known)}",
                )

    def test_the_default_realtime_model_is_recommended_and_available(self):
        # Google labels 3.1 and 2.5 Live legacy and points at 3.8. A deprecated
        # model must never be the one a new profile is seeded with.
        realtime = PROVIDERS["google"].models_for("realtime")
        default = PROVIDERS["google"].default_for("realtime")
        self.assertIsNotNone(default, "no default realtime model")
        assert default is not None
        self.assertEqual(default.status, "available")
        self.assertEqual(
            default.id,
            "gemini-3.8-live",
            "the seeded realtime model should be the one Google recommends",
        )
        # Anything else available is a deliberate choice, not an oversight.
        self.assertEqual(
            sorted(m.id for m in realtime if m.status == "available"),
            ["gemini-3.8-live", "gemini-3.8-live-extended-thinking"],
        )

    def test_deprecated_realtime_models_name_a_replacement(self):
        google = PROVIDERS["google"]
        for model in google.models_for("realtime"):
            if model.status != "deprecated":
                continue
            with self.subTest(model=model.id):
                self.assertIsNotNone(model.replaces, "no replacement named")
                self.assertTrue(
                    google.get_model(model.replaces).is_usable(),
                    f"{model.replaces!r} is not a usable replacement",
                )


class ProviderLookupTests(unittest.TestCase):
    def test_unknown_provider_returns_none(self):
        self.assertIsNone(get_provider("nope"))
        self.assertIsNone(get_model("nope", "whatever"))

    def test_capability_scoped_lookup_rejects_the_wrong_capability(self):
        # groq/gpt-oss-20b is a real Groq model, but it is an LLM, not an STT.
        self.assertIsNotNone(get_model("groq", "openai/gpt-oss-20b", "llm"))
        self.assertIsNone(get_model("groq", "openai/gpt-oss-20b", "stt"))

    def test_every_capability_has_a_usable_default(self):
        for capability in CAPABILITIES:
            if capability == "transport":
                continue  # covered by test_every_capability_is_represented
            providers = [p for p in PROVIDERS.values() if p.supports(capability)]
            self.assertTrue(providers, f"no provider offers {capability}")
            for provider in providers:
                model = provider.default_for(capability)  # type: ignore[arg-type]
                self.assertIsNotNone(model, f"{provider.id} has no default {capability} model")
                self.assertEqual(model.status, "available")  # type: ignore[union-attr]

    def test_groq_still_covers_the_models_the_legacy_cascade_uses(self):
        stt_ids = {m.id for m in models_for("groq", "stt")}
        llm_ids = {m.id for m in models_for("groq", "llm")}
        self.assertIn("whisper-large-v3", stt_ids)
        self.assertIn("openai/gpt-oss-20b", llm_ids)

    def test_cartesia_still_knows_the_voice_lumine_uses(self):
        cartesia = get_provider("cartesia")
        self.assertIsNotNone(cartesia)
        self.assertIsNotNone(cartesia.get_voice("002622d8-19d0-4567-a16a-f99c7397c062"))

    def test_gemini_live_default_is_marked_native_audio(self):
        # This is the fact the half-cascade validation rule depends on.
        model = get_model("google", "gemini-3.8-live", "realtime")
        self.assertIsNotNone(model)
        self.assertTrue(model.native_audio)
        self.assertFalse(model.text_only_modality)
        self.assertTrue(model.voices)

    def test_google_stt_models_declare_that_they_need_a_vad(self):
        for model in models_for("google", "stt"):
            self.assertTrue(model.requires_vad, f"{model.id} transcribes whole segments")


class PublicCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = to_public_catalog()

    def test_catalog_is_json_serializable_and_versioned(self):
        json.dumps(self.catalog)
        # Compared against the constant, not a literal: bumping the catalog shape
        # is supposed to require thinking about the desktop layer, and a test
        # that just matches whatever the constant says cannot catch a forgotten
        # bump. `test_frontend_mirrors_the_catalog_version` is the other half.
        self.assertEqual(self.catalog["version"], CATALOG_VERSION)

    def test_frontend_mirrors_the_catalog_version(self):
        """The settings screen refuses a catalog it was not written for.

        That guard is only useful if the version it checks stays in step with
        this one. A forgotten bump here means the app happily renders a catalog
        whose fields it does not understand, which fails deep in a render.
        """
        self.assertTrue(
            AI_CONFIG_CLIENT.exists(),
            f"expected the desktop catalog guard at {AI_CONFIG_CLIENT}",
        )
        source = AI_CONFIG_CLIENT.read_text(encoding="utf-8")
        declared = re.search(r"const CATALOG_VERSION = (\d+);", source)
        self.assertIsNotNone(
            declared,
            "aiConfigClient.ts no longer declares `const CATALOG_VERSION = <n>;`",
        )
        self.assertEqual(int(declared.group(1)), CATALOG_VERSION)

    def test_public_catalog_contains_no_secret_values(self):
        """Environment variable *names* and public setup URLs are fine; values never are."""
        blob = json.dumps(self.catalog)
        providers = self.catalog["providers"]

        # Remove the two categories of string that legitimately contain
        # secret-shaped words: declared env var names and vendor sign-up links.
        # Anything secret-looking that remains would be a leak.
        for provider in providers:
            for name in provider["keyEnv"]:
                blob = blob.replace(name, "<ENV_NAME>")
            if provider["setupUrl"]:
                blob = blob.replace(provider["setupUrl"], "<SETUP_URL>")

        lowered = blob.lower()
        for marker in ("api_key", "apikey", "secret", "password", "bearer ", "sk-", "eyj"):
            self.assertNotIn(marker, lowered, f"public catalog leaked {marker!r}")

    def test_no_catalog_field_is_named_like_a_credential(self):
        """Guards against a future field such as ``apiKey`` sneaking into the view."""
        allowed = {
            "version", "capabilities", "reservedProviderIds", "providers",
            "id", "label", "requiresKey", "keyEnv", "setupUrl", "local", "notes",
            "models", "voices", "capability", "status", "default", "replaces",
            "requiresVad", "realtime", "nativeAudio", "textOnlyModality",
            "finishResponse", "proactivity", "affectiveDialog",
            "asyncFunctionCalling", "languages", "preferredFor", "thinkingLevels", "options",
            "name", "values", "nest",
        }

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertIn(key, allowed, f"unexpected catalog field {key!r}")
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.catalog)

    def test_key_env_is_exposed_as_names_only(self):
        for provider in self.catalog["providers"]:
            for name in provider["keyEnv"]:
                self.assertRegex(name, r"^[A-Z][A-Z0-9_]*$")

    def test_realtime_models_publish_capability_flags(self):
        google = next(p for p in self.catalog["providers"] if p["id"] == "google")
        realtime = [m for m in google["models"] if m["capability"] == "realtime"]
        self.assertTrue(realtime)
        for model in realtime:
            self.assertIsInstance(model["realtime"], dict)
            self.assertIn("textOnlyModality", model["realtime"])


class CatalogCliTests(unittest.TestCase):
    def test_check_mode_passes(self):
        self.assertEqual(catalog_main(["--check"]), 0)

    def test_check_mode_fails_on_a_broken_catalog(self):
        import agent.providers as providers_module

        broken = dict(PROVIDERS["groq"].__dict__)
        broken["models"] = ()
        broken["id"] = "groq"
        self.assertTrue(providers_module.catalog_issues([providers_module.ProviderDefinition(**broken)]))


class GoldenConfigTests(unittest.TestCase):
    """The shipped example config must stay in step with the catalog."""

    def setUp(self):
        self.path = FIXTURES / "lumine.config.example.json"
        self.document = json.loads(self.path.read_text(encoding="utf-8"))

    def test_example_config_parses_and_names_known_providers(self):
        self.assertEqual(self.document["version"], 1)
        for provider_id in self.document["providers"]:
            self.assertIsNotNone(
                get_provider(provider_id),
                f"example config references unknown provider {provider_id!r}",
            )

    def test_example_config_only_references_catalogued_models_and_voices(self):
        def assert_stage(section, label):
            self.assertIn("provider", section, f"{label} has no provider")
            self.assertIn("model", section, f"{label} has no model")
            self.assertIsNotNone(
                get_model(section["provider"], section["model"]),
                f"{label}: {section['provider']}/{section['model']} is not catalogued",
            )

        for profile in self.document["profiles"]:
            if profile.get("kind") == "pipeline":
                pipeline = profile["pipeline"]
                for stage in ("stt", "llm", "tts", "vad"):
                    if stage in pipeline:
                        assert_stage(pipeline[stage], f"{profile['id']}.{stage}")
                tts = pipeline.get("tts") or {}
                if tts.get("voice"):
                    provider = get_provider(tts["provider"])
                    self.assertIsNotNone(provider.get_voice(tts["voice"]))
            else:
                realtime = profile["realtime"]
                assert_stage(realtime, f"{profile['id']}.realtime")
                model = get_model(realtime["provider"], realtime["model"], "realtime")
                self.assertIn(realtime["voice"], model.voices)
                output = realtime.get("output") or {}
                if output.get("mode") == "custom_tts":
                    assert_stage(output["tts"], f"{profile['id']}.output.tts")

    def test_active_profile_exists(self):
        ids = {p["id"] for p in self.document["profiles"]}
        self.assertIn(self.document["activeProfileId"], ids)


if __name__ == "__main__":
    unittest.main()
