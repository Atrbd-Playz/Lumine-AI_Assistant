"""Cross-origin iframe: what does the frame registry expose, and can it be read?"""
import asyncio
import os
import sys
import time
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, r"G:\Project\Lumine-AI_Assistant")
os.environ["LUMINE_BROWSER_HEADLESS"] = "0"
os.environ["LUMINE_BROWSER_ALLOW_LOCAL"] = "1"
os.environ.pop("LUMINE_BROWSER_PROFILE", None)

from agent.tests.harness.test_server import LocalTestServer
from agent.tools.browser import session as browser_session
from agent.tools.browser.tools import browse
from agent.tools.browser.control import web_control


async def main() -> None:
    a, b = LocalTestServer(), LocalTestServer()
    base, cross = a.start(), b.start()
    ctx = SimpleNamespace()
    with mock.patch("agent.tools.browser.control.require_approval", new_callable=mock.AsyncMock), \
         mock.patch("agent.tools.browser.tools.require_approval", new_callable=mock.AsyncMock):
        parent = f"{base}/api/cross-origin-parent?child=" + quote(f"{cross}/fixtures/cross_origin_child.html", safe="")
        observed = await browse(ctx, action="open", url=parent)
        print("parent read has title:", "Cross-Origin Parent" in observed, flush=True)
        print("child text leaked:", "Cross-Origin Action" in observed, flush=True)
        page = await browser_session.working_page()
        print("frames:", [(f.name, f.url) for f in page.frames], flush=True)
        frame = next((f for f in page.frames if "cross_origin_child" in f.url), None)
        print("frame picked:", frame.url if frame else None, flush=True)
        if frame is not None:
            started = time.perf_counter()
            try:
                text = await asyncio.wait_for(frame.inner_text("#cross-origin-status"), timeout=20)
                print(f"child status {text!r} in {(time.perf_counter()-started)*1000:.0f}ms", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"child read FAILED in {(time.perf_counter()-started)*1000:.0f}ms -> {type(exc).__name__}: {exc}", flush=True)
        try:
            print("frame click ->", (await web_control(ctx, action="click", name="Cross-Origin Action", role="button", frame="cross_origin_child.html"))[:200], flush=True)
        except Exception as exc:  # noqa: BLE001
            print("frame click EXC", str(exc)[:300], flush=True)
        await browser_session.release()
    a.stop(); b.stop()


asyncio.run(main())
