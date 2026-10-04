"""End-to-end check of the music tool, with nothing actually played.

`desktop_smoke.py` has the same shape and the same reason: a library cannot be
verified with unit tests alone, because the interesting failures are the ones
where a *combination* is wrong -- a folder read but a track mis-chosen, a name
matched but the wrong file handed over. This walks the whole chain against a real
folder and prints what each sentence would have done.

**Nothing opens.** The player is replaced *after* import, so this reports the
tool's decisions rather than the operating system's. That is deliberate and it is
the only reason this is safe to run against your real music folder: it reads
filenames and nothing else, and `library.py` never opens a file's contents.

    python agent/media_check.py "D:\\Music"

Add ``--recursive`` to search subfolders, or set ``LUMINE_MEDIA_LIBRARY`` and pass
nothing. ``LUMINE_ENABLE_MEDIA`` is not consulted: a check should exercise the
tool rather than the switch in front of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Filenames here contain Bengali script, which a cp1252 console cannot print.
#
# **`reconfigure`, not a new `TextIOWrapper`** -- see `media_rank.py` for why: the
# wrapping version replaced the process's stdout on *import* and closed the real
# one when it was collected, breaking every later `print` in the process. This
# module is imported by `test_import_shapes` for exactly that reason.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from tools.media import control as media_control  # noqa: E402
from tools.media.control import play_media  # noqa: E402

OPENED: list[str] = []


class Recording:
    """Stands in for the system player. Records; never opens."""

    name = "recording"

    @staticmethod
    def play(path) -> None:
        OPENED.append(Path(path).name)

    @staticmethod
    def play_many(paths, *, titles=None, player=None):
        """The **real** queue builder, with this recorder as the player.

        It used to loop and record one open per track — which is exactly the shape
        that played nine songs at once with nothing audible, so a check script that
        recorded nine opens was reporting a bug as if it were a pass. Delegating
        means `OPENED` now holds **one** `.m3u8` and the queue's contents can be
        read back off disk, which is the only evidence that says "plays in order".
        """
        from tools.media import player as media_player

        return media_player.play_many(paths, titles=titles, player=Recording)


def queued_titles() -> list[str]:
    """The track names inside the last queue handed over, in order."""
    from tools.media import queue as media_queue

    for name in reversed(OPENED):
        candidate = media_queue.queue_dir() / name
        if candidate.is_file():
            return media_queue.read_titles(candidate)
    return []


def _payload(raw: str) -> dict:
    """The JSON inside the untrusted-data wrapper.

    Split on the fence rather than on braces: the payload nests objects, so
    "everything up to the last }" is not the body.
    """
    from tools.tool_results import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

    if UNTRUSTED_OPEN not in raw:
        return json.loads(raw)
    body = raw.split(UNTRUSTED_OPEN, 1)[1].split(UNTRUSTED_CLOSE, 1)[0]
    return json.loads(body[body.index("{") :])


async def main() -> int:
    args = [item for item in sys.argv[1:] if not item.startswith("-")]
    recursive = "--recursive" in sys.argv[1:]
    if args:
        os.environ["LUMINE_MEDIA_LIBRARY"] = args[0]
    if not os.environ.get("LUMINE_MEDIA_LIBRARY"):
        print("Give me a folder, or set LUMINE_MEDIA_LIBRARY.")
        return 2
    if not recursive:
        os.environ["LUMINE_MEDIA_RECURSIVE"] = "0"

    # Replaced here, not at import: anything earlier would leave a window open.
    media_control.media_player = Recording()
    call = getattr(play_media, "__wrapped__", play_media)
    ctx = SimpleNamespace(session=None)

    # Real phrasings, not synthetic ones. The interesting failures are in the
    # difference between what people say and what files are called, so a check that
    # only ever used exact titles would pass while the feature was broken.
    for action, fields in (
        ("folders", {}),
        ("find", {"query": ""}),
        ("search", {"query": "akhon ami"}),
        ("search", {"query": "bayan"}),
        ("play", {"query": "akhon ami"}),
        ("play", {"query": "husn by anuv jain"}),
        ("play", {"query": "play every vocal song", "all_tracks": True}),
        ("play", {"query": "something not in here at all"}),
        ("play", {"query": "a"}),
        ("find", {"query": "a"}),
        ("dance", {}),
    ):
        label = f"{action} {fields.get('query', '')}".rstrip()
        try:
            result = _payload(await call(ctx, action=action, **fields))
        except Exception as exc:  # noqa: BLE001 - the refusal *is* the output
            print(f"{label:<44} REFUSED  {exc}")
            continue
        said = (
            result.get("detail")
            or result.get("note")
            or result.get("lastPlayed")
            or result.get("hint")
            or ""
        )
        counts = ""
        if result.get("results") is not None:
            counts = f" [{result.get('count')} result(s)]"
        elif result.get("started") is not None:
            counts = f" [{result.get('started')}/{result.get('attempted')} started]"
        print(f"{label:<44} {result.get('status', '?'):<9}{counts} {str(said)[:66]}")

    print()
    print(f"recorded: {OPENED}")

    print()
    print(f"library: {os.environ['LUMINE_MEDIA_LIBRARY']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))