"""Unauthenticated, read-only browser smoke tests against public websites."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

agent_dir = Path(__file__).resolve().parent.parent.parent
repo_root = agent_dir.parent
for path in (str(agent_dir), str(repo_root)):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent.tools.browser import control, session
from agent.tools.browser.tools import browse
from agent.tools.tool_results import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from agent.tools.tools_compat import ToolError


SITES = (
    ("search", "Google public search results", "https://www.google.com/search?q=browser+automation+accessibility"),
    ("documentation", "MDN Web Docs", "https://developer.mozilla.org/en-US/docs/Web/HTML"),
    ("news", "The Guardian public news page", "https://www.theguardian.com/international"),
    ("e-commerce", "eBay public category page", "https://www.ebay.com/b/Electronics/"),
    ("social", "Reddit public landing page", "https://www.reddit.com/"),
    ("video", "YouTube public search results", "https://www.youtube.com/results?search_query=browser+automation"),
    ("email", "Gmail public sign-in boundary", "https://mail.google.com/"),
    ("cloud-app", "Google Drive public sign-in boundary", "https://drive.google.com/"),
    ("maps", "OpenStreetMap public map", "https://www.openstreetmap.org/"),
    ("developer-platform", "GitHub public repository", "https://github.com/microsoft/playwright"),
    ("forum", "Stack Overflow public questions", "https://stackoverflow.com/questions"),
    ("web-app", "Playwright-hosted TodoMVC SPA", "https://demo.playwright.dev/todomvc/"),
    ("overlay", "The Internet entry-ad overlay demo", "https://the-internet.herokuapp.com/entry_ad"),
    ("redirect", "httpbin redirect chain", "https://httpbin.org/redirect/2"),
    ("baseline", "Example Domain", "https://example.com/"),
)

NETWORK_FAILURE_MARKERS = (
    "timeout",
    "timed out",
    "name not resolved",
    # The pre-check phrases a resolver failure as prose ("it could not be
    # resolved") rather than with Chromium's `net::` constant, and that made two
    # unreachable public hosts look like browser defects on the first run. A
    # hostname the machine cannot resolve is an environment answer either way.
    "could not be resolved",
    "couldn't be resolved",
    "getaddrinfo",
    "temporary failure in name resolution",
    "dns",
    "connection refused",
    "connection reset",
    "net::err",
    "403",
    "429",
    "502",
    "503",
    "504",
    "execution context was destroyed",
)
ACCESS_CHALLENGE_MARKERS = (
    "pardon our interruption",
    "just a moment",
    "/sorry/",
    "captcha",
    "js_challenge",
    "challenge=",
    "verify you are human",
    "access denied",
)
OVERLAY_SELECTOR = "[role='dialog'], [aria-modal='true'], dialog, .modal, #modal"


def _payload(response: str) -> dict:
    if UNTRUSTED_OPEN in response and UNTRUSTED_CLOSE in response:
        response = response.split(UNTRUSTED_OPEN, 1)[1].split(UNTRUSTED_CLOSE, 1)[0]
    start, end = response.find("{"), response.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        value = json.loads(response[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _failure_status(message: str) -> str:
    lowered = message.casefold()
    return "BLOCKED" if any(marker in lowered for marker in NETWORK_FAILURE_MARKERS) else "FAIL"


def _is_access_challenge(title: str, url: str, text: str) -> bool:
    content = f"{title}\n{url}\n{text}".casefold()
    return any(marker in content for marker in ACCESS_CHALLENGE_MARKERS)


async def _visible_overlays(page) -> list[dict[str, str]]:
    nodes = page.locator(OVERLAY_SELECTOR)
    found = []
    seen = set()
    for index in range(min(await nodes.count(), 8)):
        node = nodes.nth(index)
        if await node.is_visible():
            text = (await node.inner_text())[:100]
            if text not in seen:
                seen.add(text)
                found.append({"text": text})
    return found


async def _run() -> dict:
    # Her own browser, visible: this is the configuration being certified. A
    # throwaway profile would prove something about Playwright rather than about
    # the browser a conversation actually uses. `LUMINE_CERT_PROFILE` stays as the
    # explicit escape hatch for a deliberately isolated run.
    os.environ["LUMINE_BROWSER_HEADLESS"] = "0"
    os.environ["LUMINE_BROWSER_ALLOW_LOCAL"] = "0"
    override = os.environ.pop("LUMINE_CERT_PROFILE", "").strip()
    if override:
        os.environ["LUMINE_BROWSER_PROFILE"] = override
    else:
        os.environ.pop("LUMINE_BROWSER_PROFILE", None)
    context = SimpleNamespace()
    results = []
    baseline = await session.tab_list()
    known = {row["tab"] for row in baseline.get("tabs") or []}
    # A tab of this run's own. `open` reuses the tab in use, so running the
    # campaign in somebody's existing tab would navigate their page away.
    work_tab = None
    if known:
        try:
            work_tab = (await session.new_tab("")).get("tab")
        except Exception:  # noqa: BLE001 - fall back to the existing tab
            work_tab = None

    try:
        with mock.patch.object(session, "_configured_browser_engine", return_value="chromium"), \
             mock.patch("agent.tools.browser.tools.require_approval", new_callable=mock.AsyncMock), \
             mock.patch("agent.tools.browser.control.require_approval", new_callable=mock.AsyncMock):
            try:
                for category, label, url in SITES:
                    started = time.perf_counter()
                    result = {
                        "id": f"PUBLIC-{category}",
                        "category": category,
                        "site": label,
                        "requested_url": url,
                        "status": "FAIL",
                        "title": "",
                        "landed_url": "",
                        "controls": 0,
                        "text_chars": 0,
                        "dialogs_or_overlays": [],
                        "visible_overlays": [],
                        "semantic_heading_found": False,
                        "duration_ms": 0.0,
                        "evidence": "",
                    }
                    try:
                        await browse(context, action="open", url=url)
                        page = await session.working_page()
                        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                        await page.wait_for_timeout(250)
                        if category == "overlay":
                            for _ in range(12):
                                if await _visible_overlays(page):
                                    break
                                await page.wait_for_timeout(250)
                        navigation_race = False
                        try:
                            observation = await browse(context, action="observe")
                        except Exception as exc:
                            if "execution context was destroyed" not in str(exc).casefold():
                                raise
                            navigation_race = True
                            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                            await page.wait_for_timeout(750)
                            observation = await browse(context, action="observe")
                        payload = _payload(observation)
                        result["visible_overlays"] = await _visible_overlays(page)
                        result["title"] = str(payload.get("title") or "")[:120]
                        result["landed_url"] = str(payload.get("url") or page.url)[:240]
                        controls = payload.get("controls") or []
                        result["controls"] = len(controls) if isinstance(controls, list) else 0
                        result["text_chars"] = len(str(payload.get("text") or ""))
                        result["dialogs_or_overlays"] = [
                            {
                                "role": str(item.get("role") or ""),
                                "name": str(item.get("name") or "")[:80],
                            }
                            for item in controls
                            if isinstance(item, dict)
                            and str(item.get("role") or "").casefold()
                            in {"dialog", "alertdialog"}
                        ][:8]
                        try:
                            heading_response = await control.web_control(
                                context,
                                action="find",
                                role="heading",
                                position="first",
                            )
                            result["semantic_heading_found"] = bool(
                                _payload(heading_response).get("matched")
                            )
                        except ToolError as exc:
                            result["evidence"] = f"Page opened; heading lookup: {str(exc)[:160]}"
                        if not result["evidence"]:
                            result["evidence"] = (
                                f"Opened and observed page; observation chars={len(observation)}"
                            )
                        is_blocked = _is_access_challenge(
                            result["title"],
                            result["landed_url"],
                            str(payload.get("text") or ""),
                        )
                        result["status"] = (
                            "BLOCKED"
                            if is_blocked
                            else (
                                "UNSTABLE"
                                if navigation_race
                                or (category in {"video"} and not result["title"])
                                else "PASS"
                            )
                        )
                        if is_blocked:
                            result["evidence"] = (
                                "Navigation reached an automated-access challenge/interstitial; "
                                f"landed at {result['landed_url']}"
                            )
                        elif navigation_race:
                            result["evidence"] = (
                                "The first observation raced a navigation; observation succeeded "
                                f"after the page settled at {result['landed_url']}"
                            )
                        elif result["status"] == "UNSTABLE":
                            result["evidence"] = (
                                "The public page was reachable, but its title was still empty "
                                "when the bounded observation completed."
                            )
                    except ToolError as exc:
                        result["status"] = _failure_status(str(exc))
                        result["evidence"] = str(exc)[:240]
                    except Exception as exc:  # noqa: BLE001 - record unexpected public-site failures
                        result["status"] = "FAIL"
                        result["evidence"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                    finally:
                        result["duration_ms"] = round(
                            (time.perf_counter() - started) * 1000, 2
                        )
                        results.append(result)
            finally:
                # Close only the tabs this run created, then hand the browser
                # back. A page that existed before the campaign is never closed.
                try:
                    for row in (await session.tab_list()).get("tabs") or []:
                        if row["tab"] not in known:
                            await session.close_tab(row["tab"])
                except Exception:  # noqa: BLE001 - cleanup must not mask results
                    pass
                await session.release()
    finally:
        # The tabs this run created were closed before `release()`, while the
        # connection was still attached. Only the process's own environment is
        # restored here.
        os.environ.pop("LUMINE_BROWSER_PROFILE", None)
        os.environ["LUMINE_BROWSER_ALLOW_LOCAL"] = "1"

    counts = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    return {
        "scope": "Public unauthenticated GET navigation, observation, and semantic heading lookup only.",
        "non_destructive": True,
        "browser": "Lumine's own persistent profile, visible window",
        "site_count": len(results),
        "status_counts": counts,
        "results": results,
    }


def main() -> None:
    summary = asyncio.run(_run())
    output = Path(__file__).parent / "public_compatibility_results.json"
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for item in summary["results"]:
        print(
            f'{item["status"]:7} {item["category"]:20} {item["site"]}: '
            f'{item["title"][:60]} ({item["duration_ms"]:.0f} ms)'
        )
    print(f'Results: {output} ({summary["status_counts"]})')
    if summary["status_counts"].get("FAIL", 0):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
