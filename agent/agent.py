import logging
from dotenv import load_dotenv

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
)

from livekit.plugins import (
    groq,
    silero,
    cartesia,
)

from pathlib import Path

PERSONA = Path("prompts/persona.md").read_text(encoding="utf-8")

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lumine")


class Lumine(Agent):
    def __init__(self):
        super().__init__(
            instructions=PERSONA
        )


async def entrypoint(ctx: JobContext):
    logger.info(f"Connecting to room: {ctx.room.name}")

    await ctx.connect()

    session = AgentSession(
    vad=silero.VAD.load(
        min_speech_duration=0.4,
    ),

    stt=groq.STT(),

    llm=groq.LLM(
        model="llama-3.3-70b-versatile",
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


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
        )
    )