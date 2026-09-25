import os
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest import mock

from agent.llm_config import (
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


if __name__ == "__main__":
    unittest.main()
