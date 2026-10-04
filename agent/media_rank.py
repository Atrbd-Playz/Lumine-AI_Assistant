"""Try the matcher against the real library and print what it would do.

Read-only: nothing is played, and this imports the scanner and the matcher
directly so no permission gate or player is involved at all. The point is to look
at *rankings*, which a unit test cannot check because they depend on what the files
are actually called.

    python agent/media_rank.py "C:\\Users\\you\\Music\\Lumine Media Folder"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Filenames here contain Bengali script, which a cp1252 console cannot print.
#
# **`reconfigure`, not a new `TextIOWrapper`.** Wrapping `sys.stdout.buffer` and
# assigning the result replaces the process's stdout for *everyone*, and the
# original wrapper is garbage-collected with its buffer still open -- which closes
# it. Imported by `test_import_shapes`, this module broke the print of every test
# that ran after it. `reconfigure` changes the encoding in place.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # a test harness may have replaced it
        pass

from tools.media import library  # noqa: E402
from tools.media import match  # noqa: E402
from tools.media import scanner  # noqa: E402

#: What people actually say, against what is actually on disk.
QUERIES = (
    "play the akhon ami vocal song",
    "akhon ami without music vocal only",
    "play all my vocal songs",
    "play the vocals only songs",
    "play husn",
    "play husn by anuv jain",
    "play maand",
    "play nahi milta",
    "play oviman",
    "play hoor",
    "play iraaday",
    "play bayan vocals",
    "play utshorgo tasnif zaman",
    "play something completely random",
    "play a",
)


def main() -> int:
    if len(sys.argv) > 1:
        os.environ["LUMINE_MEDIA_LIBRARY"] = sys.argv[1]
    root = library.library_root()
    if root is None:
        print("Set LUMINE_MEDIA_LIBRARY or pass a folder.")
        return 2
    tracks = scanner.scan()
    print(f"{len(tracks)} tracks in {root}\n")

    print("--- how each filename is read ---")
    for track in tracks:
        print(f"  {track.name}")
        print(f"      title: {track.title!r}   tags: {', '.join(track.tags) or '-'}")

    print("\n--- what each request would do ---")
    for query in QUERIES:
        hits = match.rank(query, tracks, limit=3)
        picked = match.choose(query, tracks)
        if not hits:
            verdict = "nothing"
        elif picked.ok:
            verdict = f"PLAY  {picked.chosen.name}"
        else:
            verdict = f"ASK   ({len(picked.candidates)} close)"
        top = " | ".join(
            f"{hit.track.title or hit.track.name} [{hit.match_word()}]" for hit in hits[:2]
        )
        print(f"\n  {query!r}\n      -> {verdict}\n      {top[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())