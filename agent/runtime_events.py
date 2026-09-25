"""Runtime event and tool-lifecycle bridges for the LiveKit worker.

The worker already exposes structured records as ``LUMINE_EVENT <json>`` on
stdout. This module adds a reliable LiveKit data channel for the same records so
the React voice boundary can receive tool status without depending on Tauri.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger("lumine")


class RuntimeEventPublisher:
    def __init__(self, room: Any, *, room_name: str | None = None) -> None:
        self.room = room
        self.room_name = room_name or getattr(room, "name", "")
        self._tasks: set[asyncio.Task[None]] = set()

    def emit_record(self, record: dict[str, object]) -> dict[str, object]:
        safe_record = dict(record)
        safe_record.setdefault("type", "runtime")
        try:
            encoded = json.dumps(safe_record, default=str, separators=(",", ":"))
        except (TypeError, ValueError):
            encoded = json.dumps({"type": "runtime", "error": "event serialization failed"})
        print(f"LUMINE_EVENT {encoded}", flush=True)
        return safe_record

    def emit(self, event_type: str, **payload: object) -> dict[str, object]:
        record: dict[str, object] = {"type": event_type}
        record.update({key: value for key, value in payload.items() if key != "type"})
        return self.emit_record(record)

    async def publish_data(self, topic: str, record: dict[str, object]) -> None:
        participant = getattr(self.room, "local_participant", None)
        if participant is None:
            return
        try:
            await participant.publish_data(
                json.dumps(record, default=str, separators=(",", ":")),
                reliable=True,
                topic=topic,
            )
        except Exception:
            logger.exception("[Runtime] failed to publish %s", topic)

    def publish_async(self, topic: str, record: dict[str, object]) -> asyncio.Task[None] | None:
        try:
            task = asyncio.create_task(self.publish_data(topic, record))
        except RuntimeError:
            # A synchronous test or shutdown path can still emit stdout events.
            return None
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def emit_and_publish(self, topic: str, record: dict[str, object]) -> dict[str, object]:
        emitted = self.emit_record(record)
        self.publish_async(topic, emitted)
        return emitted

    async def aclose(self) -> None:
        tasks = list(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class ToolEventBridge:
    """Normalize LiveKit tool events into the Lumine tool-status contract."""

    def __init__(self, publisher: RuntimeEventPublisher) -> None:
        self.publisher = publisher
        self._started: dict[str, float] = {}
        self._calls: dict[str, tuple[str, str]] = {}
        self._seen: set[str] = set()
        self._finished: set[str] = set()

    @staticmethod
    def _arguments(call: Any) -> dict[str, Any]:
        raw = getattr(call, "arguments", "{}")
        if isinstance(raw, dict):
            return raw
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _display_name(call: Any) -> str:
        name = str(getattr(call, "name", "") or "tool")
        arguments = ToolEventBridge._arguments(call)
        app_name = str(arguments.get("app_name", "") or "").strip()
        if name == "open_app" and app_name:
            return app_name[:80]
        return name.replace("_", " ")[:80]

    @staticmethod
    def _message(value: object, fallback: str) -> str:
        text = " ".join(str(value or "").split())
        return (text or fallback)[:240]

    def _publish(self, record: dict[str, object]) -> None:
        record.setdefault("timestamp", time.time())
        self.publisher.emit_and_publish("lumine.tool", record)

    def _start(self, call: Any) -> str:
        call_id = str(getattr(call, "call_id", "") or getattr(call, "id", "") or "")
        if not call_id:
            return ""
        self._started[call_id] = time.monotonic()
        self._seen.add(call_id)
        name = str(getattr(call, "name", "") or "tool")
        display_name = self._display_name(call)
        self._calls[call_id] = (name, display_name)
        if name == "open_app":
            message = f"Opening {display_name}..."
        else:
            message = f"Running {display_name}..."
        self._publish(
            {
                "type": "tool_status",
                "id": call_id,
                "name": name,
                "tool": name,
                "status": "started",
                "message": message,
                "summary": message,
            }
        )
        return call_id

    def _finish(self, update: Any) -> None:
        call_id = str(getattr(update, "call_id", "") or "")
        if not call_id:
            return
        if call_id in self._finished:
            return
        if call_id not in self._seen:
            # The function-tools fallback can provide the call object later; a
            # missing start event should not suppress the terminal result.
            return
        started = self._started.pop(call_id, None)
        duration_ms = round((time.monotonic() - started) * 1000) if started is not None else None
        stored_name, stored_display = self._calls.pop(call_id, ("", ""))
        name = stored_name or str(getattr(update, "name", "") or "tool")
        display_name = stored_display or self._display_name(update)
        raw_status = str(getattr(update, "status", "error"))
        if raw_status == "done":
            status = "completed"
            fallback = f"Completed {display_name}."
        elif raw_status == "cancelled":
            status = "failed"
            fallback = f"Tool call was cancelled: {display_name}."
        else:
            status = "failed"
            fallback = f"Could not run {display_name}."
        message = self._message(getattr(update, "message", ""), fallback)
        record: dict[str, object] = {
            "type": "tool_status",
            "id": call_id,
            "name": name,
            "tool": name,
            "status": status,
            "message": message,
            "summary": message,
        }
        if duration_ms is not None:
            record["duration_ms"] = duration_ms
        self._finished.add(call_id)
        self._publish(record)

    def handle(self, event: Any) -> None:
        update = getattr(event, "update", event)
        update_type = getattr(update, "type", "")
        if update_type == "tool_call_started":
            self._start(getattr(update, "function_call", None))
        elif update_type == "tool_call_ended":
            self._finish(update)

    def handle_function_tools(self, event: Any) -> None:
        """Fallback for SDK versions that only emit the batched event."""
        calls = list(getattr(event, "function_calls", []) or [])
        outputs = list(getattr(event, "function_call_outputs", []) or [])
        for call, output in zip(calls, outputs):
            call_id = str(getattr(call, "call_id", "") or "")
            if not call_id or call_id in self._seen:
                continue
            self._start(call)
            self._finish(
                type(
                    "FunctionOutputStatus",
                    (),
                    {
                        "call_id": call_id,
                        "name": getattr(output, "name", "") or getattr(call, "name", ""),
                        "status": "error" if getattr(output, "is_error", False) else "done",
                        "message": getattr(output, "output", ""),
                    },
                )()
            )
