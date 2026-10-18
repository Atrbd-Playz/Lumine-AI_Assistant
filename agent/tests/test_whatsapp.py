"""WhatsApp: what it prepares, what it sends, and what it refuses to repeat.

Six claims, each with the test that would fail if it were untrue:

1. **Mode resolution is saved document > environment > default, and an
   unrecognised value is `off`** -- never downgraded to a mode that sends.
2. **A number is E.164 digits or it is not.** The deep link is built from
   already-validated digits and percent-encoded text, so neither message
   content nor a crafted "number" can change what the OS opens.
3. **Nobody sends without an approval**, the class is read at the call site
   (sensitive for the draft, destructive for the real send), and a
   *declined* approval means the send functions are never touched.
4. **The ledger answers two different questions.** "Did I just do that?" --
   a sent key, held for the dedup window. "Do I know what happened?" -- an
   uncertain key, held for minutes. Both are checked before the gate as
   well as before the send, because re-asking is not the fix for a
   duplicate.
5. **A name that matches several chats resolves to candidates and nothing
   else happens**: no approval, no typing, no send -- the user picks and
   the second call resolves.
6. **The modes are written down twice** (schema and runtime) and the two
   lists are pinned together, the same way the permission levels are.

Everything is offline: the WhatsApp Web steps are patched at the module
seam, the deep-link opener is patched, and no test here needs WhatsApp, a
network, or a browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent.runtime.permission_gate import MSG_NO_GATE
from agent.settings.validation import CONFIG_VERSION, WHATSAPP_MODES, validate_document
from agent.tools.messaging import whatsapp
from agent.tools.messaging import whatsapp_web as web
from agent.runtime.voice_line import GENERATED, SAID, SILENT
from agent.tools.messaging.whatsapp_flow import ChatCandidate, ContactChoice
from agent.tools.tools_compat import ToolError
from agent.tools.tool_results import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

EMPTY_DOCUMENT = ({}, "test", [])


def run_tool(recipient: str, message: str, **extra) -> str:
    """Drive the tool synchronously against a session-less context."""
    return asyncio.run(
        whatsapp.send_whatsapp(SimpleNamespace(), recipient, message, **extra)
    )


def payload_of(result: str) -> dict:
    body = result.split(UNTRUSTED_OPEN, 1)[1].split(UNTRUSTED_CLOSE, 1)[0].strip()
    _preamble, _, json_text = body.partition("\n")
    return json.loads(json_text)


class AuditRedirect(unittest.TestCase):
    """A refused approval writes an audit line; the real file is not the place."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ,
            {"LUMINE_AUDIT_PATH": str(Path(tmp.name) / "audit.jsonl")},
        )
        patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


class NumberTests(unittest.TestCase):
    def test_ordinary_spellings_all_normalize(self):
        for raw in ("+1 (555) 123-4567", "15551234567", "+15551234567", "  1555 123 4567  "):
            with self.subTest(raw=raw):
                self.assertEqual(whatsapp.normalize_number(raw), "15551234567")

    def test_anything_that_is_not_a_number_is_none(self):
        for raw in ("", "   ", None, "hello", "+12345", "1234567890123456", "abc123456", "+1234567890123456"):
            with self.subTest(raw=raw):
                self.assertIsNone(whatsapp.normalize_number(raw))

    def test_non_ascii_digits_do_not_pass(self):
        # str.isdigit() would accept these and hand them to the OS inside a
        # URL; the pattern is ASCII on purpose.
        self.assertIsNone(whatsapp.normalize_number("\u0661\u0662\u0663\u0664\u0665\u0666"))

    def test_collapsed_text_is_one_line(self):
        self.assertEqual(whatsapp.collapse_text("  hello \n there \t friend "), "hello there friend")
        self.assertEqual(whatsapp.collapse_text(None), "")


class DeeplinkTests(unittest.TestCase):
    def test_the_link_carries_digits_and_encoded_text(self):
        self.assertEqual(
            whatsapp.build_deeplink("15551234567", "Buy milk"),
            "whatsapp://send?phone=15551234567&text=Buy%20milk",
        )

    def test_message_cannot_retarget_the_link(self):
        link = whatsapp.build_deeplink("15551234567", "a&phone=999&text=x#y")
        # One ampersand -- the one before text= -- and no raw fragment.
        self.assertEqual(link.count("&"), 1)
        self.assertNotIn("#", link)
        self.assertNotIn("phone=999", link.split("&text=", 1)[0])

    def test_no_text_means_no_text_parameter(self):
        self.assertEqual(
            whatsapp.build_deeplink("15551234567", ""),
            "whatsapp://send?phone=15551234567",
        )


class ModeResolutionTests(unittest.TestCase):
    def _resolve(self, document, env=None):
        with mock.patch.object(whatsapp, "effective_document", return_value=document), mock.patch.dict(
            os.environ, env or {}, clear=True
        ):
            return whatsapp.resolve_mode()

    def test_the_default_is_the_draft_mode(self):
        self.assertEqual(self._resolve(EMPTY_DOCUMENT), "deeplink")

    def test_the_environment_reaches_the_runtime(self):
        self.assertEqual(self._resolve(EMPTY_DOCUMENT, {whatsapp.MODE_VAR: "automation"}), "automation")

    def test_the_saved_document_beats_the_environment(self):
        document = ({"messaging": {"whatsapp": {"mode": "deeplink"}}}, "ui", [])
        self.assertEqual(self._resolve(document, {whatsapp.MODE_VAR: "automation"}), "deeplink")
        document = ({"messaging": {"whatsapp": {"mode": "off"}}}, "ui", [])
        self.assertEqual(self._resolve(document, {whatsapp.MODE_VAR: "automation"}), "off")

    def test_a_document_without_the_key_leaves_the_environment_in_place(self):
        document = ({"messaging": {}}, "ui", [])
        self.assertEqual(self._resolve(document, {whatsapp.MODE_VAR: "automation"}), "automation")

    def test_an_unrecognised_value_is_off_never_a_sending_mode(self):
        # The direction that matters: a typo in a line meant to *disable*
        # sending must not produce a mode that sends.
        self.assertEqual(self._resolve(EMPTY_DOCUMENT, {whatsapp.MODE_VAR: "banana"}), "off")
        document = ({"messaging": {"whatsapp": {"mode": "send-everything"}}}, "ui", [])
        self.assertEqual(self._resolve(document), "off")

    def test_disabled_reason_names_the_switch(self):
        document = ({"messaging": {"whatsapp": {"mode": "off"}}}, "ui", [])
        with mock.patch.object(whatsapp, "effective_document", return_value=document):
            reason = whatsapp.whatsapp_disabled_reason()
        self.assertTrue(reason)
        self.assertIn(whatsapp.MODE_VAR, reason)
        with mock.patch.object(whatsapp, "effective_document", return_value=EMPTY_DOCUMENT), mock.patch.dict(
            os.environ, {}, clear=True
        ):
            self.assertEqual(whatsapp.whatsapp_disabled_reason(), "")


class LedgerTests(unittest.TestCase):
    def test_a_sent_key_is_remembered_then_forgets_itself(self):
        ledger = whatsapp.SendLedger()
        ledger.mark("Alice", "hello", "sent", now=100.0)
        self.assertEqual(ledger.check("Alice", "hello", now=101.0), "sent")
        self.assertEqual(ledger.check("Alice", "hello", now=106.0), "sent")
        self.assertIsNone(ledger.check("Alice", "hello", now=111.0))

    def test_an_uncertain_key_outlives_the_dedup_window(self):
        ledger = whatsapp.SendLedger()
        ledger.mark("Alice", "hello", "uncertain", now=100.0)
        # Well past the dedup window (10s) -- still blocked, because the
        # question "did it send?" has not been answered by looking.
        self.assertEqual(ledger.check("Alice", "hello", now=400.0), "uncertain")
        self.assertIsNone(ledger.check("Alice", "hello", now=100.0 + whatsapp.UNCERTAIN_HOLD_SECONDS + 1))

    def test_different_messages_are_different_keys(self):
        ledger = whatsapp.SendLedger()
        ledger.mark("Alice", "hello", "sent", now=100.0)
        self.assertIsNone(ledger.check("Alice", "hello there", now=100.0))
        self.assertIsNone(ledger.check("Bob", "hello", now=100.0))

    def test_reset_clears_everything(self):
        ledger = whatsapp.SendLedger()
        ledger.mark("Alice", "hello", "uncertain")
        ledger.reset()
        self.assertIsNone(ledger.check("Alice", "hello"))

    def test_the_window_can_be_switched_off(self):
        ledger = whatsapp.SendLedger()
        with mock.patch.dict(os.environ, {whatsapp.DEDUP_VAR: "0"}, clear=True):
            ledger.mark("Alice", "hello", "sent", now=100.0)
            self.assertIsNone(ledger.check("Alice", "hello", now=101.0))


# ---------------------------------------------------------------------------
# The tool: draft mode
# ---------------------------------------------------------------------------


class DeeplinkToolTests(AuditRedirect):
    """Audit-redirected: the no-gate refusal below writes a line."""

    def setUp(self):
        # Cleared *before* the audit redirect in the base class: a
        # clear-all patch started afterwards would wipe the audit path it
        # just set, and the refusal test below would write to the real file.
        # The environment is also what decides the ledger's window, so the
        # documented default applies whatever this machine has exported.
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        super().setUp()
        self.ledger_patch = mock.patch.object(whatsapp, "ledger", whatsapp.SendLedger())
        self.ledger_patch.start()
        self.addCleanup(self.ledger_patch.stop)
        self.mode = mock.patch.object(whatsapp, "resolve_mode", return_value="deeplink")
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def _patches(self, opener=None):
        stack = contextlib.ExitStack()
        approval = stack.enter_context(
            mock.patch.object(whatsapp, "require_approval", new_callable=mock.AsyncMock)
        )
        opener = stack.enter_context(
            mock.patch.object(whatsapp, "open_deeplink", side_effect=opener)
        )
        self.addCleanup(stack.close)
        return approval, opener

    def test_a_draft_is_prepared_after_a_sensitive_approval(self):
        approval, opener = self._patches()
        result = run_tool("+1 (555) 123-4567", "Buy milk")

        approval.assert_awaited_once()
        kwargs = approval.call_args.kwargs
        self.assertEqual(kwargs["tool"], "send_whatsapp")
        self.assertEqual(kwargs["action_class"], "sensitive")
        self.assertIn("+15551234567", kwargs["summary"])
        self.assertIn("Buy milk", kwargs["summary"])
        opener.assert_called_once_with("whatsapp://send?phone=15551234567&text=Buy%20milk")
        self.assertIn("press send", result)

    def test_a_name_is_resolved_to_a_number_before_a_draft_is_prepared(self):
        # The default mode is this one, and it used to refuse every name, which
        # made "text Alice hello" dead-end on "read me her number" for a contact
        # WhatsApp had already matched a moment earlier. The name is resolved
        # through a *gated read* and only then prepared.
        approval, opener = self._patches()
        with mock.patch.object(
            whatsapp.web,
            "search_chats",
            new_callable=mock.AsyncMock,
            return_value=[{"title": "Alice", "jid": "15551234567@c.us", "group": False}],
        ):
            result = run_tool("Alice", "hi")

        opener.assert_called_once_with("whatsapp://send?phone=15551234567&text=hi")
        self.assertIn("press send", result)
        # Two gates, in order: the lookup is a read, the draft is sensitive. The
        # first one is what stops this being a way around the Tools page.
        self.assertEqual(approval.await_count, 2)
        self.assertEqual(approval.await_args_list[0].kwargs["action_class"], "read")
        self.assertEqual(approval.await_args_list[1].kwargs["action_class"], "sensitive")

    def test_a_name_with_no_number_behind_it_is_refused(self):
        # A group is the honest counter-example: it is a chat the account has,
        # and it has no number to build a `whatsapp://` link from. Guessing one
        # would open a stranger's chat.
        approval, opener = self._patches()
        with mock.patch.object(
            whatsapp.web,
            "search_chats",
            new_callable=mock.AsyncMock,
            return_value=[{"title": "Alice", "jid": "15551234567-1600000000@g.us", "group": True}],
        ):
            with self.assertRaisesRegex(ToolError, "international form"):
                run_tool("Alice", "hi")
        opener.assert_not_called()

    def test_a_name_the_account_does_not_have_is_refused(self):
        approval, opener = self._patches()
        with mock.patch.object(
            whatsapp.web, "search_chats", new_callable=mock.AsyncMock, return_value=[]
        ):
            with self.assertRaisesRegex(ToolError, "international form"):
                run_tool("Alice", "hi")
        opener.assert_not_called()

    def test_an_ambiguous_name_is_refused_rather_than_guessed(self):
        # Two chats match, so no number is chosen: a draft prepared for the wrong
        # one is a message the user did not agree to send to anybody.
        approval, opener = self._patches()
        with mock.patch.object(
            whatsapp.web,
            "search_chats",
            new_callable=mock.AsyncMock,
            return_value=[
                {"title": "Alice Rahman", "jid": "15551234567@c.us", "group": False},
                {"title": "Alice Islam", "jid": "15559999999@c.us", "group": False},
            ],
        ):
            with self.assertRaises(ToolError):
                run_tool("Alice", "hi")
        opener.assert_not_called()

    def test_empty_or_absent_text_is_refused(self):
        approval, opener = self._patches()
        for message in ("", "   \n "):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ToolError, "no message text"):
                    run_tool("+15551234567", message)
        approval.assert_not_awaited()
        opener.assert_not_called()

    def test_an_overlong_text_is_refused(self):
        approval, opener = self._patches()
        with self.assertRaisesRegex(ToolError, "too long"):
            run_tool("+15551234567", "x" * (whatsapp.MESSAGE_MAX + 1))
        opener.assert_not_called()

    def test_the_same_draft_is_not_prepared_twice(self):
        approval, opener = self._patches()
        run_tool("+15551234567", "hello")
        second = run_tool("+15551234567", "hello")

        self.assertIn("already opened", second)
        approval.assert_awaited_once()
        opener.assert_called_once()

    def test_a_link_that_could_not_be_opened_is_not_remembered_as_done(self):
        # Mirror of the launch-dedup rule: a failure releases the claim, so
        # the user asking again after WhatsApp was missing gets a second try
        # rather than a permanent "already prepared".
        approval, _ = self._patches(opener=OSError("whatsapp is not installed"))
        with self.assertRaisesRegex(ToolError, "couldn't open WhatsApp"):
            run_tool("+15551234567", "hello")

        approval2, opener2 = self._patches()
        result = run_tool("+15551234567", "hello")
        self.assertIn("press send", result)
        approval2.assert_awaited_once()
        opener2.assert_called_once()

    def test_a_draft_without_an_approval_channel_prepares_nothing(self):
        # Real `require_approval`, unpatched: no gate, sensitive class, so
        # the standard refusal -- and the link is never built or opened.
        with mock.patch.object(whatsapp, "open_deeplink") as opener:
            with self.assertRaises(ToolError) as caught:
                run_tool("+15551234567", "hello")
        self.assertEqual(str(caught.exception), MSG_NO_GATE)
        opener.assert_not_called()

    def test_mode_off_says_so_before_anything_else(self):
        approval, opener = self._patches()
        with mock.patch.object(whatsapp, "resolve_mode", return_value="off"):
            with self.assertRaisesRegex(ToolError, "switched off"):
                run_tool("+15551234567", "hello")
        approval.assert_not_awaited()
        opener.assert_not_called()


# ---------------------------------------------------------------------------
# The tool: automation mode
# ---------------------------------------------------------------------------


class AutomationToolTests(unittest.TestCase):
    """Every WhatsApp Web step is patched at the module seam.

    The JS behind those steps is exercised only by WhatsApp itself; what is
    pinned here is the *ordering* -- resolve before gate, gate before
    typing, uncertain before retry -- because that ordering is what makes a
    double send impossible rather than unlikely.
    """

    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

        self.stack.enter_context(mock.patch.object(whatsapp, "resolve_mode", return_value="automation"))
        # The desktop adapter is real and running on the machine this suite runs
        # on. Web is tried first, so these tests pin the Web seam and the desktop
        # check must never be reached; the choice itself has its own tests, and
        # `DesktopPathToolTests` covers the machine where Web is down.
        self.stack.enter_context(
            mock.patch.object(
                whatsapp.desktop,
                "desktop_capability",
                return_value={
                    "available": False,
                    "reason": whatsapp.desktop.MSG_NOT_RUNNING,
                },
            )
        )
        self.ledger = self.stack.enter_context(
            mock.patch.object(whatsapp, "ledger", whatsapp.SendLedger())
        )
        self.approval = self._mock("require_approval")
        self.state = self._mock("whatsapp_state", return_value="ready")
        self.contacts = self._mock("list_contacts", return_value=["Alice"])
        self.search = self._mock("search_number", return_value="Bob")
        self.opened = self._mock("open_chat", return_value=True)
        # `draft` is the typed-and-verified operation, and it replaces
        # `type_message`: the flow no longer has a "type and move on", because
        # what is typed is now something the user is asked to approve.
        self.drafted = self._mock("draft_message", return_value=True)
        self.discarded = self._mock("discard_draft", return_value=True)
        self.sent = self._mock("press_send")
        self.delivery = self._mock("delivery_seen", return_value=True)

    def _mock(self, name, **kwargs):
        return self.stack.enter_context(
            mock.patch.object(whatsapp, name, new_callable=mock.AsyncMock, **kwargs)
        )

    def _approvals_for(self, action):
        """The gate calls for one action.

        A name send calls `require_approval` twice: once for the read-side
        contact lookup (a card nobody sees -- reads are automatic at the
        default policy), and once for the send (the card with the name and
        the wording on it). Filtering by action keeps these assertions
        about the question that is actually asked.
        """
        return [
            call
            for call in self.approval.call_args_list
            if call.kwargs.get("action") == action
        ]

    def test_a_resolved_name_is_sent_after_a_destructive_approval(self):
        result = run_tool("Alice", "hello \n world")

        sends = self._approvals_for("send")
        self.assertEqual(len(sends), 1)
        kwargs = sends[0].kwargs
        self.assertEqual(kwargs["action_class"], "destructive")
        self.assertIn("Alice", kwargs["summary"])
        # Whitespace collapsed *before* the card, so the wording the user
        # approves is the wording WhatsApp receives.
        self.assertIn("hello world", kwargs["summary"])
        self.opened.assert_awaited_once_with("Alice")
        self.drafted.assert_awaited_once_with("hello world")
        self.sent.assert_awaited_once()
        self.assertIn("Sent to Alice", result)

    def test_several_matches_return_candidates_and_send_nothing(self):
        self.contacts.return_value = ["John Doe", "Johnny Cash"]

        result = run_tool("jo", "surprise")

        payload = payload_of(result)
        self.assertEqual(payload["status"], "choose")
        self.assertEqual(payload["candidates"], ["John Doe", "Johnny Cash"])
        # No send question, no typing: the user picks first, or nobody sends.
        # (The read-side lookup did run -- that is what found the candidates.)
        self.assertEqual(self._approvals_for("send"), [])
        self.drafted.assert_not_awaited()
        self.sent.assert_not_awaited()

    def test_an_exact_match_among_near_names_wins(self):
        self.contacts.return_value = ["John Doe", "Johnny Cash"]
        result = run_tool("john doe", "hi")
        self.opened.assert_awaited_once_with("John Doe")
        self.assertIn("Sent to John Doe", result)

    def test_no_contact_at_all_is_refused(self):
        self.contacts.return_value = []
        with self.assertRaisesRegex(ToolError, "chat list"):
            run_tool("nobody", "hi")
        self.assertEqual(self._approvals_for("send"), [])
        self.drafted.assert_not_awaited()

    def test_sending_requires_somebody_who_asked(self):
        # Declined card. The send functions are never touched -- this is the
        # assertion that makes the gate load-bearing rather than decorative.
        # The draft *is* written, because that happens before the question by
        # design, and it is cleared again on the way out; what must not happen is
        # a send.
        def decline_on_send(*args, **kwargs):
            if kwargs.get("action") == "send":
                raise ToolError("The user declined this action.")

        self.approval.side_effect = decline_on_send
        with self.assertRaisesRegex(ToolError, "declined"):
            run_tool("Alice", "hello")
        self.drafted.assert_awaited_once()
        self.discarded.assert_awaited()
        self.sent.assert_not_awaited()
        self.assertIsNone(self.ledger.check("Alice", "hello"))

    def test_a_logged_out_page_is_reported_before_anything_is_asked(self):
        self.state.return_value = "logged_out"
        with self.assertRaisesRegex(ToolError, "not signed in"):
            run_tool("Alice", "hello")
        # Before the gate: a question wasted on a session that cannot send
        # is a question the user answers twice.
        self.approval.assert_not_awaited()
        self.contacts.assert_not_awaited()

    def test_a_number_that_opens_no_chat_is_not_messaged(self):
        self.search.return_value = None
        with self.assertRaisesRegex(ToolError, "isn't one of your WhatsApp chats"):
            run_tool("+15551234567", "hello")
        self.opened.assert_not_awaited()
        self.drafted.assert_not_awaited()

    def test_a_number_that_resolves_is_messaged(self):
        result = run_tool("+15551234567", "hello")
        self.search.assert_awaited_once_with("15551234567")
        self.opened.assert_awaited_once_with("Bob")
        self.assertIn("Sent to +15551234567", result)

    def test_a_send_that_could_not_be_confirmed_is_never_retried(self):
        self.delivery.return_value = False

        with self.assertRaisesRegex(ToolError, "could not confirm"):
            run_tool("Alice", "hello")
        self.assertEqual(self.ledger.check("Alice", "hello"), "uncertain")

        # The second ask is refused before the gate -- no second question,
        # no second attempt -- and the wording says why.
        with self.assertRaisesRegex(ToolError, "twice"):
            run_tool("Alice", "hello")
        self.assertEqual(len(self._approvals_for("send")), 1)
        self.drafted.assert_awaited_once()
        self.sent.assert_awaited_once()

    def test_a_confirmed_send_is_not_sent_again(self):
        run_tool("Alice", "hello")
        second = run_tool("Alice", "hello")

        self.assertIn("already sent", second)
        self.assertEqual(len(self._approvals_for("send")), 1)
        self.drafted.assert_awaited_once()

    def test_a_failure_before_the_attempt_stays_retryable(self):
        # Drafting refused = nothing left the tool. Unlike an uncertain send,
        # this must NOT poison the ledger -- the user asks again and it works.
        #
        # One fewer approval than the old flow asked for, and that is the point of
        # typing first: a draft that could not be written is discovered *before*
        # the question, so the user is never asked to approve a message that was
        # never on their screen.
        self.drafted.side_effect = ToolError("I can't see the message box on this chat. Nothing was typed.")
        with self.assertRaisesRegex(ToolError, "message box"):
            run_tool("Alice", "hello")
        self.assertIsNone(self.ledger.check("Alice", "hello"))
        self.assertEqual(self._approvals_for("send"), [])

        self.drafted.side_effect = None
        result = run_tool("Alice", "hello")
        self.assertIn("Sent to Alice", result)
        self.assertEqual(len(self._approvals_for("send")), 1)

    def test_a_marked_uncertain_key_blocks_before_the_gate(self):
        # Seeded directly: the state a previous *attempt* left behind. The
        # refusal happens before the user is asked *again* -- the read-side
        # lookup still runs, but it shows nobody a card.
        self.ledger.mark("Alice", "hello", "uncertain")
        with self.assertRaisesRegex(ToolError, "not risk sending it twice"):
            run_tool("Alice", "hello")
        self.assertEqual(self._approvals_for("send"), [])
        self.drafted.assert_not_awaited()


# ---------------------------------------------------------------------------
# The seam: which path, and what confirms a send
# ---------------------------------------------------------------------------


class BackendChoiceTests(unittest.TestCase):
    """WhatsApp Web, chosen whole, before anything is resolved.

    This class used to pin the opposite. ``PATH_PREFERENCE`` was
    ``("web", "desktop")`` and the interesting assertions were about *when* the
    desktop adapter carried on -- the fallback that needed no browser, no profile
    and no QR code.

    It was reasonable in the abstract and it failed three ways in practice, all
    visible to the user: two applications answering one sentence; a name resolved on
    one path that then could not be opened on the other; and an authority question
    nobody should have to answer mid-sentence. The assertions below are the
    replacement, and each one names the fault it would have let through.
    """

    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.web_state = self.stack.enter_context(
            mock.patch.object(
                whatsapp,
                "whatsapp_state",
                new_callable=mock.AsyncMock,
                return_value="ready",
            )
        )

    def _desktop_would_reach(self):
        """Patch the desktop adapter so that touching it is a visible failure.

        The point is that these tests would have *passed* against a fallback that
        no longer exists -- `web_state` returning "logged_out" used to select the
        desktop backend and assert it was chosen. With the adapter patched to
        raise, a single stray reference anywhere on the path fails here.
        """
        self.stack.enter_context(
            mock.patch.object(
                whatsapp.desktop,
                "desktop_capability",
                side_effect=AssertionError("a messaging path reached for the desktop app"),
            )
        )

    def test_web_is_chosen_when_it_is_ready(self):
        self._desktop_would_reach()
        chosen = asyncio.run(whatsapp.choose_backend())
        self.assertEqual(chosen.name, "WhatsApp Web")

    def test_web_not_being_ready_is_a_refusal_and_never_a_fallback(self):
        """The bug, stated as the contract that replaces it.

        Web signed out used to select the desktop app, which is what produced a
        send leaving through one application while the user watched another. The
        answer is now one sentence about Web and nothing else -- not a second
        application.
        """
        self._desktop_would_reach()
        self.web_state.return_value = "logged_out"
        with self.assertRaises(ToolError) as caught:
            asyncio.run(whatsapp.choose_backend())
        message = str(caught.exception)
        self.assertIn("not signed in", message)
        self.assertNotIn(
            "WhatsApp Desktop", message, "the refusal names two applications"
        )
        self.assertEqual(message.count("Nothing was sent."), 1)

    def test_the_refusal_never_mentions_the_installed_application(self):
        self._desktop_would_reach()
        for token in ("logged_out", "loading"):
            with self.subTest(state=token):
                self.web_state.return_value = token
                with self.assertRaises(ToolError) as caught:
                    asyncio.run(whatsapp.choose_backend())
                self.assertNotIn("Desktop", str(caught.exception))

    def test_an_operation_web_cannot_do_is_reported_as_about_web(self):
        # The verb still decides eligibility. There is no second path to skip, so
        # the refusal names the one path and says what it cannot do.
        #
        # A verb that does not exist rather than one Web lacks: every row in the
        # table is Web-capable, so "something Web cannot do" can only be reached
        # with an unknown verb -- and that is the useful case anyway, because a
        # caller passing one should hear "WhatsApp Web cannot X" and not a version
        # of that sentence naming a second application.
        self._desktop_would_reach()
        self.web_state.return_value = "logged_out"
        with self.assertRaises(ToolError) as caught:
            asyncio.run(whatsapp.choose_backend("teleport"))
        message = str(caught.exception)
        self.assertIn("WhatsApp Web cannot", message)
        self.assertNotIn("WhatsApp Desktop", message)

    def test_a_supported_verb_on_a_ready_web_path_answers(self):
        self._desktop_would_reach()
        chosen = asyncio.run(whatsapp.choose_backend("delete"))
        self.assertEqual(chosen.name, "WhatsApp Web")

    def test_the_candidate_list_never_contains_a_second_backend(self):
        self.assertEqual(len(whatsapp.backend_candidates()), 1)


def fake_backend(order: list[str], **overrides: object) -> object:
    """A Backend where everything works, recording the order it happened in.

    The sequence's rules live in the tool rather than in either path, so faking
    the path is enough to pin them: what runs, in what order, and which failures
    land on which side of "attempted". `order` is passed in rather than returned
    so a test can watch one list across several calls, and `**overrides` replaces
    a single operation without restating the other eight.

    Named "WhatsApp Web" on purpose: `_path_of` reads the name, and being the web
    path is what makes an attachment refusal the web path's own rather than a
    special case.
    """

    async def check():
        order.append("check")
        return None

    async def contacts(query: str):
        order.append("contacts")
        return [query]

    async def search(number: str):
        order.append("search")
        return "Bob"

    async def search_name(name: str):
        order.append("search_name")
        return [{"title": name, "kind": "contact"}]

    async def open_chat(name: str):
        order.append("open")
        return True

    async def draft(text: str):
        order.append("draft")
        return True

    async def discard():
        order.append("discard")
        return True

    async def press_send():
        order.append("send")

    async def delivery_seen(text: str):
        order.append("delivery")
        return whatsapp.Observation(True, time.monotonic())

    fields: dict[str, object] = {
        "name": "WhatsApp Web",
        "check": check,
        "list_contacts": contacts,
        "search_number": search,
        "search_name": search_name,
        "open_chat": open_chat,
        "draft": draft,
        "discard_draft": discard,
        "press_send": press_send,
        "delivery_seen": delivery_seen,
    }
    fields.update(overrides)
    return whatsapp.Backend(**fields)


class PathAgreementTests(unittest.TestCase):
    """One path for the whole operation.

    The two WhatsApp paths contradicted each other in front of the user twice:
    once because the Web half of this module was pointed at the *browse* tab
    while `whatsapp_web` used the WhatsApp tab, and once because contact search
    chose a path for itself while the send chose another. Both are invisible in a
    unit test that mocks everything, which is why they are pinned here by
    asserting the *structure* rather than the behaviour.
    """

    def test_the_web_half_never_touches_the_browse_tab(self):
        """The bug that made searching work and selecting do nothing.

        Search ran against one page and the click against another, so a contact
        could be found and then never opened. The two modules are one path split
        across two files; if they ever point at different pages again, every read
        and every action silently refers to a different application.

        Checked on the *syntax tree* rather than the text, because a comment
        explaining this very bug contains the name, and a substring test would
        then be a test that cannot be documented.
        """
        import ast

        for module in (whatsapp, whatsapp.web):
            with self.subTest(module=module.__name__):
                tree = ast.parse(inspect.getsource(module))
                imported = {
                    alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    for alias in node.names
                }
                self.assertNotIn(
                    "working_page",
                    imported,
                    f"{module.__name__} must not import the browse page",
                )
                called = {
                    node.func.id
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                }
                self.assertNotIn(
                    "working_page",
                    called,
                    f"{module.__name__} must not call the browse page",
                )

    def test_every_backend_can_search_a_name_and_the_ones_built_do(self):
        # `search_name` is what makes the path a decision made once. A backend
        # without it would send from one application having looked the name up in
        # another.
        for factory in (whatsapp.web_backend, whatsapp.desktop_backend):
            with self.subTest(backend=factory.__name__):
                self.assertTrue(callable(factory().search_name))

    def test_contact_search_uses_the_path_it_is_given_and_never_picks_one(self):
        import ast

        signature = inspect.signature(whatsapp.search_contact)
        self.assertIn(
            "backend",
            signature.parameters,
            "search_contact must take the chosen path rather than choosing one",
        )
        # Checked on the tree, not the text: the docstring explains that this
        # function *used* to call `choose_backend`, and a substring test would
        # then forbid documenting the bug it exists to prevent.
        tree = ast.parse(inspect.getsource(whatsapp._resolve_contact))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn(
            "choose_backend",
            called,
            "resolving a name must not choose a second path",
        )

    def test_the_resolve_step_reuses_the_backend_the_send_will_use(self):
        import inspect

        source = inspect.getsource(whatsapp.send_whatsapp)
        self.assertIn(
            "_resolve_contact(backend",
            source,
            "the recipient must be resolved on the backend that will send to it",
        )


class HeaderCheckTests(unittest.TestCase):
    """A moved selector is not a wrong chat, and must not refuse like one.

    The page is mocked at ``evaluate``, which is called once per script, so the
    fake answers by *which* script it is given rather than with one fixed value.
    A single fixed value cannot express the case that matters -- the click landing
    while the header stays unreadable -- because it would make the click fail too.
    """

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def _page_reporting_click_true_and_header(self, title: str, *, marked: str = "Afia"):
        """A page where the row is marked, the real click lands, and the header says `title`.

        Two things the fake has to get right, and both were bugs:
        `evaluate` answers *per script* rather than with one value, and `click` is
        a real method the code calls -- an untrusted `row.click()` inside the page
        is what left a healthy chat list unopened.

        The mark script is ``whatsapp_web``'s, because ``whatsapp.open_chat`` now
        delegates to it. Driving the send path against the *Web* implementation is
        the point: these tests cover the one implementation both entry points use,
        and a second copy in ``whatsapp`` is what made the send tool refuse to open
        a contact its own previous step had found.
        """
        page = mock.Mock()
        mark = web._JS_MARK_ROW
        header = web._JS_ACTIVE_CHAT

        async def evaluate(script, *args, **kwargs):
            del kwargs
            if script is mark:
                return marked
            if script is header:
                return {"title": title} if title else {}
            return {}

        page.evaluate = evaluate
        page.click = mock.AsyncMock(return_value=None)
        # Patched on **`whatsapp_web`**, not on `whatsapp`, because
        # `whatsapp.open_chat` delegates to it and the code that runs resolves
        # `whatsapp_page` and `require_ready` in the *Web* module's namespace.
        # Patching the wrapper's module was how these tests kept passing while
        # exercising nothing.
        self.stack.enter_context(
            mock.patch.object(web, "whatsapp_page", new=mock.AsyncMock(return_value=page))
        )
        self.stack.enter_context(
            mock.patch.object(web, "require_ready", new=mock.AsyncMock(return_value=None))
        )
        # No waiting: the loop is bounded by the poll count, and sleeping in a
        # test that exists to prove we do *not* refuse is 2.4 wasted seconds.
        self.stack.enter_context(
            mock.patch.object(web.asyncio, "sleep", new=mock.AsyncMock())
        )
        self.stack.enter_context(
            mock.patch.object(web, "_sleep", new=mock.AsyncMock(return_value=None))
        )
        return page

    def test_a_definitely_different_chat_is_refused(self):
        # The real mismatch, and the case the check exists for.
        self._page_reporting_click_true_and_header("Bob")
        self.assertFalse(asyncio.run(whatsapp.open_chat("Afia")))

    def test_the_right_chat_is_accepted(self):
        self._page_reporting_click_true_and_header("Afia")
        self.assertTrue(asyncio.run(whatsapp.open_chat("Afia")))

    def test_an_unreadable_header_proceeds_rather_than_refusing(self):
        # WhatsApp renaming a selector must cost the *verification*, not the
        # capability. Refusing here made "I can find Afia but cannot select her"
        # look like a product fault rather than a layout change -- and the click
        # did land, and nothing is sent until the user approves a draft they can
        # read on their own screen.
        self._page_reporting_click_true_and_header("")
        self.assertTrue(asyncio.run(whatsapp.open_chat("Afia")))

    def test_a_click_that_does_not_land_is_still_refused(self):
        # The one case where proceeding would be wrong: nothing opened, so there
        # is no conversation to type into.
        #
        # Patched on `web`, because that is where the code now runs. With a mark
        # returning "" the search fallback is *also* tried, and on this fake it
        # finds nothing either -- so this asserts both routes failing, which is the
        # real condition: no conversation anywhere.
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(return_value="")
        page.click = mock.AsyncMock(return_value=None)
        self.stack.enter_context(
            mock.patch.object(web, "whatsapp_page", new=mock.AsyncMock(return_value=page))
        )
        self.stack.enter_context(
            mock.patch.object(web, "require_ready", new=mock.AsyncMock(return_value=None))
        )
        self.stack.enter_context(
            mock.patch.object(web, "_sleep", new=mock.AsyncMock(return_value=None))
        )
        self.assertFalse(asyncio.run(whatsapp.open_chat("Afia")))

    def test_a_click_that_raises_is_refused_rather_than_crashing(self):
        # `page.click` failing is the browser refusing the element -- hidden,
        # covered, detached -- and every one of those means the row did not open.
        page = mock.Mock()
        page.evaluate = mock.AsyncMock(return_value="Afia")
        page.click = mock.AsyncMock(side_effect=RuntimeError("element is not visible"))
        self.stack.enter_context(
            mock.patch.object(web, "whatsapp_page", new=mock.AsyncMock(return_value=page))
        )
        self.stack.enter_context(
            mock.patch.object(web, "require_ready", new=mock.AsyncMock(return_value=None))
        )
        self.stack.enter_context(
            mock.patch.object(web, "_sleep", new=mock.AsyncMock(return_value=None))
        )
        self.assertFalse(asyncio.run(whatsapp.open_chat("Afia")))

    def test_the_row_is_clicked_for_real_and_not_from_inside_the_page(self):
        """The bug behind "she can search but cannot select".

        The probe showed the click reporting success and no conversation ever
        opening: `row.click()` inside the page dispatches an untrusted event, so
        WhatsApp's own handler does nothing. The click has to go through
        Playwright, which is the difference between "the page heard a click" and
        "a click happened".
        """
        page = self._page_reporting_click_true_and_header("Afia")
        self.assertTrue(asyncio.run(whatsapp.open_chat("Afia")))
        page.click.assert_awaited_once()
        self.assertEqual(page.click.await_args.args[0], whatsapp._ROW_SELECTOR)

    def test_the_marker_script_never_clicks_inside_the_page(self):
        # Asserted on the script text rather than its AST: it is a JavaScript
        # template string, so there is no tree to walk -- and the bug is a
        # literal `.click(` appearing in it.
        #
        # On the **Web** script, which is the only one there is now. It used to be
        # checked on the send path's private copy, which is why a second copy could
        # exist next to the first and be tested separately from the one actually
        # reached on some paths.
        self.assertNotIn(".click()", web._JS_MARK_ROW)
        self.assertNotIn(".click(", web._JS_MARK_ROW)
        self.assertIn("data-lumine-wa-row", web._JS_MARK_ROW)

    def test_the_probe_marks_its_row_rather_than_clicking_inside_the_page(self):
        # Same rule on the diagnostic path, which is where this was first seen.
        self.assertNotIn(".click()", whatsapp.web._JS_MARK_FIRST_CHAT)
        self.assertNotIn(".click(", whatsapp.web._JS_MARK_FIRST_CHAT)


class DeeplinkModeTests(unittest.TestCase):
    """Draft mode must never dead-end, and must say what it cannot do."""

    def test_the_refusal_names_the_switch_and_offers_both_ways_out(self):
        # This sentence is what the user reads when they say "message Afia" and
        # she asks for a number. It used to read as though she had searched and
        # failed, which sent people looking for a contact that was right there.
        text = whatsapp.MSG_NEED_NUMBER
        self.assertIn("international form", text)
        self.assertIn("automation", text)
        self.assertIn("by name", text)
        self.assertIn("Nothing was prepared", text)

    def test_the_default_mode_is_still_the_one_that_does_not_send(self):
        # The *fallback* stays draft mode for an unconfigured install. It is a
        # deliberate product decision and the reason automation has to be turned
        # on deliberately: an install nobody configured should never auto-send in
        # the user's name.
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                whatsapp, "effective_document", return_value=({}, "test", [])
            ):
                self.assertEqual(whatsapp.resolve_mode(), "deeplink")


def _answer(delivered: bool):
    """A `delivery_seen` that reports `delivered`, as a *callable*.

    A factory rather than the coroutine itself: the backend calls this, and
    handing it a coroutine object made every call raise ``TypeError: 'coroutine'
    object is not callable``. That still ended in the refusal this test asserts,
    so it passed -- while covering "the backend blew up" instead of "the backend
    said the message did not arrive", which is the claim in the test's name.
    """

    async def answer(text: str) -> whatsapp.Observation:
        return whatsapp.Observation(delivered, time.monotonic())

    return answer


async def _unfocusable():
    raise ToolError("could not take the keyboard")


async def _stale(text: str) -> whatsapp.Observation:
    """A 'delivered' answer observed long before the send it would claim.

    The staleness case: a backend vouching for its own earlier reading rather
    than for the send it is being asked about.
    """
    return whatsapp.Observation(True, time.monotonic() - 60.0)




class _SendHarness(unittest.TestCase):
    """Everything needed to drive `send_whatsapp` end to end against a fake path.

    Split out of :class:`SendSequenceTests` so another class can assert something
    about the *shape* of a send without re-running that class's ten ordering
    tests. Inheriting it instead of sharing it meant every one of those tests ran
    twice, which is the kind of duplication that makes a suite slower to read and
    nobody able to say which copy is the real one.

    `choose_backend` is replaced by a stub that still *checks* the path before
    handing it over, because the check is a step in the sequence -- dropping it
    would quietly stop asserting that a readiness check happens before anything
    is typed into a chat.
    """

    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.order: list[str] = []
        self.stack.enter_context(
            mock.patch.object(whatsapp, "resolve_mode", return_value="automation")
        )
        self.stack.enter_context(
            mock.patch.object(whatsapp, "ledger", whatsapp.SendLedger())
        )
        # The desktop adapter is real on the machine this suite runs on. These
        # tests pin the Web seam; `BackendChoiceTests` owns the choice itself.
        self.stack.enter_context(
            mock.patch.object(
                whatsapp.desktop,
                "desktop_capability",
                return_value={"available": False, "reason": "not here"},
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                whatsapp,
                "_resolve_contact",
                new=mock.AsyncMock(
                    return_value=ContactChoice(
                        ChatCandidate(title="Alice"), (ChatCandidate(title="Alice"),)
                    )
                ),
            )
        )
        self.approval = self.stack.enter_context(
            mock.patch.object(whatsapp, "require_approval", new_callable=mock.AsyncMock)
        )
        self.use_backend()

    def use_backend(self, **overrides: object) -> None:
        """Point `choose_backend` at a fake path, still checking it first."""
        backend = fake_backend(self.order, **overrides)

        async def chooser(verb: str = "send_text"):
            not_ready = await backend.check()
            if not_ready is not None:
                raise ToolError(not_ready)
            return backend

        self.stack.enter_context(
            mock.patch.object(whatsapp, "choose_backend", new=chooser)
        )

    def sends_asked_for(self) -> list:
        return [c for c in self.approval.call_args_list if c.kwargs.get("action") == "send"]


class SendSequenceTests(_SendHarness):
    """Resolve, draft, ask, send, confirm -- and where retry stops being safe.

    These used to drive `automation_send` directly. They drive the *tool* now,
    because the tool is what runs: `automation_send` was folded into it when the
    flow gained a draft step and a gate in the middle, and a test that pins an
    ordering nobody executes is a test that keeps passing while the ordering
    changes.
    """

    def test_the_draft_is_typed_before_the_question_and_the_send_after_it(self):
        # The order that makes this an assistant rather than a form: the text is
        # in the user's own WhatsApp, on their screen, before they are asked
        # anything -- and nothing leaves until after they have said yes.
        run_tool("Alice", "hello \n world")

        self.assertEqual(self.order, ["check", "open", "draft", "send", "delivery"])
        asks = self.sends_asked_for()
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0].kwargs["action_class"], "destructive")
        self.assertIn("Alice", asks[0].kwargs["summary"])
        # Whitespace collapsed before the card, so the wording the user approves
        # is the wording WhatsApp receives.
        self.assertIn("hello world", asks[0].kwargs["summary"])

    def test_a_number_is_searched_for_before_the_chat_is_opened(self):
        run_tool("+15551234567", "hi")
        self.assertEqual(self.order[:4], ["check", "search", "open", "draft"])

    def test_a_confirmation_observed_before_the_attempt_cannot_confirm_it(self):
        # The stale-observation case: a backend answering from a reading taken
        # *before* Enter was pressed is vouching for its own earlier state, not
        # for this send. It must not be believed -- the hold, not a green light,
        # is the correct outcome.
        self.use_backend(delivery_seen=_stale)
        with self.assertRaisesRegex(ToolError, "could not confirm"):
            run_tool("Alice", "hello")

    def test_a_backend_that_says_not_delivered_is_uncertain(self):
        self.use_backend(delivery_seen=_answer(False))
        with self.assertRaisesRegex(ToolError, "could not confirm"):
            run_tool("Alice", "hello")
        # The send was attempted and the *observation* is what refused it -- which
        # is only true now that the backend is actually called, and is the
        # difference between "not delivered" and "we broke".
        self.assertIn("send", self.order)

    def test_leftovers_in_the_message_box_hold_the_retry_too(self):
        # `DirtyText` happens *before* the send, so nothing went out -- but a
        # blind retry would type on top of what is already there, which is
        # exactly why it must land on the held side of the line.
        async def dirty(text: str):
            self.order.append("draft")
            raise whatsapp.desktop.DirtyText("could not clear")

        self.use_backend(draft=dirty)
        with self.assertRaisesRegex(ToolError, "could not confirm"):
            run_tool("Alice", "hello")
        self.assertNotIn("send", self.order)

    def test_a_draft_that_was_not_taken_exactly_is_refused_before_anything_is_sent(self):
        # The verification is what makes the spoken words true. A draft that lost
        # its tail must not be sent, and must not be left sitting there for
        # somebody to press send on by hand.
        async def lossy(text: str):
            self.order.append("draft")
            return False

        self.use_backend(draft=lossy)
        with self.assertRaisesRegex(ToolError, "exactly as I typed"):
            run_tool("Alice", "hello")
        self.assertNotIn("send", self.order)
        self.assertEqual(self.sends_asked_for(), [])

    def test_a_refusal_before_the_draft_stays_retryable(self):
        async def not_ready():
            self.order.append("check")
            return "WhatsApp Web is not signed in yet. Nothing was sent."

        self.use_backend(check=not_ready)
        with self.assertRaises(ToolError) as caught:
            run_tool("Alice", "hello")
        # The sentence comes back whole -- stripping and re-appending the closing
        # clause must be a no-op, not a stutter.
        self.assertEqual(
            str(caught.exception),
            "WhatsApp Web is not signed in yet. Nothing was sent.",
        )
        self.assertEqual(self.order, ["check"])

    def test_a_declined_send_discards_the_draft_and_leaves_nothing_behind(self):
        # One "no" must not break the feature. A draft left in the composer makes
        # every later send refuse with "there is already something written
        # there", so the refusal path has to clear what it typed.
        def decline(*args, **kwargs):
            if kwargs.get("action") == "send":
                raise ToolError("The user declined this action.")

        self.approval.side_effect = decline
        with self.assertRaisesRegex(ToolError, "declined"):
            run_tool("Alice", "hello")
        self.assertIn("draft", self.order)
        self.assertIn("discard", self.order)
        self.assertNotIn("send", self.order)

    def test_a_typing_failure_is_retryable_but_a_send_failure_is_not(self):
        # The line: drafting failing means nothing left the tool; the send
        # failing means we do not know, and the hold applies.
        async def unseeable(text: str):
            self.order.append("draft")
            raise ToolError("I can't see the message box on this chat. Nothing was typed.")

        self.use_backend(draft=unseeable)
        with self.assertRaisesRegex(ToolError, "message box"):
            run_tool("Alice", "hello")
        self.assertNotIn("send", self.order)

        self.order.clear()
        self.use_backend(press_send=_unfocusable)
        with self.assertRaisesRegex(ToolError, "could not confirm"):
            run_tool("Alice", "hello")
        self.assertNotIn("delivery", self.order)

    def test_an_unknown_chat_on_the_chosen_path_is_a_plain_refusal(self):
        async def nobody(name: str):
            self.order.append("open")
            return False

        self.use_backend(open_chat=nobody)
        with self.assertRaisesRegex(ToolError, "chat list"):
            run_tool("Nobody", "hello")
        self.assertEqual(self.order, ["check", "open"])
        self.assertEqual(self.sends_asked_for(), [])

    def test_draft_only_types_composer_without_approval_or_sending(self):
        result = run_tool("Alice", "draft text", draft_only=True)
        self.assertEqual(self.order, ["check", "open", "draft"])
        self.assertEqual(self.sends_asked_for(), [])
        self.assertIn("Drafted message to Alice", result)
        self.assertIn("draft text", result)


# ---------------------------------------------------------------------------


class DesktopPathToolTests(unittest.TestCase):
    """Web signed out is a refusal, not a route to the installed app.

    This class used to be the opposite: it drove the whole tool with Web mocked
    as *signed out* and the desktop adapter mocked as *ready*, and asserted the
    send went out through WhatsApp Desktop. That fallback was the user's bug --
    "she's opening the desktop WhatsApp and searching there while opening WhatsApp
    Web in the browser" -- so the class now asserts the refusal, with every
    desktop function patched to **raise**, so a single stray reference anywhere on
    the path fails here rather than in front of somebody's chat list.
    """

    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

        self.stack.enter_context(
            mock.patch.object(whatsapp, "resolve_mode", return_value="automation")
        )
        self.ledger = self.stack.enter_context(
            mock.patch.object(whatsapp, "ledger", whatsapp.SendLedger())
        )
        self.approval = self._mock(whatsapp, "require_approval")
        # Every desktop entry point, patched to fail loudly. The desktop app may be
        # *launched* as a program; it may not be *driven* as a messaging path, and
        # this is where that distinction stops being an intention.
        self.dunreachable = "the installed WhatsApp app was reached as a messaging path"
        self.stack.enter_context(
            mock.patch.object(
                whatsapp.desktop,
                "desktop_capability",
                side_effect=AssertionError(self.dunreachable),
            )
        )
        for name in (
            "state",
            "list_contacts",
            "search_number",
            "open_chat",
            "type_message",
            "clear_composer",
            "press_send",
            "delivery_seen",
        ):
            self._mock(
                whatsapp.desktop,
                name,
                side_effect=AssertionError(self.dunreachable),
            )
        self.web_contacts = self._mock(whatsapp, "list_contacts", side_effect=AssertionError("Web path used"))
        # Signed out, explicitly. Not silent, because a *reachable* Web path is the
        # case under test and a mock that said nothing would make this pass.
        self.web_state = self._mock(whatsapp, "whatsapp_state", return_value="logged_out")

    def _mock(self, owner, name, **kwargs):
        return self.stack.enter_context(
            mock.patch.object(owner, name, new_callable=mock.AsyncMock, **kwargs)
        )

    def test_a_name_send_refuses_rather_than_opening_the_installed_app(self):
        # The reported bug. Web is signed out, so the old fallback opened WhatsApp
        # Desktop and sent through it -- two applications answering one sentence.
        with self.assertRaises(ToolError) as caught:
            run_tool("Alice", "hello")
        message = str(caught.exception)
        self.assertIn("not signed in", message)
        self.assertNotIn("Desktop", message)
        # Web's state was asked, which is what the refusal is built from.
        self.web_state.assert_awaited()

    def test_a_number_send_refuses_the_same_way(self):
        # A number is not an excuse to open a second application: the lookup that
        # turns it into a chat goes through the account's own search box, which is
        # Web.
        with self.assertRaises(ToolError) as caught:
            run_tool("+15551234567", "hi")
        self.assertNotIn("Desktop", str(caught.exception))

    def test_nothing_is_typed_or_sent_anywhere(self):
        with self.assertRaises(ToolError):
            run_tool("Alice", "hello")
        self.approval.assert_not_awaited()

    def test_an_attachment_refuses_before_anything_is_opened(self):
        self.stack.enter_context(
            mock.patch.object(
                whatsapp, "resolve_attachment", return_value="C:/Users/me/Downloads/a.png"
            )
        )
        with self.assertRaises(ToolError):
            run_tool("Alice", "look", attachment="C:/Users/me/Downloads/a.png")
        self.approval.assert_not_awaited()


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------


class MessagingValidationTests(unittest.TestCase):
    @staticmethod
    def _codes(document):
        return {diag.code for diag in validate_document(document)}

    def test_the_modes_are_written_down_once_each_and_pinned_together(self):
        # validation.py and messaging/whatsapp.py both name the modes --
        # deliberately, the same way the permission levels are declared
        # twice. This is the test that stops them drifting.
        self.assertEqual(set(WHATSAPP_MODES), set(whatsapp.MODES))

    def test_an_unknown_mode_is_a_warning_that_names_the_alternatives(self):
        document = {"version": CONFIG_VERSION, "messaging": {"whatsapp": {"mode": "banana"}}}
        diagnostics = [d for d in validate_document(document) if d.code.startswith("messaging")]
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].severity, "warn")
        self.assertEqual(diagnostics[0].code, "messaging.whatsapp_unknown_mode")
        self.assertIn("deeplink", diagnostics[0].hint)

    def test_a_valid_mode_is_silent(self):
        for mode in whatsapp.MODES:
            with self.subTest(mode=mode):
                document = {"version": CONFIG_VERSION, "messaging": {"whatsapp": {"mode": mode}}}
                self.assertEqual(self._codes(document) & {"messaging.invalid", "messaging.whatsapp_invalid", "messaging.whatsapp_unknown_mode"}, set())

    def test_a_malformed_section_is_named_rather_than_ignored(self):
        self.assertIn("messaging.invalid", self._codes({"messaging": "on"}))
        self.assertIn("messaging.whatsapp_invalid", self._codes({"messaging": {"whatsapp": "on"}}))

    def test_a_document_without_the_section_is_fine(self):
        self.assertEqual(self._codes({"version": CONFIG_VERSION}) & {"messaging.invalid", "messaging.whatsapp_invalid", "messaging.whatsapp_unknown_mode"}, set())


class OneConfirmationTests(_SendHarness):
    """Exactly one thing reports that a message was sent.

    The send step used to complete with ``verified="the message is in the
    conversation"``, and the tool then returned ``"Sent to X on WhatsApp."`` --
    which is the line the model *speaks* and the line the toast renders. Two
    confirmations of one send, a fraction of a second apart. The user's words were
    "she confirms the send message state two times after sending a message".

    The verdict belongs to the return value and nowhere else. It reaches the model,
    the voice and the toast in one hop, and the refusal paths reach all three the
    same way; a progress step that also renders the verdict is a second opinion
    that can disagree with the first.

    Inherits :class:`_SendHarness` rather than repeating its setup, and rather than
    inheriting :class:`SendSequenceTests` -- which would re-run that class's ten
    ordering tests here, so every one of them would exist twice.
    """

    def setUp(self):
        super().setUp()
        self.records: list[dict] = []
        self.stack.enter_context(
            mock.patch.object(
                whatsapp, "progress_step", new=_record_step(self.records)
            )
        )

    def test_the_step_does_not_render_the_verdict(self):
        result = run_tool("Alice", "hello")
        self.assertIn("Sent to Alice", result)
        carrying = [
            record
            for record in self.records
            if any("verif" in str(key).lower() for key in record)
        ]
        self.assertEqual(
            carrying,
            [],
            "a step record is carrying the outcome as well as the result line",
        )

    def test_the_send_step_still_exists_so_the_progress_is_visible(self):
        # Removing the *outcome* must not remove the *progress* -- "see every step
        # happen" is the requirement. The steps up to the question are all still
        # there; only the send's own line is gone, because that line was the second
        # statement of the outcome.
        run_tool("Alice", "hello")
        actions = [record["action"] for record in self.records]
        for expected in ("open", "confirm"):
            self.assertIn(expected, actions)

    def test_nothing_but_the_return_value_claims_the_message_went(self):
        """The property that actually fixes the voice.

        Counting *step records with a verdict key* was the first attempt and it was
        wrong twice over: it missed the step's plain **message**, and it only looked
        at one surface. The real rule is about the turn as the model sees it --
        every step line plus the return value -- and the question is how many of
        those say the message was sent.

        This is what the user hears twice. A step line is not a private note: it
        reaches the model, and the model is what speaks.
        """
        result = run_tool("Alice", "hello")
        said_sent = [
            line
            for line in [record["message"] for record in self.records] + [result]
            if "sent" in line.casefold() or "in the conversation" in line.casefold()
        ]
        self.assertEqual(
            len(said_sent),
            1,
            f"the outcome is stated {len(said_sent)} times: {said_sent}",
        )
        self.assertIn("Sent to Alice", said_sent[0])

    def test_the_confirmation_is_spoken_by_the_product_and_told_to_the_model(self):
        """One speaker, not one fact.

        Both spoken lines the user heard came from the *model*: one because a step
        line said the message was going out, one because the return value said it
        had gone. Asking a model to report on a turn containing two facts about one
        event produces two sentences, and voice cannot be scrolled past.

        So the confirmation is spoken from code, once delivery is observed, and the
        result tells the model it has been said. The two halves matter separately:
        without the announcement there is no confirmation on a profile that cannot
        speak, and without the note the model adds a second one on a profile that
        can.
        """
        spoken: list[str] = []
        self.stack.enter_context(
            mock.patch.object(
                whatsapp.web,
                "announce_sent",
                new=mock.AsyncMock(side_effect=lambda _ctx, who: spoken.append(who) or SAID),
            )
        )
        result = run_tool("Alice", "hello")
        self.assertEqual(spoken, ["Alice"], "the confirmation was not spoken")
        self.assertIn("already said this out loud", result)

    def test_a_profile_that_cannot_speak_told_to_say_it_once(self):
        # `gemini-3.8-live` and the other native-audio profiles raise on `say()`.
        # The model's own sentence is then the only voice there is, so the result
        # says so -- and says *once*, which is the whole defect.
        self.stack.enter_context(
            mock.patch.object(
                whatsapp.web,
                "announce_sent",
                new=mock.AsyncMock(return_value=SILENT),
            )
        )
        result = run_tool("Alice", "hello")
        self.assertIn("Say this once", result)
        self.assertNotIn("already said this out loud", result)

    def test_the_step_list_ends_at_the_question_and_not_past_it(self):
        # "See every step happen" is the requirement, and it is met: opening,
        # resolving, opening the chat, drafting and asking are all still narrated.
        # What is gone is the send's own line, because the answer to the question
        # *is* the send and the product speaks it.
        run_tool("Alice", "hello")
        actions = [record["action"] for record in self.records]
        self.assertEqual(actions[-1], "confirm")
        # The count and the list agree, so the "step 5 of N" the UI renders cannot
        # drift from the number of steps that actually fire.
        self.assertEqual(whatsapp.SEND_STEPS, len(actions))
        self.assertEqual(
            [record.get("index") for record in self.records],
            list(range(1, len(actions) + 1)),
            "the steps are numbered 1..N with no gap",
        )

    def test_an_unconfirmed_send_says_so_exactly_once_too(self):
        # The other direction. When the observation is not ours to trust the tool
        # raises, and there is no step *and* no result claiming it went -- the
        # refusal is the single statement, and that is the sentence that has to be
        # believed.
        self.use_backend(delivery_seen=_stale)
        with self.assertRaises(ToolError):
            run_tool("Alice", "hello")
        self.assertFalse(
            [
                record
                for record in self.records
                if any("verif" in str(key).lower() for key in record)
            ]
        )


class DuplicateSendIsSilentTests(_SendHarness):
    """A second call for one send is refused, and says nothing.

    The report was two confirmations of one send, in voice:

        LUMINE  I have sent that message to Dipto for you.
        LUMINE  I already sent that exact message a moment ago, so I'm not
                sending it again.

    The second sentence is not a second confirmation -- it is the **dedup ledger**
    refusing, which means `send_whatsapp` was reached twice for one request. The
    first call confirms; the second is a normal non-empty return, so the model
    answers it out loud. Two statements about one send, on the channel that
    cannot be scrolled past.

    Nothing in this repository re-invokes the tool: there is no retry wrapper
    around a gated call, and the `sent` marker is written only after delivery was
    observed. So the second call is the realtime model re-issuing a function call
    it has already had answered, and the safety net is what has to be silent.

    Both halves are load-bearing. Drop the refusal and the message really is sent
    twice; let the refusal speak and the user hears about one send twice.
    """

    def test_the_refusal_never_reaches_the_voice(self):
        first = run_tool("Alice", "hello")
        self.assertIn("Sent to Alice", first)
        second = run_tool("Alice", "hello")
        # No confirmation, and nothing that invites the model to speak about the
        # send a second time. Asserted on the two things that would make it talk,
        # rather than on the absence of a word -- the string still explains itself,
        # because it is also the toast a person reads.
        self.assertNotIn("Sent to Alice", second)
        self.assertNotIn("Say this once", second)
        self.assertIn("nothing further to say", second)

    def test_the_refusal_is_still_a_refusal(self):
        # Silence is a property of the *announcement*, not a relaxation of the
        # guard. If the second call ever reached the path, `press_send` would
        # appear twice in the order and Alice would get the message twice.
        run_tool("Alice", "hello")
        run_tool("Alice", "hello")
        self.assertEqual(self.order.count("send"), 1, "the message went out twice")
        self.assertEqual(len(self.sends_asked_for()), 1, "the user was asked twice")

    def test_a_genuinely_different_message_is_still_answered(self):
        # The window is a guard on *this* send, not a mute. A different message is
        # not a duplicate, so it is confirmed in the ordinary way -- otherwise the
        # fix would cost a real user their confirmation.
        run_tool("Alice", "hello")
        second = run_tool("Alice", "something else")
        self.assertIn("Sent to Alice", second)
        self.assertEqual(self.order.count("send"), 2)

    def test_an_uncertain_send_still_speaks(self):
        # The one duplicate that must NOT be silent. "I could not confirm it went"
        # is a safety warning about a message that may or may not exist in the
        # other person's chat, and swallowing it is the failure this class exists
        # to prevent.
        self.use_backend(
            delivery_seen=_answer(False)
        )
        with self.assertRaises(ToolError):
            run_tool("Alice", "hello")
        with self.assertRaises(ToolError) as second:
            run_tool("Alice", "hello")
        self.assertIn("could not be confirmed", str(second.exception))


class AnnounceSentTests(unittest.TestCase):
    """The send confirmation is never spoken by way of the model.

    The reported defect, exactly: `announce_sent` runs inside the tool one
    statement before the tool returns, and returning a result is what makes the
    model speak -- LiveKit regenerates for any non-empty return, and a realtime
    model with server-side turn detection replies whether or not a reply was
    asked for. Escalating to the model there does not replace that reply; it
    precedes it, so one send produces two sentences and the user hears "the
    message has been sent" twice. `allow_model=False` is the guard, and the two
    directions are pinned here: a profile with no synthesiser stays silent and
    lets the model's single reply stand, and one with a synthesiser speaks it
    verbatim -- neither reaches for a second speaker.
    """

    class _Session:
        """A session whose only channel is the one the test asks to exist."""

        def __init__(self, *, tts: bool = False) -> None:
            self._tts = tts
            self.said: list[tuple[str, dict]] = []
            self.instructions: list[str] = []

        def say(self, text: str, **kwargs) -> None:
            if not self._tts:
                raise RuntimeError("no TTS model or RealtimeSession")
            self.said.append((text, kwargs))

        def generate_reply(self, *, instructions: str = "", **_kwargs) -> None:
            self.instructions.append(instructions)

    def test_a_realtime_profile_is_left_to_speak_for_itself(self):
        session = self._Session()
        channel = asyncio.run(
            web.announce_sent(SimpleNamespace(session=session), "Alice")
        )
        self.assertEqual(channel, SILENT)
        self.assertEqual(
            session.instructions,
            [],
            "the model was asked to speak a confirmation it was already going to give",
        )

    def test_a_tts_profile_speaks_it_verbatim_and_not_through_the_model(self):
        session = self._Session(tts=True)
        channel = asyncio.run(
            web.announce_sent(SimpleNamespace(session=session), "Alice")
        )
        self.assertEqual(channel, SAID)
        self.assertEqual(session.said[0][0], "The message has been sent to Alice.")
        self.assertEqual(session.instructions, [])


def _record_step(sink: list[dict]):
    """A `progress_step` stand-in that records what each step was told.

    Yields a dict, because that is what the real one does and the send step used
    to write its verdict into it -- which is the whole of what this file is about.
    """

    @contextlib.contextmanager
    def _step(_context, _tool, action, message, **kwargs):
        record: dict = {
            "action": action,
            "message": message,
            "index": kwargs.get("index"),
            "total": kwargs.get("total"),
        }
        sink.append(record)
        yield record

    return _step


if __name__ == "__main__":
    unittest.main()
