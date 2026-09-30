"""The emotion → voice bridge, and the one promise it actually makes.

The promise is narrow on purpose: *a reply's demonstrated emotion reaches a
synthesizer that can act on it, before the text carrying it is handed on.* These
tests are written around that sentence, because the two ways to get this wrong
are both silent — mapping an emotion to a name the provider ignores, and
applying it a turn late where nothing looks broken either way.
"""

from __future__ import annotations

import asyncio
import types
import typing
import unittest

from agent.runtime import emotion_voice
from agent.runtime.emotion_contract import CANONICAL_EMOTION_SET
from agent.runtime.emotion_voice import (
    MAX_SCAN_CHARS,
    EMOTION_TO_TTS,
    SUPPORTED_MODEL_PREFIX,
    TurnVoiceControls,
    apply_emotion,
    first_voice_emotion,
    supports_voice_controls,
)

# Text that genuinely demonstrates something, and text that does not. The second
# matters more than the first: `extract_emotion_sequence` never returns empty, it
# falls back to `neutral` at intensity 0.0, so "no evidence" has to be read off
# the intensity rather than off the absence of a result.
HAS_EMOTION = "*giggles*"
NO_EMOTION = "The meeting is on Thursday at ten."


class FakeTTS:
    """The minimum a synthesizer has to look like to be worth talking to."""

    provider = "Cartesia"
    model = "sonic-3"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def update_options(self, **options) -> None:
        self.calls.append(options)


class RefusingTTS(FakeTTS):
    def update_options(self, **options) -> None:
        raise RuntimeError("synthesis is busy")


class MappingTests(unittest.TestCase):
    def test_every_key_is_a_canonical_emotion(self):
        unknown = set(EMOTION_TO_TTS) - CANONICAL_EMOTION_SET
        self.assertEqual(
            unknown,
            set(),
            "a key nobody can ever emit is a branch with no caller",
        )

    def test_every_value_is_a_choice_the_settings_screen_offers(self):
        """The catalog is what the UI draws, so it is the outer bound here.

        Mapped to a value outside it would produce a voice control the profile
        could never have been configured with in the first place.
        """
        from agent.settings import providers

        offered = set(providers._CARTESIA_SONIC_3_EMOTIONS)
        self.assertLessEqual(
            set(EMOTION_TO_TTS.values()),
            offered,
            "the bridge maps to an emotion the settings screen does not offer",
        )

    def test_every_value_is_in_the_plugins_own_literal(self):
        """A copy, so something has to police the copy.

        `emotion_voice` does not import livekit at module level either, for the
        same reason `providers.py` does not: the module has to be importable and
        testable on its own. The cost is a transcription that can fall behind,
        and a stale name only fails at synthesis time.
        """
        try:
            from livekit.plugins.cartesia.models import TTSVoiceEmotion
        except ImportError:  # pragma: no cover - plugin not installed
            self.skipTest("livekit-plugins-cartesia is not installed")

        drifted = set(EMOTION_TO_TTS.values()) - set(typing.get_args(TTSVoiceEmotion))
        self.assertEqual(
            drifted,
            set(),
            "the bridge maps to an emotion the plugin's Literal does not have",
        )

    def test_the_supported_family_is_the_plugins_own_predicate(self):
        """`sonic-3` is written down twice, so the two copies are compared.

        A gate one model wide in either direction is invisible until somebody
        configures a voice whose slider does nothing.
        """
        try:
            from livekit.plugins.cartesia.models import _is_sonic_3
        except ImportError:  # pragma: no cover - plugin not installed
            self.skipTest("livekit-plugins-cartesia is not installed")

        family = [m for m in ("sonic-3", "sonic-3.5", "sonic-3.6", "sonic-2", "sonic-turbo")]
        gated = tuple(m for m in family if _is_sonic_3(m))
        self.assertEqual(
            gated,
            tuple(m for m in family if m.startswith(SUPPORTED_MODEL_PREFIX)),
            "the bridge's model family has drifted from the plugin's predicate",
        )

    def test_several_emotions_may_share_one_target(self):
        """The two vocabularies differ in size, and collapsing is the point.

        Asserted so that a later "cleanup" of the duplicates is a deliberate
        change rather than an accidental one.
        """
        self.assertGreater(len(EMOTION_TO_TTS), len(set(EMOTION_TO_TTS.values())))


class SupportsVoiceControlsTests(unittest.TestCase):
    def test_nothing_to_talk_to(self):
        self.assertFalse(supports_voice_controls(None))

    def test_a_provider_without_the_control(self):
        tts = FakeTTS()
        tts.provider = "ElevenLabs"
        self.assertFalse(supports_voice_controls(tts))

    def test_a_model_that_cannot_take_it(self):
        tts = FakeTTS()
        tts.model = "sonic-2"
        self.assertFalse(
            supports_voice_controls(tts),
            "sonic-2 only logs that the control was ignored, so applying it "
            "would look like expressiveness and deliver silence",
        )

    def test_no_update_options_at_all(self):
        self.assertFalse(supports_voice_controls(object()))

    def test_the_case_that_works(self):
        self.assertTrue(supports_voice_controls(FakeTTS()))


class FirstVoiceEmotionTests(unittest.TestCase):
    def test_nothing_in_is_nothing_out(self):
        self.assertIsNone(first_voice_emotion(""))
        self.assertIsNone(first_voice_emotion("   "))

    def test_a_plain_sentence_is_not_evidence(self):
        """The neutral *fallback* is silence, not a demonstration of neutral.

        Reading it as an emotion would set the voice to Neutral on every reply
        that said nothing about how it was said, replacing whatever the profile
        was configured with.
        """
        self.assertIsNone(first_voice_emotion(NO_EMOTION))

    def test_the_word_neutral_is_alone_not_a_demonstration(self):
        self.assertIsNone(first_voice_emotion("neutral"))

    def test_a_marker_names_its_emotion(self):
        self.assertEqual(first_voice_emotion(HAS_EMOTION), "amused")

    def test_a_sentence_can_demonstrate_one_too(self):
        # A marker is the common case, not the only one: ordinary prose scores
        # just as well, so a reply that never writes `*action*` is still heard.
        self.assertEqual(first_voice_emotion("That made me angry, truly."), "angry")

    def test_the_first_demonstrated_emotion_wins(self):
        """The reply has one tone, so the first clause sets it.

        The losing clause here is deliberately the *stronger* reading — 0.67
        against 0.51 — because "strongest wins" is the tempting rule and it is
        the wrong one: it would flip the voice partway through a reply on the
        strength of an aside, after the opening line was already spoken under
        the other control.
        """
        self.assertEqual(
            first_voice_emotion("I am angry. *giggles* so happy!"),
            "angry",
        )


class ApplyEmotionTests(unittest.TestCase):
    def test_applies_the_mapped_emotion(self):
        tts = FakeTTS()
        self.assertTrue(apply_emotion(tts, "happy"))
        self.assertEqual(tts.calls, [{"emotion": ["Happy"]}])

    def test_an_emotion_with_no_target_changes_nothing(self):
        tts = FakeTTS()
        # `shy` is one of the eight deliberately unmapped emotions.
        self.assertFalse(apply_emotion(tts, "shy"))
        self.assertEqual(tts.calls, [])

    def test_a_synthesizer_that_refuses_does_not_raise(self):
        self.assertFalse(apply_emotion(RefusingTTS(), "happy"))

    def test_no_emotion_is_not_a_request(self):
        tts = FakeTTS()
        self.assertFalse(apply_emotion(tts, None))
        self.assertEqual(tts.calls, [])


class TurnVoiceControlsTests(unittest.TestCase):
    def test_no_synthesizer_means_nothing_to_do(self):
        controls = TurnVoiceControls(None)
        self.assertFalse(controls.active)
        self.assertFalse(controls.observe("I am happy!"))

    def test_it_applies_once_and_then_stops_looking(self):
        tts = FakeTTS()
        controls = TurnVoiceControls(tts)
        self.assertTrue(controls.active)
        self.assertTrue(controls.observe("I am happy"))
        self.assertFalse(controls.active)
        self.assertEqual(len(tts.calls), 1)

        # The rest of the reply must not be able to re-open the decision.
        controls.observe(" and now I am angry")
        self.assertEqual(len(tts.calls), 1)

    def test_it_keeps_looking_until_it_has_evidence(self):
        tts = FakeTTS()
        controls = TurnVoiceControls(tts)
        self.assertFalse(controls.observe(NO_EMOTION))
        self.assertTrue(controls.active, "one dull clause is not a verdict")
        self.assertTrue(controls.observe(" I am angry"))
        self.assertEqual(tts.calls, [{"emotion": ["Angry"]}])

    def test_it_gives_up_at_the_scan_budget(self):
        tts = FakeTTS()
        controls = TurnVoiceControls(tts)
        # Sliced so the accumulation crosses the budget mid-flight rather than
        # arriving as one obviously-too-long piece.
        chunk = NO_EMOTION + " "
        for _ in range(MAX_SCAN_CHARS // len(chunk) + 2):
            controls.observe(chunk)
        self.assertFalse(controls.active)
        self.assertEqual(tts.calls, [])

    def test_a_refusing_synthesizer_does_not_end_the_scan_unfinished(self):
        controls = TurnVoiceControls(RefusingTTS())
        self.assertFalse(controls.observe("I am happy"))
        self.assertFalse(
            controls.active,
            "the decision was made; a synthesizer that refused it must not be "
            "asked again on every following chunk",
        )


class ConfiguredVoiceSurvivesTests(unittest.TestCase):
    """The synthesizer is shared for the session, so a reply must not inherit.

    Ordering is covered elsewhere: ``test_the_reply_reaches_the_voice_before_
    it_is_handed_on`` proves that observing happens before a chunk is yielded,
    and the restore is the *first* thing an observation does, so it inherits
    that guarantee by composition rather than by being re-proved here.
    """

    def test_a_reply_that_shows_nothing_returns_to_the_profile(self):
        tts = FakeTTS()
        emotion_voice.remember_baseline(tts, None)

        TurnVoiceControls(tts).observe("That made me angry, truly.")
        self.assertEqual(tts.calls[-1], {"emotion": ["Angry"]})

        TurnVoiceControls(tts).observe(NO_EMOTION)
        self.assertEqual(
            tts.calls[-1],
            {"emotion": None},
            "the profile's own setting has to come back, not the last reply's mood",
        )

    def test_a_configured_colour_is_restored_not_cleared(self):
        """The settings screen offers `emotion`, so it has to still mean something.

        A bridge that blanked it on every quiet reply would win the expressiveness
        argument by quietly making one of the provider's settings inert.
        """
        tts = FakeTTS()
        emotion_voice.remember_baseline(tts, "Calm")

        TurnVoiceControls(tts).observe("That made me angry, truly.")
        TurnVoiceControls(tts).observe(NO_EMOTION)

        self.assertEqual(tts.calls[-1], {"emotion": "Calm"})

    def test_nothing_written_down_means_nothing_is_guessed(self):
        """Unremembered is not an instruction to clear.

        A synthesizer built outside `_build_tts` — a preview, a test — keeps
        whatever it was given rather than having it wiped on a guess.
        """
        tts = FakeTTS()
        TurnVoiceControls(tts).observe(NO_EMOTION)
        self.assertEqual(tts.calls, [])

    def test_the_restore_happens_once_per_reply_not_once_per_chunk(self):
        tts = FakeTTS()
        emotion_voice.remember_baseline(tts, None)

        controls = TurnVoiceControls(tts)
        for _ in range(5):
            controls.observe(NO_EMOTION)

        self.assertEqual(len(tts.calls), 1)


class LumineNodeTests(unittest.TestCase):
    """The override itself — that it exists, and that it passes text through."""

    @staticmethod
    def _classes():
        """`Lumine` and the base it overrides, or skip.

        Both halves together: `agent.agent` imports fine without livekit (it
        keeps a stub), so guarding the import of `Lumine` alone would let these
        tests run and then fail on the missing `Agent`.
        """
        try:
            from agent.agent import Lumine
            from livekit.agents import Agent
        except Exception as exc:  # pragma: no cover - import-time environments
            raise unittest.SkipTest(f"agent module unavailable: {exc}") from exc
        return Lumine, Agent

    def test_llm_node_is_an_async_generator(self):
        import inspect

        Lumine, Agent = self._classes()
        self.assertTrue(inspect.isasyncgenfunction(Lumine.llm_node))
        # The parameter names are the contract with the framework; renaming one
        # breaks the override silently, because Python does not check them
        # against the base class.
        self.assertEqual(
            inspect.signature(Lumine.llm_node).parameters.keys(),
            inspect.signature(Agent.llm_node).parameters.keys(),
        )

    def test_the_reply_reaches_the_voice_before_it_is_handed_on(self):
        """The whole feature, end to end.

        The fake LLM records what the synthesizer had already been told at the
        moment each piece of text *leaves* the node. If the control landed one
        clause late — which is what the obvious implementation does — this
        fails, and nothing else in the product would have noticed.
        """
        Lumine, Agent = self._classes()
        tts = FakeTTS()
        agent = Lumine()
        agent._get_activity_or_raise = lambda: types.SimpleNamespace(tts=tts)

        told_at_departure: list[list[dict]] = []

        async def fake_default(_agent, _ctx, _tools, _settings):
            yield types.SimpleNamespace(delta=types.SimpleNamespace(content="I am "))
            yield types.SimpleNamespace(delta=types.SimpleNamespace(content="happy"))
            # Everything up to this point has now left the node.
            told_at_departure.append(list(tts.calls))
            yield types.SimpleNamespace(delta=types.SimpleNamespace(content="to see you."))
            # A sentinel has no text and must not be read as any.
            yield types.SimpleNamespace()

        original = Agent.default.llm_node
        Agent.default.llm_node = fake_default
        self.addCleanup(setattr, Agent.default, "llm_node", original)

        async def run() -> list:
            return [chunk async for chunk in agent.llm_node(None, None, None)]

        delivered = asyncio.run(run())

        self.assertEqual(len(delivered), 4, "the node must pass every chunk through")
        self.assertEqual(
            tts.calls,
            [{"emotion": ["Happy"]}],
            "exactly one control per reply",
        )
        self.assertEqual(
            told_at_departure,
            [[{"emotion": ["Happy"]}]],
            "the voice was still unset when the text that revealed it left",
        )

    def test_a_turn_without_a_synthesizer_is_untouched(self):
        Lumine, Agent = self._classes()
        agent = Lumine()
        agent._get_activity_or_raise = lambda: types.SimpleNamespace(tts=None)

        async def fake_default(_agent, _ctx, _tools, _settings):
            yield "I am happy to see you."

        original = Agent.default.llm_node
        Agent.default.llm_node = fake_default
        self.addCleanup(setattr, Agent.default, "llm_node", original)

        async def run() -> list:
            return [chunk async for chunk in agent.llm_node(None, None, None)]

        # Bare-str chunks are still text, and still worth observing.
        self.assertEqual(asyncio.run(run()), ["I am happy to see you."])


if __name__ == "__main__":
    unittest.main()
