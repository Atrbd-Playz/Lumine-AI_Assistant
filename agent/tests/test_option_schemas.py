"""Every model declares what it accepts; the factory and validator read that.

The point of the option schema is that adding a provider is a catalog change. A
synthetic provider is the proof: nothing in the factory or the validator knows it
exists, and it is still built and validated correctly.
"""

import unittest
from dataclasses import replace
from unittest import mock

import agent.providers as providers_module
import agent.validation as validation_module
from agent.config_store import ResolvedProfile, ResolvedStage
from agent.pipeline_factory import _build_llm, _build_stt, _build_tts
from agent.providers import (
    PROVIDERS,
    SESSION_OPTIONS,
    ModelDefinition,
    OptionDefinition,
    ProviderDefinition,
    get_model,
)
from agent.validation import validate_document

ALL_PRESENT = {provider_id: True for provider_id in PROVIDERS}


def install(provider: ProviderDefinition) -> None:
    PROVIDERS[provider.id] = provider
    validation_module.PROVIDERS[provider.id] = provider


def remove(provider_id: str) -> None:
    PROVIDERS.pop(provider_id, None)
    validation_module.PROVIDERS.pop(provider_id, None)


def synthetic() -> ProviderDefinition:
    """A provider that exists only in this test."""
    return ProviderDefinition(
        id="acme",
        label="Acme",
        requires_key=True,
        key_env=("ACME_API_KEY",),
        setup_url="https://example.invalid/",
        notes="Exists only in this test.",
        models=(
            ModelDefinition(
                id="acme-1",
                label="Acme One",
                capability="llm",
                default=True,
                thinking_levels=("terse", "verbose"),
                # Declared for a provider the shared table has never heard of,
                # with a nesting shape Google's does not use.
                options=(
                    OptionDefinition(
                        name="thinking_level",
                        values=("terse", "verbose"),
                        nest="reasoning",
                        companions=("show_work",),
                        notes="How much the model reasons.",
                    ),
                    OptionDefinition(name="mood", values=("formal", "casual")),
                ),
            ),
            ModelDefinition(
                id="acme-2",
                label="Acme Two",
                capability="tts",
                default=True,
                options=(OptionDefinition(name="voice"), OptionDefinition(name="speed")),
            ),
            ModelDefinition(
                id="acme-stt",
                label="Acme Ears",
                capability="stt",
                default=True,
                requires_vad=True,
                options=(OptionDefinition(name="language"),),
            ),
        ),
    )


def document_for(profile_id: str, kind: str, **section) -> dict:
    profile = {"id": profile_id, "name": profile_id, "kind": kind, kind: section}
    return {
        "version": 1,
        "activeProfileId": profile_id,
        "providers": {"acme": {"enabled": True, "keyRef": "acme"}},
        "profiles": [profile],
    }


class SyntheticProviderTests(unittest.TestCase):
    """A provider with no branch anywhere in the factory or the validator."""

    def setUp(self):
        install(synthetic())
        # The provider only exists once installed, so credentials are resolved
        # after that rather than from a module-level snapshot.
        self.credentials = {provider_id: True for provider_id in PROVIDERS}

    def tearDown(self):
        remove("acme")

    def _codes(self, document: dict) -> list[str]:
        return [d.code for d in validation_module.validate_document(document, self.credentials)]

    def _build(self, capability: str, options: dict, module_name: str = "LLM"):
        stage = ResolvedStage(provider="acme", model="acme-1", options=options)
        with mock.patch("agent.pipeline_factory.require_module") as require:
            require.return_value = mock.MagicMock(**{module_name: mock.MagicMock()})
            import asyncio

            asyncio.run(
                {
                    "llm": _build_llm,
                    "stt": _build_stt,
                    "tts": _build_tts,
                }[capability](stage)
            )
        return require.return_value

    def test_a_declared_enum_is_forwarded(self):
        module = self._build("llm", {"thinking_level": "verbose"})
        options = module.LLM.call_args.kwargs
        self.assertEqual(options["reasoning"]["thinking_level"], "verbose")

    def test_a_flat_option_is_forwarded_directly(self):
        module = self._build("llm", {"mood": "casual"})
        self.assertEqual(module.LLM.call_args.kwargs["mood"], "casual")

    def test_a_nested_option_is_gathered_into_its_group(self):
        """`nest` is the whole point: this provider's shape is not Google's."""
        module = self._build("llm", {"thinking_level": "terse", "show_work": True})
        options = module.LLM.call_args.kwargs
        self.assertNotIn("thinking_level", options)
        self.assertNotIn("show_work", options)
        self.assertEqual(options["reasoning"], {"thinking_level": "terse", "show_work": True})

    def test_the_nested_group_name_is_this_providers_own(self):
        # A provider that nests the same idea differently must be honoured, not
        # rewritten into Google's shape.
        self.assertNotIn(
            "thinking_config", self._build("llm", {"thinking_level": "terse"}).LLM.call_args.kwargs
        )

    def test_an_undeclared_value_is_replaced_not_forwarded(self):
        module = self._build("llm", {"thinking_level": "nonsense"})
        options = module.LLM.call_args.kwargs
        self.assertEqual(options["reasoning"]["thinking_level"], "terse")

    def test_an_absent_value_sends_nothing(self):
        module = self._build("llm", {})
        self.assertNotIn("thinking_config", module.LLM.call_args.kwargs)
        self.assertNotIn("reasoning", module.LLM.call_args.kwargs)

    def test_an_unknown_key_is_dropped(self):
        # A saved profile can carry anything; the plugin would reject it.
        module = self._build("llm", {"who_knows": 1})
        self.assertNotIn("who_knows", module.LLM.call_args.kwargs)

    def test_the_same_schema_validates_any_provider(self):
        bad = document_for(
            "a",
            "pipeline",
            stt={"provider": "acme", "model": "acme-stt"},
            llm={"provider": "acme", "model": "acme-1", "thinking_level": "nonsense"},
            tts={"provider": "acme", "model": "acme-2"},
            vad={"provider": "silero", "model": "silero"},
        )
        self.assertIn("llm.thinking_level_unsupported", self._codes(bad))

    def test_an_unsupported_enum_value_blocks(self):
        bad = document_for(
            "a",
            "pipeline",
            stt={"provider": "acme", "model": "acme-stt"},
            llm={"provider": "acme", "model": "acme-1", "mood": "sarcastic"},
            tts={"provider": "acme", "model": "acme-2"},
            vad={"provider": "silero", "model": "silero"},
        )
        diagnostics = validation_module.validate_document(bad, self.credentials)
        blocked = [d for d in diagnostics if d.code == "stage.option_unsupported"]
        self.assertTrue(blocked, [d.code for d in diagnostics])
        self.assertIn("formal, casual", blocked[0].hint or "")

    def test_an_undeclared_key_warns_rather_than_blocks(self):
        # A setting the model ignores is a mistake worth surfacing, not a reason
        # to refuse a working configuration.
        odd = document_for(
            "a",
            "pipeline",
            stt={"provider": "acme", "model": "acme-stt"},
            llm={"provider": "acme", "model": "acme-1", "who_knows": 1},
            tts={"provider": "acme", "model": "acme-2"},
            vad={"provider": "silero", "model": "silero"},
        )
        diagnostics = validation_module.validate_document(odd, self.credentials)
        warned = [d for d in diagnostics if d.code == "stage.option_ignored"]
        self.assertTrue(warned, [d.code for d in diagnostics])
        self.assertEqual(warned[0].severity, "warn")
        self.assertEqual([d.code for d in diagnostics if d.severity == "error"], [])

    def test_a_valid_configuration_is_completely_clean(self):
        good = document_for(
            "a",
            "pipeline",
            stt={"provider": "acme", "model": "acme-stt"},
            llm={"provider": "acme", "model": "acme-1", "thinking_level": "terse", "mood": "casual"},
            tts={"provider": "acme", "model": "acme-2"},
            vad={"provider": "silero", "model": "silero"},
        )
        self.assertEqual([d.to_dict() for d in validation_module.validate_document(good, self.credentials)], [])


class SharedSchemaBindingTests(unittest.TestCase):
    def test_a_thinking_option_only_exists_where_levels_are_declared(self):
        for provider in PROVIDERS.values():
            for model in provider.models:
                with self.subTest(model=model.id):
                    has_option = model.accepts("thinking_level")
                    self.assertEqual(
                        has_option,
                        bool(model.thinking_levels),
                        f"{model.id}: the option and the levels must agree",
                    )

    def test_a_thinking_option_carries_the_models_own_levels(self):
        for provider in PROVIDERS.values():
            for model in provider.models:
                definition = model.option("thinking_level")
                if definition is None:
                    continue
                with self.subTest(model=model.id):
                    self.assertEqual(definition.values, model.thinking_levels)

    def test_every_nested_option_keeps_its_companions_private(self):
        """A companion rides inside the group and is not also a top-level option.

        Passing it twice is how `include_thoughts` reached a plugin that does not
        accept it, which broke the realtime session.
        """
        for provider in PROVIDERS.values():
            for model in provider.models:
                for definition in model.options:
                    if not definition.nest:
                        continue
                    for companion in definition.companions:
                        with self.subTest(model=model.id, companion=companion):
                            self.assertIsNone(
                                model.option(companion),
                                f"{model.id}: {companion} would be forwarded twice",
                            )

    def test_session_options_are_never_declared_as_model_options(self):
        for provider in PROVIDERS.values():
            for model in provider.models:
                for definition in model.options:
                    with self.subTest(model=model.id, option=definition.name):
                        self.assertNotIn(definition.name, SESSION_OPTIONS)

    def test_every_option_name_is_unique_within_a_model(self):
        for provider in PROVIDERS.values():
            for model in provider.models:
                names = [definition.name for definition in model.options]
                with self.subTest(model=model.id):
                    self.assertEqual(len(names), len(set(names)))

    def test_build_options_is_pure(self):
        model = get_model("google", "gemini-3.8-flash", "llm")
        assert model is not None
        settings = {"thinking_level": "high", "temperature": 0.5, "junk": 1}
        snapshot = dict(settings)
        first = model.build_options(settings)
        self.assertEqual(settings, snapshot, "the caller's settings were modified")
        self.assertEqual(first, model.build_options(settings))

    def test_a_free_form_option_accepts_a_number(self):
        model = get_model("cartesia", "sonic-3", "tts")
        assert model is not None
        built = model.build_options({"speed": 0.95})
        self.assertEqual(built["speed"], 0.95)


if __name__ == "__main__":
    unittest.main()
