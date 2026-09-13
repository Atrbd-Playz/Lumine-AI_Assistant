import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit import api


async def dispatch(room: str, agent_name: str) -> str:
    load_dotenv(Path(__file__).with_name(".env"))
    client = api.LiveKitAPI(
        os.environ["LIVEKIT_URL"],
        os.environ["LIVEKIT_API_KEY"],
        os.environ["LIVEKIT_API_SECRET"],
    )
    try:
        result = await client.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name=agent_name, room=room)
        )
        return result.id
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("room")
    parser.add_argument("agent_name")
    args = parser.parse_args()
    print(asyncio.run(dispatch(args.room, args.agent_name)))


if __name__ == "__main__":
    main()
