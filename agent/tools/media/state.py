"""What Lumine last played, so "the next track" means something.

`os.startfile` hands one file to the OS and the OS owns it from there. That rules
out skipping, pausing and stopping -- the player has the window and we do not
have a handle to it. It does **not** rule out playing the next track, because
playing a file is exactly the one thing this module can do.

So "play the next one" is answered by playing the next *file*, from the same
folder, after the one Lumine played last. That is a real answer to what the user
asked rather than a refusal, and it is the difference between the feature
working and the feature existing.

## Why a module-level dict

The state is per *worker process*, not per room, and that is a deliberate
limitation rather than an oversight. Two rooms dispatched to one worker would
share it. The alternative -- a per-session dict reached through the tool context
-- is the right answer for a product with concurrent calls, and it is more code
than the thing it buys. Stated here because the next reader will otherwise assume
this was scoped on purpose.

A ``Lock`` because two LiveKit rooms *can* reach one worker, and a
read-modify-write on a shared dict is exactly the shape that interleaves badly.
The critical section is a dict assignment.
"""

from __future__ import annotations

import threading
from pathlib import Path

_LOCK = threading.Lock()
#: The last file handed to the player, and the folder it came from.
#:
#: The folder is kept because "the next track" is only meaningful relative to
#: something: the next file in the album, not the next file in a natural sort of
#: the whole library. Without it, "next" after any track would restart the library
#: from its first folder.
_LAST: dict[str, str] = {"path": "", "folder": ""}


def remember(path: object) -> None:
    """Record a file as the one now playing."""
    try:
        target = Path(str(path))
        folder = target.parent.name if target.parent and target.parent.name else ""
    except (OSError, ValueError, TypeError):
        # A path this cannot parse is not worth a failed play, and remembering it
        # would be worse: `remember` is called after the hand-off.
        return
    with _LOCK:
        _LAST["path"] = str(target)
        _LAST["folder"] = folder


def last_played() -> str:
    """The file Lumine played last this process, or ``""``."""
    with _LOCK:
        return _LAST["path"]


def last_folder() -> str:
    """The folder that file was in, or ``""``."""
    with _LOCK:
        return _LAST["folder"]


def step(tracks: list, path: object, *, forward: bool = True):
    """The track beside ``path`` in ``tracks``.

    ``tracks`` is the ordered list the same folder produced -- natural order from
    :mod:`scanner` -- so "next" is the next *track*, in the order a person sees in
    Explorer.

    Wraps around, because a playlist that stops at the end is a playlist whose
    last track cannot be followed, and "next" from the end almost always means
    "back to the start".

    Returns ``None`` when the answer is not knowable -- an empty list, or a ``path``
    that is not in it. Not an error and not a guess: returning the first track
    would silently restart a playlist the user thought they had finished.
    """
    if not tracks:
        return None
    names = [str(getattr(item, "path", item) or "") for item in tracks]
    try:
        index = names.index(str(path))
    except ValueError:
        return None
    step_size = 1 if forward else -1
    # `modulo` rather than a branch, so both directions wrap through one
    # expression and neither can be got wrong at the boundary.
    return tracks[(index + step_size) % len(tracks)]


def forget() -> None:
    """Clear the record. For tests, and for a worker that has just restarted."""
    with _LOCK:
        _LAST["path"] = ""
        _LAST["folder"] = ""