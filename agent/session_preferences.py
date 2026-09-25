"""Per-job user preferences received through LiveKit dispatch metadata."""

from __future__ import annotations

import json
import os
from typing import Any

INTERRUPTION_MODES = ("finish_response", "barge_in")
DEFAULT_INTERRUPTION_MODE = "barge_in"


def normalize_interruption_mode(value: object, *, default: str = DEFAULT_INTERRUPTION_MODE) -> str:
    """Validate one of the supported voice interruption modes."""
    candidate = str(value or "").strip().lower()
    if candidate in INTERRUPTION_MODES:
        return candidate
    if default in INTERRUPTION_MODES:
        return default
    raise ValueError(f"Unsupported interruption mode: {value!r}")


def interruption_mode_from_metadata(metadata: str | None = None) -> str:
    """Read the UI preference, falling back safely for manual dispatches.

    Metadata is untrusted input. Malformed or unknown values must not prevent a
    voice session from starting; they use the environment/default mode instead.
    """
    fallback = normalize_interruption_mode(os.getenv("LUMINE_INTERRUPTION_MODE"))
    if not metadata:
        return fallback

    try:
        parsed: Any = json.loads(metadata)
    except (TypeError, ValueError):
        return fallback
    if not isinstance(parsed, dict):
        return fallback
    return normalize_interruption_mode(parsed.get("interruption_mode"), default=fallback)
