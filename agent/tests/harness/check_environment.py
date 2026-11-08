"""Gate 2 — inspect the active Python/Playwright environment without changing it.

This script is deliberately read-only. It does not install, upgrade or remove a
package, and it does not edit a lockfile. It records what is present so a later
result can be tied to a specific interpreter, driver and browser build.

Run from the repository root:

    .\\.venv\\Scripts\\python.exe agent\\tests\\harness\\check_environment.py

Writes ``environment_report.json`` beside this file and prints a Markdown
summary. Exit code is 0 only when the browser can actually launch and navigate
a local page.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent
AGENT_DIR = REPO_ROOT / "agent"

RELEVANT_PACKAGES = (
    "playwright",
    "greenlet",
    "pyee",
    "httpx",
    "beautifulsoup4",
    "pyyaml",
    "livekit-agents",
    "livekit",
)


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(block)
    return digest.hexdigest()


def _browser_dir() -> Path | None:
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override not in ("0",):
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "ms-playwright" if base else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _installed_browsers() -> list[dict[str, Any]]:
    root = _browser_dir()
    if root is None or not root.is_dir():
        return []
    rows = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        rows.append(
            {
                "name": entry.name,
                "bytes": sum(f.stat().st_size for f in entry.rglob("*") if f.is_file()),
            }
        )
    return rows


def _launch_probe() -> dict[str, Any]:
    """Launch headless Chromium over the real driver and navigate to a data page."""
    started = time.perf_counter()
    report: dict[str, Any] = {"ok": False}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - the report must survive a broken import
        report["error"] = f"import failed: {exc}"
        return report

    with tempfile.TemporaryDirectory(prefix="lumine_env_probe_") as profile:
        try:
            with sync_playwright() as node:
                context = node.chromium.launch_persistent_context(
                    user_data_dir=profile,
                    headless=True,
                )
                browser = context.browser
                report["browser_version"] = browser.version if browser else "persistent"
                page = context.pages[0] if context.pages else context.new_page()
                page.set_content("<title>env-probe</title><h1 id='ok'>probe</h1>")
                report["title"] = page.title()
                report["text"] = page.inner_text("#ok")
                session = page.context.new_cdp_session(page)
                report["cdp"] = session.send("Browser.getVersion")
                context.close()
                report["ok"] = report["title"] == "env-probe" and report["text"] == "probe"
        except Exception as exc:  # noqa: BLE001
            report["error"] = f"{type(exc).__name__}: {exc}"
    report["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return report


def _lockfiles() -> list[dict[str, Any]]:
    candidates = (
        REPO_ROOT / "agent" / "requirements.txt",
        REPO_ROOT / "package.json",
        REPO_ROOT / "package-lock.json",
        REPO_ROOT / "lumine-ui" / "package.json",
        REPO_ROOT / "lumine-ui" / "package-lock.json",
        REPO_ROOT / "lumine-ui" / "src-tauri" / "Cargo.toml",
        REPO_ROOT / "lumine-ui" / "src-tauri" / "Cargo.lock",
    )
    rows = []
    for path in candidates:
        if path.is_file():
            rows.append(
                {
                    "path": str(path.relative_to(REPO_ROOT)),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    return rows


def _git_state() -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=20,
            )
            return out.stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    return {
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": run("rev-parse", "HEAD"),
        "dirty_entries": len([line for line in run("status", "--porcelain").splitlines() if line]),
    }


def _test_environment() -> dict[str, Any]:
    keys = (
        "LUMINE_BROWSER_HEADLESS",
        "LUMINE_BROWSER_ALLOW_LOCAL",
        "LUMINE_BROWSER_PROFILE",
        "LUMINE_BROWSER_ENGINE",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PYTHONPATH",
    )
    return {key: os.environ.get(key, "") for key in keys}


def main() -> int:
    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": {
            "executable": sys.executable,
            "version": sys.version,
            "implementation": platform.python_implementation(),
            "architecture": platform.machine(),
            "in_virtualenv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
            "pip": _version("pip"),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
        },
        "packages": {name: _version(name) for name in RELEVANT_PACKAGES},
        "browsers": _installed_browsers(),
        "launch_probe": _launch_probe(),
        "lockfiles": _lockfiles(),
        "git": _git_state(),
        "test_environment": _test_environment(),
        "modifications_made": [],
    }

    output = HERE / "environment_report.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    python = report["python"]
    probe = report["launch_probe"]
    print("# Environment report")
    print()
    print(f"- Python: {python['version'].splitlines()[0]}")
    print(f"- Executable: `{python['executable']}`")
    print(f"- Virtual environment: {python['in_virtualenv']}")
    print(f"- pip: {python['pip']}")
    print(f"- OS: {report['platform']['system']} {report['platform']['release']}")
    print()
    print("| Package | Version |")
    print("| --- | --- |")
    for name, version in report["packages"].items():
        print(f"| {name} | {version or 'absent'} |")
    print()
    print("| Browser build | Size |")
    print("| --- | --- |")
    for row in report["browsers"]:
        print(f"| {row['name']} | {row['bytes'] / (1024 * 1024):.1f} MiB |")
    print()
    print(f"- Chromium launch/navigate: **{'OK' if probe.get('ok') else 'FAILED'}**")
    if probe.get("ok"):
        print(f"- Browser version: {probe['browser_version']}")
        print(f"- CDP protocol: {probe['cdp'].get('protocolVersion')}")
        print(f"- Probe duration: {probe['duration_ms']} ms")
    else:
        print(f"- Error: {probe.get('error')}")
    print()
    print(f"Report written to `{output.relative_to(REPO_ROOT)}`")
    print("- Environment modifications in this step: **none**")

    return 0 if probe.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
