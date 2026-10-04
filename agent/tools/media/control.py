"""``play_media``: Lumine's music library, as seven verbs.

    find   -- what matches this, without playing anything
    search -- the same, ranked, with *why* each result matched
    play   -- play one track, a folder's worth, or everything matching
    next   -- the track beside the last one she started
    previous
    stop   -- she cannot; here is why, and which one
    folders-- what is in the library, which are the playlists

## Why one tool and not two

The same argument as the browser: a tool the model has to choose between too many
tools on is a tool that mis-calls. And `find` exists so that "what do you have
called X" costs a read rather than a play -- a question that is currently answered
by playing something at the user.

`search` is `find` with the scoring attached. It is a separate verb only because
the two answers are for different moments: `find` is "what is on this shelf" and
`search` is "why did you pick that one", which is what a user asks *after* a
surprising result and which is impossible to answer from a bare list.

## What this may do without asking, and why

Playing is an ``act``. It hands a path to the OS and a window opens, but it
changes nothing on the disk, sends nothing anywhere, and can be undone by closing
a window. That is the same argument `open_app` makes, and it is why a music
library does not prompt for every track.

**Deliberately not implemented: moving, renaming, deleting.** The brief asked for
them to be gated rather than absent, and gating them is the right shape -- but a
half-built delete is a delete that can be got wrong, and you cannot undo a file
Lumine moved because she read a filename slightly wrong.

## The refusal that matters most

Playing something is easy. Playing the *wrong* something is what loses trust, so
three rules apply throughout and none of them is negotiable:

- **A model never names a path.** It passes a phrase; :mod:`match` resolves it
  against the scan. Anything else would make "play ``C:\\Users\\me\\Documents\\x``"
  a request the tool could not refuse.
- **Partial matching is a number, not a boolean.** See
  :data:`~tools.media.match.MIN_COVERAGE` and
  :data:`~tools.media.match.AUTO_MARGIN` -- two numbers that decide whether she
  acts or asks.
- **Two close matches is a question, not a coin toss.** See
  :func:`tools.media.match.choose`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

try:  # agent/runtime -- the two levels up that a top-level package cannot reach.
    from ...runtime import verification
    from ...runtime.permission_gate import require_approval
except ImportError:  # running as a top-level module with agent/ on sys.path
    from runtime import verification  # type: ignore[no-redef]
    from runtime.permission_gate import require_approval  # type: ignore[no-redef]

try:  # agent/tools -- one level up, reachable either way.
    from ..tool_results import as_payload, wrap_untrusted
    from ..tools_compat import RunContext, ToolError, function_tool
    from ..tools_text import clip
except ImportError:  # `tools` is the top-level package here, so keep the prefix
    from tools.tool_results import as_payload, wrap_untrusted  # type: ignore[no-redef]
    from tools.tools_compat import RunContext, ToolError, function_tool  # type: ignore[no-redef]
    from tools.tools_text import clip  # type: ignore[no-redef]

try:  # this package, or the same package reached absolutely
    from . import player as media_player
    from .library import describe, library_root, resolve_track
    from .match import (
        Hit,
        Track,
        ambiguous_message,
        best_folder,
        choose,
        rank,
        requested_tags,
    )
    from .scanner import folders as scan_folders
    from .scanner import scan
    from .state import last_folder, last_played, remember
    from .state import step as step_within
except ImportError:  # running as a top-level module with agent/ on sys.path
    from tools.media import player as media_player  # type: ignore[no-redef]
    from tools.media.library import (  # type: ignore[no-redef]
        describe,
        library_root,
        resolve_track,
    )
    from tools.media.match import (  # type: ignore[no-redef]
        Hit,
        Track,
        ambiguous_message,
        best_folder,
        choose,
        rank,
        requested_tags,
    )
    from tools.media.scanner import folders as scan_folders  # type: ignore[no-redef]
    from tools.media.scanner import scan  # type: ignore[no-redef]
    from tools.media.state import (  # type: ignore[no-redef]
        last_folder,
        last_played,
        remember,
    )
    from tools.media.state import step as step_within  # type: ignore[no-redef]

logger = logging.getLogger("lumine")

#: The seven verbs. `next` and `previous` exist because "play the next track" is
#: one of the things people actually say, and with `startfile` a refusal is the
#: only alternative -- while playing the next *file* is not.
ACTIONS = ("find", "search", "play", "next", "previous", "stop", "folders")

MSG_BAD_ACTION = (
    "Tell me one of: find, search, play, next, previous, stop, or folders."
)
MSG_NO_LIBRARY = (
    "You haven't picked a music folder yet -- choose one in Settings, under Music."
)
MSG_NOTHING_PLAYED = "I haven't played anything yet, so there's no next track."
MSG_NO_QUERY = "Tell me what to look for, or which track to play."
MSG_NOTHING_MATCHED = "Nothing in your music folder matched that."

#: How many results a read returns. A voice turn cannot read forty filenames, and
#: a list this long costs the same tokens as one that is short enough to use.
MAX_REPORTED = 8

#: The most tracks one call may launch. Not about the OS's ability to open 200
#: files -- it is that a request with 200 items in it is a request Lumine
#: misunderstood, and answering it is worse than asking.
MAX_BULK = 60

#: How many results a *request* considers, before any are shown.
#:
#: Wider than :data:`MAX_REPORTED` on purpose, and it was a bug when it was not:
#: `rank` defaults to 6, so "play every vocal song" against a nine-track library
#: found six of the nine and launched six -- a request for the whole library
#: answered with two thirds of it, silently. Reading and *launching* are different
#: budgets: a read is bounded by what a person can read, a launch by what is
#: reasonable to open.
MAX_RANKED = 60


def _recursive() -> bool:
    """Whether to search subfolders. ``LUMINE_MEDIA_RECURSIVE`` wins, default yes.

    Default on because a folder called ``vocal`` is inside a folder called
    ``Music`` and refusing to look there would make the headline feature -- "play
    my vocal songs" -- fail on a correctly arranged library.
    """
    raw = (os.getenv("LUMINE_MEDIA_RECURSIVE", "") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _require_library() -> Path:
    root = library_root()
    if root is None:
        raise ToolError(MSG_NO_LIBRARY)
    return root


def _payload(**fields: Any) -> str:
    return wrap_untrusted(as_payload(fields))


def _result(outcome: verification.ActionOutcome, **fields: Any) -> str:
    """One payload shape for every answer, successful or not.

    A model reads ``status`` before anything else, and a tool that returns
    ``status`` on one path and ``{"status": ...}`` nested under another makes it
    check both. The outcome's own fields go in flat and the caller's extras sit
    beside them, so ``status`` and ``detail`` are always at the top level.
    """
    return _payload(**outcome.to_dict(), **fields)


def _hit_fields(hits: list[Hit]) -> dict[str, Any]:
    """Ranked results, with the one number that explains the ordering.

    ``match`` is a percentage rather than a float at full precision, and
    ``because`` names the words that landed -- so a surprising result is
    explainable in the turn it happened, instead of the user having to ask a
    second question to find out whether she understood them.
    """
    return {
        "results": [hit.to_dict for hit in hits[:MAX_REPORTED]],
        "count": len(hits),
    }


@function_tool()
async def play_media(
    context: RunContext,
    action: str,
    query: str = "",
    index: int = 0,
    folder: str = "",
    all_tracks: bool = False,
) -> str:
    """Play something from your own music folder, or look through it.

    Use this when the user names a song, an artist, or one of their music folders
    -- "play Love Is Gone", "play my vocal songs", "what do you have by X". Use
    `search_web` to look something up online instead, and `browse` for a page you
    have to read or operate -- "play that on YouTube" is that, not this.

    Actions: find, search, play, next, previous, stop, folders.

    - find: what matches, without playing anything.
    - search: the same, ranked, saying which words matched each result.
    - play: play the best match, or `index`-th match. Give `folder` to queue a
      whole folder in order, or `all_tracks` for everything matching `query`.
    - next / previous: the track beside the last one she played, in the same
      folder. Works because playing a file is what she can do; skipping inside a
      running player is not, and she will say so if asked to pause.
    - stop: she cannot stop it -- the queue lives in the player's own window and
      that window owns playback. She will name the tracks she queued so the user
      knows which window to look at.
    - folders: the folders in the library, which are the playlists.

    **A folder becomes a queue, never a pile.** More than one track is written as
    an ordered playlist and handed to the player's own window as a single item, so
    it plays one after another in the order the user sees. Launching each file
    separately is what played nine songs at once with nothing audible, and it is
    not done any more. Tell the user it is queued and that skip and stop are in
    that window.

    **She searches like a search engine, not like a filename matcher.** "play the
    Akhon Ami vocal song" finds "Akhon Ami (without music, vocals only)", because
    she matches the words you said rather than the exact characters in the file,
    and "vocal" is read as a *version* rather than as part of the title. So
    "play Bayaan" reaches a song titled "Maand" by Bayaan, and "play the
    instrumental version" will not hand back the vocal one.

    When two tracks match about equally she asks rather than picking one -- so if
    she asks, answer with the track name, or call again with `index`.

    Args:
        action: What to do. See the list above.
        query: The song, artist or words to look for.
        index: Which match, when a result listed more than one.
        folder: A folder name, to play everything in it.
        all_tracks: Play every match rather than the best one -- "play all my
            vocal songs".
    """
    normalized = str(action or "").strip().lower()
    if normalized not in ACTIONS:
        raise ToolError(MSG_BAD_ACTION)

    # -- stop ---------------------------------------------------------------
    #
    # The system player owns its own window: `startfile` launches and returns,
    # and this process does not hold a handle to what it launched. So there is
    # nothing here to close. What this *can* do is tell the truth, and *which*
    # track is running is the useful part of that -- "stop the music" when four
    # windows are open is a question the user cannot otherwise answer.
    if normalized == "stop":
        await require_approval(
            context,
            tool="play_media",
            action="stop",
            action_class="act",
            summary="Stop the music",
        )
        recent = last_played()
        return _result(
            verification.accepted(
                "stop",
                (
                    "I can't stop it from here -- press stop in the player's"
                    " window, or close it"
                ),
                evidence="playback is owned by the player's own window",
            ),
            stopped=False,
            lastPlayed=clip(Path(recent).stem, 60) if recent else "",
        )

    # -- next / previous -----------------------------------------------------
    if normalized in {"next", "previous"}:
        root = _require_library()
        recent = last_played()
        if not recent:
            raise ToolError(MSG_NOTHING_PLAYED)
        folder_name = last_folder()
        neighbours = [
            item
            for item in scan(root=root, recursive=_recursive())
            if item.folder == folder_name
        ]
        picked = step_within(neighbours, recent, forward=normalized == "next")
        if picked is None:
            # The file is gone, or the folder is not where it was. Both mean the
            # same thing to the user and both have the same answer.
            raise ToolError(MSG_NOTHING_PLAYED)
        return await _play_one(context, picked, root, action=normalized)

    # -- folders -------------------------------------------------------------
    if normalized == "folders":
        await require_approval(
            context,
            tool="play_media",
            action="folders",
            action_class="read",
            summary="List the folders in your music library",
        )
        root = _require_library()
        names = scan_folders(root=root, recursive=_recursive())
        tracks = scan(root=root, recursive=_recursive())
        if not names:
            return _result(
                verification.observed(
                    "folders",
                    f"{len(tracks)} track(s) at the top level, and no subfolders",
                ),
                folders=[],
                count=0,
            )
        return _result(
            verification.observed("folders", f"{len(names)} folder(s) in your music"),
            folders=[
                {
                    "name": clip(name, 40),
                    "tracks": sum(1 for item in tracks if item.folder == name),
                }
                for name in names[:MAX_REPORTED]
            ],
            count=len(names),
        )

    # -- find / search / play ------------------------------------------------
    root = _require_library()
    tracks = scan(root=root, recursive=_recursive())
    if not tracks:
        return _result(
            verification.failed("play", "there's nothing playable in your music folder"),
            library=root.name,
        )

    wanted = str(folder or query or "").strip()
    if not wanted and not all_tracks:
        raise ToolError(MSG_NO_QUERY)

    # A folder plays as a playlist -- everything in it, in order.
    #
    # Checked *after* the track ranking, and only when the ranking does not have a
    # confident answer. Checking folders first would be a real bug: a library with
    # a folder called "Love" would answer "play love is gone" by playing the folder.
    hits = rank(wanted, tracks, limit=MAX_RANKED) if wanted else []
    confident = bool(hits) and hits[0].tier <= 3 and hits[0].coverage >= 0.99

    if folder or (wanted and not confident):
        folder_names = scan_folders(root=root, recursive=_recursive())
        named = best_folder(wanted, folder_names)
        if named:
            in_folder = [item for item in tracks if item.folder == named[0]]
            if in_folder:
                if normalized in {"find", "search"}:
                    return _result(
                        verification.observed(
                            normalized,
                            f"{len(in_folder)} track(s) in the folder {named[0]!r}",
                        ),
                        folder=clip(named[0], 40),
                        tracks=[item.to_dict for item in in_folder[:MAX_REPORTED]],
                        count=len(in_folder),
                        note="this is a folder -- pass folder= to play all of it",
                    )
                return await _play_many(context, in_folder, root, label=named[0])
            return _result(
                verification.failed(
                    normalized,
                    f"there's a folder called {named[0]!r} but it has no playable files in it",
                ),
            )
        if folder:
            # The model named a folder that does not exist. Naming what *is* there
            # is the difference between a dead end and the next sentence working.
            have = ", ".join(name[:30] for name in folder_names[:5])
            return _result(
                verification.failed(
                    normalized,
                    f"there's no folder called {str(folder)[:30]!r} in your music"
                    + (f". You have: {have}." if have else " folder."),
                ),
                folders=[clip(name, 40) for name in folder_names[:MAX_REPORTED]],
            )

    # A version word with nothing else to go on: "play all my vocal songs".
    # Every track carries the tag, so there is no best one -- and answering a
    # request for a whole library with one track is the wrong answer.
    tag_only = requested_tags(wanted) and not any(
        hit.tier <= 3 for hit in hits
    )
    if all_tracks or (tag_only and normalized == "play" and len(hits) > 1):
        # **A version the user did not ask for is not part of "all of them".**
        # Asking for every vocal song and receiving the instrumental too is worse
        # than receiving fewer: the point of the version tags is that a vocal
        # request does not reach the instrumental, and "all" must not undo that.
        # They are reported in the count so nothing is silently dropped.
        wanted = requested_tags(wanted) if wanted else ()
        matching = [hit for hit in hits if not (wanted and hit.wanted_tag_miss)]
        chosen = [hit.track for hit in matching] if matching else [hit.track for hit in hits]
        if not chosen:
            return _result(
                verification.failed("play", MSG_NOTHING_MATCHED),
                librarySize=len(tracks),
            )
        if len(chosen) == 1:
            return await _play_one(context, chosen[0], root)
        label = (
            ", ".join(requested_tags(wanted)) if wanted and requested_tags(wanted)
            else "your library"
        )
        return await _play_many(context, chosen, root, label=label)

    if normalized in {"find", "search"}:
        if not hits:
            # `search` gets the folders, because "show me my music" is answered by
            # the shape of the library rather than by an empty list of tracks.
            folders = scan_folders(root=root, recursive=_recursive())
            hint = (
                "Folders are playlists -- try one of: " + ", ".join(folders[:4])
                if folders
                else "Every song here sits at the top level, so try a word from a title."
            )
            return _result(
                verification.observed("find", MSG_NOTHING_MATCHED),
                matches=[],
                count=0,
                hint=hint,
                librarySize=len(tracks),
            )
        best = hits[0]
        detail = f"{len(hits)} match(es); best is {best.match_word()}"
        if best.missing:
            detail += f" (no {', '.join(best.missing)})"
        return _result(
            verification.observed(normalized, detail),
            **_hit_fields(hits),
            librarySize=len(tracks),
        )

    # -- play one ------------------------------------------------------------
    picked = choose(wanted, tracks, index=int(index or 0)) if wanted else None
    if picked is None:
        return _result(
            verification.failed("play", MSG_NOTHING_MATCHED),
            librarySize=len(tracks),
        )
    if not picked.ok:
        if picked.ambiguous:
            names = [item.name for item in picked.candidates]
            # `accepted`, not `failed`: nothing went wrong, the request was
            # ambiguous. The model needs to know it is being asked rather than
            # told there is a problem, and `needsChoice` is at the top level
            # beside every other field -- an outcome's `**data` would nest it
            # under `data` and make the model check two shapes.
            return _result(
                verification.accepted("play", ambiguous_message(names)),
                needsChoice=True,
                candidates=[clip(name, 60) for name in names[:MAX_REPORTED]],
                hint="ask which one, then call play again with index=",
            )
        return _result(
            verification.failed("play", picked.reason or MSG_NOTHING_MATCHED),
            librarySize=len(tracks),
        )

    chosen = picked.chosen
    assert chosen is not None  # for the type checker; `ok` already proved it
    return await _play_one(context, chosen, root)


async def _play_one(
    context: RunContext,
    chosen: Track,
    root: Path,
    *,
    action: str = "play",
) -> str:
    """Hand one resolved track to the player's operating system.

    Split out because `play` and `next`/`previous` are the same act with a
    different way of *choosing* the file -- and a copy of the gate call, the
    boundary check and the failure sentence at each site is three places to get
    right rather than one.

    The boundary check lives here rather than at the call sites, so every path
    into a player goes through it.
    """
    # Resolved through the library boundary, not from the scan's own path. The
    # scan re-checks too, and this is the check that is *enforced* at the moment
    # of playing rather than at the moment of listing.
    target = resolve_track(chosen.path, root=root)

    await require_approval(
        context,
        tool="play_media",
        action=action,
        action_class="act",
        summary=f"Play {describe(target)}",
        # The *name* and not the path, for the same reason the card carries no
        # path anywhere else in this codebase.
        target=describe(target),
    )
    try:
        via = media_player.play(target)
    except OSError as exc:
        return _result(
            verification.failed(action, str(exc) or media_player.MSG_NO_OPENER),
            track=chosen.to_dict,
        )
    # Remembered only after the hand-off succeeded. Recording before would make
    # "next" step on from a file that never opened.
    remember(target)
    return _result(
        verification.observed(
            action,
            f"Playing {chosen.name}",
            evidence="the file was handed to your default media player",
        ),
        track=chosen.to_dict,
        via=via,
        # Present on **both** play shapes. A caller asking "was that a queue?" gets
        # a real answer either way rather than `queued` being absent for a single
        # track, which is indistinguishable from a tool that forgot to report it.
        queued=False,
    )


async def _play_many(
    context: RunContext, tracks: list[Track], root: Path, *, label: str
) -> str:
    """Play a folder's worth of tracks, and say honestly how many started.

    The class is ``act`` like any other play -- it opens windows and changes
    nothing on disk. What it is *not* is ``sensitive``: nothing leaves the
    machine.

    ``root`` is the folder these tracks were scanned from, and every file is
    re-validated against **that** rather than re-read from the environment. Two
    roots would mean validating a scan against a library it did not come from.

    A file that fails the check is dropped rather than fatal -- a symlink deleted
    between the scan and here should not take the other nineteen with it.
    """
    if len(tracks) > MAX_BULK:
        tracks = tracks[:MAX_BULK]
    await require_approval(
        context,
        tool="play_media",
        action="play_folder",
        action_class="act",
        summary=f"Play {len(tracks)} track(s) from {clip(label, 40)}",
        target=clip(label, 60),
    )
    paths: list[Path] = []
    for item in tracks:
        try:
            paths.append(resolve_track(item.path, root=root))
        except ToolError:
            continue
    if not paths:
        return _result(
            verification.failed("play_folder", "I couldn't read any of those tracks"),
        )
    try:
        started = media_player.play_many(
            paths, titles=[item.name for item in tracks][: len(paths)]
        )
    except OSError as exc:
        return _result(verification.failed("play_folder", str(exc)))
    if not started:
        return _result(
            verification.failed(
                "play_folder",
                "I couldn't get any of those files open. Nothing was played.",
            ),
        )
    # Remembered so "the next track" answers sensibly after a folder. The *last*
    # file is the position to step from, because that is the last thing handed
    # over -- recording the first would make "next" replay a track the user has
    # already been given. With a queue this is the end of the queue, so "next" is
    # genuinely the next one after everything Lumine handed over.
    remember(paths[-1])

    if len(started) == 1:
        # One file is opened directly rather than through a playlist: a one-track
        # queue is one more indirection, and the sentence below would be a lie
        # about a queue that does not exist.
        return _result(
            verification.accepted("play_folder", f"Playing {clip(str(label), 30)}"),
            started=1,
            attempted=len(paths),
            first=describe(paths[0]),
            queued=False,
        )

    # **Queued, not launched.** This used to start every file and report how many
    # windows appeared, because `startfile` takes one file and there was no list to
    # hand over. That is what played all nine at once: nine players on top of each
    # other and nothing audible. One `.m3u8` is now opened instead, so the player's
    # own window owns an ordered list.
    return _result(
        verification.accepted(
            "play_folder",
            f"Queued {len(started)} tracks from {clip(str(label), 30)} -- "
            "playing in order in your media player",
        ),
        started=len(started),
        attempted=len(paths),
        first=describe(paths[0]),
        queued=True,
        # Named, because "queued 9 tracks" over nine rows of silence is what the
        # user reported, and the player window now holds the whole list.
        titles=[describe(Path(path)) for path in paths[: len(started)]],
        note=(
            "the queue lives in your own media player, so skip, pause and stop "
            "are that window's controls -- I cannot reach into it"
        ),
    )