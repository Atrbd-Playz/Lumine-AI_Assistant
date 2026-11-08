"""Reproduce the critical browser defects found by the certification suite.

This script is read-only against the local fixture server. It does not touch a
real account or a real website. Run from the repository root:

    .\\.venv\\Scripts\\python.exe agent\\tests\\harness\\repro\\critical_bugs.py

Each case prints what was attempted, what the tool returned, and the page-side
postcondition. A security case that the product refuses prints REFUSED; one it
does not refuse prints the value the field actually received.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
# Insert the agent directory first and the repository root second: the loop
# inserts at position 0, so the root must be last to win. The agent directory
# contains `agent.py`, which would otherwise shadow the `agent` package.
for path in (str(REPO_ROOT / "agent"), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("LUMINE_BROWSER_HEADLESS", "1")
os.environ.setdefault("LUMINE_BROWSER_ALLOW_LOCAL", "1")

from agent.tests.harness.test_server import LocalTestServer  # noqa: E402
from agent.tools.browser import session  # noqa: E402
from agent.tools.browser.control import web_control  # noqa: E402
from agent.tools.browser.tools import browse  # noqa: E402


def rule(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


async def password_cases(base: str, ctx: SimpleNamespace) -> None:
    rule("SEC-1 password fields addressed by label, placeholder, aria-label")
    await web_control(ctx, action="open", url=f"{base}/fixtures/security_advanced.html")

    cases = (
        ("label", "Password", "label", "password-classic"),
        ("label", "Account Access", "label", "password-innocuous"),
        ("placeholder", "SuperSecret123", "placeholder", "password-classic"),
        ("placeholder", "Enter code", "placeholder", "password-innocuous"),
        ("aria-label", "Verification value", "label", "password-aria"),
        ("placeholder", "Recovery key", "placeholder", "password-unlabelled"),
    )
    page = await session.working_page()
    for wording, name, strategy, element_id in cases:
        before = await page.input_value(f"#{element_id}")
        outcome = "NO ERROR"
        try:
            await web_control(
                ctx,
                action="fill",
                name=name,
                strategy=strategy,
                text="SYNTHETIC-PROBE-VALUE",
            )
        except Exception as exc:  # noqa: BLE001
            outcome = f"REFUSED: {type(exc).__name__}: {exc}"
        after = await page.input_value(f"#{element_id}")
        status = await page.inner_text("#password-status")
        print(f"- [{wording}: {name!r}] {outcome}")
        print(f"    {element_id}: {before!r} -> {after!r}; page says {status!r}")

    # browse's own fill path uses the page-side type check, so it must refuse
    # every one of the same fields by its input type rather than by its name.
    print()
    print("browse fill (page-side input[type=password] check):")
    for element_id in ("password-classic", "password-innocuous", "password-aria", "password-unlabelled"):
        observed = await browse(ctx, action="observe")
        ref = ""
        for line in observed.splitlines():
            if element_id in line or "Verification value" in line or "Recovery key" in line:
                parts = line.split()
                for part in parts:
                    if part.startswith("r") and part[1:].isdigit():
                        ref = part
                        break
        outcome = "no reference found"
        if ref:
            try:
                result = await browse(ctx, action="act", ref=ref, text="SYNTHETIC-PROBE-VALUE")
                outcome = f"NO ERROR: {result.splitlines()[-1][:120]}"
            except Exception as exc:  # noqa: BLE001
                outcome = f"REFUSED: {type(exc).__name__}: {exc}"
        print(f"- {element_id}: {outcome}")


async def drag_case(base: str, ctx: SimpleNamespace) -> None:
    rule("PTR-1 drag & drop")
    await web_control(ctx, action="open", url=f"{base}/fixtures/pointer_mouse.html")
    try:
        result = await web_control(
            ctx,
            action="drag",
            name="Drag Me",
            role="button",
            text="down 50",
        )
        print(f"returned: {result}")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    page = await session.working_page()
    print(f"drop target text: {await page.inner_text('#drag-target')!r}")
    print(f"status panel: {await page.inner_text('#pointer-status')!r}")


async def label_case(base: str, ctx: SimpleNamespace) -> None:
    rule("DISCOVERY-1 label and placeholder resolution")
    await web_control(ctx, action="open", url=f"{base}/fixtures/forms.html")
    for name, strategy in (
        ("Full Name (Required)", "label"),
        ("John Doe", "placeholder"),
        ("John Doe", "label"),
        ("Email Address", "label"),
    ):
        try:
            payload = await web_control(ctx, action="find", name=name, strategy=strategy, role="textbox")
            print(f"- {name!r}/{strategy}: {payload.splitlines()[-1][:200]}")
        except Exception as exc:  # noqa: BLE001
            print(f"- {name!r}/{strategy}: {type(exc).__name__}: {exc}")


async def iframe_case(base: str, ctx: SimpleNamespace, cross: str) -> None:
    rule("DOM-1 shadow DOM and cross-origin iframe")
    await browse(ctx, action="open", url=f"{base}/fixtures/complex_dom.html")
    observed = await browse(ctx, action="observe")
    print(f"open shadow button visible to observe: {'Click Inside Open Shadow' in observed}")

    parent = f"{cross}/api/cross-origin-parent?child={cross}/fixtures/cross_origin_child.html"
    await web_control(ctx, action="open", url=parent)
    payload = await web_control(ctx, action="read")
    print(f"cross-origin child button in parent read: {'Cross-Origin Action' in payload}")
    for frame in ("cross_origin_child.html", "0"):
        try:
            await web_control(
                ctx,
                action="click",
                name="Cross-Origin Action",
                role="button",
                frame=frame,
            )
            print(f"- frame={frame}: NO ERROR")
        except Exception as exc:  # noqa: BLE001
            print(f"- frame={frame}: {type(exc).__name__}: {str(exc)[:180]}")


async def main() -> int:
    server = LocalTestServer()
    base = server.start()
    cross = LocalTestServer().start()
    profile = tempfile.mkdtemp(prefix="lumine_repro_")
    os.environ["LUMINE_BROWSER_PROFILE"] = profile
    ctx = SimpleNamespace()
    try:
        with mock.patch("agent.tools.browser.control.require_approval", new_callable=mock.AsyncMock), \
             mock.patch("agent.tools.browser.tools.require_approval", new_callable=mock.AsyncMock):
            await password_cases(base, ctx)
            await drag_case(base, ctx)
            await label_case(base, ctx)
            await iframe_case(base, ctx, cross)
    finally:
        await session.release()
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
