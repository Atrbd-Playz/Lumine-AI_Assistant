"""Isolate the redirect-chain navigation outside unittest."""
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
    print("base", base, flush=True)
    ctx = SimpleNamespace()
    with mock.patch("agent.tools.browser.control.require_approval", new_callable=mock.AsyncMock), \
         mock.patch("agent.tools.browser.tools.require_approval", new_callable=mock.AsyncMock):
        before = await browser_session.tab_list()
        print("tabs before", before, flush=True)
        opened = await browser_session.new_tab("")
        print("work tab", opened, flush=True)
        for url in (
            f"{base}/fixtures/static_elements.html",
            f"{base}/api/redirect-chain?count=3",
            f"{base}/fixtures/forms.html",
        ):
            started = time.perf_counter()
            try:
                text = await browse(ctx, action="open", url=url)
                print(f"OK {url} in {(time.perf_counter()-started)*1000:.0f}ms -> {text[:120]!r}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {url} in {(time.perf_counter()-started)*1000:.0f}ms -> {exc}", flush=True)
        print("tabs after", await browser_session.tab_list(), flush=True)
        await browser_session.release()
    server.stop()


asyncio.run(main())
