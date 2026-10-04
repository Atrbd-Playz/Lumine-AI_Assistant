"""Walking the library, and the order the results come back in.

Two decisions here are load-bearing and neither is obvious.

**Folders are playlists.** "Play my vocal playlist" means a folder called
``vocal``. No ``.m3u``, no ``.pls``, nothing to keep in step with the folders that
actually exist -- because a playlist file that disagrees with the directory is a
playlist file that will be wrong, and the directories are the thing the user
actually maintains.

**Natural order, not alphabetical.** ``sorted()`` puts ``Track 10`` before
``Track 2``, which plays a playlist in the wrong order and looks like a bug.
:func:`natural_key` splits each name into its text and its numbers so the sort
the user already sees in Explorer is the sort they get.

Every path is re-checked against the root after the walk, not trusted from
``rglob``. A symlink inside the library pointing at ``C:\\Users\\me\\Documents``
would otherwise be followed, and a scan that trusts its own directory iterator is
a scan that trusts symlinks.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

try:
    from .library import SKIP_DIRS, _resolved_root, allowed_suffixes, is_inside_library
    from .match import Track, track_for
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools.media.library import (  # type: ignore[no-redef]
        SKIP_DIRS,
        _resolved_root,
        allowed_suffixes,
        is_inside_library,
    )
    from tools.media.match import Track, track_for  # type: ignore[no-redef]

logger = logging.getLogger("lumine")

#: The most files a scan will return. A person with 40,000 tracks has a library
#: that cannot be searched in a voice turn anyway -- the answer would be a
#: paragraph -- and an unbounded list is a slow tool on every call.
MAX_TRACKS = 4000

#: The most directories a walk will descend into, for the same reason.
MAX_DIRS = 2000

_DIGITS = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """A sort key that orders ``Track 2`` before ``Track 10``.

    Splits on runs of digits and compares each numerically, so the zero-padded and
    unpadded forms of the same number sort together -- ``01`` and ``1`` are the
    same position, and a user whose files are zero-padded did not want them in a
    different order from the ones that are not.

    The string is casefolded first so ``BANANA`` and ``apple`` sort the way a
    file manager shows them rather than by ASCII, where every capital letter
    sorts before every lower-case one.
    """
    parts = _DIGITS.split(str(text or "").casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def _sorted_entries(directory: Path) -> list[Path]:
    """One directory's entries, natural order, with folders first.

    Folders first because a folder is a *playlist* here: when a request could
    match either a folder or a file, the folder is the more likely answer to
    "play my vocal songs", and it also comes first for free in a listing.
    """
    entries = [entry for entry in directory.iterdir() if not entry.name.startswith(".")]
    return sorted(
        entries,
        key=lambda entry: (0 if entry.is_dir() else 1, natural_key(entry.name)),
    )


def _walk(directory: Path, suffixes: frozenset[str], recursive: bool, budget: list[int]) -> Iterator[Path]:
    """Yield playable files under ``directory``, in natural order.

    The budget is a single-element list rather than a return value because a
    generator's own state is invisible to a caller and a limit that cannot be
    seen is a limit that gets removed.
    """
    if budget[0] <= 0:
        return
    try:
        entries = _sorted_entries(directory)
    except OSError:
        # An unreadable folder is skipped, not fatal. A music library with one
        # locked folder in it should still answer for the other nineteen.
        return
    for entry in entries:
        if budget[0] <= 0:
            return
        try:
            if entry.is_dir():
                if entry.name.casefold() in SKIP_DIRS or not recursive:
                    continue
                budget[1] -= 1
                if budget[0] <= 0:
                    return
                yield from _walk(entry, suffixes, recursive, budget)
                continue
            if entry.suffix.casefold() not in suffixes:
                continue
            if entry.stat().st_size <= 0:
                # A zero-byte file is a failed copy, and playing it opens a player
                # window showing an error.
                continue
        except OSError:
            continue
        budget[0] -= 1
        yield entry


def scan(
    *,
    root: Path | None = None,
    recursive: bool = True,
    limit: int = MAX_TRACKS,
) -> list[Track]:
    """Every playable track under the library, in the order a person would see.

    Returns ``[]`` for no library rather than raising, because "there is no music
    folder" and "your music folder is empty" are different sentences and the
    caller has to be able to tell them apart. :func:`library_root` is what says
    which of those it is.
    """
    # Resolved once, here, rather than per file. A walk that compares an
    # unresolved root against a resolved child reports every file as outside the
    # library and returns nothing -- a silent failure that looks exactly like an
    # empty folder.
    resolved_root = _resolved_root(root)
    if resolved_root is None:
        return []
    suffixes = allowed_suffixes()
    budget = [max(0, int(limit)), MAX_DIRS]
    tracks: list[Track] = []
    for path in _walk(resolved_root, suffixes, bool(recursive), budget):
        # Re-checked, never trusted from the walk. `rglob` follows a symlinked
        # directory, and a link inside the library pointing at the user's
        # documents is the shape this escape takes in practice.
        if not is_inside_library(path, resolved_root):
            continue
        try:
            tracks.append(track_for(path))
        except (OSError, ValueError):
            continue
    return tracks


def folders(*, root: Path | None = None, recursive: bool = True, limit: int = 200) -> list[str]:
    """The names of the folders under the library, natural order.

    Named only -- a folder name goes into an approval card and into speech, and a
    path would put the user's directory layout in front of both. Duplicates
    collapse: "play my vocal songs" means every vocal folder, and listing five of
    them helps nobody.
    """
    resolved_root = _resolved_root(root)
    if resolved_root is None:
        return []
    seen: set[str] = set()
    out: list[str] = []
    budget = [max(0, int(limit))]
    stack = [resolved_root]
    while stack and budget[0] > 0:
        current = stack.pop(0)
        try:
            entries = sorted(
                (entry for entry in current.iterdir() if entry.is_dir()),
                key=lambda entry: natural_key(entry.name),
            )
        except OSError:
            continue
        for entry in entries:
            if budget[0] <= 0:
                break
            if entry.name.casefold() in SKIP_DIRS:
                continue
            budget[0] -= 1
            name = entry.name
            if name not in seen:
                seen.add(name)
                out.append(name)
            if recursive:
                stack.append(entry)
    return out