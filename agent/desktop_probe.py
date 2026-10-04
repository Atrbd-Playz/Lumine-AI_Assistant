"""What the desktop control tool can actually reach, right now.

``whatsapp_probe.py`` exists because a UI that changes under you should produce
one specific line rather than a silent no-op, and the only way to keep a
selector list true is to ask the running application rather than recall it.
That reasoning applies unchanged to ``desktop_control``, and it matters more
here: the difference between a usable tool and an unusable one is *whether the
application in front of the user exposes a tree at all*, and that varies per
window. A tool that worked against Notepad and silently does nothing against a
game is the failure this reports.

Read-only by construction. It opens no window, clicks nothing, types nothing,
and sends no input -- the probe reports what is addressable and stops. Running
it on a machine mid-task cannot disturb that task.

    python agent/desktop_probe.py            # the foreground window
    python agent/desktop_probe.py --json     # machine-readable
    python agent/desktop_probe.py --capability
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # running as `python agent/desktop_probe.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.desktop import capture, control, input as desktop_input, ops, uia  # noqa: E402

try:
    from tools.tools_permission import _desktop_input_mode
except ImportError:  # pragma: no cover - loaded as a package
    from agent.tools.tools_permission import _desktop_input_mode  # type: ignore


def _window_line(observation: capture.Observation) -> str:
    bits = [f"window      {observation.title or '(untitled)'}"]
    bits.append(f"process     {observation.process or '(unknown)'}")
    bits.append(f"generation  {observation.generation}")
    bits.append(f"dpi aligned {observation.dpi_aligned}")
    if observation.addressable:
        bits.append(f"elements    {len(observation.elements)}")
        bits.append("")
        bits.append("  #   role          name")
        for element in observation.elements:
            flags = ""
            if not element.enabled:
                flags = "  (disabled)"
            elif element.is_password:
                flags = "  (password - typing refused)"
            bits.append(
                f"  {element.ref:<3} {element.role:<13}{element.name or '(no name)'}{flags}"
            )
    else:
        bits.append("elements    none -- nothing on this screen is addressable")
        bits.append("")
        bits.append(
            "  Only an x/y coordinate would work here, which is the weakest tier"
        )
        bits.append("  and always asks first. A canvas, a game, or a remote")
        bits.append("  desktop all look like this.")
    return "\n".join(bits)


def report(hwnd: int | None = None) -> dict[str, Any]:
    """Everything the probe knows, as one dict."""
    target = int(hwnd or 0) or uia.foreground_window()
    environment = capture.describe_environment()
    result: dict[str, Any] = {
        "supported": environment["windows"] and environment["driver"],
        "switchedOn": _desktop_input_mode() == "on",
        "environment": environment,
        "capability": control.capability(),
        "operations": [op.to_dict() for op in ops.OPERATIONS],
        "integratedWindows": [
            {"processes": list(entry.processes), "tool": entry.tool, "reason": entry.reason}
            for entry in ops.INTEGRATED_WINDOWS
        ],
    }
    if not target:
        result["window"] = None
        result["reason"] = "no foreground window"
        return result
    observation = capture.observe(target)
    integration = ops.integration_for(capture.process_names_for(target))
    result["window"] = observation.to_dict()
    # Kept out of the JSON: it holds live UIA control objects, which have no
    # serializable form and are only useful to the text renderer.
    result["_observation"] = observation
    result["refused"] = integration.tool if integration else None
    if integration:
        result["refusal"] = ops.refusal_for(integration)
    return result


def render(result: dict[str, Any]) -> str:
    lines = ["desktop_control probe", "=" * 22, ""]
    if not result["supported"]:
        lines.append("NOT SUPPORTED on this machine.")
        lines.append(f"  windows {result['environment']['windows']}")
        lines.append(f"  driver  {result['environment']['driver']}")
        lines.append("")
        lines.append("Both are needed. The driver is the `uiautomation` package:")
        lines.append("  .\\.venv\\Scripts\\python.exe -m pip install uiautomation")
        return "\n".join(lines)

    lines.append(f"switched on   {result['switchedOn']}")
    if not result["switchedOn"]:
        lines.append("")
        lines.append(
            "  The tool is OFF. Turn it on with LUMINE_ENABLE_DESKTOP_INPUT=true"
        )
        lines.append("  or desktop.input = \"on\" in lumine.config.json.")

    window = result.get("window")
    lines.append("")
    if window is None:
        lines.append(f"no window: {result.get('reason', '')}")
    else:
        lines.append(_window_line(result["_observation"]))
    if result.get("refused"):
        lines.append("")
        lines.append(f"REFUSED HERE -- this window belongs to {result['refused']}")
        lines.append(f"  {result['refusal']}")

    lines.append("")
    lines.append("verbs")
    for op in ops.OPERATIONS:
        hold = f"  hold {op.hold_seconds:.0f}s" if op.hold_seconds else ""
        lines.append(f"  {op.verb:<13} {op.action_class:<12} {op.summary}{hold}")

    lines.append("")
    lines.append("windows that are never driven by this tool")
    for entry in ops.INTEGRATED_WINDOWS:
        lines.append(f"  {entry.processes[0]:<12} -> {entry.tool}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--capability",
        action="store_true",
        help="only whether the tool could run right now",
    )
    args = parser.parse_args(argv)

    if args.capability:
        verdict = control.capability()
        print(json.dumps(verdict, indent=2) if args.json else _capability_line(verdict))
        return 0 if verdict.get("available") else 1

    result = report()
    if args.json:
        result.pop("_observation", None)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(render(result))
    return 0


def _capability_line(verdict: dict[str, Any]) -> str:
    if verdict.get("available"):
        state = verdict.get("state", "ready")
        return f"available ({state})"
    return f"unavailable: {verdict.get('reason', 'unknown')}"


if __name__ == "__main__":
    raise SystemExit(main())
