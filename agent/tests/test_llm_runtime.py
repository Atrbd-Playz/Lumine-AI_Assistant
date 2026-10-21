import os
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest import mock

from agent.settings.llm_config import (
    DEFAULT_CONNECT_MAX_RETRY,
    DEFAULT_MAX_COMPLETION_TOKENS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    connect_max_retry,
    llm_config,
    max_completion_tokens,
    max_retries,
    temperature,
)
from agent.tools.tools_policy import TOOL_USAGE_POLICY, compose_instructions


class LlmConfigTests(unittest.TestCase):
    def test_defaults_cap_completion_tokens(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = llm_config()
        self.assertEqual(config["model"], DEFAULT_MODEL)
        self.assertEqual(config["max_completion_tokens"], DEFAULT_MAX_COMPLETION_TOKENS)
        self.assertEqual(config["temperature"], DEFAULT_TEMPERATURE)
        self.assertEqual(config["max_retries"], DEFAULT_MAX_RETRIES)

    def test_defaults_prevent_a_retry_storm(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(connect_max_retry(), DEFAULT_CONNECT_MAX_RETRY)
            self.assertEqual(DEFAULT_CONNECT_MAX_RETRY, 1)

    def test_env_overrides_are_honoured(self):
        env = {
            "GROQ_MODEL": "llama-3.3-70b-versatile",
            "GROQ_MAX_COMPLETION_TOKENS": "150",
            "GROQ_TEMPERATURE": "0.3",
            "GROQ_MAX_RETRIES": "1",
            "GROQ_CONNECT_MAX_RETRY": "2",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = llm_config()
            self.assertEqual(config["model"], "llama-3.3-70b-versatile")
            self.assertEqual(config["max_completion_tokens"], 150)
            self.assertEqual(config["temperature"], 0.3)
            self.assertEqual(config["max_retries"], 1)
            self.assertEqual(connect_max_retry(), 2)

    def test_parallel_tool_calls_are_disabled(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIs(llm_config()["parallel_tool_calls"], False)

    def test_retry_settings_are_clamped(self):
        with mock.patch.dict(os.environ, {"GROQ_MAX_RETRIES": "25", "GROQ_CONNECT_MAX_RETRY": "25"}, clear=True):
            self.assertEqual(max_retries(), 2)
            self.assertEqual(connect_max_retry(), 2)

    def test_retry_settings_allow_disabling(self):
        with mock.patch.dict(os.environ, {"GROQ_MAX_RETRIES": "0", "GROQ_CONNECT_MAX_RETRY": "0"}, clear=True):
            self.assertEqual(max_retries(), 0)
            self.assertEqual(connect_max_retry(), 0)

    def test_invalid_values_fall_back_to_defaults(self):
        env = {
            "GROQ_MODEL": "",
            "GROQ_MAX_COMPLETION_TOKENS": "many",
            "GROQ_TEMPERATURE": "warm",
            "GROQ_MAX_RETRIES": "lots",
            "GROQ_CONNECT_MAX_RETRY": "many",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = llm_config()
            self.assertEqual(config["model"], DEFAULT_MODEL)
            self.assertEqual(config["max_completion_tokens"], DEFAULT_MAX_COMPLETION_TOKENS)
            self.assertEqual(config["temperature"], DEFAULT_TEMPERATURE)
            self.assertEqual(config["max_retries"], DEFAULT_MAX_RETRIES)
            self.assertEqual(connect_max_retry(), DEFAULT_CONNECT_MAX_RETRY)

    def test_absurdly_small_completion_cap_is_ignored(self):
        with mock.patch.dict(os.environ, {"GROQ_MAX_COMPLETION_TOKENS": "1"}, clear=True):
            self.assertEqual(max_completion_tokens(), DEFAULT_MAX_COMPLETION_TOKENS)

    def test_temperature_default_is_unchanged(self):
        self.assertEqual(temperature(), DEFAULT_TEMPERATURE)


class GroqPluginWiringTests(unittest.TestCase):
    """The config must match what the installed plugin actually accepts."""

    def setUp(self):
        try:
            from livekit.plugins import groq  # noqa: F401
        except ImportError:
            self.skipTest("livekit-plugins-groq is not installed")

    def test_config_keys_are_valid_plugin_arguments(self):
        import inspect

        from livekit.plugins import groq

        parameters = inspect.signature(groq.LLM.__init__).parameters
        for key in llm_config():
            with self.subTest(argument=key):
                self.assertIn(key, parameters)

    def test_caps_reach_the_plugin(self):
        import asyncio

        from livekit.plugins import groq

        env = {"GROQ_API_KEY": "unit-test-placeholder"}
        with mock.patch.dict(os.environ, env, clear=True):
            llm = groq.LLM(**llm_config())
            try:
                self.assertEqual(llm._opts.max_completion_tokens, DEFAULT_MAX_COMPLETION_TOKENS)
                self.assertIs(llm._opts.parallel_tool_calls, False)
                self.assertEqual(llm._client.max_retries, DEFAULT_MAX_RETRIES)
            finally:
                asyncio.run(llm._client.close())


class ToolPolicyTests(unittest.TestCase):
    def test_persona_is_preserved_and_policy_is_appended(self):
        composed = compose_instructions("PERSONA TEXT")
        self.assertTrue(composed.startswith("PERSONA TEXT"))
        self.assertIn(TOOL_USAGE_POLICY.strip(), composed)

    def test_policy_names_every_registered_tool(self):
        from agent.tools.tools_registry import tool_ids

        for tool_id in tool_ids():
            with self.subTest(tool=tool_id):
                self.assertIn(tool_id, TOOL_USAGE_POLICY)

    def test_policy_constrains_each_tool(self):
        self.assertIn("only for a weather", TOOL_USAGE_POLICY)
        self.assertIn("only when the user explicitly asks for current or recent news", TOOL_USAGE_POLICY)
        self.assertIn("only when the user explicitly asks to open or launch an app", TOOL_USAGE_POLICY)

    def test_policy_blocks_casual_and_context_satisfied_lookups(self):
        # Regression guard for a stray search_web call on a casual remark.
        self.assertIn("Never call a tool just because a name, place, or topic came up in chat.", TOOL_USAGE_POLICY)
        self.assertIn("Casual conversation, opinions, and reactions never need a tool.", TOOL_USAGE_POLICY)
        self.assertIn("If the answer is already in this conversation, answer without a tool.", TOOL_USAGE_POLICY)

    def test_policy_limits_one_tool_per_reply(self):
        self.assertIn("at most one tool per reply", TOOL_USAGE_POLICY)

    def test_policy_offers_a_safe_alternative_after_failure(self):
        self.assertIn("offer one safe,", TOOL_USAGE_POLICY)
        self.assertIn("Never repeat the failed action", TOOL_USAGE_POLICY)
        self.assertIn("or bypass a refusal or denied approval", TOOL_USAGE_POLICY)

    def test_the_policy_keeps_the_browser_verbs_small(self):
        # Two things the model will get wrong without being told, both of which
        # cost a turn each time it does.
        self.assertIn("`search` action", TOOL_USAGE_POLICY)
        # `accepted` is the status that means "typed it, the page has not
        # confirmed" -- reporting it as a result list is how a search invents
        # results the user never saw.
        self.assertIn("`accepted`", TOOL_USAGE_POLICY)

    def test_the_policy_explains_the_screenshot_cost(self):
        # The whole point of `for_model` defaulting off. Without this the model
        # screenshots on every page read and pays for the image on every later
        # turn of the conversation.
        self.assertIn("for_model", TOOL_USAGE_POLICY)
        self.assertIn("describe only what is in the picture", TOOL_USAGE_POLICY)

    def test_screenshots_are_a_fallback_when_structured_page_text_is_insufficient(self):
        self.assertIn("skip it when the text already told you what you needed", TOOL_USAGE_POLICY)
        self.assertIn("A canvas, map or chart has no text", TOOL_USAGE_POLICY)

    def test_policy_asks_for_short_spoken_replies(self):
        self.assertIn("one or two short sentences", TOOL_USAGE_POLICY)

    def test_no_shell_tool_is_registered(self):
        from agent.tools.tools_registry import tool_ids

        for tool_id in tool_ids():
            with self.subTest(tool=tool_id):
                self.assertNotIn("shell", tool_id)
                self.assertNotIn("command", tool_id)
                self.assertNotIn("terminal", tool_id)


class AgentInstructionsTests(unittest.TestCase):
    def test_lumine_agent_carries_the_policy(self):
        try:
            from agent.agent import Lumine
        except Exception as exc:  # pragma: no cover - import-time environments
            self.skipTest(f"agent module unavailable: {exc}")
        instructions = Lumine().instructions
        self.assertIn("Tool use", instructions)
        self.assertIn("at most one tool per reply", instructions)


class VisionInstructionPushTests(IsolatedAsyncioTestCase):
    """The one channel by which a sight state the model cannot observe reaches it.

    A frame stream is not the answer: the model has no idea whether a picture in
    its context is current, and an instruction that says "you are sharing" is
    just as wrong an hour later. So the state is pushed as a *block*, deduped as
    a whole string, and this class is what makes those two properties hold at
    the seam where the worker meets the provider.
    """

    @staticmethod
    def _lumine(visual):
        try:
            from agent.agent import Lumine
        except Exception as exc:  # pragma: no cover - import-time environments
            raise unittest.SkipTest(f"agent module unavailable: {exc}")
        return Lumine(tools=[], visual=visual)

    @staticmethod
    def _visual():
        from agent.runtime.visual_state import VisualState

        visual = VisualState()
        visual.note_capability(True)
        return visual

    async def test_the_sight_state_is_there_from_the_first_turn(self):
        # The first request is built from `instructions` alone -- no tick has
        # run yet -- so a block that only arrived on a later push would leave
        # the opening reply of every session blind to its own sight state.
        agent = self._lumine(self._visual())

        self.assertIn("## Sight state", agent.instructions, "the pushed block, not the persona's own Sight section")
        self.assertIn('"can_see": true', agent.instructions)
        self.assertIn('"phase": "not_requested"', agent.instructions)
        self.assertIn("Tool use", agent.instructions)

    async def test_a_sight_state_that_never_changes_is_never_pushed_again(self):
        # `update_instructions` re-sends the whole persona, so a push with no
        # comparison is a per-tick tax on every conversation for a line that has
        # not moved. The dedup is the entire cost model of this feature.
        agent = self._lumine(self._visual())
        before = agent.instructions

        self.assertFalse(await agent.sync_vision_instructions())
        self.assertEqual(agent.instructions, before)

    async def test_a_transition_is_pushed_once_and_carries_the_whole_persona(self):
        visual = self._visual()
        agent = self._lumine(visual)
        before = agent.instructions

        visual.note_permission("denied")

        self.assertTrue(await agent.sync_vision_instructions())
        pushed = agent.instructions
        self.assertNotEqual(pushed, before)
        self.assertIn('"permission": "denied"', pushed)
        # The persona rides along: a push that replaced the instructions with
        # the block alone would delete everything else on the first transition.
        self.assertIn("Tool use", pushed)
        self.assertIn("## Sight state", pushed)

        self.assertFalse(await agent.sync_vision_instructions())
        self.assertEqual(agent.instructions, pushed, "the second call re-pushed a block that had not moved")

    async def test_a_refused_push_is_tried_again_on_the_next_tick(self):
        # Left unadvanced on purpose: a provider that fails this call once must
        # not leave her describing a sight state that is wrong for the rest of
        # the conversation, and a failure that never retries is a silent one.
        visual = self._visual()
        agent = self._lumine(visual)
        visual.note_permission("denied")
        before = agent.instructions

        async def refuse(_instructions: str) -> None:
            raise RuntimeError("provider refused")

        agent.update_instructions = refuse
        self.assertFalse(await agent.sync_vision_instructions())
        self.assertEqual(agent.instructions, before, "a failed push must not half-apply")

        del agent.update_instructions  # back to the real method
        self.assertTrue(await agent.sync_vision_instructions())
        self.assertIn('"permission": "denied"', agent.instructions)

    async def test_a_session_with_no_state_to_report_does_not_try(self):
        # The cascade pipeline has no frames to see and never subscribes, so
        # there is nothing to say -- and saying it would be a push whose only
        # content is that nothing is happening.
        try:
            from agent.agent import Lumine
        except Exception as exc:  # pragma: no cover - import-time environments
            self.skipTest(f"agent module unavailable: {exc}")
        agent = Lumine(tools=[], visual=None)

        self.assertFalse(await agent.sync_vision_instructions())
        self.assertNotIn("[vision]", agent.instructions)


if __name__ == "__main__":
    unittest.main()
