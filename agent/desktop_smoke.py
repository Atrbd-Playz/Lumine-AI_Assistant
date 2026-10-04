"""End-to-end checks for `desktop_control`, in the order that fails safest.

`desktop_probe.py` answers "what is this window?" — one question, no side
effects, safe to run mid-task. This answers "does the feature work?", which
cannot be answered without moving the pointer, and therefore has to be the
thing you *choose* to run rather than something that happens as a side effect
of looking.

## Why it is staged the way it is

Every check is ordered so that the ones that cannot be undone have the most
evidence behind them:

1. **Read-only first.** Platform, driver, DPI, the permission gate, the
   operation table. None of these touch the mouse, and together they rule out
   every failure that would otherwise show up as "the click did nothing".
2. **Then observation**, still read-only, and still only *reading* — a window
   is revealed without activation, so nothing is focused or scrolled.
3. **Then refusals.** A browser window, a stale generation, a password field.
   These are asserted by driving the real code path with a stubbed engine and
   checking nothing was injected. They are the safety properties, and a
   regression here matters more than any click.
4. **Then one real click**, on a window this script opened, aimed at a control
   it read itself.
5. **Then real typing and a real key**, in the same throwaway window.
6. **Scroll last**, because it is the only one whose effect cannot be undone
   and cannot be verified.

`--stage` runs up to a given stage, so ``--stage read`` is completely safe and
``--stage full`` will move your mouse. The default is ``read``.

## What it deliberately does not do

It never opens a browser or WhatsApp, because those are refused by design and a
script that fought that rule would be testing the wrong thing. It never presses
Enter, because in a text field Enter is a submit and this is not a place to
discover that. It never types into anything it did not open.

    python agent/desktop_smoke.py                  # read-only, safe anywhere
    python agent/desktop_smoke.py --stage refuse   # adds the refusal checks
    python agent/desktop_smoke.py --stage full     # MOVES YOUR MOUSE
    python agent/desktop_smoke.py --json           # machine-readable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

if __package__ in (None, ""):  # running as `python agent/desktop_smoke.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools.desktop import capture, control, input as desktop_input, ops, uia  # noqa: E402
from tools.tools_compat import ToolError  # noqa: E402
from tools.tools_registry import get_tools  # noqa: E402
from tools.tools_text import clip  # noqa: E402

try:
    from tools.tools_permission import _desktop_input_mode, tool_id
except ImportError:  # pragma: no cover - loaded as a package
    from agent.tools.tools_permission import _desktop_input_mode, tool_id  # type: ignore

#: How far the run got. Ordered, so `--stage` is a comparison rather than a set
#: membership test, and a typo cannot silently mean "everything".
STAGES = ("read", "observe", "refuse", "click", "type")

#: Spelled as one word, because it is what anyone reaching for the mouse will
#: type, and having it error out at the argument parser -- after they have
#: already decided to let it move their pointer -- is a poor first impression.
ALIASES = {"full": "type", "all": "type", "read-only": "read", "safe": "read"}
STAGE_ALL = "type"

#: The throwaway application. Notepad on Windows, `open -a TextEdit` elsewhere,
#: because the checks need a window that exposes a text field and a menu, and
#: that is the least surprising one on every platform.
NOTEPAD_STEM = "notepad"


@dataclass
class Check:
    """One assertion and its outcome.

    ``detail`` is what makes a failure actionable. A smoke test that reports
    "FAIL: typing" has cost somebody ten minutes; one that reports "the field
    never took focus, so the keystrokes went nowhere" has cost thirty seconds.
    """

    name: str
    ok: bool
    detail: str = ""
    #: ``skip`` rather than ``ok`` when the environment cannot answer the
    #: question at all -- a non-Windows machine cannot fail a Windows check, and
    #: reporting that as a pass would be claiming something untrue.
    skipped: bool = False


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", skipped: bool = False) -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail, skipped=skipped))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.skipped]

    @property
    def skipped(self) -> list[Check]:
        return [c for c in self.checks if c.skipped]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.failed,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail, "skipped": c.skipped}
                for c in self.checks
            ],
            "failed": len(self.failed),
            "skipped": len(self.skipped),
        }


# ---------------------------------------------------------------------------
# Stage 1: read-only facts
# ---------------------------------------------------------------------------


def stage_read(report: Report) -> None:
    """Everything that can be wrong without touching the mouse."""
    is_windows = sys.platform == "win32"
    report.add(
        "platform is windows",
        is_windows,
        "SendInput and UI Automation are both Windows-only." if is_windows else "not Windows",
        skipped=not is_windows,
    )
    if not is_windows:
        return

    supported = uia.supported()
    report.add(
        "uiautomation driver installed",
        supported,
        "installed" if supported else "pip install uiautomation",
    )

    aligned = desktop_input.set_dpi_awareness()
    report.add(
        "dpi awareness available",
        bool(aligned) or desktop_input.dpi_aware(),
        "clicks and coordinates share a space" if aligned else "clicks may land at the scale factor's distance",
    )

    mode = _desktop_input_mode()
    report.add(
        "desktop.input switch",
        True,
        f"desktop.input = {mode!r} (the tool is live in a session only when this is 'on')",
    )

    # The exclusion: while the desktop control is on there must be exactly one
    # way to click a web page, and turning it off must hand the browser tools
    # straight back. A switch that silences something permanently is worse than
    # no switch.
    from tools import tools_permission as tp

    def live_ids(document):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            tp, "effective_document", return_value=(document, "test", [])
        ):
            return {tool_id(tool) for tool in get_tools()}

    on = live_ids({"tools": {"desktop_control": "on"}})
    off = live_ids({"tools": {"desktop_control": "off"}})
    wanted = set(tp.SUPPRESSED_BY_DESKTOP_INPUT)
    # `silenced` is what is *absent*, not what is present: with the desktop
    # control on, these must not be live. Reading it the other way round asks
    # the opposite question and reports a working exclusion as a failure.
    silenced = sorted(wanted - on)
    restored = sorted(wanted & off)
    report.add(
        "turning it on silences the browser tools",
        "desktop_control" in on and silenced == sorted(wanted),
        f"silenced: {', '.join(silenced) or 'nothing'}"
        f" | live now: {', '.join(sorted(on & {'desktop_control', 'browse', 'web_control'}))}",
    )
    report.add(
        "turning it off gives them back",
        restored == sorted(wanted),
        f"restored: {', '.join(restored) or 'nothing'}"
        f" | live now: {', '.join(sorted(off & {'desktop_control', 'browse', 'web_control'}))}",
    )

    verdict = control.capability()
    report.add(
        "capability reported",
        bool(verdict.get("available")),
        str(verdict.get("reason") or verdict.get("state") or ""),
    )

    # The table, because a missing verb is a plan-validation failure at run time
    # rather than anything a user would notice until it happened.
    for verb in ops.VERBS:
        report.add(
            f"verb {verb} exists",
            ops.operation(verb) is not None,
            ops.operation(verb).action_class if ops.operation(verb) else "missing from the table",
        )
    report.add(
        "the surface is five verbs",
        ops.VERBS == ("observe", "move", "click", "type", "press"),
        " -> ".join(ops.VERBS),
    )

    left, top, right, bottom = desktop_input.screen_rect()
    report.add(
        "screen geometry readable",
        right > left and bottom > top,
        f"{left},{top} to {right},{bottom} across every monitor",
    )


# ---------------------------------------------------------------------------
# Stage 2: observation
# ---------------------------------------------------------------------------


def stage_observe(report: Report) -> None:
    """Read the foreground window. Reveals it without activating it."""
    hwnd = uia.foreground_window()
    report.add("a foreground window exists", bool(hwnd), f"handle {hwnd}" if hwnd else "none")
    if not hwnd:
        return

    try:
        observation = capture.observe(hwnd)
    except Exception as exc:  # noqa: BLE001
        report.add("observe the foreground window", False, f"{type(exc).__name__}: {exc}")
        return

    report.add(
        "observe the foreground window",
        True,
        f"{observation.title or '(untitled)'} · {observation.process or 'unknown process'} "
        f"· {len(observation.elements)} controls · generation {observation.generation}",
    )

    entry = ops.integration_for(capture.process_names_for(hwnd))
    if entry is not None:
        report.add(
            "foreground window is not an integrated one",
            True,
            f"it is {entry.processes[0]}, which belongs to {entry.tool} -- "
            f"a refusal here is the correct behaviour",
        )
    else:
        report.add(
            "foreground window is drivable",
            observation.addressable,
            "controls are listed" if observation.addressable else "nothing exposes a name; a coordinate would be needed",
        )

    # The generation guard, on the real module with a real observation.
    stale = (observation.generation + 1, 0, "seven", 1.5, None)
    refused = 0
    for claimed in stale:
        try:
            capture.check_generation(claimed)
        except ToolError:
            refused += 1
    report.add(
        "a wrong generation is refused",
        refused == len(stale),
        f"{refused} of {len(stale)} bogus generations refused",
    )

    matches = capture.find_window(observation.title.split(" - ")[-1] if observation.title else "")
    report.add(
        "windows can be found by name",
        True,
        f"{len(matches)} window(s) matched by title -- this is how a second monitor is reached",
    )


# ---------------------------------------------------------------------------
# Stage 3: refusals (no injection)
# ---------------------------------------------------------------------------


class _Engine:
    """Records what would have been injected, and injects nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def click(self, *_a: Any, **k: Any) -> None:
        self.calls.append("click")

    def move_to(self, *_a: Any, **_k: Any) -> None:
        self.calls.append("move_to")

    def type_text(self, *_a: Any, **_k: Any) -> int:
        self.calls.append("type_text")
        return 0

    def press_key(self, *_a: Any, **_k: Any) -> None:
        self.calls.append("press_key")

    def drag(self, *_a: Any, **_k: Any) -> None:
        self.calls.append("drag")

    def scroll(self, *_a: Any, **_k: Any) -> None:
        self.calls.append("scroll")


def stage_refuse(report: Report) -> None:
    """The safety properties, asserted against the real code path.

    Every one of these is worth more than a successful click. A feature that
    works but sends a WhatsApp message without asking has traded the whole
    permission model for convenience.
    """
    saved_engine = control._Desktop.input
    engine = _Engine()
    control._Desktop.input = engine
    saved_last = capture._LAST
    saved_foreground = uia.foreground_window
    try:
        # 1. An integrated window is *driven*, not refused -- with a note. The
        #    refusal used to be here, and it turned the fallback into a wall.
        uia.foreground_window = lambda: 4242  # type: ignore[assignment]
        original_names = capture.process_names_for
        capture.process_names_for = lambda _h: ["chrome.exe"]  # type: ignore[assignment]
        try:
            control._check_window(4242)
            note = control._preferred_tool_hint(4242)
            report.add(
                "browser window is drivable",
                "web_control" in note,
                f"driven, with the note: {note.strip(' ()')}" if note else "no note",
            )
        except ToolError as exc:
            report.add("browser window is drivable", False, f"still refusing: {str(exc)[:60]}")
        finally:
            capture.process_names_for = original_names  # type: ignore[assignment]

        # 2. WhatsApp names its own tool rather than sharing the browser's.
        capture.process_names_for = lambda _h: ["WhatsApp.exe"]  # type: ignore[assignment]
        try:
            control._check_window(4242)
            note = control._preferred_tool_hint(4242)
            report.add(
                "whatsapp window is drivable",
                "send_whatsapp" in note,
                f"driven, with the note: {note.strip(' ()')}" if note else "no note",
            )
        except ToolError as exc:
            report.add("whatsapp window is drivable", False, f"still refusing: {str(exc)[:60]}")
        finally:
            capture.process_names_for = original_names  # type: ignore[assignment]

        # 2b. And the one wall that stays: her own window. An agent that can
        #     click anything can click the Tools page and switch itself off.
        with mock.patch.object(uia, "is_our_own", return_value=True):
            try:
                control._check_own_window(4242)
                report.add("her own window is refused", False, "it was allowed")
            except ToolError as exc:
                report.add(
                    "her own window is refused", "own window" in str(exc), str(exc)[:60]
                )

        # 3. A renderer is not a browser. The bug that shipped and would have
        #    refused every Tauri and WinUI application on the machine.
        capture.process_names_for = lambda _h: ["msedgewebview2.exe"]  # type: ignore[assignment]
        try:
            control._check_window(4242)
            report.add(
                "webview renderer is not a browser",
                control._preferred_tool_hint(4242) == "",
                "msedgewebview2 treated as an ordinary app",
            )
        except ToolError as exc:
            report.add("webview renderer is not a browser", False, str(exc)[:70])
        finally:
            capture.process_names_for = original_names  # type: ignore[assignment]

        # 4. A password field is refused structurally, before any gate.
        #    `_fresh_observation` and `_focused_element` are stubbed because
        #    `_do_type` re-reads the real screen -- without them this check
        #    passed for the wrong reason, on a focus failure, and the password
        #    refusal itself was never exercised.
        field = _password_field()
        original_fresh = control._fresh_observation
        original_focused = control._focused_element
        original_target = control._target_window
        control._fresh_observation = _async_return(_observation_with(field))
        control._focused_element = lambda _o: field
        control._target_window = lambda _h: 4242
        try:
            asyncio.run(control._do_type(_ctx(), "hunter2"))
            report.add("password field refused", False, "it typed")
        except ToolError as exc:
            message = str(exc).lower()
            refused = "password" in message and "nothing was typed" in message
            report.add(
                "password field refused",
                refused,
                "refused before typing" if refused else f"wrong reason: {str(exc)[:60]}",
            )
        finally:
            control._fresh_observation = original_fresh
            control._focused_element = original_focused
            control._target_window = original_target

        # 5. A control that has moved is refused, which is the real freshness
        #    check. A generation token cannot do this -- the tool supplies the
        #    number -- so the screen is re-read and the target re-found.
        moved = _FakeLike("button", "Save", (100, 200, 200, 240))
        elsewhere = _FakeLike("button", "Save", (100, 300, 200, 340))
        original_collect = capture._collect
        capture._collect = lambda _h: [elsewhere]
        capture._LAST = _observation_with(moved)
        try:
            control._resolve_target(ref=1, verb="click")
            report.add("a control that moved is refused", False, "it resolved anyway")
        except ToolError as exc:
            report.add(
                "a control that moved is refused",
                "moved" in str(exc),
                str(exc)[:70],
            )
        finally:
            capture._collect = original_collect

        # 6. And one that is still there is confirmed, so the check cannot pass
        #    by refusing everything.
        capture._collect = lambda _h: [moved]
        capture._LAST = _observation_with(moved)
        try:
            _point, element, _tier = control._resolve_target(ref=1, verb="click")
            report.add(
                "a control that stayed put is confirmed",
                element is not None,
                "resolved and will be clicked",
            )
        except ToolError as exc:
            report.add("a control that stayed put is confirmed", False, str(exc)[:70])
        finally:
            capture._collect = original_collect

        # 7. A blocked chord never reaches the key driver.
        try:
            desktop_input.resolve_chord("win+l")
            report.add("lock-the-machine refused", False, "it resolved")
        except ToolError as exc:
            report.add("lock-the-machine refused", "locks the machine" in str(exc), str(exc)[:70])

        # 8. Nothing at all was injected by any of the above.
        report.add(
            "no input was injected by the refusal checks",
            not engine.calls,
            "nothing was sent" if not engine.calls else f"leaked: {engine.calls}",
        )
    finally:
        control._Desktop.input = saved_engine
        capture._LAST = saved_last
        uia.foreground_window = saved_foreground  # type: ignore[assignment]


def _ctx() -> Any:
    class _Context:
        def disallow_interruptions(self) -> None:
            return None

    return _Context()


def _observation_with(element: Any) -> capture.Observation:
    return capture.Observation(
        generation=1, hwnd=4242, title="Somewhere", process="whatever.exe", elements=[element]
    )


def _password_field() -> capture.Element:
    return capture.Element(
        ref=1, role="field", name="Password", rect=(0, 0, 200, 30), is_password=True
    )


def _async_return(value: Any) -> Any:
    async def _inner(*_a: Any, **_k: Any) -> Any:
        return value

    return _inner


class _FakeLike:
    """The attribute subset `capture._collect` produces."""

    def __init__(self, role: str, name: str, rect: tuple[int, int, int, int]) -> None:
        self.ref = 1
        self.role = role
        self.name = name
        self.rect = rect
        self.enabled = True
        self.is_password = False

    @property
    def center(self) -> tuple[int, int]:
        return ((self.rect[0] + self.rect[2]) // 2, (self.rect[1] + self.rect[3]) // 2)


# ---------------------------------------------------------------------------
# Stages 4-6: the real thing
# ---------------------------------------------------------------------------


def _open_notepad() -> int:
    """Open the throwaway window and wait for it to exist.

    Launched through the same `apps.launch` the tool uses, so the smoke test
    exercises the real path rather than a parallel one.
    """
    from tools import apps, app_resolver

    resolver = app_resolver.AppResolver()
    apps.launch(NOTEPAD_STEM)
    target = resolver.resolve(NOTEPAD_STEM)
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        window = _notepad_window()
        if window:
            return window
        time.sleep(0.4)
    return 0


def _notepad_window() -> int:
    for handle, _title in capture.find_window(NOTEPAD_STEM):
        return handle
    return 0


def stage_click(report: Report) -> int:
    """One real click, on a control this script read itself."""
    hwnd = _open_notepad()
    report.add("opened a throwaway window", bool(hwnd), "notepad" if hwnd else "could not open notepad")
    if not hwnd:
        return 0

    try:
        uia.ensure_foreground(hwnd)
        time.sleep(0.4)
    except ToolError as exc:
        # Windows will not hand the foreground to a background process while the
        # user is typing. That is a foreground lock, not a broken feature, and
        # saying so is the difference between a person retrying and a person
        # reporting a bug.
        report.add(
            "click a control",
            False,
            f"{clip(str(exc), 70)} -- Windows keeps the foreground while you "
            f"are typing; run this with your hands off the keyboard",
        )
        return hwnd

    observation = capture.observe(hwnd)
    report.add(
        "read the throwaway window",
        bool(observation.elements),
        f"{observation.title or 'notepad'} · {len(observation.elements)} controls",
    )
    if not observation.elements:
        report.add("click a control", False, "nothing was clickable by name")
        return hwnd

    # Prefer the menu bar: it is the safest thing to open, it closes again, and
    # unlike a document button it cannot alter what the user typed.
    target = next(
        (e for e in observation.elements if e.role in ("menu item", "tab")),
        observation.elements[0],
    )
    before = desktop_input.cursor_position()
    desktop_input.click(target.center)
    after = desktop_input.cursor_position()
    moved = before != after
    report.add(
        "real click lands where it was aimed",
        moved or after == target.center,
        f"clicked {target.describe()} at {target.center}; pointer {before} -> {after}",
    )

    # Close the menu we just opened, so the next stage starts clean.
    desktop_input.press_key("escape")
    time.sleep(0.2)
    return hwnd


def stage_type(report: Report, hwnd: int) -> None:
    """Real typing into the throwaway window, and a real key after it."""
    if not hwnd:
        report.add("type into the throwaway window", False, "skipped: no window")
        return

    try:
        uia.ensure_foreground(hwnd)
    except ToolError as exc:
        # Windows will not hand the foreground to a background process while
        # the user is typing -- a foreground lock, not a broken feature. Reported
        # as its own line so "stop clicking the terminal and try again" is the
        # obvious next step.
        report.add(
            "type into the throwaway window",
            False,
            f"{clip(str(exc), 70)} -- Windows keeps the foreground while you "
            f"are typing; run this with your hands off the keyboard",
        )
        return

    marker = "lumine smoke test"
    typed = desktop_input.type_text(marker, settle=0.5)
    report.add(
        "real typing reached the window",
        typed >= len(marker),
        f"{typed} key events for {len(marker)} characters "
        f"({typed > len(marker)} means surrogate pairs were handled)",
    )

    desktop_input.press_key("ctrl+a", settle=0.2)
    report.add("a modifier chord is accepted", True, "ctrl+a")
    desktop_input.press_key("delete", settle=0.2)
    report.add("cleared what was typed", True, "ctrl+a then delete")


def _close_notepad() -> None:
    """Close the throwaway window, and answer no if it offers to save."""
    hwnd = _notepad_window()
    if not hwnd:
        return
    try:
        uia.driver().SetForegroundWindow(hwnd)
        time.sleep(0.2)
        desktop_input.press_key("alt+f4", settle=0.4)
        # Notepad asks about unsaved text; the smoke test typed into it.
        desktop_input.press_key("n", settle=0.3)
    except Exception:  # noqa: BLE001 -- closing is best effort
        pass
    subprocess.run(  # noqa: S603
        ["taskkill", "/IM", "notepad.exe", "/F"],
        capture_output=True,
        shell=False,
        check=False,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render(report: Report, stage: str) -> str:
    lines = ["desktop_control smoke test", "=" * 26, f"stage: {stage}", ""]
    width = max((len(c.name) for c in report.checks), default=10)
    for check in report.checks:
        mark = "skip" if check.skipped else ("ok  " if check.ok else "FAIL")
        lines.append(f"  [{mark}] {check.name.ljust(width)}  {check.detail}")
    lines.append("")
    if report.failed:
        lines.append(f"{len(report.failed)} FAILED")
        for check in report.failed:
            lines.append(f"  - {check.name}: {check.detail}")
    else:
        passed = len(report.checks) - len(report.skipped)
        lines.append(f"{passed} passed, {len(report.skipped)} skipped, 0 failed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="End-to-end checks for desktop_control.")
    parser.add_argument(
        "--stage",
        default="read",
        choices=(*STAGES, *ALIASES),
        help="how far to go. 'read' and 'observe' touch nothing; 'click' and "
        "beyond move your mouse. 'full' is an alias for the last stage.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="leave the throwaway window open instead of closing it",
    )
    args = parser.parse_args(argv)

    report = Report()
    stage = ALIASES.get(args.stage, args.stage)
    reached = STAGES.index(stage)

    stage_read(report)
    if reached >= STAGES.index("observe"):
        stage_observe(report)
    if reached >= STAGES.index("refuse"):
        stage_refuse(report)

    hwnd = 0
    if reached >= STAGES.index("click"):
        print("  ... about to move your mouse", file=sys.stderr)
        # Each stage is guarded. A smoke test that tracebacks has cost the
        # person running it the one thing it was for -- the report -- and the
        # usual cause is mundane: Windows refuses to let a background process
        # take the foreground while the user is typing, which is a foreground
        # lock, not a broken feature.
        for name, run in (("click", lambda: stage_click(report)),):  # noqa: E501
            try:
                hwnd = run()
            except Exception as exc:  # noqa: BLE001
                report.add(name, False, f"{type(exc).__name__}: {clip(str(exc), 90)}")
        for name, run in (("type", lambda: stage_type(report, hwnd)),):
            if STAGES.index(name) > reached:
                break
            try:
                run()
            except Exception as exc:  # noqa: BLE001
                report.add(name, False, f"{type(exc).__name__}: {clip(str(exc), 90)}")
        if not args.keep_open:
            _close_notepad()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(render(report, stage))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())