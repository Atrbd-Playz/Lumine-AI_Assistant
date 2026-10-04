"""The music library: find a track Lumine was told about, and play it.

Four modules, each with one job, because the three things that go wrong here are
independent and fixing one of them should not mean touching the others:

- :mod:`library` -- what may be played. The security boundary.
- :mod:`match`   -- what was *said* versus what the file is *called*.
- :mod:`scanner` -- what exists, and in what order.
- :mod:`player`  -- what plays it.

:mod:`control` is the tool the model sees, and it is thin on purpose: it decides
what to ask and when, and holds no matching or path logic of its own. The same
split `tools/browser` uses between `tools.py` and `session.py`, for the same
reason -- the decisions are what a test needs to pin, and they should be reachable
without a browser or a disk.
"""

from __future__ import annotations

try:
    from .control import ACTIONS, play_media
    from .library import allowed_suffixes, describe, library_root, resolve_track
    from .match import Choice, Hit, Track, choose, find, rank, tags_of, title_of
    from .player import Player, default_player, play, play_many
    from .scanner import folders, natural_key, scan
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools.media.control import ACTIONS, play_media  # type: ignore[no-redef]
    from tools.media.library import (  # type: ignore[no-redef]
        allowed_suffixes,
        describe,
        library_root,
        resolve_track,
    )
    from tools.media.match import (  # type: ignore[no-redef]
        Choice,
        Hit,
        Track,
        choose,
        find,
        rank,
        tags_of,
        title_of,
    )
    from tools.media.player import (  # type: ignore[no-redef]
        Player,
        default_player,
        play,
        play_many,
    )
    from tools.media.scanner import (  # type: ignore[no-redef]
        folders,
        natural_key,
        scan,
    )

__all__ = [
    "ACTIONS",
    "Choice",
    "Hit",
    "Player",
    "Track",
    "allowed_suffixes",
    "choose",
    "default_player",
    "describe",
    "find",
    "folders",
    "library_root",
    "natural_key",
    "play",
    "play_many",
    "play_media",
    "rank",
    "resolve_track",
    "scan",
    "tags_of",
    "title_of",
]