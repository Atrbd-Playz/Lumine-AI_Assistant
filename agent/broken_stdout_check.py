"""End-to-end: a worker with a broken stdout must still start its job.

The crash this guards against killed the session on its opening line, because
emitting the first latency record was the first thing the job did. Unit tests
cover the writer; this proves the consequence, which is the part that matters.

The child's stdout is pointed at a closed pipe, so every write raises
``OSError: [Errno 22] Invalid argument`` exactly as the desktop app's failed pipe
did. A room is then dispatched and the worker's stderr is inspected.

**What this does not check:** whether the session ran to completion. Every session
signal is a ``LUMINE_EVENT`` record, which is precisely what went to the broken
pipe -- asserting on it would be asserting that the thing under test is broken.
``multisession_check.py`` covers the healthy-stdout case.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent"
AGENT_NAME = "lumine"
READY_TIMEOUT = 90.0
JOIN_TIMEOUT = 90.0
LOG = Path(tempfile.gettempdir()) / "lumine-broken-stdout.log"


async def main() -> int:
    sys.path.insert(0, str(AGENT))
    from livekit import api
    from dotenv import load_dotenv

    load_dotenv(AGENT / ".env")

    # A pipe whose read end is already closed: writing to it fails the way a dead
    # desktop-app pipe does, rather than merely filling up.
    dead_read, dead_write = os.pipe()
    os.close(dead_read)

    LOG.write_text("", encoding="utf-8")
    log_handle = LOG.open("w", encoding="utf-8", errors="replace")

    env = dict(os.environ)
    env["LIVEKIT_LOG_LEVEL"] = "info"
    env["PYTHONUNBUFFERED"] = "1"
    # stdout is the broken pipe; stderr is the log we inspect.
    proc = subprocess.Popen(
        [sys.executable, str(AGENT / "agent.py"), "dev"],
        # cwd is the repo root so the worker's own relative paths resolve the way
        # they do under `npm run tauri dev`.
        cwd=str(ROOT),
        env=env,
        stdout=os.fdopen(dead_write, "w", closefd=True),
        stderr=log_handle,
        stdin=subprocess.DEVNULL,
    )
    print(f"worker pid={proc.pid}, stdout -> closed pipe, log -> {LOG}")

    try:
        registered = _wait_for_log(r"registered worker", READY_TIMEOUT)
        print(f"  worker registered: {registered}")
        if not registered:
            return 1

        room = f"lumine-broken-stdout-{uuid.uuid4().hex[:8]}"
        lk = api.LiveKitAPI()
        try:
            await lk.room.create_room(api.CreateRoomRequest(name=room))
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room)
            )
            print(f"  dispatched -> {room}")

            # Give the job time to run the entrypoint. There is deliberately no
            # "did the session start" assertion here: every session signal is a
            # `LUMINE_EVENT` record, which is exactly what went to the broken pipe.
            # Asserting on it would be asserting that the thing under test is
            # broken. `multisession_check.py` covers the healthy-stdout case.
            await asyncio.sleep(20)
        finally:
            try:
                await lk.room.delete_room(api.DeleteRoomRequest(room=room))
            except Exception as exc:  # noqa: BLE001
                print(f"  room delete failed: {exc}")
            await lk.aclose()

        text = _log_text()
        errors = text.count("Invalid argument")
        crashed = "unhandled exception while running the job task" in text
        job_started = "initializing job runner" in text
        warned = "runtime events are being dropped" in text

        print(f"\n  job runner initialised:   {job_started}")
        print(f"  one-time notice printed:  {warned}")
        print(f"  'Invalid argument' count: {errors}")
        print(f"  job crashed:              {crashed}")

        # The regression this guards: before the fix, the entrypoint's first
        # latency record raised, the job died on its opening line, and the agent
        # never joined -- leaving only a restart of the desktop app.
        if job_started and errors == 0 and not crashed and warned:
            print("\nresult: SURVIVED a broken stdout "
                  "(the job ran its startup instead of dying on the first write)")
            return 0
        print("\nresult: FAILED")
        return 1
    finally:
        # Kill the whole tree. LiveKit runs each job in a child process, and that
        # child outlives a plain terminate of the parent -- which then stays
        # registered as `lumine` and quietly absorbs the next run's dispatches.
        # Cost an hour of chasing a "regression" that was a leftover process.
        _kill_tree(proc)
        log_handle.close()


def _log_text() -> str:
    try:
        return LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _wait_for_log(pattern: str, timeout: float) -> bool:
    compiled = re.compile(pattern)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if compiled.search(_log_text()):
            return True
        time.sleep(0.5)
    return False


def _log_errors() -> int:
    return _log_text().count("Invalid argument")


def _kill_tree(proc: subprocess.Popen) -> None:
    """Terminate the worker and any job process it spawned."""
    if os.name == "nt":  # pragma: no cover - platform branch
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
