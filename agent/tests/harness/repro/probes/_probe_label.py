"""Why does a label resolve to more than one textbox?"""
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
        await web_control(ctx, action="open", url=f"{base}/fixtures/forms.html")
        payload = await browser_session.observe_page()
        for control in payload.get("controls") or []:
            print("control", {k: control.get(k) for k in ("ref", "role", "label", "name", "tag", "type")}, flush=True)
        for kwargs in (
            {"action": "find", "name": "Full Name (Required)", "role": "textbox"},
            {"action": "find", "name": "Full Name", "role": "textbox"},
            {"action": "find", "name": "Catalog Search", "role": "textbox"},
        ):
            try:
                print(kwargs, "->", (await web_control(ctx, **kwargs))[:900], flush=True)
            except Exception as exc:  # noqa: BLE001
                print(kwargs, "-> EXC", str(exc)[:900], flush=True)
        for kwargs in (
            {"action": "fill", "name": "Full Name (Required)", "role": "textbox", "text": "Probe"},
            {"action": "fill", "name": "Full Name", "role": "textbox", "text": "Probe"},
        ):
            try:
                print(kwargs, "->", (await web_control(ctx, **kwargs))[:400], flush=True)
            except Exception as exc:  # noqa: BLE001
                print(kwargs, "-> EXC", str(exc)[:900], flush=True)
        page = await browser_session.working_page()
        print("value", await page.input_value("#input-text"), flush=True)
        await browser_session.release()
    server.stop()


asyncio.run(main())
