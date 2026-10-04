"""The desktop operation table, and the four rules that live in it.

Everything here is a decision that would be easy to get wrong one call site at
a time, so each class of test is named after the mistake it prevents rather
than the function it exercises.
"""

from __future__ import annotations

import unittest

from agent.runtime import permission_gate
from agent.tools.desktop import ops


class TableShapeTests(unittest.TestCase):
    """The table has to be complete, and it has to agree with the gate."""

    def test_every_verb_has_a_row(self):
        for verb in ops.VERBS:
            self.assertIsNotNone(ops.operation(verb), f"{verb} has no row")

    def test_rows_are_unique(self):
        self.assertEqual(len(ops.VERBS), len({op.verb for op in ops.OPERATIONS}))

    def test_every_class_exists_in_the_gate(self):
        # A class invented here and not in permission_gate would be resolved
        # as "unknown" there and quietly become `sensitive`. That is the safe
        # direction, but it means the table is lying about what it declared.
        for op in ops.OPERATIONS:
            self.assertIn(op.action_class, permission_gate.ACTION_CLASSES, op.verb)

    def test_the_badge_is_the_strictest_row(self):
        strictest = max(op.action_class for op in ops.OPERATIONS)
        self.assertEqual(ops.tool_action_class(), strictest)

    def test_an_unknown_verb_is_sensitive(self):
        # Fail closed: a verb that slipped past validation must cost a
        # question, not run free.
        self.assertEqual(ops.action_class("definitely_not_a_verb"), "sensitive")
        self.assertIsNone(ops.operation("definitely_not_a_verb"))

    def test_an_unknown_verb_holds_a_retry(self):
        # ...and an unrecognised *effect* is exactly the case where repeating
        # the act could duplicate it.
        self.assertGreater(ops.holds_on_uncertainty("nope"), 0)

    def test_every_row_says_how_it_proves_itself(self):
        for op in ops.OPERATIONS:
            self.assertTrue(op.verify.strip(), f"{op.verb} does not say how it verifies")

    def test_the_round_trip_is_lossless(self):
        for op in ops.OPERATIONS:
            payload = op.to_dict()
            self.assertEqual(payload["verb"], op.verb)
            self.assertEqual(payload["action_class"], op.action_class)
            self.assertEqual(payload["tiers"], list(op.tiers))


class TierTests(unittest.TestCase):
    """Which verbs may use a bare coordinate."""

    def test_a_coordinate_is_never_the_only_option_for_a_named_verb(self):
        for op in ops.OPERATIONS:
            if op.tiers:
                self.assertIn(ops.ELEMENT_TIER, op.tiers, op.verb)

    def test_press_has_no_coordinate_tier(self):
        # A key goes to whatever has focus, so a coordinate says nothing about
        # where it lands -- offering one would be a second thing to get wrong.
        self.assertEqual(ops.operation("press").tiers, ())
        self.assertFalse(ops.operation("press").allows_tier(ops.COORDINATE_TIER))

    def test_observe_has_no_action_tier(self):
        self.assertEqual(ops.operation("observe").tiers, ())

    def test_every_act_allows_a_coordinate_as_the_fallback(self):
        for verb in ("click", "move", "type"):
            self.assertTrue(ops.operation(verb).allows_tier(ops.COORDINATE_TIER), verb)

    def test_the_surface_is_five_verbs_and_no_more(self):
        # Deliberately a small surface. It was nine, and the four that went
        # (`double_click`, `right_click`, `drag`, `scroll`) were four ways to ask
        # for one of two intentions. Every extra verb is a choice the model can
        # lose on a voice turn's budget of attention, and "she cannot click
        # anything" is what an ambiguous surface sounds like from outside.
        self.assertEqual(
            ops.VERBS, ("observe", "move", "click", "type", "press")
        )

    def test_the_removed_verbs_are_gone_from_the_table(self):
        for verb in ("double_click", "right_click", "drag", "scroll"):
            self.assertIsNone(ops.operation(verb), verb)
            self.assertEqual(ops.action_class(verb), "sensitive", verb)

    def test_only_press_asks_without_an_element(self):
        # The whole gating rule in one assertion: everything runs except `press`,
        # because Enter is a submit in a chat box, a search with autosubmit, and
        # most settings dialogs.
        for verb in ops.VERBS:
            expected = {"observe": "read", "press": "sensitive"}.get(verb, "act")
            self.assertEqual(ops.action_class(verb), expected, verb)


class ElementClassTests(unittest.TestCase):
    """A control's own words decide how much the user is asked."""

    def test_ordinary_controls_run_without_asking(self):
        for name in ("Next", "Cancel", "Settings", "Open file", "Bold", "Back"):
            self.assertEqual(ops.classify_element(name), "act", name)

    def test_a_submit_asks(self):
        for name in ("Send", "Submit", "Place order", "Pay", "Publish", "Confirm"):
            self.assertEqual(ops.classify_element(name), "sensitive", name)

    def test_an_irreversible_control_asks_hardest(self):
        for name in ("Delete", "Delete all", "Erase", "Uninstall", "Empty trash"):
            self.assertEqual(ops.classify_element(name), "destructive", name)

    def test_a_substring_is_not_a_word(self):
        # "Sender" is a column heading and "Deleted items" is a folder. If
        # these matched, every ordinary click in a mail client would ask.
        self.assertEqual(ops.classify_element("Sender"), "act")
        self.assertEqual(ops.classify_element("Deleted items (3)"), "act")
        self.assertEqual(ops.classify_element("Unsent"), "act")

    def test_a_multi_word_entry_is_matched_as_a_phrase(self):
        # "place order" is one entry, so "Order history" -- which contains the
        # word "order" -- must not match it.
        self.assertEqual(ops.classify_element("Place order"), "sensitive")
        self.assertEqual(ops.classify_element("Order history"), "act")
        self.assertEqual(ops.classify_element("Empty trash"), "destructive")
        self.assertEqual(ops.classify_element("Sign out"), "sensitive")

    def test_a_keyword_still_matches_on_its_own(self):
        # The phrase rule is about multi-word entries, not about ignoring a
        # word that is dangerous by itself.
        self.assertEqual(ops.classify_element("Empty"), "sensitive")

    def test_a_disabled_control_never_asks(self):
        # It cannot be pressed, so there is no act to ask about. Checked
        # *before* the name, so a disabled "Delete" is not a question.
        self.assertEqual(ops.classify_element("Delete", enabled=False), "act")
        self.assertEqual(ops.classify_element("Send", enabled=False), "act")

    def test_an_unnamed_control_is_an_ordinary_click(self):
        self.assertEqual(ops.classify_element(""), "act")

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(ops.classify_element("  sEnD   message "), "sensitive")


class _Element:
    """The subset of an element the classing reads."""

    def __init__(self, name="", role="button", enabled=True):
        self.name = name
        self.role = role
        self.enabled = enabled


class CombineTests(unittest.TestCase):
    """Verb class and element class, and the rule for combining them."""

    def test_an_element_only_ever_raises_the_class(self):
        # The load-bearing rule. A control called "Next" cannot make a
        # `sensitive` verb safe, and lowering a class because an element's
        # name looks harmless would make a string off the screen -- which a web
        # page chose -- the thing deciding how much the user is asked.
        self.assertEqual(ops.classify("press", _Element("Next")), "sensitive")

    def test_no_element_leaves_the_verb_alone(self):
        self.assertEqual(ops.classify("click"), "act")
        self.assertEqual(ops.classify("press"), "sensitive")
        self.assertEqual(ops.classify("observe"), "read")

    def test_an_element_can_raise_a_mild_verb(self):
        self.assertEqual(ops.classify("click", _Element("Delete")), "destructive")
        self.assertEqual(ops.classify("click", _Element("Send")), "sensitive")

    def test_a_deleted_control_still_asks(self):
        # The check is on what is being *pressed*, which is where the blast
        # radius is -- never on the verb.
        self.assertEqual(ops.classify("click", _Element("Delete")), "destructive")
        self.assertEqual(ops.classify("click", _Element("Cancel")), "act")

    def test_typing_into_a_submit_field_is_still_a_question(self):
        # `type` runs by default -- the product decision is that Lumine has
        # hands -- but the field's own name can still raise it. That is the
        # difference between "typing is free" and "typing into *that* is not".
        self.assertEqual(ops.classify("type", _Element("Search")), "act")
        self.assertEqual(ops.classify("type", _Element("Send message")), "sensitive")

    def test_an_unknown_verb_is_sensitive_even_with_a_tame_element(self):
        self.assertEqual(ops.classify("teleport", _Element("Next")), "sensitive")


class IntegratedWindowTests(unittest.TestCase):
    """Which windows have a better tool behind them -- and that it is a hint.

    This was a hard refusal, and it was the wrong call: it refused every
    browser and WhatsApp window, so `desktop_control` stopped being a fallback
    and became a wall, and a user asking for something the dedicated tools
    could not phrase for got nowhere at all. The table is still worth keeping
    -- the router uses it to choose -- but nothing here can stop an act.
    """

    def test_a_browser_window_names_web_control(self):
        for name in ("chrome.exe", "msedge.exe", "firefox.exe", "safari"):
            entry = ops.integration_for([name])
            self.assertIsNotNone(entry, name)
            self.assertEqual(entry.tool, "web_control", name)

    def test_whatsapp_names_the_messaging_tool(self):
        entry = ops.integration_for(["WhatsApp.exe"])
        self.assertIsNotNone(entry)
        self.assertEqual(entry.tool, "send_whatsapp")

    def test_a_webview_renderer_is_not_a_browser(self):
        # The bug this table shipped with: a prefix match on "msedge" also
        # matched "msedgewebview2", which is the renderer inside every Tauri
        # and WinUI application -- so Lumine's own window, and every desktop
        # app built on a webview, was reported as a browser.
        for name in ("msedgewebview2.exe", "chrome_elf.exe", "WebKitNetworkProcess"):
            self.assertIsNone(ops.integration_for([name]), name)

    def test_an_ordinary_app_is_untouched(self):
        for name in ("notepad.exe", "explorer.exe", "Code.exe", "slack.exe", "devenv.exe"):
            self.assertIsNone(ops.integration_for([name]), name)

    def test_an_unreadable_process_is_untouched_rather_than_guessed(self):
        # [] means "could not tell", and the caller treats it as unknown.
        self.assertIsNone(ops.integration_for([]))
        self.assertIsNone(ops.integration_for([""]))

    def test_the_note_names_the_tool_and_does_not_claim_a_refusal(self):
        # It appears on an approval card for something that is about to
        # happen, so "I won't" there would be a lie.
        entry = ops.integration_for(["chrome.exe"])
        text = ops.refusal_for(entry)
        self.assertIn("web_control", text)
        self.assertNotIn("Nothing was done", text)

    def test_the_redirect_list_is_deduplicated_and_ordered(self):
        names = ops.integrated_tool_names()
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("web_control", names)
        self.assertIn("send_whatsapp", names)


class ToolRowTests(unittest.TestCase):
    """The Tools-page badge and the gate must read the same table."""

    def test_the_registry_badge_is_computed_from_the_table(self):
        from agent.tools.tools_permission import TOOL_ACTION_CLASS, permission_floor

        self.assertEqual(TOOL_ACTION_CLASS["desktop_control"], ops.tool_action_class())
        self.assertEqual(permission_floor("desktop_control"), "ask")

    def test_the_tool_id_is_the_one_the_registry_registers(self):
        from agent.tools.tools_registry import ALL_TOOLS
        from agent.tools.tools_permission import tool_id

        ids = [tool_id(tool) for tool in ALL_TOOLS]
        self.assertIn(ops.TOOL, ids)
        self.assertEqual(ids.count(ops.TOOL), 1)


if __name__ == "__main__":
    unittest.main()
