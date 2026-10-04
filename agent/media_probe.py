"""Report what is in a music folder, so the Settings screen can say so.

The Music page needs one number -- "how many tracks are in there" -- and that
number is the only honest way to tell a person they picked the right folder.
Choosing ``D:\\Music`` when their music is in ``D:\\Music\\Library`` is a mistake
nobody can see until they ask for a song and get nothing, and an empty count in
the picker is the only place it can be caught.

So this walks the folder with the *same* scanner the tool plays from
(:mod:`tools.media.scanner`) rather than a second implementation. A count from a
different walk would be a count of a different thing, and the screen would then
say 40 when Lumine can find 3.

It reports **names and counts only**. No file contents, and no absolute paths in
the output beyond the folder the caller already supplied -- which matters because
this writes JSON to a process the frontend is watching.

    python agent/media_probe.py "D:\\Music" [--no-recursive]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:  # as part of the agent package
    from .tools.media import library as media_library
    from .tools.media import scanner as media_scanner
except ImportError:  # python agent/media_probe.py, with agent/ on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    from tools.media import library as media_library  # type: ignore[no-redef]
    from tools.media import scanner as media_scanner  # type: ignore[no-redef]

#: How many folders to name. A person scanning for "vocal" does not read forty
#: names, and this payload goes into a settings screen.
MAX_FOLDERS = 24


def inspect(root: Path, *, recursive: bool = True) -> dict:
    """What is under ``root``: a track count, folder names, and any refusal.

    **Counts, never a track list.** It carried every filename for a while, so the
    screen could draw a library browser. That browser is gone -- music is a widget
    and a permission now, and the complaint the change answers was *"there's so
    much loading in the music settings"*, which started with a settings screen
    asking a worker for a few thousand filenames in order to show a number.

    What survives is what answers *"did I point this at the right place?"*, and a
    count is the only thing that can: point Lumine at ``D:\\Music`` when the music
    is in ``D:\\Music\\Library`` and every voice request answers "I couldn't find
    that" with nothing on screen to explain it.

    Never raises. This is a screen that is asking a question, and a folder it
    cannot read is an answer ("I could not read that folder") rather than a
    stack trace in the console.
    """
    if not root.is_dir():
        return {
            "ok": False,
            "error": "That is not a folder, or it is not there any more.",
            "path": str(root),
        }

    try:
        tracks = media_scanner.scan(root=root, recursive=recursive)
        folders = media_scanner.folders(root=root, recursive=recursive)
    except OSError as err:
        # A permission error on a subfolder must not read as "no music here".
        return {
            "ok": False,
            "error": f"That folder could not be read: {err}",
            "path": str(root),
        }

    suffixes: dict[str, int] = {}
    counts: dict[str, int] = {}
    for track in tracks:
        suffix = Path(track.path).suffix.casefold()
        if suffix:
            suffixes[suffix] = suffixes.get(suffix, 0) + 1
        counts[track.folder] = counts.get(track.folder, 0) + 1

    return {
        "ok": True,
        "path": str(root),
        # The folder's own name, which is what the screen shows. The full path is
        # for the "this is where she will look" line and is never spoken.
        "name": root.name or str(root),
        "trackCount": len(tracks),
        "folderCount": len(folders),
        # Names **and** counts: a folder with a number on it is a thing a person can
        # choose, and "vocal" on its own is a guess.
        "folders": [
            {"name": name, "tracks": counts.get(name, 0)} for name in folders[:MAX_FOLDERS]
        ],
        "formats": dict(sorted(suffixes.items())),
        "allowed": sorted(media_library.allowed_suffixes()),
        "recursive": recursive,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", help="The folder to look inside.")
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Count only what is directly in the folder.",
    )
    args = parser.parse_args(argv)

    raw = str(args.folder or "").strip().strip('"')
    if not raw:
        print(json.dumps({"ok": False, "error": "No folder was given."}))
        return 2

    try:
        root = Path(raw).expanduser().resolve(strict=False)
    except (OSError, ValueError, RuntimeError) as err:
        print(json.dumps({"ok": False, "error": f"That path cannot be used: {err}"}))
        return 2

    report = inspect(root, recursive=not args.no_recursive)
    print(json.dumps(report))
    # Always 0. The envelope carries `ok`, and a non-zero exit here would make the
    # caller treat "no playable files" as a *crash* -- which is how an empty music
    # folder would arrive at the settings screen as "could not read that folder".
    # Both facts are successes of the inspection: it read the folder, and the
    # answer is what it is.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())