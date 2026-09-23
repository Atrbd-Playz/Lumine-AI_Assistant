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

try:
    from livekit.plugins import (
        groq,
        silero,
        cartesia,
    )
except ImportError:
    groq = None
    silero = None
    cartesia = None

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
    record = {"type": event_type}
    for key, value in payload.items():
        if key != "type":
            record[key] = value
    print(f"LUMINE_EVENT {json.dumps(record)}", flush=True)


async def publish_emotion_event(room: rtc.Room, event: dict[str, object]) -> None:
    payload = event.get("payload", {})
    if EMOTION_DEBUG:
        logger.info("[Emotion] resolved: %s %.2f (%s)", payload.get("primary"), payload.get("intensity"), payload.get("source"))
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


class Lumine(Agent):
    def __init__(self):
        super().__init__(
            instructions=PERSONA
        )


async def entrypoint(ctx: JobContext):
    logger.info(f"Connecting to room: {ctx.room.name}")

    await ctx.connect()
    emit_runtime_event("connected", room=ctx.room.name)

    groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
    if EMOTION_DEBUG:
        logger.info("[Emotion] Groq model: %s; structured emotion output: disabled; resolver: heuristic fallback", groq_model)

    session = AgentSession(
    vad=silero.VAD.load(
        min_speech_duration=0.4,
    ),

    stt=groq.STT(),

    llm=groq.LLM(
        model=groq_model,
        temperature=0.7,
    ),

    tts=cartesia.TTS(
        model="sonic-2",
        voice="002622d8-19d0-4567-a16a-f99c7397c062",  #Huda voice ID
        language="en",
        speed=0.95,
    ),

    min_endpointing_delay=0.4,
)

    @session.on("conversation_item_added")
    def on_conversation_item_added(event):
        item = event.item
        content = getattr(item, "content", [])
        text = " ".join(
            part if isinstance(part, str) else getattr(part, "text", "")
            for part in content
        ).strip()
        if text:
            emit_runtime_event(
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
                        emit_runtime_event(
                            event["type"],
                            **{key: value for key, value in event.items() if key != "type"},
                        )

                asyncio.create_task(publish_sequence(emotion_events))

    @session.on("error")
    def on_session_error(event):
        emit_runtime_event("error", message=str(getattr(event, "error", event)))

    async def close_session(reason: str = "room ended"):
        emit_runtime_event("session_ending", room=ctx.room.name, reason=reason)
        await session.aclose()
        emit_runtime_event("session_ended", room=ctx.room.name, reason=reason)

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
        agent=Lumine(),
    )

    await session.generate_reply(
        instructions="""
Introduce yourself as Lumine.
Greet the user briefly and ask how you can help.
"""
    )


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