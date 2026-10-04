"""A snapshot must not raise on the fields a page's script actually returns.

The bug these pin is one line long and made `web_control` fail on essentially
every call against a page with more than one frame. `GenerationGate.stamp` read
`payload["tabs"]` as a count and called `int()` on it, but `tabs` is a *list* of
tab descriptors everywhere else in the module: the `tabs` action returns one,
`_JS_TABS` produces one, and the snapshot script's own failure path sets it to
`[]`. The result was

    TypeError: int() argument must be a string, a bytes-like object or a real
    number, not 'list'

surfacing to the user as a bare tool failure with a Python message and no
address. Six of those in one session, each looking like a different problem,
because the one piece of information needed to find the cause was the very thing
that crashed.

So the tests here are deliberately about *shape tolerance* rather than counting:
a snapshot is built from whatever a page returned, and no single unexpected field
may be the reason a read fails. That direction is also the cheaper failure -- a
tab count of zero costs nothing, a crashed tool costs the user their request.
"""

from __future__ import annotations

import unittest

from agent.tools.browser import engine, state

#: What the snapshot script puts in `tabs`: a list of descriptors, not a number.
#: Taken from `control.py`'s `tabs` action, which returns exactly this shape.
TABS_PAYLOAD = [
    {"name": "Results", "selected": True, "url": "https://www.youtube.com/results?search_query=a+song"},
    {"name": "", "selected": False, "url": "https://www.youtube.com/"},
]

SNAPSHOT_PAYLOAD = {
    "url": "https://www.youtube.com/results?search_query=a+song",
    "title": "a song - YouTube",
    "text": "results",
    "tabs": TABS_PAYLOAD,
    "ready": "ready",
}


class TabCountTests(unittest.TestCase):
    def test_a_list_of_tabs_is_counted_and_not_crashed_on(self):
        # The regression itself.
        self.assertEqual(engine._tab_count(SNAPSHOT_PAYLOAD), 2)

    def test_the_empty_list_the_failure_path_produces_counts_zero(self):
        self.assertEqual(engine._tab_count({"tabs": []}), 0)

    def test_a_missing_field_counts_zero(self):
        self.assertEqual(engine._tab_count({"url": "https://x/"}), 0)

    def test_a_caller_that_already_counted_is_still_honoured(self):
        self.assertEqual(engine._tab_count({"tabs": 4}), 4)

    def test_nothing_a_page_returns_can_raise(self):
        # Every shape worth being defensive about, because the whole point is
        # that a field the module did not expect costs a count and nothing more.
        for payload in (
            None, [], "nope", 7,
            {"tabs": None}, {"tabs": "3"}, {"tabs": {}}, {"tabs": True},
            {"tabs": [1, 2, 3]},
        ):
            self.assertIsInstance(engine._tab_count(payload), int, payload)

    def test_a_negative_or_absurd_count_never_reaches_the_snapshot(self):
        for value in (-5, "abc", None, [1]):
            self.assertGreaterEqual(state._as_count(value), 0)


class SnapshotTests(unittest.TestCase):
    def test_a_snapshot_with_tabs_is_built_and_carries_the_count(self):
        snapshot = state.snapshot_from_script(7, SNAPSHOT_PAYLOAD)
        self.assertEqual(snapshot.tab_count, 2)
        self.assertEqual(snapshot.url, "https://www.youtube.com/results?search_query=a+song")

    def test_stamping_a_real_payload_does_not_raise(self):
        # The path that failed: `GenerationGate.stamp` on a payload carrying the
        # list. Called through the gate rather than the helper, because the helper
        # being correct says nothing about the call site that was wrong.
        gate = engine.GenerationGate()
        snapshot = gate.stamp(dict(SNAPSHOT_PAYLOAD))
        self.assertEqual(snapshot.tab_count, 2)
        self.assertEqual(snapshot.generation, 1)

    def test_stamping_a_payload_with_no_tabs_still_works(self):
        gate = engine.GenerationGate()
        snapshot = gate.stamp({"url": "https://example.com/", "text": "", "candidates": []})
        self.assertEqual(snapshot.tab_count, 0)

    def test_the_generation_gate_keeps_working_after_a_stamp(self):
        # A stamp that raised would leave the store empty, so every later action
        # would refuse with "observe the page first" -- the failure would outlive
        # the crash that caused it and point somewhere else entirely.
        gate = engine.GenerationGate()
        snapshot = gate.stamp(dict(SNAPSHOT_PAYLOAD))
        self.assertEqual(gate.check(snapshot.generation), "")
        self.assertIn("observe it again", gate.check(snapshot.generation + 1))


if __name__ == "__main__":
    unittest.main()