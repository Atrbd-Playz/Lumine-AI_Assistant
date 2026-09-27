"""Factories for Lumine's voice pipelines.

Stages are built from a :class:`~agent.config_store.ResolvedProfile` rather than
being hardcoded, so STT, LLM, TTS, and VAD can be selected independently and
realtime can be paired with a separate TTS when a model supports it.

Two invariants protect the working voice path:

* The environment path still produces the same session options it always did. The
  only model that changes is Cartesia TTS, which moved off the retiring Sonic 2.
* Provider plugins are registered on the worker main thread, so they are imported
  here before any job event loop exists. Only their potentially blocking
  constructors are deferred to threads.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

try:
    from .config_store import ResolvedProfile, ResolvedStage, env_profile, resolve_profile
    from .llm_config import connect_max_retry
    from .pipeline_config import pipeline_name
    from .providers import Capability, get_model
    from .session_preferences import normalize_interruption_mode
    from .validation import (
        Diagnostic,
        diagnostic,
        env_credential_status,
        validate_profile,
    )
except ImportError:
    from config_store import ResolvedProfile, ResolvedStage, env_profile, resolve_profile
    from llm_config import connect_max_retry
    from pipeline_config import pipeline_name
    from providers import Capability, get_model
    from session_preferences import normalize_interruption_mode
    from validation import (
        Diagnostic,
        diagnostic,
        env_credential_status,
        validate_profile,
    )

# LiveKit plugins must be registered on the process's main thread. Import the
# provider modules here, before any job/event loop is created, and only defer
# their potentially blocking constructors to worker threads.
#
# The module-level names are also what the tests patch, so they must stay.
try:
    from livekit.plugins import cartesia as _cartesia
except ImportError:
    _cartesia = None
try:
    from livekit.plugins import google as _google
except ImportError:
    _google = None
try:
    from livekit.plugins import groq as _groq
except ImportError:
    _groq = None
try:
    from livekit.plugins import openai as _openai
except ImportError:
    _openai = None
try:
    from livekit.plugins import silero as _silero
except ImportError:
    _silero = None


#: Provider ids that are reached through another provider's plugin.
#:
#: A local model server is not a LiveKit plugin. There is no
#: `livekit-plugins-ollama`, and there will not be one: what speaks to Ollama is
#: the OpenAI client, pointed somewhere else. So `ollama` is an *address*, not a
#: package, and the factory has to know that a model chosen under it is built by
#: the OpenAI plugin with a `base_url`.
#:
#: This is the one place the factory knows a provider exists, and it is a
#: dictionary of names rather than a branch. A second local provider is a second
#: line here, not a second `if`.
_PLUGIN_ALIASES: dict[str, str] = {
    "ollama": "openai",
}


def provider_module(provider_id: str) -> Any:
    """The imported plugin module for a provider id, or ``None`` if absent.

    Looked up by name rather than captured in a closure so a test can patch the
    module-level ``_groq``/``_cartesia``/``_silero`` handles and have every
    builder observe the patch.
    """
    resolved = _PLUGIN_ALIASES.get(provider_id, provider_id)
    return {
        "cartesia": _cartesia,
        "google": _google,
        "groq": _groq,
        "openai": _openai,
        "silero": _silero,
    }.get(resolved)


def require_module(provider_id: str, capability: str) -> Any:
    module = provider_module(provider_id)
    if module is None:
        # The install instruction names the *package*, which for an aliased
        # provider is not the provider's own name. "Install livekit-plugins-ollama"
        # is advice nobody can follow -- the package does not exist -- and it is
        # the only thing standing between a confusing ImportError and a fix.
        package = _PLUGIN_ALIASES.get(provider_id, provider_id)
        raise RuntimeError(
            f"The {provider_id} provider is not installed. "
            f"Install the matching livekit-plugins-{package} package to use it for {capability}."
        )
    return module


@dataclass
class PipelineComponents:
    profile: str
    model_name: str
    interruption_mode: str
    llm: Any
    response_token_limit: int | None = None
    session_kwargs: dict[str, Any] = field(default_factory=dict)
    vad: Any | None = None
    stt: Any | None = None
    tts: Any | None = None


def _gemini_realtime_input_config(interruption_mode: str, silence_ms: int) -> Any:
    """Build conservative Gemini VAD settings for the selected mode."""
    from google.genai import types

    automatic = types.AutomaticActivityDetection(
        disabled=False,
        start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
        end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
        prefix_padding_ms=250,
        silence_duration_ms=silence_ms,
    )
    activity_handling = (
        types.ActivityHandling.NO_INTERRUPTION
        if interruption_mode == "finish_response"
        else types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
    )
    return types.RealtimeInputConfig(
        automatic_activity_detection=automatic,
        activity_handling=activity_handling,
    )


# ---------------------------------------------------------------------------
# Stage builders
#
# One function per capability. Each dispatches on the *provider id* from the
# resolved profile, which is what makes stages independently selectable.
# Constructors are deferred to threads because plugin constructors can load CA
# bundles and clients synchronously.
# ---------------------------------------------------------------------------


def _declared_options(stage: ResolvedStage, capability: Capability) -> dict[str, Any]:
    """The keyword arguments to build this stage with, per the catalog.

    Every builder goes through here, so there is no provider branch anywhere in
    the factory. A model declares the settings it accepts and how they are shaped;
    an unknown key in a saved profile is dropped rather than passed to a plugin
    that would reject it, and a value the model does not accept is replaced with
    one it does.

    An uncatalogued model gets its settings verbatim. Refusing to build would be
    worse than trying: a model added to a provider without a catalog update should
    still work, and the provider will complain far more clearly than we could.
    """
    model = get_model(stage.provider, stage.model, capability)
    if model is None:
        return dict(stage.options)
    return model.build_options(stage.options)


def _voice_keyword(stage: ResolvedStage) -> str:
    """The keyword this stage's plugin wants a voice passed as.

    Cartesia calls it `voice`; Google's TTS calls it `voice_name`. Hardcoding
    `voice` built a Google TTS session that raised `TypeError` on an unexpected
    keyword, after the profile had saved and validated.
    """
    model = get_model(stage.provider, stage.model, "tts")
    if model is None:
        return "voice"
    definition = model.option("voice")
    return definition.plugin_keyword if definition is not None else "voice"


async def _build_llm(stage: ResolvedStage) -> Any:
    module = require_module(stage.provider, "language")
    options = {"model": stage.model, **_declared_options(stage, "llm")}
    return await asyncio.to_thread(module.LLM, **options)


async def _build_tts(stage: ResolvedStage) -> Any:
    module = require_module(stage.provider, "speech")
    options = {"model": stage.model, **_declared_options(stage, "tts")}
    if stage.voice:
        options[_voice_keyword(stage)] = stage.voice
    return await asyncio.to_thread(module.TTS, **options)


async def _build_stt(stage: ResolvedStage) -> Any:
    module = require_module(stage.provider, "speech-to-text")
    options = {"model": stage.model, **_declared_options(stage, "stt")}
    return await asyncio.to_thread(module.STT, **options)


async def _build_vad(stage: ResolvedStage) -> Any:
    module = require_module(stage.provider, "voice activity detection")
    options = _declared_options(stage, "vad")
    return await asyncio.to_thread(module.VAD.load, **options)


async def _build_realtime(stage: ResolvedStage, interruption_mode: str) -> Any:
    """Build a speech-to-speech model.

    The module is resolved from the stage's own provider rather than a hardcoded
    one, so a second realtime provider needs a catalog entry and a plugin, not an
    edit here. Gemini is the only one with a builder's worth of turn-handling
    wiring; another provider without it is refused with a clear message rather
    than producing a session that cannot be interrupted correctly.

    A thinking level is not special-cased here. `_declared_options` drops it
    entirely for a model that takes no level, and downgrades a stale one to a level
    the model accepts, so the realtime path cannot send a parameter the model
    rejects. It used to default to the model's lowest accepted level, which was a
    way of always sending one -- and `gemini-3.8-live` rejects the parameter
    outright, so the session failed at setup with no audio.
    """
    try:
        from livekit.agents import APIConnectOptions
    except ImportError as exc:
        raise RuntimeError("Realtime models require livekit-agents>=1.8.2.") from exc

    module = require_module(stage.provider, "realtime")
    if not hasattr(module, "realtime"):
        raise RuntimeError(
            f"The {stage.provider} plugin has no realtime module. "
            "Add one in agent/pipeline_factory.py before selecting it."
        )

    # Turn handling is session wiring rather than a model option: the models
    # behind these are native-audio, so LiveKit needs to be told when a turn
    # ends. The rest comes from the catalog like any other stage.
    options: dict[str, Any] = {
        "model": stage.model,
        "voice": stage.voice,
        **_declared_options(stage, "realtime"),
        "realtime_input_config": _gemini_realtime_input_config(
            interruption_mode, stage.options.get("silence_duration_ms") or 700
        ),
        "conn_options": APIConnectOptions(
            max_retry=stage.options.get("connect_max_retry", 0),
            timeout=stage.options.get("connect_timeout", 10.0),
        ),
    }

    try:
        return module.realtime.RealtimeModel(**options)
    except Exception as exc:
        # The plugin's own message is the useful one. A missing key, an unknown
        # model and a bad parameter all raise here, and they do not have the same
        # fix -- replacing the underlying text with generic advice ("check the API
        # key and the model id") throws away the part that names which one it was,
        # which is the only thing a user can act on. So it is carried, not
        # discarded. The class stays RuntimeError because callers already handle
        # that as "this profile cannot be built".
        raise RuntimeError(
            f"Could not create the {stage.provider} realtime model: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


async def _build_realtime_session(resolved: ResolvedProfile) -> PipelineComponents:
    assert resolved.realtime is not None
    model = await _build_realtime(resolved.realtime, resolved.interruption_mode)
    options = resolved.realtime.options

    # A separate TTS turns this into LiveKit's half-cascade: the realtime model
    # understands speech, the TTS speaks the reply. Validation refuses this for a
    # native-audio model, so reaching here means the model really is text-capable.
    tts = None
    if resolved.output_mode == "custom_tts" and resolved.tts is not None:
        tts = await _build_tts(resolved.tts)

    session_kwargs: dict[str, Any] = {
        "llm": model,
        # A realtime model with server-side turn detection must be the only turn
        # detector, so the session's default VAD stays off.
        "vad": None,
        "turn_handling": {"turn_detection": "realtime_llm"},
        "max_tool_steps": resolved.max_tool_steps,
    }
    if tts is not None:
        session_kwargs["tts"] = tts

    return PipelineComponents(
        profile=resolved.profile_id,
        model_name=resolved.realtime.model,
        interruption_mode=resolved.interruption_mode,
        llm=model,
        response_token_limit=options.get("max_output_tokens"),
        tts=tts,
        session_kwargs=session_kwargs,
    )


async def _build_cascade_session(resolved: ResolvedProfile) -> PipelineComponents:
    try:
        from livekit.agents import APIConnectOptions
        from livekit.agents.voice.agent_session import SessionConnectOptions
    except ImportError as exc:
        raise RuntimeError("The STT-LLM-TTS pipeline requires livekit-agents>=1.8.2.") from exc

    async def prewarm_openai_resources():
        try:
            import openai.resources  # noqa: F401
        except ImportError:
            pass

    await asyncio.to_thread(prewarm_openai_resources)

    missing = [
        stage.provider
        for stage in (resolved.stt, resolved.llm, resolved.tts)
        if stage is not None and provider_module(stage.provider) is None
    ]
    if missing:
        raise RuntimeError(
            "These providers are not installed: "
            + ", ".join(sorted(set(missing)))
            + ". Install the matching livekit-plugins packages to use this profile."
        )

    stt = await _build_stt(resolved.stt) if resolved.stt else None
    llm = await _build_llm(resolved.llm) if resolved.llm else None
    tts = await _build_tts(resolved.tts) if resolved.tts else None
    vad = await _build_vad(resolved.vad) if resolved.vad else None

    return PipelineComponents(
        profile=resolved.profile_id,
        model_name=resolved.llm.model if resolved.llm else "unknown",
        interruption_mode=resolved.interruption_mode,
        llm=llm,
        response_token_limit=(resolved.llm.options.get("max_completion_tokens") if resolved.llm else None),
        vad=vad,
        stt=stt,
        tts=tts,
        session_kwargs={
            "vad": vad,
            "stt": stt,
            "llm": llm,
            "tts": tts,
            "conn_options": SessionConnectOptions(
                llm_conn_options=APIConnectOptions(max_retry=connect_max_retry()),
            ),
            # Preserve the current LiveKit cascade behavior while allowing the
            # UI preference to disable voice barge-in for complete responses.
            "turn_handling": {
                "interruption": {
                    "enabled": resolved.interruption_mode == "barge_in",
                },
            },
            "max_tool_steps": resolved.max_tool_steps,
            "min_endpointing_delay": resolved.min_endpointing_delay,
        },
    )


def _validation_source(resolved: ResolvedProfile) -> dict[str, Any]:
    """Rebuild a config-shaped profile from a resolved one, for validation.

    Validation is written against the stored document shape rather than against
    ``ResolvedProfile``, so the same rules serve the settings UI and the worker.
    """
    if resolved.kind == "pipeline":
        return {
            "pipeline": {
                "stt": _stage_dict(resolved.stt),
                "llm": _stage_dict(resolved.llm),
                "tts": _stage_dict(resolved.tts),
                "vad": _stage_dict(resolved.vad),
            },
            "turnHandling": {"interruptionMode": resolved.interruption_mode},
        }

    output: dict[str, Any] = {"mode": resolved.output_mode}
    if resolved.output_mode == "custom_tts":
        output["tts"] = _stage_dict(resolved.tts)
        if resolved.tts is not None and resolved.tts.voice:
            output["voice"] = resolved.tts.voice

    realtime: dict[str, Any] = {"output": output}
    if resolved.realtime is not None:
        realtime.update(
            {
                "provider": resolved.realtime.provider,
                "model": resolved.realtime.model,
                "voice": resolved.realtime.voice,
            }
        )
    return {
        "realtime": realtime,
        "turnHandling": {"interruptionMode": resolved.interruption_mode},
    }


async def build_resolved(
    resolved: ResolvedProfile,
    *,
    validate: bool = True,
) -> tuple[PipelineComponents, list[Diagnostic]]:
    """Build a session from a resolved profile.

    Returns the diagnostics alongside the components rather than raising on a
    validation problem, so the caller can surface a precise reason to the user
    before anything is published to a room.
    """
    diagnostics: list[Diagnostic] = []
    if validate:
        diagnostics = validate_profile(
            {
                "id": "resolved",
                "name": resolved.name,
                "kind": resolved.kind,
                **_validation_source(resolved),
            },
            # Presence, not validity: this says a key is *set*, not that it works.
            # Passing None here skipped the check entirely, so a profile missing
            # its Cartesia key saved clean, validated clean, and failed at
            # construction with a 401 on the user's first spoken turn. The
            # environment is the right place to look because it is where the
            # desktop app injects the keyring credentials.
            credentials=env_credential_status(),
        )
        if any(d.severity == "error" for d in diagnostics):
            raise ConfigurationRejected(diagnostics)

    if resolved.kind == "realtime":
        return await _build_realtime_session(resolved), diagnostics
    return await _build_cascade_session(resolved), diagnostics


class ConfigurationRejected(RuntimeError):
    """A profile cannot be built. Carries the diagnostics for the caller."""

    def __init__(self, diagnostics: list[Diagnostic]):
        first = diagnostics[0]
        super().__init__(first.message if first else "The configuration was rejected.")
        self.diagnostics = diagnostics


def _stage_dict(stage: ResolvedStage | None) -> dict[str, Any]:
    if stage is None:
        return {}
    value: dict[str, Any] = {"provider": stage.provider, "model": stage.model}
    if stage.voice:
        value["voice"] = stage.voice
    for key in ("language", "speed"):
        if key in stage.options:
            value[key] = stage.options[key]
    return value


async def build_pipeline(
    profile: str | None = None,
    interruption_mode: str | None = None,
) -> PipelineComponents:
    """The environment-driven entry point used by ``agent.py``.

    Unchanged behaviour: ``LUMINE_PIPELINE`` selects between the native Gemini
    Live profile and the preserved Groq/Silero/Cartesia cascade. The only
    observable difference from before is the Cartesia TTS model, which moved off
    the retiring Sonic 2.
    """
    selected = pipeline_name(profile)
    selected_interruption_mode = normalize_interruption_mode(interruption_mode)
    # Pass the resolved name through: re-reading LUMINE_PIPELINE here could
    # disagree with the pipeline the caller asked for.
    resolved = resolve_profile(
        env_profile(selected), interruption_mode=selected_interruption_mode
    )
    # Runtime events and logs report the pipeline that was actually built, which
    # is the pipeline name rather than the env profile's read-only id.
    resolved = replace(resolved, profile_id=selected)
    components, _ = await build_resolved(resolved, validate=False)
    return components
