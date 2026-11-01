"""Open or focus Lumine's browser without opening duplicate tabs."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

try:
    from .tools.browser import session
except ImportError:  # Running as `python agent/open_browser.py`
    from tools.browser import session


START_TIMEOUT_SECONDS = 15.0
POLL_SECONDS = 0.1


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
    profile_directory: str | None = None,
) -> list[str]:
    command = [
        executable,
        f"--user-data-dir={user_data_dir}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if profile_directory:
        command.append(f"--profile-directory={profile_directory}")
    command.append(_new_tab_url(engine))
    return command


def _launch_detached(command: list[str]) -> subprocess.Popen[bytes]:
    options: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        options["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)


def _active_port(user_data_dir: Path) -> int | None:
    marker = user_data_dir / "DevToolsActivePort"
    try:
        port = marker.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return int(port) if port.isdigit() else None


def _wait_for_browser(user_data_dir: Path, process: subprocess.Popen[bytes]) -> int:
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        port = _active_port(user_data_dir)
        if port and session._endpoint_alive(f"http://127.0.0.1:{port}"):
            return port
        if process.poll() is not None:
            raise RuntimeError(
                "The browser closed before its local control endpoint was ready. Nothing else was opened."
            )
        time.sleep(POLL_SECONDS)
    raise RuntimeError(
        "The browser did not publish its local control endpoint in time. Nothing else was opened."
    )


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

    profile_directory: str | None = None
    if engine == "brave":
        try:
            user_data_dir = session.brave_user_data_dir()
            profile_directory = session.brave_profile_directory(user_data_dir).name
        except ValueError as exc:
            raise RuntimeError(
                f"{session.MSG_BRAVE_PROFILE_NOT_FOUND} {exc}"
            ) from None
    else:
        user_data_dir = session.profile_dir()
        try:
            user_data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise RuntimeError(
                "Lumine couldn't access its browser profile. Check folder permissions."
            ) from None

    # A browser already using this profile may have started before its endpoint
    # file was published. Join its live endpoint rather than launching a second
    # window or falling back to another profile.
    port = _active_port(user_data_dir)
    if port and session._endpoint_alive(f"http://127.0.0.1:{port}"):
        session._publish_endpoint(port, endpoint_file)
        asyncio.run(_focus_browser(engine))
        return engine

    # **A locked profile opens a window beside it, not a refusal.** Brave may be
    # running on another profile (or on this one), and on Windows a second
    # `brave.exe` launched against the same user-data directory hands off and
    # exits 0 -- so the wait that follows would time out with no window and no
    # reason. `profile_in_use` asks first, and the spare is the same fallback the
    # worker uses. Its cost is named in `session.spare_profile_dir`: it is not
    # signed in.
    #
    # The held directory is watched for a moment before that fallback, for the
    # same reason the worker does it: a browser that is *closing* holds the lock
    # exactly as one that is open does, and sending the next window to the
    # unsigned-in spare for a lock that clears in 200 ms is the worse mistake.
    if session.profile_in_use(user_data_dir) and not session.settle_profile_lock(
        user_data_dir
    ):
        user_data_dir = session.spare_profile_dir(engine)
        profile_directory = None
        try:
            user_data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise RuntimeError(
                "Lumine couldn't access its spare browser profile. Check folder permissions."
            ) from None
        if session.profile_in_use(user_data_dir) and not session.settle_profile_lock(
            user_data_dir
        ):
            raise RuntimeError(session.MSG_PROFILE_LOCKED)

    if engine == "brave":
        executable_path = session.brave_executable()
        if executable_path is None:
            raise RuntimeError(session.MSG_BRAVE_NOT_FOUND)
        executable = str(executable_path)
    else:
        executable = asyncio.run(_playwright_chromium_path())

    command = _browser_command(engine, executable, user_data_dir, profile_directory)
    process = _launch_detached(command)
    try:
        port = _wait_for_browser(user_data_dir, process)
    except Exception:
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            except OSError:
                pass
        raise

    session._publish_endpoint(port, endpoint_file)
    return engine


if __name__ == "__main__":
    selected = open_browser()
    print(f"Opened Lumine's {selected} browser.")
