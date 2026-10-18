"""Unit tests for whatsapp_account: presence, devices, profile, calls.

Tests all operations:
- about (reading account's own profile)
- devices (listing signed-in devices)
- presence (checking online status and last seen)
- unlink (signing out a linked device)
- call (voice and video calls)
- Refusals on unknown action, missing device, or failed operations.
"""

from __future__ import annotations

import asyncio
import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.tools.messaging import whatsapp_account as account_tool
from agent.tools.tools_compat import ToolError


class WhatsAppAccountTests(unittest.TestCase):
    def setUp(self):
        self.context = SimpleNamespace(session=SimpleNamespace())
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

        self.resolve = self.stack.enter_context(
            mock.patch.object(
                account_tool, "_resolve", new_callable=mock.AsyncMock,
                side_effect=lambda name: f"{name.strip()}"
            )
        )
        self.open_chat = self.stack.enter_context(
            mock.patch.object(account_tool.web, "open_chat", new_callable=mock.AsyncMock, return_value=True)
        )
        self.chat_info = self.stack.enter_context(
            mock.patch.object(
                account_tool.web,
                "chat_info",
                new_callable=mock.AsyncMock,
                return_value={"name": "Me", "phone": "+1234567890", "about": "Hey there!", "presence": "online"},
            )
        )
        self.devices = self.stack.enter_context(
            mock.patch.object(
                account_tool.web,
                "devices",
                new_callable=mock.AsyncMock,
                return_value=[{"text": "Chrome on Windows"}, {"text": "Safari on Mac"}],
            )
        )
        self.unlink_device = self.stack.enter_context(
            mock.patch.object(account_tool.web, "unlink_device", new_callable=mock.AsyncMock, return_value=True)
        )
        self.start_call = self.stack.enter_context(
            mock.patch.object(account_tool.web, "start_call", new_callable=mock.AsyncMock, return_value=True)
        )

    def call(self, action: str, who: str = "", call_type: str = "voice") -> str:
        return asyncio.run(
            account_tool.whatsapp_account(
                self.context, action=action, who=who, call_type=call_type
            )
        )

    # -- Refusals -----------------------------------------------------------

    def test_unknown_action_refuses(self):
        with self.assertRaisesRegex(ToolError, "I don't know how to do that"):
            self.call("teleport")

    def test_unlink_without_device_refuses(self):
        with self.assertRaisesRegex(ToolError, "Tell me which device to sign out"):
            self.call("unlink", who="")

    # -- Profile (about) ----------------------------------------------------

    def test_about_returns_profile_info(self):
        result = self.call("about")
        self.chat_info.assert_awaited_once()
        self.assertIn("Me", result)
        self.assertIn("+1234567890", result)
        self.assertIn("Hey there!", result)

    # -- Devices ------------------------------------------------------------

    def test_devices_lists_signed_in_devices(self):
        result = self.call("devices")
        self.devices.assert_awaited_once()
        self.assertIn("Chrome on Windows", result)
        self.assertIn("Safari on Mac", result)

    def test_devices_failure_raises(self):
        self.devices.return_value = []
        with self.assertRaisesRegex(ToolError, "couldn't read the list of signed-in devices"):
            self.call("devices")

    # -- Presence -----------------------------------------------------------

    def test_presence_returns_status(self):
        result = self.call("presence", who="Alice")
        self.open_chat.assert_awaited_once_with("Alice")
        self.chat_info.assert_awaited_once()
        self.assertEqual(result, "Alice: online.")

    def test_presence_hidden_by_privacy_settings(self):
        self.chat_info.return_value = {"presence": ""}
        result = self.call("presence", who="Alice")
        self.assertIn("privacy settings", result)

    def test_presence_open_chat_failure_raises(self):
        self.open_chat.return_value = False
        with self.assertRaisesRegex(ToolError, "couldn't open Alice's chat"):
            self.call("presence", who="Alice")

    # -- Unlink -------------------------------------------------------------

    def test_unlink_device(self):
        result = self.call("unlink", who="Chrome on Windows")
        self.unlink_device.assert_awaited_once_with("Chrome on Windows")
        self.assertIn("Signed Chrome on Windows out of your WhatsApp", result)

    def test_unlink_device_failure_raises(self):
        self.unlink_device.return_value = False
        with self.assertRaisesRegex(ToolError, "couldn't find a device called Chrome on Windows"):
            self.call("unlink", who="Chrome on Windows")

    # -- Calls (voice & video) ----------------------------------------------

    def test_voice_call_starts_call_with_video_false(self):
        result = self.call("call", who="Alice", call_type="voice")
        self.open_chat.assert_awaited_once_with("Alice")
        self.start_call.assert_awaited_once_with("Alice", video=False)
        self.assertIn("Started a call with Alice", result)

    def test_video_call_starts_call_with_video_true(self):
        result = self.call("call", who="Alice", call_type="video")
        self.open_chat.assert_awaited_once_with("Alice")
        self.start_call.assert_awaited_once_with("Alice", video=True)
        self.assertIn("Started a video call with Alice", result)

    def test_call_open_chat_failure_raises(self):
        self.open_chat.return_value = False
        with self.assertRaisesRegex(ToolError, "couldn't open Alice's chat, so no call started"):
            self.call("call", who="Alice")

    def test_call_start_failure_raises(self):
        self.start_call.return_value = False
        with self.assertRaisesRegex(ToolError, "call button may have moved"):
            self.call("call", who="Alice", call_type="voice")


if __name__ == "__main__":
    unittest.main()
