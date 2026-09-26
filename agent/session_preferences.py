"""Per-job user preferences received through LiveKit dispatch metadata."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

INTERRUPTION_MODES = ("finish_response", "barge_in")
DEFAULT_INTERRUPTION_MODE = "barge_in"

# A profile id is an opaque reference chosen by the desktop app. It travels in
# dispatch metadata, so it is untrusted input like everything else there: keep it
# short and boring rather than trying to be clever.
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def normalize_interruption_mode(value: object, *, default: str = DEFAULT_INTERRUPTION_MODE) -> str:
    """Validate one of the supported voice interruption modes."""
    candidate = str(value or "").strip().lower()
    if candidate in INTERRUPTION_MODES:
        return candidate
    if default in INTERRUPTION_MODES:
        return default
    raise ValueError(f"Unsupported interruption mode: {value!r}")


def normalize_profile_id(value: object) -> str | None:
    """Return a safe profile id, or ``None`` if the value is unusable."""
    candidate = str(value or "").strip()
    return candidate if _PROFILE_ID.match(candidate) else None


@dataclass(frozen=True)
class JobPreferences:
    """What the desktop app asked for when it dispatched this job.

    Only the *reference* travels in metadata, never configuration content and
    never a credential. A missing or unusable field resolves to the environment
    default so a hand-dispatched job still starts.
    """

    interruption_mode: str
    profile_id: str | None = None

    @property
    def uses_default_profile(self) -> bool:
        return self.profile_id is None


def _parse_metadata(metadata: str | None) -> dict[str, Any] | None:
    """Parse dispatch metadata, treating anything unexpected as absent."""
    if not metadata:
        return None
    try:
        parsed: Any = json.loads(metadata)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def interruption_mode_from_metadata(metadata: str | None = None) -> str:
    """Read the UI preference, falling back safely for manual dispatches.

    Metadata is untrusted input. Malformed or unknown values must not prevent a
    voice session from starting; they use the environment/default mode instead.
    """
    fallback = normalize_interruption_mode(os.getenv("LUMINE_INTERRUPTION_MODE"))
    parsed = _parse_metadata(metadata)
    if parsed is None:
        return fallback
    return normalize_interruption_mode(parsed.get("interruption_mode"), default=fallback)


def job_preferences_from_metadata(metadata: str | None = None) -> JobPreferences:
    """Read the whole per-job request in one pass.

    Backwards compatible by construction: metadata that carries only
    ``interruption_mode`` (which is all the current frontend sends) produces the
    same interruption mode as :func:`interruption_mode_from_metadata` and no
    profile reference.
    """
    parsed = _parse_metadata(metadata) or {}
    return JobPreferences(
        interruption_mode=normalize_interruption_mode(
            parsed.get("interruption_mode"),
            default=normalize_interruption_mode(os.getenv("LUMINE_INTERRUPTION_MODE")),
        ),
        profile_id=normalize_profile_id(parsed.get("profile_id")),
    )
