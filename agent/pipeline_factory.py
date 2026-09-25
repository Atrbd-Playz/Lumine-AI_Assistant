"""Factories for the selectable Lumine voice pipelines.

The legacy factory intentionally keeps the existing provider construction and
LiveKit connection options. Provider plugins are registered on the worker main
thread; only their potentially blocking constructors are deferred to threads.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

try:
    from .llm_config import connect_max_retry, llm_config
    from .pipeline_config import gemini_settings, pipeline_name
    from .session_preferences import normalize_interruption_mode
except ImportError:
    from llm_config import connect_max_retry, llm_config
    from pipeline_config import gemini_settings, pipeline_name
    from session_preferences import normalize_interruption_mode

# LiveKit plugins must be registered on the process's main thread. Import the
# provider modules here, before any job/event loop is created, and only defer
# their potentially blocking constructors to worker threads.
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
    from livekit.plugins import silero as _silero
except ImportError:
    _silero = None

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


def _gemini_realtime_input_config(interruption_mode: str) -> Any:
    """Build conservative Gemini VAD settings for the selected mode."""
    from google.genai import types

    automatic = types.AutomaticActivityDetection(
        disabled=False,
        start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
        end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
        prefix_padding_ms=250,
        silence_duration_ms=800 if interruption_mode == "finish_response" else 700,
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


async def _build_gemini_live(interruption_mode: str) -> PipelineComponents:
    try:
        from livekit.agents import APIConnectOptions
    except ImportError as exc:
        raise RuntimeError("Gemini Live requires livekit-agents>=1.8.2.") from exc
    if _google is None:
        raise RuntimeError("Gemini Live requires livekit-plugins-google>=1.8.2.")

    settings = gemini_settings()
    options: dict[str, Any] = {
        "model": settings["model"],
        "voice": settings["voice"],
        "language": settings["language"],
        "vertexai": False,
        "thinking_config": settings["thinking_config"],
        "max_output_tokens": settings["max_output_tokens"],
        "realtime_input_config": _gemini_realtime_input_config(interruption_mode),
        "conn_options": APIConnectOptions(
            max_retry=settings["connect_max_retry"],
            timeout=settings["connect_timeout"],
        ),
    }
    if "temperature" in settings:
        options["temperature"] = settings["temperature"]

    try:
        model = _google.realtime.RealtimeModel(**options)
    except Exception as exc:
        raise RuntimeError(
            "Could not create Gemini Live model. Check GOOGLE_API_KEY and GEMINI_MODEL."
        ) from exc

    return PipelineComponents(
        profile="gemini_live",
        model_name=settings["model"],
        interruption_mode=interruption_mode,
        llm=model,
        response_token_limit=settings["max_output_tokens"],
        # Gemini's native audio model supplies STT, turn detection, and TTS.
        session_kwargs={
            "llm": model,
            # Explicitly disable AgentSession's default Silero VAD so Gemini's
            # native activity/turn detection remains the only turn detector.
            "vad": None,
            "turn_handling": {"turn_detection": "realtime_llm"},
            "max_tool_steps": settings["max_tool_steps"],
        },
    )


async def _build_legacy_cascade(interruption_mode: str) -> PipelineComponents:
    def prewarm_openai_resources():
        try:
            import openai.resources  # noqa: F401
        except ImportError:
            pass

    await asyncio.to_thread(prewarm_openai_resources)

    try:
        from livekit.agents import APIConnectOptions
        from livekit.agents.voice.agent_session import SessionConnectOptions
    except ImportError as exc:
        raise RuntimeError("The legacy pipeline requires livekit-agents>=1.8.2.") from exc
    if any(plugin is None for plugin in (_cartesia, _groq, _silero)):
        raise RuntimeError(
            "The legacy pipeline requires the LiveKit Groq, Silero, and Cartesia plugins."
        )

    settings = llm_config()
    # Plugin constructors can load CA bundles and clients synchronously. Keep
    # that work off the event loop exactly as the previous agent.py did.
    vad = await asyncio.to_thread(_silero.VAD.load, min_speech_duration=0.4)
    stt = await asyncio.to_thread(_groq.STT)
    llm = await asyncio.to_thread(_groq.LLM, **settings)
    tts = await asyncio.to_thread(
        _cartesia.TTS,
        model="sonic-2",
        voice="002622d8-19d0-4567-a16a-f99c7397c062",
        language="en",
        speed=0.95,
    )

    return PipelineComponents(
        profile="legacy_cascade",
        model_name=settings["model"],
        interruption_mode=interruption_mode,
        llm=llm,
        response_token_limit=settings["max_completion_tokens"],
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
                    "enabled": interruption_mode == "barge_in",
                },
            },
            "max_tool_steps": 3,
            "min_endpointing_delay": 0.4,
        },
    )


async def build_pipeline(
    profile: str | None = None,
    interruption_mode: str | None = None,
) -> PipelineComponents:
    selected = pipeline_name(profile)
    selected_interruption_mode = normalize_interruption_mode(interruption_mode)
    if selected == "gemini_live":
        return await _build_gemini_live(selected_interruption_mode)
    return await _build_legacy_cascade(selected_interruption_mode)
