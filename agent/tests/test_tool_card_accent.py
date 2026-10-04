"""The card's colour rule, as a test.

A colour rule is a *rule*, not decoration, and the only way to tell the
difference is to count. Both earlier rules failed that count and both read like
obvious improvements in a diff:

  - "asks you first"   9/17  -- the accent meant "a tool"
  - "can act at all"  11/17  -- worse

So the invariant is that the accented partition is a **minority**, and that it is
specifically `sends`. Stated here rather than in the CSS because a stylesheet
cannot count, and the next person to move this accent will have the same reason
to move it that I did.

Also pinned: nothing else on a card uses `--color-accent`. An accent that leaks
onto a second element is the same failure one level down.
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

# `parents[1]` is the repository root only when the suite is discovered from it.
# Walk up until the stylesheet is found rather than assuming a depth: the agent
# tests are also run as `python -m unittest discover -s agent\tests`, where
# `__file__` is relative, and a test that cannot find the file it is testing is
# a test that silently stops testing.
ROOT = next(
    p
    for p in Path(__file__).resolve().parents
    if (p / "lumine-ui" / "src" / "pages" / "index.css").exists()
)
sys.path.insert(0, str(ROOT))

CSS = (ROOT / "lumine-ui" / "src" / "pages" / "index.css").read_text(encoding="utf-8")
CARD_CSS = CSS[CSS.index("A tool card.") : CSS.index(".permission-stack")]


def catalog():
    proc = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "agent/tool_catalog.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise AssertionError(f"tool_catalog.py failed:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout)["tools"]


class AccentIsAMinorityTests(unittest.TestCase):
    def setUp(self):
        self.tools = catalog()

    def test_the_accented_partition_is_a_minority_of_the_cards(self):
        sends = [t for t in self.tools if t["effect"] == "sends"]
        self.assertLessEqual(
            len(sends) * 2,
            len(self.tools),
            f"{len(sends)}/{len(self.tools)} cards are accented, which is not a "
            f"minority -- so the accent no longer means anything",
        )

    def test_it_is_a_minority_and_not_simply_a_different_partition(self):
        # Named explicitly, because "minority" alone would also be satisfied by
        # "no parameters" (1/17), which is not what the colour is for.
        sends = [t for t in self.tools if t["effect"] == "sends"]
        self.assertEqual(
            [t["id"] for t in sends],
            ["send_whatsapp"],
            "the accented card is no longer the one that messages people",
        )

    def test_the_rejected_partitions_are_still_majorities(self):
        # Guards the *reason*. If asking ever became rare, the accent should move
        # back, and this is what tells you so.
        asks = [t for t in self.tools if t["permission"] == "ask"]
        acts = [t for t in self.tools if t["effect"] != "answers"]
        for name, group in (("asks", asks), ("acts", acts)):
            self.assertGreater(
                len(group) * 2,
                len(self.tools),
                f"{name} is now a minority ({len(group)}/{len(self.tools)}), so the "
                f"accent belongs on it and this expectation should change",
            )


class AccentHasOneJobTests(unittest.TestCase):
    def test_only_one_rule_in_the_card_paints_text_with_the_accent(self):
        # Scoped to *text colour*, not to any mention of the accent. The focus
        # ring is legitimately the accent -- it is how you say "this is the
        # element you are on" -- but a ring is not a competing emphasis, and
        # counting every occurrence would have made this test fail for a correct
        # rule and get deleted rather than fixed.
        rules = re.findall(
            r"\.tool-card[^{]*\{[^}]*color:\s*var\(--color-accent\)[^}]*\}",
            CARD_CSS,
        )
        self.assertEqual(
            len(rules),
            1,
            "--color-accent paints text in "
            f"{len(rules)} card rules; it has one job:\n" + "\n".join(rules),
        )

    def test_the_focus_ring_is_still_the_accent(self):
        # Stated so the test above cannot be satisfied by deleting it.
        self.assertIn("focus-within", CARD_CSS)
        self.assertRegex(CARD_CSS, r"\.tool-card:focus-within\s*\{[^}]*--color-accent")

    def test_the_accented_rule_is_the_sends_one(self):
        self.assertIn(".tool-card-meta .is-sends", CARD_CSS)
        self.assertNotIn(".tool-card-meta .is-reach", CARD_CSS)
        self.assertNotIn(".tool-card-meta .is-asks {\n  color: var(--color-accent)", CARD_CSS)


if __name__ == "__main__":
    unittest.main(verbosity=2)