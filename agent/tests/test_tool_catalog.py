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
from agent.tools.messaging import whatsapp
from agent.tools.tools_registry import (
    ALL_TOOLS,
    TOOL_CARDS,
    TOOL_CATEGORIES,
    tool_cards,
)
from agent.tools.tools_permission import (
    APP_LAUNCH_VAR,
    DESKTOP_INPUT_VAR,
    DISABLED_TOOLS_VAR,
    disabled_reason,
    tool_id,
)


class CardSummaryBudgetTests(unittest.TestCase):
    """A card summary is a *label*, not a paragraph.

    The grid is read by scanning: you are not reading seventeen cards, you are
    looking for the one tool that does something you did not expect. A summary
    that runs to nine lines stops being comparable to its neighbours, which is
    the only thing a grid of cards is for -- and it makes the grid ragged, so
    the cards that follow it on the same row start lower.

    The failure this guards against is not hypothetical. Seven of seventeen
    summaries were over budget, the longest at 71 words, and two of those were
    describing verbs that no longer existed -- because the summary is written
    separately from the docstring, so removing a verb does not touch it and
    nothing else notices.
    """

    #: 22 words. Two short clauses at 13px in a 280px card is two lines; past
    #: that it is three, and the third line is the one nobody reads.
    BUDGET = 22

    def test_no_summary_is_longer_than_the_budget(self):
        for card in tool_cards():
            words = len(card["summary"].split())
            self.assertLessEqual(
                words,
                self.BUDGET,
                f"{card['id']}: {words} words -- {card['summary']!r}",
            )

    def test_every_summary_is_a_sentence_and_ends_in_a_full_stop(self):
        # A fragment on a card reads as a truncated thought rather than a
        # deliberate brevity, which is the wrong reason to be short.
        for card in tool_cards():
            self.assertTrue(
                card["summary"].rstrip().endswith("."),
                f"{card['id']}: {card['summary']!r}",
            )

    def test_a_summary_does_not_name_a_verb_the_tool_no_longer_has(self):
        # The bug that motivated the budget. `desktop_control` lost four verbs
        # and its card kept saying "drags, scrolls" -- on the screen, to the
        # person deciding whether to switch it on.
        from agent.tools.desktop import ops

        card = next(c for c in tool_cards() if c["id"] == "desktop_control")
        listed = next(
            p for p in card["parameters"] if p["name"] == "action"
        )["help"].lower()
        # Word-boundary, not substring: "scroll" is a substring of "observe" is
        # not, but "scroll" *is* a substring of nothing here and "drag" is not
        # either -- yet a plain `in` check is exactly the test that let a stale
        # list through in the first place, by being too weak to notice.
        for verb in ops.VERBS:
            self.assertRegex(listed, rf"\b{verb}\b", f"{verb} is a verb and is not offered")
        for verb in ("double_click", "right_click", "drag", "scroll"):
            self.assertNotRegex(listed, rf"\b{verb}\b", f"{verb} is offered but does not exist")
        for phrase in ("drag", "scroll"):
            self.assertNotIn(
                phrase, card["summary"].lower(), f"the summary still says {phrase!r}"
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
                self.assertIn(card["effect"], ("answers", "opens", "acts", "sends"))

    def test_the_desktop_tool_is_the_one_marked_as_opening_something(self):
        # `open_app` launches, `manage_window` arranges, and `play_media` hands a
        # file to the player's operating system -- the three tools that do
        # something to the machine. A fourth must be added here by hand: that is
        # the point of the assertion, that no tool can quietly start acting
        # without the card saying so.
        acting = {card["id"] for card in tool_cards() if card["effect"] == "opens"}
        self.assertEqual(acting, {"open_app", "manage_window", "play_media"})

    def test_the_web_tools_that_act_are_marked_as_acting(self):
        # The sibling assertion for `acts`: browsing reaches out and clicks,
        # and a reader must not have to know that from the tool's name. A new
        # web-acting tool has to be added here by hand, like the desktop pair
        # above.
        #
        # `run_task` is here rather than with the desktop pair because it is
        # not only a desktop thing: it may spend its whole budget in a lookup,
        # and the category a card lands in is a presentation decision. What the
        # effect says -- that it changes something -- is not.
        #
        # The two WhatsApp *management* tools are here rather than answering
        # because they change the account: archiving a chat, blocking a contact.
        # `whatsapp_read` is deliberately absent -- it only reads, so `answers` is
        # the honest word for it, and lumping it in with the tools that change
        # something would be the exact mistake this assertion exists to catch.
        on_the_web = {card["id"] for card in tool_cards() if card["effect"] == "acts"}
        self.assertEqual(
            on_the_web,
            {
                "browse",
                "web_control",
                "run_task",
                "cancel_task",
                "whatsapp_chats",
                "whatsapp_account",
                # The mouse and keyboard: it changes the machine, which is all
                # `acts` claims. Named here for the same reason as the desktop
                # pair above -- a reader must not have to infer it from the id.
                "desktop_control",
            },
        )

    def test_the_messaging_tool_is_marked_as_sending(self):
        # And `sends`: leaves the conversation and cannot be taken back. One
        # card, one name -- a second sender must be added here by hand.
        sends = {card["id"] for card in tool_cards() if card["effect"] == "sends"}
        self.assertEqual(sends, {"send_whatsapp"})

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


#: Tools that ship *off*, mapped to the switch that turns them on. Named rather
#: than loosened, so the "everything else is on by default" assertion below stays
#: true for any tool added later -- a new tool defaulting to off has to be listed
#: here, which is the moment somebody has to say out loud that they meant it.
#:
#: A map rather than a set because each entry has to name its own switch, and the
#: assertion below checks that the card's reason does: "you have not switched this
#: on" is only actionable if it says *where*. Two tools with two different
#: switches is what forced this, and it is the shape that will stay right.
#:
#: `desktop_control` is not a close call. It can click any pixel and press any
#: named key, in any application, which is the widest capability in the product
#: -- and it can also click Lumine's own window, so shipping it on would mean
#: shipping a tool that can remove its own switch.
#:
#: `play_media` **was** here and was removed. It shipped off on the reasoning that
#: a song keeps playing after the call ends, which is true and is not a boundary
#: problem: the capability can only open one file inside one allowlisted folder the
#: user chose, and it has no verb that moves, renames or deletes anything. Making
#: a person find a second switch before their assistant will play a song was the
#: wrong trade, and a fresh install gets no folder at all -- so the honest default
#: state is "on, and there is nothing to reach yet".
DEFAULT_OFF_TOOLS = {"desktop_control": "desktop.input"}


class EnabledStateTests(unittest.TestCase):
    def test_every_tool_is_enabled_by_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            for card in tool_cards():
                with self.subTest(tool=card["id"]):
                    if card["id"] in DEFAULT_OFF_TOOLS:
                        continue
                    self.assertTrue(card["enabled"], card["disabledReason"])
                    self.assertEqual(card["disabledReason"], "")

    def test_a_default_off_tool_says_how_to_turn_it_on(self):
        # "Unavailable" and "you have not switched this on" are different
        # things to be told, and a card that only had a boolean would have to
        # pick one -- the wrong one, since a deliberate choice is exactly what a
        # user is most likely to have forgotten making. And the reason has to name
        # *that tool's* switch: a card pointing at the wrong one sends the user to
        # a setting that does not control it.
        with mock.patch.dict("os.environ", {}, clear=True):
            for card in tool_cards():
                switch = DEFAULT_OFF_TOOLS.get(card["id"])
                if switch is None:
                    continue
                with self.subTest(tool=card["id"]):
                    self.assertFalse(card["enabled"])
                    self.assertIn(switch, card["disabledReason"])

    def test_a_default_off_tool_is_hidden_from_the_worker_too(self):
        # The card and the worker's tool list are computed from the same
        # toggles. If the card said "off" while the tool was still handed to
        # the model, the Tools page would be describing a switch that does
        # nothing.
        from agent.tools.tools_registry import get_tools

        with mock.patch.dict("os.environ", {}, clear=True):
            live = {tool_id(tool) for tool in get_tools()}
            shown = {card["id"] for card in tool_cards() if card["enabled"]}
        self.assertEqual(live, shown)
        for tool in DEFAULT_OFF_TOOLS:
            self.assertNotIn(tool, live, f"{tool} is off on the page but live in the session")

    def test_a_default_off_tool_switches_on_when_asked(self):
        with mock.patch.dict("os.environ", {DESKTOP_INPUT_VAR: "true"}, clear=True):
            card = next(c for c in tool_cards() if c["id"] == "desktop_control")
            self.assertTrue(card["enabled"], card["disabledReason"])

    def test_the_tools_page_card_is_the_switch_that_turns_it_on(self):
        # The product decision was *one* switch, in the place every other
        # capability has one. There were briefly two -- the card and
        # `desktop.input` -- and the card did nothing while the second read
        # "off", so a user flipping a switch in the app watched nothing happen
        # and concluded the feature was broken. The card alone must now work.
        document = {"tools": {"desktop_control": "on"}}
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
            "agent.tools.tools_permission.effective_document",
            return_value=(document, "test", []),
        ):
            card = next(c for c in tool_cards() if c["id"] == "desktop_control")
            self.assertTrue(card["enabled"], card["disabledReason"])


class DesktopInputExclusionTests(unittest.TestCase):
    """Turning the desktop control on silences the browser tools.

    Two tools that both click a web page is the problem `browse` and
    `web_control` were already split to solve -- *by difficulty*. With a third
    that can drive a browser by coordinate, the model has three ways to click
    the same button and no principled way to choose, which is how a request ends
    up in the weakest of the three.
    """

    def _reason(self, tool_id_name: str, document: dict, env=None):
        from agent.tools import tools_permission as tp

        with mock.patch.dict("os.environ", env or {}, clear=True), mock.patch(
            "agent.tools.tools_permission.effective_document",
            return_value=(document, "test", []),
        ):
            return tp.disabled_reason(tp._IdentityTool(tool_id_name))

    def test_the_browser_tools_are_off_while_desktop_control_is_on(self):
        document = {"tools": {"desktop_control": "on"}}
        for name in ("browse", "web_control"):
            reason = self._reason(name, document)
            self.assertNotEqual(reason, "", name)
            self.assertIn("desktop", reason.lower(), name)

    def test_the_reason_names_the_switch_responsible(self):
        # "Not available" and "you turned this on" are different things, and the
        # second is the common one. A card that only had a boolean would have to
        # pick one, and the wrong one looks like a broken tool.
        reason = self._reason("browse", {"tools": {"desktop_control": "on"}})
        self.assertIn("desktop.input", reason)

    def test_they_come_straight_back_when_it_goes_off(self):
        # One-directional on purpose: nothing is disabled permanently, and a
        # machine that does not want the desktop control keeps both browser
        # tools with no other ceremony.
        for document in ({}, {"tools": {"desktop_control": "off"}}):
            self.assertEqual(self._reason("browse", document), "", document)

    def test_unrelated_tools_are_untouched(self):
        document = {"tools": {"desktop_control": "on"}}
        for name in ("get_weather", "open_app", "manage_window", "send_whatsapp"):
            self.assertEqual(self._reason(name, document), "", name)

    def test_it_does_not_fire_when_the_desktop_tool_is_itself_off(self):
        # The one state where this exclusion was actively harmful.
        #
        # `desktop.input` and the card are separate keys, so a document can carry
        # both: the switch says "on" and the card says "off". Reading the switch
        # alone silenced two browser tools on the authority of a capability
        # nobody has -- the user switched it on, turned it off again, and lost
        # the tools they had. This was found by enumerating the precedence rather
        # than by testing the two ends, because each end alone behaves.
        document = {"tools": {"desktop_control": "off"}, "desktop": {"input": "on"}}
        self.assertNotEqual(self._reason("desktop_control", document), "")
        for name in ("browse", "web_control"):
            self.assertEqual(self._reason(name, document), "", name)

    def test_the_switch_alone_is_enough_when_the_card_agrees(self):
        # The counterpart: with no card key at all, `desktop.input = on` still
        # drives everything, so the fix above is not "the card is required".
        document = {"desktop": {"input": "on"}}
        self.assertEqual(self._reason("desktop_control", document), "")
        for name in ("browse", "web_control"):
            self.assertNotEqual(self._reason(name, document), "", name)

    def test_the_exclusion_is_the_same_set_the_tools_registry_sees(self):
        from agent.tools.tools_permission import SUPPRESSED_BY_DESKTOP_INPUT, tool_id
        from agent.tools.tools_registry import ALL_TOOLS

        registered = {tool_id(tool) for tool in ALL_TOOLS}
        for name in SUPPRESSED_BY_DESKTOP_INPUT:
            self.assertIn(name, registered, name)


class DesktopInputPrecedenceTests(unittest.TestCase):
    """`desktop.input` vs the Tools page card vs the environment.

    Four sources can answer, and the order is the whole question. It lives in
    `tools_permission._desktop_input_mode` rather than in the settings screen,
    so it is one function with one precedence rather than three writers
    reasoning about which of them wins.
    """

    def _mode(self, document=None, env=None) -> str:
        from agent.tools import tools_permission as tp

        with mock.patch.dict("os.environ", env or {}, clear=True), mock.patch(
            "agent.tools.tools_permission.effective_document",
            return_value=(document or {}, "test", []),
        ):
            return tp._desktop_input_mode()

    def test_a_fresh_install_is_off(self):
        # The default must be the state you get by doing nothing.
        self.assertEqual(self._mode(), "off")

    def test_the_card_turns_it_on(self):
        self.assertEqual(self._mode({"tools": {"desktop_control": "on"}}), "on")

    def test_the_card_turned_off_stays_off(self):
        self.assertEqual(self._mode({"tools": {"desktop_control": "off"}}), "off")

    def test_an_untouched_card_does_not_enable_it(self):
        # Another tool's switch is not this one's consent.
        self.assertEqual(self._mode({"tools": {"browse": "on"}}), "off")

    def test_the_document_key_wins_over_the_card(self):
        # A hand-written document pins it off regardless of the card, so a
        # managed machine can forbid this without the screen being able to
        # argue.
        document = {"desktop": {"input": "off"}, "tools": {"desktop_control": "on"}}
        self.assertEqual(self._mode(document), "off")
        document = {"desktop": {"input": "on"}, "tools": {"desktop_control": "off"}}
        self.assertEqual(self._mode(document), "on")

    def test_the_environment_is_underneath_both(self):
        self.assertEqual(self._mode(None, {DESKTOP_INPUT_VAR: "true"}), "on")
        # ...and the card still beats it, because a switch in the app is a
        # later decision than a line in a file.
        document = {"tools": {"desktop_control": "off"}}
        self.assertEqual(self._mode(document, {DESKTOP_INPUT_VAR: "true"}), "off")

    def test_a_typo_fails_closed_rather_than_enabling_the_mouse(self):
        # The safe direction for a key that *grants* authority is off, which is
        # the opposite of `tools.<id>` on purpose.
        self.assertEqual(self._mode({"desktop": {"input": "enabled"}}), "off")
        self.assertEqual(self._mode({"desktop": {"input": "yes"}}), "off")
        self.assertEqual(self._mode({"tools": {"desktop_control": "enabled"}}), "off")

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
        # Isolated from the machine's saved document: `send_whatsapp` reads
        # it first (document over environment), so *its* default here is
        # "no document" -- which is also what a fresh install has.
        with mock.patch.object(
            whatsapp, "effective_document", return_value=({}, "test", [])
        ), mock.patch.dict("os.environ", {}, clear=True):
            for tool in ALL_TOOLS:
                if tool_id(tool) in DEFAULT_OFF_TOOLS:
                    continue
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
