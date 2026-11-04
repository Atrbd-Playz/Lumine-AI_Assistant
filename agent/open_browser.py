"""Open or focus Lumine's browser without opening duplicate tabs.

**The launch lives in `session.py` now, and that is a fix rather than a tidy-up.**
This module already had the healthy shape -- start Chromium as an ordinary
detached process, then let Playwright attach over a debugging port -- while the
`browse` tool started its browser through `launch_persistent_context`, which
drives Chromium over `--remote-debugging-pipe`. Measured side by side on the same
profile, same flags, same engine: everything Playwright launched stalled YouTube
playback at ~40 s with the media request returning 401; everything started here
and merely attached to played past a minute. Two copies of "how to start the
browser" is how that difference stayed invisible, so there is one copy and this
module calls it. **And the port number itself carried the second half** (see
`session.browser_command`): launched with `--remote-debugging-port=0` this
healthy shape stalled again, and with an explicit -- picked, not fixed -- number
it played. The choosing lives in `session.free_debug_port`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

try:
    from .tools.browser import session
except ImportError:  # Running as `python agent/open_browser.py`
    from tools.browser import session


def _new_tab_url(engine: str) -> str:
    return "brave://newtab/" if engine == "brave" else "chrome://newtab/"


async def _focus_browser(engine: str) -> None:
    browser = session._Browser()
    try:
        page = await browser.ensure()
        if (getattr(page, "url", "") or "").strip().lower() == "about:blank":
            await page.goto(_new_tab_url(engine))
        await browser._reveal(page)
    finally:
        await browser.release()


async def _playwright_chromium_path() -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(session.MSG_NOT_INSTALLED) from None

    playwright = await async_playwright().start()
    try:
        return str(playwright.chromium.executable_path)
    finally:
        await playwright.stop()


def _browser_command(
    engine: str,
    executable: str,
    user_data_dir: Path,
    debug_port: int | None = None,
) -> list[str]:
    """The command that opens a *window*, on the browser's own new tab page."""
    return session.browser_command(
        engine,
        executable,
        user_data_dir,
        start_url=_new_tab_url(engine),
        debug_port=debug_port,
    )


#: Shared with the `browse` tool, so the two cannot drift. Kept as module-level
#: names because they are the seams their tests patch.
_launch_detached = session.launch_detached
_wait_for_browser = session.wait_for_devtools


def _active_port(user_data_dir: Path) -> int | None:
    port = session._discover_devtools_port(user_data_dir, attempts=1)
    return port or None


def open_browser() -> str:
    engine = session._configured_browser_engine()
    if session.headless_default():
        raise RuntimeError(
            "Lumine's browser is configured to run without a window. Turn off headless mode, then try again."
        )

    endpoint_file = session._endpoint_file(engine)
    endpoint = session._read_endpoint(endpoint_file)
    if endpoint and session._endpoint_alive(endpoint):
        asyncio.run(_focus_browser(engine))
        return engine
    if endpoint:
        session._forget_endpoint(endpoint_file)

    directory = session.profile_dir(engine)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise RuntimeError(
            "Lumine couldn't access its browser profile. Check folder permissions."
        ) from None

    # A browser already using this directory may have started before its endpoint
    # file was published. Join its live endpoint rather than launching a second
    # window or falling back to another profile.
    port = _active_port(directory)
    if port and session._endpoint_alive(f"http://127.0.0.1:{port}"):
        session._publish_endpoint(port, endpoint_file)
        asyncio.run(_focus_browser(engine))
        return engine

    # **One directory, hers, no spare.** With full isolation (2026-11-04) the
    # user's own Brave never shares a directory with Lumine, so there is no
    # contention to route around and no lock question to ask the user. The one
    # check left is Windows' own: a second `brave.exe` against a held directory
    # hands off and exits 0, which the wait below would report as a bare
    # timeout. `profile_in_use` asks first -- its attempted delete is also the
    # stale-lock cleanup a crash needs -- and a genuinely held directory can
    # only be a browser *of Lumine's* that stopped answering, so the refusal
    # describes her own window and never the user's.
    if session.profile_in_use(directory):
        raise RuntimeError(session.MSG_OWN_PROFILE_HELD)

    if engine == "brave":
        executable_path = session.brave_executable()
        if executable_path is None:
            raise RuntimeError(session.MSG_BRAVE_NOT_FOUND)
        executable = str(executable_path)
    else:
        executable = asyncio.run(_playwright_chromium_path())

    # One number, picked once: the command line carries it and the wait
    # polls it, because an explicit-port launch leaves no port file for the
    # older discovery route to read (see session.free_debug_port for the
    # measured reason the number has to be explicit).
    port = session.free_debug_port()
    command = _browser_command(engine, executable, directory, port)
    process = _launch_detached(command)
    try:
        port = _wait_for_browser(directory, process, port)
    except BaseException:
        # The same teardown the `browse` tool uses, so a launch that never
        # produced a window cannot leave a browser behind either.
        session.terminate_process(process)
        raise

    session._publish_endpoint(port, endpoint_file)
    return engine


if __name__ == "__main__":
    selected = open_browser()
    print(f"Opened Lumine's {selected} browser.")
