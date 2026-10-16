"""Approvals, and why none of them can be replayed.

Three claims are pinned here, in this order because each one depends on the
last:

1. **The floor is in the code.** Reading and ordinary acting are automatic,
   sensitive and destructive actions ask, and no policy value -- including a
   literal ``auto`` -- can lower that. A tool's class is a literal at its call
   site; the model never touches it.
2. **An approval belongs to one session, once.** Ids are random and
   single-use, the pending set is keyed by the session, answers from agents or
   unknown senders are ignored, and everything refuses on expiry, on session
   close, and when nobody is in the room.
3. **Voice consent is a transcript event.** Only a *final*
   ``user_input_transcribed`` that matches the approval vocabulary decides
   anything; ambiguous speech decides nothing, and non-final text decides
   nothing.

Everything runs against fake rooms and sessions, so no test here joins a
LiveKit room, starts a program, or needs a provider key. The audit log is
redirected to a temp file: tests must not write into the user's real one.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from unittest.mock import patch

from agent.runtime import permission_gate
from agent.runtime import audit_log
from agent.runtime.permission_gate import (
    ACTION_CLASSES,
    CLASS_FLOOR,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    MSG_CANCELLED,
    MSG_DENIED,
    MSG_DUPLICATE,
    MSG_EXPIRED,
    MSG_NO_GATE,
    MSG_NO_USER,
    MSG_OFF,
    NOT_DECISIONS,
    TOPIC,
    VALID_LEVELS,
    VOICE_APPROVAL_VAR,
    PermissionGate,
    PermissionRequest,
    acquire_gate,
    clamp_timeout,
    effective_handling,
    env_permission_levels,
    gate_for,
    is_decision,
    load_policy,
    match_voice,
    normalize_level,
    release_gate,
    require_approval,
)
from agent.settings import validation
from agent.tools.tools_compat import ToolError
from agent.tools.tools_permission import TOOL_ACTION_CLASS, permission_floor


# ---------------------------------------------------------------------------
# Fakes -- a room, a session, a publisher, nothing real
# ---------------------------------------------------------------------------


class FakeParticipant:
    def __init__(self, kind: str = "user", identity: str = "user-1") -> None:
        self.kind = kind
        self.identity = identity


class FakeLocalParticipant:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.attempts: list[tuple[dict, str]] = []

    async def publish_data(self, data, *, reliable=True, topic=""):
        # The attempt is recorded before the failure: a card the worker tried
        # to send and could not is a different fact from a card never sent.
        record = json.loads(data)
        self.attempts.append((record, topic))
        if self.fail:
            raise RuntimeError("the data channel is gone")
        return None


class FakeRoom:
    def __init__(self, *, users=True, publish_fails=False) -> None:
        self.name = "room-test"
        self.remote_participants = {"user-1": FakeParticipant()} if users else {}
        self.local_participant = FakeLocalParticipant(fail=publish_fails)


class FakeSession:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

        def unsubscribe():
            try:
                self.handlers.get(event, []).remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def emit(self, event, ev) -> None:
        for callback in list(self.handlers.get(event, [])):
            callback(ev)


class FakePublisher:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def emit_record(self, record):
        self.records.append(dict(record))
        return record


class AuditFileMixin:
    """Point the audit log at a temp file for the duration of one test."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.audit_file = Path(self._tmp.name) / "audit.jsonl"
        patcher = mock.patch.dict(
            os.environ,
            {
                audit_log.AUDIT_PATH_ENV: str(self.audit_file),
                permission_gate.PERMISSIONS_VAR: "",
                permission_gate.PERMISSION_TIMEOUT_VAR: "",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def audit_lines(self) -> list[dict]:
        if not self.audit_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.audit_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def audit_events(self, event: str) -> list[dict]:
        return [entry for entry in self.audit_lines() if entry.get("event") == event]


def make_gate(*, users=True, publish_fails=False, policy=None, timeout=5.0):
    session = FakeSession()
    room = FakeRoom(users=users, publish_fails=publish_fails)
    publisher = FakePublisher()
    gate = PermissionGate(
        session,
        room=room,
        publisher=publisher,
        policy=policy or {},
        timeout=timeout,
        label="room-test",
    )
    return gate, session, room, publisher


def transcript(text: str, *, is_final: bool = True) -> SimpleNamespace:
    return SimpleNamespace(transcript=text, is_final=is_final, speaker_id=None)


def response(request_id: str, decision: str) -> bytes:
    return json.dumps(
        {"type": "permission_response", "id": request_id, "decision": decision}
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. The floor
# ---------------------------------------------------------------------------


class PermissionFloorTests(unittest.TestCase):
    def test_read_and_act_are_automatic_by_default(self):
        self.assertEqual(effective_handling("read"), "auto")
        self.assertEqual(effective_handling("act"), "auto")

    def test_sensitive_and_destructive_always_ask(self):
        for action_class in ("sensitive", "destructive"):
            with self.subTest(action_class=action_class):
                self.assertEqual(effective_handling(action_class), "ask")

    def test_no_policy_value_lowers_the_floor(self):
        """The whole point: `auto` in a settings file means *the default*,
        and the default for a sensitive action is a question."""
        for level in ("default", "auto", "", None, "whatever"):
            with self.subTest(level=level):
                self.assertEqual(effective_handling("sensitive", level), "ask")
                self.assertEqual(effective_handling("destructive", level), "ask")

    def test_policy_can_tighten_but_not_loosen(self):
        self.assertEqual(effective_handling("read", "ask"), "ask")
        self.assertEqual(effective_handling("read", "off"), "off")
        self.assertEqual(effective_handling("act", "off"), "off")
        # ...and never the other way: a level never turns a floor down.
        self.assertEqual(effective_handling("sensitive", "off"), "off")

    def test_an_unknown_action_class_asks_rather_than_allows(self):
        # Mislabelling something milder is the dangerous direction, so an
        # unknown class lands where the consequences of guessing wrong are
        # one extra question.
        self.assertEqual(effective_handling("banana"), "ask")

    def test_every_declared_class_has_a_floor(self):
        for action_class in ACTION_CLASSES:
            with self.subTest(action_class=action_class):
                self.assertIn(action_class, CLASS_FLOOR)

    def test_the_card_floor_agrees_with_the_class_map(self):
        for ident, action_class in TOOL_ACTION_CLASS.items():
            with self.subTest(tool=ident):
                self.assertEqual(
                    permission_floor(ident),
                    effective_handling(action_class, "default"),
                )

    def test_open_app_is_an_ordinary_act_at_default_policy(self):
        # A behaviour change here would silently start interrogating every
        # existing "open notepad" flow.
        self.assertEqual(permission_floor("open_app"), "auto")

    def test_the_settings_validator_mirrors_the_gate(self):
        # Two copies of one vocabulary, because importing the gate's copy into
        # settings/validation.py would be circular. The copies are pinned
        # together here instead of trusted.
        self.assertEqual(validation.PERMISSION_LEVELS, tuple(VALID_LEVELS))
        self.assertEqual(
            validation.PERMISSION_TIMEOUT_RANGE,
            (MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS),
        )
        self.assertEqual(clamp_timeout(None), DEFAULT_TIMEOUT_SECONDS)

    def test_the_default_wait_is_a_minute(self):
        # Half a minute lost a real person's decision to a glance away from
        # the screen. A minute is still bounded -- the card counts down either
        # way, so the deadline is legible rather than guessed at -- and it
        # sits inside the range both the gate and the validator clamp to.
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, 60.0)
        self.assertLessEqual(MIN_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)
        self.assertGreaterEqual(MAX_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(clamp_timeout(None), 60.0)


# ---------------------------------------------------------------------------
# 2. Policy loading: env base, document on top, fail closed
# ---------------------------------------------------------------------------


class PolicyLoadingTests(unittest.TestCase):
    def test_an_unusable_level_fails_closed_to_ask(self):
        self.assertEqual(normalize_level("ask"), "ask")
        self.assertEqual(normalize_level("off"), "off")
        self.assertEqual(normalize_level("auto"), "default")
        self.assertEqual(normalize_level("banana"), "ask")
        self.assertEqual(normalize_level(None), "ask")

    def test_env_levels_parse_as_pairs(self):
        with mock.patch.dict(os.environ, {permission_gate.PERMISSIONS_VAR: "Open_App=ask, browse = off , mystery"}):
            self.assertEqual(
                env_permission_levels(),
                {"open_app": "ask", "browse": "off", "mystery": "ask"},
            )

    def test_a_document_overrides_the_environment_per_key(self):
        with mock.patch.dict(os.environ, {permission_gate.PERMISSIONS_VAR: "open_app=off,browse=off"}):
            levels, _timeout = load_policy(
                {"permissions": {"tools": {"open_app": "default", "browse": "ask"}}}
            )
        # The saved document is the newer intent, key by key; the rest of the
        # environment survives underneath it.
        self.assertEqual(levels["open_app"], "default")
        self.assertEqual(levels["browse"], "ask")
        self.assertEqual(levels.get("mystery"), None)

    def test_a_document_without_a_permissions_section_changes_nothing(self):
        with mock.patch.dict(os.environ, {permission_gate.PERMISSIONS_VAR: "open_app=ask"}):
            levels, _timeout = load_policy({"profiles": []})
        self.assertEqual(levels, {"open_app": "ask"})

    def test_the_timeout_is_clamped_on_both_paths(self):
        with mock.patch.dict(os.environ, {permission_gate.PERMISSION_TIMEOUT_VAR: "45"}):
            _levels, timeout = load_policy({})
            self.assertEqual(timeout, 45.0)
        _levels, timeout = load_policy({"permissions": {"timeout": 999}})
        self.assertEqual(timeout, MAX_TIMEOUT_SECONDS)
        with mock.patch.dict(os.environ, {permission_gate.PERMISSION_TIMEOUT_VAR: "later"}):
            _levels, timeout = load_policy({})
            self.assertEqual(timeout, DEFAULT_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# 3. Voice consent vocabulary
# ---------------------------------------------------------------------------


class VoiceConsentTests(unittest.TestCase):
    def test_plain_approvals_match(self):
        for text in (
            "Yes",
            "yes.",
            "Yeah!",
            "Sure",
            "ok",
            "Go ahead",
            "do it",
            "yes please",
            "Yes, do it",
            "of course",
            "Absolutely",
        ):
            with self.subTest(text=text):
                self.assertEqual(match_voice(text), "approve")

    def test_plain_refusals_match(self):
        for text in (
            "No",
            "No way!",
            "no thanks",
            "stop",
            "Wait a moment",
            "don't do it",
            "Cancel that",
            "not now",
            "Never mind",
        ):
            with self.subTest(text=text):
                self.assertEqual(match_voice(text), "deny")

    def test_ambiguous_speech_decides_nothing(self):
        for text in ("", "hmm", "maybe later", "yes but tomorrow", "what do you think", "right..."):
            with self.subTest(text=text):
                self.assertIsNone(match_voice(text))

    def test_a_refusal_lead_wins_over_a_longer_sentence(self):
        # Refusing when the user was agreeing costs a question; approving
        # when they were refusing costs the action. Asymmetric on purpose.
        self.assertEqual(match_voice("no I don't want that"), "deny")
        self.assertEqual(match_voice("stop, think about it"), "deny")


# ---------------------------------------------------------------------------
# 4. The gate in a session
# ---------------------------------------------------------------------------


class GateApprovalTests(AuditFileMixin, unittest.IsolatedAsyncioTestCase):
    async def arm(self, gate: PermissionGate, **overrides):
        """Start a require() call and return the task once it is waiting."""
        kwargs = dict(
            tool="manage_window",
            action="close",
            action_class="sensitive",
            summary="Close the window Notepad",
            target="Notepad",
        )
        kwargs.update(overrides)
        task = asyncio.ensure_future(gate.require(**kwargs))
        await asyncio.sleep(0.02)
        return task

    async def test_a_sensitive_action_publishes_a_card_and_waits(self):
        gate, _session, room, publisher = make_gate()
        task = await self.arm(gate)
        self.assertFalse(task.done(), "the tool proceeded before anyone answered")

        attempts = room.local_participant.attempts
        self.assertEqual(len(attempts), 1)
        record, topic = attempts[0]
        self.assertEqual(topic, TOPIC)
        self.assertEqual(record["type"], "permission_request")
        self.assertEqual(record["action_class"], "sensitive")
        self.assertIn("Notepad", record["summary"])
        self.assertGreater(record["expires_at"], time.time())
        # The same record is echoed to stdout for diagnostics, through the
        # publisher's write-safe path.
        self.assertEqual(publisher.records[0]["id"], record["id"])

        gate.handle_channel_message(
            response(record["id"], "approve"),
            sender_kind="user",
            sender_identity="user-1",
        )
        await asyncio.wait_for(task, timeout=1)
        decisions = self.audit_events("decision")
        self.assertEqual(decisions[-1]["decision"], "approved")
        self.assertEqual(decisions[-1]["channel"], "ui")

    async def test_a_refusal_stops_the_tool_and_says_so(self):
        gate, _session, room, _publisher = make_gate()
        task = await self.arm(gate)
        request_id = room.local_participant.attempts[0][0]["id"]
        gate.handle_channel_message(
            response(request_id, "deny"), sender_kind="user", sender_identity="user-1"
        )
        with self.assertRaises(ToolError) as caught:
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual(str(caught.exception), MSG_DENIED)
        self.assertEqual(self.audit_events("decision")[-1]["decision"], "denied")

    async def test_a_replayed_answer_is_ignored(self):
        gate, _session, room, _publisher = make_gate()
        task = await self.arm(gate)
        request_id = room.local_participant.attempts[0][0]["id"]
        first = gate.handle_channel_message(
            response(request_id, "approve"), sender_kind="user", sender_identity="user-1"
        )
        await asyncio.wait_for(task, timeout=1)
        second = gate.handle_channel_message(
            response(request_id, "approve"), sender_kind="user", sender_identity="user-1"
        )
        self.assertTrue(first)
        self.assertFalse(second, "an answered id must not answer anything again")
        ignored = [entry for entry in self.audit_lines() if entry.get("event") == "ignored"]
        self.assertTrue(ignored, "the replay was not recorded")
        self.assertIn("already", ignored[-1]["detail"])

    async def test_an_answer_minted_for_another_session_is_not_an_answer(self):
        gate, _session, room, _publisher = make_gate()
        task = await self.arm(gate)
        request_id = room.local_participant.attempts[0][0]["id"]

        other_gate, _other_session, _other_room, _other_pub = make_gate()
        counted = other_gate.handle_channel_message(
            response(request_id, "approve"), sender_kind="user", sender_identity="user-1"
        )
        self.assertFalse(counted, "a foreign id resolved a local request")
        self.assertEqual(gate.pending_count, 1)
        self.assertFalse(task.done())

        # ...and the right session can still answer it.
        gate.handle_channel_message(
            response(request_id, "approve"), sender_kind="user", sender_identity="user-1"
        )
        await asyncio.wait_for(task, timeout=1)

    async def test_agents_and_unknown_senders_cannot_approve(self):
        for sender_kind in (permission_gate._AGENT_KIND, None):
            with self.subTest(sender_kind=sender_kind):
                gate, _session, room, _publisher = make_gate()
                task = await self.arm(gate)
                request_id = room.local_participant.attempts[0][0]["id"]
                counted = gate.handle_channel_message(
                    response(request_id, "approve"),
                    sender_kind=sender_kind,
                    sender_identity="Lumine",
                )
                self.assertFalse(counted)
                self.assertEqual(gate.pending_count, 1)
                gate.close("cleanup")
                with self.assertRaises(ToolError):
                    await task

    async def test_a_malformed_packet_decides_nothing(self):
        gate, _session, _room, _publisher = make_gate()
        task = await self.arm(gate)
        for payload in (b"not json", json.dumps({"type": "other"}).encode(), "nonsense"):
            self.assertFalse(
                gate.handle_channel_message(payload, sender_kind="user", sender_identity="user-1")
            )
        self.assertEqual(gate.pending_count, 1)
        gate.close("cleanup")
        with self.assertRaises(ToolError):
            await task

    async def test_silence_expires_to_a_refusal(self):
        gate, _session, room, _publisher = make_gate(timeout=0.2)
        task = await self.arm(gate)
        with self.assertRaises(ToolError) as caught:
            await asyncio.wait_for(task, timeout=2)
        self.assertEqual(str(caught.exception), MSG_EXPIRED)
        self.assertEqual(gate.pending_count, 0)
        decisions = self.audit_events("decision")
        self.assertEqual(decisions[-1]["decision"], "expired")

    async def test_an_answer_after_the_deadline_records_expired_not_approved(self):
        # The clock is re-checked when a decision arrives, so even an answer
        # that slips past the waiter's own timeout cannot approve. The request
        # is built by hand because the normal path removes it on expiry.
        gate, _session, _room, _publisher = make_gate()
        request = PermissionRequest(
            id="stale-token",
            tool="manage_window",
            action="close",
            action_class="sensitive",
            summary="Close the window Notepad",
            target="Notepad",
            fingerprint=audit_log.digest("Close the window Notepad", "Notepad"),
            created_at=time.time() - 60,
            expires_at=time.time() - 30,
            future=asyncio.get_running_loop().create_future(),
        )
        gate._pending[request.id] = request
        gate._order.append(request.id)

        counted = gate.resolve_ui("stale-token", "approve", sender="user-1")
        self.assertFalse(counted)
        self.assertEqual(request.state, "expired")
        self.assertEqual(self.audit_events("decision")[-1]["decision"], "expired")

    async def test_the_session_ending_refuses_everything_waiting(self):
        gate, session, _room, _publisher = make_gate()
        task = await self.arm(gate)
        gate.close("user left room")
        with self.assertRaises(ToolError) as caught:
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual(str(caught.exception), MSG_CANCELLED)
        self.assertEqual(gate.pending_count, 0)
        # The voice channel is torn down with the gate as well, so a later
        # transcript has nothing left to resolve.
        self.assertFalse(session.handlers.get("user_input_transcribed"))

    async def test_a_closed_gate_refuses_new_work(self):
        gate, _session, _room, _publisher = make_gate()
        gate.close("session released")
        with self.assertRaises(ToolError) as caught:
            await gate.require(
                tool="manage_window",
                action="close",
                action_class="sensitive",
                summary="Close the window Notepad",
                target="Notepad",
            )
        self.assertEqual(str(caught.exception), MSG_CANCELLED)
        self.assertEqual(len(self.audit_events("decision")), 1)

    async def test_a_room_with_nobody_in_it_refuses_without_a_card(self):
        gate, _session, room, _publisher = make_gate(users=False)
        with self.assertRaises(ToolError) as caught:
            await gate.require(
                tool="manage_window",
                action="close",
                action_class="sensitive",
                summary="Close the window Notepad",
                target="Notepad",
            )
        self.assertEqual(str(caught.exception), MSG_NO_USER)
        self.assertEqual(room.local_participant.attempts, [])

    async def test_a_failed_publish_still_fails_closed_but_voice_can_answer(self):
        # The card could not leave the machine. That does not immediately
        # refuse -- voice is an independent consent channel -- but the wait
        # still has a deadline, so a room where nothing arrives refuses.
        gate, _session, room, _publisher = make_gate(publish_fails=True, timeout=0.3)
        task = await self.arm(gate)
        self.assertEqual(len(room.local_participant.attempts), 1)
        self.assertFalse(task.done())
        gate._on_user_transcript(transcript("yes"))
        await asyncio.wait_for(task, timeout=1)

    async def test_policy_can_only_tighten_never_loosen(self):
        gate, _session, room, _publisher = make_gate(
            policy={"send_whatsapp": "auto", "manage_window": "default", "get_weather": "ask"}
        )
        task = await self.arm(gate)  # sensitive at default policy -> still asks
        self.assertEqual(len(room.local_participant.attempts), 1)
        gate.close("cleanup")
        with self.assertRaises(ToolError):
            await task

        read_gate, _s2, room2, _p2 = make_gate(policy={"get_weather": "ask"})
        task2 = asyncio.ensure_future(
            read_gate.require(
                tool="get_weather",
                action="lookup",
                action_class="read",
                summary="Weather for Dhaka",
                target="Dhaka",
            )
        )
        await asyncio.sleep(0.02)
        self.assertEqual(len(room2.local_participant.attempts), 1)
        read_gate.close("cleanup")
        with self.assertRaises(ToolError):
            await task2

    async def test_off_stops_the_tool_before_anything_is_published(self):
        gate, _session, room, _publisher = make_gate(policy={"open_app": "off"})
        with self.assertRaises(ToolError) as caught:
            await gate.require(
                tool="open_app",
                action="launch",
                action_class="act",
                summary="Open the app notepad",
                target="notepad",
            )
        self.assertEqual(str(caught.exception), MSG_OFF)
        self.assertEqual(room.local_participant.attempts, [])

    # -- one action deserves one question -----------------------------------

    async def test_a_second_identical_ask_while_the_first_is_pending_is_refused(self):
        # The model can emit the same call twice in one step (parallel tool
        # calls, a retry). Two cards would be a question answered twice, and
        # one "yes" would then run a non-idempotent action -- a send, a
        # force-kill -- twice off one decision.
        gate, _session, room, _publisher = make_gate()
        first = await self.arm(gate)
        second = await self.arm(gate)

        with self.assertRaises(ToolError) as caught:
            await asyncio.wait_for(second, timeout=1)
        self.assertEqual(str(caught.exception), MSG_DUPLICATE)

        self.assertEqual(len(room.local_participant.attempts), 1, "one action must not mint two cards")
        self.assertFalse(first.done(), "the original request must still be waiting")
        decisions = self.audit_events("decision")
        self.assertEqual(decisions[-1]["decision"], "refused")
        self.assertEqual(decisions[-1]["channel"], "policy")

        # The original is untouched: answering it still runs the action.
        request_id = room.local_participant.attempts[0][0]["id"]
        gate.handle_channel_message(
            response(request_id, "approve"), sender_kind="user", sender_identity="user-1"
        )
        await asyncio.wait_for(first, timeout=1)

    async def test_a_different_verb_of_the_same_tool_is_its_own_question(self):
        # Dedupe is on (tool, verb, what it would do), not on the tool: two
        # different verbs are two different actions, whatever they share.
        gate, _session, room, _publisher = make_gate()
        first = await self.arm(gate)
        second = await self.arm(gate, action="focus", summary="Focus the window Notepad")

        self.assertEqual(len(room.local_participant.attempts), 2)
        gate.close("cleanup")
        for task in (first, second):
            with self.assertRaises(ToolError):
                await asyncio.wait_for(task, timeout=1)

    async def test_an_identical_ask_after_the_first_was_answered_asks_again(self):
        # The dedupe is about a card already on screen, never about
        # remembering a decision: each call asks again, exactly as it did
        # before this existed.
        gate, _session, room, _publisher = make_gate()
        first = await self.arm(gate)
        request_id = room.local_participant.attempts[0][0]["id"]
        gate.handle_channel_message(
            response(request_id, "approve"), sender_kind="user", sender_identity="user-1"
        )
        await asyncio.wait_for(first, timeout=1)

        again = await self.arm(gate)
        self.assertEqual(len(room.local_participant.attempts), 2, "an answered ask was not asked again")
        request_id = room.local_participant.attempts[1][0]["id"]
        gate.handle_channel_message(
            response(request_id, "deny"), sender_kind="user", sender_identity="user-1"
        )
        with self.assertRaises(ToolError):
            await asyncio.wait_for(again, timeout=1)


# ---------------------------------------------------------------------------
# 4b. One verb of one tool, named in the policy
# ---------------------------------------------------------------------------


class PerActionPolicyTests(AuditFileMixin, unittest.IsolatedAsyncioTestCase):
    async def _ask(self, gate, room, **overrides):
        kwargs = dict(
            tool="manage_window",
            action="close",
            action_class="act",
            summary="Close the window Notepad",
            target="Notepad",
        )
        kwargs.update(overrides)
        task = asyncio.ensure_future(gate.require(**kwargs))
        await asyncio.sleep(0.02)
        return task, room

    async def test_a_policy_key_naming_one_verb_puts_the_question_back(self):
        # The entry the settings docs name: an ordinary act becomes an ask,
        # for that verb alone.
        gate, _session, room, _publisher = make_gate(policy={"manage_window.close": "ask"})
        task, room = await self._ask(gate, room)
        self.assertEqual(len(room.local_participant.attempts), 1, "the tightened verb did not ask")
        self.assertEqual(room.local_participant.attempts[0][0]["action_class"], "act")
        gate.close("cleanup")
        with self.assertRaises(ToolError):
            await task

    async def test_the_rest_of_the_tool_keeps_their_own_level(self):
        gate, _session, room, _publisher = make_gate(policy={"manage_window.close": "ask"})
        task, room = await self._ask(gate, room, action="focus", summary="Focus the window Notepad")
        self.assertEqual(
            room.local_participant.attempts,
            [],
            "tightening one verb must not drag the others up with it",
        )
        await asyncio.wait_for(task, timeout=1)

    async def test_a_tool_level_entry_still_governs_every_verb_it_does_not_name(self):
        # The dotted key overrides where it exists; the tool-level entry is
        # still the broad stroke everywhere else. An act under a tool-wide
        # `ask` is an ask.
        gate, _session, room, _publisher = make_gate(policy={"manage_window": "ask"})
        task, room = await self._ask(gate, room, action="focus", summary="Focus the window Notepad")
        self.assertEqual(
            len(room.local_participant.attempts),
            1,
            "the tool-level entry did not reach an unnamed verb",
        )
        gate.close("cleanup")
        with self.assertRaises(ToolError):
            await task

    async def test_no_policy_value_lowers_a_destructive_verb(self):
        # The dotted key is a tightening mechanism, not a loophole: naming the
        # verb as `auto` cannot make a force-kill automatic, because `auto`
        # means *the default*, and the default for destructive is a question.
        gate, _session, room, _publisher = make_gate(
            policy={"manage_window.terminate": "auto", "manage_window": "auto"}
        )
        task, room = await self._ask(
            gate,
            room,
            action="terminate",
            action_class="destructive",
            summary="Force-terminate the process running Notepad",
        )
        self.assertEqual(len(room.local_participant.attempts), 1)
        gate.close("cleanup")
        with self.assertRaises(ToolError):
            await task


# ---------------------------------------------------------------------------
# 5. Voice consent inside the gate
# ---------------------------------------------------------------------------


class VoiceChannelTests(AuditFileMixin, unittest.IsolatedAsyncioTestCase):
    async def test_only_a_final_matching_transcript_approves(self):
        gate, session, _room, _publisher = make_gate()
        task = asyncio.ensure_future(
            gate.require(
                tool="manage_window",
                action="close",
                action_class="sensitive",
                summary="Close the window Notepad",
                target="Notepad",
            )
        )
        await asyncio.sleep(0.02)

        session.emit("user_input_transcribed", transcript("yes", is_final=False))
        await asyncio.sleep(0.01)
        self.assertFalse(task.done(), "a partial transcript approved the action")

        session.emit("user_input_transcribed", transcript("Yes!"))
        await asyncio.wait_for(task, timeout=1)
        self.assertEqual(self.audit_events("decision")[-1]["channel"], "voice")

    async def test_a_voice_refusal_is_honoured(self):
        gate, session, _room, _publisher = make_gate()
        task = asyncio.ensure_future(
            gate.require(
                tool="manage_window",
                action="close",
                action_class="sensitive",
                summary="Close the window Notepad",
                target="Notepad",
            )
        )
        await asyncio.sleep(0.02)
        session.emit("user_input_transcribed", transcript("no thanks"))
        with self.assertRaises(ToolError) as caught:
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual(str(caught.exception), MSG_DENIED)

    async def test_ambiguous_speech_keeps_waiting(self):
        gate, session, _room, _publisher = make_gate(timeout=0.3)
        task = asyncio.ensure_future(
            gate.require(
                tool="manage_window",
                action="close",
                action_class="sensitive",
                summary="Close the window Notepad",
                target="Notepad",
            )
        )
        await asyncio.sleep(0.02)
        session.emit("user_input_transcribed", transcript("hmm, maybe later"))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done(), "ambiguous speech resolved the request")
        # ...and the unanswered request still refuses on its own.
        with self.assertRaises(ToolError):
            await asyncio.wait_for(task, timeout=2)

    async def test_voice_without_a_pending_request_does_nothing(self):
        gate, session, _room, _publisher = make_gate()
        session.emit("user_input_transcribed", transcript("yes"))
        self.assertEqual(gate.pending_count, 0)
        self.assertEqual(self.audit_lines(), [])


# ---------------------------------------------------------------------------
# 6. The registry and the helper tools call
# ---------------------------------------------------------------------------


class RegistryTests(AuditFileMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        # A developer's own saved permissions must not decide what these
        # tests assert, so the loader is pinned to an empty policy.
        patcher = mock.patch.object(permission_gate, "load_policy", return_value=({}, 5.0))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_acquire_bind_and_release(self):
        session = FakeSession()
        gate = acquire_gate(session, room=FakeRoom(), publisher=FakePublisher(), label="r")
        self.assertIs(gate_for(session), gate)

        task = asyncio.ensure_future(
            gate.require(
                tool="manage_window",
                action="close",
                action_class="sensitive",
                summary="Close the window Notepad",
                target="Notepad",
            )
        )
        await asyncio.sleep(0.02)
        release_gate(session)
        with self.assertRaises(ToolError) as caught:
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual(str(caught.exception), MSG_CANCELLED)
        self.assertIsNone(gate_for(session), "a released gate must not answer later work")

    async def test_two_sessions_have_independent_gates(self):
        first, second = FakeSession(), FakeSession()
        gate_a = acquire_gate(first, room=FakeRoom(), publisher=FakePublisher(), label="a")
        gate_b = acquire_gate(second, room=FakeRoom(), publisher=FakePublisher(), label="b")
        self.addCleanup(release_gate, first)
        self.addCleanup(release_gate, second)
        self.assertIsNot(gate_a, gate_b)

        task = asyncio.ensure_future(
            gate_a.require(
                tool="manage_window",
                action="close",
                action_class="sensitive",
                summary="Close the window Notepad",
                target="Notepad",
            )
        )
        await asyncio.sleep(0.02)
        request_id = gate_a._order[0]
        # Session B cannot answer session A's question, in either direction.
        self.assertFalse(gate_b.resolve_ui(request_id, "approve", sender="user-1"))
        self.assertEqual(gate_a.pending_count, 1)
        gate_a.close("cleanup")
        with self.assertRaises(ToolError):
            await task

    async def test_without_a_gate_only_automatic_classes_pass(self):
        # A script or a unit test has no room and nobody to ask: automatic
        # work proceeds, anything that would have had to ask refuses.
        context = SimpleNamespace()
        self.assertIsNone(
            await require_approval(
                context,
                tool="get_weather",
                action="lookup",
                action_class="read",
                summary="Weather for Dhaka",
            )
        )
        with self.assertRaises(ToolError) as caught:
            await require_approval(
                context,
                tool="manage_window",
                action="close",
                action_class="sensitive",
                summary="Close the window Notepad",
                target="Notepad",
            )
        self.assertEqual(str(caught.exception), MSG_NO_GATE)

    async def test_with_a_gate_require_approval_delegates(self):
        session = FakeSession()
        gate = acquire_gate(session, room=FakeRoom(), publisher=FakePublisher(), label="r")
        self.addCleanup(release_gate, session)
        context = SimpleNamespace(session=session)

        task = asyncio.ensure_future(
            require_approval(
                context,
                tool="manage_window",
                action="close",
                action_class="sensitive",
                summary="Close the window Notepad",
                target="Notepad",
            )
        )
        await asyncio.sleep(0.02)
        self.assertEqual(gate.pending_count, 1)
        gate.resolve_ui(gate._order[0], "approve", sender="user-1")
        await asyncio.wait_for(task, timeout=1)


# ---------------------------------------------------------------------------
# 7. The audit log itself
# ---------------------------------------------------------------------------


class AuditLogTests(AuditFileMixin, unittest.TestCase):
    def test_a_line_is_json_with_the_fields_it_was_given(self):
        audit_log.record("decision", decision="approved", tool="open_app", detail="")
        lines = self.audit_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["event"], "decision")
        self.assertEqual(lines[0]["decision"], "approved")
        self.assertIn("ts", lines[0])
        self.assertNotIn("detail", lines[0], "empty fields are dropped")

    def test_a_broken_path_loses_the_line_not_the_session(self):
        with mock.patch.dict(os.environ, {audit_log.AUDIT_PATH_ENV: str(Path(self._tmp.name) / "gone" / "deep" / "audit.jsonl")}):
            audit_log.record("decision", decision="approved")  # must not raise

    def test_the_digest_detects_edits(self):
        body = audit_log.digest("Close the window Notepad", "Notepad")
        self.assertEqual(body, audit_log.digest("Close the window Notepad", "Notepad"))
        self.assertNotEqual(body, audit_log.digest("Close the window Chrome", "Chrome"))
        self.assertEqual(len(body), 64)


# ---------------------------------------------------------------------------
# 8. The validator's view of the permissions section
# ---------------------------------------------------------------------------


class PermissionValidationTests(unittest.TestCase):
    @staticmethod
    def base() -> dict:
        from agent.settings.config_store import env_document

        return env_document()

    @staticmethod
    def all_present() -> dict[str, bool]:
        # The tests are about the permissions section, not about whether this
        # machine happens to have a provider key in its environment right now.
        return {provider_id: True for provider_id in validation.PROVIDERS}

    def diagnostics(self, document) -> list:
        return validation.validate_document(document, self.all_present())

    def permission_codes(self, document) -> list[str]:
        return [d.code for d in self.diagnostics(document) if d.code.startswith("permissions.")]

    def test_a_clean_permissions_section_is_silent(self):
        document = self.base()
        document["permissions"] = {"tools": {"open_app": "ask", "get_weather": "default"}, "timeout": 30}
        self.assertEqual(self.permission_codes(document), [])

    def test_an_unknown_level_is_a_warning_that_explains_the_fallback(self):
        document = self.base()
        document["permissions"] = {"tools": {"open_app": "sometimes"}}
        codes = self.permission_codes(document)
        self.assertIn("permissions.unknown_level", codes)
        diagnostic = next(
            d for d in self.diagnostics(document) if d.code == "permissions.unknown_level"
        )
        self.assertEqual(diagnostic.severity, "warn")
        self.assertIn("treated as ask", diagnostic.hint)

    def test_an_out_of_range_timeout_warns(self):
        document = self.base()
        document["permissions"] = {"timeout": 999}
        self.assertIn("permissions.timeout_out_of_range", self.permission_codes(document))

    def test_permissions_must_be_an_object(self):
        document = self.base()
        document["permissions"] = "yes"
        codes = self.permission_codes(document)
        self.assertIn("permissions.invalid", codes)

    def test_a_permission_typo_is_not_an_error_that_deactivates_the_config(self):
        # One bad level must not reject the whole saved document: that would
        # throw away the provider settings over a permission note and fall
        # back to the environment, which is *less* strict, not more.
        document = self.base()
        document["permissions"] = {"tools": {"open_app": "banana"}}
        self.assertFalse(any(d.severity == "error" for d in self.diagnostics(document)))


class VoiceApprovalAnnouncementTests(unittest.TestCase):
    """Voice is the primary channel; the card carries what a sentence cannot."""

    class Session:
        """Records what was said, and emits the transcripts the test scripts.

        ``say`` mirrors the pinned SDK's signature exactly -- text plus keyword
        options -- because a fake with a looser signature accepts calls the real
        session would reject, and the announcement test would pass for a reason
        the runtime does not share.
        """

        def __init__(self):
            self.spoken: list[str] = []
            self.options: list[dict] = []
            self._handlers: dict[str, list] = {}

        def on(self, event, handler):
            self._handlers.setdefault(event, []).append(handler)
            return lambda: self._handlers[event].remove(handler)

        def say(self, text, *, allow_interruptions=True, add_to_chat_ctx=True, **_kwargs):
            self.spoken.append(str(text))
            self.options.append(
                {"allow_interruptions": allow_interruptions, "add_to_chat_ctx": add_to_chat_ctx}
            )
            return SimpleNamespace(text=text)

        def emit(self, event, payload):
            for handler in list(self._handlers.get(event, [])):
                handler(payload)

    def _gate(self, session=None, **kwargs):
        return PermissionGate(session or self.Session(), policy={"run_task": "ask"}, **kwargs)

    def _request(self, gate: PermissionGate, summary: str = "Send WhatsApp to Afia") -> PermissionRequest:
        # Armed inside a loop: the request's future belongs to the loop that will
        # resolve it, so a test that armed one from sync code would be exercising a
        # shape the runtime never produces.
        async def arm():
            return gate._arm(
                tool="run_task",
                action="send",
                action_class="destructive",
                summary=summary,
                target="Afia",
            )

        return asyncio.run(arm())

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("livekit.agents"), "livekit-agents is not installed"
    )
    def test_arming_speaks_the_question(self):
        # Driven through `require` rather than `_arm`, because the announcement is
        # part of arming a *real* question and not a side effect of minting an id.
        session = self.Session()
        room = SimpleNamespace(remote_participants={"u": SimpleNamespace(kind="user")})
        gate = self._gate(session, room=room)

        async def run():
            task = asyncio.create_task(
                gate.require(
                    tool="run_task",
                    action="send",
                    action_class="destructive",
                    summary="Send WhatsApp to Afia",
                    target="Afia",
                )
            )
            # Wait for the question to actually be spoken rather than guessing how
            # many loop turns the gate needs to get there, then answer it so the
            # coroutine finishes instead of running out its full deadline.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if session.spoken:
                    break
            session.emit(
                "user_input_transcribed",
                SimpleNamespace(is_final=True, transcript="yes", speaker_id="u"),
            )
            await task

        asyncio.run(run())
        self.assertEqual(len(session.spoken), 1)
        self.assertIn("Shall I go ahead?", session.spoken[0])

    def test_the_question_does_not_join_the_conversation(self):
        # If it did, the model would read its own approval request back as part of
        # the transcript -- and "the tool said the user approved" is exactly the
        # inference this product must never make.
        session = self.Session()
        room = SimpleNamespace(remote_participants={"u": SimpleNamespace(kind="user")})
        gate = self._gate(session, room=room)

        async def run():
            task = asyncio.create_task(
                gate.require(
                    tool="run_task",
                    action="send",
                    action_class="destructive",
                    summary="Send WhatsApp to Afia",
                    target="Afia",
                )
            )
            for _ in range(50):
                await asyncio.sleep(0.01)
                if session.spoken:
                    break
            session.emit(
                "user_input_transcribed",
                SimpleNamespace(is_final=True, transcript="yes", speaker_id="u"),
            )
            await task

        asyncio.run(run())
        self.assertTrue(session.options)
        self.assertFalse(session.options[0]["add_to_chat_ctx"])

    def test_the_question_cannot_be_interrupted_away(self):
        # An approval cut off mid-sentence is an approval that is sometimes never
        # heard, and silence refuses.
        session = self.Session()
        gate = self._gate(session)
        asyncio.run(gate._announce(self._request(gate)))
        self.assertTrue(session.options)
        self.assertFalse(session.options[0]["allow_interruptions"])

    def test_one_approval_speaks_one_question_and_the_card_agrees(self):
        # The reported "duplicate confirmation ask voice", pinned from the side
        # that could not be the cause.
        #
        # The cause was not this module. A search reached the gate **twice** --
        # once for `run_search` and again for the Enter press, which
        # `classify_key` had classed `sensitive` -- and two requests are two
        # spoken questions however well each is delivered. That is fixed at the
        # source in `browser.actions.is_search_control`, and pinned there by
        # `test_browser.SearchControlTests` and
        # `test_browser_search.ApprovalTests`.
        #
        # What this pins is the property that made the model escalation
        # necessary, so that nobody "fixes" a future duplicate by muting the
        # question instead: on a profile whose `say()` raises -- which is the
        # default `gemini_live` -- refusing the fallback would mean the
        # question is never spoken at all, and silence is a refusal.
        session = self.Session()
        gate = self._gate(session)
        request = self._request(gate)

        asyncio.run(gate._announce(request))

        self.assertEqual(len(session.spoken), 1, "one approval spoke more than one question")
        self.assertEqual(session.spoken[0], gate.spoken_question(request))
        # The same sentence the card carries, because a card that paraphrased
        # the action differently would let someone answer "yes" to one and
        # believe they had agreed to the other.
        self.assertEqual(gate._ui_record(request)["question"], session.spoken[0])

    def test_a_realtime_profile_still_asks_out_loud(self):
        # The regression, and the reason `runtime.voice_line` exists.

        # A realtime model has no TTS stage, so `say()` raises -- and that is what
        # the default `gemini_live` profile looks like. The announcement used to
        # call `say()`, swallow the raise, and leave a card on screen with no
        # voice: the user was asked to consent to something they had only been
        # shown, and the gate counted their silence as a denial.
        class RealtimeSession(self.Session):
            def say(self, text, **_kwargs):
                raise RuntimeError(
                    "trying to generate speech from text without a TTS model "
                    "or a RealtimeSession that supports say()"
                )

            def generate_reply(self, *, instructions="", **_kwargs):
                self.spoken.append(instructions)
                return None

        session = RealtimeSession()
        gate = self._gate(session)
        asyncio.run(gate._announce(self._request(gate)))

        self.assertTrue(session.spoken, "the question reached no audio channel at all")
        self.assertIn("Shall I go ahead?", session.spoken[0])
        self.assertFalse(gate.last_announce_failed)
        self.assertEqual(gate.last_announce_channel, "generated")

    def test_a_session_with_no_way_to_speak_says_so_rather_than_pretending(self):
        # The honest floor. A card that arrived with no voice needs the app to
        # compensate, and it can only do that if it is told.
        class Mute:
            def on(self, event, handler):
                return lambda: None

        gate = self._gate(Mute())
        asyncio.run(gate._announce(self._request(gate)))
        self.assertTrue(gate.last_announce_failed)
        self.assertEqual(gate.last_announce_channel, "silent")

    def test_the_channel_the_question_used_is_published_after_the_card(self):
        # Not a field on the card: the card goes out first, on purpose, so the
        # voice can never arrive before the thing it asks about. Which means the
        # channel is not yet known when the card is built, and putting it there
        # would report "none" for every question.
        published: list[tuple[str, dict]] = []

        class Publisher:
            # Mirrors `RuntimeEventPublisher.emit(event_type, **payload)`. A fake
            # with a different signature would swallow the call this test is
            # about -- the gate's publish is wrapped in a bare `except`, so a
            # mismatch shows up as "nothing was published" rather than an error.
            def emit(self, event_type, **payload):
                published.append((event_type, payload))
                return dict(payload)

        class PublisherSession(self.Session):
            def say(self, text, **_kwargs):
                raise RuntimeError("no TTS")

            def generate_reply(self, *, instructions="", **_kwargs):
                return None

        gate = self._gate(PublisherSession(), publisher=Publisher())
        request = self._request(gate)
        card = gate._ui_record(request)
        self.assertNotIn("spokenVia", card, "the card is built before the question is asked")

        asyncio.run(gate._announce(request))
        records = [payload for event, payload in published if event == "permission_announced"]
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["audible"])
        self.assertEqual(records[0]["spoken_via"], "model_voice")
        self.assertEqual(records[0]["id"], request.id)

    def test_the_card_and_the_voice_use_the_same_words(self):
        # A card that phrased the action differently from the spoken question
        # would let someone answer "yes" to one and believe they'd agreed to the
        # other.
        gate = self._gate()
        request = self._request(gate)
        record = gate._ui_record(request)
        self.assertEqual(record["question"], gate.spoken_question(request))

    def test_the_spoken_question_is_short(self):
        gate = self._gate()
        question = gate.spoken_question(self._request(gate, "S" * 900))
        self.assertLessEqual(len(question), 165)

    def test_the_question_ends_so_a_bare_yes_is_a_complete_answer(self):
        gate = self._gate()
        self.assertTrue(gate.spoken_question(self._request(gate)).endswith("?"))

    def test_voice_can_be_switched_off_without_changing_whether_it_asks(self):
        gate = self._gate()
        with patch.dict(os.environ, {VOICE_APPROVAL_VAR: "false"}):
            self.assertFalse(gate.voice_approval_enabled())
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(gate.voice_approval_enabled())

    def test_a_session_that_cannot_speak_is_not_a_refusal(self):
        class Broken:
            def on(self, *_a, **_k):
                return lambda: None

            def say(self, *_a, **_k):
                raise RuntimeError("no synthesizer")

        gate = self._gate(Broken())
        # The gate must still arm, publish and wait; only the announcement fails.
        request = self._request(gate)
        asyncio.run(gate._announce(request))
        self.assertEqual(request.state, "pending")


class VoiceDecisionTests(unittest.TestCase):
    """Hedges are not answers, and an answer is one thing not two."""

    def test_a_question_is_never_an_approval(self):
        for phrase in ("are you sure?", "what exactly?", "why?", "hold on"):
            with self.subTest(phrase=phrase):
                self.assertTrue(is_decision(phrase))
                self.assertIsNone(match_voice(phrase))

    def test_a_hedge_does_not_settle_a_pending_request(self):
        session = VoiceApprovalAnnouncementTests.Session()
        gate = PermissionGate(session, policy={"run_task": "ask"})

        async def arm() -> PermissionRequest:
            return gate._arm(tool="run_task", action="send", action_class="destructive", summary="s", target="t")

        request = asyncio.run(arm())
        session.emit(
            "user_input_transcribed",
            SimpleNamespace(is_final=True, transcript="are you sure?", speaker_id="u"),
        )
        self.assertEqual(request.state, "pending")

    def test_a_withdrawal_is_a_refusal(self):
        # Changing your mind a moment after saying yes has to be answerable.
        for phrase in ("cancel", "stop", "withdraw", "never mind", "not now"):
            with self.subTest(phrase=phrase):
                self.assertEqual(match_voice(phrase), "deny")

    def test_answering_the_spoken_sentence_is_understood(self):
        for phrase in ("go ahead", "send it", "yes send it", "do it now", "you can send it"):
            with self.subTest(phrase=phrase):
                self.assertEqual(match_voice(phrase), "approve")

    def test_every_non_decision_is_not_also_an_approval(self):
        # The two sets must not overlap, or "later" would be a yes.
        for phrase in NOT_DECISIONS:
            with self.subTest(phrase=phrase):
                self.assertNotEqual(match_voice(phrase), "approve")

    def test_an_answer_runs_the_action_once_even_if_both_channels_reply(self):
        session = VoiceApprovalAnnouncementTests.Session()
        gate = PermissionGate(session, policy={"run_task": "ask"})

        async def arm() -> PermissionRequest:
            return gate._arm(tool="run_task", action="send", action_class="destructive", summary="s", target="t")

        request = asyncio.run(arm())
        session.emit(
            "user_input_transcribed",
            SimpleNamespace(is_final=True, transcript="yes", speaker_id="u"),
        )
        self.assertEqual(request.state, "approved")
        # The card's answer for the same id is ignored: the request is settled, so
        # one decision cannot run a send twice.
        self.assertFalse(gate.resolve_ui(request.id, "approve"))
        self.assertEqual(request.state, "approved")


if __name__ == "__main__":
    unittest.main()
