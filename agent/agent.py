import json
import logging
import os
import time
import asyncio
from pathlib import Path

from dotenv import load_dotenv

try:
    from .emotion_contract import build_emotion_event
except ImportError:
    from emotion_contract import build_emotion_event

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


EMOTION_DEBUG = os.getenv("LUMINE_DEBUG_EMOTION", "false").lower() == "true"


def emit_runtime_event(event_type: str, **payload):
    record = {"type": event_type}
    for key, value in payload.items():
        if key != "type":
            record[key] = value
    print(f"LUMINE_EVENT {json.dumps(record)}", flush=True)


def infer_emotion_from_text(text: str, source: str = "llm") -> dict[str, object] | None:
    if not text:
        return None

    lower = text.lower()
    hints = {
        "happy": ["yay", "so happy", "delighted", "thrilled", "i'm really happy", "i'm glad", "beautiful", "that makes me smile"],
        "excited": ["excited", "that is exciting", "so exciting", "thrilled", "let's go", "this is huge"],
        "playful": ["hehe", "cute", "adorable", "act cute", "be cute", "tease", "joke", "playful", "be playful", "you got me", "caught you", "silly", "banter"],
        "jealous": ["jealous", "act jealous", "be jealous", "who is she", "you seem interested in her", "i'm not jealous", "hmm, who's that"],
        "thinking": ["let me think", "hmm", "consider", "ponder", "evaluate", "analyze"],
        "confused": ["confused", "unclear", "not sure", "what do you mean", "huh", "i don't get it"],
        "sad": ["sad", "down", "upset", "hurt", "disappointed", "lonely", "i'm sorry that happened"],
        "angry": ["angry", "mad", "furious", "annoyed", "frustrated", "that's not fair", "i'm upset"],
        "curious": ["curious", "wonder", "ask", "learn", "tell me more", "what happened"],
        "focused": ["focus", "important", "urgent", "serious", "need to work", "we need to fix this"],
        "proud": ["proud", "great job", "excellent", "accomplished", "well done", "nice work"],
        "worried": ["worried", "nervous", "afraid", "stress", "anxious", "unsafe", "i'm scared"],
        "surprised": ["surprised", "wow", "oh wow", "unexpected", "impossible", "that shocked me"],
        "shy": ["shy", "embarrassed", "awkward", "timid", "i'm blushing"],
        "embarrassed": ["embarrassed", "ashamed", "awkward", "i feel silly"],
        "loving": ["aww", "i'm really glad you're here", "i care about you", "sweet", "you're important to me"],
    }

    scored: list[tuple[str, int]] = []
    for emotion, keywords in hints.items():
        score = sum(3 for keyword in keywords if keyword in lower)
        if score:
            scored.append((emotion, score))

    if not scored:
        return build_emotion_event({"primary": "neutral", "secondary": None, "intensity": 0.0, "source": "user" if source == "user" else "heuristic"})

    scored.sort(key=lambda item: item[1], reverse=True)
    primary = scored[0][0]
    secondary = scored[1][0] if len(scored) > 1 and scored[1][1] >= 2 else None
    intensity = min(1.0, max(0.0, 0.35 + (scored[0][1] * 0.08)))

    return build_emotion_event({
        "primary": primary,
        "secondary": secondary,
        "intensity": round(intensity, 2),
        "source": "user" if source == "user" else "heuristic",
        "priority": 5,
    })


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
                if EMOTION_DEBUG:
                    logger.info("[Emotion] assistant response received; resolving response text heuristically")
                emotion_event = infer_emotion_from_text(text)
            elif role == "user" and any(
                cue in text.lower()
                for cue in ("act cute", "be cute", "act playful", "be playful", "act jealous", "be jealous", "act angry", "be angry", "roleplay")
            ):
                if EMOTION_DEBUG:
                    logger.info("[Emotion] explicit user roleplay intent received")
                emotion_event = infer_emotion_from_text(text, source="user")
            else:
                emotion_event = None

            if emotion_event:
                emit_runtime_event(
                    emotion_event["type"],
                    **{key: value for key, value in emotion_event.items() if key != "type"},
                )
                asyncio.create_task(publish_emotion_event(ctx.room, emotion_event))

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