import argparse
import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from livekit import api


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("room")
    parser.add_argument("identity")
    args = parser.parse_args()

    load_dotenv(Path(__file__).with_name(".env"))
    token = (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(args.identity)
        .with_name(args.identity)
        .with_ttl(timedelta(minutes=10))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=args.room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
    )
    print(token.to_jwt())


if __name__ == "__main__":
    main()