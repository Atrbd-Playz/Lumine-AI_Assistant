"""A telemetry write must never be able to end a voice session.

The failure this pins down was observed, not hypothesised. The desktop app pipes
the worker's stdout; a write to a pipe whose read end has gone away raises
``OSError: [Errno 22] Invalid argument`` on Windows. Because the first thing a job
does is emit a latency record, the session died on its opening line -- the agent
appeared to join and then produced nothing, and the only cure was restarting the
desktop app.

Every test here breaks stdout deliberately and then does the thing the worker
actually does at that point.
"""

import io
import sys
import unittest
from unittest import mock

from agent.runtime import runtime_events
from agent.runtime.runtime_events import RuntimeEventPublisher, write_event_line


class _BrokenStream(io.TextIOBase):
    """A stream that fails the way a dead Windows pipe does."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def write(self, *_args, **_kwargs) -> int:
        raise self.error

    def flush(self) -> None:
        raise self.error


class StdoutResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        # The module reports a broken pipe only once, so each test starts clean.
        runtime_events._STDOUT_FAILED = False
        self.addCleanup(setattr, runtime_events, "_STDOUT_FAILED", False)

    def test_the_windows_pipe_error_does_not_escape(self):
        with mock.patch.object(sys, "stdout", _BrokenStream(OSError(22, "Invalid argument"))):
            write_event_line('{"type":"latency"}')  # must not raise

    def test_a_closed_stream_does_not_escape(self):
        # A stream closed underneath us raises ValueError, not OSError.
        with mock.patch.object(sys, "stdout", _BrokenStream(ValueError("I/O operation on closed file"))):
            write_event_line('{"type":"latency"}')  # must not raise

    def test_emit_record_still_returns_its_record(self):
        publisher = RuntimeEventPublisher(room=None, room_name="r")
        with mock.patch.object(sys, "stdout", _BrokenStream(OSError(22, "Invalid argument"))):
            record = publisher.emit_record({"type": "session_started", "room": "r"})
        # The caller needs the record even when the write failed -- the data
        # channel path and the return value are unaffected.
        self.assertEqual(record["type"], "session_started")
        self.assertEqual(record["room"], "r")

    def test_the_broken_pipe_is_reported_exactly_once(self):
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdout", _BrokenStream(OSError(22, "Invalid argument"))), \
                mock.patch.object(sys, "stderr", stderr):
            for _ in range(25):
                write_event_line('{"type":"latency"}')

        notice = stderr.getvalue()
        self.assertEqual(notice.count("stdout is unavailable"), 1)
        # The point of reporting once: on a busy DEBUG stream, 25 events would
        # otherwise produce 25 identical warnings and bury anything real.
        self.assertLess(len(notice), 400)

    def test_it_stays_quiet_once_it_has_reported(self):
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdout", _BrokenStream(OSError(22, "Invalid argument"))), \
                mock.patch.object(sys, "stderr", stderr):
            write_event_line('{"type":"a"}')
            first = len(stderr.getvalue())
            write_event_line('{"type":"b"}')
        self.assertEqual(len(stderr.getvalue()), first)

    def test_a_healthy_stdout_still_writes_the_event(self):
        # The guard must not swallow the channel that the desktop app depends on
        # for worker readiness. A silent emitter would hang start_agent forever.
        buffer = io.StringIO()
        with mock.patch.object(sys, "stdout", buffer):
            write_event_line('{"type":"worker_registered"}')
        self.assertIn("LUMINE_EVENT", buffer.getvalue())
        self.assertIn("worker_registered", buffer.getvalue())

    def test_a_broken_stderr_does_not_escape_either(self):
        # The warning about a broken pipe is itself best-effort.
        with mock.patch.object(sys, "stdout", _BrokenStream(OSError(22, "Invalid argument"))), \
                mock.patch.object(sys, "stderr", _BrokenStream(OSError(22, "Invalid argument"))):
            write_event_line('{"type":"latency"}')  # must not raise

    def test_the_worker_bootstrap_emitter_is_also_safe(self):
        # `emit_runtime_event` is what announces worker_registered. If that raises,
        # the worker never registers and start_agent times out after 20 seconds.
        from agent.agent import emit_runtime_event

        with mock.patch.object(sys, "stdout", _BrokenStream(OSError(22, "Invalid argument"))):
            record = emit_runtime_event("worker_registered", worker_id="AW_1")
        self.assertEqual(record["type"], "worker_registered")


class PublisherSurfaceTests(unittest.TestCase):
    """The publisher's shape, asserted rather than assumed.

    This exists because a module-level function was once inserted in the middle of
    `RuntimeEventPublisher`, which silently ended the class body. Everything after
    it became a nested function, so `publisher.emit` vanished and every session
    died at `LatencyTracker(publisher.emit)` with an `AttributeError`.

    The unit tests for the writer all passed throughout, because none of them
    touched the methods that got swallowed. Only dispatching a real session found
    it. Asserting the public surface is the cheap guard that closes that gap.
    """

    def test_the_publisher_keeps_every_method_the_worker_calls(self):
        publisher = RuntimeEventPublisher(room=None, room_name="r")
        for name in (
            "emit_record",
            "emit",
            "publish_data",
            "publish_async",
            "emit_and_publish",
            "aclose",
        ):
            with self.subTest(method=name):
                self.assertTrue(
                    callable(getattr(publisher, name, None)),
                    f"RuntimeEventPublisher.{name} is missing -- the class body was "
                    f"probably truncated by a top-level definition",
                )

    def test_the_bridge_keeps_its_entry_points(self):
        from agent.runtime.runtime_events import ToolEventBridge

        bridge = ToolEventBridge(RuntimeEventPublisher(room=None, room_name="r"))
        for name in ("handle", "handle_function_tools"):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(bridge, name, None)))

    def test_the_module_level_writer_is_not_a_method(self):
        # It must be reachable both ways: the publisher uses it, and `agent.py`
        # imports it directly for the bootstrap events.
        from agent.runtime.runtime_events import write_event_line

        self.assertTrue(callable(write_event_line))
        self.assertNotIn("write_event_line", vars(RuntimeEventPublisher))

    def test_emitting_through_the_publisher_actually_works(self):
        # The end-to-end symptom, in miniature: this is the exact call the job
        # entrypoint makes on its first line.
        publisher = RuntimeEventPublisher(room=None, room_name="r")
        buffer = io.StringIO()
        with mock.patch.object(sys, "stdout", buffer):
            publisher.emit("latency", stage="entrypoint", elapsed_ms=1)
        self.assertIn('"stage":"entrypoint"', buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
