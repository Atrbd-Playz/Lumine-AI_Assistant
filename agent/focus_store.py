"""Where Lumine's Focus Workspace document lives, and how to read it safely.

## Why this exists, and why it is a *reader*

The Focus Workspace document (`focus.json`) is written by the desktop app through
``lumine-ui/src-tauri/src/focus_store.rs``. That Rust module is the single writer.
This one finds the same file and reads it.

There is no tool registration here and no ``@function_tool`` anywhere near it. The
agent-side integration is a deliberate later phase, and the reason it is not
being built now is worth stating: **a tool that can write this file is a tool that
can delete somebody's study history**, and that needs its own permission class and
its own approval wording. A reader has neither problem, so the reader is what ships
first.

## Why the path search is duplicated rather than shared

``settings/config_store.py`` already resolves ``lumine.config.json`` the same way,
and the honest answer is that this is a deliberate second implementation rather than
a shared helper. The two files have different override variables, different
fallbacks, and one of them has to work in a packaged build where the agent directory
is somewhere else entirely. Coupling them would mean a change to one document's
lookup could break the other, and the two are read by different processes for
different reasons.

``tests/test_focus_store.py`` pins the precedence, which is the part that actually
breaks.

## What this module will not do

- **It will not write, repair or migrate.** A document that will not parse is
  reported. Overwriting it with defaults is how a person loses a term of history
  and is never told, and nothing in this file has a legitimate reason to do that.
- **It will not migrate a future version.** Rust refuses ``version > 1`` for the
  same reason: this build cannot know what it would be dropping.
- **It will not interpret note or intention text.** The contents of those fields
  are text a person wrote, and text a person wrote is data. ``focus_stats.py``
  carries that through :func:`~agent.tools.tool_results.wrap_untrusted` for exactly
  this reason.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

try:  # package import (repo root on sys.path)
    from .settings.config_store import (
        AGENT_DIR,
        APP_IDENTIFIER,
        LEGACY_APP_IDENTIFIER,
        app_local_data_dir,
        config_path,
    )
except ImportError:  # running as a top-level module (agent/ on sys.path)
    from settings.config_store import (  # type: ignore[no-redef]
        AGENT_DIR,
        APP_IDENTIFIER,
        LEGACY_APP_IDENTIFIER,
        app_local_data_dir,
        config_path,
    )

logger = logging.getLogger("lumine")

#: Overrides the resolved location. Mirrors ``focus_store::FOCUS_PATH_ENV`` in Rust.
FOCUS_PATH_ENV = "LUMINE_FOCUS_PATH"
FOCUS_FILENAME = "focus.json"

#: The schema this build understands. Mirrors ``focus_store::FOCUS_VERSION``.
FOCUS_VERSION = 1

#: 8 MB, matching the Rust ceiling. A reader that will not enforce the same limit
#: is a reader that will happily allocate whatever it is handed.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


class FocusStoreError(Exception):
    """A focus document that exists but cannot be used.

    Distinct from "there is no document", which is not an error at all: a person
    who has never opened the Focus page has no ``focus.json``, and that is the
    normal state rather than a failure to report.
    """


def focus_path_candidates(identifier: str = APP_IDENTIFIER) -> list[Path]:
    """Every place a focus document may live, most specific first.

    The order is the contract, and it is the reverse of a "most general first"
    instinct:

    1. :data:`FOCUS_PATH_ENV`, so a packaged build, a test or a developer can point
       at their own file without touching code.
    2. The desktop app's local-data directory, because that is where the Tauri
       side writes. It is searched rather than relying only on the child
       environment, so a worker started *outside* Tauri -- the documented
       ``agent.py dev`` workflow -- still reads the file the app last saved.
    3. The pre-rename directory, for a file saved before the identifier moved.
    4. Beside the saved configuration, and then the agent directory itself. This
       pair is the one that is easy to get wrong: ``focus.json`` sits next to
       ``lumine.config.json``, so when the desktop app writes one it writes the
       other, and a worker that resolves the config but not the focus document
       beside it will report "no focus data" for a file that is right there.
    """
    override = (os.getenv(FOCUS_PATH_ENV) or "").strip()
    if override:
        return [Path(override)]

    candidates: list[Path] = []
    data_dir = app_local_data_dir(identifier)
    if data_dir is not None:
        candidates.append(data_dir / FOCUS_FILENAME)
    if identifier != LEGACY_APP_IDENTIFIER:
        legacy_dir = app_local_data_dir(LEGACY_APP_IDENTIFIER)
        if legacy_dir is not None:
            candidates.append(legacy_dir / FOCUS_FILENAME)

    # The two "beside it" fallbacks. `config_path()` returns the primary candidate
    # when nothing exists yet, so its *parent* is the directory the desktop app
    # uses when it does.
    try:
        candidates.append(config_path().parent / FOCUS_FILENAME)
    except (OSError, ValueError):  # pragma: no cover - an unusable environment
        logger.warning("[Focus] could not resolve the configuration directory")
    candidates.append(AGENT_DIR / FOCUS_FILENAME)

    # De-duplicated while preserving order. Two candidates resolving to one path is
    # normal (the desktop directory *is* the config directory), and a caller that
    # stats the same file twice learns nothing.
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def focus_path() -> Path:
    """The first candidate that exists, or the primary one when none do.

    Returning the primary path when there is no file keeps a log line or an error
    message pointing somewhere a person can look, rather than at a candidate that
    was merely skipped.
    """
    candidates = focus_path_candidates()
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError:  # pragma: no cover - an unreadable directory
            continue
    return candidates[0]


def load_focus(path: Path | None = None) -> dict[str, Any]:
    """Read the focus document, or report why it cannot be read.

    Returns an empty document when there is no file -- the first-visit case, and
    not an error.

    Raises :class:`FocusStoreError` for a file that exists but cannot be used: too
    large, not JSON, not an object, or written by a newer Lumine. Each of those is
    a case where the *right* thing to do is to tell somebody, and overwriting the
    file with defaults is the one thing that is never right.
    """
    target = path or focus_path()
    try:
        if not target.exists():
            return {}
        size = target.stat().st_size
    except OSError as err:
        raise FocusStoreError(f"Could not read the focus data: {err}") from err

    if size > MAX_DOCUMENT_BYTES:
        raise FocusStoreError("The focus data file is unexpectedly large.")

    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise FocusStoreError(f"Could not read the focus data: {err}") from err

    if not raw.strip():
        return {}

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as err:
        raise FocusStoreError(f"The focus data is not valid JSON: {err}") from err

    if not isinstance(document, dict):
        raise FocusStoreError("The focus data is not valid: the top level must be an object.")

    version = document.get("version")
    if not isinstance(version, int):
        raise FocusStoreError("The focus data is not valid: it has no version number.")
    if version > FOCUS_VERSION:
        raise FocusStoreError(
            f"This focus data was written by a newer version of Lumine (v{version}). "
            "Update the app to read it."
        )
    if version < FOCUS_VERSION:
        # An older, migratable document is readable without being rewritten. The
        # migration happens in Rust on the next save; all this side needs is to not
        # refuse a file whose contents it can still make sense of.
        logger.info("[Focus] reading a v%s document; Rust will migrate it on next save", version)

    return document


def load_focus_or_empty(path: Path | None = None) -> dict[str, Any]:
    """:func:`load_focus`, with a broken document treated as an empty one.

    For callers that genuinely cannot do anything useful with the error -- a
    dashboard that would rather show nothing than refuse to render. The error is
    still logged, because a chart quietly drawing zero for a file it could not read
    is indistinguishable from a person who has not studied.
    """
    try:
        return load_focus(path)
    except FocusStoreError as err:
        logger.warning("[Focus] %s", err)
        return {}