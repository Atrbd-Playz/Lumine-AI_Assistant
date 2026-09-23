import unittest

from agent.emotion_contract import (
    EmotionIntent,
    EmotionResponse,
    build_emotion_event,
    extract_emotion_sequence,
    infer_emotion_from_text,
    normalize_emotion,
    validate_emotion_event,
)


class EmotionContractTests(unittest.TestCase):
    def test_normalize_emotion_keeps_canonical_values(self):
        self.assertEqual(normalize_emotion("happy"), "happy")
        self.assertEqual(normalize_emotion("jealous"), "jealous")
        self.assertEqual(normalize_emotion("idle"), "neutral")
        self.assertEqual(normalize_emotion("neutral"), "neutral")

    def test_unknown_emotion_falls_back_to_neutral(self):
        self.assertEqual(normalize_emotion("foobar"), "neutral")
        event = build_emotion_event({"primary": "foobar", "intensity": 0.9, "source": "llm"})
        self.assertEqual(event["payload"]["primary"], "neutral")

    def test_validate_emotion_event_accepts_expected_shape(self):
        payload = {
            "primary": "happy",
            "secondary": "loving",
            "intensity": 0.84,
            "source": "llm",
        }
        self.assertTrue(validate_emotion_event(payload))

    def test_emotion_response_model_has_separate_response_and_emotion(self):
        item = EmotionResponse(
            response="Aww, thank you, Master!",
            emotion={
                "primary": "happy",
                "secondary": "loving",
                "intensity": 0.8,
                "source": "llm",
            },
        )
        self.assertEqual(item.response, "Aww, thank you, Master!")
        self.assertEqual(item.emotion["primary"], "happy")

    def test_build_emotion_event_creates_versioned_runtime_envelope(self):
        event = build_emotion_event({
            "primary": "happy",
            "secondary": "loving",
            "intensity": 0.8,
            "source": "llm",
        })
        self.assertEqual(event["type"], "lumine.emotion")
        self.assertEqual(event["payload"]["primary"], "happy")
        self.assertEqual(event["payload"]["source"], "llm")

    def test_missing_emotion_defaults_to_neutral_zero_intensity(self):
        event = build_emotion_event({"source": "llm"})
        self.assertEqual(event["payload"]["primary"], "neutral")
        self.assertEqual(event["payload"]["intensity"], 0.0)

    def test_roleplay_resolver_uses_user_intent(self):
        self.assertEqual(infer_emotion_from_text("Act cute.", source="user")["payload"]["primary"], "playful")
        self.assertEqual(infer_emotion_from_text("Act jealous.", source="user")["payload"]["primary"], "jealous")
        self.assertEqual(infer_emotion_from_text("Be angry with me.", source="user")["payload"]["primary"], "angry")

    def test_factual_resolver_stays_neutral(self):
        event = infer_emotion_from_text("What is Python?", source="user")
        self.assertEqual(event["payload"]["primary"], "neutral")
        self.assertEqual(event["payload"]["intensity"], 0.0)

    def test_non_english_emotion_keywords_resolve_naturally(self):
        bangla = infer_emotion_from_text("আমি খুব খুশি এবং আনন্দিত।", source="user")
        self.assertEqual(bangla["payload"]["primary"], "happy")

        arabic = infer_emotion_from_text("أنا متحمس جدًا لهذا!", source="user")
        self.assertEqual(arabic["payload"]["primary"], "excited")

        confused = infer_emotion_from_text("আমি বুঝতে পারছি না, এটা কী?", source="user")
        self.assertEqual(confused["payload"]["primary"], "confused")

    def test_roleplay_cover_expands_shy_and_other_variants(self):
        shy = infer_emotion_from_text("Act shy and blush a little.", source="user")
        self.assertEqual(shy["payload"]["primary"], "shy")

        proud = infer_emotion_from_text("Be proud, calm, and curious.", source="user")
        self.assertIn(proud["payload"]["primary"], {"proud", "calm", "curious"})

    def test_extract_emotion_sequence_supports_multiple_moods_in_one_message(self):
        sequence = extract_emotion_sequence("Act shy, then laugh happily, then get excited.", source="user")
        self.assertGreaterEqual(len(sequence), 2)
        emotions = {event["payload"]["primary"] for event in sequence}
        self.assertTrue({"shy", "happy", "excited"} & emotions)


if __name__ == "__main__":
    unittest.main()
