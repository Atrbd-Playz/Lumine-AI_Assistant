import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit import api


async def dispatch(room: str, agent_name: str, metadata: str | None = None) -> str:
    load_dotenv(Path(__file__).with_name(".env"))
    client = api.LiveKitAPI(
        os.environ["LIVEKIT_URL"],
        os.environ["LIVEKIT_API_KEY"],
        os.environ["LIVEKIT_API_SECRET"],
    )
    try:
        request = api.CreateAgentDispatchRequest(agent_name=agent_name, room=room)
        if metadata:
            request.metadata = metadata
        result = await client.agent_dispatch.create_dispatch(request)
        return result.id
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("room")
    parser.add_argument("agent_name")
    parser.add_argument("metadata", nargs="?")
    args = parser.parse_args()
    print(asyncio.run(dispatch(args.room, args.agent_name, args.metadata)))


if __name__ == "__main__":
    main()
