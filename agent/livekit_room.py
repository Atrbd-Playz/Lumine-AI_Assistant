import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit import api


async def delete_room(room: str) -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    client = api.LiveKitAPI(
        os.environ["LIVEKIT_URL"],
        os.environ["LIVEKIT_API_KEY"],
        os.environ["LIVEKIT_API_SECRET"],
    )
    try:
        await client.room.delete_room(api.DeleteRoomRequest(room=room))
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("room")
    args = parser.parse_args()
    asyncio.run(delete_room(args.room))


if __name__ == "__main__":
    main()
