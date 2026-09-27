"""The Tools page can only be honest if it is generated from the registry.

`agent/tools/tools_registry.py` is the source of truth for what Lumine can do.
The Tools page renders `tool_catalog.py`'s output. If those two can drift, the page
shows a list of things the agent cannot actually do -- which is worse than an
empty page, because it looks like an answer.

These tests pin the two ends of that chain, plus the environment toggles, since a
card that claims a tool is available while `LUMINE_DISABLED_TOOLS` has switched it
off is a lie with a tick next to it.
"""

import json
import unittest
from unittest import mock

from agent import tool_catalog
from agent.tool_catalog import catalog_issues, main
from agent.tools.tools_registry import (
    ALL_TOOLS,
    TOOL_CARDS,
    TOOL_CATEGORIES,
    tool_cards,
)
from agent.tools.tools_permission import (
    APP_LAUNCH_VAR,
    DISABLED_TOOLS_VAR,
    disabled_reason,
    tool_id,
)


class RegistryCardAgreementTests(unittest.TestCase):
    def test_the_registry_and_the_cards_agree(self):
        # The whole reason TOOL_CARDS exists. A tool added without a card never
        # appears in the app, and its existence has to be read from source.
        self.assertEqual(catalog_issues(), [])

    def test_every_registered_tool_appears_exactly_once(self):
        ids = [card["id"] for card in tool_cards()]
        self.assertEqual(len(ids), len(set(ids)), "a tool is listed twice")
        self.assertEqual(set(ids), {tool_id(tool) for tool in ALL_TOOLS})

    def test_a_tool_added_without_a_card_fails_the_check(self):
        # Proves the guard actually guards, rather than passing because the
        # catalog happens to be complete right now.
        with mock.patch.dict(TOOL_CARDS, {}, clear=True):
            issues = catalog_issues()
            self.assertTrue(issues)
            self.assertTrue(any("has no card" in issue for issue in issues))
            # Asserted inside the patch: the exit code is the part a script or a
            # test runner acts on, and it has to be wrong for the same reason the
            # issue list is.
            self.assertEqual(main(["--check"]), 1)

    def test_a_card_for_a_tool_that_is_gone_fails_the_check(self):
        # The other direction: a card for something no longer callable is a page
        # offering a capability that cannot be used.
        with mock.patch.dict(TOOL_CARDS, {"no_such_tool": {"label": "Ghost", "effect": "answers"}}):
            issues = catalog_issues()
        self.assertTrue(any("not registered" in issue for issue in issues))

    def test_every_card_says_whether_it_acts_on_the_machine(self):
        # The distinction a person is being asked to make by leaving a tool
        # switched on. Inferring it from the tool's name is not good enough.
        for card in tool_cards():
            with self.subTest(tool=card["id"]):
                self.assertIn(card["effect"], ("answers", "opens"))

    def test_the_desktop_tool_is_the_one_marked_as_opening_something(self):
        # `open_app` is the only tool that does anything to the machine. If a
        # second one is added, the card has to say so -- this test is the nudge.
        acting = {card["id"] for card in tool_cards() if card["effect"] == "opens"}
        self.assertEqual(acting, {"open_app"})

    def test_every_card_has_something_to_read(self):
        for card in tool_cards():
            with self.subTest(tool=card["id"]):
                self.assertTrue(card["label"].strip())
                self.assertTrue(card["summary"].strip(), "a card with no summary is a blank tile")
                self.assertTrue(card["category"].strip())

    def test_every_card_is_in_a_declared_category(self):
        # The Tools page builds its tab strip from the published category list. A
        # card in a category that is not declared there has no tab to live under,
        # which means it is a tool the app can call and the app cannot show. That
        # is the same failure as a tool with no card at all, one step later.
        published = {name for name, _ in TOOL_CATEGORIES}
        for card in tool_cards():
            with self.subTest(tool=card["id"]):
                self.assertIn(card["category"], published)

    def test_a_card_in_an_undeclared_category_fails_the_check(self):
        # Proves the guard, rather than trusting that the table happens to line
        # up. The Tools page would not error on this -- it would silently hide the
        # tool behind a tab that was never drawn.
        with mock.patch.dict(
            TOOL_CARDS,
            {"get_weather": {**TOOL_CARDS["get_weather"], "category": "Vibes"}},
        ):
            issues = catalog_issues()
        self.assertTrue(any("Vibes" in issue for issue in issues))

    def test_every_category_says_what_it_contains(self):
        # A tab is a single word, and a single word is a guess. "Desktop" could
        # mean the app, the OS, or a preference pane; the description is what
        # settles it, and the page shows it for the open tab.
        for name, description in TOOL_CATEGORIES:
            with self.subTest(category=name):
                self.assertTrue(name.strip())
                self.assertTrue(description.strip())

    def test_a_declared_category_with_nothing_in_it_is_not_an_error(self):
        # Declaring a group before the first tool lands in it is legitimate -- the
        # alternative is editing two files in a specific order to add a tool. It
        # simply does not earn a tab, and the frontend drops empty groups for
        # that reason.
        with mock.patch(
            "agent.tools.tools_registry.TOOL_CATEGORIES",
            (*TOOL_CATEGORIES, ("Unpopulated", "Nothing is here yet.")),
        ):
            self.assertEqual(catalog_issues(), [])

    def test_every_parameter_explains_itself(self):
        # A parameter with no help is a name the reader has to guess the type of.
        for card in tool_cards():
            for parameter in card["parameters"]:
                with self.subTest(tool=card["id"], parameter=parameter["name"]):
                    self.assertTrue(parameter["help"].strip())


class EnabledStateTests(unittest.TestCase):
    def test_every_tool_is_enabled_by_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            for card in tool_cards():
                with self.subTest(tool=card["id"]):
                    self.assertTrue(card["enabled"], card["disabledReason"])
                    self.assertEqual(card["disabledReason"], "")

    def test_a_listed_tool_reports_the_variable_that_turned_it_off(self):
        # "Not available" and "you turned this off" are different things, and the
        # second is the common one: a deliberate choice somebody then forgets.
        with mock.patch.dict("os.environ", {DISABLED_TOOLS_VAR: "get_weather"}, clear=True):
            card = next(c for c in tool_cards() if c["id"] == "get_weather")
            self.assertFalse(card["enabled"])
            self.assertIn(DISABLED_TOOLS_VAR, card["disabledReason"])
            # And the others are untouched: disabling one is not disabling all.
            self.assertTrue(next(c for c in tool_cards() if c["id"] == "get_news")["enabled"])

    def test_the_desktop_tool_follows_its_own_toggle(self):
        with mock.patch.dict("os.environ", {APP_LAUNCH_VAR: "false"}, clear=True):
            card = next(c for c in tool_cards() if c["id"] == "open_app")
            self.assertFalse(card["enabled"])
            self.assertIn(APP_LAUNCH_VAR, card["disabledReason"])
            # Answering a question is not a desktop action, so it stays on.
            self.assertTrue(next(c for c in tool_cards() if c["id"] == "search_web")["enabled"])

    def test_the_reason_agrees_with_the_filter_the_worker_actually_applies(self):
        # The card and the worker's tool list are computed from the same toggles.
        # If these ever disagree, the page describes a session that will not happen.
        from agent.tools.tools_registry import get_tools

        for env in (
            {},
            {DISABLED_TOOLS_VAR: "get_news, open_app"},
            {APP_LAUNCH_VAR: "false"},
        ):
            with self.subTest(env=env):
                with mock.patch.dict("os.environ", env, clear=True):
                    live = {tool_id(tool) for tool in get_tools()}
                    shown = {card["id"] for card in tool_cards() if card["enabled"]}
                    self.assertEqual(live, shown)

    def test_disabled_reason_is_empty_for_an_available_tool(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            for tool in ALL_TOOLS:
                self.assertEqual(disabled_reason(tool), "", tool_id(tool))


class CatalogOutputTests(unittest.TestCase):
    def test_check_mode_passes(self):
        self.assertEqual(main(["--check"]), 0)

    def test_the_payload_is_json_and_carries_every_tool(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main([]), 0)
        payload = json.loads(buffer.getvalue())
        # Version 2 added `categories`. The Tools page builds its tab strip from
        # that list rather than from the `category` strings on the cards, because a
        # group with no strip is a group whose tools cannot be seen -- so the
        # shape of this payload is the shape of the page's navigation, and the
        # test asserts it rather than just checking the tools still arrive.
        self.assertEqual(payload["version"], 2)
        self.assertEqual(len(payload["tools"]), len(ALL_TOOLS))
        self.assertEqual(
            [entry["id"] for entry in payload["categories"]],
            [name for name, _ in TOOL_CATEGORIES],
            "the published order is what the tab strip is drawn in",
        )
        # And the strip is not decorative: every card lands in a group that exists.
        published = {entry["id"] for entry in payload["categories"]}
        for tool in payload["tools"]:
            self.assertIn(tool["category"], published)

    def test_the_payload_leaks_nothing_but_documentation(self):
        # The payload crosses into the webview, so it must describe tools without
        # carrying anything a provider key would look like.
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            main([])
        blob = json.loads(buffer.getvalue())
        lowered = json.dumps(blob).lower()
        for marker in ("api_key", "apikey", "secret", "bearer", "sk-", "password", "token"):
            self.assertNotIn(marker, lowered, f"tool catalog leaked {marker!r}")
