"""The always-sent system prompt, and the detail held back from it.

The persona was ~2,700 tokens prepended to every request. Splitting it into a
short core plus an on-demand reference is only worth anything if the core really
is much smaller, and if the detail is genuinely still reachable -- otherwise the
optimisation has quietly deleted a chunk of the product's character.
"""

import unittest
from pathlib import Path

from agent.tools.persona import PERSONA_PATH, _sections, find_sections, recall
from agent.tools.tools_policy import TOOL_USAGE_POLICY, compose_instructions

AGENT_DIR = Path(__file__).resolve().parent.parent
CORE_PATH = AGENT_DIR / "prompts" / "persona_core.md"
FULL_PATH = AGENT_DIR / "prompts" / "persona.md"

#: A ratio, not an absolute budget. What matters is that the always-sent prompt is
#: a fraction of the old one; a hard character limit would just get bumped every
#: time the persona grows.
MAX_CORE_RATIO = 0.5


class PersonaCoreTests(unittest.TestCase):
    def test_the_core_exists_and_is_not_empty(self):
        self.assertTrue(CORE_PATH.is_file(), "persona_core.md is missing")
        self.assertGreater(len(CORE_PATH.read_text(encoding="utf-8").strip()), 0)

    def test_the_core_is_much_smaller_than_the_full_persona(self):
        core = len(CORE_PATH.read_text(encoding="utf-8"))
        full = len(FULL_PATH.read_text(encoding="utf-8"))
        self.assertLess(
            core,
            full * MAX_CORE_RATIO,
            f"the always-sent core is {core} of {full} characters; "
            f"it is supposed to be under {int(full * MAX_CORE_RATIO)}",
        )

    def test_the_full_persona_is_still_on_disk_unchanged(self):
        # The split is about *when* the persona is read, never *what* it says.
        # If this file is edited to match the core, the recall tool has nothing to
        # recall and the split has silently become a deletion.
        full = FULL_PATH.read_text(encoding="utf-8")
        self.assertGreater(len(full), 5000)
        for section in ("Values", "Islamic Alignment", "Emotional Awareness", "Humor"):
            self.assertIn(f"# {section}", full)

    def test_the_core_keeps_the_traits_that_shape_every_reply(self):
        core = CORE_PATH.read_text(encoding="utf-8").lower()
        for trait in ("gentle", "honest", "kindness", "language", "short"):
            self.assertIn(trait, core, f"the core lost '{trait}', which shapes every reply")

    def test_the_policy_still_goes_after_the_core(self):
        # Order matters: the tool policy is the more specific rule, and it reads
        # as a correction of a more general persona rather than the other way
        # round.
        composed = compose_instructions("CORE")
        self.assertTrue(composed.startswith("CORE"))
        self.assertIn(TOOL_USAGE_POLICY.splitlines()[0], composed)


class PersonaRecallTests(unittest.TestCase):
    def test_every_top_level_section_is_indexed(self):
        sections = _sections()
        self.assertGreaterEqual(len(sections), 20)

    def test_a_heading_is_found_by_name(self):
        self.assertIn("Emotional Awareness", find_sections("emotional awareness"))

    def test_a_phrased_intent_is_found_even_without_a_matching_word(self):
        # "happy" appears in no heading, which is the case that matters: the model
        # asks about how it should feel, not about a section title.
        self.assertIn("Emotional Awareness", find_sections("how do you show happiness"))

    def test_question_words_do_not_decide_the_match(self):
        # Matching on "what" would send a question about religion to a section
        # titled "What Makes Her Feel Human".
        self.assertNotIn("What Makes Her Feel Human", find_sections("what is my religion"))

    def test_an_unmatched_topic_returns_nothing_rather_than_a_guess(self):
        self.assertEqual(find_sections("qwertyuiop asdfgh"), [])

    def test_an_unmatched_recall_names_what_is_available(self):
        # A dead end wastes the turn. Listing the sections costs one more call and
        # the model can pick a real heading from it.
        import json

        payload = json.loads(recall("qwertyuiop asdfgh"))
        self.assertIn("sections", payload)
        self.assertIn("Emotional Awareness", payload["sections"])

    def test_a_recall_is_bounded(self):
        # The whole point is to replace a large always-sent block with a small
        # occasional one. An unbounded recall would defeat it.
        from agent.tools.tool_results import MAX_RESULT_CHARS

        self.assertLessEqual(len(recall("language switching")), MAX_RESULT_CHARS)

    def test_the_recall_reads_the_full_persona_not_the_core(self):
        self.assertEqual(PERSONA_PATH, FULL_PATH)


if __name__ == "__main__":
    unittest.main()
