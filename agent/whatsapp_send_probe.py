"""Send one real message through the Web path and report every stage.

Usage::

    python agent/whatsapp_send_probe.py "Me2"
    python agent/whatsapp_send_probe.py "Me2" "hello from the test"
    python agent/whatsapp_send_probe.py --read-only "Me2"

Why this is a CLI at ``agent/`` root rather than a snippet to paste
-------------------------------------------------------------------
It has to run the real functions, in order, from a cold process -- the same code
path ``send_whatsapp`` takes, not a reimplementation of it. Written as something
to ``exec``, it broke on the first line that read ``sys.argv``, which is not a
useful thing to hand somebody trying to verify a bug fix.

``--read-only`` types the draft and then **clears it**, never sending. That is the
mode to use first: it proves the composer works without putting a message in
somebody's phone, and it is safe against any contact.

Stages, because the whole point is that a failure names its stage:

1. ``resolve`` -- search WhatsApp's own search box for the name
2. ``open``    -- open the chat, and confirm the *header* agrees
3. ``draft``   -- focus the composer and type the text
4. ``verify``  -- read the composer back and compare it to what was asked for
5. ``send``    -- press Send (skipped by ``--read-only``)
6. ``confirm`` -- wait for the outgoing bubble to appear

Every stage prints how long it took, because a send that works in eight seconds
and one that works in forty-five are different problems, and a green tick does
not say which you have.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

try:
    from dotenv import load_dotenv

    load_dotenv(AGENT_DIR / ".env")
except ImportError:  # pragma: no cover - dotenv is a hard dependency in practice
    pass

from tools.browser.session import release  # noqa: E402
from tools.messaging import whatsapp_web as web  # noqa: E402

DEFAULT_TEXT = "Lumine send test - delete this message."


class Ambiguous(Exception):
    """More than one chat matched, and none of them exactly."""


class Reporter:
    """Prints each stage, its outcome, and how long it took."""

    def __init__(self) -> None:
        self.failed = False

    async def stage(self, label: str, coro) -> tuple[Any, bool]:
        started = time.monotonic()
        try:
            value = await coro
        except Exception as exc:  # noqa: BLE001 - a CLI reports, it does not traceback
            print(f"  {label:<8} REFUSED  ({self._since(started)})")
            print(f"           {exc}")
            self.failed = True
            return None, False
        print(f"  {label:<8} ok        ({self._since(started)})  {_short(value)}")
        return value, True

    @staticmethod
    def _since(started: float) -> str:
        return f"{(time.monotonic() - started) * 1000:.0f} ms"


def _short(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (list, tuple)):
        return f"{len(value)} item(s)"
    if isinstance(value, dict):
        return ", ".join(f"{k}={_short(v)}" for k, v in list(value.items())[:4])
    text = str(value)
    return text if len(text) <= 90 else text[:87] + "..."


def _fold(text: str) -> str:
    """Comparable form of a chat name: collapsed whitespace, casefolded."""
    return " ".join(str(text or "").split()).casefold()


async def resolve(name: str) -> str:
    """One chat title for a spoken name, preferring an exact match.

    Uses WhatsApp's own search box, for the reason the send path does: the rendered
    list is the last few dozen conversations and nothing more.
    """
    rows = await web.search_chats(name)
    titles = [t for t in (str(row.get("title") or "").strip() for row in rows) if t]
    if not titles:
        raise Ambiguous(f"No chat matched {name!r}.")
    exact = [t for t in titles if _fold(t) == _fold(name)]
    if len(exact) == 1:
        return exact[0]
    if len(titles) == 1:
        return titles[0]
    raise Ambiguous(
        f"{name!r} matched several chats and none exactly:\n"
        + "\n".join(f"    - {t}" for t in titles[:8])
    )


async def run(args: argparse.Namespace) -> int:
    report = Reporter()

    print(f"chat    : {args.chat!r}")
    print(f"message : {args.text!r}")
    print(f"mode    : {'type and clear -- nothing is sent' if args.read_only else 'SEND for real'}")

    print("\n1. resolve")
    try:
        title = await resolve(args.chat)
    except Ambiguous as exc:
        print(f"  {exc}")
        print("  Nothing was opened or typed.")
        return 1
    print(f"  -> chose {title!r}")

    print("\n2. open")
    opened, ok = await report.stage("open", web.open_chat(title))
    if not ok or not opened:
        print("  -> the chat did not open. Nothing was typed.")
        return 1
    print(f"  -> header reports {await web.active_chat()!r}")

    print("\n3. draft")
    before = await web.composer()
    print(
        f"  composer: found={before.get('found')} empty={before.get('empty')} "
        f"(raw length {before.get('rawLength')})"
    )
    if not before.get("empty", True):
        print("  a draft was already there; clearing it first")
        print(f"  cleared: {await web._clear_composer()}")
    typed, ok = await report.stage("type", web.type_message(args.text))
    if not ok:
        return 1

    print("\n4. verify the draft is really in the box")
    draft = await web.composer()
    holds = web._comparable(args.text) == web._comparable(str(draft.get("text") or ""))
    print(f"  holds exactly what was asked : {holds}")
    print(f"  send button live             : {draft.get('hasSend')}")
    if not holds:
        print("  -> draft does not match; clearing it and NOT sending")
        await web._clear_composer()
        return 1
    if not draft.get("hasSend"):
        print(
            "  note: no Send button found, so this presses Enter instead. That is\n"
            "        WhatsApp's own send gesture and equally reliable -- but it\n"
            "        means the control we could have verified is not the one used."
        )

    if args.read_only:
        print("\n5. clear")
        cleared = await web._clear_composer()
        print(f"  cleared: {cleared}")
        print(f"  composer now: {json.dumps(await web.composer())}")
        print("\nRESULT: the composer types, verifies and clears. Nothing was sent.")
        return 0 if cleared else 1

    print("\n5. send")
    _, ok = await report.stage("press", web.press_send())
    if not ok:
        return 1

    print("\n6. confirm it left")
    seen = False
    started = time.monotonic()
    for attempt in range(10):
        if await web.delivery_seen(args.text, timeout=1.0):
            seen = True
            print(f"  DELIVERY CONFIRMED on attempt {attempt + 1}")
            break
        await asyncio.sleep(0.6)
    print(f"  confirm took {(time.monotonic() - started):.1f}s")

    if seen:
        print("\nRESULT: SENT AND CONFIRMED")
        return 0
    print(
        "\nRESULT: SENT BUT NOT CONFIRMED\n"
        "  Send was pressed and no error came back, but the outgoing message never\n"
        "  appeared. The real tool reports this as *uncertain* and holds the retry\n"
        "  for five minutes -- check WhatsApp before asking again."
    )
    return 2


async def run_and_release(args: argparse.Namespace) -> int:
    """The whole job on **one** event loop.

    This used to be ``asyncio.run(run(args))`` and then, in a ``finally``,
    ``asyncio.run(release())`` -- two loops in one process. Playwright's objects
    belong to the loop that created them, so the second call was reaching a dead
    one. The visible cost was the noise after every run::

        RuntimeError: Route.continue_: Event loop is closed
        Task was destroyed but it is pending!
        Error: EPIPE: broken pipe, write

    which is Playwright's driver losing its pipe while the page's route handler is
    still mid-flight. It arrives *after* the answer, so it reads like the tool
    failing when it had already succeeded.

    Releasing before the loop closes is what the route handler needs, and it is the
    only order in which "hand the browser back" actually means that.
    """
    try:
        return await run(args)
    finally:
        try:
            await release()
        except Exception:  # noqa: BLE001 - nothing left to do at this point
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "Several agents may share Lumine's browser at once, so this joins a "
            "running one if it finds it."
        ),
    )
    parser.add_argument("chat", help="the contact's name, as WhatsApp shows it")
    parser.add_argument(
        "text", nargs="?", default=DEFAULT_TEXT, help=f"the message (default: {DEFAULT_TEXT!r})"
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="type the draft and clear it; never press Send",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_and_release(args))
    except KeyboardInterrupt:  # pragma: no cover
        # Only worth a warning when there could be a draft to look at. Saying it
        # after a confirmed send is a false alarm about work that is already done,
        # and this tool exists to be trusted about exactly that.
        print(
            "\nInterrupted. If anything was typed, check WhatsApp for a draft "
            "before retrying."
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())