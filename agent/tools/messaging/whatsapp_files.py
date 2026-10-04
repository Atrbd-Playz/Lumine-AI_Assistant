"""The file boundary: which files on this computer Lumine may attach.

A path arrives from a language model. That is the entire threat model, and it is
worth being blunt about what it means: without a boundary, "attach
`C:\\Users\\me\\.ssh\\id_rsa`" is a perfectly reasonable thing for a confused model
to produce after a misunderstood request, and the resulting message leaves the
machine inside somebody's phone. Nothing about the *model* prevents that. The
gate does not either -- the gate asks a person who has no reason to suspect that
the thing being sent is a private key rather than the photo they asked for.

So the boundary is here, in code, and it is deliberately narrow:

- **Only inside an allowlisted root.** Downloads, Pictures and Desktop by
  default, because that is where the files a person asks to send actually live.
  Configurable for anybody whose life is differently arranged.
- **Resolved, then compared.** ``Path.resolve`` first -- which collapses ``..``,
  follows symlinks, and makes the comparison against the root meaningful.
  Checking the string and then resolving is the classic way to write a check
  that passes.
- **The resolved path must still be inside a resolved root.** Both sides
  resolved, or a root that is itself a symlink fails the comparison and every
  attachment is mysteriously refused.
- **Extension allowlist and a size cap.** No executables, no scripts, no archives
  (an archive is a container for exactly the things this is excluding), and
  nothing large enough to be somebody's disk.
- **The file is read, not executed.** Nothing here opens the file. Handing a path
  to Playwright makes WhatsApp read it, and that is the whole of what happens to
  it on this side.

Local paths only. A URL attachment would mean downloading, which means lifting
``accept_downloads=False`` on the browser context -- a guard that exists so a page
cannot drop files on this disk. Trading that for convenience would be a bad deal,
so an ``http://`` attachment is refused with a sentence explaining why rather than
implemented.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from ..tools_compat import ToolError
    from ..tools_text import clip
except ImportError:  # running as a top-level script with agent/ on sys.path
    from tools_compat import ToolError
    from tools_text import clip

#: The variable that replaces the default roots. A ``os.pathsep``-separated list,
#: because that is how Windows already spells a path list.
ROOTS_VAR = "LUMINE_ATTACH_ROOTS"

#: The largest file that may be attached. WhatsApp's own ceiling for a document is
#: 100 MB, but a voice turn that sends a 90 MB file is a request the user did not
#: mean to make, and the cap is here to catch the miswording as much as the size.
MAX_ATTACH_BYTES = 25 * 1024 * 1024

#: Extensions allowed to leave the machine. Images and documents, because those
#: are what people send. Deliberately absent: archives (a container for
#: everything excluded here), anything executable or scriptable, and every
#: "unknown" -- an extension not on this list is refused, because a whitelist that
#: fails open is not a whitelist.
ALLOWED_SUFFIXES = frozenset(
    {
        # images
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif",
        # documents
        ".pdf", ".txt", ".md", ".csv", ".rtf", ".doc", ".docx",
        ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
        # audio and video, for voice notes and short clips
        ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".amr",
        ".mp4", ".mov", ".3gp", ".webm",
    }
)

MSG_TOO_BIG = "That file is too large to send from here. Nothing was sent."
MSG_BAD_TYPE = (
    "I can only send photos, documents, and short clips from this computer. "
    "Nothing was sent."
)
MSG_NOT_A_FILE = "I couldn't find that file on this computer. Nothing was sent."
MSG_OUTSIDE_ROOTS = (
    "I can only send files from your downloads, pictures, or desktop folder. "
    "Nothing was sent."
)
MSG_IS_URL = (
    "I can only send a file that's already on this computer -- I won't download "
    "something from the internet and send it on your behalf. Nothing was sent."
)


def default_roots() -> list[Path]:
    """Downloads, Pictures and Desktop, when they exist.

    Existence is checked rather than assumed because a headless machine, a fresh
    profile, or a Linux box with no Desktop has none of them, and a root that does
    not exist would otherwise make every attachment fail its own comparison with a
    confusing "cannot send from your downloads".
    """
    home = Path.home()
    candidates = ("Downloads", "Pictures", "Desktop")
    return [home / name for name in candidates if (home / name).is_dir()]


def allowed_roots() -> list[Path]:
    """The roots, resolved, from the setting or the defaults."""
    raw = (os.getenv(ROOTS_VAR, "") or "").strip()
    if not raw:
        return default_roots()
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        piece = part.strip().strip('"')
        if not piece:
            continue
        try:
            root = Path(piece).expanduser()
            if root.is_dir():
                roots.append(root.resolve())
        except OSError:
            # An unusable entry is skipped rather than raising: a settings file
            # with one bad path in it should narrow what can be sent, not stop
            # anything being sent at all.
            continue
    return roots


def _inside(child: Path, root: Path) -> bool:
    """Whether `child` is `root` or lives under it. Both must be resolved."""
    try:
        return child == root or root in child.parents
    except (OSError, ValueError):
        # A path that cannot be compared is not a path we will hand to a file
        # picker. On Windows this is reachable with a reserved device name.
        return False


def resolve_attachment(raw: object) -> str:
    """The validated absolute path of a file that may be attached.

    Returns the path rather than a bool so the caller has something to hand to
    the browser, and raises :class:`ToolError` with a sentence a person can act
    on for every refusal -- "move it to your downloads" is advice, "invalid
    attachment" is not.
    """
    text = str(raw or "").strip().strip('"')
    if not text:
        raise ToolError(MSG_NOT_A_FILE)

    lowered = text.casefold()
    if lowered.startswith(("http://", "https://", "ftp://")):
        raise ToolError(MSG_IS_URL)

    try:
        # ``strict=False`` so a missing file produces a path we can then test,
        # rather than an exception at resolution time. Everything after this is
        # about the *resolved* form, which is the only form that means anything.
        candidate = Path(text).expanduser().resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        raise ToolError(MSG_NOT_A_FILE) from None

    roots = allowed_roots()
    if not any(_inside(candidate, root) for root in roots):
        raise ToolError(MSG_OUTSIDE_ROOTS)

    if candidate.suffix.casefold() not in ALLOWED_SUFFIXES:
        raise ToolError(MSG_BAD_TYPE)

    try:
        if not candidate.is_file():
            raise ToolError(MSG_NOT_A_FILE)
        size = candidate.stat().st_size
    except OSError:
        raise ToolError(MSG_NOT_A_FILE) from None

    if size <= 0:
        raise ToolError(MSG_NOT_A_FILE)
    if size > MAX_ATTACH_BYTES:
        raise ToolError(MSG_TOO_BIG)

    return str(candidate)


def describe(raw: object) -> str:
    """A short, safe description of an attachment for an approval card.

    Only the file *name* and a rounded size. A full path on an approval card
    would put the user's directory layout in front of whoever is standing behind
    them, and nothing about deciding whether to send a photo needs it.
    """
    text = str(raw or "").strip().strip('"')
    name = Path(text).name or text
    try:
        size = Path(text).expanduser().stat().st_size
    except OSError:
        return clip(name, 40)
    if size >= 1024 * 1024:
        size_text = f"{size / (1024 * 1024):.1f} MB"
    else:
        size_text = f"{max(1, size // 1024)} KB"
    return clip(f"{name} ({size_text})", 50)