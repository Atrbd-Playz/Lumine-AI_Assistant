"""Turning a list of tracks into one queued playlist the player's own window owns.

## The incident this exists for

"Play my vocal songs" launched all nine files at once: nine `os.startfile` calls,
nine player windows or nine simultaneous streams on top of each other, and nothing
audible. `startfile` hands **one** file to the OS and the OS takes it from there --
there is no list to hand over, so the only way to play nine was to start nine.

The fix is not to launch them one after another with a sleep. It is to stop asking
the OS to open nine files and instead ask it to open **one playlist**, because a
playlist is a thing every media player already understands and already queues.
`os.startfile("queue.m3u8")` gives the user's own player the whole ordered list,
and from that moment the *player* owns skip, next, pause and stop -- which is also
the honest version of "she cannot stop it": she hands over a queue and the player
is in charge of it.

Nothing is bundled. No mpv, no VLC, no shim: the same player that was already
going to open the first song opens the list instead.

## Why the file is written outside the library

:mod:`tools.media.library` refuses to *play* a ``.m3u`` -- a playlist is a file of
paths, so honouring one would hand playback a directory that was never checked.
That is still right, and this does not contradict it: **every path in this file
has already been through :func:`library.resolve_track`.** We are not trusting a
playlist, we are writing one from paths we resolved ourselves.

It goes to the app's own directory rather than the user's music folder, because a
file dropped into the library would be picked up by the next scan and offered as a
track. A queue is not part of anybody's collection.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

# Only `ToolError`, and nothing else from this package. `player` imports *this*
# module, so importing `control` or `scanner` here would close a cycle through the
# very module that is meant to depend on this one.
try:
    from ..tools_compat import ToolError
except ImportError:  # running as a top-level module with agent/ on sys.path
    from tools_compat import ToolError  # type: ignore[no-redef]

logger = logging.getLogger("lumine")

#: Where queues are written. Overridable for a machine that wants them elsewhere.
QUEUE_DIR_VAR = "LUMINE_MEDIA_QUEUE_DIR"

#: Extension chosen deliberately: ``.m3u8`` is UTF-8, so a filename with a
#: non-Latin script survives it. ``.m3u`` is read as the local codepage by several
#: players, which turns a Bengali title into mojibake in the player's own list.
QUEUE_SUFFIX = ".m3u8"

#: How many old queues to keep. One per distinct request, pruned, so a long
#: session does not leave hundreds of files in the app's directory.
KEEP = 8

#: The most tracks one queue may carry. The same reasoning as the tool's own
#: `MAX_BULK`: a request with 500 items in it is a request Lumine misunderstood,
#: and a 500-line queue in somebody's player is worse than a refusal.
MAX_TRACKS = 200

MSG_TOO_LONG = "That's too many tracks to queue at once -- pick a folder or a few by name."


def queue_dir() -> Path:
    """Where queues live, created on first use.

    The system temp directory rather than the app's data directory on purpose: a
    queue is a transient hand-off, and the OS is already clearing this one. The
    override exists for a machine whose temp directory is somewhere a media player
    cannot read.
    """
    configured = (os.getenv(QUEUE_DIR_VAR, "") or "").strip().strip('"')
    base = Path(configured).expanduser() if configured else Path(tempfile.gettempdir())
    try:
        target = base / "lumine-queues"
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError:
        # A temp directory that cannot be written to is not a reason to fail a
        # play; the caller's own error handling reports it if this is fatal.
        raise ToolError("I could not prepare a play queue on this computer.") from None


def build(paths: list[Path], titles: list[str] | None = None) -> Path:
    """Write an ordered playlist for ``paths`` and return its path.

    **Ordered exactly as given.** The caller passes natural-sorted tracks, so the
    queue is the order a person sees in a file manager -- which is the whole point
    of a playlist, and the reason this is not a set.

    The filename is a hash of the contents, which makes the operation idempotent:
    the same request twice writes the same file rather than leaving a second copy,
    and a different request leaves exactly one new file for :func:`prune` to
    reason about.
    """
    if not paths:
        raise ToolError("There was nothing to play.")
    if len(paths) > MAX_TRACKS:
        raise ToolError(MSG_TOO_LONG)

    lines: list[str] = ["#EXTM3U"]
    for index, path in enumerate(paths):
        # `#EXTINF` is what gives the player's list a *name* per row rather than a
        # bare filename, and it is why the queue reads as a playlist instead of a
        # column of paths. The duration is -1, which every player reads as "unknown"
        # rather than as a track of minus one second.
        title = ""
        if titles is not None and index < len(titles):
            title = str(titles[index] or "").strip()
        # A newline inside a title would end the directive early and inject a
        # second `#EXTINF`, so it is stripped rather than escaped: there is no
        # escape in this format, and a filename cannot legally contain one anyway.
        title = title.replace("\r", " ").replace("\n", " ")
        lines.append(f"#EXTINF:-1,{title}" if title else "#EXTINF:-1")
        lines.append(str(Path(path).resolve()))

    body = "\n".join(lines) + "\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    target = queue_dir() / f"lumine-{digest}{QUEUE_SUFFIX}"

    try:
        # Written whole, then moved into place, so a player can never open a
        # half-written queue and play four of the nine tracks.
        staging = target.with_suffix(target.suffix + ".part")
        staging.write_text(body, encoding="utf-8")
        os.replace(staging, target)
    except OSError as err:
        raise ToolError(f"I could not prepare a play queue: {err}") from None

    prune(keep=target)
    logger.info("[Media] queued %d track(s) as %s", len(paths), target.name)
    return target


def prune(*, keep: Path | None = None) -> None:
    """Remove old queues, leaving ``keep`` and the newest :data:`KEEP` after it.

    Best-effort by design: a queue that could not be deleted is a file in a temp
    directory, and failing a play because a temp file is locked would be absurd.
    """
    try:
        files = sorted(
            (entry for entry in queue_dir().glob(f"lumine-*{QUEUE_SUFFIX}") if entry.is_file()),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    survivors = 0
    for entry in files:
        # The newest is counted, not skipped around. Counting only the *others*
        # kept `KEEP` more than `KEEP`, which is the kind of off-by-one that reads
        # as a slow leak in the temp directory and is never noticed.
        if keep is not None and entry == keep:
            continue
        survivors += 1
        if survivors >= KEEP:
            try:
                entry.unlink()
            except OSError:
                logger.debug("[Media] could not remove an old queue %s", entry.name, exc_info=True)


def read_titles(path: Path) -> list[str]:
    """The titles in a queue we wrote, in order.

    Used by the tool and the widget to say "queued 9" and mean the nine the user
    would see, rather than re-reading the library and risking a different order.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[str] = []
    for line in lines:
        if line.startswith("#EXTINF:"):
            out.append(line.partition(",")[2].strip())
    return out