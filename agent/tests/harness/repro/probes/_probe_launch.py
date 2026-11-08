"""Launch path: fresh Brave, first navigation, last-tab teardown, second navigation."""
import asyncio
import logging
import os
import sys
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, r"G:\Project\Lumine-AI_Assistant")
os.environ["LUMINE_BROWSER_HEADLESS"] = "0"
os.environ["LUMINE_BROWSER_ALLOW_LOCAL"] = "1"
os.environ.pop("LUMINE_BROWSER_PROFILE", None)
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

from agent.tests.harness.test_server import LocalTestServer
from agent.tools.browser import session as browser_session
from agent.tools.browser.tools import browse


async def main() -> None:
    server = LocalTestServer()
    base = server.start()
    ctx = SimpleNamespace()
    with mock.patch("agent.tools.browser.control.require_approval", new_callable=mock.AsyncMock), \
         mock.patch("agent.tools.browser.tools.require_approval", new_callable=mock.AsyncMock):
        started = time.perf_counter()
        out = await browse(ctx, action="open", url=f"{base}/fixtures/static_elements.html")
        print(f"first open OK {(time.perf_counter()-started)*1000:.0f}ms", flush=True)
        print("tabs", await browser_session.tab_list(), flush=True)
        print("shared?", getattr(browser_session._browser, "_shared", "?"),
              "process?", getattr(getattr(browser_session._browser, "_process", None), "pid", None), flush=True)
        for row in (await browser_session.tab_list()).get("tabs") or []:
            listing = await browser_session.close_tab(row["tab"])
            print("closed", row["tab"], "->", listing, flush=True)
        print("after teardown", await browser_session.tab_list(), flush=True)
        opened = await browser_session.new_tab("")
        print("work tab", opened, flush=True)
        started = time.perf_counter()
        try:
            await browse(ctx, action="open", url=f"{base}/api/redirect-chain?count=3")
            print(f"redirect OK {(time.perf_counter()-started)*1000:.0f}ms", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"redirect FAIL {(time.perf_counter()-started)*1000:.0f}ms -> {exc}", flush=True)
        print("tabs final", await browser_session.tab_list(), flush=True)
        await browser_session.release()
    server.stop()


asyncio.run(main())
