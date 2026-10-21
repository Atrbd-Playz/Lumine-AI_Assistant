"""The WhatsApp operation table, and the references that address a message.

Two claims, each with the test that would fail if it were untrue:

1. **The table is the authority.** Every verb it declares appears on a card, its
   strictest class is what the registry and the permission floor report, and no
   verb reaches `destructive` without a reason for not retrying it. A verb added
   to a tool but not to the table would be a verb with no class, no card and no
   refusal -- so this file fails on any disagreement rather than trusting one of
   the three.
2. **A reference stops being usable before it becomes wrong.** Position from the
   end, a digest of the text, and a generation that a later read invalidates --
   each tested on its own, because each closes a different way of acting on
   somebody else's message by accident.
"""

from __future__ import annotations

import inspect
import re
import time
import unittest

from agent.runtime import permission_gate
from agent.settings.validation import WHATSAPP_MODES, validate_document
from agent.tools.messaging import whatsapp, whatsapp_account, whatsapp_chats
from agent.tools.messaging import whatsapp_ops as ops
from agent.tools.messaging import whatsapp_read, whatsapp_refs as refs
from agent.tools.tools_permission import TOOL_ACTION_CLASS, permission_floor
from agent.tools.tools_registry import TOOL_CARDS, ALL_TOOLS
from agent.tools.tools_permission import tool_id

#: The model-facing surface of each messaging tool: the function itself, because
#: its docstring *is* the schema the model reads. ReachableVerbTests reads verbs
#: out of these, which is the whole point -- a verb is offered by being written
#: down where the model can see it.
_TOOL_FUNCTIONS = {
    ops.READ_TOOL: whatsapp_read.whatsapp_read,
    ops.CHATS_TOOL: whatsapp_chats.whatsapp_chats,
    ops.ACCOUNT_TOOL: whatsapp_account.whatsapp_account,
    ops.SEND_TOOL: whatsapp.send_whatsapp,
}


class OperationTableTests(unittest.TestCase):
    def test_every_verb_is_in_one_of_known_action_classes(self):
        for row in ops.OPERATIONS:
            with self.subTest(verb=row.verb):
                self.assertIn(row.action_class, permission_gate.ACTION_CLASSES)

    def test_the_class_vocabulary_is_the_same_one_the_gate_enforces(self):
        # Written down in two places on purpose, so this is the pin. A class
        # invented in the table and not in the gate would be silently treated as
        # `sensitive` there -- safe, but the table would be lying about it.
        self.assertEqual(set(ops.ACTION_CLASSES), set(permission_gate.ACTION_CLASSES))

    def test_every_verb_can_be_carried_out_by_at_least_one_path(self):
        # A verb neither path supports is a tool that refuses forever, which reads
        # to a user exactly like a broken feature and is invisible in review.
        for row in ops.OPERATIONS:
            with self.subTest(verb=row.verb):
                self.assertTrue(
                    row.supported_paths,
                    f"{row.verb} names no path that can do it",
                )

    def test_every_verb_says_how_it_proves_it_happened(self):
        # Verification is not optional for anything that changes a chat. An
        # operation with no clause is an operation whose result is a guess, and a
        # guess about somebody's messages is the failure this whole design is
        # arranged to prevent.
        for row in ops.OPERATIONS:
            with self.subTest(verb=row.verb):
                self.assertTrue(
                    row.verify.strip(),
                    f"{row.verb} does not say how it would be confirmed",
                )

    def test_a_delete_is_confirmed_by_a_tombstone_and_not_by_its_absence(self):
        # Absence is not evidence: a conversation that scrolled past the message
        # and one where the delete worked look identical from outside. This is
        # asserted on the wording because the wording *is* the instruction to the
        # next person who changes the check.
        self.assertIn("tombstone", ops.verify_clause("delete").lower())
        self.assertIn("not its absence", ops.verify_clause("delete").lower())

    def test_every_destructive_operation_holds_its_retry(self):
        # The direction that matters and is always true: something irreversible
        # may exist outside this process, so an unconfirmed outcome must not be
        # answered with a retry. Getting this wrong is a duplicate message or a
        # second delete-for-everyone.
        for row in ops.OPERATIONS:
            if row.action_class == "destructive":
                with self.subTest(verb=row.verb):
                    self.assertGreater(
                        row.hold_seconds, 0.0, f"{row.verb} is destructive but retryable"
                    )

    def test_nothing_that_only_reads_or_acts_ever_holds_its_retry(self):
        # A read or an ordinary act leaves nothing behind, so holding its retry
        # would turn "ask again" into "never ask again" for no reason. The cost is
        # real: a mute that failed would stay unfixable for the whole window.
        for row in ops.OPERATIONS:
            if row.action_class in ("read", "act"):
                with self.subTest(verb=row.verb):
                    self.assertEqual(row.hold_seconds, 0.0)

    def test_a_sensitive_operation_holds_only_when_it_changes_something_outside(self):
        # `sensitive` is the awkward class here: blocking is sensitive *and* has
        # an effect the account keeps. Draft preparation is an ordinary action:
        # it leaves text unsent and requires no default approval. The set is
        # written out so that adding a hold somewhere unexpected is a test failure
        # rather than a quiet behaviour change.
        holding = {
            row.verb for row in ops.OPERATIONS
            if row.action_class == "sensitive" and row.hold_seconds > 0
        }
        self.assertEqual(holding, {"block"})

    def test_draft_preparation_is_not_classed_as_a_permission_prompt(self):
        self.assertEqual(ops.operation("prepare_draft").action_class, "act")

    def test_whatsapp_operations_expose_only_the_web_path(self):
        self.assertEqual(ops.PATHS, ("web",))
        self.assertEqual(ops.ONLY_PATH, "web")
        self.assertTrue(all(ops.supported_by(row.verb, "web") for row in ops.OPERATIONS))
        self.assertFalse(any(ops.supported_by(row.verb, "desktop") for row in ops.OPERATIONS))

    def test_a_refusal_names_the_web_path_and_the_verb(self):
        sentence = ops.refusal_for("delete", "web")
        self.assertIn("WhatsApp Web", sentence)
        self.assertIn("delete", sentence)
        self.assertIn("cannot", ops.refusal_for("teleport", "web"))

    def test_the_registry_and_the_cards_agree_about_every_messaging_tool(self):
        for tool in ops.MESSAGING_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, TOOL_ACTION_CLASS)
                self.assertIn(tool, TOOL_CARDS)
                self.assertEqual(
                    TOOL_ACTION_CLASS[tool],
                    ops.tool_action_class(tool),
                    "the registry and the operation table disagree about the class",
                )

    def test_the_badge_never_promises_less_than_the_gate_will_do(self):
        # The card's badge is the strictest class any verb reaches. This is the
        # anti-drift assertion: if a verb is raised to `destructive` the badge
        # follows without anyone remembering to change a card.
        for tool in ops.MESSAGING_TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(
                    permission_floor(tool),
                    permission_gate.effective_handling(
                        ops.tool_action_class(tool), "default"
                    ),
                )
                self.assertEqual(permission_floor(tool), "ask")

    def test_an_unknown_tool_is_treated_as_sensitive_not_as_harmless(self):
        # The fallback for a tool id nobody declared. `sensitive` rather than
        # `read`, for the reason the gate applies to an unknown action class:
        # mislabelling something milder is the dangerous direction.
        self.assertEqual(ops.tool_action_class("whatsapp_something_new"), "sensitive")

    def test_reading_history_is_declared_and_is_only_reading_history(self):
        readers = {row.verb for row in ops.OPERATIONS if row.reads_history}
        self.assertEqual(readers, {"messages"})
        for row in ops.OPERATIONS:
            if row.reads_history:
                with self.subTest(verb=row.verb):
                    self.assertEqual(row.action_class, "sensitive")

    def test_the_messaging_modes_are_still_the_three_the_schema_declares(self):
        # `whatsapp_ops` did not touch the mode list, and this is the pin that
        # would notice if it had.
        self.assertEqual(set(WHATSAPP_MODES), {"off", "deeplink", "automation"})

    def test_the_catalog_describes_every_declared_verb(self):
        described = ops.catalog()
        verbs = {
            row["verb"]
            for tool in described["tools"]
            for row in tool["operations"]
        }
        self.assertEqual(verbs, {row.verb for row in ops.OPERATIONS})

    def test_every_messaging_tool_is_registered_exactly_once(self):
        registered = [tool_id(tool) for tool in ALL_TOOLS]
        for tool in ops.MESSAGING_TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(registered.count(tool), 1)

    def test_the_read_verbs_are_declared_before_the_send_ones(self):
        # The order in which the model meets them is the cheapest-answer-first
        # nudge the registry relies on, and reading is always cheaper than
        # sending.
        read_verbs = set(ops.verbs_for(ops.READ_TOOL))
        self.assertTrue(read_verbs)
        self.assertTrue(ops.verbs_for(ops.SEND_TOOL))


class ReachableVerbTests(unittest.TestCase):
    """Every verb a settings key can name is a verb some tool actually accepts.

    The gap this closes is not hypothetical. `whatsapp_ops` declared a
    `message_search` verb that `whatsapp_read` had no branch for, and named the
    find-by-name verb `search_chats` when the tool's action is `find`. Both were
    invisible: the table checks itself for internal consistency, and a verb the
    tool cannot be asked for is perfectly consistent. The cost lands on a person
    who typed `messaging.whatsapp.react=ask` into a settings file and was told,
    correctly by the mechanism and wrongly in effect, that they had tightened
    something.

    So the check is against the model-facing surface itself -- the tool
    docstrings, which are the schema the model reads and the only place a verb is
    actually offered.
    """

    def _documented_verbs(self, tool: str) -> set[str]:
        doc = inspect.getdoc(_TOOL_FUNCTIONS[tool]) or ""
        return set(re.findall(r"``([a-z_]+)``", doc))

    def test_every_selectable_verb_is_one_the_tool_offers(self):
        for tool in ops.MESSAGING_TOOLS:
            documented = self._documented_verbs(tool)
            with self.subTest(tool=tool):
                self.assertTrue(
                    documented,
                    f"{tool} has no docstring to read verbs from",
                )
                for verb in ops.verbs_for(tool):
                    if not ops.operation(verb).selectable:
                        continue
                    self.assertIn(
                        verb,
                        documented,
                        f"{tool}.{verb} is a policy key that tightens nothing, because "
                        f"no action of {tool} dispatches on it",
                    )

    def test_a_verb_the_tool_does_not_offer_is_marked_unselectable(self):
        # The other direction, and the one that keeps the flag honest: a verb
        # absent from the docstring has to be declared unselectable rather than
        # left looking like a working switch.
        for tool in ops.MESSAGING_TOOLS:
            documented = self._documented_verbs(tool)
            for row in ops.operations_for(tool):
                if row.verb in documented:
                    continue
                with self.subTest(verb=row.verb):
                    self.assertFalse(
                        row.selectable,
                        f"{row.verb} is not offered by {tool} but is still declared "
                        f"as a verb a settings key can tighten",
                    )

    def test_the_authority_readers_ignore_unselectable_verbs(self):
        # A badge computed from an unreachable verb describes a tool stricter
        # than it is, forever, with no way to notice the verb is gone.
        for tool in ops.MESSAGING_TOOLS:
            with self.subTest(tool=tool):
                selectable = {op.verb for op in ops.selectable_operations(tool)}
                unselectable = {
                    op.verb for op in ops.operations_for(tool) if not op.selectable
                }
                self.assertFalse(
                    selectable & unselectable,
                    "a verb is both selectable and not",
                )
                classes = {op.action_class for op in ops.selectable_operations(tool)}
                all_classes = {op.action_class for op in ops.operations_for(tool)}
                if all_classes - classes:
                    # The floor may only be as strict as the reachable verbs.
                    self.assertLessEqual(
                        ops.ACTION_CLASSES.index(ops.tool_action_class(tool)),
                        max(ops.ACTION_CLASSES.index(name) for name in classes),
                        "the badge is stricter than any verb the tool can be asked for",
                    )


class RefStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = refs.RefStore()

    def test_a_read_bumps_the_generation_and_a_reference_from_before_it_is_stale(self):
        self.assertEqual(self.store.freshness(refs.MessageRef(index_from_end=1)), "")
        first = refs.MessageRef(index_from_end=1, generation=self.store.bump("Afia"), chat="Afia")
        # Fresh the instant it is minted, because that read is what produced it.
        self.store._generations["afia"] = first.generation
        self.assertEqual(self.store.freshness(first), "")
        self.store.bump("Afia")
        self.assertEqual(self.store.freshness(first), refs.MSG_STALE_CHAT)

    def test_a_reference_from_another_chat_is_not_confused_with_this_one(self):
        generation = self.store.bump("Afia")
        reference = refs.MessageRef(index_from_end=1, generation=generation, chat="Afia")
        self.store.bump("Bob")
        self.assertEqual(self.store.freshness(reference), "")

    def test_a_reference_expires_rather_than_being_trusted_forever(self):
        # Long enough to survive an approval question, which is a minute of the
        # user's time, and short enough that something from a while ago is
        # refused rather than guessed at.
        old = refs.MessageRef(
            index_from_end=1, chat="Afia", created_at=time.monotonic() - 10_000
        )
        self.assertEqual(self.store.freshness(old), refs.MSG_STALE_CHAT)

    def test_a_handle_that_is_not_one_reads_as_no_position(self):
        self.assertEqual(refs.parse_handle("m3"), 3)
        self.assertEqual(refs.parse_handle("3"), 0)
        self.assertEqual(refs.parse_handle("m"), 0)
        self.assertEqual(refs.parse_handle(""), 0)
        self.assertEqual(refs.parse_handle("m3; drop table"), 0)

    def test_references_count_back_from_the_newest(self):
        rows = [{"text": "one"}, {"text": "two"}, {"text": "three"}]
        made = refs.message_refs(rows, chat="Afia", generation=1)
        # Newest last in, and `1` is the newest -- so the end that moves is the
        # end we count from.
        self.assertEqual([ref.index_from_end for ref in made], [3, 2, 1])
        self.assertEqual(made[-1].preview, "three")

    def test_two_references_agree_only_when_they_describe_the_same_message(self):
        rows = [{"text": "hello", "mine": False}, {"text": "hi", "mine": True}]
        made = refs.message_refs(rows, chat="Afia", generation=1)
        self.assertTrue(made[0].matches(made[0]))
        self.assertFalse(made[0].matches(made[1]))
        # Position alone is not identity: the text and the direction must agree.
        same_position_other_text = refs.MessageRef(
            index_from_end=2, text_digest="something else", outgoing=False, kind="text"
        )
        self.assertFalse(made[1].matches(same_position_other_text))

    def test_a_message_with_no_text_is_matched_on_position_and_kind_alone(self):
        # Two adjacent stickers are otherwise indistinguishable, and refusing to
        # act on those is better than picking one of them.
        sticker = refs.MessageRef(index_from_end=1, kind="image")
        other = refs.MessageRef(index_from_end=1, kind="image")
        self.assertTrue(sticker.matches(other))
        self.assertFalse(sticker.matches(refs.MessageRef(index_from_end=1, kind="text")))

    def test_a_chat_key_folds_case_and_spacing_so_a_moved_row_is_not_a_moved_chat(self):
        one = refs.ChatRef(title="  Afia Rahman ")
        two = refs.ChatRef(title="afia rahman")
        self.assertEqual(one.key, two.key)
        self.assertEqual(one.handle, two.handle)

    def test_a_group_is_a_group_by_its_participants_and_not_by_its_name(self):
        # WhatsApp does not prefix group names, so the name cannot tell us. The
        # participant count can.
        self.assertTrue(refs.ChatRef(title="Team", participants=("a", "b")).is_group)
        self.assertFalse(refs.ChatRef(title="Team", participants=("a",)).is_group)

    def test_clearing_one_chat_leaves_the_others_alone(self):
        self.store.bump("Afia")
        self.store.bump("Bob")
        self.store.clear("Afia")
        self.assertEqual(self.store.generation("Afia"), 0)
        self.assertEqual(self.store.generation("Bob"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()