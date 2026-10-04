"""The report that turns a captured event stream into a number somebody can act on.

Three properties are worth pinning because each one is a way this could quietly
report something untrue:

* **A mark is not a duration.** `elapsed_ms` without `duration_ms` means "how
  far into the session", and folding it into a mean would produce a figure that
  describes no interval anyone waited for.
* **Percentiles are nearest-rank.** With two turns, an interpolated p95 invents
  a value no user ever sat through; the answer must be a measurement that
  happened.
* **Visual counters are per session.** They are cumulative within a session and
  carry no session id, so the last record is reported as the latest session
  rather than summed across sessions into a number describing none of them.

The report is offline by construction -- it reads a file and prints text -- which
is what allows these tests to run with no network, no worker and no credentials.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent.perf_report import (
    gather,
    main,
    parse_line,
    percentile,
    read_records,
    render,
    stats,
    summarize,
)


def latency(**fields) -> dict:
    record = {"type": "latency", "phase": "local", **fields}
    return record


class ParseTests(unittest.TestCase):
    def test_a_prefixed_event_line_is_read(self):
        record = parse_line('LUMINE_EVENT {"type":"latency","stage":"entrypoint","elapsed_ms":12}')
        self.assertEqual(record["type"], "latency")
        self.assertEqual(record["elapsed_ms"], 12)

    def test_a_bare_json_line_is_read_too(self):
        # The capture may come from a sink that already stripped the prefix.
        self.assertEqual(parse_line('{"type":"notice"}'), {"type": "notice"})

    def test_ordinary_log_lines_are_ignored_rather_than_raised_on(self):
        # A capture is interleaved with the worker's logging. A report that
        # refuses to run because line 402 was a traceback is unused.
        for line in ("", "   ", "INFO [lumine] started", "ERROR: boom"):
            with self.subTest(line=line):
                self.assertIsNone(parse_line(line))

    def test_malformed_json_is_ignored(self):
        self.assertIsNone(parse_line("{not json"))
        self.assertIsNone(parse_line("{} trailing"))

    def test_valid_json_that_is_not_an_object_is_ignored(self):
        self.assertIsNone(parse_line("[1, 2, 3]"))
        self.assertIsNone(parse_line('"a string"'))
        self.assertIsNone(parse_line("42"))

    def test_counts_distinguish_records_from_noise(self):
        records, counts = read_records(
            [
                "LUMINE_EVENT " + json.dumps({"type": "latency", "stage": "a"}),
                "a log line",
                json.dumps({"type": "visual", "reason": "frame"}),
                "",
            ]
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(counts, {"lines": 4, "records": 2, "ignored": 2})


class StatisticsTests(unittest.TestCase):
    def test_percentiles_are_nearest_rank_not_interpolated(self):
        # Interpolation would answer with a duration nobody waited for.
        sample = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertEqual(percentile(sample, 0.50), 30.0)
        self.assertEqual(percentile(sample, 0.95), 50.0)
        self.assertEqual(percentile(sample, 0.0), 10.0)
        self.assertEqual(percentile([7.0], 0.95), 7.0)

    def test_percentile_refuses_an_empty_sample(self):
        with self.assertRaises(ValueError):
            percentile([], 0.5)

    def test_stats_reports_the_columns_every_table_uses(self):
        summary = stats([100.0, 200.0, 300.0])
        self.assertEqual(set(summary), {"count", "min", "p50", "p95", "max", "mean"})
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["min"], 100.0)
        self.assertEqual(summary["max"], 300.0)
        self.assertEqual(summary["p50"], 200.0)
        self.assertEqual(summary["mean"], 200.0)


class SummarizeTests(unittest.TestCase):
    def test_a_mark_never_becomes_a_duration(self):
        # `elapsed_ms` alone is a position on a timeline. Averaging it with
        # durations would describe an interval nobody waited for.
        records = [
            latency(stage="entrypoint", elapsed_ms=12),
            latency(stage="greeting_requested", elapsed_ms=430),
            latency(stage="tool_exec", elapsed_ms=500, duration_ms=250.0, phase="tool", tool="open_app"),
        ]
        summary = summarize(records)

        self.assertNotIn("entrypoint", {row["name"] for row in summary["stages"]})
        self.assertNotIn("entrypoint", {row["name"] for row in summary["phases"]})
        self.assertEqual(
            [row["name"] for row in summary["milestones"]],
            ["entrypoint", "greeting_requested"],
            "milestones must come out in the order they happened",
        )
        self.assertEqual(summary["milestones"][0]["elapsed_ms"], 12.0)

    def test_a_milestone_keeps_only_its_first_occurrence(self):
        # `agent_state` fires whenever the session changes state; the earliest
        # one is what says how long a fresh worker took to get there, which is
        # the cold-start question the marks exist to answer.
        records = [
            latency(stage="agent_state", elapsed_ms=900, state="listening"),
            latency(stage="agent_state", elapsed_ms=4200, state="speaking"),
        ]
        summary = summarize(records)
        (row,) = summary["milestones"]
        self.assertEqual(row["elapsed_ms"], 900.0)
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["detail"], "state=speaking", "the latest detail explains the stage")

    def test_turns_are_split_by_what_the_user_experienced(self):
        records = [
            latency(stage="turn_end_to_end", duration_ms=1000.0, phase="e2e", interrupted=False),
            latency(stage="turn_end_to_end", duration_ms=2000.0, phase="e2e", interrupted=False),
            latency(stage="turn_end_to_end", duration_ms=3000.0, phase="e2e", interrupted=True),
        ]
        summary = summarize(records)

        self.assertEqual(summary["turns"], {"total": 3, "completed": 2, "interrupted": 1})
        self.assertEqual(summary["turn_end_to_end"]["all"]["count"], 3)
        self.assertEqual(summary["turn_end_to_end"]["completed"]["max"], 2000.0)
        self.assertEqual(summary["turn_end_to_end"]["interrupted"]["max"], 3000.0)

    def test_a_turn_that_was_never_closed_is_not_invented(self):
        summary = summarize([latency(stage="tool_exec", duration_ms=10.0, phase="tool")])
        self.assertEqual(summary["turns"]["total"], 0)
        self.assertIsNone(summary["turn_end_to_end"]["all"])

    def test_phases_follow_the_declared_order_with_unknown_ones_after(self):
        # Two runs are compared side by side, so a table that reorders itself
        # makes a change in position look like a change in timing.
        records = [
            latency(stage="a", duration_ms=10.0, phase="e2e"),
            latency(stage="b", duration_ms=20.0, phase="model"),
            latency(stage="c", duration_ms=30.0, phase="banana"),  # not declared
            latency(stage="d", duration_ms=40.0, phase="tool"),
        ]
        names = [row["name"] for row in summarize(records)["phases"]]
        self.assertEqual(names, ["model", "tool", "e2e", "banana"])

    def test_stages_are_ordered_by_the_tail_not_by_the_mean(self):
        # One 5000ms outlier drags a mean; a p95 is *about* the tail, and the
        # tail is where a voice conversation starts to feel broken.
        records = [
            latency(stage="patient", duration_ms=10.0, phase="model"),
            latency(stage="patient", duration_ms=12.0, phase="model"),
            latency(stage="patient", duration_ms=14.0, phase="model"),
            latency(stage="jittery", duration_ms=900.0, phase="tool"),
        ]
        rows = summarize(records)["stages"]
        self.assertEqual(rows[0]["name"], "jittery")

    def test_tools_group_by_the_tool_that_was_slow(self):
        records = [
            latency(stage="tool_exec", duration_ms=100.0, phase="tool", tool="open_app"),
            latency(stage="tool_exec", duration_ms=300.0, phase="tool", tool="open_app"),
            latency(stage="tool_exec", duration_ms=900.0, phase="tool", tool="browse"),
        ]
        tools = {row["name"]: row for row in summarize(records)["tools"]}
        self.assertEqual(tools["open_app"]["count"], 2)
        # Nearest-rank, so the answer is one of the two waits rather than their
        # midpoint: with samples this few, an invented middle value would be the
        # only number in the report that nobody actually sat through.
        self.assertEqual(tools["open_app"]["p50"], 100.0)
        self.assertEqual(tools["browse"]["max"], 900.0)

    def test_a_tool_without_a_name_is_still_counted(self):
        records = [latency(stage="tool_exec", duration_ms=5.0, phase="tool")]
        self.assertEqual(summarize(records)["tools"][0]["name"], "unknown")

    def test_permissions_report_the_wait_and_how_it_was_answered(self):
        records = [
            latency(
                stage="permission_wait",
                duration_ms=4000.0,
                phase="permission",
                tool="browse",
                decision="approved",
                channel="ui",
            ),
            latency(
                stage="permission_wait",
                duration_ms=500.0,
                phase="permission",
                tool="send_whatsapp",
                decision="denied",
                channel="voice",
            ),
        ]
        permissions = summarize(records)["permissions"]

        self.assertEqual(permissions["decisions"], {"approved": 1, "denied": 1})
        self.assertEqual(permissions["channels"], {"ui": 1, "voice": 1})
        self.assertEqual(permissions["by_tool"], {"browse": 1, "send_whatsapp": 1})
        self.assertEqual(permissions["wait"]["count"], 2)
        self.assertEqual(permissions["wait"]["max"], 4000.0)

    def test_permissions_with_none_recorded_say_so_rather_than_zero(self):
        permissions = summarize([latency(stage="x", duration_ms=1.0, phase="model")])["permissions"]
        self.assertEqual(permissions["decisions"], {})
        self.assertIsNone(permissions["wait"])

    def test_visual_counters_are_read_as_the_latest_session_not_summed(self):
        # Cumulative within a session, and these records carry no session id:
        # summing two sessions would produce a figure describing neither.
        records = [
            {"type": "visual", "reason": "started", "sharing": True, "source": "camera", "frame_seq": 0},
            {"type": "visual", "reason": "frame", "sharing": True, "source": "camera", "frame_seq": 4,
             "dropped_stale": 1},
            {"type": "visual", "reason": "stopped", "sharing": False, "source": None, "frame_seq": 9,
             "dropped_stale": 3},
        ]
        visual = summarize(records)["visual"]

        self.assertEqual(visual["reasons"], {"started": 1, "frame": 1, "stopped": 1})
        self.assertEqual(visual["latest"]["frame_seq"], 9, "the last record wins")
        self.assertEqual(visual["latest"]["dropped_stale"], 3)
        self.assertFalse(visual["latest"]["sharing"])

    def test_sessions_records_and_notices_are_counted(self):
        records = [
            latency(session_id="one", stage="a", duration_ms=1.0),
            latency(session_id="one", stage="b", duration_ms=2.0),
            latency(session_id="two", stage="c", duration_ms=3.0),
            {"type": "notice", "code": "rate_limited"},
        ]
        summary = summarize(records)
        self.assertEqual(summary["sessions"], 2)
        self.assertEqual(summary["types"], {"latency": 3, "notice": 1})
        self.assertEqual(summary["notices"], {"rate_limited": 1})

    def test_a_boolean_duration_is_not_treated_as_a_measurement(self):
        # `True` is an instance of `int`; accepting it would report a 1ms wait
        # that never happened.
        summary = summarize([latency(stage="odd", elapsed_ms=5, duration_ms=True)])
        self.assertEqual(summary["milestones"][0]["name"], "odd")
        self.assertEqual(summary["stages"], [])

    def test_a_mark_without_any_usable_numbers_is_skipped(self):
        summary = summarize([latency(stage="no_numbers")])
        self.assertEqual(summary["milestones"], [])


class RenderTests(unittest.TestCase):
    RECORDS = [
        latency(session_id="s", stage="entrypoint", elapsed_ms=12),
        latency(session_id="s", stage="turn_end_to_end", duration_ms=1240.0, phase="e2e", interrupted=False),
        latency(session_id="s", stage="turn_end_to_end", duration_ms=2100.0, phase="e2e", interrupted=True),
        latency(session_id="s", stage="tool_exec", duration_ms=310.0, phase="tool", tool="open_app"),
        latency(
            session_id="s",
            stage="permission_wait",
            duration_ms=4000.0,
            phase="permission",
            tool="browse",
            decision="approved",
            channel="ui",
        ),
    ]

    def test_the_headline_is_the_users_own_wait(self):
        text = render(summarize(self.RECORDS), {"lines": 5, "records": 5, "ignored": 0})
        self.assertIn("Turn end to end", text)
        self.assertIn("1,240.0", text)
        self.assertIn("interrupted", text)

    def test_the_permission_wait_is_visible_not_hidden_in_a_phase(self):
        text = render(summarize(self.RECORDS), {"lines": 5, "records": 5, "ignored": 0})
        self.assertIn("Permissions", text)
        self.assertIn("4,000.0", text)
        self.assertIn("approved", text)

    def test_a_capture_with_no_turns_says_so(self):
        text = render(
            summarize([latency(stage="entrypoint", elapsed_ms=3)]),
            {"lines": 1, "records": 1, "ignored": 0},
        )
        self.assertIn("no turns recorded", text)

    def test_the_top_limit_is_honoured(self):
        records = [latency(stage=f"stage_{index}", duration_ms=float(index), phase="tool") for index in range(20)]
        summary = summarize(records)
        full = render(summary, {"lines": 20, "records": 20, "ignored": 0}, top=3)
        self.assertEqual(sum(1 for line in full.splitlines() if line.strip().startswith("stage_")), 3)

    def test_every_line_is_free_of_trailing_whitespace(self):
        # Cosmetic, but a report pasted into an issue with ragged edges reads as
        # though the tool itself is approximate.
        text = render(summarize(self.RECORDS), {"lines": 5, "records": 5, "ignored": 0})
        for line in text.splitlines():
            self.assertEqual(line, line.rstrip())


class MainTests(unittest.TestCase):
    LOG = "\n".join(
        [
            "INFO [lumine] a line the report must skip",
            'LUMINE_EVENT {"type":"latency","stage":"entrypoint","elapsed_ms":12,"phase":"local"}',
            'LUMINE_EVENT {"type":"latency","stage":"turn_end_to_end","duration_ms":1500.0,'
            '"phase":"e2e","interrupted":false}',
            "{malformed",
        ]
    )

    def _write(self, directory: str, name: str, body: str) -> str:
        path = Path(directory) / name
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_it_reads_a_capture_and_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "worker.log", self.LOG)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main([path])

        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("Lumine latency report", out.getvalue())
        self.assertIn("1,500.0", out.getvalue())
        self.assertIn("4 line(s)", out.getvalue())

    def test_json_output_is_the_same_data_as_the_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "worker.log", self.LOG)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([path, "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["source"]["records"], 2)
        self.assertEqual(payload["turns"]["total"], 1)
        self.assertEqual(payload["phases"][0]["name"], "e2e")

    def test_no_usable_records_is_a_failure_with_a_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "worker.log", "just a log\nand another\n")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main([path])

        self.assertEqual(code, 1, "a capture with nothing in it must not read as success")
        self.assertIn("no LUMINE_EVENT records", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_several_captures_are_reported_together(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self._write(directory, "a.log", 'LUMINE_EVENT {"type":"latency","stage":"x","duration_ms":10,"phase":"tool"}\n')
            second = self._write(directory, "b.log", 'LUMINE_EVENT {"type":"latency","stage":"x","duration_ms":30,"phase":"tool"}\n')
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([first, second])

        self.assertEqual(code, 0)
        self.assertIn("2 records parsed", out.getvalue())
        self.assertIn("20.0", out.getvalue(), "the mean across both captures")

    def test_gather_reports_what_it_could_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "mixed.log", self.LOG)
            records, counts = gather([path])
        self.assertEqual(len(records), 2)
        self.assertEqual(counts["lines"], 4)
        self.assertEqual(counts["ignored"], 2)

    def test_the_text_report_never_reaches_for_the_network(self):
        # It reads a file and prints text. Checking the source rather than the
        # namespace, because a network library imported anywhere in the module
        # -- even inside a function -- is the thing that must not be there.
        import agent.perf_report as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("urllib", "requests", "http.client", "socket", "httpx", "aiohttp"):
            with self.subTest(module=forbidden):
                self.assertNotRegex(
                    source,
                    rf"^\s*(import {forbidden}|from {forbidden} )",
                    f"{forbidden} would make an offline report able to phone home",
                )


if __name__ == "__main__":
    unittest.main()
