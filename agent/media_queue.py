"""Queue a folder's tracks and hand the queue to the system player.

The Music **widget**'s "Play all" button. It lives outside the worker because the
widget runs in the app process and the worker's tools are not callable from there
-- so this is the same decision the voice path makes, reached the same way: build
one playlist from the same scanner and open it.

``play_media`` in the worker and this script are two callers of
:mod:`tools.media.queue`. That is the point -- a widget that queued differently
from the voice path would be the same class of bug as a search box that ranked
differently, and it was fixed the same way.

    python agent/media_queue.py "D:\\Music"              # everything, in order
    python agent/media_queue.py "D:\\Music" --folder vocal
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:  # as part of the agent package
    from .tools.media import library as media_library
    from .tools.media import player as media_player
    from .tools.media import queue as media_queue
    from .tools.media import scanner as media_scanner
except ImportError:  # python agent/media_queue.py, with agent/ on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    from tools.media import library as media_library  # type: ignore[no-redef]
    from tools.media import player as media_player  # type: ignore[no-redef]
    from tools.media import queue as media_queue  # type: ignore[no-redef]
    from tools.media import scanner as media_scanner  # type: ignore[no-redef]

# `tools_compat` is deliberately *not* imported here. It lives at
# `agent/tools/tools_compat.py`, so reaching it from this script means putting
# `agent/tools/` on the path too -- which is exactly the flat-shape arrangement
# `test_import_shapes` guards against. Catching broadly below is a better answer
# than a second import shape for one exception class.


def build_queue(
    root: Path,
    *,
    folder: str = "",
    recursive: bool = True,
) -> dict:
    """Write a queue for ``root`` and hand it to the player. Never raises.

    The widget's button, so every failure has to arrive as a sentence the page can
    show rather than as a traceback it would have to catch.
    """
    if not root.is_dir():
        return {"ok": False, "error": "That folder is not there any more."}

    try:
        tracks = media_scanner.scan(root=root, recursive=recursive)
    except OSError as err:
        return {"ok": False, "error": f"That folder could not be read: {err}"}

    if folder:
        wanted = folder.casefold()
        tracks = [item for item in tracks if item.folder.casefold() == wanted]
    if not tracks:
        return {
            "ok": False,
            "error": "Nothing playable there." if not folder else f"There's nothing in '{folder}'.",
        }

    try:
        started = media_player.play_many(
            [Path(item.path) for item in tracks],
            titles=[item.name for item in tracks],
        )
    except OSError as exc:
        return {"ok": False, "error": str(exc) or "That file could not be opened."}
    except Exception as exc:  # noqa: BLE001 - a button must not take the app down
        return {"ok": False, "error": f"I could not queue that: {exc}"}

    return {
        "ok": True,
        "queued": len(started),
        "folder": folder or root.name,
        # **Say what it is, because it is not obvious.** One button, one queue, and
        # the player window owns it from here -- which is where skip and stop live.
        "note": "Queued in order in your media player. Skip and stop are in that window.",
        "first": started[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if started else "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", nargs="?", default="", help="The music folder.")
    parser.add_argument("--folder", dest="sub", default="", help="Queue one subfolder.")
    parser.add_argument("--no-recursive", action="store_true", help="Top level only.")
    args = parser.parse_args(argv)

    # `LUMINE_MEDIA_LIBRARY` is what a button without a folder argument uses, and
    # `library_root` prefers the saved document -- which is what the settings screen
    # writes. That is the whole bridge between "the user chose a folder" and "the
    # widget can queue it", and it is the same one the voice path uses.
    root = media_library.library_root()
    if args.folder:
        root = Path(args.folder).expanduser().resolve(strict=False)

    if root is None:
        print(json.dumps({"ok": False, "error": "Choose a music folder in Settings first."}))
        return 2

    try:
        result = build_queue(
            root,
            folder=args.sub,
            recursive=not args.no_recursive,
        )
    except (OSError, ValueError) as err:
        result = {"ok": False, "error": f"That folder could not be used: {err}"}

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())