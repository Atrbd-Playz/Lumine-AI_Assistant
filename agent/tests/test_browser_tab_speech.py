"""Tabs said the way people say them, and what each one costs the user.

The registry and the wiring are in `test_browser_tabs.py` and
`test_browser_tab_wiring.py`. This file is the *language* layer, and it exists
because it was the layer that was broken.

The first version of `parse_tab_request` wanted an exact word. Measured against
what people actually say, **almost nothing worked**: "delete the 1st tab",
"delete the first tab", "close the first tab", "tab 2", "close tab 2",
"switch to tab 3", "what tabs", "show tabs", "how many tabs", "next tab",
"previous tab", "close this tab", "remove tab 2" — all refused.

Three were worse than refused, because they were silently wrong:

    "open a new tab"            -> a new tab addressed to the string "a new tab"
    "new tab"                   -> a new tab addressed to the string "tab"
    "open tab 4"                -> a new tab addressed to the string "tab 4"

The first two tried to navigate to `https://a new tab`. So the rule here is the
opposite of the old one: find the operation and the target *anywhere* in the
sentence, and refuse only what is genuinely ambiguous. **A refusal here is not a
dead end** — the model retries, and the retry is where a second approval card
comes from, which is worse than the tab question ever was.
"""

from __future__ import annotations

import unittest

from agent.tools.browser import actions


class SpokenRequestTests(unittest.TestCase):
    """Every phrasing measured, with what it must resolve to."""

    def test_the_words_that_were_refused(self):
        # The reported failures. Each one used to come back refused, and a refusal
        # sends the model looking for another way to do it.
        for said, want in [
            ("delete the 1st tab", ("close", "1")),
            ("delete the first tab", ("close", "1")),
            ("close the first tab", ("close", "1")),
            ("close the 2nd tab", ("close", "2")),
            ("close the third tab", ("close", "3")),
            ("delete the last tab", ("close", "last")),
            ("remove tab 2", ("close", "2")),
            ("delete that tab", ("close", "")),
            ("close this tab", ("close", "")),
            ("close it", ("close", "")),
            ("tab 2", ("switch", "2")),
            ("close tab 2", ("close", "2")),
            ("switch to tab 3", ("switch", "3")),
            ("switch to the second tab", ("switch", "2")),
            ("go back to the first tab", ("switch", "1")),
            ("what tabs", ("list", "")),
            ("show tabs", ("list", "")),
            ("how many tabs", ("list", "")),
            ("next tab", ("switch", "+1")),
            ("previous tab", ("switch", "-1")),
            ("go to the other tab", ("switch", "+1")),
            ("switch tabs", ("switch", "+1")),
        ]:
            with self.subTest(said=said):
                self.assertEqual(actions.parse_tab_request(said), want)

    def test_open_a_new_tab_names_no_page(self):
        # It used to name `https://a new tab`. Three phrasings, all of which
        # produced a navigation to a nonsense address and reported it as an open.
        for said in ("new", "new tab", "open a new tab", "open another tab",
                     "open a new tab for youtube", "new tab example"):
            with self.subTest(said=said):
                op, address = actions.parse_tab_request(said)
                self.assertEqual(op, "new")
                self.assertEqual(address, "", f"{said!r} invented the address {address!r}")

    def test_open_with_a_number_is_a_switch_not_a_new_tab(self):
        # "open tab 4" was read as a new tab *addressed to* "tab 4".
        self.assertEqual(actions.parse_tab_request("open tab 4"), ("switch", "4"))

    def test_a_real_address_survives_intact(self):
        # Taken from the original text rather than the tokens: tokenising
        # `https://example.com` gives `https example com`, and putting that back
        # together produced the address `com`.
        for said, want in [
            ("new tab https://example.com", "https://example.com"),
            ("open a new tab at https://example.com/x?y=1", "https://example.com/x?y=1"),
            ("new tab www.example.com", "www.example.com"),
            ("open a new tab for example.com", "example.com"),
        ]:
            with self.subTest(said=said):
                op, address = actions.parse_tab_request(said)
                self.assertEqual(op, "new")
                self.assertEqual(address, want)

    def test_asking_about_tabs_is_never_a_refusal(self):
        # A sentence with nothing recognisable is a question about them. Refusing
        # it is what sends the model off to press a keyboard shortcut instead.
        for said in ("", "   ", "tabs", "?", "tab stuff", "whatever"):
            with self.subTest(said=said):
                self.assertEqual(actions.parse_tab_request(said)[0], "list")

    def test_the_rest_of_the_tabs_are_the_active_one(self):
        # No target means the tab in use, which is why a target is optional at
        # all -- and why "close" alone is a complete request.
        self.assertEqual(actions.parse_tab_request("close"), ("close", ""))
        self.assertEqual(actions.parse_tab_request("delete"), ("close", ""))

    def test_closing_the_others_is_not_closing_one_of_them(self):
        # "close the other tabs" destroys several pages and "close the other tab"
        # destroys one. A tool that cannot tell them does one when asked for the
        # other, and there is no way to know which single one was meant.
        for said in ("close the other tabs", "close all the others", "close the rest"):
            with self.subTest(said=said):
                self.assertEqual(actions.parse_tab_request(said)[0], "close_others")

    def test_a_bare_number_is_a_switch(self):
        self.assertEqual(actions.parse_tab_request("7"), ("switch", "7"))

    def test_one_is_not_read_as_tab_one(self):
        # "no one", "anyone", "one of them". Closing tab 1 because the sentence
        # contained "one" destroys a page the user never named -- so a close that
        # named "one" and nothing else is *refused* rather than resolved, and a
        # close that also said "this" means the tab in use.
        self.assertEqual(actions.parse_tab_request("close this one"), ("close", ""))
        for said in ("close one of them", "close one"):
            with self.subTest(said=said):
                with self.assertRaises(ValueError):
                    actions.parse_tab_request(said)

    def test_last_is_a_tab_and_not_a_direction(self):
        # It was `-1`, which collided with "previous". "the last tab" is a
        # *specific* tab; "the previous one" is a direction.
        self.assertEqual(actions.parse_tab_request("delete the last tab"), ("close", "last"))
        self.assertEqual(actions.parse_tab_request("previous tab"), ("switch", "-1"))

    def test_every_operation_is_reachable(self):
        seen = {actions.parse_tab_request(said)[0] for said in (
            "", "new", "2", "close tab 1", "close the other tabs",
        )}
        self.assertEqual(seen, set(actions.TAB_OPS))


class WhatEachTabOperationCostsTests(unittest.TestCase):
    """The classes, and the reason they are not all the same."""

    def test_switching_and_opening_are_reads(self):
        # The "she always asks" report. The test used across this tool is whether
        # the act changes anything *off* the screen: switching changes which of the
        # pages the agent already opened is in front, exactly as `scroll` moves a
        # viewport and `back` walks history. Asking about the agent's own
        # housekeeping interrupts a voice turn for nothing.
        for op in ("list", "switch", "new"):
            with self.subTest(op=op):
                self.assertEqual(actions.TAB_ACTION_CLASS[op], "read")

    def test_closing_is_an_act(self):
        # The only tab operation that can lose something. The asymmetry is the
        # point: what cannot be lost never interrupts, what can always does.
        for op in ("close", "close_others"):
            with self.subTest(op=op):
                self.assertEqual(actions.TAB_ACTION_CLASS[op], "act")

    def test_the_table_covers_every_operation(self):
        self.assertEqual(set(actions.TAB_ACTION_CLASS), set(actions.TAB_OPS))

    def test_both_browser_tools_charge_the_same(self):
        # Two tools drive one browser. Two copies of this table is how they end up
        # charging the user differently for the same request, so the table lives
        # here and both import it.
        from agent.tools.browser import control, tools

        self.assertIs(tools.TAB_ACTION_CLASS, actions.TAB_ACTION_CLASS)
        self.assertIs(control.TAB_ACTION_CLASS, actions.TAB_ACTION_CLASS)

    def test_both_browser_tools_ask_with_the_same_verb(self):
        # `permission_gate` collapses two pending cards into one by matching
        # (tool, verb, summary) -- and cannot collapse two cards that name
        # themselves differently. `web_control` used to ask with `switchtab` where
        # `browse` asked with `tab`, so a model that tried one and then the other
        # produced two cards for one request.
        from agent.tools.browser import control, tools

        self.assertEqual(tools.TAB_ASK_VERB, "tab")
        self.assertEqual(control.TAB_ASK_VERB, tools.TAB_ASK_VERB)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()