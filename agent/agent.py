import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from livekit.agents import (
    Agent,
    AgentSession,
    AgentServer,
    JobContext,
    WorkerOptions,
    cli,
)
from livekit import rtc

from livekit.plugins import (
    groq,
    silero,
    cartesia,
)

AGENT_DIR = Path(__file__).resolve().parent
PERSONA = (AGENT_DIR / "prompts" / "persona.md").read_text(encoding="utf-8")

# Tauri launches this script with the repository root as cwd; keep worker
# configuration anchored to the agent directory like manual `python agent.py`.
load_dotenv(AGENT_DIR / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lumine")
WORKER_STARTED_AT = time.monotonic()


def emit_runtime_event(event_type: str, **payload):
    print(f"LUMINE_EVENT {json.dumps({'type': event_type, **payload})}", flush=True)


class Lumine(Agent):
    def __init__(self):
        super().__init__(
            instructions=PERSONA
        )


async def entrypoint(ctx: JobContext):
    logger.info(f"Connecting to room: {ctx.room.name}")

    await ctx.connect()
    emit_runtime_event("connected", room=ctx.room.name)

    session = AgentSession(
    vad=silero.VAD.load(
        min_speech_duration=0.4,
    ),

    stt=groq.STT(),

    llm=groq.LLM(
        model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
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