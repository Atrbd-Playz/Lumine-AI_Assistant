"""Ask the live WhatsApp page what it actually offers.

Usage::

    python agent/whatsapp_probe.py            # targets only
    python agent/whatsapp_probe.py --icons    # every data-icon the page renders
    python agent/whatsapp_probe.py --json     # machine-readable

For the send button and the message bubbles -- both of which need a chat open and
text in the composer -- use ``whatsapp_message_probe.py`` instead.

Why this exists
---------------
`tools/messaging/whatsapp_web.py` keeps several candidate selectors per target,
because WhatsApp renames things between releases and a path that knows only one
spelling is a path that works until a Tuesday. The lists were written from
knowledge of how WhatsApp has looked, and *knowledge is exactly the thing that
goes stale*. When a read starts refusing -- "WhatsApp's layout has changed and I
couldn't find the message menu" -- this says which spelling is live and which
are not, so the fix is made from evidence instead of from a guess that fails the
same way next month.

It is also how the remaining gaps get closed honestly. Every target listed here
that reports as missing is an operation that would refuse rather than act
wrongly, which is the safe failure, but a refusal nobody can fix is a feature
that does not work. This turns "something changed" into a specific line.

Requires the WhatsApp tab to be signed in **and finished loading**. The tab is
brought to the front as it opens, so if there is a QR code to scan it is the one
on screen. If it reports `loading`, WhatsApp had not finished starting -- that is
not a sign-in problem, and re-running it once the tab settles is the whole fix.
If it reports `logged_out`, scan the QR code in the window it opens and run it
again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

try:
    from dotenv import load_dotenv

    load_dotenv(AGENT_DIR / ".env")
except ImportError:  # pragma: no cover - dotenv is a hard dependency in practice
    pass

try:
    from tools.browser.session import profile_dir, release, sharing_endpoint
    from tools.messaging import whatsapp_web as web
except ImportError:  # running with `agent/` on sys.path rather than as a package
    sys.path.insert(0, str(AGENT_DIR))
    from tools.browser.session import profile_dir, release, sharing_endpoint
    from tools.messaging import whatsapp_web as web


def _already_running() -> bool:
    """Whether a Chromium is holding the profile *and cannot be joined*.

    **A running browser is no longer on its own a reason to refuse.** A Lumine
    browser now publishes a DevTools endpoint beside the profile, and a second
    process joins it rather than failing against its lock -- that is what makes the
    worker, the desktop app and several rooms able to use one window. Refusing here
    while that browser is up and joinable is exactly what made this probe
    single-use, and then unusable alongside anything else.

    What is still worth refusing is a locked profile with **no reachable endpoint**:
    somebody's own Chromium, or a crashed Lumine holding a lock nobody will release.
    No amount of connecting gets past that, and the fix is a different one.

    The lock is tested by trying to take it. Chromium takes an exclusive lock on
    the profile directory; Windows and Linux put it in a file inside that directory,
    macOS uses a symlink. An unreadable answer is treated as "held", because
    assuming otherwise reproduces Chromium's own paragraph -- the failure this
    replaces.
    """
    try:
        _, joinable = sharing_endpoint()
    except Exception:  # noqa: BLE001 - an unreadable endpoint is not a live one
        joinable = False
    if joinable:
        return False

    directory = profile_dir()
    if not directory.is_dir():
        return False
    for name in ("SingletonLock", "lockfile"):
        if _lock_held(directory / name):
            return True
    return False


def _lock_held(candidate: Path) -> bool:
    """Whether a Chromium lock file exists **and is held**.

    The distinction is the whole job, and getting it wrong in the permissive
    direction means this module believes it can launch while the profile is locked,
    and then Chromium answers with its paragraph about sessions.

    An earlier version asked by trying to ``os.open`` the file with ``O_EXCL``, and
    on Windows **that succeeded** -- ``O_EXCL`` means nothing without ``O_CREAT``
    there, so every lock looked unheld and the check never once fired. A test that
    only ever ran against an unlocked profile could not see it.

    So the question is put to the OS in the form each platform answers:

    - **Windows**: Chromium holds the file with an exclusive share mode, so
      deleting it fails with a sharing violation. Trying the delete *is* the test,
      and a stale lock -- which is most of them, after a crash -- simply succeeds
      and is cleaned up on the way past.
    - **POSIX**: an open file can be unlinked, so deletion always "works" and proves
      nothing. There the advisory lock file itself is the signal: if it is there,
      somebody created it and nobody has removed it.
    """
    if not candidate.exists():
        return False
    if os.name == "nt":
        try:
            os.remove(candidate)
        except OSError:
            return True
        return False
    try:
        handle = os.open(str(candidate), os.O_RDWR | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return True
    except OSError:
        # Present but not lockable by this user. Assume it is held.
        return True
    else:
        os.close(handle)
        return False


def render(report: dict, *, icons: bool = False) -> str:
    """The report as a person reads it.

    Hits and misses are separated rather than interleaved, because the question
    being asked is "what is broken", and a list that is mostly working targets
    buries the two that are not.
    """
    if report.get("error"):
        return f"Could not probe WhatsApp: {report['error']}"

    lines: list[str] = []
    lines.append(f"URL:    {report.get('url') or 'unknown'}")
    lines.append(f"State:  {report.get('ready')}")
    lines.append("")

    if report.get("note"):
        # A loading tab has no opinion yet, and reporting its opinion-less state
        # as a list of seventeen missing selectors is how a working
        # configuration gets "fixed" into a broken one.
        lines.append(report["note"])
        return "\n".join(lines)

    hit: dict = report.get("hit") or {}
    missing: list = report.get("missing") or []

    if report.get("chat"):
        lines.append(f"Chat:   {report['chat']} (opened: {report.get('opened')})")
        lines.append("")

    lines.append(f"Found ({len(hit)}):")
    for name in sorted(hit):
        lines.append(f"  ok  {name:<16} {hit[name]}")
    if not hit:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"Not found ({len(missing)}):")
    for name in sorted(missing):
        lines.append(f"  --  {name}")
    if not missing:
        lines.append("  (none)")

    # Reported apart from "not found" because they were not measured. A list that
    # mixes "WhatsApp changed this" with "this only exists inside a chat" sends
    # somebody to rewrite selectors that are already right.
    not_measured: list = report.get("not_measured") or []
    if not_measured:
        lines.append("")
        lines.append(f"Not measured ({len(not_measured)} -- need a chat open):")
        for name in sorted(not_measured):
            lines.append(f"  ??  {name}")

    home_only: list = report.get("home_only") or []
    if home_only:
        lines.append("")
        lines.append(
            "Present on the home screen but not inside a chat: "
            + ", ".join(sorted(home_only))
        )

    if missing:
        lines.append("")
        lines.append(
            "A missing target means the operations using it will refuse rather than "
            "act wrongly. Add the selector that is live today to TARGETS in "
            "whatsapp_web.py, keeping the old ones -- they come back."
        )

        # The two findings that look like breakage and are not. Said here rather
        # than left in the list, because a list of "not found" is read as a
        # verdict and these two are the reason somebody would go and rewrite
        # working selectors.
        explained: list[str] = []
        if "send_button" in missing:
            explained.append(
                "send_button -- WhatsApp only renders it once the composer has "
                "text in it, so an empty chat cannot show it. Run "
                "whatsapp_message_probe.py to see it."
            )
        if {"message_in", "message_out"} <= set(missing):
            explained.append(
                "message_in / message_out -- not stale, and not fixable by adding "
                "a spelling: this build gives message rows hashed class names "
                "(x1liijdw xu342n7 ...) and no data-id. The tools read direction "
                "from layout instead, via [data-testid='msg-container'], which is "
                "measured to be present. See _JS_ROWS_FN."
            )
        if explained:
            lines.append("")
            lines.append("Two of these are expected, and neither is a fault:")
            for note in explained:
                lines.append(f"  - {note}")

    if icons:
        lines.append("")
        lines.append("data-icons on the page:")
        for icon in report.get("icons") or []:
            lines.append(f"  {icon}")

    return "\n".join(lines)


async def probe(*, icons: bool = False) -> dict:
    """Read the page, then hand the browser back -- on the same event loop.

    The release is the whole reason this is not a bare ``return await
    web.probe()``. A diagnostic that leaves Chromium running holds the profile,
    so the next run is refused -- which made this tool single-use -- and the
    abandoned browser's pipe to a dead driver raises ``EPIPE`` on the way out,
    printing a Node stack trace over the report the user came to read.

    The loop matters as much as the release. It used to be driven as
    ``asyncio.run(probe())``, with the release awaited inside, which is correct --
    but an earlier version ran a *second* ``asyncio.run(release())`` afterwards,
    on a loop that was not the one Playwright's objects were made on. That is what
    produced ``RuntimeError: Route.continue_: Event loop is closed`` and
    ``Task was destroyed but it is pending!`` after a perfectly good report.
    """
    try:
        return await web.probe()
    finally:
        try:
            await release()
        except Exception:  # noqa: BLE001 - see the docstring
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--icons", action="store_true", help="list every data-icon the page renders")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    # Asked first, and reported in our own words. Starting Chromium into a locked
    # profile fails with a paragraph about sessions that reads like a product
    # fault, when the entire fix is to close a window.
    if _already_running():
        report = {
            "error": (
                "A Lumine browser window is already open and holding the profile "
                f"at {profile_dir()}. This probe needs that profile to itself -- "
                "close the window (or quit the Lumine app if it is the one holding "
                "it) and run this again."
            )
        }
    else:
        report = asyncio.run(probe(icons=args.icons))

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report, icons=args.icons))

    # A signed-out page is a setup problem and a loading one is a timing problem;
    # neither is a broken tool, so neither exits non-zero. Otherwise a CI check
    # would report a failure when all that is wrong is that nobody has scanned the
    # QR code yet, or that the tab needed another moment.
    ready = report.get("ready")
    if ready == "logged_out":
        print(
            "\nNot signed in. Scan the QR code in the WhatsApp window this opened, "
            "then run it again.",
            file=sys.stderr,
        )
    elif ready == "loading":
        print(
            "\nWhatsApp had not finished loading. Wait for the tab to settle and "
            "run it again -- nothing above is a real finding yet.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())