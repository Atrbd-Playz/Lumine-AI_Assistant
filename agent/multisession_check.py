"""Check that one worker serves many voice sessions.

A reported failure was "the agent joined the first session, then never joined
another, and only restarting the server helped". This dispatches three sequential
sessions at a single already-running worker -- create a room, dispatch, wait for
the worker to report `session_started`, hold it open, tear it down -- and reports
whether all three ran.

It asserts on the worker's own `session_started` record rather than on room
participants. That record is the event the desktop app's agent-join timeout is
really waiting for, and an agent participant is not always listed the instant it
starts -- which is enough to make a participant poll report a false failure.

Run it against a worker that is already up: `python agent.py dev`, or let the
desktop app start one. `LUMINE_WORKER_LOG` points it at the worker's stdout if
that is not in the system temp directory.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import uuid
from pathlib import Path

from livekit import api
from dotenv import load_dotenv

AGENT_NAME = "lumine"
ROUNDS = 3
START_TIMEOUT = 60.0
PATTERN = re.compile(r'LUMINE_EVENT \{"type":"session_started","room":"([^"]+)"')


def _log_path() -> Path:
    """Where the worker's stdout lands.

    ``LUMINE_WORKER_LOG`` wins, because where the child's output is redirected is
    the caller's business rather than this script's.
    """
    override = os.environ.get("LUMINE_WORKER_LOG")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "lumine-worker.log"


def started_rooms() -> set[str]:
    try:
        return set(PATTERN.findall(_log_path().read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return set()


async def one_round(lk: api.LiveKitAPI, index: int) -> bool:
    room = f"lumine-retest-{index}-{uuid.uuid4().hex[:8]}"
    joined = False
    try:
        await lk.room.create_room(api.CreateRoomRequest(name=room))
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room)
        )
        print(f"  round {index}: dispatched -> {room}")

        deadline = asyncio.get_running_loop().time() + START_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if room in started_rooms():
                joined = True
                break
            await asyncio.sleep(0.5)

        if joined:
            # Hold it open so the session is a real one, not an instant teardown.
            await asyncio.sleep(5)
        print(f"  round {index}: session_started = {joined}")
        return joined
    finally:
        try:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room))
        except Exception as exc:  # noqa: BLE001
            print(f"  round {index}: room delete failed: {exc}")
        # Let the previous session finish shutting down before the next dispatch.
        await asyncio.sleep(3)


async def main() -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    print(f"worker log: {_log_path()}")
    print(f"sessions already recorded before this run: {len(started_rooms())}\n")

    lk = api.LiveKitAPI()
    results = []
    for index in range(1, ROUNDS + 1):
        try:
            results.append(await one_round(lk, index))
        except Exception as exc:  # noqa: BLE001
            print(f"  round {index}: EXCEPTION {type(exc).__name__}: {exc}")
            results.append(False)

    await lk.aclose()

    print(f"\n{'=' * 56}")
    for index, ok in enumerate(results, start=1):
        print(f"  session {index}: {'JOINED' if ok else 'DID NOT JOIN'}")
    print(f"{'=' * 56}")
    all_ok = all(results)
    print("result:", "MULTI-SESSION OK" if all_ok else "MULTI-SESSION BROKEN")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
