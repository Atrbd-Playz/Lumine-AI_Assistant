"""Resolves Lumine's effective AI configuration from the environment.

Phase 1 establishes the *precedence rule* and the *shape* of a saved
configuration, without changing how a session is built. Nothing here is wired
into ``agent.py`` yet, so the running voice path is untouched.

Precedence, highest first::

    a saved configuration document   (LUMINE_CONFIG_PATH, or agent/lumine.config.json)
    agent/.env
    the built-in defaults in pipeline_config.py / llm_config.py

The important property is the first one: **when no document exists, resolution
falls through to the environment and behaves exactly as it did before this
module existed.** A user who never opens the settings screen gets today's
behaviour, not a new one.

:func:`env_document` synthesizes a read-only configuration document from the
resolved environment. That is what the desktop layer will show as the "managed
by agent/.env" profile, so the active stack is visible without editing anything.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:
    from .llm_config import llm_config
    from .pipeline_config import cartesia_tts_settings, gemini_settings, pipeline_name
    from .providers import Capability, get_model
    from .session_preferences import normalize_interruption_mode
    from .validation import CONFIG_VERSION, Diagnostic, diagnostic, validate_document
except ImportError:  # running as a top-level module
    from settings.llm_config import llm_config
    from settings.pipeline_config import cartesia_tts_settings, gemini_settings, pipeline_name
    from settings.providers import Capability, get_model
    from settings.session_preferences import normalize_interruption_mode
    from settings.validation import CONFIG_VERSION, Diagnostic, diagnostic, validate_document

logger = logging.getLogger("lumine")

# This module now lives in ``agent/settings/``, so ``__file__`` points one level
# deeper than the directory it is describing. ``AGENT_DIR`` is the *agent* folder
# -- it is where ``.env`` and the saved ``lumine.config.json`` live, and Tauri
# resolves that same folder independently -- hence ``parent.parent``.
AGENT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILENAME = "lumine.config.json"
CONFIG_PATH_ENV = "LUMINE_CONFIG_PATH"

#: The Tauri application identifier. This has to match ``tauri.conf.json``, because
#: the desktop app is what writes the file and this module is what reads it.
#:
#: The app was renamed from ``lumine-ui`` to ``Lumine`` and the identifier with it,
#: so this is no longer the old ``com.art.lumine-ui``. It matters more than it looks:
#: the two names are two different directories under ``%LOCALAPPDATA%``, and a
#: mismatch does not fail — the old one still exists, so the worker finds a stale
#: config there and cheerfully runs on settings the UI stopped writing. The
#: `test_the_app_identifier_matches_the_tauri_bundle` test exists to catch exactly
#: that, and it is the reason this value is a test failure rather than a surprise.
APP_IDENTIFIER = "com.art.lumine"

#: The identifier in use before the rename, still searched as a fallback.
#:
#: ``settings_store.migrate_legacy_config`` copies the file into the current
#: directory when Tauri starts, so after a normal launch this is inert. It is not
#: inert for a worker started outside Tauri -- the documented ``agent.py dev``
#: workflow -- which never runs that setup and would otherwise find nothing, then
#: quietly run on ``agent/.env`` while the app's settings screen showed different
#: values. Searching here keeps the two halves agreeing about the same file.
LEGACY_APP_IDENTIFIER = "com.art.lumine-ui"

# Provenance labels, so the UI can say where the active configuration came from.
SOURCE_UI = "ui"
SOURCE_ENV = "env"


def app_local_data_dir(identifier: str = APP_IDENTIFIER) -> Path | None:
    """Where Tauri puts per-user application data on this platform.

    Mirrors Tauri's ``app_local_data_dir()``, which is ``dirs::data_local_dir()``
    plus the application identifier:

    | Platform | Location |
    | --- | --- |
    | Windows | ``%LOCALAPPDATA%\\<identifier>`` |
    | macOS | ``~/Library/Application Support/<identifier>`` |
    | Linux | ``$XDG_DATA_HOME/<identifier>``, else ``~/.local/share/<identifier>`` |

    Returns ``None`` where the platform's location cannot be determined, so the
    caller can fall back rather than write somewhere arbitrary.

    ``Path.home()`` is not called unless the platform's own variable is absent:
    it raises when the environment has been stripped, and a worker that cannot
    start over a missing home directory is worse than one that falls back.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base and base.strip() else None
    elif sys.platform == "darwin":
        root = None
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base and base.strip() else None

    if root is None:
        try:
            home = Path.home()
        except (RuntimeError, OSError):  # pragma: no cover - stripped environment
            logger.warning("[Config] no home directory; not searching %s", identifier)
            return None
        if sys.platform == "win32":
            root = home / "AppData" / "Local"
        elif sys.platform == "darwin":
            root = home / "Library" / "Application Support"
        else:
            root = home / ".local" / "share"

    try:
        return root / identifier
    except (OSError, ValueError):  # pragma: no cover - an unusable HOME
        return None


def config_path_candidates(identifier: str = APP_IDENTIFIER) -> list[Path]:
    """Every place a saved document may live, most specific first.

    The override wins so a packaged build, a test, and a developer can each point
    at their own file without changing code.

    The application data directory comes next, because that is where the desktop
    app writes. It is here, rather than only in the child environment, so that a
    worker started *outside* Tauri — the documented ``lk agent dev`` workflow —
    still reads the settings a user just saved. Without it, the two halves
    disagree and the worker silently falls back to the environment.

    The pre-rename directory comes next, for the same reason: the identifier
    moved, so a settings file the user saved before the rename sits in a
    directory this module no longer looks at by default.

    The agent directory is last, so a hand-placed file keeps working.
    """
    override = (os.getenv(CONFIG_PATH_ENV) or "").strip()
    if override:
        return [Path(override)]

    candidates: list[Path] = []
    data_dir = app_local_data_dir(identifier)
    if data_dir is not None:
        candidates.append(data_dir / DEFAULT_CONFIG_FILENAME)
    if identifier != LEGACY_APP_IDENTIFIER:
        legacy_dir = app_local_data_dir(LEGACY_APP_IDENTIFIER)
        if legacy_dir is not None:
            candidates.append(legacy_dir / DEFAULT_CONFIG_FILENAME)
    candidates.append(AGENT_DIR / DEFAULT_CONFIG_FILENAME)
    return candidates


def config_path() -> Path:
    """The first candidate that exists, or the primary one when none do.

    Returning the primary path when no file exists keeps error messages and log
    lines pointing somewhere meaningful instead of at a candidate that was
    merely skipped.
    """
    candidates = config_path_candidates()
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


# ---------------------------------------------------------------------------
# The environment-derived profile
# ---------------------------------------------------------------------------


def _realtime_profile() -> dict[str, Any]:
    settings = gemini_settings()
    return {
        "id": "env-realtime",
        "name": "Gemini Live (from agent/.env)",
        "kind": "realtime",        "realtime": {
            "provider": "google",
            "model": settings["model"],
            "voice": settings["voice"],
            "output": {"mode": "model_voice"},
            "turnHandling": {
                "interruptionMode": (os.getenv("LUMINE_INTERRUPTION_MODE") or "barge_in").strip().lower(),
            },
        },
    }


def _pipeline_profile() -> dict[str, Any]:
    llm = llm_config()
    tts = cartesia_tts_settings()
    return {
        "id": "env-pipeline",
        "name": "Legacy cascade (from agent/.env)",
        "kind": "pipeline",
        "pipeline": {
            # whisper-large-v3-turbo is the Groq STT plugin's own default, so
            # naming it explicitly preserves the legacy cascade's behaviour.
            "stt": {"provider": "groq", "model": "whisper-large-v3-turbo"},
            "llm": {"provider": "groq", "model": llm["model"]},
            "tts": {
                "provider": "cartesia",
                "model": tts["model"],
                "voice": tts["voice"],
                "language": tts["language"],
            },
            "vad": {"provider": "silero", "model": "silero"},
            "turnHandling": {
                "interruptionMode": (os.getenv("LUMINE_INTERRUPTION_MODE") or "barge_in").strip().lower(),
            },
        },
    }


def env_profile(pipeline: str | None = None) -> dict[str, Any]:
    """A read-only profile describing the pipeline ``pipeline`` selects.

    Defaults to whatever ``LUMINE_PIPELINE`` says. Callers that already resolved
    a pipeline name must pass it, otherwise the environment is consulted a second
    time and the two answers can disagree.
    """
    if pipeline_name(pipeline) == "legacy_cascade":
        return _pipeline_profile()
    return _realtime_profile()


def env_document() -> dict[str, Any]:
    """Wrap :func:`env_profile` in a complete, valid configuration document."""
    profile = env_profile()
    return {
        "version": CONFIG_VERSION,
        "activeProfileId": profile["id"],
        "providers": {},
        "profiles": [profile],
        "agent": {},
    }


# ---------------------------------------------------------------------------
# Loading a saved document
# ---------------------------------------------------------------------------


def config_search_summary(path: Path | None = None) -> str:
    """A one-line description of where configuration is being looked for.

    A silent fallback to the environment is indistinguishable from "the user has
    no saved settings", which is how a settings change that was saved correctly
    can look like it was ignored. Naming the searched paths makes the difference
    visible in the log and in the ``config_applied`` event.
    """
    if path is not None:
        return f"{path} (explicit)"
    candidates = config_path_candidates()
    if (os.getenv(CONFIG_PATH_ENV) or "").strip():
        return f"{candidates[0]} (from {CONFIG_PATH_ENV})"
    return f"{candidates[0]} (first existing of {len(candidates)})"


def load_document(path: Path | None = None) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    """Read a saved document, or report why it is unusable.

    Returns ``(None, diagnostics)`` when there is no usable document. A missing
    file is normal, not an error; a corrupt or unsupported one is reported but
    still returns ``None`` so the caller can fall back to the environment rather
    than refuse to start.
    """
    target = path or config_path()
    if not target.exists():
        logger.info("[Config] no saved document; using agent/.env (%s)", config_search_summary(target))
        return None, []

    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[Config] could not read %s: %s", target, exc)
        return None, []

    try:
        document = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("[Config] %s is not valid JSON: %s", target, exc)
        return None, [
            diagnostic(
                "error",
                "document.unreadable",
                "",
                f"{target.name} is not valid JSON.",
                str(exc),
            )
        ]

    diagnostics = validate_document(document)
    blocking = [d for d in diagnostics if d.severity == "error"]
    if blocking:
        logger.warning(
            "[Config] %s has %d blocking problem(s); falling back to the environment",
            target,
            len(blocking),
        )
        return None, diagnostics

    return document, diagnostics


def active_profile(document: dict[str, Any]) -> dict[str, Any] | None:
    """The profile a document points at, or ``None`` if the reference dangles."""
    profiles = document.get("profiles") or []
    wanted = document.get("activeProfileId")
    for profile in profiles:
        if isinstance(profile, dict) and profile.get("id") == wanted:
            return profile
    return None


def effective_document(path: Path | None = None) -> tuple[dict[str, Any], str, list[Diagnostic]]:
    """The document that should be treated as authoritative, and where it came from.

    This is the single place the precedence rule is implemented.
    """
    document, diagnostics = load_document(path)
    if document is None:
        return env_document(), SOURCE_ENV, diagnostics
    return document, SOURCE_UI, diagnostics


# ---------------------------------------------------------------------------
# Resolving a profile into build instructions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedStage:
    """One stage of a pipeline, ready to hand to a provider builder."""

    provider: str
    model: str
    options: Mapping[str, Any] = field(default_factory=dict)
    voice: str | None = None


@dataclass(frozen=True)
class ResolvedProfile:
    """A profile normalized for :mod:`agent.pipeline.pipeline_factory`.

    Every field is already filled in from the environment or the catalog, so the
    factory never has to consult configuration itself. That keeps configuration
    resolution and provider construction in separate, separately testable places.
    """

    kind: str
    name: str
    interruption_mode: str
    stt: ResolvedStage | None = None
    llm: ResolvedStage | None = None
    tts: ResolvedStage | None = None
    vad: ResolvedStage | None = None
    realtime: ResolvedStage | None = None
    output_mode: str = "model_voice"
    max_tool_steps: int = 3
    min_endpointing_delay: float = 0.4
    #: Stable identifier, kept separate from ``name`` because the display name is
    #: presentation and this value is what logs and runtime events report.
    profile_id: str = "profile"

    def providers(self) -> tuple[str, ...]:
        """Every provider this profile needs, for a credential preflight."""
        found = {
            stage.provider
            for stage in (self.stt, self.llm, self.tts, self.vad, self.realtime)
            if stage is not None
        }
        return tuple(sorted(found))


def _stage_options(
    stage: Mapping[str, Any],
    provider: str,
    capability: Capability,
) -> dict[str, Any]:
    """Only the settings the chosen model declares, dropped if it is unknown.

    The allowlist used to be a hand-written tuple of five names. That is the same
    list the provider catalog already holds, kept by hand in a second place, so
    it drifted: it still allowed `temperature` for a TTS that has no such
    parameter, and it silently discarded `emotion`, `volume`, `max_completion_tokens`,
    `top_p` and `reasoning_effort` -- settings a profile could save, validate and
    then never send. Deriving the answer from the catalog removes the duplicate
    rather than reconciling it.

    A stage naming a model the catalog does not know passes through untouched.
    Refusing to resolve a profile would break a locally-run model the catalog
    cannot know about, and `build_options` drops undeclared keys downstream
    anyway, so an unknown model loses nothing by not being filtered here.
    """
    model = get_model(provider, str(stage.get("model") or ""), capability)
    if model is None:
        return {key: value for key, value in stage.items() if value is not None}
    return {
        key: value
        for key, value in stage.items()
        if value is not None and model.accepts(key)
    }


def resolve_profile(
    profile: Mapping[str, Any],
    *,
    interruption_mode: str | None = None,
) -> ResolvedProfile:
    """Normalize a stored or environment-derived profile for the factory.

    Environment-backed defaults are folded in here rather than in the factory, so
    an explicitly saved value always wins and an absent one keeps today's
    behaviour. Notably the Groq LLM options come from :func:`llm_config`, which
    carries the completion cap and retry clamps that keep a voice turn inside
    the provider's rate limit.

    ``normalize_interruption_mode`` is imported at module scope, not here: a
    function-level relative import breaks when the worker runs this file as a
    top-level script, which is exactly how ``python agent.py`` loads it.
    """
    kind = str(profile.get("kind") or "").strip().lower()
    mode = normalize_interruption_mode(
        interruption_mode
        or ((profile.get("realtime") or {}).get("turnHandling") or {}).get("interruptionMode")
        or ((profile.get("pipeline") or {}).get("turnHandling") or {}).get("interruptionMode")
    )

    if kind == "realtime":
        realtime = profile.get("realtime") or {}
        output = realtime.get("output") or {}
        settings = gemini_settings()
        # A realtime profile carries provider-specific options, so they are read
        # from the environment as the base and overridden by the profile. A
        # thinking level is included only when the environment names one: the
        # accepted set is per-model, and seeding a level nobody asked for sends a
        # parameter Google rejects on models that take no level at all.
        options: dict[str, Any] = {
            "max_output_tokens": settings["max_output_tokens"],
            "connect_max_retry": settings["connect_max_retry"],
            "connect_timeout": settings["connect_timeout"],
            "language": settings["language"],
            "silence_duration_ms": 800 if mode == "finish_response" else 700,
        }
        thinking = settings.get("thinking_config")
        if thinking:
            options["thinking_level"] = thinking["thinking_level"]
            options["include_thoughts"] = thinking["include_thoughts"]
        if "temperature" in settings:
            options["temperature"] = settings["temperature"]
        options.update(
            _stage_options(realtime, str(realtime.get("provider") or "google"), "realtime")
        )
        return ResolvedProfile(
            kind="realtime",
            name=str(profile.get("name") or "Realtime"),
            interruption_mode=mode,
            profile_id=str(profile.get("id") or "realtime"),
            realtime=ResolvedStage(
                provider=str(realtime.get("provider") or "google"),
                model=str(realtime.get("model") or settings["model"]),
                options=options,
                voice=str(realtime.get("voice") or settings["voice"]),
            ),
            output_mode=str(output.get("mode") or "model_voice"),
            # Gemini 3.1 answers a tool result in the same turn, so one step is
            # both sufficient and the latency-minimising choice.
            max_tool_steps=int(realtime.get("max_tool_steps") or 1),
        )

    pipeline = profile.get("pipeline") or {}
    tts_source = pipeline.get("tts") or {}
    stt_source = pipeline.get("stt") or {}
    llm_source = pipeline.get("llm") or {}
    vad_source = pipeline.get("vad") or {}
    tts_env = cartesia_tts_settings()
    groq_llm = llm_config()
    llm_provider = str(llm_source.get("provider") or "groq")

    # Groq's completion cap and retry clamps exist to keep a voice turn inside
    # that provider's rate limit. They are not generic LLM settings, so they are
    # only applied when Groq is actually the chosen provider -- otherwise
    # selecting Google or OpenAI would silently inherit a Groq token budget and
    # Groq retry policy.
    llm_base = groq_llm if llm_provider == "groq" else {}

    tts_provider = str(tts_source.get("provider") or "cartesia")
    stt_provider = str(stt_source.get("provider") or "groq")
    vad_provider = str(vad_source.get("provider") or "silero")

    return ResolvedProfile(
        kind="pipeline",
        name=str(profile.get("name") or "Pipeline"),
        interruption_mode=mode,
        profile_id=str(profile.get("id") or "pipeline"),
        stt=ResolvedStage(
            provider=stt_provider,
            model=str(stt_source.get("model") or "whisper-large-v3-turbo"),
            options=_stage_options(stt_source, stt_provider, "stt"),
        ),
        llm=ResolvedStage(
            provider=llm_provider,
            model=str(llm_source.get("model") or (groq_llm if llm_provider == "groq" else {}).get("model", "")),
            options={**llm_base, **_stage_options(llm_source, llm_provider, "llm")},
        ),
        tts=ResolvedStage(
            provider=tts_provider,
            model=str(tts_source.get("model") or tts_env["model"]),
            # Environment values are the base; an explicit profile value wins.
            # Order matters here, so the spread comes last.
            options={
                "language": tts_env["language"],
                "speed": tts_env["speed"],
                **_stage_options(tts_source, tts_provider, "tts"),
            },
            voice=str(tts_source.get("voice") or tts_env["voice"]),
        ),
        vad=ResolvedStage(
            provider=vad_provider,
            model=str(vad_source.get("model") or "silero"),
            # The legacy cascade has always used this value; changing it would
            # alter turn-taking for the working pipeline.
            options={"min_speech_duration": 0.4},
        )
        if vad_provider
        else None,
        max_tool_steps=3,
    )
