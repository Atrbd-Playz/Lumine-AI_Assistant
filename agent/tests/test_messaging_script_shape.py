"""Every script WhatsApp is driven with must *parse*, and Python cannot see that.

The messaging package has the *worse* history of this fault, not the better one, and
it is written up at length in `agent/AGENTS.md`. Two of the fixes there were found by
reading a live account rather than by a test going red:

* A helper concatenated in front of an arrow function. Playwright evaluates a string
  argument as a **single expression**, so ``function f(){...}`` followed by
  ``(x) => {...}`` parses as ``f(){...}(x) => {...}`` and the page answers
  ``SyntaxError: Malformed arrow function parameter list``. The cost was
  ``open_chat`` returning ``False`` for chats that opened perfectly well by hand.
* A wrapped script whose body never closed the arrow function its wrapper opened.

Both were *total*: the script did nothing at all, and the tool reported a failure for
work that had succeeded. ``test_whatsapp_web.ScriptShapeTests`` guards the second
with a brace balance, and a balance is the right instrument for it -- but it passes
``const budget`` declared twice, and it passes anything that is wrong somewhere a
brace does not go.

So this compiles them with a real parser instead, which is the same guard the browser
package now has. The two share `js_script_shape` because they are in the same
business, and because a guard written for one package and hand-copied to the other is
two guards that drift.

What is particular to this package is the shape of its scripts. Almost none of them
are standalone: `_JS_ROWS_FN`, `_JS_CHAT_ROW_FN` and `_JS_CONTACT_NAME_FN` are
*fragments* -- the one definition of a message, of a chat row and of a contact name
-- spliced into the scripts that use them. Parsing a fragment alone would test
something the page never sees and would point at the wrong fix.
"""

from __future__ import annotations

import unittest

import agent.tools.messaging as messaging_package

from agent.tests import js_script_shape


class MessagingScriptShapeTests(js_script_shape.ScriptShapeSuite):
    """Parse every script `agent.tools.messaging` hands to ``page.evaluate``."""

    PACKAGE = messaging_package

    CRITICAL_SCRIPTS = (
        # The chat list, the search result list, and the two rows a send resolves
        # a recipient through.
        "whatsapp_web._JS_READ_CHATS",
        # Message history, and the reader `whatsapp_read` asks for consent to use.
        "whatsapp_web._JS_READ_MESSAGES",
        # The header that decides whether the *right* conversation is open, and
        # whose mismatch refuses a send on purpose.
        "whatsapp_web._JS_ACTIVE_CHAT",
        # The marker that redirects every later press; a stale one sent a message
        # into the wrong chat.
        "whatsapp_web._JS_MARK_ROW",
    )

    # The single definition of "a message", shared by seven scripts -- the reader,
    # the delivery check, the deleted/flagged/reacted checks, the message-menu
    # opener and the single-message lookup. It trusts the test id alone on purpose:
    # a structural fallback once matched the encryption banner and an empty chat
    # reported one delivered message.
    KNOWN_FRAGMENT = "whatsapp_web._JS_ROWS_FN"
    KNOWN_HOLDERS = (
        "whatsapp_web._JS_READ_MESSAGES",
        "whatsapp_web._JS_OPEN_MESSAGE_MENU",
        "whatsapp_web._JS_MESSAGE_AT",
        "whatsapp_web._JS_MESSAGE_FLAG",
        "whatsapp_web._JS_DELIVERED",
        "whatsapp_web._JS_DELETED",
        "whatsapp_web._JS_REACTED",
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
