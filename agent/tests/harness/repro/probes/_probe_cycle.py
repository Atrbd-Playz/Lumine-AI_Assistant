"""Mimic the harness lifecycle: launch, open, close every tab, reopen, navigate."""
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


async def cycle(ctx, base, label, url):
    before = await browser_session.tab_list()
    known = {row["tab"] for row in before.get("tabs") or []}
    work = None
    if before.get("tabs"):
        opened = await browser_session.new_tab("")
        work = opened.get("tab")
    print(f"[{label}] before={before} work={work}", flush=True)
    started = time.perf_counter()
    try:
        await browse(ctx, action="open", url=url)
        print(f"[{label}] open OK {(time.perf_counter()-started)*1000:.0f}ms", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] open FAIL {(time.perf_counter()-started)*1000:.0f}ms -> {exc}", flush=True)
    # exact harness teardown
    for row in (await browser_session.tab_list()).get("tabs") or []:
        if row["tab"] not in known:
            print(f"[{label}] closing tab {row['tab']}", flush=True)
            await browser_session.close_tab(row["tab"])
    print(f"[{label}] after teardown={await browser_session.tab_list()}", flush=True)


async def main() -> None:
    server = LocalTestServer()
    base = server.start()
    ctx = SimpleNamespace()
    with mock.patch("agent.tools.browser.control.require_approval", new_callable=mock.AsyncMock), \
         mock.patch("agent.tools.browser.tools.require_approval", new_callable=mock.AsyncMock):
        await browse(ctx, action="open", url=f"{base}/fixtures/static_elements.html")
        print("warm", await browser_session.tab_list(), flush=True)
        # now the real harness cycle: tabs_before will be non-empty (the warm page)
        # and on the very first test it was empty, so exercise both orders.
        await cycle(ctx, base, "cycle-1", f"{base}/api/redirect-chain?count=3")
        await cycle(ctx, base, "cycle-2", f"{base}/fixtures/forms.html")
        await cycle(ctx, base, "cycle-3", f"{base}/api/redirect-loop")
        await browser_session.release()
    server.stop()


asyncio.run(main())
