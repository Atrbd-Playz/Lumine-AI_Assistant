"""Contact search, chat selection, and the draft-first approval binding.

The reported WhatsApp failures were two, and both are decisions this file covers:

* **"find Afia" could not find Afia.** The old path searched only the *rendered*
  chat list, which is the last few dozen conversations and nothing else. These
  tests pin the search-then-read behaviour and the ambiguity rule that stops
  "message Afia" landing in "Afia, Rahim and 3 others".
* **"she asked for a phone number when I gave a name."** Nothing here needs a
  number: every name comes from the interface the user's own account shows, and
  the tests assert a name is enough.

No WhatsApp, no UIA, no browser. The backends are recorded doubles, which is the
right fixture here: the flow's job is *ordering and refusal*, and an ordering bug
does not care whether the click landed.
"""

from __future__ import annotations

import asyncio
import unittest

from agent.tools.messaging.whatsapp_flow import (
    ChatCandidate,
    ContactFlow,
    DraftApproval,
    DraftState,
    FlowError,
    choose,
    is_group,
    match,
    normalize_title,
)

CONTACTS = [
    ChatCandidate("Afia Rahman"),
    ChatCandidate("Afia Chowdhury"),
    ChatCandidate("Afia, Rahim and 3 others", kind="group", participants=("Afia", "Rahim", "Toma", "Nayeem")),
    ChatCandidate("Rahim Uddin"),
]


# -- candidates -----------------------------------------------------------------


class MatchTests(unittest.TestCase):
    def test_a_partial_name_finds_a_longer_one(self):
        self.assertEqual([c.title for c in match("afia", CONTACTS)][:2], ["Afia Rahman", "Afia Chowdhury"])

    def test_an_exact_name_comes_first(self):
        found = match("Rahim Uddin", CONTACTS)
        self.assertEqual(found[0].title, "Rahim Uddin")

    def test_whitespace_is_collapsed_before_matching(self):
        self.assertTrue(match("  afia   rahman  ", CONTACTS))

    def test_nothing_matching_is_an_empty_list(self):
        self.assertEqual(match("Zainab", CONTACTS), [])


class GroupTests(unittest.TestCase):
    def test_participants_are_what_mark_a_group(self):
        # WhatsApp does not prefix group names, so the name cannot be trusted to
        # tell us. The adapter reports participants, and this is the whole use.
        self.assertTrue(is_group("Team", participants=("A", "B")))
        self.assertFalse(is_group("Team", participants=("Team",)))

    def test_a_candidate_carries_its_kind(self):
        group = next(c for c in CONTACTS if c.kind == "group")
        self.assertTrue(group.is_group)
        self.assertFalse(CONTACTS[0].is_group)


class ChooseTests(unittest.TestCase):
    def test_an_exact_single_match_is_chosen_without_a_question(self):
        choice = choose("Rahim Uddin", CONTACTS)
        self.assertTrue(choice.ok)
        self.assertEqual(choice.chosen.title, "Rahim Uddin")

    def test_several_partial_matches_refuse_rather_than_guessing(self):
        choice = choose("Afia", CONTACTS)
        self.assertFalse(choice.ok)
        self.assertTrue(choice.ambiguous)
        self.assertEqual(len(choice.candidates), 3)

    def test_a_group_is_never_auto_selected_for_a_person(self):
        # "Message Afia" landing in "Afia, Rahim and 3 others" because WhatsApp
        # listed that row first is the failure this rule exists for.
        choice = choose("Afia, Rahim and 3 others", CONTACTS)
        self.assertFalse(choice.ok)
        self.assertTrue(choice.ambiguous)
        self.assertIn("group", choice.reason)

    def test_a_group_may_be_chosen_when_the_user_means_the_group(self):
        choice = choose("Afia, Rahim and 3 others", CONTACTS, allow_group=True)
        self.assertTrue(choice.ok)

    def test_two_chats_with_the_same_exact_name_still_ask(self):
        # Two accounts with the same display name is a real thing, and picking one
        # is a coin flip the user should make.
        contacts = [ChatCandidate("Afia"), ChatCandidate("Afia")]
        choice = choose("Afia", contacts)
        self.assertFalse(choice.ok)
        self.assertIn("exact name", choice.reason)

    def test_nothing_matching_says_so_plainly(self):
        choice = choose("Zainab", CONTACTS)
        self.assertFalse(choice.ok)
        self.assertIn("couldn't find", choice.reason)

    def test_an_empty_query_asks_who(self):
        choice = choose("", CONTACTS)
        self.assertFalse(choice.ok)
        self.assertIn("who", choice.reason)

    def test_the_payload_is_what_a_caller_can_act_on(self):
        payload = choose("Afia", CONTACTS).to_dict()
        self.assertFalse(payload["resolved"])
        self.assertTrue(payload["ambiguous"])
        self.assertIn("Afia Rahman", payload["candidates"])


# -- the flow --------------------------------------------------------------------


class RecordingBackend:
    """A WhatsApp stand-in that records the order of every operation."""

    def __init__(
        self,
        contacts=CONTACTS,
        active: str = "Afia Rahman",
        open_ok: bool = True,
        opens: str = "",
    ):
        self.contacts = list(contacts)
        self.active = active
        self.open_ok = open_ok
        #: What a click on any row actually opens. Empty means "the row that was
        #: clicked", which is the honest default; set it to simulate the app
        #: opening something else -- a reconnect, a notification, a moved row --
        #: which is exactly the case the post-open read-back exists to catch.
        self.opens = opens
        self.calls: list[str] = []
        self.typed = ""

    async def search(self, name: str) -> list[ChatCandidate]:
        self.calls.append(f"search:{name}")
        return match(name, self.contacts)

    async def open_chat(self, title: str) -> bool:
        self.calls.append(f"open:{title}")
        if not self.open_ok:
            return False
        self.active = self.opens or title
        return True

    async def active_chat(self) -> str:
        self.calls.append("active")
        return self.active

    async def type_message(self, text: str) -> None:
        self.calls.append("type")
        self.typed = text

    async def press_send(self) -> None:
        self.calls.append("send")

    def flow(self) -> ContactFlow:
        return ContactFlow(
            search=self.search,
            open_chat=self.open_chat,
            active_chat=self.active_chat,
            type_message=self.type_message,
            press_send=self.press_send,
        )


class FindTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_name_is_enough_to_search(self):
        # The reported bug: a phone number used to be required. Nothing here knows
        # what a number is.
        backend = RecordingBackend()
        choice = await backend.flow().find("Afia")
        self.assertEqual(backend.calls, ["search:Afia"])
        self.assertFalse(choice.ok)

    async def test_a_unique_name_resolves(self):
        backend = RecordingBackend()
        choice = await backend.flow().find("Rahim Uddin")
        self.assertTrue(choice.ok)


class OpenAndVerifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_active_chat_is_re_read_after_opening(self):
        backend = RecordingBackend()
        await backend.flow().open_and_verify("Rahim Uddin")
        self.assertEqual(backend.calls, ["search:Rahim Uddin", "open:Rahim Uddin", "active"])

    async def test_a_chat_that_opened_something_else_is_refused(self):
        # The click landed; a *different* chat is open. Sending here would message
        # whoever is actually on screen.
        backend = RecordingBackend(opens="Afia Chowdhury")
        with self.assertRaises(FlowError) as caught:
            await backend.flow().open_and_verify("Afia Rahman")
        self.assertIn("showing", str(caught.exception))

    async def test_a_chat_that_refuses_to_open_is_refused(self):
        backend = RecordingBackend(open_ok=False)
        with self.assertRaises(FlowError):
            await backend.flow().open_and_verify("Rahim Uddin")

    async def test_an_unfound_name_refuses_before_opening_anything(self):
        backend = RecordingBackend()
        with self.assertRaises(FlowError):
            await backend.flow().open_and_verify("Zainab")
        self.assertEqual(backend.calls, ["search:Zainab"])


class DraftFirstTests(unittest.IsolatedAsyncioTestCase):
    def _state(self, recipient="Afia Rahman", message="I'll be there in 20 minutes") -> DraftState:
        state = DraftState()
        state.prepare(recipient, message, recipient, "req-1")
        return state

    def test_an_unapproved_draft_may_not_be_sent(self):
        self.assertIn("never approved", self._state().may_send())

    def test_approval_is_bound_to_the_exact_recipient_and_text(self):
        state = self._state()
        state.approve(
            DraftApproval(
                recipient="Afia Rahman",
                message="I'll be there in 20 minutes",
                chat_identity="Afia Rahman",
                request_id="req-1",
            )
        )
        self.assertEqual(state.may_send(), "")

    def test_changing_the_message_invalidates_the_approval(self):
        state = self._state()
        state.approve(
            DraftApproval("Afia Rahman", "I'll be there in 20 minutes", "Afia Rahman", "req-1")
        )
        state.message = "I'll be there in 5 minutes"
        self.assertIn("message changed", state.may_send())

    def test_changing_the_recipient_invalidates_the_approval(self):
        state = self._state()
        state.approve(
            DraftApproval("Afia Rahman", "I'll be there in 20 minutes", "Afia Rahman", "req-1")
        )
        state.recipient = "Someone Else"
        self.assertIn("recipient changed", state.may_send())

    def test_changing_the_open_chat_invalidates_the_approval(self):
        state = self._state()
        state.approve(
            DraftApproval("Afia Rahman", "I'll be there in 20 minutes", "Afia Rahman", "req-1")
        )
        state.chat_identity = "Afia Chowdhury"
        self.assertIn("active chat", state.may_send())

    def test_preparing_a_new_draft_drops_the_previous_approval(self):
        state = self._state()
        state.approve(
            DraftApproval("Afia Rahman", "I'll be there in 20 minutes", "Afia Rahman", "req-1")
        )
        state.prepare("Afia Chowdhury", "hello", "Afia Chowdhury", "req-2")
        self.assertIsNone(state.approval)

    def test_an_approval_for_another_recipient_is_not_stored(self):
        state = self._state()
        state.approve(DraftApproval("Someone Else", "I'll be there in 20 minutes", "Afia Rahman", "req-9"))
        self.assertIsNone(state.approval)

    def test_an_expired_approval_is_refused(self):
        state = self._state()
        state.approve(
            DraftApproval("Afia Rahman", "I'll be there in 20 minutes", "Afia Rahman", "req-1", expires_at=1.0)
        )
        self.assertIn("expired", state.may_send(now=2.0))

    def test_clearing_removes_everything(self):
        state = self._state()
        state.clear()
        self.assertEqual(state.recipient, "")
        self.assertIsNone(state.approval)


class SendTests(unittest.IsolatedAsyncioTestCase):
    def _approved(
        self, backend: RecordingBackend, chat: str = "Afia Rahman"
    ) -> DraftState:
        """A draft approved for `chat`, which is *not* whatever is open now.

        Separating the approved chat from the open one is the point: the send test
        is "approval said Afia Rahman, WhatsApp is showing somebody else", and a
        helper that derived the draft from the open chat could never express it.
        """
        state = DraftState()
        state.prepare(chat, "I'll be there in 20 minutes", chat, "req-1")
        state.approve(
            DraftApproval(chat, "I'll be there in 20 minutes", chat, "req-1")
        )
        return state

    async def test_an_approved_send_presses_send_after_re_verifying(self):
        backend = RecordingBackend()
        await backend.flow().send(self._approved(backend))
        self.assertEqual(backend.calls, ["active", "send"])

    async def test_an_unapproved_draft_never_presses_send(self):
        backend = RecordingBackend()
        state = DraftState()
        state.prepare("Afia Rahman", "hello", "Afia Rahman", "req-1")
        with self.assertRaises(FlowError):
            await backend.flow().send(state)
        self.assertNotIn("send", backend.calls)

    async def test_a_chat_that_changed_between_approval_and_send_is_refused(self):
        backend = RecordingBackend(active="Afia Chowdhury")
        with self.assertRaises(FlowError) as caught:
            await backend.flow().send(self._approved(backend))
        self.assertNotIn("send", backend.calls)
        self.assertIn("showing", str(caught.exception))

    async def test_a_failure_at_the_send_button_is_uncertain_not_a_clean_failure(self):
        # The distinction that stops a blind retry: something may have gone out.
        backend = RecordingBackend()

        async def boom() -> None:
            backend.calls.append("send")
            raise FlowError("the window closed")

        backend.press_send = boom
        with self.assertRaises(FlowError) as caught:
            await backend.flow().send(self._approved(backend))
        self.assertTrue(caught.exception.uncertain)

    async def test_an_unexpected_error_at_the_send_button_is_also_uncertain(self):
        backend = RecordingBackend()

        async def boom() -> None:
            raise RuntimeError("comtypes blew up")

        backend.press_send = boom
        with self.assertRaises(FlowError) as caught:
            await backend.flow().send(self._approved(backend))
        self.assertTrue(caught.exception.uncertain)

    async def test_typing_happens_only_after_the_chat_is_verified(self):
        backend = RecordingBackend(active="")
        with self.assertRaises(FlowError):
            await backend.flow().type_into_open_chat("hello")
        self.assertEqual(backend.typed, "")

    async def test_typing_reaches_the_composer_after_re_reading_the_chat(self):
        backend = RecordingBackend()
        await backend.flow().type_into_open_chat("hello")
        self.assertEqual(backend.calls, ["active", "type"])
        self.assertEqual(backend.typed, "hello")


class OrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_full_order_is_resolve_then_verify_then_type(self):
        backend = RecordingBackend()
        flow = backend.flow()
        await flow.open_and_verify("Rahim Uddin")
        await flow.type_into_open_chat("hello")
        self.assertEqual(
            backend.calls,
            ["search:Rahim Uddin", "open:Rahim Uddin", "active", "active", "type"],
        )


if __name__ == "__main__":
    unittest.main()