"""Aggregate captured ``LUMINE_EVENT`` records into a latency report.

The worker publishes every timing on stdout as one ``LUMINE_EVENT <json>`` line
per record, because that is the stream the desktop app re-emits. It is the only
place these numbers exist: there is no metrics server, and a local-first app
should not need one. So this CLI reads a *capture* of that stream and answers
the question the numbers were collected for -- **what is the user actually
waiting for?**

It is deliberately offline. It reads a file (or stdin), parses what it finds and
prints aggregates. It never contacts a provider, never starts a worker, needs no
credentials, and so can sit in the test suite: that is the whole reason the
aggregation lives here rather than in a script that has to hold a session open.

Capture a session, then report on it::

    .\\.venv\\Scripts\\python.exe agent\\agent.py dev *> worker.log
    # ... have a conversation ...
    .\\.venv\\Scripts\\python.exe agent\\perf_report.py worker.log

or follow a live one::

    Get-Content worker.log -Wait | .\\.venv\\Scripts\\python.exe agent\\perf_report.py -

Two shapes of record come out of the recorder, and reporting them separately is
the point of the exercise:

* **Durations** carry ``duration_ms``: *how long did this take*, measured around
  the thing itself on a monotonic clock. These group by ``phase`` -- so model
  time is never read as tool time -- and by ``stage``.
* **Marks** have no ``duration_ms``: their ``elapsed_ms`` is *how far into the
  session* the mark was. Those are milestones on a timeline. Averaging them
  would produce a number describing nothing, so they get their own table and are
  never mixed into a duration.

The headline is ``turn_end_to_end`` -- user finished speaking to reply actually
spoken -- because it is the only figure that is the user's own wait end to end.
Every other row is a component of it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from typing import Any, TextIO

try:
    from .runtime.latency import PHASES
except ImportError:  # run as ``python agent/perf_report.py``, agent/ on sys.path
    from runtime.latency import PHASES

#: How a captured line is prefixed on stdout. Stripped before parsing so a file
#: and a pipe behave identically.
PREFIX = "LUMINE_EVENT "

#: Rows printed per table unless ``--top`` says otherwise.
DEFAULT_TOP = 10


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def parse_line(line: str) -> dict[str, Any] | None:
    """One captured line to a record, or ``None`` if it is not one.

    Anything unrecognised is *ignored rather than raised on*: a capture is
    interleaved with the worker's ordinary logging, and a report that refuses to
    run because line 402 was a traceback is a report nobody will use.
    """
    text = line.strip()
    if not text:
        return None
    if text.startswith(PREFIX):
        text = text[len(PREFIX) :].strip()
    if not text.startswith("{"):
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def read_records(lines: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Records from a stream of captured lines, plus what was skipped."""
    records: list[dict[str, Any]] = []
    counts = {"lines": 0, "records": 0, "ignored": 0}
    for line in lines:
        counts["lines"] += 1
        record = parse_line(line)
        if record is None:
            counts["ignored"] += 1
        else:
            records.append(record)
            counts["records"] += 1
    return records, counts


def _open(path: str) -> TextIO:
    if path == "-":
        return sys.stdin
    # Errors replaced rather than raised: a capture from a killed process can end
    # mid-character, and losing the whole report to one bad byte is absurd.
    return open(path, "r", encoding="utf-8", errors="replace", newline="")


def gather(paths: Sequence[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Records from every path given, with counts summed across them."""
    records: list[dict[str, Any]] = []
    totals = {"lines": 0, "records": 0, "ignored": 0}
    for path in paths:
        handle = _open(path)
        try:
            found, counts = read_records(handle)
        finally:
            if handle is not sys.stdin:
                handle.close()
        records.extend(found)
        for key in totals:
            totals[key] += counts[key]
    return records, totals


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _round(value: float) -> float:
    return round(float(value), 1)


def percentile(ordered: Sequence[float], quantile: float) -> float:
    """Nearest-rank percentile.

    Chosen over interpolation deliberately: with a handful of turns, an
    interpolated p95 invents a value no user ever waited for. Nearest-rank
    always answers with a measurement that actually happened.
    """
    if not ordered:
        raise ValueError("percentile of an empty sample")
    if len(ordered) == 1:
        return float(ordered[0])
    rank = math.ceil(quantile * len(ordered))
    index = min(len(ordered) - 1, max(0, rank - 1))
    return float(ordered[index])


def stats(values: Sequence[float]) -> dict[str, float | int]:
    """The shape every table in this report uses."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("stats of an empty sample")
    return {
        "count": len(ordered),
        "min": _round(ordered[0]),
        "p50": _round(percentile(ordered, 0.50)),
        "p95": _round(percentile(ordered, 0.95)),
        "max": _round(ordered[-1]),
        "mean": _round(sum(ordered) / len(ordered)),
    }


def _duration(record: dict[str, Any]) -> float | None:
    """The measured duration of a record, or ``None`` if it is a mark."""
    value = record.get("duration_ms")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _is_turn_end(record: dict[str, Any]) -> bool:
    return record.get("stage") == "turn_end_to_end"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def summarize(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Everything the report prints, as data.

    Kept separate from the rendering so a test can assert on the numbers rather
    than on column alignment, and so ``--json`` is the same data by definition
    rather than a second implementation that can drift from the first.
    """
    by_phase: dict[str, list[float]] = defaultdict(list)
    by_stage: dict[str, list[float]] = defaultdict(list)
    by_tool: dict[str, list[float]] = defaultdict(list)
    milestones: dict[str, list[float]] = defaultdict(list)
    milestone_detail: dict[str, str] = {}
    decisions: Counter[str] = Counter()
    channels: Counter[str] = Counter()
    types: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    sessions: set[str] = set()
    tools_asked: Counter[str] = Counter()

    turn_all: list[float] = []
    turn_completed: list[float] = []
    turn_interrupted: list[float] = []
    turns = {"total": 0, "interrupted": 0, "completed": 0}

    visual: dict[str, Any] = {}
    notices: Counter[str] = Counter()

    for record in records:
        record_type = str(record.get("type") or "unknown")
        types[record_type] += 1
        session_id = record.get("session_id")
        if isinstance(session_id, str) and session_id:
            sessions.add(session_id)

        if record_type == "visual":
            reason = str(record.get("reason") or "unknown")
            reasons[reason] += 1
            # Counters are cumulative *within a session* and a session does not
            # publish its identity on these records, so the last one is reported
            # as the latest session's state rather than summed into a number
            # that would describe none of them.
            visual = {
                "reason": reason,
                "sharing": bool(record.get("sharing")),
                "source": record.get("source"),
                "frame_seq": record.get("frame_seq"),
                "frame_age_ms": record.get("frame_age_ms"),
                "content_age_ms": record.get("content_age_ms"),
                "dropped_stale": record.get("dropped_stale"),
                "dropped_unchanged": record.get("dropped_unchanged"),
            }
            continue

        if record_type == "notice":
            notices[str(record.get("code") or record.get("kind") or "notice")] += 1
            continue

        if record_type != "latency":
            continue

        stage = str(record.get("stage") or "unknown")
        phase = str(record.get("phase") or "local")
        duration = _duration(record)

        if duration is None:
            elapsed = record.get("elapsed_ms")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
                milestones[stage].append(float(elapsed))
                # The most recent detail wins: `pipeline_error` and `agent_state`
                # carry the thing that explains why the milestone is there.
                for key in ("state", "room", "profile", "error"):
                    if key in record:
                        milestone_detail[stage] = f"{key}={record[key]}"
            continue

        by_phase[phase].append(duration)
        by_stage[stage].append(duration)

        if _is_turn_end(record):
            turns["total"] += 1
            turn_all.append(duration)
            if record.get("interrupted"):
                turns["interrupted"] += 1
                turn_interrupted.append(duration)
            else:
                turns["completed"] += 1
                turn_completed.append(duration)

        if stage == "tool_exec":
            tool = str(record.get("tool") or "unknown")
            by_tool[tool].append(duration)

        if stage == "permission_wait":
            decisions[str(record.get("decision") or "unknown")] += 1
            channels[str(record.get("channel") or "unknown")] += 1
            tool = str(record.get("tool") or "unknown")
            tools_asked[tool] += 1

    return {
        "sessions": len(sessions),
        "records": len(records),
        "types": dict(types.most_common()),
        "turns": turns,
        "turn_end_to_end": _group(
            {
                "all": turn_all,
                "completed": turn_completed,
                "interrupted": turn_interrupted,
            }
        ),
        "phases": _phase_table(by_phase),
        "stages": _table(by_stage),
        "tools": _table(by_tool),
        "permissions": {
            "decisions": dict(decisions.most_common()),
            "channels": dict(channels.most_common()),
            "by_tool": dict(tools_asked.most_common()),
            "wait": stats(by_phase["permission"]) if by_phase.get("permission") else None,
        },
        "milestones": _milestones(milestones, milestone_detail),
        "visual": {"reasons": dict(reasons.most_common()), "latest": visual or None},
        "notices": dict(notices.most_common()),
    }


def _group(groups: dict[str, list[float]]) -> dict[str, dict[str, float | int] | None]:
    return {name: (stats(values) if values else None) for name, values in groups.items()}


def _table(by_key: dict[str, list[float]]) -> list[dict[str, Any]]:
    """Per-key statistics, slowest p95 first.

    Sorted by p95 rather than by mean on purpose: a mean is dragged around by
    one outlier, while a p95 is *about* the tail, and the tail is where a voice
    conversation starts to feel broken.
    """
    rows = []
    for key, values in by_key.items():
        row = {"name": key, **stats(values)}
        rows.append(row)
    rows.sort(key=lambda row: (-float(row["p95"]), -int(row["count"]), str(row["name"])))
    return rows


def _phase_table(by_phase: dict[str, list[float]]) -> list[dict[str, Any]]:
    """Durations by phase, in the declared order, unknown phases after.

    Declared order matters more than it looks: two runs of this report are
    compared side by side, and a table that reorders itself between them makes
    a difference in *position* look like a difference in timing.
    """
    rows = {row["name"]: row for row in _table(by_phase)}
    ordered: list[dict[str, Any]] = [rows.pop(phase) for phase in PHASES if phase in rows]
    ordered.extend(rows[name] for name in sorted(rows))
    return ordered


def _milestones(milestones: dict[str, list[float]], detail: dict[str, str]) -> list[dict[str, Any]]:
    """Marks, reported as when they happened rather than how long they took.

    Only the *first* occurrence of each stage is kept: `agent_state` fires
    whenever the session changes state, and a later one is not a second session
    start. The earliest value is the one that says how long a fresh worker took
    to get there, which is the cold-start question these marks exist to answer.
    """
    rows = []
    for stage, values in milestones.items():
        first = min(values)
        row: dict[str, Any] = {"name": stage, "elapsed_ms": _round(first), "count": len(values)}
        if stage in detail:
            row["detail"] = detail[stage]
        rows.append(row)
    rows.sort(key=lambda row: float(row["elapsed_ms"]))
    return rows


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _ms(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.1f}"


def _format_row(cells: Sequence[Any], widths: Sequence[int], aligns: Sequence[str]) -> str:
    """One row, padded to the widths computed from the whole table."""
    return (
        "  "
        + "  ".join(
            f"{cell:>{width}}" if align == "r" else f"{cell:<{width}}"
            for cell, width, align in zip(cells, widths, aligns, strict=True)
        )
    ).rstrip()


def _table_block(headers: Sequence[str], rows: Sequence[Sequence[Any]], aligns: Sequence[str]) -> list[str]:
    """A header, a rule, and the rows -- one width computation, used throughout.

    Deriving the widths once from both header and data is what keeps the columns
    aligned whatever the numbers are; two passes over different inputs is how a
    table ends up one character out on the widest row.
    """
    widths = [
        max([len(str(header))] + [len(str(row[index])) for row in rows])
        for index, header in enumerate(headers)
    ]
    lines = [
        _format_row(headers, widths, aligns),
        "  " + "  ".join("-" * width for width in widths),
    ]
    lines.extend(_format_row(row, widths, aligns) for row in rows)
    return lines


def render(summary: dict[str, Any], counts: dict[str, int], *, top: int = DEFAULT_TOP) -> str:
    """The report as text. Data in, lines out; no I/O, so it is testable."""
    out: list[str] = []
    out.append("Lumine latency report")
    out.append(
        f"  {counts.get('records', 0)} records parsed from {counts.get('lines', 0)} line(s)"
        + (f", {counts['ignored']} ignored" if counts.get("ignored") else "")
    )
    out.append(f"  {summary['sessions']} session(s)")

    turns = summary["turns"]
    out.append("")
    out.append("Turn end to end -- user finished speaking, reply actually spoken")
    groups = summary["turn_end_to_end"]
    if not any(groups.values()):
        out.append("  no turns recorded")
    else:
        rows = []
        for label, key in (("all", "all"), ("completed", "completed"), ("interrupted", "interrupted")):
            group = groups.get(key)
            if not group:
                continue
            rows.append(
                [
                    label,
                    group["count"],
                    _ms(group["p50"]),
                    _ms(group["p95"]),
                    _ms(group["max"]),
                    _ms(group["mean"]),
                ]
            )
        out.extend(_table_block(["", "count", "p50", "p95", "max", "mean"], rows, ["l", "r", "r", "r", "r", "r"]))
        out.append("  (milliseconds)")

    if summary["phases"]:
        out.append("")
        out.append("Durations by phase -- model time is never tool time")
        rows = [
            [row["name"], row["count"], _ms(row["p50"]), _ms(row["p95"]), _ms(row["max"]), _ms(row["mean"])]
            for row in summary["phases"]
        ]
        out.extend(_table_block(["phase", "count", "p50", "p95", "max", "mean"], rows, ["l", "r", "r", "r", "r", "r"]))

    if summary["stages"]:
        out.append("")
        out.append(f"Slowest stages by p95 (top {top} of {len(summary['stages'])})")
        rows = [
            [row["name"], row["count"], _ms(row["p50"]), _ms(row["p95"]), _ms(row["max"])]
            for row in summary["stages"][:top]
        ]
        out.extend(_table_block(["stage", "count", "p50", "p95", "max"], rows, ["l", "r", "r", "r", "r"]))

    if summary["tools"]:
        out.append("")
        out.append("Tools")
        rows = [
            [row["name"], row["count"], _ms(row["p50"]), _ms(row["p95"]), _ms(row["max"])]
            for row in summary["tools"][:top]
        ]
        out.extend(_table_block(["tool", "count", "p50", "p95", "max"], rows, ["l", "r", "r", "r", "r"]))

    permissions = summary["permissions"]
    if permissions["decisions"] or permissions["wait"]:
        out.append("")
        out.append("Permissions -- a wait the user sits through")
        if permissions["wait"]:
            wait = permissions["wait"]
            out.append(
                f"  wait: count {wait['count']}  p50 {_ms(wait['p50'])}  "
                f"p95 {_ms(wait['p95'])}  max {_ms(wait['max'])} ms"
            )
        if permissions["decisions"]:
            out.append("  decisions: " + ", ".join(f"{k} {v}" for k, v in permissions["decisions"].items()))
        if permissions["channels"]:
            out.append("  answered on: " + ", ".join(f"{k} {v}" for k, v in permissions["channels"].items()))
        if permissions["by_tool"]:
            out.append("  asked for: " + ", ".join(f"{k} {v}" for k, v in permissions["by_tool"].items()))

    if summary["milestones"]:
        out.append("")
        out.append("Session milestones -- elapsed since the worker started (first occurrence)")
        rows = [
            [row["name"], _ms(row["elapsed_ms"]), row["count"], row.get("detail", "")]
            for row in summary["milestones"][:top]
        ]
        out.extend(_table_block(["mark", "elapsed", "count", "detail"], rows, ["l", "r", "r", "l"]))

    visual = summary["visual"]
    if visual["reasons"] or visual["latest"]:
        out.append("")
        out.append("Visual freshness -- latest session")
        latest = visual["latest"] or {}
        if latest:
            out.append(
                f"  sharing={latest.get('sharing')} source={latest.get('source')} "
                f"frames={latest.get('frame_seq')} "
                f"frame_age={_ms(latest.get('frame_age_ms'))}ms "
                f"content_age={_ms(latest.get('content_age_ms'))}ms "
                f"dropped_stale={latest.get('dropped_stale')} "
                f"dropped_unchanged={latest.get('dropped_unchanged')}"
            )
        if visual["reasons"]:
            out.append("  records: " + ", ".join(f"{k} {v}" for k, v in visual["reasons"].items()))

    other = {name: count for name, count in summary["types"].items() if name != "latency"}
    if other:
        out.append("")
        out.append("Other records: " + ", ".join(f"{name} {count}" for name, count in other.items()))

    if summary["notices"]:
        out.append("Notices: " + ", ".join(f"{name} {count}" for name, count in summary["notices"].items()))

    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perf_report.py",
        description="Aggregate captured LUMINE_EVENT records into a latency report.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["-"],
        help="captured log files, or '-' for stdin (default: stdin)",
    )
    parser.add_argument("--json", action="store_true", help="print the aggregated data as JSON")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"rows per table (default {DEFAULT_TOP})")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records, counts = gather(list(args.paths) or ["-"])
    if not records:
        sys.stderr.write(
            "perf_report: no LUMINE_EVENT records found "
            f"({counts['lines']} line(s) read, {counts['ignored']} ignored)\n"
        )
        return 1
    summary = summarize(records)
    if args.json:
        sys.stdout.write(json.dumps({"source": counts, **summary}, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(render(summary, counts, top=max(1, args.top)) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
