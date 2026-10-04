"""The message-bubble geometry, pinned against what the live DOM actually is.

These exist because the class-name selectors this module was built on turned out
to match **nothing** on the current WhatsApp Web build, and every symptom was
silent. A read returned zero messages. A delivery check could never confirm a
send. Both looked like a tool that worked and a network that did not.

The facts below were measured, not inferred -- see
``agent/whatsapp_message_probe.py`` and the notes on ``_JS_ROWS_FN``:

* class names are hashed: ``x1liijdw xu342n7 xelbjmh x16pgt24``
* ``[role="row"]`` is a full-width container, 920px inside a 932px pane
* ``[data-testid="msg-container"]`` is the bubble: 200px, inside the row
* the conversation pane starts at x=491 of a 1422px window

That last one is why direction is compared against the *pane* centre. A viewport
midpoint puts every message in the conversation in the left column, and a
correctly-sent message is then indistinguishable from an incoming one.
"""

from __future__ import annotations

import re
import unittest


#: The scripts that address a message, named. Not discovered by scanning: a scan
#: picks up ``_JS_ROWS_FN`` itself, and the failure then reads as "a script is not
#: wrapped", which points at the wrapper instead of at the real omission -- that a
#: script stopped addressing messages.
_MESSAGE_SCRIPTS = (
    "_JS_READ_MESSAGES",
    "_JS_MESSAGE_AT",
    "_JS_DELIVERED",
    "_JS_REACTED",
    "_JS_DELETED",
    "_JS_MESSAGE_FLAG",
    "_JS_OPEN_MESSAGE_MENU",
)


def _scripts_using_rows() -> list[str]:
    from agent.tools.messaging import whatsapp_web as web

    return [getattr(web, name) for name in _MESSAGE_SCRIPTS]


class ClassSelectorTests(unittest.TestCase):
    """The old selectors must not come back, in any spelling.

    They are not merely *stale*: the rows they name carry hashed class names, so
    ``div.message-in`` and ``div.message-out`` cannot match on this build however
    many spellings are added to the list.
    """

    def test_no_live_script_selects_a_message_by_class_name(self):
        from agent.tools.messaging import whatsapp_web as web

        for name in dir(web):
            script = getattr(web, name)
            if not isinstance(script, str) or not script.startswith("("):
                continue
            for dead in ("div.message-in", "div.message-out", ".message-out", ".message-in"):
                self.assertNotIn(
                    dead,
                    script,
                    f"{name} selects messages by a class name that does not exist "
                    "on the current build",
                )

    def test_the_legacy_spellings_are_kept_only_in_the_probe_target_list(self):
        """They stay in ``TARGETS`` on purpose -- to be reported, not used.

        A future build may bring the classes back. Keeping the entries means the
        probe says so instead of the target quietly disappearing from the list.
        """
        from agent.tools.messaging import whatsapp_web as web

        self.assertTrue(web.TARGETS["message_out"])
        self.assertIn("div.message-out", web.TARGETS["message_out"])


class BubbleGeometryTests(unittest.TestCase):
    """How a message is found, asserted on the text of the one shared helper."""

    def setUp(self):
        from agent.tools.messaging import whatsapp_web as web

        self.fn = web._JS_ROWS_FN

    def test_the_bubble_is_found_by_its_test_id(self):
        self.assertIn('[data-testid="msg-container"]', self.fn)

    def test_direction_is_measured_against_the_pane_not_the_window(self):
        self.assertIn("pane.left + pane.width / 2", self.fn)
        # A viewport midpoint is the bug this replaced: the pane does not start at
        # x=0, so half the window is inside the chat *list* and every message in
        # the conversation lands left of the line.
        self.assertNotIn("window.innerWidth", self.fn)

    def test_a_row_without_a_bubble_is_not_a_message(self):
        """The encryption banner is the case that made this a hard rule.

        It has no ``msg-container``. An earlier structural fallback -- "the
        narrowest descendant carrying the row's text" -- matched some element of
        it, and that element sat right of centre, so the banner was read as a
        message of yours and ``delivery_seen`` confirmed a delivery against it.
        """
        self.assertIn("if (!bubble) continue;", self.fn)
        self.assertNotIn("bestWidth", self.fn, "the structural fallback must be gone")

    def test_an_unsent_draft_cannot_be_confirmed_as_a_delivery(self):
        # The composer holds the text verbatim, so a match that ignores it
        # confirms a send that never happened.
        self.assertIn("floor", self.fn)
        self.assertIn("the composer", self.fn)

    def test_a_near_full_width_bubble_is_refused(self):
        # Measured: real bubbles are ~200px in a 932px pane. A 920px element is a
        # divider or a call log, not a message.
        self.assertIn("pane.width * 0.85", self.fn)


class WrappingTests(unittest.TestCase):
    """Every script is one evaluable expression.

    Playwright evaluates a string argument as a single expression, so a helper
    cannot simply be concatenated in front of an arrow function: the page parses
    ``function f(m){...}`` followed by ``(x) => {...}`` as ``f(m){...}(x) => {...}``
    and answers ``SyntaxError: Malformed arrow function parameter list``. That is
    not hypothetical -- it is what happened, and it read as a broken tool.
    """

    def test_the_helper_is_declared_inside_the_body_not_prepended(self):
        from agent.tools.messaging import whatsapp_web as web

        self.assertTrue(web._JS_READ_MESSAGES.startswith("(limits) => {"))
        self.assertNotIn(web._JS_ROWS_FN, web._JS_READ_MESSAGES[:1])

    def test_every_script_starts_with_its_own_arrow_function(self):
        from agent.tools.messaging import whatsapp_web as web

        for script in _scripts_using_rows():
            self.assertRegex(script.strip(), r"^\(\w+\)\s*=>\s*\{")

    def test_the_helper_appears_exactly_once_per_script(self):
        for script in _scripts_using_rows():
            self.assertEqual(
                script.count("function lumineRows"),
                1,
                "the geometry must be defined once per script, not copied",
            )

    def test_every_script_that_reads_messages_shares_the_one_definition(self):
        """One answer to "what is a message", not one per script.

        The alternative is seven copies of the piece of knowledge that must not
        drift -- and when it drifts, the failure is silent: a delivered message
        simply stops being seen.
        """
        from agent.tools.messaging import whatsapp_web as web

        for name in _MESSAGE_SCRIPTS:
            self.assertIn(
                "function lumineRows", getattr(web, name), f"{name} lost the helper"
            )

    def test_the_named_list_is_the_whole_set(self):
        """If a new script addresses a message, it must be named here.

        Without this the list can fall behind the code silently, and the test
        above keeps passing while a script goes back to its own geometry.
        """
        from agent.tools.messaging import whatsapp_web as web

        found = {
            name
            for name in dir(web)
            if re.fullmatch(r"_JS_[A-Z_]+", name)
            and isinstance(getattr(web, name), str)
            and "lumineRows(main)" in getattr(web, name)
            and name != "_JS_ROWS_FN"
        }
        self.assertEqual(
            found,
            set(_MESSAGE_SCRIPTS),
            "a script addresses messages without being listed, so its geometry "
            "is no longer covered",
        )


class DeliveryTests(unittest.TestCase):
    """What a delivery check is allowed to conclude."""

    def test_the_send_path_checks_the_same_bubble_hook(self):
        from agent.tools.messaging import whatsapp_web as web

        self.assertIn('[data-testid="msg-container"]', web._JS_DELIVERED)

    def test_delivery_requires_the_mine_side_of_the_pane(self):
        from agent.tools.messaging import whatsapp_web as web

        self.assertIn("item.mine", web._JS_DELIVERED)
        self.assertIn("if (!item.mine) continue;", web._JS_DELIVERED)

    def test_delivery_compares_the_whole_message(self):
        """A substring test accepts the wrong message.

        A bubble carries its clock too, so "yes" would match "yes, delete that
        photo" -- and the send would be reported as confirmed on the strength of
        an older message.
        """
        from agent.tools.messaging import whatsapp_web as web

        self.assertIn("item.body === wanted", web._JS_DELIVERED)
        self.assertNotIn("indexOf(wanted)", web._JS_DELIVERED)

    def test_a_clock_is_stripped_before_comparing(self):
        from agent.tools.messaging import whatsapp_web as web

        self.assertIn(r"\d{1,2}:\d{2}", web._JS_ROWS_FN)


class UntrustedClickTests(unittest.TestCase):
    """A click inside the page is not a click.

    The probe showed ``Chat: <name> (opened: False)`` -- the click reported
    success and no conversation ever opened. ``element.click()`` dispatches an
    untrusted event: the page's own listeners run and none of the browser's
    defaults happen, which for a framework-driven list is nothing at all.

    Same rule ``browser/actions.py`` applies to keystrokes.
    """

    def test_the_chat_row_is_marked_and_clicked_by_playwright(self):
        """One row marker, in one module, and the send path delegates to it.

        There were two -- ``whatsapp._JS_MARK_CHAT_ROW`` beside
        ``whatsapp_web._JS_MARK_ROW`` -- and only the Web copy had the search-box
        fallback. So the send tool could find a contact through WhatsApp's search
        and then fail to open it, which is exactly what the worker log showed:
        ``find`` returned ``chat: "Me2"``, and 400 ms later ``open_chat("Me2")``
        refused with "I couldn't read your chat list -- nothing matched".
        """
        from agent.tools.messaging import whatsapp as wa
        from agent.tools.messaging import whatsapp_web as web

        self.assertNotIn(".click(", web._JS_MARK_ROW)
        self.assertIn("data-lumine-wa-row", web._JS_MARK_ROW)
        self.assertEqual(wa._ROW_SELECTOR, "[data-lumine-wa-row='1']")
        self.assertFalse(hasattr(wa, "_JS_MARK_CHAT_ROW"))

    def test_the_message_menu_is_marked_and_clicked_by_playwright(self):
        from agent.tools.messaging import whatsapp_web as web

        self.assertNotIn(".click(", web._JS_OPEN_MESSAGE_MENU)
        self.assertIn(web._MENU_MARK, web._JS_OPEN_MESSAGE_MENU)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()