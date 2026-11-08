"""Runner for Lumine's browser certification campaign.

The campaign is deliberately runnable in **groups**. A single 68-scenario run in
a real, visible browser takes long enough that a failure near the end costs the
whole sample, and a group's JSON is mergeable, so the evidence is assembled from
several bounded runs rather than one heroic one.

    python run_certification.py --group 1
    python run_certification.py --phase 16
    python run_certification.py --public
    python run_certification.py --merge certification_results.group1.json ...

Every scenario writes one record with one of seven statuses. Those statuses are
*findings*, not runner state: a `FAIL` means the product has a defect, and the
run still completed. The process therefore exits non-zero only when the suite
could not be executed at all.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Iterable

# Ensure repo root and agent directory are in sys.path
agent_dir = Path(__file__).resolve().parent.parent.parent
repo_root = agent_dir.parent
for p in (str(agent_dir), str(repo_root)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent.tests.harness.test_certification_phases import (
    BrowserCertificationTestCase,
    CERTIFICATION_RESULTS,
    record_result,
)

HERE = Path(__file__).parent

#: Phase groups. Bounded so one wedged scenario cannot cost the whole sample, and
#: arranged by subject so a group's JSON answers one question on its own.
GROUPS: dict[int, tuple[int, int]] = {
    1: (1, 3),    # lifecycle, inspection, discovery
    2: (4, 6),    # pointer, keyboard, forms
    3: (7, 9),    # scrolling, complex DOM, tabs
    4: (10, 12),  # dialogs, downloads/uploads, screenshots
    5: (13, 15),  # session sharing, planner, recovery
    6: (16, 17),  # security, performance
    7: (18, 19),  # stability, real-world sites
}

PHASE_NAMES: dict[int, str] = {
    1: "Lifecycle & Navigation",
    2: "Page Inspection",
    3: "Element Discovery",
    4: "Pointer Interaction",
    5: "Keyboard Control",
    6: "Form Interaction",
    7: "Scrolling & Viewport",
    8: "Complex DOM",
    9: "Tabs & Popups",
    10: "Dialogs & Permissions",
    11: "Downloads & Uploads",
    12: "Screenshots & Vision",
    13: "Session Sharing & State Sync",
    14: "Planner Intelligence",
    15: "Failure Recovery",
    16: "Security & Safety",
    17: "Performance",
    18: "Stability",
    19: "Real-World Compatibility",
}

#: The seven classifications the campaign reports in. The record's status is the
#: machine value; this is the sentence a reader needs.
CLASSIFICATION: dict[str, str] = {
    "PASS": "implemented and verified",
    "UNRELIABLE": "implemented but unreliable",
    "UNSUPPORTED": "not implemented",
    "INTENTIONALLY_REFUSED": "intentionally blocked by policy",
    "FAIL": "failed due to implementation defect",
    "BLOCKED": "blocked by environment",
    "NOT_TESTED": "not tested",
}

#: Public-site states are its own vocabulary; they are folded into the same seven
#: so the matrix cannot count one capability two ways.
PUBLIC_STATUS: dict[str, str] = {
    "PASS": "PASS",
    "UNSTABLE": "UNRELIABLE",
    "BLOCKED": "BLOCKED",
    "FAIL": "FAIL",
}


class TimedTestResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started_at: dict[str, float] = {}
        self.durations_ms: dict[str, float] = {}

    def startTest(self, test):
        self.started_at[test.id()] = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        started = self.started_at.pop(test.id(), None)
        if started is not None:
            self.durations_ms[test.id()] = (time.perf_counter() - started) * 1000
        super().stopTest(test)


def phase_of(test: unittest.TestCase) -> int:
    method = getattr(test, "_testMethodName", "")
    parts = method.removeprefix("test_").split("_", 2)
    if len(parts) >= 2 and parts[0].isdigit():
        return int(parts[0])
    return 0


def _record_unreported_results(test_result: TimedTestResult) -> None:
    recorded = {item["id"]: item for item in CERTIFICATION_RESULTS}
    outcomes = [
        (test, trace, "FAIL") for test, trace in test_result.failures
    ] + [
        (test, trace, "FAIL") for test, trace in test_result.errors
    ] + [
        (test, reason, "NOT_TESTED") for test, reason in test_result.skipped
    ]

    for test, details, status in outcomes:
        method = getattr(test, "_testMethodName", "")
        parts = method.removeprefix("test_").split("_", 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            test_id, phase, category = test.id(), 0, "Unclassified"
        else:
            phase = int(parts[0])
            test_id = f"TC-{parts[0]}-{parts[1]}"
            category = PHASE_NAMES.get(phase, "Unclassified")

        description = test.shortDescription() or method or test.id()
        detail = str(details)
        evidence = detail.splitlines()[-1] if detail else status
        duration = round(test_result.durations_ms.get(test.id(), 0.0), 2)
        result = recorded.get(test_id)
        if result is None:
            record_result(
                test_id,
                phase,
                category,
                description,
                status,
                duration,
                evidence,
                detail if status == "FAIL" else "",
            )
        else:
            result["status"] = status
            result["actual_outcome"] = evidence
            result["failure_reason"] = detail if status == "FAIL" else ""
            result["defect"] = result["failure_reason"]
            result["evidence"] = evidence
            result["duration_ms"] = duration
            result["evidence_ref"] = f"test:{test.id()}"


def _status_counts(results: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    return counts


def _capability_matrix(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per phase, because the phases are the capability areas."""
    grouped: dict[int, dict[str, Any]] = {}
    for result in results:
        phase = int(result.get("phase") or 0)
        row = grouped.setdefault(
            phase,
            {
                "phase": phase,
                "category": PHASE_NAMES.get(phase, result.get("category") or "Unclassified"),
                "classification": {},
                "capabilities": [],
            },
        )
        status = result["status"]
        row["classification"][status] = row["classification"].get(status, 0) + 1
        row["capabilities"].append(
            {
                "id": result["id"],
                "capability": result["description"],
                "status": status,
                "classification": CLASSIFICATION.get(status, status),
                "evidence": result["evidence"],
                "defect": result["defect"],
            }
        )

    for row in grouped.values():
        row["capabilities"].sort(key=lambda item: item["id"])
        counts = row["classification"]
        if counts.get("FAIL", 0):
            row["status"] = "FAIL"
        elif counts.get("BLOCKED", 0):
            row["status"] = "BLOCKED"
        elif counts.get("UNRELIABLE", 0):
            row["status"] = "UNRELIABLE"
        elif counts.get("UNSUPPORTED", 0) or counts.get("NOT_TESTED", 0):
            row["status"] = "PARTIAL"
        else:
            row["status"] = "PASS"
    return [grouped[key] for key in sorted(grouped)]


def _scope_gaps() -> list[dict[str, str]]:
    """What this campaign deliberately does not answer.

    Named rather than omitted: an unmeasured capability is not a working one, and
    a report that silently drops it reads as coverage it does not have.
    """
    return [
        {
            "capability": "Authenticated account behaviour",
            "status": "NOT_TESTED",
            "reason": "Only the public, signed-out surface was exercised; no account was used.",
        },
        {
            "capability": "Real consent round-trip (ask, speak, answer, act)",
            "status": "NOT_TESTED",
            "reason": "Local fixtures stub `require_approval`; the gate's class is measured, the spoken consent path is not.",
        },
        {
            "capability": "Browser crash and restart recovery",
            "status": "NOT_TESTED",
            "reason": "No browser process was deliberately crashed.",
        },
        {
            "capability": "Long-duration soak and resource growth",
            "status": "NOT_TESTED",
            "reason": "Stability is a bounded repeated-cycle sample, not an hours-long soak.",
        },
        {
            "capability": "Model interpretation of screenshots",
            "status": "NOT_TESTED",
            "reason": "Capture, format and bounds are measured; what a vision model makes of them is not.",
        },
        {
            "capability": "Cross-platform behaviour (macOS/Linux)",
            "status": "NOT_TESTED",
            "reason": "This run used the current Windows machine only.",
        },
        {
            "capability": "File upload capability",
            "status": "UNSUPPORTED",
            "reason": "No `set_input_files`/file-chooser path exists in either tool.",
        },
        {
            "capability": "Open Shadow DOM controls",
            "status": "UNSUPPORTED",
            "reason": "The collector does not pierce shadow roots; verified rather than assumed.",
        },
        {
            "capability": "Same- or cross-origin iframe action by semantic locator",
            "status": "UNSUPPORTED",
            "reason": "Frames are resolved by the driver but not by the candidate collector.",
        },
    ]


def _write_report(path: Path, summary: dict[str, Any]) -> None:
    """Write the human-readable companion to the JSON.

    A merged summary has no `tests_run`/`elapsed_seconds` -- it is assembled from
    several bounded runs -- so every field here is read with a default. The first
    version assumed the fields a single run produces and crashed on the merge,
    which is to say it crashed on the only document worth reading.
    """
    details = sorted(summary.get("details") or [], key=lambda item: item["id"])
    lines = [
        "# Lumine browser certification — run summary",
        "",
        f"- Label: **{summary.get('label') or 'merged campaign'}**",
        f"- Generated: **{summary.get('generated_at') or 'n/a'}**",
        f"- Recorded scenarios: **{summary.get('recorded_tests', len(details))}**",
    ]
    if summary.get("tests_run") is not None:
        lines.append(f"- Test cases executed: **{summary['tests_run']}**")
    if summary.get("elapsed_seconds") is not None:
        lines.append(f"- Wall clock: **{summary['elapsed_seconds']} s**")
    lines.append(
        f"- Status counts: `{json.dumps(summary.get('status_counts') or {}, sort_keys=True)}`"
    )
    lines += [
        "",
        "Every record is one of seven classifications. `PASS` means implemented and",
        "verified; `UNSUPPORTED` and `INTENTIONALLY_REFUSED` are **not** successes.",
        "",
        "## Capability matrix",
        "",
        "| Phase | Area | Result | Counts |",
        "| --- | --- | --- | --- |",
    ]
    for row in summary.get("capability_matrix") or []:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(row["classification"].items()))
        lines.append(
            f"| {row['phase']} | {row['category']} | **{row['status']}** | {counts} |"
        )

    defects = [item for item in details if item.get("status") == "FAIL"]
    if defects:
        lines += ["", "## Open defects", "", "| ID | Capability | Evidence |", "| --- | --- | --- |"]
        for item in defects:
            evidence = str(item.get("evidence") or "").replace("|", "\\|")
            lines.append(f"| {item['id']} | {item['description']} | {evidence} |")

    lines += [
        "",
        "## Scenarios",
        "",
        "| ID | Status | Classification | Capability | Evidence |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in details:
        evidence = str(result.get("evidence") or "").replace("|", "\\|")
        if len(evidence) > 320:
            evidence = evidence[:317] + "…"
        label = CLASSIFICATION.get(result["status"], result["status"])
        lines.append(
            f"| {result['id']} | {result['status']} | {label} | "
            f"{result['description']} | {evidence} |"
        )

    gaps = summary.get("scope_gaps") or []
    if gaps:
        lines += ["", "## Not answered by this campaign", "", "| Capability | Status | Reason |", "| --- | --- | --- |"]
        for gap in gaps:
            lines.append(
                f"| {gap['capability']} | {gap['status']} | {gap['reason']} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_public_campaign() -> list[dict[str, Any]]:
    """Run the real-site campaign in its own process, then fold it in as phase 19."""
    script = HERE / "run_public_compatibility.py"
    print("\n" + "=" * 80)
    print("PHASE 19: public site compatibility campaign")
    print("=" * 80)
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    sys.stdout.write(completed.stdout)
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr)
    path = HERE / "public_compatibility_results.json"
    if not path.exists():
        return [
            {
                "id": "TC-19-PUBLIC",
                "phase": 19,
                "category": PHASE_NAMES[19],
                "description": "Public site campaign",
                "status": "BLOCKED",
                "duration_ms": 0.0,
                "preconditions": [],
                "steps": [],
                "expected_outcome": "",
                "actual_outcome": "the public campaign produced no results file",
                "evidence": (completed.stderr or completed.stdout or "")[-600:],
                "evidence_ref": "script:run_public_compatibility.py",
                "failure_reason": "",
                "defect": "",
            }
        ]

    return _fold_public_results(path)


def _fold_public_results(path: Path) -> list[dict[str, Any]]:
    """Fold the real-site campaign's JSON into the same seven statuses.

    Shared by the run path and the merge path, because the phase-19 evidence is
    produced by a separate process: a merge that ignored it would report 68
    scenarios while the machine held 15 more public-site results, and phase 19
    would look untested on a campaign that had in fact measured it.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    folded: list[dict[str, Any]] = []
    for item in payload.get("results") or []:
        status = PUBLIC_STATUS.get(str(item.get("status")), "UNRELIABLE")
        folded.append(
            {
                "id": f"TC-19-PUBLIC-{item.get('category', 'site')}",
                "phase": 19,
                "category": PHASE_NAMES[19],
                "description": f"Open and read {item.get('site')}",
                "status": status,
                "duration_ms": item.get("duration_ms") or 0.0,
                "preconditions": ["Lumine's own persistent browser profile"],
                "steps": [f"open {item.get('requested_url')}", "observe", "find the first heading"],
                "expected_outcome": "The public page opens and its content is readable.",
                "actual_outcome": item.get("evidence") or "",
                "evidence": item.get("evidence") or "",
                "evidence_ref": f"public:{item.get('id')}",
                "failure_reason": "" if status == "PASS" else (item.get("evidence") or ""),
                "defect": "",
                "landed_url": item.get("landed_url") or "",
                "title": item.get("title") or "",
            }
        )
    return folded


def _select_tests(group: int | None, phase: int | None) -> unittest.TestSuite:
    loader = unittest.defaultTestLoader
    all_tests = list(loader.loadTestsFromTestCase(BrowserCertificationTestCase))

    def flat(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from flat(item)
            else:
                yield item

    selected = []
    for test in flat(all_tests):
        current = phase_of(test)
        if phase is not None and current != phase:
            continue
        if group is not None:
            low, high = GROUPS[group]
            if not low <= current <= high:
                continue
        selected.append(test)
    return unittest.TestSuite(selected)


def _merge(paths: list[Path]) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("details") or []:
            merged[item["id"]] = item
    # Phase 19's breadth evidence lives in its own file, written by its own
    # process. Folding it in here means one merged document answers the whole
    # campaign rather than 68 of its 83 observations.
    public_path = HERE / "public_compatibility_results.json"
    public_rows = _fold_public_results(public_path) if public_path.exists() else []
    for item in public_rows:
        merged[item["id"]] = item
    details = list(merged.values())
    counts = _status_counts(details)
    return {
        "campaign": "Lumine browser control certification",
        "merged_from": [str(path) for path in paths],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "recorded_tests": len(details),
        "status_counts": counts,
        "failed_capabilities": counts.get("FAIL", 0),
        "capability_matrix": _capability_matrix(details),
        "scope_gaps": _scope_gaps(),
        "details": details,
        "public_results": public_rows,
        "environment": {
            "profile": "Lumine's own persistent profile (LUMINE_CERT_PROFILE overrides)",
            "headless": False,
            "allow_local": os.getenv("LUMINE_BROWSER_ALLOW_LOCAL", ""),
            "python": sys.version.split()[0],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", type=int, choices=sorted(GROUPS))
    parser.add_argument("--phase", type=int)
    parser.add_argument("--public", action="store_true", help="also run the real-site campaign")
    parser.add_argument("--merge", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--list", action="store_true", help="list selected scenarios and exit")
    args = parser.parse_args()

    if args.merge:
        summary = _merge(args.merge)
        output = args.out or (HERE / "certification_results.json")
        output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        _write_report(output.with_suffix(".md"), summary)
        print(f"merged {len(args.merge)} file(s) -> {output}")
        print(json.dumps(summary["status_counts"], sort_keys=True))
        return 0

    suite = _select_tests(args.group, args.phase)
    tests = list(suite)
    if args.list or not tests:
        for test in tests:
            print(test.id())
        print(f"{len(tests)} scenario(s) selected")
        return 0

    label = (
        f"phase {args.phase}"
        if args.phase is not None
        else (f"group {args.group} (phases {GROUPS[args.group][0]}-{GROUPS[args.group][1]})" if args.group else "all phases")
    )
    print("=" * 80)
    print(f"LUMINE BROWSER CONTROL — CERTIFICATION ({label})")
    print("=" * 80)

    CERTIFICATION_RESULTS.clear()
    runner = unittest.TextTestRunner(verbosity=2, resultclass=TimedTestResult)
    started = time.time()
    test_result = runner.run(suite)
    elapsed = round(time.time() - started, 2)
    _record_unreported_results(test_result)

    if args.public:
        CERTIFICATION_RESULTS.extend(_run_public_campaign())

    counts = _status_counts(CERTIFICATION_RESULTS)
    summary = {
        "campaign": "Lumine browser control certification",
        "label": label,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tests_run": test_result.testsRun,
        "failures": len(test_result.failures),
        "errors": len(test_result.errors),
        "skipped": len(test_result.skipped),
        "recorded_tests": len(CERTIFICATION_RESULTS),
        "elapsed_seconds": elapsed,
        "status_counts": counts,
        "failed_capabilities": counts.get("FAIL", 0),
        "capability_matrix": _capability_matrix(CERTIFICATION_RESULTS),
        "scope_gaps": _scope_gaps(),
        "environment": {
            "profile": "Lumine's own persistent profile (LUMINE_CERT_PROFILE overrides)",
            "headless": os.getenv("LUMINE_BROWSER_HEADLESS", ""),
            "allow_local": os.getenv("LUMINE_BROWSER_ALLOW_LOCAL", ""),
            "python": sys.version.split()[0],
        },
        "details": CERTIFICATION_RESULTS,
    }

    output = args.out or (
        HERE / f"certification_results.group{args.group}.json"
        if args.group is not None
        else HERE / "certification_results.json"
    )
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _write_report(output.with_suffix(".md"), summary)
    print("\n" + "=" * 80)
    print(f"results -> {output}")
    print(f"scenarios recorded: {len(CERTIFICATION_RESULTS)} | statuses: {json.dumps(counts, sort_keys=True)}")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
