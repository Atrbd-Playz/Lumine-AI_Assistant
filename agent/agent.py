import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

try:
    from .emotion_contract import build_emotion_event, extract_emotion_sequence, infer_emotion_from_text
except ImportError:
    from emotion_contract import build_emotion_event, extract_emotion_sequence, infer_emotion_from_text

try:
    from .latency import LatencyTracker
    from .pipeline_factory import build_pipeline
    from .pipeline_config import pipeline_name
    from .runtime_events import RuntimeEventPublisher, ToolEventBridge
    from .session_preferences import interruption_mode_from_metadata
    from .tools.http_client import acquire as acquire_http_client, release as release_http_client
    from .tools.tools_policy import compose_instructions
    from .tools.tools_registry import get_tools, tool_ids
except ImportError:
    from latency import LatencyTracker
    from pipeline_factory import build_pipeline
    from pipeline_config import pipeline_name
    from runtime_events import RuntimeEventPublisher, ToolEventBridge
    from session_preferences import interruption_mode_from_metadata
    from tools.http_client import acquire as acquire_http_client, release as release_http_client
    from tools.tools_policy import compose_instructions
    from tools.tools_registry import get_tools, tool_ids

try:
    from livekit.agents import (
        Agent,
        AgentSession,
        AgentServer,
        JobContext,
        WorkerOptions,
        cli,
    )
except ImportError:
    class Agent:
        def __init__(self, *args, **kwargs):
            pass

    class AgentSession:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("livekit-agents is required to run the Lumine voice worker.")

    class _MissingServer:
        @staticmethod
        def from_server_options(_options):
            return _MissingServer()

        def on(self, *_args, **_kwargs):
            return lambda fn: fn

    class AgentServer:
        @staticmethod
        def from_server_options(_options):
            return _MissingServer()

    class JobContext:
        pass

    class WorkerOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _MissingCli:
        def run_app(self, *_args, **_kwargs):
            raise RuntimeError("livekit-agents is required to run the Lumine voice worker.")

    cli = _MissingCli()

try:
    from livekit import rtc
except ImportError:
    class _MissingRTC:
        class ParticipantKind:
            PARTICIPANT_KIND_AGENT = "agent"
    rtc = _MissingRTC()


AGENT_DIR = Path(__file__).resolve().parent
PERSONA = (AGENT_DIR / "prompts" / "persona.md").read_text(encoding="utf-8")

# Tauri launches this script with the repository root as cwd; keep worker
# configuration anchored to the agent directory like manual `python agent.py`.
load_dotenv(AGENT_DIR / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lumine")
WORKER_STARTED_AT = time.monotonic()
EMOTION_DEBUG = os.getenv("LUMINE_DEBUG_EMOTION", "false").lower() == "true"


def emit_runtime_event(event_type: str, **payload):
    """Keep the worker bootstrap event contract for Tauri readiness handling."""
    record = {"type": event_type}
    record.update({key: value for key, value in payload.items() if key != "type"})
    print(f"LUMINE_EVENT {json.dumps(record, default=str, separators=(',', ':'))}", flush=True)
    return record


async def publish_emotion_event(room: rtc.Room, event: dict[str, object]) -> None:
    payload = event.get("payload", {})
    if EMOTION_DEBUG:
        logger.info(
            "[Emotion] resolved: %s %.2f (%s)",
            payload.get("primary"),
            payload.get("intensity"),
            payload.get("source"),
        )
        logger.info("[Emotion] publishing LiveKit event")
    try:
        await room.local_participant.publish_data(
            json.dumps(event),
            reliable=True,
            topic="lumine.emotion",
        )
        if EMOTION_DEBUG:
            logger.info("[Emotion] packet published")
    except Exception:
        logger.exception("[Emotion] packet publish failed")


def build_instructions(profile: str) -> str:
    instructions = compose_instructions(PERSONA)
    if profile == "gemini_live":
        instructions += """

## Response continuity
Finish the current response before yielding, unless the user clearly interrupts.
"""
    return instructions


class Lumine(Agent):
    def __init__(self, tools=None, profile: str = "gemini_live"):
        super().__init__(
            instructions=build_instructions(profile),
            tools=tools if tools is not None else get_tools(),
        )


async def entrypoint(ctx: JobContext):
    publisher = RuntimeEventPublisher(ctx.room, room_name=ctx.room.name)
    latency = LatencyTracker(publisher.emit)
    latency.mark("entrypoint", room=ctx.room.name)

    logger.info("Connecting to room: %s", ctx.room.name)
    tools = get_tools()
    interruption_mode = interruption_mode_from_metadata(getattr(getattr(ctx, "job", None), "metadata", None))
    logger.info("Voice interruption mode: %s", interruption_mode)
    logger.info("Enabled tools: %s", ", ".join(tool_ids(tools)) or "none")

    await ctx.connect()
    publisher.emit("connected", room=ctx.room.name)

    profile = "unknown"
    try:
        profile = pipeline_name()
        logger.info("Building voice pipeline: %s (%s)", profile, interruption_mode)
        components = await build_pipeline(profile, interruption_mode=interruption_mode)
    except Exception as exc:
        publisher.emit("error", message=str(exc), source="pipeline")
        latency.mark("pipeline_error", profile=profile)
        raise
    latency.mark(
        "pipeline_built",
        profile=components.profile,
        model=components.model_name,
        interruption_mode=components.interruption_mode,
        response_token_limit=components.response_token_limit,
    )
    logger.info(
        "Voice pipeline ready: profile=%s model=%s interruption_mode=%s",
        components.profile,
        components.model_name,
        components.interruption_mode,
    )

    session = AgentSession(**components.session_kwargs)
    tool_events = ToolEventBridge(publisher)

    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        item = event.item
        content = getattr(item, "content", [])
        text = " ".join(
            part if isinstance(part, str) else getattr(part, "text", "")
            for part in content
        ).strip()
        if not text:
            return

        publisher.emit(
            "conversation",
            role=getattr(item, "role", "assistant"),
            content=text,
        )

        role = getattr(item, "role", "")
        if role == "assistant":
            emotion_events = []
            for part in re.split(r"\b(?:then|and|but|while|so|as|because)\b|[;,.!?]+", text):
                if not part.strip():
                    continue
                emotion_events.extend(extract_emotion_sequence(part, source="llm"))
            if not emotion_events:
                emotion_events = extract_emotion_sequence(text, source="llm")
        elif role == "user":
            lower = text.lower()
            roleplay_cues = (
                "act ", "be ", "pretend to be", "roleplay", "play as", "do a", "look ", "blush", "smile", "laugh",
                "wink", "shy", "proud", "angry", "jealous", "curious", "calm", "surprised", "happy", "excited"
            )
            if any(cue in lower for cue in roleplay_cues):
                emotion_events = []
                for part in re.split(r"\b(?:then|and|but|while|so|as|because)\b|[;,.!?]+", text):
                    if not part.strip():
                        continue
                    emotion_events.extend(extract_emotion_sequence(part, source="user"))
                if not emotion_events:
                    emotion_events = extract_emotion_sequence(text, source="user")
            else:
                emotion_events = []
        else:
            emotion_events = []

        if emotion_events:
            async def publish_sequence(events):
                for index, event in enumerate(events):
                    if index:
                        await asyncio.sleep(0.5)
                    await publish_emotion_event(ctx.room, event)
                    publisher.emit(
                        event["type"],
                        **{key: value for key, value in event.items() if key != "type"},
                    )

            asyncio.create_task(publish_sequence(emotion_events))

    @session.on("tool_execution_updated")
    def on_tool_execution_updated(event):
        tool_events.handle(event)

    @session.on("function_tools_executed")
    def on_function_tools_executed(event):
        # The updated event is the normal path. This batch handler is retained
        # as a compatibility fallback for SDK versions that omit one update.
        tool_events.handle_function_tools(event)

    @session.on("agent_state_changed")
    def on_agent_state_changed(event):
        state = getattr(event, "new_state", None)
        if state:
            latency.mark("agent_state", state=state)

    @session.on("user_state_changed")
    def on_user_state_changed(event):
        state = getattr(event, "new_state", None)
        if state:
            latency.mark("user_state", state=state)
            publisher.emit("user_state", state=state)

    @session.on("agent_false_interruption")
    def on_false_interruption(event):
        publisher.emit(
            "false_interruption",
            resumed=bool(getattr(event, "resumed", False)),
        )

    @session.on("close")
    def on_session_close(event):
        publisher.emit("session_close", reason=str(getattr(event, "reason", "unknown")))

    @session.on("speech_created")
    def on_speech_created(event):
        latency.mark(
            "speech_created",
            source=getattr(event, "source", None),
            user_initiated=getattr(event, "user_initiated", None),
        )
        speech_handle = getattr(event, "speech_handle", None)
        add_done_callback = getattr(speech_handle, "add_done_callback", None)
        if callable(add_done_callback):
            def on_speech_done(handle):
                publisher.emit(
                    "speech_finished",
                    speech_id=getattr(handle, "id", None),
                    interrupted=bool(getattr(handle, "interrupted", False)),
                    allow_interruptions=bool(getattr(handle, "allow_interruptions", True)),
                )

            add_done_callback(on_speech_done)

    @session.on("session_usage_updated")
    def on_session_usage_updated(event):
        usage = getattr(event, "usage", None)
        model_usage = getattr(usage, "model_usage", []) if usage is not None else []
        serialized_usage = []
        for item in model_usage:
            try:
                if hasattr(item, "model_dump"):
                    serialized_usage.append(item.model_dump(mode="json"))
                else:
                    serialized_usage.append(str(item))
            except Exception:
                serialized_usage.append(repr(item))
        publisher.emit(
            "session_usage",
            usage=serialized_usage,
            response_token_limit=components.response_token_limit,
        )

    @session.on("error")
    def on_session_error(event):
        publisher.emit(
            "error",
            message=str(getattr(event, "error", event)),
            source=str(getattr(event, "source", "session")),
        )

    async def close_session(reason: str = "room ended"):
        publisher.emit("session_ending", room=ctx.room.name, reason=reason)
        try:
            await session.aclose()
        finally:
            # The last session to finish closes the shared HTTP client.
            await release_http_client()
        publisher.emit("session_ended", room=ctx.room.name, reason=reason)
        await publisher.aclose()

    ctx.add_shutdown_callback(close_session)

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant):
        remaining_users = [
            remote
            for remote in ctx.room.remote_participants.values()
            if remote.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        ]
        if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT and not remaining_users:
            ctx.shutdown(reason="user left room")

    await session.start(
        room=ctx.room,
        agent=Lumine(tools, profile=components.profile),
    )
    latency.mark(
        "session_started",
        profile=components.profile,
        model=components.model_name,
        interruption_mode=components.interruption_mode,
        response_token_limit=components.response_token_limit,
    )
    publisher.emit(
        "session_started",
        room=ctx.room.name,
        profile=components.profile,
        model=components.model_name,
        interruption_mode=components.interruption_mode,
        response_token_limit=components.response_token_limit,
    )

    # Hold the shared HTTP client for the lifetime of this job, so every tool
    # call reuses one SSL context and one connection pool.
    await acquire_http_client()

    latency.mark("greeting_requested")
    await session.generate_reply(
        instructions="""
Introduce yourself as Lumine.
Greet the user briefly and ask how you can help.
"""
    )
    latency.mark("greeting_requested_done")


options = WorkerOptions(
    entrypoint_fnc=entrypoint,
    agent_name="lumine",
)
server = AgentServer.from_server_options(options)


def on_worker_registered(worker_id, _server_info):
    emit_runtime_event(
        "worker_registered",
        worker_id=worker_id,
        agent_name="lumine",
        registration_ms=round((time.monotonic() - WORKER_STARTED_AT) * 1000),
    )


server.on("worker_registered", on_worker_registered)

if __name__ == "__main__":
    cli.run_app(server)
