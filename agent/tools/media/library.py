"""The library boundary: which files on this computer Lumine may play.

A path arrives from a language model. That is the whole threat model, and it is
worth stating plainly: without a boundary, "play ``C:\\Windows\\System32\\config\\SAM``"
is a perfectly reasonable thing for a model to produce after a misunderstood
request. Nothing about the *model* prevents it.

So the boundary is here, in code, and it is deliberately narrow:

- **Only inside one root** -- the folder the user picked in Settings. Not
  Downloads-and-Pictures-and-Desktop the way attachments are, because a music
  library is one place a person deliberately designates and can revoke by
  changing one field.
- **Resolved, then compared.** ``Path.resolve`` first, which collapses ``..``,
  follows symlinks, and makes the comparison against the root meaningful.
  Checking the string and then resolving is the classic way to write a check
  that passes.
- **Extension allowlist.** Media and nothing else. Deliberately absent: archives
  (a container for everything excluded here), anything executable or scriptable,
  and every "unknown" -- an extension not on this list is refused, because a
  whitelist that fails open is not a whitelist.
- **The file is played, never executed as a program.** :mod:`player` hands a path
  to the OS default handler and that is the whole of what happens to it here.

**Nothing here reads file contents.** A filename and a size are the only facts
this module will produce, and they reach the model and the approval card. That is
what makes "let her look at your music folder" a reasonable thing to say yes to:
she can see what it is called, and nothing about what is in it.

The shape is deliberately the one `messaging/whatsapp_files.py` already uses for
attachments. Two path resolvers in one product is how one of them forgets the
``resolve`` step, so this is a second *consumer* of that shape rather than a
second implementation of the idea.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

try:
    from ..tools_compat import ToolError
    from ..tools_text import clip
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_compat import ToolError
    from tools_text import clip

#: The saved document, read at module scope rather than inside the function that
#: wants it -- `test_import_shapes` fails a function-level relative import on
#: purpose, because it breaks the script shape the worker actually runs in.
try:
    from ...settings.config_store import effective_document
except ImportError:  # running as a top-level module with agent/ on sys.path
    try:
        from settings.config_store import effective_document  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover - only without a config store at all
        def effective_document():  # type: ignore[misc]
            """No configuration store reachable: nothing to read, and no crash."""
            return {}, "", []

#: The environment override for the library root, for a machine configured by
#: file rather than by the Settings screen. **Underneath** the document, not
#: instead of it -- see `library_root`.
LIBRARY_VAR = "LUMINE_MEDIA_LIBRARY"

#: Where the Settings screen writes the folder. Declared here rather than reached
#: for by key so `library_root` and `tools_permission._media_mode` cannot disagree
#: about the shape of the section: one names it, both read it.
DOCUMENT_SECTION = "media"
DOCUMENT_KEY = "library"

#: Media Lumine may play. Audio and video, and nothing else.
#:
#: `.mkv` and `.avi` are here because they are ordinary video containers people
#: actually have, and `.wma` because Windows wrote it by default for years.
#: Absent on purpose: anything that can execute (`.exe`, `.bat`, `.cmd`, `.ps1`,
#: `.vbs`, `.js`), archives, and playlists -- a playlist is a file of *paths*, so
#: honouring one would hand playback a directory this module never checked.
AUDIO_SUFFIXES = frozenset({".mp3", ".m4a", ".flac", ".wav", ".opus", ".ogg", ".wma", ".aac"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv"})
ALLOWED_SUFFIXES = AUDIO_SUFFIXES | VIDEO_SUFFIXES

#: Directories never walked, whatever they are named. A music library is a folder
#: of media and these are three ways a media folder stops being that -- and a
#: symlink or a junction here is the shape an escape takes in practice.
SKIP_DIRS = frozenset({"$recycle.bin", "system volume information", ".git", "node_modules"})

MSG_NO_LIBRARY = (
    "I don't have a music folder yet. Pick one in Settings, under Music."
)
MSG_NOT_A_FILE = "I couldn't find that in your music folder. Nothing was played."
MSG_OUTSIDE = "I can only play things inside your music folder. Nothing was played."
MSG_BAD_TYPE = "That's not a kind of media file I can play. Nothing was played."
MSG_IS_URL = (
    "I can only play a file that's already on this computer. "
    "Nothing was played."
)


def allowed_suffixes() -> frozenset[str]:
    """The playable extensions, read from the environment or the default set.

    An extension may only be **removed** from the set, never added to it, by a
    value that is not already on this list. A media folder can hold anything; the
    allowlist is the thing standing between a `.lnk` and `startfile`, so widening
    it from a settings file would remove the only barrier that matters.
    """
    raw = (os.getenv("LUMINE_MEDIA_EXTENSIONS", "") or "").strip()
    if not raw:
        return ALLOWED_SUFFIXES
    narrowed = set()
    for part in raw.split(","):
        piece = part.strip().casefold()
        if not piece:
            continue
        if not piece.startswith("."):
            piece = "." + piece
        if piece in ALLOWED_SUFFIXES:
            narrowed.add(piece)
    return frozenset(narrowed) or ALLOWED_SUFFIXES


def library_root() -> Path | None:
    """The configured library folder, resolved, or ``None`` when there is none.

    ``None`` rather than an empty list so "no library configured" is
    distinguishable from "a library with nothing in it" -- one is a settings step
    the user has not done, the other is a search that found nothing, and they
    need different sentences.

    ## The saved document is read first, and that ordering was a real bug

    This read only ``LUMINE_MEDIA_LIBRARY`` at first, and the Music settings page
    writes ``media.library`` into the saved document. Nothing ever bridged the two,
    so **choosing a folder in the app did nothing for the worker**: five consecutive
    voice requests came back "You haven't picked a music folder yet" after the user
    had done exactly that, and the tool was visibly *enabled* the whole time --
    which is the worst shape of this bug, because the Tools page said the capability
    was on while every call failed.

    The environment variable is now the *fallback*, for a machine configured by
    file, and it sits underneath the document for the same reason every other key in
    this product does: a settings screen has to be able to write an answer, and a
    file edit is not somewhere a person looks.
    """
    for raw in (_document_library(), os.getenv(LIBRARY_VAR, "") or ""):
        text = str(raw or "").strip().strip('"')
        if not text:
            continue
        try:
            root = Path(text).expanduser()
            if not root.is_dir():
                continue
            return root.resolve()
        except (OSError, ValueError, RuntimeError):
            # An unusable path is no library rather than a crash: a settings file
            # with a bad path in it should stop the feature, not the worker. The
            # loop continues, so a stale document value falls through to the
            # environment rather than silently disabling a configured library.
            continue
    return None


def _document_library() -> str:
    """``media.library`` from the saved document, or ``""``.

    Read defensively and in a ``try``: a missing, malformed or unreadable document
    is "no opinion", and a music library must not be the thing that stops the
    worker starting.
    """
    try:
        document, _source, _diagnostics = effective_document()
    except Exception:  # noqa: BLE001 - "cannot read it" means "no opinion"
        return ""
    if not isinstance(document, Mapping):
        return ""
    section = document.get(DOCUMENT_SECTION)
    if not isinstance(section, Mapping):
        return ""
    value = section.get(DOCUMENT_KEY)
    return str(value or "") if value is not None else ""


def _resolved_root(root: Path | None) -> Path | None:
    """The root to compare against, resolved, or ``None`` when there is none.

    **Both sides of the comparison have to be resolved**, and that is the whole
    reason this helper exists rather than a bare ``root if root else
    library_root()``. :func:`library_root` resolves what it reads, but a *passed*
    root did not, so a caller handing in an unresolved path got a silent failure:
    the child resolves, the root does not, `root in child.parents` is False, and
    every file in a perfectly good library is reported as outside it.

    That is not hypothetical — it is what `scan(root=...)` did until this was
    written, on a Windows temp directory whose resolved form carries the short
    `8.3` name. It failed silently and looked exactly like an empty library.
    """
    candidate = root if root is not None else library_root()
    if candidate is None:
        return None
    try:
        return Path(candidate).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return None


def _inside(child: Path, root: Path) -> bool:
    """Whether ``child`` is ``root`` or lives under it. Both must be resolved."""
    try:
        return child == root or root in child.parents
    except (OSError, ValueError):
        # A path that cannot be compared is not a path we will hand to a file
        # picker. On Windows this is reachable with a reserved device name.
        return False


def is_inside_library(candidate: Path, root: Path | None = None) -> bool:
    """Whether a path is playable, judged the way :func:`resolve_track` does.

    Split out because the library walk needs the same judgement for every file it
    finds, and a scan that trusted its own ``rglob`` rather than re-checking each
    result would be a scan that trusted symlinks.
    """
    resolved_root = _resolved_root(root)
    if resolved_root is None:
        return False
    try:
        resolved = Path(candidate).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return False
    return _inside(resolved, resolved_root)


def resolve_track(raw: object, *, root: Path | None = None) -> Path:
    """The validated absolute path of a playable file, or a refusal.

    Returns the ``Path`` rather than a bool so the caller has something to hand
    the player, and raises :class:`ToolError` with a sentence a person can act on
    for every refusal. "Pick a folder in Settings" is advice; "invalid path" is
    not.
    """
    text = str(raw or "").strip().strip('"')
    if not text:
        raise ToolError(MSG_NOT_A_FILE)

    lowered = text.casefold()
    if lowered.startswith(("http://", "https://", "ftp://")):
        raise ToolError(MSG_IS_URL)

    resolved_root = _resolved_root(root)
    if resolved_root is None:
        raise ToolError(MSG_NO_LIBRARY)

    try:
        # `strict=False` so a missing file produces a path we can then test,
        # rather than an exception at resolution time. Everything after this is
        # about the *resolved* form, which is the only form that means anything.
        candidate = Path(text).expanduser().resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        raise ToolError(MSG_NOT_A_FILE) from None

    if not _inside(candidate, resolved_root):
        # The load-bearing line. `..`, an absolute path and a symlink pointing
        # out of the library all arrive here, and all three are judged by where
        # they *land* rather than by what they were written as.
        raise ToolError(MSG_OUTSIDE)

    if candidate.suffix.casefold() not in allowed_suffixes():
        raise ToolError(MSG_BAD_TYPE)

    try:
        if not candidate.is_file():
            raise ToolError(MSG_NOT_A_FILE)
        if candidate.stat().st_size <= 0:
            # A zero-byte file is a failed copy or an interrupted download, and
            # playing it hands the user a player window with an error in it.
            raise ToolError(MSG_NOT_A_FILE)
    except OSError:
        raise ToolError(MSG_NOT_A_FILE) from None

    return candidate


def describe(path: object) -> str:
    """A short, safe description of a track for a spoken line or a card.

    Name and folder only -- never the full path. An approval card carrying
    ``C:\\Users\\me\\Music\\...`` puts the user's directory layout in front of
    whoever is standing behind them, and nothing about deciding whether to play a
    song needs it.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    p = Path(text)
    folder = p.parent.name if p.parent and p.parent.name else ""
    label = p.stem or p.name
    return clip(f"{label} ({folder})" if folder else label, 60)