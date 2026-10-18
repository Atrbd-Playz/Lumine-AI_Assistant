"""Unit tests for whatsapp_chats: managing and arranging chats and messages.

Tests all selectable actions:
- mark_read, mark_unread, pin, unpin, mute, unmute, archive, unarchive
- star, unstar
- react, forward
- clear_chat
- delete
- block, unblock
- Refusals on unknown action, missing chat, invalid message handle, and failures.
"""

from __future__ import annotations

import asyncio
import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.tools.messaging import whatsapp_chats as chats_tool
from agent.tools.tools_compat import ToolError


class WhatsAppChatsTests(unittest.TestCase):
    def setUp(self):
        self.context = SimpleNamespace(session=SimpleNamespace())
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

        # Mock chat resolution to return the title directly
        self.resolve = self.stack.enter_context(
            mock.patch.object(
                chats_tool, "_resolve", new_callable=mock.AsyncMock,
                side_effect=lambda name: f"{name.strip()}"
            )
        )
        self.require_ready = self.stack.enter_context(
            mock.patch.object(chats_tool.web, "require_ready", new_callable=mock.AsyncMock)
        )
        self.open_chat = self.stack.enter_context(
            mock.patch.object(chats_tool.web, "open_chat", new_callable=mock.AsyncMock, return_value=True)
        )
        self.set_flag = self.stack.enter_context(
            mock.patch.object(chats_tool.web, "set_flag", new_callable=mock.AsyncMock, return_value=True)
        )
        self.star_message = self.stack.enter_context(
            mock.patch.object(chats_tool.web, "star_message", new_callable=mock.AsyncMock, return_value=True)
        )
        self.delete_message = self.stack.enter_context(
            mock.patch.object(chats_tool.web, "delete_message", new_callable=mock.AsyncMock, return_value=True)
        )
        self.react = self.stack.enter_context(
            mock.patch.object(chats_tool.web, "react", new_callable=mock.AsyncMock, return_value=True)
        )
        self.forward = self.stack.enter_context(
            mock.patch.object(chats_tool.web, "forward", new_callable=mock.AsyncMock, return_value=True)
        )
        self.clear_chat = self.stack.enter_context(
            mock.patch.object(chats_tool.web, "clear_chat", new_callable=mock.AsyncMock, return_value=True)
        )
        self.block_contact = self.stack.enter_context(
            mock.patch.object(chats_tool.web, "block_contact", new_callable=mock.AsyncMock, return_value=True)
        )
        self.clear_composer = self.stack.enter_context(
            mock.patch.object(chats_tool.web, "clear_composer", new_callable=mock.AsyncMock, return_value=True)
        )

    def call(
        self,
        action: str,
        chat: str = "",
        message: str = "",
        emoji: str = "",
        to: str = "",
    ) -> str:
        return asyncio.run(
            chats_tool.whatsapp_chats(
                self.context,
                action=action,
                chat=chat,
                message=message,
                emoji=emoji,
                to=to,
            )
        )

    # -- Basic validations & refusals ---------------------------------------

    def test_unknown_action_refuses(self):
        with self.assertRaisesRegex(ToolError, "I don't know how to do that"):
            self.call("teleport", chat="Alice")

    def test_missing_chat_refuses(self):
        with self.assertRaisesRegex(ToolError, "Tell me which chat you mean"):
            self.call("pin", chat="")

    # -- Chat-level flag verbs ----------------------------------------------

    def test_mark_read_and_unread(self):
        result = self.call("mark_read", chat="Alice")
        self.assertIn("Marked read: Alice", result)
        self.set_flag.assert_awaited_once_with(
            "Alice", "unread", "mark_read", want=False, fallback_text="mark as read"
        )

        self.set_flag.reset_mock()
        result = self.call("mark_unread", chat="Alice")
        self.assertIn("Marked unread: Alice", result)
        self.set_flag.assert_awaited_once_with(
            "Alice", "unread", "mark_unread", want=True, fallback_text="mark as unread"
        )

    def test_pin_and_unpin(self):
        result = self.call("pin", chat="Alice")
        self.assertIn("Pinned: Alice", result)
        self.set_flag.assert_awaited_once_with(
            "Alice", "pinned", "pin", want=True, fallback_text="pin chat"
        )

        self.set_flag.reset_mock()
        result = self.call("unpin", chat="Alice")
        self.assertIn("Unpinned: Alice", result)
        self.set_flag.assert_awaited_once_with(
            "Alice", "pinned", "unpin", want=False, fallback_text="unpin chat"
        )

    def test_mute_and_unmute(self):
        result = self.call("mute", chat="Alice")
        self.assertIn("Muted: Alice", result)
        self.set_flag.assert_awaited_once_with(
            "Alice", "muted", "mute", want=True, fallback_text="mute notifications"
        )

        self.set_flag.reset_mock()
        result = self.call("unmute", chat="Alice")
        self.assertIn("Unmuted: Alice", result)
        self.set_flag.assert_awaited_once_with(
            "Alice", "muted", "unmute", want=False, fallback_text="unmute notifications"
        )

    def test_archive_and_unarchive(self):
        result = self.call("archive", chat="Alice")
        self.assertIn("Archived: Alice", result)
        self.set_flag.assert_awaited_once_with(
            "Alice", "archived", "archive", want=True, fallback_text="archive chat"
        )

        self.set_flag.reset_mock()
        result = self.call("unarchive", chat="Alice")
        self.assertIn("Restored: Alice", result)
        self.set_flag.assert_awaited_once_with(
            "Alice", "archived", "unarchive", want=False, fallback_text="unarchive chat"
        )

    def test_set_flag_failure_raises(self):
        self.set_flag.return_value = False
        with self.assertRaisesRegex(ToolError, "couldn't confirm that Alice was"):
            self.call("pin", chat="Alice")

    # -- Star / Unstar ------------------------------------------------------

    def test_star_and_unstar_message(self):
        result = self.call("star", chat="Alice", message="m1")
        self.assertIn("Starred that message in Alice", result)
        self.star_message.assert_awaited_once_with(1, star=True)

        self.star_message.reset_mock()
        result = self.call("unstar", chat="Alice", message="m2")
        self.assertIn("Unstarred that message in Alice", result)
        self.star_message.assert_awaited_once_with(2, star=False)

    def test_star_invalid_handle_refuses(self):
        with self.assertRaisesRegex(ToolError, "Tell me which message to star"):
            self.call("star", chat="Alice", message="")

    def test_star_failure_raises(self):
        self.star_message.return_value = False
        with self.assertRaisesRegex(ToolError, "couldn't confirm the message was starred"):
            self.call("star", chat="Alice", message="m1")

    # -- Delete -------------------------------------------------------------

    def test_delete_message(self):
        result = self.call("delete", chat="Alice", message="m1")
        self.assertIn("Deleted that message in Alice for everyone", result)
        self.delete_message.assert_awaited_once_with(1)

    def test_delete_invalid_handle_refuses(self):
        with self.assertRaisesRegex(ToolError, "Tell me which message to delete"):
            self.call("delete", chat="Alice", message="not_a_handle")

    def test_delete_failure_raises(self):
        self.delete_message.return_value = False
        with self.assertRaisesRegex(ToolError, "couldn't confirm that message was deleted"):
            self.call("delete", chat="Alice", message="m1")

    # -- React --------------------------------------------------------------

    def test_react_with_named_emoji(self):
        result = self.call("react", chat="Alice", message="m1", emoji="heart")
        self.assertIn("Reacted with ❤️ to that message in Alice", result)
        self.react.assert_awaited_once_with(1, "❤️")

    def test_react_with_thumbs_up(self):
        result = self.call("react", chat="Alice", message="m2", emoji="thumbs up")
        self.assertIn("Reacted with 👍 to that message in Alice", result)
        self.react.assert_awaited_once_with(2, "👍")

    def test_react_with_literal_emoji(self):
        result = self.call("react", chat="Alice", message="m3", emoji="🔥")
        self.assertIn("Reacted with 🔥 to that message in Alice", result)
        self.react.assert_awaited_once_with(3, "🔥")

    def test_react_invalid_handle_refuses(self):
        with self.assertRaisesRegex(ToolError, "Tell me which message to react to"):
            self.call("react", chat="Alice", message="", emoji="heart")

    def test_react_failure_raises(self):
        self.react.return_value = False
        with self.assertRaisesRegex(ToolError, "couldn't confirm the reaction was added"):
            self.call("react", chat="Alice", message="m1", emoji="laugh")

    # -- Forward ------------------------------------------------------------

    def test_forward_message(self):
        result = self.call("forward", chat="Alice", message="m1", to="Bob")
        self.assertIn("Forwarded that message from Alice to Bob", result)
        self.forward.assert_awaited_once_with(1, destination="Bob")

    def test_forward_missing_destination_refuses(self):
        with self.assertRaisesRegex(ToolError, "Tell me who to forward this message to"):
            self.call("forward", chat="Alice", message="m1", to="")

    def test_forward_invalid_handle_refuses(self):
        with self.assertRaisesRegex(ToolError, "Tell me which message to forward"):
            self.call("forward", chat="Alice", message="", to="Bob")

    def test_forward_failure_raises(self):
        self.forward.return_value = False
        with self.assertRaisesRegex(ToolError, "couldn't confirm the message was forwarded"):
            self.call("forward", chat="Alice", message="m1", to="Bob")

    # -- Clear Chat ---------------------------------------------------------

    def test_clear_chat(self):
        result = self.call("clear_chat", chat="Alice")
        self.assertIn("Cleared all messages in Alice", result)
        self.clear_chat.assert_awaited_once()

    def test_clear_chat_failure_raises(self):
        self.clear_chat.return_value = False
        with self.assertRaisesRegex(ToolError, "couldn't clear the messages in Alice"):
            self.call("clear_chat", chat="Alice")

    # -- Block / Unblock ----------------------------------------------------

    def test_block_and_unblock(self):
        result = self.call("block", chat="Alice")
        self.assertIn("Blocked Alice", result)
        self.block_contact.assert_awaited_once_with(block=True)

        self.block_contact.reset_mock()
        result = self.call("unblock", chat="Alice")
        self.assertIn("Unblocked Alice", result)
        self.block_contact.assert_awaited_once_with(block=False)

    def test_block_failure_raises(self):
        self.block_contact.return_value = False
        with self.assertRaisesRegex(ToolError, "couldn't find the block option"):
            self.call("block", chat="Alice")

    # -- Clear Composer -----------------------------------------------------

    def test_clear_composer_clears_box_without_affecting_chat(self):
        result = self.call("clear_composer", chat="Alice")
        self.assertIn("Cleared the composer in Alice", result)
        self.clear_composer.assert_awaited_once()

    def test_clear_composer_aliases(self):
        self.call("clear_draft", chat="Alice")
        self.clear_composer.assert_awaited()
        self.clear_composer.reset_mock()
        self.call("discard_draft", chat="Alice")
        self.clear_composer.assert_awaited()

    def test_clear_composer_failure_raises(self):
        self.clear_composer.return_value = False
        with self.assertRaisesRegex(ToolError, "couldn't clear the message box"):
            self.call("clear_composer", chat="Alice")


if __name__ == "__main__":
    unittest.main()
