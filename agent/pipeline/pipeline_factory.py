"""Factories for Lumine's voice pipelines.

Stages are built from a :class:`~agent.settings.config_store.ResolvedProfile` rather than
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
    from ..settings.config_store import ResolvedProfile, ResolvedStage, env_profile, resolve_profile
    from ..runtime.emotion_voice import remember_baseline
    from ..runtime.frame_sampler import FreshnessVideoSampler
    from ..settings.llm_config import connect_max_retry
    from ..settings.pipeline_config import pipeline_name
    from ..settings.providers import Capability, get_model
    from ..settings.session_preferences import normalize_interruption_mode
    from ..settings.validation import (
        Diagnostic,
        diagnostic,
        env_credential_status,
        validate_profile,
    )
except ImportError:
    from settings.config_store import ResolvedProfile, ResolvedStage, env_profile, resolve_profile
    from runtime.emotion_voice import remember_baseline
    from runtime.frame_sampler import FreshnessVideoSampler
    from settings.llm_config import connect_max_retry
    from settings.pipeline_config import pipeline_name
    from settings.providers import Capability, get_model
    from settings.session_preferences import normalize_interruption_mode
    from settings.validation import (
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
    #: What the room should subscribe this session to, resolved from the catalog
    #: rather than configured. Always a real ``RoomOptions`` and never ``None``:
    #: ``session.start`` treats ``None`` as a supplied-but-wrong type and raises,
    #: so "off" has to be an options object that says off.
    room_options: Any = None
    #: Whether this session's model can read a frame at all, from the catalog's
    #: own ``input_modalities``. Kept beside ``room_options`` rather than
    #: re-derived from it, because ``RoomOptions`` says what the *room* will
    #: subscribe to and this is a statement about the *model* -- the two agree
    #: today only because both come from ``_stage_sees_frames``, and the vision
    #: block tells the model the second one in words.
    sees_frames: bool = False
    vad: Any | None = None
    stt: Any | None = None
    tts: Any | None = None
    #: Which provider is answering, and therefore what it charges. Carried so the
    #: context budget can be worked out for *this* model rather than a guess: a
    #: per-minute allowance is a property of the provider, and a local model has
    #: none. Without it the conversation would be trimmed to a cloud tier's limit
    #: on a machine that never had one.
    llm_provider: str = ""


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


def _stage_sees_frames(stage: ResolvedStage, capability: Capability) -> bool:
    """Whether a camera frame published to the room would reach this stage.

    Read from the catalog's own ``input_modalities`` rather than decided here,
    because the alternative is the exact failure this file already had once: a
    constant that compiles is not a constant that is true of the service it names.

    An uncatalogued model answers ``False``, which is the safe direction. LiveKit
    documents that turning on ``video_input`` for a model that cannot read frames
    *silently ignores them* -- no error, no warning, and a session that looks
    healthy while the frames go nowhere. Refusing to subscribe is the only answer
    that cannot be mistaken for working.

    This is also why the cascade path answers ``False`` without consulting
    anything: its LLM stage is handed a chat history of strings, and a frame
    injected into that history is a separate feature with its own modality story.
    """
    model = get_model(stage.provider, stage.model, capability)
    return model is not None and model.sees


def _room_options(video_input: bool) -> Any:
    """A ``RoomOptions`` that subscribes to video exactly when it can be read.

    Not ``RoomInputOptions``: LiveKit deprecated it, and passing one logs
    "use RoomOptions instead" on every session. It is also no longer importable
    from ``voice.agent_session`` -- the two live in different modules now, which
    is a rename that fails loudly rather than silently, so it is worth naming.

    ``video_input`` defaults to False, so a session that cannot use frames
    subscribes to nothing and the cost of saying so is zero.
    """
    try:
        from livekit.agents.voice.room_io import RoomOptions
    except ImportError as exc:  # pragma: no cover - depends on the installed SDK
        raise RuntimeError(
            "Video input requires a livekit-agents version that provides "
            "livekit.agents.voice.room_io.RoomOptions."
        ) from exc
    return RoomOptions(video_input=video_input)


async def _build_llm(stage: ResolvedStage) -> Any:
    module = require_module(stage.provider, "language")
    options = {"model": stage.model, **_declared_options(stage, "llm")}
    return await asyncio.to_thread(module.LLM, **options)


async def _build_tts(stage: ResolvedStage) -> Any:
    module = require_module(stage.provider, "speech")
    options = {"model": stage.model, **_declared_options(stage, "tts")}
    if stage.voice:
        options[_voice_keyword(stage)] = stage.voice
    tts = await asyncio.to_thread(module.TTS, **options)
    # Write down what this was built with before anything can change it. Which
    # settings exist, and what they default to, is the catalog's decision and
    # already made above; this is only the observation that a synthesizer's
    # options are mutated in place for as long as the session lasts, so the
    # configured value has to be kept somewhere it will not be overwritten.
    remember_baseline(tts, options.get("emotion"))
    return tts


async def _build_stt(stage: ResolvedStage) -> Any:
    module = require_module(stage.provider, "speech-to-text")
    options = {"model": stage.model, **_declared_options(stage, "stt")}
    return await asyncio.to_thread(module.STT, **options)


async def _build_vad(stage: ResolvedStage) -> Any:
    module = require_module(stage.provider, "voice activity detection")
    options = _declared_options(stage, "vad")
    return await asyncio.to_thread(module.VAD.load, **options)


#: Where Gemini Live starts compressing its own context, in tokens.
#:
#: The measured failure this answers: a session was observed re-sending **~44,000
#: text tokens and ~110,000 audio tokens per turn** after ten minutes, with the
#: fixed prompt only 6,500 of them. `runtime/context_trim.py` cannot prevent this.
#: It counts *chat items*, and a realtime session's history is mostly audio, which
#: arrives as `input_audio_tokens` and is not an item anything can drop. Trimming
#: text items alone left the audio growing by ~1,500 tokens a turn for as long as
#: the user kept talking.
#:
#: So the provider does the compression, because the provider is the only thing
#: that can see the audio. Gemini Live's sliding window drops the oldest turns once
#: the context passes this threshold, which is the mechanism designed for exactly
#: this and costs nothing when a conversation is short.
#:
#: Sized against the window rather than picked: Gemini Live holds ~1M, and firing
#: at 65% leaves room for a long tool call's result plus a reply before the next
#: compression. A lower number would compress during ordinary conversation and
#: throw away context the model is still using.
CONTEXT_COMPRESSION_TRIGGER_TOKENS = 650_000


def _context_window_compression() -> Any:
    """Gemini's own context compression, or ``None`` when unavailable.

    ``None`` rather than a raise, deliberately. This is a provider capability
    arriving in a plugin, and an older plugin without the parameter should cost a
    long session some efficiency -- not cost it the ability to talk. The
    constructor is only asked for the keyword it actually takes, so a plugin that
    predates the option does not fail on an unexpected argument either.
    """
    try:
        from google.genai import types as genai_types
    except ImportError:  # pragma: no cover - google-genai ships with the plugin
        return None
    try:
        return genai_types.ContextWindowCompressionConfig(
            trigger_tokens=CONTEXT_COMPRESSION_TRIGGER_TOKENS,
            sliding_window=genai_types.SlidingWindow(),
        )
    except Exception:  # noqa: BLE001 - an absent capability must not break setup
        logger.debug("[Pipeline] context window compression unavailable", exc_info=True)
        return None


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
            "Add one in agent/pipeline/pipeline_factory.py before selecting it."
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

    # Ask the provider to bound its own context. See
    # `CONTEXT_COMPRESSION_TRIGGER_TOKENS` for why the item-count trimmer cannot do
    # this job: a realtime session's history is audio, and audio is not a chat item.
    compression = _context_window_compression()
    if compression is not None:
        try:
            import inspect as _inspect

            if "context_window_compression" in _inspect.signature(module.realtime.RealtimeModel).parameters:
                options["context_window_compression"] = compression
        except (TypeError, ValueError, AttributeError):
            # An older plugin without the keyword. The session still works; it just
            # grows. Logged rather than silent, because "the context is unbounded"
            # is a thing somebody has to know.
            logger.info(
                "[Pipeline] this realtime plugin takes no context_window_compression; "
                "a long session will keep re-sending its whole history"
            )

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

    # Whether a frame reaches the model at all, decided by the catalog. The two
    # reasons this is not simply `True` are both quiet: LiveKit drops frames from
    # a model that cannot read them without saying so, and a native-audio model
    # that declares no image input would look identical while doing it.
    can_see = _stage_sees_frames(resolved.realtime, "realtime")

    if can_see:
        # Declared rather than inherited. The library default is exactly these
        # numbers, so this is not a behaviour change -- it is a behaviour change
        # that has to be *read*, because a frame rate nobody chose is a frame
        # rate nobody can reason about when the bill arrives. Gemini tokenizes
        # every frame by its dimensions, so this is money.
        #
        # The sampler is ours rather than `VoiceActivityVideoSampler`, which only
        # asks whether enough time has passed. Frames reach it from an unbounded
        # queue, so "enough time" can be measured against a picture captured
        # before the event loop stalled -- see `runtime/frame_sampler.py`.
        session_kwargs["video_sampler"] = FreshnessVideoSampler(
            speaking_fps=1.0,
            silent_fps=0.3,
        )

    return PipelineComponents(
        profile=resolved.profile_id,
        model_name=resolved.realtime.model,
        interruption_mode=resolved.interruption_mode,
        llm=model,
        llm_provider=resolved.realtime.provider,
        response_token_limit=options.get("max_output_tokens"),
        tts=tts,
        session_kwargs=session_kwargs,
        room_options=_room_options(can_see),
        sees_frames=can_see,
    )


async def _build_cascade_session(resolved: ResolvedProfile) -> PipelineComponents:
    try:
        from livekit.agents import APIConnectOptions
        from livekit.agents.voice.agent_session import SessionConnectOptions
    except ImportError as exc:
        raise RuntimeError("The STT-LLM-TTS pipeline requires livekit-agents>=1.8.2.") from exc

    def prewarm_openai_resources():
        # A plain function, deliberately. This used to be `async def`, handed to
        # `asyncio.to_thread` -- which calls it in a worker thread and gets a
        # *coroutine object* back. Nothing ever awaited it, so the body never ran
        # and the pre-import it was written for never happened, while every single
        # cascade session build printed
        # `RuntimeWarning: coroutine 'prewarm_openai_resources' was never awaited`.
        # The import is also not worth a thread: it is a module already imported by
        # the Groq plugin this stage is about to construct.
        try:
            import openai.resources  # noqa: F401
        except ImportError:
            pass

    prewarm_openai_resources()

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
        llm_provider=(resolved.llm.provider if resolved.llm else ""),
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
        # Said off, and said deliberately. On the installed 1.8.3 nothing can
        # put a frame into a cascade conversation: `AgentActivity.push_video`
        # forwards only to `_rt_session` (the realtime model), and when there is
        # none the frame is dropped. There is no image-message path to turn on.
        # Even if there were, the LLM stage here is handed a history of strings
        # and declares `("text",)` in the catalog, so subscribing would promise
        # frames the stage cannot use -- LiveKit ignores frames a model cannot
        # read *without raising*, which is the failure that hides itself.
        room_options=_room_options(False),
        # And recorded beside it, because the model is told this in words. The
        # vision block states `can_see` so a cascade session says "this model
        # cannot see" instead of leaving her to infer it from frames that never
        # arrive -- which is an absence, and an absence is the one thing a model
        # cannot distinguish from a bug.
        sees_frames=False,
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
