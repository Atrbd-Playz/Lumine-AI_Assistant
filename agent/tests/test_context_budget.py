"""What a turn is allowed to cost, and what is left over for the conversation.

The failure this exists for is arithmetic, not code. A cascade profile on a
per-minute allowance sends the *whole* prompt on every request -- persona, tool
policy, every tool schema, and the conversation so far -- and the provider charges
prompt plus completion against one shared per-minute number. Measured on this
toolset the fixed part alone was ~8.1k tokens against Groq's free 8,000 TPM, which
means the first request of a minute could not succeed and the session was mute
with nothing to show for it.

So the budget is derived, not declared: measure the fixed cost, subtract it and
the completion cap from the provider's allowance, and hand the remainder to the
conversation. That is also what makes the two kinds of provider fall out
naturally -- a local model has no per-minute allowance, so the same code path
gives it a large budget and nothing else changes.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from agent.runtime import context_trim as ct


class FakeItem:
    def __init__(self, text: str = "", role: str = "user", kind: str = "message"):
        self.type = kind
        self.role = role
        self.content = text


class FakeCtx:
    def __init__(self, items):
        self.items = list(items)

    def truncate(self, *, max_items: int):
        # Mirrors the SDK: keep the newest, and put the instruction back.
        kept = self.items[-max_items:]
        system = [i for i in self.items if ct._is_instruction(i)]
        self.items = system + [i for i in kept if not ct._is_instruction(i)]


class FakeSession:
    def __init__(self, items):
        self.chat_ctx = FakeCtx(items)


class EstimateTests(unittest.TestCase):
    def test_an_empty_string_costs_nothing(self):
        self.assertEqual(ct.estimate_tokens(""), 0)
        self.assertEqual(ct.estimate_tokens(None), 0)

    def test_the_estimate_is_generous_rather_than_tight(self):
        # Under-estimating a prompt is what gets a request rejected, so the ratio
        # is deliberately on the high side of English's ~4 chars/token.
        text = "the quick brown fox jumps over the lazy dog " * 40
        self.assertGreaterEqual(ct.estimate_tokens(text), len(text) / 4.5)

    def test_it_scales(self):
        self.assertGreater(ct.estimate_tokens("x" * 4000), ct.estimate_tokens("x" * 400))


class BudgetArithmeticTests(unittest.TestCase):
    def test_the_fixed_cost_is_measured_not_declared(self):
        # The whole point: adding a tool changes this number, and a budget built
        # from a constant written down months ago is wrong exactly when it matters.
        self.assertGreater(ct.fixed_prompt_tokens(), 0, "the fixed cost measured as zero")

    def test_a_rate_limited_provider_leaves_the_remainder_for_history(self):
        with mock.patch.object(ct, "fixed_prompt_tokens", return_value=5000):
            budget = ct.history_budget("groq", output_cap=900)
        self.assertEqual(budget["tpm"], 8000)
        self.assertEqual(budget["fixed"], 5000)
        self.assertEqual(budget["budget"], 8000 - 5000 - 900)

    def test_a_local_model_has_no_per_minute_ceiling_to_respect(self):
        # The future-local-model case, and the reason this is one function: a
        # self-hosted model is not charged per minute, so the conversation gets
        # the working default rather than a remainder of an allowance.
        with mock.patch.object(ct, "fixed_prompt_tokens", return_value=5000):
            local = ct.history_budget("ollama", output_cap=0)
        self.assertEqual(local["tpm"], 0, "a local model was given an invented ceiling")
        self.assertEqual(local["budget"], ct.DEFAULT_HISTORY_TOKENS)

    def test_a_realtime_model_gets_a_real_budget_not_an_allowance_it_does_not_have(self):
        # Gemini Live bills per token with implicit caching of the stable prefix.
        # It has no per-minute ceiling, so subtracting the fixed cost from an
        # invented one produced a number that read as measured and behaved as
        # though nobody had considered it -- the conversation simply never
        # trimmed and the panel could not say why.
        with mock.patch.object(ct, "fixed_prompt_tokens", return_value=5000):
            live = ct.history_budget("google", output_cap=1024)
        self.assertEqual(live["tpm"], 0)
        self.assertEqual(live["budget"], ct.DEFAULT_HISTORY_TOKENS)

    def test_an_unknown_provider_is_not_given_a_made_up_ceiling(self):
        # 0 means "none published", not "assume something". Only a provider whose
        # limit was actually read gets a number subtracted from it.
        self.assertEqual(ct.provider_tpm("some-provider-nobody-measured"), 0)

    def test_the_only_provider_with_a_published_cap_is_the_one_that_has_one(self):
        # Guard against the table quietly growing an entry nobody measured.
        self.assertEqual(sorted(ct.PROVIDER_TPM), ["groq"])

    def test_the_conversation_is_never_left_with_nothing(self):
        # A fixed prompt larger than the whole allowance would otherwise divide
        # into a negative budget and trim the conversation to nothing, and still
        # fail. The floor keeps a usable agent alive and the caller reports the
        # overspend separately.
        with mock.patch.object(ct, "fixed_prompt_tokens", return_value=99_000):
            budget = ct.history_budget("groq", output_cap=900)
        self.assertEqual(budget["budget"], ct.MIN_HISTORY_TOKENS)

    def test_the_environment_can_override_the_whole_arithmetic(self):
        with mock.patch.dict(os.environ, {ct.BUDGET_ENV: "1234"}), mock.patch.object(
            ct, "fixed_prompt_tokens", return_value=5000
        ):
            budget = ct.history_budget("groq", output_cap=900)
        self.assertEqual(budget["budget"], 1234)
        self.assertEqual(budget["overridden"], 1)


class TrimmingTests(unittest.TestCase):
    def test_a_short_conversation_is_left_alone(self):
        items = [FakeItem("hi"), FakeItem("hello")]
        outcome = ct.trim_context(FakeSession(items), token_budget=100_000)
        self.assertFalse(outcome.trimmed)
        self.assertEqual(len(items), 2)

    def test_the_token_budget_trims_where_the_item_count_would_not(self):
        # The case the item count could never catch: few items, each large. Ten
        # tool results are ten items and thousands of tokens.
        items = [FakeItem("x" * 4000) for _ in range(10)]
        session = FakeSession(items)
        outcome = ct.trim_context(session, limit=40, token_budget=2000)
        self.assertTrue(outcome.trimmed)
        self.assertLess(outcome.tokens_after, outcome.tokens_before)
        self.assertLessEqual(outcome.tokens_after, 2000)

    def test_the_newest_exchange_is_the_one_that_survives(self):
        items = [FakeItem(f"old {n}" + " " * 500) for n in range(8)]
        items.append(FakeItem("what did I just ask?"))
        session = FakeSession(items)
        ct.trim_context(session, limit=40, token_budget=400)
        kept = " ".join(str(getattr(i, "content", "")) for i in session.chat_ctx.items)
        self.assertIn("what did I just ask?", kept)

    def test_the_instruction_message_is_never_the_thing_dropped(self):
        # A model with no persona and no tool policy is not a working agent, and a
        # trim that lost them would be worse than one that failed loudly.
        items = [FakeItem("SYSTEM", role="system")]
        items += [FakeItem("x" * 4000) for _ in range(10)]
        session = FakeSession(items)
        ct.trim_context(session, limit=40, token_budget=200)
        self.assertTrue(
            any(ct._is_instruction(i) for i in session.chat_ctx.items),
            "the persona was trimmed away",
        )
        self.assertTrue(ct.instructions_intact(session))

    def test_the_outcome_reports_tokens_and_whether_it_is_still_over(self):
        items = [FakeItem("x" * 4000) for _ in range(10)]
        outcome = ct.trim_context(FakeSession(items), limit=40, token_budget=2000)
        self.assertGreater(outcome.tokens_before, 0)
        self.assertGreater(outcome.budget, 0)
        self.assertFalse(outcome.over_budget, outcome)

    def test_an_unreadable_chat_context_is_survivable(self):
        class Broken:
            @property
            def chat_ctx(self):
                raise RuntimeError("no context here")

        outcome = ct.trim_context(Broken(), token_budget=1000)
        self.assertFalse(outcome.trimmed)

    def test_a_trim_that_cannot_happen_does_not_raise(self):
        class NoTruncate:
            items = [FakeItem("x")]

            def truncate(self, **_kwargs):
                raise RuntimeError("not supported")

        session = FakeSession([FakeItem("x" * 4000)])
        session.chat_ctx = NoTruncate()
        outcome = ct.trim_context(session, limit=40, token_budget=10)
        self.assertFalse(outcome.trimmed)


class ItemTextTests(unittest.TestCase):
    def test_it_reads_a_plain_string(self):
        self.assertEqual(ct._item_text(FakeItem("hello")), "hello")

    def test_it_reads_a_list_of_parts(self):
        item = FakeItem()
        item.content = ["a", type("P", (), {"text": "b"})()]
        self.assertEqual(ct._item_text(item), "a b")

    def test_an_unreadable_item_costs_a_small_constant_not_zero(self):
        # Biasing towards trimming: an item this cannot measure must not be free,
        # or a context full of them would measure as empty and be sent.
        self.assertGreaterEqual(ct.conversation_tokens([object()]), 1)


if __name__ == "__main__":
    unittest.main()
