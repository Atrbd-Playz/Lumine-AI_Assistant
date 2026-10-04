"""Tests for the Focus Workspace read side.

## Why these tests exist rather than being left to the contract script

``scripts/verify-focus-contract.mts`` compares the Python and Rust numbers against
each other. That catches a *disagreement* but not a *wrong answer both share* -- if
both implementations filed a session under the wrong day, the comparison would pass.

So the cases that have a defensible right answer are pinned here in isolation, and
the fixtures are deliberately the same ones ``focus_stats.rs`` uses, at the same
fixed clock, so the two suites are checking each other rather than merely coexisting.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# No `sys.path` manipulation here, deliberately. Several tests in this directory
# insert `agent/` onto the path and then import `agent.*`, which works only by luck:
# with `agent/` at position zero, `import agent` finds `agent/agent.py` -- the
# worker's entrypoint, a plain module -- and "agent is not a package".
#
# `python -m unittest` from the repository root already puts the root on the path,
# so `agent` resolves as the namespace package it is and this import just works.

from agent.focus_stats import (  # noqa: E402
    CONTRACT,
    FOCUS_CONTRACT,
    STATS_VERSION,
    build_report,
    civil_from_days,
    days_from_civil,
    local_day_string,
    local_ms,
    main,
    summarise,
    today_index,
)
from agent.focus_store import (  # noqa: E402
    FOCUS_FILENAME,
    FOCUS_PATH_ENV,
    FOCUS_VERSION,
    FocusStoreError,
    focus_path,
    focus_path_candidates,
    load_focus,
    load_focus_or_empty,
)
from agent.tools.tool_results import UNTRUSTED_CLOSE, UNTRUSTED_OPEN  # noqa: E402

MS_PER_DAY = 86_400_000.0

# The same instant the Rust fixtures use, so "today" means the same thing in both
# suites. 2026-02-02T09:13:20Z.
T0 = 1_770_000_000_000.0


def session(days_ago: float, minutes: float, completed: bool, subject: str | None) -> dict:
    started = T0 - days_ago * MS_PER_DAY
    return {
        "id": f"s{days_ago}-{minutes}",
        "startedAt": started,
        "endedAt": started + minutes * 60_000.0,
        "plannedMin": 25,
        "actualMin": minutes,
        "phase": "work",
        "subject": subject,
        "todoId": None,
        "noteId": None,
        "completed": completed,
        "interruptions": 0,
    }


def document(rows: list[dict] | None = None) -> dict:
    return {
        "version": FOCUS_VERSION,
        "settings": {"dailyGoalMin": 120},
        "todos": [
            {"id": "t1", "done": True},
            {"id": "t2", "done": False},
            {"id": "t3", "done": False},
            {"id": "t4", "done": True},
        ],
        "notes": [{"id": "n1"}, {"id": "n2"}],
        "sessions": rows or [],
    }


class TempDocumentTests(unittest.TestCase):
    """Path resolution and safe loading, against a real temporary file."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="lumine-focus-test-"))
        self.path = self.dir / FOCUS_FILENAME
        self.previous = os.environ.get(FOCUS_PATH_ENV)
        os.environ[FOCUS_PATH_ENV] = str(self.path)

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop(FOCUS_PATH_ENV, None)
        else:
            os.environ[FOCUS_PATH_ENV] = self.previous
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    # -- resolution ---------------------------------------------------------

    def test_the_override_wins_and_is_the_only_candidate(self) -> None:
        self.assertEqual(focus_path_candidates(), [self.path])
        self.assertEqual(focus_path(), self.path)

    def test_a_blank_override_falls_through_to_the_search(self) -> None:
        os.environ[FOCUS_PATH_ENV] = "   "
        candidates = focus_path_candidates()
        self.assertGreater(len(candidates), 1, "a blank override must not pin the path")
        self.assertTrue(all(c.name == FOCUS_FILENAME for c in candidates))
        # No candidate is repeated: the desktop directory and the configuration
        # directory are usually the same place, and stat-ing it twice teaches nothing.
        self.assertEqual(len(candidates), len({str(c) for c in candidates}))

    def test_the_search_ends_at_the_agent_directory(self) -> None:
        """The last fallback has to be somewhere that exists for a developer.

        A worker started outside Tauri reads this file, and if the search stopped one
        step short the agent would report "no focus data" for a file sitting in the
        repository.
        """
        candidates = focus_path_candidates()
        self.assertTrue(str(candidates[-1]).endswith(FOCUS_FILENAME))

    def test_without_an_override_the_search_still_ends_somewhere_real(self) -> None:
        """The fallback search must resolve to *a* path even when nothing exists.

        Returning the primary candidate rather than `None` is deliberate: a log line
        or an error message then points somewhere a person can look, instead of at a
        candidate that was merely skipped.
        """
        os.environ.pop(FOCUS_PATH_ENV, None)
        resolved = focus_path()
        self.assertEqual(resolved.name, FOCUS_FILENAME)
        self.assertTrue(str(resolved).strip())

    def test_the_configured_override_is_honoured_over_the_search(self) -> None:
        self.write(json.dumps(document([session(0.0, 25.0, True, "Maths")])))
        self.assertEqual(focus_path(), self.path)
        self.assertEqual(len(load_focus(self.path)["sessions"]), 1)

    # -- loading ------------------------------------------------------------

    def test_a_missing_file_is_an_empty_document_and_not_an_error(self) -> None:
        self.assertEqual(load_focus(self.dir / "nope.json"), {})

    def test_an_empty_file_is_an_empty_document(self) -> None:
        self.path.write_text("   ", encoding="utf-8")
        self.assertEqual(load_focus(self.path), {})

    def test_a_valid_document_round_trips(self) -> None:
        self.write(json.dumps(document([session(0.0, 25.0, True, "Maths")])))
        loaded = load_focus(self.path)
        self.assertEqual(loaded["version"], FOCUS_VERSION)
        self.assertEqual(len(loaded["sessions"]), 1)

    def test_malformed_json_is_reported_and_the_file_is_left_alone(self) -> None:
        self.write("{not json")
        with self.assertRaises(FocusStoreError):
            load_focus(self.path)
        # The reader must not have touched it. A reader that "fixed" a corrupt file
        # would be destroying somebody's history.
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_a_non_object_root_is_refused(self) -> None:
        self.write("[]")
        with self.assertRaises(FocusStoreError):
            load_focus(self.path)

    def test_a_document_with_no_version_is_refused_rather_than_guessed(self) -> None:
        self.write(json.dumps({"settings": {}}))
        with self.assertRaises(FocusStoreError):
            load_focus(self.path)

    def test_a_newer_version_is_refused_because_this_build_cannot_know_what_it_drops(self) -> None:
        self.write(json.dumps({**document(), "version": FOCUS_VERSION + 1}))
        with self.assertRaises(FocusStoreError) as caught:
            load_focus(self.path)
        self.assertIn("newer version", str(caught.exception))

    def test_an_older_version_is_readable_without_being_rewritten(self) -> None:
        """A v0 document must not be refused.

        Rust migrates it on the next save. Refusing here would mean the agent reported
        "cannot read" for a file the app opens every day -- and this side has no
        legitimate reason to overwrite it even if it could.
        """
        self.write(json.dumps({"version": 0, "sessions": [session(0.0, 25.0, True, "Maths")]}))
        loaded = load_focus(self.path)
        self.assertEqual(loaded["version"], 0)
        self.assertEqual(len(loaded["sessions"]), 1)
        self.assertEqual(self.path.read_text(encoding="utf-8").count('"version": 0'), 1)

    def test_a_broken_document_can_be_treated_as_empty_for_a_caller_that_must_render(self) -> None:
        self.write("{not json")
        self.assertEqual(load_focus_or_empty(self.path), {})


class CivilDateTests(unittest.TestCase):
    def test_the_conversion_round_trips_across_leap_and_century_boundaries(self) -> None:
        for year, month, day in [
            (1970, 1, 1),
            (2000, 2, 29),
            (2024, 2, 29),
            (2026, 2, 2),
            (2100, 3, 1),
            (1900, 3, 1),
        ]:
            self.assertEqual(civil_from_days(days_from_civil(year, month, day)), (year, month, day))

    def test_a_malformed_date_is_refused_rather_than_read_as_the_epoch(self) -> None:
        self.assertEqual(today_index("2026-10-04"), days_from_civil(2026, 10, 4))
        self.assertEqual(today_index("not-a-date"), 0)
        self.assertEqual(today_index(""), 0)
        self.assertEqual(today_index("2026-13-01"), 0, "month 13 is not a month")


class TimezoneTests(unittest.TestCase):
    """The offset is the whole reason this module takes one."""

    def test_the_offset_shifts_the_day_boundary_east_positive(self) -> None:
        just_before = days_from_civil(2026, 2, 1) * MS_PER_DAY - 1
        just_after = days_from_civil(2026, 2, 1) * MS_PER_DAY

        self.assertEqual(local_day_string(local_ms(just_before, 0)), "2026-01-31")
        self.assertEqual(local_day_string(local_ms(just_after, 0)), "2026-02-01")
        # UTC+6: the last second of UTC Jan 31 is already Feb 1 locally.
        self.assertEqual(local_day_string(local_ms(just_before, 360)), "2026-02-01")
        # UTC-5: the same instant is still Jan 31.
        self.assertEqual(local_day_string(local_ms(just_after, -300)), "2026-01-31")

    def test_one_session_can_land_on_different_days_at_different_offsets(self) -> None:
        start = days_from_civil(2026, 1, 31) * MS_PER_DAY + 22 * 3_600_000
        row = dict(session(0.0, 25.0, True, "Maths"))
        row["startedAt"] = start
        row["endedAt"] = start + 25 * 60_000

        utc_view = summarise(document([row]), 0, 7, T0)
        shifted_view = summarise(document([row]), 360, 7, T0)

        def minutes_on(report: dict, date: str) -> float | None:
            for entry in report["byDate"]:
                if entry["date"] == date:
                    return entry["focusMinutes"]
            return None

        self.assertEqual(minutes_on(utc_view, "2026-01-31"), 25.0)
        self.assertEqual(minutes_on(shifted_view, "2026-01-31"), 0.0)
        self.assertEqual(minutes_on(shifted_view, "2026-02-01"), 25.0)
        # Both agree on the lifetime total: the disagreement is a labelling one, not
        # data loss, which is what makes it safe to correct by passing an offset.
        self.assertEqual(
            utc_view["totals"]["focusMinutes"], shifted_view["totals"]["focusMinutes"]
        )


class SummariseTests(unittest.TestCase):
    def totals(self, report: dict) -> dict:
        return report["totals"]

    def minutes_on(self, report: dict, date: str) -> float | None:
        for entry in report["byDate"]:
            if entry["date"] == date:
                return entry["focusMinutes"]
        return None

    def test_an_empty_document_answers_every_field_rather_than_returning_none(self) -> None:
        """Every contracted field exists, and is the type the contract declares.

        Driven by FOCUS_CONTRACT rather than a blanket `isinstance`, so a field
        declared nullable is *allowed* to be null and a field declared numeric is
        not. The alternative -- asserting everything is a number -- is what made
        `today.date`, a string, look like a bug when the test was the thing at fault.
        """
        report = summarise({}, 0, 7, T0)
        for key, declared in FOCUS_CONTRACT.items():
            head, _, tail = key.partition(".")
            value = report[head][tail] if tail else report[head]
            if "or null" in declared:
                self.assertIn(value, (None, int, float, str), key)
            elif declared.startswith("array of"):
                # Checked by shape rather than by type, because "it is a list" is the
                # least interesting thing about a series. The element keys are the
                # contract; the length is asserted separately per test.
                self.assertIsInstance(value, list, key)
                for element in value:
                    self.assertIsInstance(element, dict, key)
                    for field in ("date", "subject"):
                        if field in element:
                            break
                    self.assertTrue(
                        {"date", "subject"} & set(element),
                        f"{key} rows must carry a date or a subject",
                    )
                    for field in ("focusMinutes", "sessions"):
                        if field in element:
                            self.assertIsInstance(element[field], (int, float), f"{key}.{field}")
            elif "str" in declared or "YYYY" in declared:
                self.assertIsInstance(value, str, f"{key} (declared {declared})")
            else:
                # `generatedAt` is a float in both implementations: epoch millis
                # carried as f64, because the arithmetic on it is float arithmetic.
                self.assertIsInstance(value, (int, float), f"{key} (declared {declared})")
        self.assertEqual(self.totals(report)["averageSessionMinutes"], 0.0)
        self.assertEqual(self.totals(report)["dailyGoalMinutes"], 120.0)
        self.assertEqual(len(report["byDate"]), 7)
        self.assertIsNone(report["streak"]["lastActiveDate"])

    def test_completed_and_interrupted_minutes_are_reported_separately(self) -> None:
        report = summarise(
            document(
                [
                    session(0.0, 25.0, True, "Maths"),
                    session(0.0, 10.0, False, "Maths"),
                ]
            ),
            0,
            7,
            T0,
        )
        self.assertEqual(self.totals(report)["sessions"], 2)
        self.assertEqual(self.totals(report)["completedSessions"], 1)
        self.assertEqual(self.totals(report)["interruptedSessions"], 1)
        self.assertEqual(self.totals(report)["focusMinutes"], 35.0, "both count as time sat")
        self.assertEqual(self.totals(report)["completedFocusMinutes"], 25.0)

    def test_a_session_with_no_subject_is_counted_rather_than_dropped(self) -> None:
        report = summarise(
            document(
                [
                    session(0.0, 25.0, True, "Maths"),
                    session(0.0, 25.0, True, None),
                    session(0.0, 25.0, True, "   "),
                ]
            ),
            0,
            7,
            T0,
        )
        self.assertEqual(self.totals(report)["focusMinutes"], 75.0)
        self.assertEqual(self.totals(report)["unassignedMinutes"], 50.0)
        self.assertEqual(len(report["bySubject"]), 1, "a blank subject is not a subject")

    def test_goal_progress_is_todays_share_of_the_goal(self) -> None:
        report = summarise(document([session(0.0, 60.0, True, "Maths")]), 0, 7, T0)
        self.assertEqual(self.totals(report)["dailyGoalMinutes"], 120.0)
        self.assertEqual(self.totals(report)["todayMinutes"], 60.0)
        self.assertEqual(self.totals(report)["goalProgress"], 0.5)

        zero = document([session(0.0, 60.0, True, "Maths")])
        zero["settings"]["dailyGoalMin"] = 0
        self.assertEqual(
            self.totals(summarise(zero, 0, 7, T0))["goalProgress"],
            0.0,
            "a zero goal must be zero progress, not a division by zero",
        )

    def test_a_session_is_bucketed_by_the_day_it_started_not_the_day_it_ended(self) -> None:
        late = days_from_civil(2026, 2, 1) * MS_PER_DAY + 23 * 3_600_000
        row = session(0.0, 25.0, True, "Maths")
        row["startedAt"] = late
        row["endedAt"] = late + 25 * 60_000
        report = summarise(document([row]), 0, 7, T0)
        self.assertEqual(self.minutes_on(report, "2026-02-01"), 25.0)
        self.assertEqual(
            self.minutes_on(report, "2026-02-02"),
            0.0,
            "bucketing by endedAt would have moved it to tomorrow and shown today empty",
        )

    def test_the_series_carries_every_day_in_the_window_including_empty_ones(self) -> None:
        report = summarise(document([session(0.0, 25.0, True, "Maths")]), 0, 14, T0)
        self.assertEqual(len(report["byDate"]), 14)
        self.assertTrue(
            any(entry["focusMinutes"] == 0.0 for entry in report["byDate"]),
            "a bar chart with no position for a zero renders one day's minutes as the next",
        )
        indices = [today_index(entry["date"]) for entry in report["byDate"]]
        self.assertEqual(
            [b - a for a, b in zip(indices, indices[1:])],
            [1] * 13,
            "the series must be contiguous",
        )
        self.assertEqual(report["byDate"][-1]["date"], "2026-02-02", "it ends on today")

    def test_the_window_is_clamped_inside_the_aggregate_not_only_by_the_caller(self) -> None:
        self.assertEqual(summarise({}, 0, 100_000, T0)["windowDays"], 365)
        self.assertEqual(len(summarise({}, 0, 100_000, T0)["byDate"]), 365)
        self.assertEqual(summarise({}, 0, 0, T0)["windowDays"], 1)
        self.assertEqual(summarise({}, 0, -5, T0)["windowDays"], 1)

    def test_a_session_outside_the_window_is_in_the_totals_but_not_the_series(self) -> None:
        report = summarise(
            document(
                [
                    session(0.0, 25.0, True, "Maths"),
                    session(200.0, 50.0, True, "Maths"),
                ]
            ),
            0,
            7,
            T0,
        )
        self.assertEqual(self.totals(report)["focusMinutes"], 75.0, "it really happened")
        self.assertEqual(
            sum(entry["focusMinutes"] for entry in report["byDate"]),
            25.0,
            "but it does not belong in a seven-day chart",
        )
        self.assertEqual(len(report["bySubject"]), 1)
        self.assertEqual(
            report["bySubject"][0]["focusMinutes"],
            75.0,
            "the subject breakdown is deliberately not windowed",
        )

    def test_a_row_that_is_not_a_record_is_skipped_rather_than_fatal(self) -> None:
        rows = [session(0.0, 25.0, True, "Maths"), "not a record", 42, None]
        self.assertEqual(self.totals(summarise(document(rows), 0, 7, T0))["focusMinutes"], 25.0)

    def test_a_corrupt_timestamp_does_not_poison_the_day_it_lands_in(self) -> None:
        doc = {
            "version": FOCUS_VERSION,
            "settings": {"dailyGoalMin": 120},
            "sessions": [
                {"id": "neg", "startedAt": -1, "actualMin": 999.0, "completed": True},
                {"id": "text", "startedAt": "yesterday", "actualMin": 500.0, "completed": True},
                {"id": "nan", "startedAt": float("nan"), "actualMin": 800.0, "completed": True},
                {"id": "bool", "startedAt": True, "actualMin": 700.0, "completed": True},
                dict(session(0.0, 25.0, True, "Maths")),
            ],
        }
        report = summarise(doc, 0, 7, T0)
        self.assertEqual(
            self.totals(report)["focusMinutes"],
            25.0,
            "only the usable row may contribute; a `true` is not a timestamp in Python "
            "even though bool subclasses int",
        )

    def test_a_negative_actual_minutes_cannot_reduce_the_total(self) -> None:
        row = session(0.0, 25.0, True, "Maths")
        row["actualMin"] = -500.0
        report = summarise(document([row, session(0.0, 25.0, True, "Maths")]), 0, 7, T0)
        self.assertEqual(self.totals(report)["focusMinutes"], 25.0)

    def test_todo_completion_is_counts_and_a_ratio(self) -> None:
        report = summarise(document(), 0, 7, T0)
        self.assertEqual(report["todoCompletion"], {"total": 4, "done": 2, "ratio": 0.5})
        self.assertEqual(report["noteCount"], 2)
        empty = summarise({}, 0, 7, T0)
        self.assertEqual(empty["todoCompletion"]["ratio"], 0.0)

    def test_the_contract_declares_its_version_and_offset(self) -> None:
        report = summarise(document(), 330, 7, T0)
        self.assertEqual(report["version"], STATS_VERSION)
        self.assertEqual(report["contract"], CONTRACT)
        self.assertEqual(report["utcOffsetMinutes"], 330)
        self.assertEqual(report["generatedAt"], T0, "the injected clock must be reported back")

    def test_every_contract_field_is_actually_present(self) -> None:
        """The contract is a list of claims; this is what makes it true.

        A field documented but never emitted is worse than one missing, because the
        next implementation reads the docstring and waits for a key that never comes.
        """
        report = summarise(document([session(0.0, 25.0, True, "Maths")]), 0, 7, T0)
        for path in FOCUS_CONTRACT:
            head, _, tail = path.partition(".")
            self.assertIn(head, report, path)
            if tail:
                self.assertIn(tail, report[head], path)


class StreakTests(unittest.TestCase):
    def test_a_streak_survives_an_unstarted_today_but_ends_after_a_missed_day(self) -> None:
        alive = summarise(
            document(
                [
                    session(1.0, 25.0, True, "Maths"),
                    session(2.0, 25.0, True, "Maths"),
                ]
            ),
            0,
            7,
            T0,
        )
        self.assertEqual(alive["streak"]["current"], 2, "one grace day for today")

        lost = summarise(
            document(
                [
                    session(3.0, 25.0, True, "Maths"),
                    session(4.0, 25.0, True, "Maths"),
                ]
            ),
            0,
            7,
            T0,
        )
        self.assertEqual(lost["streak"]["current"], 0, "a full day missed ends it")
        self.assertEqual(lost["streak"]["longest"], 2, "but the record is kept")
        self.assertEqual(lost["streak"]["lastActiveDate"], "2026-01-30")

    def test_a_streak_counts_today_when_today_is_active(self) -> None:
        report = summarise(
            document(
                [
                    session(0.0, 25.0, True, "Maths"),
                    session(1.0, 25.0, True, "Maths"),
                    session(2.0, 25.0, True, "Maths"),
                ]
            ),
            0,
            7,
            T0,
        )
        self.assertEqual(report["streak"]["current"], 3)

    def test_a_gap_in_the_middle_does_not_join_two_runs(self) -> None:
        report = summarise(
            document(
                [
                    session(1.0, 25.0, True, "Maths"),
                    session(3.0, 25.0, True, "Maths"),
                ]
            ),
            0,
            7,
            T0,
        )
        self.assertEqual(report["streak"]["current"], 1, "the gap breaks it")
        self.assertEqual(report["streak"]["longest"], 1, "and neither run is two long")


class SubjectFilterTests(unittest.TestCase):
    def test_the_filter_is_applied_before_summarising_so_every_number_agrees(self) -> None:
        doc = document(
            [
                session(0.0, 25.0, True, "Maths"),
                session(0.0, 50.0, True, "Physics"),
            ]
        )
        report = build_report(doc, days=7, subject="maths", now=T0, utc_offset_minutes=0)
        self.assertEqual(report["totals"]["focusMinutes"], 25.0)
        self.assertEqual(report["totals"]["todayMinutes"], 25.0)
        self.assertEqual(report["totals"]["goalProgress"], round(25 / 120, 4))
        self.assertEqual(report["streak"]["current"], 1)
        self.assertEqual(report["subjectFilter"], "maths")

    def test_an_unknown_subject_reports_nothing_rather_than_everything(self) -> None:
        report = build_report(
            document([session(0.0, 25.0, True, "Maths")]),
            days=7,
            subject="Chemistry",
            now=T0,
            utc_offset_minutes=0,
        )
        self.assertEqual(report["totals"]["focusMinutes"], 0.0)
        self.assertEqual(report["bySubject"], [])


class CommandLineTests(unittest.TestCase):
    """The CLI surface, including its exit codes."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="lumine-focus-cli-"))
        self.path = self.dir / FOCUS_FILENAME
        self.previous = os.environ.get(FOCUS_PATH_ENV)
        os.environ[FOCUS_PATH_ENV] = str(self.path)

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop(FOCUS_PATH_ENV, None)
        else:
            os.environ[FOCUS_PATH_ENV] = self.previous
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def test_json_output_is_parseable_and_carries_the_contract(self) -> None:
        import io
        from contextlib import redirect_stdout

        self.path.write_text(json.dumps(document([session(0.0, 25.0, True, "Maths")])), "utf-8")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--json", "--days", "7"])
        self.assertEqual(code, 0)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["contract"], CONTRACT)
        self.assertEqual(report["totals"]["focusMinutes"], 25.0)

    def test_json_output_is_not_truncated_the_way_a_model_payload_is(self) -> None:
        """`--json` must be full fidelity.

        It is the verification script's input. Running it through `as_payload` would
        mean the contract check compared pruned output against full output and agreed
        for the wrong reason.
        """
        import io
        from contextlib import redirect_stdout

        rows = [session(float(index % 20), 25.0, True, f"Subject {index % 20}") for index in range(400)]
        self.path.write_text(json.dumps(document(rows)), "utf-8")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            main(["--json"])
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["totals"]["sessions"], 400)
        # 20 subjects across a 20-day spread, in a 7-day window: the subject
        # breakdown is lifetime, so all 20 are present even though the window is not.
        # This doubles as a check that nothing was pruned to fit the budget.
        self.assertEqual(len(report["bySubject"]), 20)
        self.assertEqual(len(report["byDate"]), 7)

    def test_the_model_facing_output_is_wrapped_as_untrusted(self) -> None:
        import io
        from contextlib import redirect_stdout

        self.path.write_text(json.dumps(document([session(0.0, 25.0, True, "Maths")])), "utf-8")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main(["--days", "7"]), 0)
        output = buffer.getvalue()
        self.assertIn(UNTRUSTED_OPEN, output)
        self.assertIn(UNTRUSTED_CLOSE, output)

    def test_a_broken_document_exits_one_rather_than_printing_zeroes(self) -> None:
        import io
        from contextlib import redirect_stderr

        self.path.write_text("{not json", "utf-8")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--json"]), 1)

    def test_an_impossible_window_exits_two(self) -> None:
        import io
        from contextlib import redirect_stderr

        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--days", "0"]), 2)
            self.assertEqual(main(["--days", "400"]), 2)

    def test_no_document_at_all_is_a_successful_empty_report(self) -> None:
        """A person who has never opened Focus has no file, and that is not a fault."""
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main(["--json"]), 0)
        self.assertEqual(json.loads(buffer.getvalue())["totals"]["sessions"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
