"""Playing a file, through whatever the machine already has.

## Why ``os.startfile`` and not a bundled player

Nothing is shipped. The user has a player -- VLC, PotPlayer, Media Player, mpv --
and handing them a second one inside an assistant is worse on every axis: another
150 MB, another thing to configure, another thing that is not the player they
already know. ``startfile`` hands the file to whatever the OS has associated with
the extension, which is the answer to "what does my computer do with a .flac".

## The seam, and why it exists now rather than later

:class:`Player` is the interface and :func:`default_player` is the only
implementation. A VLC or mpv backend is a second implementation and nothing else
-- it takes a path and plays it.

It is written this way from the start because the *hard* part of queue support is
not the launching, it is the process lifecycle: knowing what is playing, asking it
to skip, and stopping it without leaving a window behind. Retrofitting a
``startfile`` call into an interface after that has been built twice is how you end
up with two ways of saying "stop the music", one of which works.

**What ``startfile`` could not do** was queue: it launches one file and the OS takes
it from there, so "play my vocal songs" started all nine at once and the user heard
nothing. :func:`play_many` now writes one ``.m3u8`` and opens **that**, so the
player's own window owns an ordered list -- and skip, pause and stop become that
window's controls rather than something we would have to invent. See
:mod:`tools.media.queue`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Protocol

# At module scope rather than inside `play_many`: `test_import_shapes` fails a
# function-level import on purpose, because it breaks the script shape Tauri runs.
try:
    from . import queue as media_queue
except ImportError:  # running as a top-level module with agent/ on sys.path
    from tools.media import queue as media_queue  # type: ignore[no-redef]

logger = logging.getLogger("lumine")

MSG_NO_OPENER = (
    "I couldn't find anything on this computer that can open that kind of file. "
    "Nothing was played."
)
MSG_NOTHING_PLAYING = "Nothing is playing right now."
MSG_TOO_MANY = (
    "That's a lot of tracks -- tell me which one, or name a folder instead. "
    "Nothing was played."
)

#: A queue longer than this is refused rather than launched. It is not about the
#: OS's ability to open 200 files -- it is that a request with 200 items in it is
#: a request Lumine misunderstood, and answering it is worse than asking.
MAX_QUEUE = 50


class Player(Protocol):
    """What a backend has to be able to do. One method, on purpose."""

    name: str

    def play(self, path: Path) -> None: ...


class SystemPlayer:
    """The default handler for the file's extension. Ships with the OS.

    Named for what it is rather than for which API calls it: the module is not
    going to stay ``startfile``, because the two other platforms spell this
    differently and a subclass that overrode three methods would be worse than
    one that overrode one.
    """

    name = "system"

    def play(self, path: Path) -> None:
        if sys.platform == "win32":
            if not hasattr(os, "startfile"):  # pragma: no cover - Windows always has it
                raise OSError("no shell execute on this platform")
            os.startfile(str(path))  # type: ignore[arg-type]
            return
        if sys.platform == "darwin":
            opener = shutil.which("open")
            if not opener:
                raise OSError("open is not available")
            subprocess.run([opener, str(path)], capture_output=True, timeout=10, check=True)
            return
        opener = shutil.which("xdg-open")
        if not opener:
            raise OSError("xdg-open is not available")
        subprocess.run([opener, str(path)], capture_output=True, timeout=10, check=True)


def default_player() -> Player:
    """The player in use. The one place this is decided.

    A single function so that a future backend is chosen in one place and every
    caller gets it -- rather than a ``startfile`` call typed at each site, which
    is what makes "switch to VLC" a search-and-replace across the tool.
    """
    return SystemPlayer()


def play(path: Path, *, player: Player | None = None) -> str:
    """Play one file. Raises :class:`OSError` rather than swallowing it.

    The caller turns a failure into a sentence. Returning ``""`` on failure would
    be the shape that produces "I played it" for a track that never opened, which
    is the one claim here the user would notice immediately.
    """
    active = player or default_player()
    try:
        active.play(Path(path))
    except OSError as exc:
        raise OSError(str(exc) or MSG_NO_OPENER) from None
    return active.name


def play_many(
    paths: list[Path],
    *,
    titles: list[str] | None = None,
    player: Player | None = None,
) -> list[str]:
    """Queue several files, in order, and report what is in the queue.

    ## Why this writes a playlist rather than launching N times

    It launched N times, and that was an incident a user reported: *"play my vocal
    songs" played all nine songs at once, no queue or anything, I couldn't able to
    hear anything.* ``startfile`` takes **one** file -- the OS takes it from there
    and there is no list to hand over -- so the only way to play nine was to start
    nine, and nine simultaneous players is silence.

    So this no longer launches anything N times. It writes one ``.m3u8`` from the
    already-validated paths and opens **that**, and the player's own window owns the
    queue from that moment -- which is also what makes skip, pause and stop work:
    they are the player's controls over its own list, not something we would have
    to invent.

    Nothing is bundled. The player that was going to open the first song opens the
    list instead, which is the same player on the same machine.

    **One track is opened directly**, never through a playlist: a single file in a
    queue is one more indirection for no benefit, and it keeps the common case on
    the path with the fewest ways to go wrong.
    """
    if len(paths) > MAX_QUEUE:
        raise OSError(MSG_TOO_MANY)
    if not paths:
        return []
    active = player or default_player()
    if len(paths) == 1:
        active.play(Path(paths[0]))
        return [str(paths[0])]

    playlist = media_queue.build([Path(path) for path in paths], titles)
    # The playlist, not the tracks: this is the single hand-off. The return value
    # is the queue's *contents* rather than the playlist path, so a caller can say
    # "9 tracks queued" without opening the file to find out.
    active.play(playlist)
    return [str(path) for path in paths]