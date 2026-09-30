"""The values a fresh install actually gets.

`.env.example` is the first file a person copies, so a wrong value in it is a
wrong default for everyone, silently, and it is the one place a setting is
*documented* as well as *set*. These tests compare it against the code that will
read it, so the two cannot drift apart quietly.

Two failure modes are guarded, and both have already happened:

* **A placeholder in a credential variable.** Every credential check in the
  product -- the setup gate, the keyring probe, the worker's own environment
  read -- treats "the variable holds a non-empty string" as "this key is
  configured". There is no way to tell `your-groq-api-key` from a real key
  without making a billable request, so none of them try. An uncommented
  placeholder therefore reads as a working credential: the gate opens and the
  first turn is a 401. This is the same shape as the LiveKit bug where one
  stored secret was written into all three variables, and it is guarded the same
  way -- by making absence the only honest absence.

* **A tuning value that contradicts the default it is supposed to document.**
  `GROQ_MAX_COMPLETION_TOKENS=300` shipped while the code's default was 900. On
  a *reasoning* model the thinking is drawn from the same budget, so 300 buys a
  monologue and no audio. A file that is wrong about a value is worse than a file
  that omits it, because it is the one somebody trusts.
"""

import unittest
from pathlib import Path

from agent.settings import llm_config
from agent.settings import pipeline_config
from agent.settings.providers import PROVIDERS

AGENT_DIR = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = AGENT_DIR / ".env.example"


def credential_variables() -> set[str]:
    """Every environment variable that *is* a credential, read from the catalog.

    Written out by hand this list drifts: a provider gains a second variable and
    the guard quietly stops covering it, which is the exact failure it exists to
    prevent. The catalog is the only place that knows.
    """
    names: set[str] = set()
    for provider in PROVIDERS.values():
        if provider.requires_key:
            names.update(provider.key_env)
    return names


def parse_env_example() -> dict[str, str]:
    """Active ``NAME=value`` lines, with comments and blanks dropped."""
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()
    return values


def catalog_defaults() -> dict[str, str]:
    """The catalog's own default model per provider capability."""
    found: dict[str, str] = {}
    for provider in PROVIDERS.values():
        for capability in ("llm", "stt", "tts", "realtime", "vad"):
            model = provider.default_for(capability)  # type: ignore[arg-type]
            if model is not None:
                found[f"{provider.id}.{capability}"] = model.id
    return found


class NoPlaceholderCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.values = parse_env_example()
        self.text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.credentials = credential_variables()

    def test_the_example_file_exists(self):
        # Guards the rest of the class: an unreadable path would make every
        # assertion below pass for the wrong reason.
        self.assertTrue(ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} is missing")

    def test_the_catalog_still_names_some_credential(self):
        # Also guards the guard: a `requires_key = False` sweep would make
        # `credential_variables()` empty and the class below vacuous.
        self.assertIn("LIVEKIT_API_SECRET", self.credentials)
        self.assertIn("GOOGLE_API_KEY", self.credentials)

    def test_no_credential_variable_is_shipped_with_a_value(self):
        offenders = {
            name: self.values[name]
            for name in self.credentials
            if self.values.get(name)
        }
        self.assertEqual(
            offenders,
            {},
            "a shipped placeholder reads as a configured credential, because no "
            "check can tell it from a real key without spending a request on it",
        )

    def test_every_credential_variable_is_at_least_named(self):
        """A variable nobody has heard of cannot be entered.

        The inverse failure: dropping a line to make the test above pass is how a
        required credential becomes invisible. An empty line is fine -- it leaves
        somewhere to paste -- but the name has to be in the file.
        """
        for name in sorted(self.credentials):
            with self.subTest(variable=name):
                self.assertIn(name, self.text, f"{name} is required but not named in .env.example")


class ShippedValuesMatchTheCodeTests(unittest.TestCase):
    """The file documents the defaults; the defaults are in the code."""

    def setUp(self):
        self.values = parse_env_example()

    def test_the_default_pipeline_is_the_one_the_code_prefers(self):
        # `gemini_live` needs one model provider. The cascade needs two more, and
        # the file that ships them is the first thing a new user sees, so which
        # one is uncommented decides what "works out of the box" means.
        self.assertEqual(self.values.get("LUMINE_PIPELINE"), pipeline_config.DEFAULT_PIPELINE)
        self.assertEqual(pipeline_config.DEFAULT_PIPELINE, "gemini_live")

    def test_the_default_pipeline_needs_no_optional_credential(self):
        """The whole point of the default.

        A realtime stack is one model doing three jobs, so the credentials a
        fresh install has to enter are LiveKit's three and Google's one. If the
        default pipeline ever moved back to the cascade, the first-run
        requirement would silently double -- which is why this is asserted rather
        than left to the value above.
        """
        self.assertEqual(PROVIDERS["google"].key_env, ("GOOGLE_API_KEY",))
        self.assertEqual(
            sorted(PROVIDERS["livekit"].key_env),
            ["LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL"],
        )

    def test_the_gemini_model_is_the_catalog_default(self):
        self.assertEqual(self.values.get("GEMINI_MODEL"), "gemini-3.8-live")
        self.assertEqual(catalog_defaults()["google.realtime"], "gemini-3.8-live")

    def test_the_gemini_voice_is_the_one_the_catalog_publishes(self):
        google = PROVIDERS["google"]
        # Exactly one, and the shipped value is it. A provider with two defaults
        # answers this question differently depending on which list the caller
        # reached, which is the defect `_dedupe_voices` now guards against.
        defaults = [voice.id for voice in google.voices if voice.default]
        self.assertEqual(len(defaults), 1, f"google names {len(defaults)} default voices")
        self.assertEqual(self.values.get("GEMINI_VOICE"), defaults[0])

    def test_the_gemini_voice_is_the_one_the_worker_defaults_to(self):
        # The catalog is what the settings screen offers; `DEFAULT_GEMINI_VOICE`
        # is what the environment-derived profile uses when there is no saved
        # document. They are two paths to one answer, so they have to agree: a
        # fresh install and a configured one otherwise speak differently.
        self.assertEqual(self.values.get("GEMINI_VOICE"), pipeline_config.DEFAULT_GEMINI_VOICE)

    def test_the_default_realtime_model_offers_the_shipped_voice(self):
        model = PROVIDERS["google"].default_for("realtime")
        assert model is not None
        self.assertIn(self.values.get("GEMINI_VOICE", ""), model.voices)

    def test_the_groq_llm_values_match_the_code(self):
        self.assertEqual(self.values.get("GROQ_MODEL"), llm_config.DEFAULT_MODEL)
        self.assertEqual(
            self.values.get("GROQ_MAX_COMPLETION_TOKENS"),
            str(llm_config.DEFAULT_MAX_COMPLETION_TOKENS),
        )
        self.assertEqual(
            self.values.get("GROQ_TEMPERATURE"), str(llm_config.DEFAULT_TEMPERATURE)
        )

    def test_the_groq_token_budget_leaves_room_to_speak(self):
        """The specific bug this file is now protected against.

        GPT-OSS is a reasoning model and its thinking is drawn from the same
        budget as its answer. A cap low enough to sound prudent for a chat reply
        spends the whole turn on thinking, so the session gets a monologue and no
        audio -- and a silent agent looks exactly like a muted microphone, so the
        cause is never found from the symptom.
        """
        budget = llm_config.max_completion_tokens()
        self.assertGreaterEqual(
            budget,
            900,
            "below 900 a reasoning turn spends its whole budget thinking and says "
            "nothing, which presents as a muted microphone rather than a cap",
        )

    def test_the_cartesia_model_is_the_catalog_default_and_not_retired(self):
        default = PROVIDERS["cartesia"].default_for("tts")
        assert default is not None, "cartesia must publish a default tts model"
        self.assertEqual(default.status, "available")
        # The example comments the model out rather than setting it, so there is
        # no value to compare -- the assertion is that shipping it is optional.
        self.assertNotIn("CARTESIA_TTS_MODEL", self.values)


if __name__ == "__main__":
    unittest.main()
