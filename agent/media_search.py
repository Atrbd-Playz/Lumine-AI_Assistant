"""Rank a music library against a typed query.

The Settings screen's search box and Lumine's own matching **must** agree, and the
only way to guarantee that is for both to call one matcher. So this is a thin CLI
around `tools.media.match.rank` -- it holds no ranking logic of its own, and there
is no second implementation here to drift.

`agent/media_probe.py` is its sibling: that one answers "what is in this folder",
this one answers "which of them did you mean". Both are called by `media.rs`.

    python agent/media_search.py "D:\\Music" "akhon ami" [--limit 30]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:  # as part of the agent package
    from .tools.media import library as media_library
    from .tools.media import match as media_match
    from .tools.media import scanner as media_scanner
except ImportError:  # python agent/media_search.py, with agent/ on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    from tools.media import library as media_library  # type: ignore[no-redef]
    from tools.media import match as media_match  # type: ignore[no-redef]
    from tools.media import scanner as media_scanner  # type: ignore[no-redef]

#: Never return more than this whatever `--limit` says. The webview draws a list;
#: a list of four hundred rows is not a list, and the payload is already bounded
#: by `tool_results` on the voice side.
HARD_LIMIT = 100


def tighten(ranked: list, limit: int, *, floor_ratio: float = 0.75) -> list:
    """Drop results far below the best one, so the list stays a list.

    **This is the search box's rule and not Lumine's.** She gets every match,
    because "play every vocal song" is a request for all nine and the voice path
    answers it in one call. A person typing into a box is looking at a list, and a
    list of nine rows where one is the answer and eight are merely *related* is a
    list they have to read rather than a result they can use.

    The cut is relative -- three quarters of the best score -- so a library where
    everything scores alike keeps everything, and one where one track is clearly
    the answer shows that one. Measured against a real library: "play the Akhon
    Ami vocal song" drops from nine rows to one, while "play bayan" keeps both,
    because two songs by one artist is a real choice and one song out of nine is
    not. `total` still reports the full count, so the box can say "2 of 9" rather
    than pretending the other seven do not exist.
    """
    if not ranked:
        return []
    best = ranked[0].confidence
    threshold = max(0.30, best * floor_ratio)
    kept = [hit for hit in ranked if hit.confidence >= threshold]
    return kept[: max(1, limit)]


def search(root: Path, query: str, *, recursive: bool = True, limit: int = 30) -> dict:
    """Rank the library against ``query``.

    Never raises. This is a text box being typed into, and an exception here is a
    stack trace in the console instead of an empty list on screen -- which reads as
    "your library is empty" rather than "the search broke".
    """
    if not root.is_dir():
        return {
            "ok": False,
            "error": "That folder is not there any more. Choose it again in Settings.",
            "total": 0,
            "hits": [],
            "missing": [],
        }

    try:
        tracks = media_scanner.scan(root=root, recursive=recursive)
    except OSError as err:
        return {
            "ok": False,
            "error": f"That folder could not be read: {err}",
            "total": 0,
            "hits": [],
            "missing": [],
        }

    if not query.strip():
        # An empty box is not "no results" -- it is no question. The screen shows
        # the whole library in that state, so this returns everything unranked.
        return {
            "ok": True,
            "total": len(tracks),
            "hits": [
                {
                    "path": track.path,
                    "name": track.name,
                    "folder": track.folder,
                    "tags": list(track.tags),
                    "score": 100.0,
                }
                for track in tracks[: min(limit, HARD_LIMIT)]
            ],
            "missing": [],
        }

    ranked = media_match.rank(query, tracks, limit=max(limit, 60))
    shown = tighten(ranked, max(1, min(limit, HARD_LIMIT)))

    # Words present in **no** result. The one piece of feedback that turns "she
    # found nothing" into "try a different word", and the reason the search box
    # does not feel broken when a person types a title that is not in there.
    landed: set[str] = set()
    for hit in ranked:
        landed.update(hit.matched)
    wanted = media_match.significant_words(query)
    missing = [word for word in wanted if word not in landed]

    return {
        "ok": True,
        "total": len(ranked),
        "hits": [
            {
                "path": hit.track.path,
                "name": hit.track.name,
                "folder": hit.track.folder,
                "tags": list(hit.track.tags),
                "score": round(hit.confidence, 4),
            }
            for hit in shown
        ],
        "missing": missing[:6],
        # Told apart from `hits` so the box can say "2 of 9 matching" and be
        # honest about what it is not showing.
        "shown": len(shown),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", help="The music folder to search.")
    parser.add_argument("query", nargs="?", default="", help="What to search for.")
    parser.add_argument("--limit", type=int, default=30, help="How many results.")
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Search only what is directly in the folder.",
    )
    args = parser.parse_args(argv)

    raw = str(args.folder or "").strip().strip('"')
    if not raw:
        print(json.dumps({"ok": False, "error": "No folder was given.", "hits": []}))
        return 2

    try:
        root = Path(raw).expanduser().resolve(strict=False)
    except (OSError, ValueError, RuntimeError) as err:
        print(json.dumps({"ok": False, "error": f"That path cannot be used: {err}", "hits": []}))
        return 2

    result = search(
        root,
        str(args.query or ""),
        recursive=not args.no_recursive,
        limit=max(1, min(int(args.limit or 30), HARD_LIMIT)),
    )
    # **ASCII-escaped on purpose**, and this is not a default left alone. A Bengali
    # or accented filename is perfectly ordinary, and writing it as UTF-8 put a
    # `UnicodeEncodeError` between the search and the caller whenever the worker's
    # stdout was a cp1252 console -- which is what a Tauri child process gets on
    # Windows. `serde_json` reads `\uXXXX` perfectly, so escaping costs nothing at
    # the other end and removes the whole class of problem. `media_probe.py` does
    # the same for the same reason.
    print(json.dumps(result))
    # Always 0, for the reason `media_probe.py` gives: a folder that could not be
    # read is reported in `ok`, and exiting non-zero would make the caller report a
    # *crash* instead of the sentence the script already wrote.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())