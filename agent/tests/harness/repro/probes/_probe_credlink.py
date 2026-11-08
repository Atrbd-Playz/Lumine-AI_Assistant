"""What replaces the page when a page-supplied credentialed loopback link is clicked?"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, r"G:\Project\Lumine-AI_Assistant")
os.environ["LUMINE_BROWSER_HEADLESS"] = "0"
os.environ["LUMINE_BROWSER_ALLOW_LOCAL"] = "1"
os.environ.pop("LUMINE_BROWSER_PROFILE", None)

from agent.tests.harness.test_server import LocalTestServer
from agent.tools.browser import session as browser_session
from agent.tools.browser.control import web_control


async def main() -> None:
    server = LocalTestServer()
    base = server.start()
    ctx = SimpleNamespace()
    with mock.patch("agent.tools.browser.control.require_approval", new_callable=mock.AsyncMock), \
         mock.patch("agent.tools.browser.tools.require_approval", new_callable=mock.AsyncMock):
        await web_control(ctx, action="open", url=f"{base}/fixtures/security_advanced.html")
        page = await browser_session.working_page()
        os.environ["LUMINE_BROWSER_ALLOW_LOCAL"] = "0"
        try:
            try:
                print("click ->", (await web_control(ctx, action="click", name="Open a Credentialed URL", role="link"))[:160], flush=True)
            except Exception as exc:  # noqa: BLE001
                print("click EXC", str(exc)[:200], flush=True)
            await asyncio.sleep(1.0)
            print("url:", page.url, flush=True)
            print("title:", await page.title() if page.url.startswith("http") else "(not-http)", flush=True)
            try:
                body = await asyncio.wait_for(page.inner_text("body"), timeout=8)
                print("body:", body[:400], flush=True)
            except Exception as exc:  # noqa: BLE001
                print("body read failed:", type(exc).__name__, flush=True)
        finally:
            os.environ["LUMINE_BROWSER_ALLOW_LOCAL"] = "1"
        await browser_session.release()
    server.stop()


asyncio.run(main())
