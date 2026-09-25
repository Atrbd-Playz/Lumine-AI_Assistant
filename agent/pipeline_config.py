"""Environment-backed configuration for Lumine's selectable voice pipelines.

The Groq/Silero/Cartesia settings remain in :mod:`agent.llm_config`; this module
only describes the new Gemini Live profile and the small profile selector.
Keeping the provider settings here lets the runtime choose a pipeline without
loading every provider plugin at import time.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_PIPELINE = "gemini_live"
LEGACY_PIPELINE = "legacy_cascade"
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_GEMINI_VOICE = "Sulafat"
DEFAULT_GEMINI_LANGUAGE = "en"
DEFAULT_GEMINI_THINKING_LEVEL = "minimal"
DEFAULT_GEMINI_MAX_OUTPUT_TOKENS = 1024
DEFAULT_GEMINI_CONNECT_MAX_RETRY = 0
DEFAULT_GEMINI_CONNECT_TIMEOUT = 10.0
DEFAULT_GEMINI_MAX_TOOL_STEPS = 1

_PIPELINE_ALIASES = {
    "gemini": "gemini_live",
    "gemini-live": "gemini_live",
    "gemini_live": "gemini_live",
    "legacy": LEGACY_PIPELINE,
    "legacy_cascade": LEGACY_PIPELINE,
    "cascade": LEGACY_PIPELINE,
}


def _raw(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _int_env(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = _raw(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def _float_env(name: str, default: float, minimum: float, maximum: float | None = None) -> float:
    raw = _raw(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = _raw(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def pipeline_name(value: str | None = None) -> str:
    """Return a normalized pipeline profile name.

    An unknown value is rejected instead of silently selecting a different
    provider, which makes configuration mistakes visible during worker startup.
    """
    raw = (value if value is not None else _raw("LUMINE_PIPELINE") or DEFAULT_PIPELINE).strip().lower()
    normalized = _PIPELINE_ALIASES.get(raw)
    if normalized is None:
        supported = ", ".join(sorted(set(_PIPELINE_ALIASES.values())))
        raise ValueError(f"Unsupported LUMINE_PIPELINE={raw!r}. Choose one of: {supported}.")
    return normalized


def gemini_settings() -> dict[str, Any]:
    """Return the Gemini Live settings without importing a Google plugin."""
    settings: dict[str, Any] = {
        "model": _raw("GEMINI_MODEL") or _raw("LUMINE_GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
        "voice": _raw("GEMINI_VOICE") or DEFAULT_GEMINI_VOICE,
        "language": _raw("GEMINI_LANGUAGE") or DEFAULT_GEMINI_LANGUAGE,
        "thinking_config": {
            "thinking_level": _raw("GEMINI_THINKING_LEVEL") or DEFAULT_GEMINI_THINKING_LEVEL,
            "include_thoughts": _bool_env("GEMINI_INCLUDE_THOUGHTS", False),
        },
        "max_output_tokens": _int_env(
            "GEMINI_MAX_OUTPUT_TOKENS", DEFAULT_GEMINI_MAX_OUTPUT_TOKENS, 16, 4096
        ),
        "connect_max_retry": _int_env(
            "GEMINI_CONNECT_MAX_RETRY", DEFAULT_GEMINI_CONNECT_MAX_RETRY, 0, 2
        ),
        "connect_timeout": _float_env(
            "GEMINI_CONNECT_TIMEOUT", DEFAULT_GEMINI_CONNECT_TIMEOUT, 1.0, 120.0
        ),
        "max_tool_steps": _int_env(
            "GEMINI_MAX_TOOL_STEPS", DEFAULT_GEMINI_MAX_TOOL_STEPS, 1, 3
        ),
    }

    # Gemini 3 models should use the provider default unless explicitly tuned.
    temperature = _raw("GEMINI_TEMPERATURE")
    if temperature:
        try:
            settings["temperature"] = float(temperature)
        except ValueError:
            pass

    return settings
