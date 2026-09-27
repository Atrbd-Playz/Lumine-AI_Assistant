"""Configuration validation for Lumine voice profiles.

Rules live here, in the worker, rather than in React, so the settings UI cannot
promise a combination the runtime will refuse. ``agent.py`` validates before it
joins a room; the desktop layer renders the returned diagnostics.

Everything is a pure function over plain dictionaries because the configuration
document is JSON and may be untrusted. No provider is imported and no network
call is made: this answers "could this work?", not "does this key work?" (that
is the provider test command's job).

Diagnostics are graded:

* ``error`` - the profile cannot run. Activation is blocked.
* ``warn``  - it can run, but something is off (deprecated model, missing voice).
* ``info``  - worth surfacing, never blocking.

References for the compatibility rules encoded here are collected in
``docs/ai-control-center.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, Mapping

try:
    from .providers import (
        PROVIDERS,
        SESSION_OPTIONS,
        Capability,
        ModelDefinition,
        ProviderDefinition,
        get_model,
        get_provider,
    )
    from .session_preferences import INTERRUPTION_MODES
except ImportError:  # running as a top-level module
    from providers import (
        PROVIDERS,
        SESSION_OPTIONS,
        Capability,
        ModelDefinition,
        ProviderDefinition,
        get_model,
        get_provider,
    )
    from session_preferences import INTERRUPTION_MODES

Severity = Literal["error", "warn", "info"]

PROFILE_KINDS = ("pipeline", "realtime")

#: Keys that describe the profile's shape rather than a model's configuration.
#: They are read by the validator and the factory and are never passed to a
#: plugin, so they are not "options" and not reported as ignored ones either.
PROFILE_STRUCTURE: frozenset[str] = frozenset({"output", "turnHandling"})
OUTPUT_MODES = ("model_voice", "custom_tts")

CONFIG_VERSION = 1


@dataclass(frozen=True)
class Diagnostic:
    """One validation result, addressed at a path inside the profile."""

    severity: Severity
    code: str
    path: str
    message: str
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "hint": self.hint,
        }


def diagnostic(
    severity: Severity,
    code: str,
    path: str,
    message: str,
    hint: str = "",
) -> Diagnostic:
    """Build a Diagnostic from outside this module.

    Other modules report conditions that are not about a profile's shape (an
    unreadable file, a missing runtime default) and need the same grading.
    """
    return Diagnostic(severity=severity, code=code, path=path, message=message, hint=hint)


def has_errors(diagnostics: list[Diagnostic]) -> bool:
    return any(d.severity == "error" for d in diagnostics)


def env_credential_status() -> dict[str, bool]:
    """Whether each credential-bearing provider has *some* value in the environment.

    This is a presence check used as the default for offline validation. It says
    nothing about whether a key is valid, and the worker may not have loaded
    ``agent/.env`` yet, so callers running inside the job should pass an explicit
    mapping instead.
    """
    return {
        provider_id: any((os.getenv(name) or "").strip() for name in provider.key_env)
        for provider_id, provider in PROVIDERS.items()
        if provider.requires_key
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _diag(severity: Severity, code: str, path: str, message: str, hint: str = "") -> Diagnostic:
    return Diagnostic(severity=severity, code=code, path=path, message=message, hint=hint)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _check_credential(
    provider: ProviderDefinition,
    path: str,
    credentials: Mapping[str, bool] | None,
    out: list[Diagnostic],
) -> None:
    if not provider.requires_key or credentials is None:
        return
    if credentials.get(provider.id):
        return
    env_names = ", ".join(provider.key_env) or "a credential"
    out.append(
        _diag(
            "error",
            "credential.missing",
            path,
            f"{provider.label} has no credential available.",
            f"Add {env_names} in agent/.env, or store the key in the desktop credential store.",
        )
    )


def _check_lifecycle(
    model: ModelDefinition,
    path: str,
    out: list[Diagnostic],
) -> None:
    """Retired is fatal; deprecated is a nudge toward the successor."""
    if model.status == "retired":
        out.append(
            _diag(
                "error",
                "model.retired",
                path,
                f"{model.label} has been retired by its provider and is no longer reachable.",
            )
        )
    elif model.status == "deprecated":
        out.append(
            _diag(
                "warn",
                "model.deprecated",
                path,
                f"{model.label} is deprecated.",
                f"Move to {model.replaces}." if model.replaces else "",
            )
        )


def _resolve_stage(
    stage: Any,
    path: str,
    capability: Capability,
    credentials: Mapping[str, bool] | None,
    out: list[Diagnostic],
) -> tuple[ProviderDefinition | None, ModelDefinition | None]:
    """Resolve a ``{provider, model}`` pair for a required capability."""
    data = _as_mapping(stage)
    provider_id = _as_text(data.get("provider"))
    model_id = _as_text(data.get("model"))

    if not provider_id:
        out.append(_diag("error", "stage.provider_missing", f"{path}.provider", "No provider is selected."))
        return None, None
    if not model_id:
        out.append(_diag("error", "stage.model_missing", f"{path}.model", "No model is selected."))
        return None, None

    provider = get_provider(provider_id)
    if provider is None:
        out.append(
            _diag(
                "error",
                "stage.unknown_provider",
                f"{path}.provider",
                f"{provider_id!r} is not a known Lumine provider.",
            )
        )
        return None, None

    if not provider.supports(capability):
        supported = ", ".join(provider.capabilities()) or "nothing"
        out.append(
            _diag(
                "error",
                "stage.capability_mismatch",
                f"{path}.provider",
                f"{provider.label} does not provide {capability}.",
                f"It provides: {supported}.",
            )
        )
        return provider, None

    model = get_model(provider_id, model_id, capability)
    if model is None:
        available = ", ".join(m.id for m in provider.models_for(capability)) or "none"
        out.append(
            _diag(
                "error",
                "stage.unknown_model",
                f"{path}.model",
                f"{model_id!r} is not a known {capability} model for {provider.label}.",
                f"Available: {available}.",
            )
        )
        return provider, None

    _check_lifecycle(model, f"{path}.model", out)
    _check_credential(provider, f"{path}.provider", credentials, out)
    return provider, model


def _check_voice(
    provider: ProviderDefinition | None,
    voice_id: Any,
    path: str,
    out: list[Diagnostic],
) -> None:
    """A voice must exist when the provider publishes a catalog of them.

    An unknown voice is a warning, not an error: provider voice libraries are
    large and change often, and a custom or newly cloned voice is legitimate.
    """
    if provider is None or not provider.voices:
        return
    voice = _as_text(voice_id)
    if not voice:
        out.append(
            _diag(
                "warn",
                "voice.missing",
                path,
                f"No {provider.label} voice is selected; the provider default will be used.",
            )
        )
        return
    if provider.get_voice(voice) is None:
        out.append(
            _diag(
                "warn",
                "voice.unknown",
                path,
                f"{voice!r} is not in Lumine's {provider.label} voice catalog.",
                "It may still be a valid custom voice; this check only covers the curated list.",
            )
        )


def _check_interruption(
    mode: Any,
    path: str,
    out: list[Diagnostic],
) -> str:
    value = _as_text(mode).lower()
    if not value:
        return "barge_in"
    if value not in INTERRUPTION_MODES:
        out.append(
            _diag(
                "error",
                "turn.unknown_interruption_mode",
                path,
                f"{value!r} is not a supported interruption mode.",
                f"Choose one of: {', '.join(INTERRUPTION_MODES)}.",
            )
        )
    return value


# ---------------------------------------------------------------------------
# Profile kinds
# ---------------------------------------------------------------------------


def _check_stage_options(
    provider: ProviderDefinition | None,
    model: ModelDefinition | None,
    stage: Any,
    path: str,
    out: list[Diagnostic],
    structure: frozenset[str] = frozenset(),
) -> None:
    """Reject a setting the selected model does not accept.

    A provider answers an unsupported value with a 4xx at request time. In a voice
    session that is a silent failure: the session starts, the greeting is
    requested, and no audio ever arrives. Catching it here turns that into a
    diagnostic the settings screen can refuse to save over.

    Driven by the model's own declared options, so it covers every provider and
    every setting rather than the one combination that happened to break.
    """
    if provider is None or model is None:
        return
    data = _as_mapping(stage)
    for definition in model.options:
        if definition.name not in data:
            continue
        value = data[definition.name]
        if value is None or definition.is_valid(value):
            continue
        if definition.values:
            accepted = ", ".join(definition.values)
            out.append(
                _diag(
                    "error",
                    "stage.option_unsupported",
                    f"{path}.{definition.name}",
                    f"{model.label} does not accept {definition.name}={value!r}.",
                    f"Accepted: {accepted}. {definition.notes}".strip(),
                )
            )
        else:
            out.append(
                _diag(
                    "error",
                    "stage.option_unsupported",
                    f"{path}.{definition.name}",
                    f"{model.label} does not take a {definition.name} setting.",
                    definition.notes,
                )
            )

    # A setting the model never declared is dropped at build time, so saying so
    # here turns a silently ignored value into something the user can see.
    declared = {definition.name for definition in model.options}
    for companion in _companions(model):
        declared.add(companion)
    # The factory consumes these itself, so they are neither forwarded to the
    # plugin nor ignored. `structure` holds the keys that describe the profile
    # rather than the model -- a realtime section's `output` and `turnHandling`
    # are read by the validator and the factory, never passed to a plugin.
    declared.update(SESSION_OPTIONS)
    declared.update(structure)
    for key in data:
        if key in ("provider", "model", "voice") or key in declared:
            continue
        out.append(
            _diag(
                "warn",
                "stage.option_ignored",
                f"{path}.{key}",
                f"{model.label} does not use a {key} setting, so it is ignored.",
                "Remove it, or choose a model that accepts it.",
            )
        )


def _companions(model: ModelDefinition) -> set[str]:
    """Settings that ride along inside a nested object rather than standing alone."""
    found: set[str] = set()
    for definition in model.options:
        found.update(definition.companions)
    return found


def _check_thinking_level(
    provider: ProviderDefinition | None,
    model: ModelDefinition | None,
    stage: Any,
    path: str,
    out: list[Diagnostic],
) -> None:
    """Reject a thinking level the model does not accept.

    A specific case of :func:`_check_stage_options`, kept separate because the
    failure is worth naming precisely: `gemini-3.8-flash` rejects `minimal` while
    `gemini-3.6-flash` accepts it, so a profile that worked on one model fails on
    the next with a 400 and no audio.
    """
    level = _as_text(_as_mapping(stage).get("thinking_level")).strip().lower()
    if not level or model is None or provider is None:
        return
    if not model.thinking_levels:
        out.append(
            _diag(
                "error",
                "llm.thinking_level_unsupported",
                f"{path}.thinking_level",
                f"{model.label} is not configured by thinking level.",
                "Remove the thinking level, or choose a model that takes one.",
            )
        )
        return
    if not model.supports_thinking_level(level):
        supported = ", ".join(model.thinking_levels)
        out.append(
            _diag(
                "error",
                "llm.thinking_level_unsupported",
                f"{path}.thinking_level",
                f"{model.label} does not accept thinking level {level!r}.",
                f"Supported levels: {supported}.",
            )
        )


def _validate_pipeline(
    profile: Mapping[str, Any],
    credentials: Mapping[str, bool] | None,
    out: list[Diagnostic],
) -> None:
    pipeline = _as_mapping(profile.get("pipeline"))
    if not pipeline:
        out.append(
            _diag(
                "error",
                "profile.pipeline_missing",
                "pipeline",
                "A pipeline profile needs a pipeline section with stt, llm and tts.",
            )
        )
        return

    stt_provider, stt_model = _resolve_stage(pipeline.get("stt"), "pipeline.stt", "stt", credentials, out)
    llm_provider, llm_model = _resolve_stage(pipeline.get("llm"), "pipeline.llm", "llm", credentials, out)
    tts_provider, tts_model = _resolve_stage(pipeline.get("tts"), "pipeline.tts", "tts", credentials, out)

    _check_thinking_level(llm_provider, llm_model, pipeline.get("llm"), "pipeline.llm", out)

    vad_provider, vad_model = (None, None)
    vad = _as_mapping(pipeline.get("vad"))
    if vad and _as_text(vad.get("provider")):
        vad_provider, vad_model = _resolve_stage(vad, "pipeline.vad", "vad", credentials, out)

    for provider, model, key in (
        (stt_provider, stt_model, "stt"),
        (llm_provider, llm_model, "llm"),
        (tts_provider, tts_model, "tts"),
        (vad_provider, vad_model, "vad"),
    ):
        _check_stage_options(
            provider, model, pipeline.get(key), f"pipeline.{key}", out, PROFILE_STRUCTURE
        )

    # The VAD check is below because it is conditional on the STT model, which is
    # only known once the stage above has resolved.

    if stt_model is not None and stt_model.requires_vad and vad_provider is None:
        out.append(
            _diag(
                "error",
                "stt.requires_vad",
                "pipeline.stt.model",
                f"{stt_model.label} transcribes whole segments rather than streaming, so it needs a VAD to know when a segment ended.",
                "Select a VAD provider for this profile.",
            )
        )

    _check_voice(tts_provider, _as_mapping(pipeline.get("tts")).get("voice"), "pipeline.tts.voice", out)
    _check_interruption(_as_mapping(pipeline.get("turnHandling")).get("interruptionMode"), "pipeline.turnHandling.interruptionMode", out)


def _validate_realtime(
    profile: Mapping[str, Any],
    credentials: Mapping[str, bool] | None,
    out: list[Diagnostic],
) -> None:
    realtime = _as_mapping(profile.get("realtime"))
    if not realtime:
        out.append(
            _diag(
                "error",
                "profile.realtime_missing",
                "realtime",
                "A realtime profile needs a realtime section.",
            )
        )
        return

    realtime_provider, model = _resolve_stage(realtime, "realtime", "realtime", credentials, out)
    _check_stage_options(realtime_provider, model, realtime, "realtime", out, PROFILE_STRUCTURE)
    _check_thinking_level(realtime_provider, model, realtime, "realtime", out)

    output = _as_mapping(realtime.get("output"))
    mode = _as_text(output.get("mode")) or "model_voice"
    if mode not in OUTPUT_MODES:
        out.append(
            _diag(
                "error",
                "output.unknown_mode",
                "realtime.output.mode",
                f"{mode!r} is not a supported output mode.",
                f"Choose one of: {', '.join(OUTPUT_MODES)}.",
            )
        )
        return

    if mode == "model_voice":
        if model is not None and not model.voices and realtime_provider is not None:
            out.append(
                _diag(
                    "error",
                    "output.no_model_voice",
                    "realtime.output.mode",
                    f"{model.label} does not publish a voice, so it cannot speak for itself.",
                    "Use a separate TTS instead, or choose a realtime model with voices.",
                )
            )
        voice = _as_text(realtime.get("voice"))
        if model is not None and voice and voice not in model.voices:
            out.append(
                _diag(
                    "warn",
                    "output.voice_unknown",
                    "realtime.voice",
                    f"{voice!r} is not one of {model.label}'s listed voices.",
                    f"Listed voices: {', '.join(model.voices) or 'none'}.",
                )
            )
    else:
        tts_provider, tts_model = _resolve_stage(
            output.get("tts"), "realtime.output.tts", "tts", credentials, out
        )
        _check_voice(tts_provider, output.get("voice"), "realtime.output.voice", out)

        if model is not None and not model.text_only_modality:
            reason = (
                "it is a native-audio model, so it always generates speech itself"
                if model.native_audio
                else "it does not advertise a text-only response modality"
            )
            out.append(
                _diag(
                    "error",
                    "realtime.custom_tts_unsupported",
                    "realtime.output.mode",
                    f"{model.label} cannot be paired with a separate TTS because {reason}.",
                    "LiveKit's separate-TTS path needs a non-native-audio realtime model. "
                    "Use a pipeline profile, or keep the model's own voice.",
                )
            )
        if tts_model is not None and tts_model.status == "retired":
            pass  # already reported by _check_lifecycle

    interruption = _as_text(_as_mapping(realtime.get("turnHandling")).get("interruptionMode")).lower()
    if model is not None and interruption == "finish_response" and not model.finish_response:
        out.append(
            _diag(
                "error",
                "realtime.finish_response_unsupported",
                "realtime.turnHandling.interruptionMode",
                f"{model.label} cannot be told to finish a reply instead of yielding.",
                "A realtime model that owns turn detection cannot have interruptions "
                "disabled at the session level; LiveKit rejects that outright.",
            )
        )

    if model is not None:
        for key, supported, label in (
            ("proactivity", model.proactivity, "proactive replies"),
            ("affectiveDialog", model.affective_dialog, "affective dialog"),
            ("asyncFunctionCalling", model.async_function_calling, "asynchronous function calling"),
        ):
            if _as_mapping(profile.get("options")).get(key) and not supported:
                out.append(
                    _diag(
                        "warn",
                        f"realtime.{key}_unsupported",
                        f"options.{key}",
                        f"{model.label} does not support {label}.",
                    )
                )

    _ = realtime_provider  # resolved for its credential check above


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def validate_profile(
    profile: Any,
    credentials: Mapping[str, bool] | None = None,
) -> list[Diagnostic]:
    """Validate one voice profile. Never raises; problems come back as diagnostics."""
    out: list[Diagnostic] = []

    if not isinstance(profile, Mapping):
        return [_diag("error", "profile.invalid", "", "A profile must be an object.")]

    name = _as_text(profile.get("name"))
    if not name:
        out.append(_diag("warn", "profile.unnamed", "name", "This profile has no name."))

    kind = _as_text(profile.get("kind")).lower()
    if kind not in PROFILE_KINDS:
        out.append(
            _diag(
                "error",
                "profile.unknown_kind",
                "kind",
                f"{kind or '(empty)'!r} is not a supported profile kind.",
                f"Choose one of: {', '.join(PROFILE_KINDS)}.",
            )
        )
        return out

    if kind == "pipeline":
        _validate_pipeline(profile, credentials, out)
    else:
        _validate_realtime(profile, credentials, out)

    return out


def validate_document(
    document: Any,
    credentials: Mapping[str, bool] | None = None,
) -> list[Diagnostic]:
    """Validate a whole configuration document, including the active profile ref."""
    out: list[Diagnostic] = []
    if not isinstance(document, Mapping):
        return [_diag("error", "document.invalid", "", "The configuration document must be an object.")]

    version = document.get("version")
    if version != CONFIG_VERSION:
        out.append(
            _diag(
                "error",
                "document.unsupported_version",
                "version",
                f"Configuration version {version!r} is not supported.",
                f"This build understands version {CONFIG_VERSION}.",
            )
        )

    profiles = document.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        out.append(_diag("error", "document.no_profiles", "profiles", "The configuration has no profiles."))
        return out

    seen: set[str] = set()
    seen_names: dict[str, int] = {}
    for index, profile in enumerate(profiles):
        profile_id = _as_text(_as_mapping(profile).get("id"))
        if not profile_id:
            out.append(_diag("error", "profile.id_missing", f"profiles[{index}].id", "A profile has no id."))
        elif profile_id in seen:
            out.append(_diag("error", "profile.duplicate_id", f"profiles[{index}].id", f"Duplicate profile id {profile_id!r}."))
        else:
            seen.add(profile_id)

        # Two profiles may share a name and both still run, so this is a warning
        # rather than a block. It matters because the settings list shows names,
        # and two identical rows make it impossible to tell which is active.
        name = _as_text(_as_mapping(profile).get("name")).strip().lower()
        if name and name in seen_names:
            out.append(
                _diag(
                    "warn",
                    "profile.duplicate_name",
                    f"profiles[{index}].name",
                    f"Another profile is also named {name!r}.",
                    f"First seen at profiles[{seen_names[name]}].",
                )
            )
        elif name:
            seen_names[name] = index

        out.extend(validate_profile(profile, credentials))

    active = _as_text(document.get("activeProfileId"))
    if active and active not in seen:
        out.append(
            _diag(
                "error",
                "document.unknown_active_profile",
                "activeProfileId",
                f"The active profile {active!r} is not in this configuration.",
            )
        )

    return out
