"""Append-only records of what was asked about, approved, or refused.

An approval is a security decision, and a security decision nobody can review
afterwards was made in the dark. This log answers "did that actually get
approved, and by whom?" from a file on disk rather than from the model's memory
of the conversation -- a model can be wrong about its own history, and the chat
context is trimmed by design.

Format: one JSON object per line (JSONL). JSONL because the file is read while
the worker is writing it: a torn last line breaks one row, not the whole file.

Three rules this module keeps:

- **It never raises.** A telemetry write that can fail is a session that can
  die -- see ``runtime_events.write_event_line``, which exists for exactly this
  reason: the desktop app pipes the worker's stdout, and a write to a dead pipe
  raises on Windows. Losing an audit line is acceptable; losing the session is
  not.
- **Entries carry a digest** of the action's content, so a record can be
  checked against what was really done afterwards.
- **The path is resolved per write** from ``LUMINE_AUDIT_PATH``, else the
  desktop app's local data directory (where the settings document lives), else
  the agent directory -- so a worker started three different ways still writes
  somewhere findable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

try:
    from ..settings.config_store import app_local_data_dir
except ImportError:  # running as a top-level script with agent/ on sys.path
    from settings.config_store import app_local_data_dir

logger = logging.getLogger("lumine")

#: Overridable location of the log file. A path rather than a directory, so a
#: test (or a paranoid user) can point it anywhere without renaming the file.
AUDIT_PATH_ENV = "LUMINE_AUDIT_PATH"

#: The file's name when no override is set.
AUDIT_FILENAME = "lumine_audit.jsonl"

AGENT_DIR = Path(__file__).resolve().parent.parent

#: Rotate once the file passes this size, keeping one previous copy. Unbounded
#: growth on a persistent worker would eventually fill the disk, and an audit
#: log that eats the volume it protects is its own incident.
MAX_BYTES = 5 * 1024 * 1024

#: Set once a write has failed, so a permanently unwritable path warns once
#: instead of once per action.
_WRITE_WARNED = False


def audit_path() -> Path:
    """Where audit lines go right now."""
    override = (os.getenv(AUDIT_PATH_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    data_dir = None
    try:
        data_dir = app_local_data_dir()
    except Exception:  # noqa: BLE001 - a missing platform dir must not break logging
        data_dir = None
    base = Path(data_dir) if data_dir else AGENT_DIR
    return base / AUDIT_FILENAME


def digest(*parts: str) -> str:
    """SHA-256 over the action's content, for tamper-evidence.

    Not a secret: the inputs are on disk next to it. It detects *edits* -- if a
    line's summary is later changed by hand, the digest no longer matches, and
    a line edited after the fact stands out.
    """
    joined = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()


def _rotate(path: Path) -> None:
    if not path.is_file() or path.stat().st_size < MAX_BYTES:
        return
    previous = path.with_name(path.name + ".1")
    path.replace(previous)


def record(event: str, **fields: Any) -> None:
    """Append one audit line. Never raises.

    ``event`` is a short verb ("armed", "decision", "ignored"); ``fields`` are
    the specifics. Empty and None fields are dropped so the line carries only
    what happened.
    """
    entry: dict[str, Any] = {"ts": round(time.time(), 3), "event": str(event)}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        entry[key] = value
    try:
        line = json.dumps(entry, default=str, separators=(",", ":"), ensure_ascii=False)
        path = audit_path()
        _rotate(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except (OSError, ValueError, TypeError):
        global _WRITE_WARNED
        if not _WRITE_WARNED:
            _WRITE_WARNED = True
            logger.warning(
                "[Audit] %s is not writable; approval decisions will not be recorded",
                _safe_path(),
            )


def _safe_path() -> str:
    try:
        return str(audit_path())
    except Exception:  # noqa: BLE001 - this is the already-failing path lookup
        return AUDIT_FILENAME
