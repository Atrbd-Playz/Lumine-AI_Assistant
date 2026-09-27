import inspect
import json
import re
import typing
import unittest
from pathlib import Path

from agent import providers as providers_module
from agent.provider_catalog import main as catalog_main
from agent.providers import (
    CAPABILITIES,
    CATALOG_VERSION,
    MODALITIES,
    PROVIDERS,
    RESERVED_PROVIDER_IDS,
    _OPTION_SETS,
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

    def test_each_provider_names_at_most_one_default_voice(self):
        """A provider that defaults to two voices has no default.

        Google draws its voice catalogue from two lists, and marking a default in
        each gave the merged provider two. Every consumer asking "which voice does
        Google default to?" then answered with whichever it reached first, and the
        per-model list answered `Sulafat` while the merged list answered
        `Zephyr` — so the same provider spoke two ways depending on whether the
        caller happened to name a model.

        `_dedupe_voices` demotes the later default rather than trusting the
        callers to; this is what says that has to stay true.
        """
        for provider in PROVIDERS.values():
            defaults = [voice.id for voice in provider.voices if voice.default]
            with self.subTest(provider=provider.id):
                self.assertLessEqual(
                    len(defaults), 1, f"{provider.id} names {len(defaults)} default voices"
                )

    def test_the_google_default_voice_is_the_one_the_worker_ships(self):
        # Two paths to one answer. The catalog is what the settings screen offers;
        # `DEFAULT_GEMINI_VOICE` is what an environment-derived profile uses when
        # there is no saved document. If they disagree, a fresh install and a
        # configured one sound different for no reason the user can see.
        from agent.pipeline_config import DEFAULT_GEMINI_VOICE

        google = PROVIDERS["google"]
        defaults = [voice.id for voice in google.voices if voice.default]
        self.assertEqual(defaults, [DEFAULT_GEMINI_VOICE])
        for model in google.models_for("realtime"):
            with self.subTest(model=model.id):
                self.assertIn(DEFAULT_GEMINI_VOICE, model.voices)

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

    def test_every_declared_option_is_accepted_by_its_plugin(self):
        """A declared option must name a parameter the plugin actually takes.

        This is the test that would have caught both real bugs in the option
        catalog: Cartesia declared `temperature`, which its `TTS.__init__` has no
        such parameter for, and Google's TTS declares `voice` and `speed` where
        the plugin calls them `voice_name` and `speaking_rate`. Either way the
        profile saved, validated clean, and then raised `TypeError` at session
        construction -- on the user's first spoken turn, with no way to trace it
        back to a settings field.

        The mapping is asserted, not just membership: `OptionDefinition.keyword`
        is what exists precisely so Lumine can call these `voice` and `speed`
        while still handing the plugin its own spelling.
        """
        try:
            from livekit.plugins import cartesia as lk_cartesia
            from livekit.plugins import google as lk_google
            from livekit.plugins import groq as lk_groq
        except ImportError:  # pragma: no cover - plugin not installed
            self.skipTest("livekit plugins are not installed")

        # RealtimeModel is not re-exported from the package root; it lives in the
        # realtime submodule and the pipeline factory imports it from there.
        from livekit.plugins.google.realtime import RealtimeModel

        constructors = {
            ("cartesia", "tts"): lk_cartesia.TTS.__init__,
            ("google", "tts"): lk_google.TTS.__init__,
            ("google", "stt"): lk_google.STT.__init__,
            ("google", "realtime"): RealtimeModel.__init__,
            ("groq", "llm"): lk_groq.LLM.__init__,
            ("groq", "stt"): lk_groq.STT.__init__,
        }

        for (provider_id, capability), constructor in constructors.items():
            parameters = set(inspect.signature(constructor).parameters)
            for option in _OPTION_SETS.get((provider_id, capability), ()):
                with self.subTest(provider=provider_id, option=option.name):
                    if option.nest:
                        # A nested setting is delivered as one object, so the
                        # plugin sees the group name, not the option name. The
                        # group's own keys are checked where the group is built.
                        self.assertIn(option.nest, parameters)
                        continue
                    if option.companions:
                        continue
                    self.assertIn(
                        option.plugin_keyword,
                        parameters,
                        f"{provider_id} {capability} declares {option.name!r}, which "
                        f"maps to {option.plugin_keyword!r} -- a parameter "
                        f"{constructor.__qualname__} does not accept",
                    )

    def test_cartesia_emotions_match_the_installed_plugin(self):
        """The emotion list is a copy, so something has to police the copy.

        `providers.py` stays free of a livekit import, which is what keeps the
        catalog importable and testable on its own. The cost of that is a
        transcription that can fall behind the plugin, and a stale entry renders
        as a dropdown choice that fails only at synthesis time.
        """
        try:
            from livekit.plugins.cartesia.models import TTSVoiceEmotion
        except ImportError:  # pragma: no cover - plugin not installed
            self.skipTest("livekit-plugins-cartesia is not installed")

        self.assertEqual(
            sorted(providers_module._CARTESIA_SONIC_3_EMOTIONS),
            sorted(typing.get_args(TTSVoiceEmotion)),
            "the catalog's emotion copy has drifted from the plugin's Literal",
        )

    def test_cartesia_speed_is_declared_as_the_plugin_bounds_it(self):
        """sonic-3 rejects speed outside 0.6-2.0, and only accepts a float.

        The bounds live in the catalog rather than in a factory clamp so the
        settings screen can draw a slider that stops where the provider stops,
        and so a value the provider would refuse is caught when a profile is
        saved instead of when somebody first speaks.
        """
        definition = next(o for o in _OPTION_SETS[("cartesia", "tts")] if o.name == "speed")
        self.assertEqual(definition.minimum, 0.6)
        self.assertEqual(definition.maximum, 2.0)
        # The plugin still defines a `fastest|fast|normal|slow|slowest` alias, but
        # for sonic-3 it raises on a non-float. Offering those names in a
        # dropdown would therefore produce a setting that always fails.
        self.assertEqual(definition.values, ())
        self.assertEqual(definition.control_kind, "slider")

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


class LocalModelProviderTests(unittest.TestCase):
    """The local-server provider, which is the one that has no credential.

    A provider that asks for nothing is easy to get wrong in the direction of
    asking for something: the setup gate lists what is missing, and a key that
    does not exist cannot be entered. So these tests mostly assert absences.
    """

    def setUp(self):
        self.provider = get_provider("ollama")
        assert self.provider is not None, "the local provider must exist"

    def test_it_asks_for_no_credential(self):
        self.assertFalse(self.provider.requires_key)
        self.assertEqual(self.provider.key_env, ())
        # Empty slots, not absent: a provider with no key still has to render
        # *something* on the Providers page, and "this one needs nothing" is a
        # different sentence from nothing at all.
        self.assertEqual(self.provider.slots(), ())

    def test_it_is_marked_local(self):
        # `local` is what keeps it out of the "these keys leave your machine"
        # grouping and keeps the setup gate from waiting on it.
        self.assertTrue(self.provider.local)

    def test_the_reserved_id_stays_reserved(self):
        """`local` is not this provider, and must not quietly become it.

        `local` was reserved for inference whose weights ship *with the app* — a
        bundled Whisper, a bundled Kokoro. What exists instead is `ollama`: a
        server on this machine that the user started. Shipping the second under
        the first's name would make a promise the app cannot keep, because
        installing it would leave somebody expecting a bundled model and finding
        a server they have to run themselves.
        """
        self.assertIn("local", RESERVED_PROVIDER_IDS)
        self.assertIsNone(get_provider("local"))
        self.assertEqual(self.provider.id, "ollama")

    def test_it_never_seeds_a_stage(self):
        """Opt-in, not the app's default.

        A local 8B model is a worse conversation partner than a hosted one.
        Seeding a new stage onto it because it happens to be listed would be
        Lumine making that trade on the user's behalf.
        """
        self.assertEqual(self.provider.preferred_for, ())
        self.assertIsNone(self.provider.default_for("llm"))

    def test_the_base_url_is_offered_and_is_not_advanced(self):
        """A wrong address is the entire failure mode, so it cannot be hidden.

        Everything else about a local provider is right at the default: the
        port, the `/v1` prefix, the protocol. The one thing that can be wrong is
        the address, so `base_url` is the only setting on this stage that a person
        has to be able to change without opening a disclosure.
        """
        for model in self.provider.models_for("llm"):
            base_url = model.option("base_url")
            assert base_url is not None, f"{model.id} has no base_url"
            self.assertFalse(base_url.advanced)
            self.assertEqual(base_url.default, "http://localhost:11434/v1")

    def test_the_ignored_api_key_is_sent_but_not_drawn(self):
        """The OpenAI client requires a key; this server has no authentication.

        The two facts together mean a value has to be sent that is not a secret
        and that nobody can meaningfully change. Declaring it in the catalog is
        what keeps it out of a provider branch in the factory; `hidden` is what
        keeps it out of the settings screen, where an `api_key` box on a
        credential-free provider reads as a setup wizard that cannot be completed.
        """
        for model in self.provider.models_for("llm"):
            api_key = model.option("api_key")
            assert api_key is not None, f"{model.id} has no api_key default"
            self.assertTrue(api_key.hidden)
            self.assertEqual(api_key.default, "ollama")
            # Sent even when the profile says nothing about it.
            self.assertEqual(model.build_options({})["api_key"], "ollama")
            # And the profile still wins, which is what keeps this from being a
            # hardcoded value in a place that cannot be corrected.
            self.assertEqual(
                model.build_options({"api_key": "something-else"})["api_key"],
                "something-else",
            )

    def test_no_token_cap_on_a_local_model(self):
        """OpenAI's newer token parameter is not universally understood.

        A rejected keyword is a 400 on the first spoken turn, which in a voice
        session is indistinguishable from a muted microphone. The model's own
        context length is the cap, and it is the server's job to enforce it.
        """
        for model in self.provider.models_for("llm"):
            self.assertIsNone(model.option("max_completion_tokens"))

    def test_the_ignored_key_is_never_published_as_a_credential(self):
        """`api_key` is an option, not a `KeySlot`.

        The distinction is the whole point: a slot is something the person types
        into the keyring, and there is nothing to type here. Publishing this as a
        slot would put an empty password field on the Providers page under a
        provider whose defining property is that it has no credentials.
        """
        envs = {slot.env for slot in self.provider.slots()}
        self.assertNotIn("OLLAMA_API_KEY", envs)

    def test_the_base_url_is_reachable_as_an_option_and_not_as_a_key(self):
        # A URL *is* something the person sets, and the local provider does need
        # one. It is still an option rather than a credential: it is not secret,
        # it does not need the keyring, and it lives in the profile next to the
        # model, so switching models between two local servers carries it along.
        for model in self.provider.models_for("llm"):
            self.assertIsNotNone(model.option("base_url"))


class ProviderLookupTests(unittest.TestCase):
    def test_unknown_provider_returns_none(self):
        self.assertIsNone(get_provider("nope"))
        self.assertIsNone(get_model("nope", "whatever"))

    def test_capability_scoped_lookup_rejects_the_wrong_capability(self):
        # groq/gpt-oss-20b is a real Groq model, but it is an LLM, not an STT.
        self.assertIsNotNone(get_model("groq", "openai/gpt-oss-20b", "llm"))
        self.assertIsNone(get_model("groq", "openai/gpt-oss-20b", "stt"))

    def test_every_capability_has_a_usable_default(self):
        """A seeded stage has to land on a model that exists.

        Every provider offers every model it can *name*. The one exception is a
        provider whose contents are not knowable in advance -- a local model
        server, which may have nothing pulled -- and that exception is expressed
        by the provider naming no default at all, so `default_for` returns
        `None`.
        """
        for capability in CAPABILITIES:
            if capability == "transport":
                continue  # covered by test_every_capability_is_represented
            providers = [p for p in PROVIDERS.values() if p.supports(capability)]
            self.assertTrue(providers, f"no provider offers {capability}")
            for provider in providers:
                model = provider.default_for(capability)  # type: ignore[arg-type]
                if model is None:
                    # Only legal if Lumine never seeds a stage from this provider.
                    # A default that exists but is not `preferred_for` still has to
                    # be usable, because a person can pick it by hand.
                    self.assertNotIn(
                        capability,  # type: ignore[arg-type]
                        provider.preferred_for,
                        f"{provider.id} seeds {capability} stages but has no default to "
                        f"seed them with",
                    )
                    continue
                self.assertEqual(model.status, "available")

    def test_a_provider_without_a_default_says_why_in_its_notes(self):
        """A provider with no default must explain itself.

        `default_for` returning `None` is legal, but a reader of the catalog has no
        way to tell "deliberately none" from "nobody got round to marking one". The
        notes are where that difference is recorded, so an unmarked provider fails
        here rather than in somebody's settings screen.
        """
        for provider in PROVIDERS.values():
            if provider.default_for("llm") is not None:
                continue
            self.assertTrue(
                provider.notes.strip(),
                f"{provider.id} has no default llm model and does not say why",
            )

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

        # Remove the categories of string that legitimately contain secret-shaped
        # words: declared env var names, vendor sign-up links, and a probe's
        # connectivity metadata. Anything secret-looking that remains is a leak.
        for provider in providers:
            for name in provider["keyEnv"]:
                blob = blob.replace(name, "<ENV_NAME>")
            if provider["setupUrl"]:
                blob = blob.replace(provider["setupUrl"], "<SETUP_URL>")
            probe = provider.get("probe")
            if probe:
                # A probe publishes *where* it calls and *which header* it uses.
                # The auth scheme is a fixed word, not anyone's credential, and
                # the header names are the same kind of documentation as keyEnv.
                #
                # Guarded on non-empty: a minted-token probe (LiveKit) has no
                # fixed url, because the host *is* the credential under test.
                # `"".replace` inserts its argument between every character, which
                # would have filled the blob with markers and made this scan pass
                # on anything at all.
                if probe["url"]:
                    blob = blob.replace(probe["url"], "<PROBE_URL>")
                blob = blob.replace(probe["authHeader"].lower(), "<HEADER_NAME>")
                blob = blob.replace(probe["authPrefix"].lower(), "<AUTH_SCHEME>")
                for name in probe["headers"]:
                    blob = blob.replace(name.lower(), "<HEADER_NAME>")
                    blob = blob.replace(name, "<HEADER_NAME>")

        lowered = blob.lower()
        for marker in ("api_key", "apikey", "secret", "password", "bearer ", "sk-", "eyj"):
            self.assertNotIn(marker, lowered, f"public catalog leaked {marker!r}")

    def test_a_probe_carries_no_credential_shaped_value(self):
        """The stronger check the blob scan cannot make.

        A probe is the one place the catalog describes how to *send* a secret, so
        every string in it is classified explicitly rather than pattern-matched.
        """
        for provider in self.catalog["providers"]:
            probe = provider.get("probe")
            if not probe:
                continue
            with self.subTest(provider=provider["id"]):
                if probe.get("authKind") == "livekit_token":
                    # No fixed address: the host is the credential. What must
                    # hold is that the path is rooted, so the probe cannot be
                    # pointed somewhere unversioned, and that the provider
                    # supplies more than one value to sign with.
                    self.assertRegex(probe["tokenPath"], r"^/[A-Za-z0-9/_.-]+$")
                    self.assertGreaterEqual(
                        len(provider["keyEnv"]), 2, "a minted token needs a URL and a key pair"
                    )
                else:
                    self.assertTrue(probe["url"].startswith("https://"))
                self.assertIn(probe["method"], ("GET", "POST"))
                # The header is a name; the prefix is a scheme or empty.
                self.assertRegex(probe["authHeader"], r"^[A-Za-z0-9-]+$")
                self.assertIn(probe["authPrefix"], ("", "Bearer "))
                for name, value in probe["headers"].items():
                    self.assertRegex(name, r"^[A-Za-z0-9-]+$")
                    # A fixed header value is a version or a flag, never a key:
                    # no spaces, no long opaque token, no key-shaped prefix.
                    self.assertRegex(value, r"^[A-Za-z0-9._-]{1,32}$")
                self.assertEqual(probe["costs"], "")

    def test_no_catalog_field_is_named_like_a_credential(self):
        """Guards against a future field such as ``apiKey`` sneaking into the view.

        A probe contributes header *names* and an auth scheme, which are
        documentation rather than credentials, so they are allowed explicitly.
        """
        allowed = {
            "version", "capabilities", "reservedProviderIds", "providers",
            "id", "label", "requiresKey", "keyEnv", "setupUrl", "local", "notes",
            # How to draw one field per credential variable. Names and kinds, no
            # values: the frontend renders these instead of knowing that LiveKit
            # has three fields and Groq has one.
            "keySlots", "env", "kind", "help",
            "models", "voices", "capability", "status", "default", "replaces",
            "requiresVad", "realtime", "nativeAudio", "textOnlyModality",
            "finishResponse", "proactivity", "affectiveDialog",
            "asyncFunctionCalling", "languages", "preferredFor", "thinkingLevels", "options",
            # What Lumine can hand the model. The capability screen draws a matrix
            # from it and the camera control reads it, so a stage that silently
            # received nothing would be a control that silently lies.
            "inputModalities",
            "name", "values", "nest",
            # An option's widget and numeric bounds. A bound is a number, not a
            # secret, and it is what lets the slider stop where the provider does.
            "control", "minimum", "maximum", "step", "advanced",
            # An option's default and whether it is drawn. A local model server
            # has no key at all, but the OpenAI client refuses to construct
            # without one, so the value the worker sends comes from the catalog
            # rather than from a provider branch. `hidden` is what keeps that
            # argument out of the screen.
            "default", "hidden",
            "probe", "method", "url", "authHeader", "authPrefix", "headers", "invalidStatus", "costs",
            # How a probe authenticates, and -- for the minted-token kind -- the
            # rooted path it appends to the server address. Both are request
            # *shape*, like a header name: they say which API is called, and the
            # address itself stays in the credential store, never in the catalog.
            "authKind", "tokenPath",
            # Header names and auth schemes inside a probe are documentation, and
            # are asserted separately to be name-shaped.
            "Cartesia-Version",
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

    def test_the_settings_a_person_chooses_are_never_behind_the_disclosure(self):
        """What a voice app exists to expose must be one click away.

        Speed, voice, emotion, language and thinking level all have a default a
        person can predict. Hiding any of them would be hiding the product, not
        hiding a tuning knob.
        """
        always_visible = {"voice", "speed", "emotion", "language", "thinking_level", "reasoning_effort"}
        for provider in self.catalog["providers"]:
            for model in provider["models"]:
                for option in model["options"]:
                    with self.subTest(provider=provider["id"], option=option["name"]):
                        if option["name"] in always_visible:
                            self.assertFalse(
                                option["advanced"],
                                f"{option['name']} is a headline control and must stay visible",
                            )

    def test_the_knobs_with_no_predictable_default_are_advanced(self):
        """The other direction.

        Temperature, token caps, top-p and volume have sensible values that depend
        on the model and the prompt. A wrong setting of any of them makes replies
        worse in a way that is hard to trace back here, so they go behind the
        disclosure rather than onto the first screen of a stage.
        """
        tuning = {"temperature", "top_p", "max_output_tokens", "max_completion_tokens", "volume"}
        seen: set[str] = set()
        for provider in self.catalog["providers"]:
            for model in provider["models"]:
                for option in model["options"]:
                    if option["name"] in tuning:
                        seen.add(option["name"])
                        with self.subTest(provider=provider["id"], option=option["name"]):
                            self.assertTrue(option["advanced"], f"{option['name']} should be advanced")
        # Guard the guard: if the names are ever renamed, this fails rather than
        # silently testing nothing.
        self.assertEqual(seen, tuning)

    def test_every_model_declares_what_lumine_can_hand_it(self):
        """A stage that never says what it accepts cannot be reasoned about.

        The settings screen draws a capability matrix from these, and the camera
        control decides whether to offer itself by them. Both are wrong if a model
        is silent, so the default is filled in from the capability and the result is
        checked rather than assumed.
        """
        for provider in self.catalog["providers"]:
            for model in provider["models"]:
                if model["capability"] == "transport":
                    # A transport is not given anything; it carries what another
                    # stage produced. Declaring a modality for it would put a row in
                    # the capability matrix that means nothing.
                    with self.subTest(provider=provider["id"], model=model["id"]):
                        self.assertEqual(model["inputModalities"], [])
                    continue
                with self.subTest(provider=provider["id"], model=model["id"]):
                    modalities = model["inputModalities"]
                    self.assertTrue(
                        modalities,
                        f"{model['id']} declares no input modality, so nothing can be said about it",
                    )
                    for name in modalities:
                        self.assertIn(name, MODALITIES)
                    if model["capability"] in ("stt", "vad"):
                        self.assertIn("audio", modalities)
                    if model["capability"] in ("llm", "tts"):
                        self.assertIn("text", modalities)

    def test_a_camera_reaches_exactly_the_models_that_accept_frames(self):
        """Video implies a still frame, and neither implies a voice.

        A model that lists `video` but not `image` would mean a screenshare works
        and a camera does not, which is not a thing any provider offers and would
        be a declaration error rather than a capability.
        """
        for provider in self.catalog["providers"]:
            for model in provider["models"]:
                with self.subTest(provider=provider["id"], model=model["id"]):
                    modalities = set(model["inputModalities"])
                    if "video" in modalities:
                        self.assertIn("image", modalities)
                    if "image" in modalities:
                        self.assertNotEqual(
                            modalities & MODALITIES - {"image", "video"},
                            set(),
                            "a model that sees should say what it sees alongside",
                        )

    def test_the_realtime_family_is_the_only_thing_that_sees(self):
        """Pins the answer the camera button actually gives today.

        This is the whole reason the field exists. Every Live model takes image and
        video; no LLM stage does, because the LiveKit LLM plugin sends a chat
        history of strings and has nowhere to put a frame. If a future plugin grows
        video input this test is the thing to update, and updating it is the moment
        to find out whether the agent can receive frames yet.
        """
        seeing = {
            (provider["id"], model["id"])
            for provider in self.catalog["providers"]
            for model in provider["models"]
            if "image" in model["inputModalities"]
        }
        capabilities = {
            (provider["id"], model["id"]): model["capability"]
            for provider in self.catalog["providers"]
            for model in provider["models"]
        }
        for entry in seeing:
            with self.subTest(provider=entry[0], model=entry[1]):
                self.assertEqual(capabilities[entry], "realtime")
        self.assertTrue(seeing, "no model declares vision, which cannot be right")

    def test_every_credential_variable_has_exactly_one_field(self):
        """The contract the Providers screen draws against.

        One field per variable, no more and no fewer. A provider with three
        variables and one field is the original bug: the user pastes one value and
        the worker receives it three times, and nothing on screen says so.
        """
        for provider in self.catalog["providers"]:
            with self.subTest(provider=provider["id"]):
                slots = provider["keySlots"]
                self.assertEqual(
                    [slot["env"] for slot in slots],
                    provider["keyEnv"],
                    "fields must cover the credential variables, in order",
                )
                for slot in slots:
                    self.assertTrue(slot["label"], f"{provider['id']}: {slot['env']}")
                    self.assertIn(slot["kind"], ("secret", "text", "url"))

    def test_a_provider_needing_no_credential_has_no_fields(self):
        for provider in self.catalog["providers"]:
            if not provider["requiresKey"]:
                with self.subTest(provider=provider["id"]):
                    self.assertEqual(provider["keySlots"], [])

    def test_a_value_named_like_a_secret_is_always_masked(self):
        """A field drawn in the clear must not be one that holds a key.

        The mistake this prevents is the one that broke LiveKit: an API key typed
        into the server URL, which the worker then used as an address. A variable
        whose name says it is a credential is masked whatever else it is, and a
        field shown in the clear has to be something whose name says it is not.
        """
        secret_markers = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD")
        for provider in self.catalog["providers"]:
            for slot in provider["keySlots"]:
                with self.subTest(provider=provider["id"], env=slot["env"]):
                    looks_secret = any(m in slot["env"] for m in secret_markers)
                    if looks_secret:
                        self.assertEqual(
                            slot["kind"],
                            "secret",
                            f"{slot['env']} is named like a credential and must be masked",
                        )
                    if slot["kind"] == "url":
                        # And the converse: only something address-shaped is shown
                        # in the clear. `KeySlot` enforces the same rule on the
                        # Python side; this pins it in the published view.
                        self.assertRegex(slot["env"], r"_(URL|URI|ENDPOINT)$")

    def test_livekit_offers_three_separately_named_fields(self):
        """The concrete shape the reported bug needed.

        One field called "LiveKit key" cannot express a URL, a key and a secret.
        """
        livekit = next(p for p in self.catalog["providers"] if p["id"] == "livekit")
        self.assertEqual(
            [slot["env"] for slot in livekit["keySlots"]],
            ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"],
        )
        self.assertEqual(livekit["keySlots"][0]["label"], "Server URL")
        self.assertEqual(livekit["keySlots"][0]["kind"], "url")
        # The two that are actually secrets are masked.
        self.assertEqual(
            [slot["kind"] for slot in livekit["keySlots"][1:]], ["secret", "secret"]
        )

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
