"""
Tests for the wide-format Excel availability matrix importer.

Unit tests cover the pure parsing helpers (parse_header, parse_time_ampm,
parse_hours_band).  Integration tests drive import_matrix against an
in-memory SQLite DB that already has shift instances generated for
week 2025-01-06 (a Monday).

Shift block size = 120 min (DEFAULT_SHIFT_BLOCK_MINUTES = 120).
Monday library hours = 07:30–02:00+1, so actual generated blocks include:
  07:30–09:30, 09:30–11:30, 11:30–13:30, 13:30–15:30, …
Tuesday hours are the same.
These exact times are used in HEADERS below so the availability lookup
can match real shift_instance rows.
"""

from __future__ import annotations

import io
import os
import tempfile
import uuid

import openpyxl
import pytest

from app.intake.excel_matrix import (
    HOURS_BANDS,
    _DEFAULT_HOURS,
    MatrixImportResult,
    import_matrix,
    parse_header,
    parse_hours_band,
    parse_time_ampm,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workbook(headers: list[str], rows: list[list]) -> str:
    """Write an in-memory workbook to a temp file, return the path."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as f:
        wb.save(f.name)
        return f.name


def _generate_shifts(conn, week_start_date: str = "2025-01-06"):
    """Generate shift instances for the given week using the standard generator."""
    from app.services.shift_generator import generate_from_templates
    generate_from_templates(conn, week_start_date, slots_per_window=1, force=True)
    conn.commit()


# Column headers matching 2-hour blocks generated for 2025-01-06
WEEK = "2025-01-06"
HEADERS = [
    "Seniority Level",
    "Name",
    "Number of hours you would ideally like to work per week",
    "Monday Availability.07:30 AM - 09:30 AM",
    "Monday Availability.09:30 AM - 11:30 AM",
    "Tuesday Availability.01:30 PM - 03:30 PM",
    "Additional Comments",
]

NAME_COL      = 1
SENIORITY_COL = 0
HOURS_COL     = 2
MON_0730_COL  = 3
MON_0930_COL  = 4
TUE_1330_COL  = 5


# ---------------------------------------------------------------------------
# parse_header — unit tests
# ---------------------------------------------------------------------------

class TestParseHeader:
    def test_basic_monday(self):
        result = parse_header("Monday Availability.07:30 AM - 09:30 AM")
        assert result == (0, "07:30", "09:30")

    def test_sunday(self):
        result = parse_header("Sunday Availability.10:00 AM - 12:00 PM")
        assert result == (6, "10:00", "12:00")

    def test_pm_afternoon(self):
        result = parse_header("Tuesday Availability.01:30 PM - 03:30 PM")
        assert result == (1, "13:30", "15:30")

    def test_case_insensitive(self):
        result = parse_header("MONDAY AVAILABILITY.07:30 AM - 09:30 AM")
        assert result == (0, "07:30", "09:30")

    def test_non_avail_column_returns_none(self):
        assert parse_header("Name") is None
        assert parse_header("Seniority Level") is None
        assert parse_header("Additional Comments") is None
        assert parse_header("") is None
        assert parse_header(None) is None  # type: ignore[arg-type]

    def test_late_night(self):
        # 11:00 PM → 23:00;  01:00 AM → 01:00
        result = parse_header("Monday Availability.11:00 PM - 01:00 AM")
        assert result == (0, "23:00", "01:00")

    def test_friday(self):
        result = parse_header("Friday Availability.07:30 AM - 09:30 AM")
        assert result == (4, "07:30", "09:30")

    def test_en_dash_separator(self):
        # en-dash (U+2013) should also be accepted
        result = parse_header("Wednesday Availability.09:30 AM – 11:30 AM")
        assert result == (2, "09:30", "11:30")


# ---------------------------------------------------------------------------
# parse_time_ampm — unit tests
# ---------------------------------------------------------------------------

class TestParseTimeAmpm:
    def test_morning(self):
        assert parse_time_ampm("7:30 AM") == "07:30"

    def test_afternoon(self):
        assert parse_time_ampm("1:30 PM") == "13:30"

    def test_midnight(self):
        # 12:00 AM = midnight = 00:00
        assert parse_time_ampm("12:00 AM") == "00:00"

    def test_noon(self):
        # 12:00 PM = noon = 12:00
        assert parse_time_ampm("12:00 PM") == "12:00"

    def test_single_digit_hour(self):
        assert parse_time_ampm("9:30 AM") == "09:30"

    def test_case_insensitive(self):
        assert parse_time_ampm("7:30 am") == "07:30"
        assert parse_time_ampm("1:30 pm") == "13:30"

    def test_eleven_pm(self):
        assert parse_time_ampm("11:00 PM") == "23:00"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_time_ampm("invalid")

    def test_no_meridiem_raises(self):
        with pytest.raises(ValueError):
            parse_time_ampm("07:30")  # no AM/PM


# ---------------------------------------------------------------------------
# parse_hours_band — unit tests
# ---------------------------------------------------------------------------

class TestParseHoursBand:
    def test_exact_8_10(self):
        result = parse_hours_band("8-10")
        assert result == {"min_hours": 8, "target_hours": 9, "max_hours": 20}

    def test_exact_10_12(self):
        result = parse_hours_band("10-12")
        assert result == {"min_hours": 8, "target_hours": 11, "max_hours": 20}

    def test_exact_12_14(self):
        result = parse_hours_band("12-14")
        assert result == {"min_hours": 8, "target_hours": 13, "max_hours": 20}

    def test_15_plus_values(self):
        result = parse_hours_band("15+")
        assert result == {"min_hours": 8, "target_hours": 15, "max_hours": 20}

    def test_with_trailing_text(self):
        result = parse_hours_band("8-10 hours per week")
        assert result == {"min_hours": 8, "target_hours": 9, "max_hours": 20}

    def test_unknown_defaults(self):
        result = parse_hours_band("30+")
        assert result == dict(_DEFAULT_HOURS)

    def test_none_defaults(self):
        result = parse_hours_band(None)
        assert result == dict(_DEFAULT_HOURS)

    def test_empty_string_defaults(self):
        result = parse_hours_band("")
        assert result == dict(_DEFAULT_HOURS)

    def test_returns_copy(self):
        # Mutating the returned dict should not change the module constant
        result = parse_hours_band("8-10")
        result["min_hours"] = 999
        assert HOURS_BANDS["8-10"]["min_hours"] == 8


# ---------------------------------------------------------------------------
# import_matrix — integration tests
# ---------------------------------------------------------------------------

class TestImportMatrix:
    """Integration tests that write to the in-memory test DB."""

    def test_import_creates_students(self, conn):
        _generate_shifts(conn)

        rows = [
            ["Sophomore", "Alice Smith", "8-10", "Preferred", "Available", "", ""],
            ["Junior",    "Bob Jones",   "10-12", "Available", "",          "", ""],
        ]
        path = _make_workbook(HEADERS, rows)
        try:
            result = import_matrix(path, WEEK, conn)
        finally:
            os.unlink(path)

        assert result.students_created == 2
        assert result.students_updated == 0
        assert result.students_processed == 2
        assert not result.errors

        # Verify students exist with correct hours
        alice = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT * FROM students WHERE LOWER(name)='alice smith'"
            )
        ).fetchone()
        assert alice is not None
        assert alice.min_hours == 8
        assert alice.target_hours == 9
        assert alice.max_hours == 20
        assert alice.seniority_date == "2023-09-01"  # sophomore

    def test_import_matches_shift_instances(self, conn):
        _generate_shifts(conn)

        rows = [
            ["Senior", "Carol White", "10-12", "Preferred", "Available", "Preferred", ""],
        ]
        path = _make_workbook(HEADERS, rows)
        try:
            result = import_matrix(path, WEEK, conn)
        finally:
            os.unlink(path)

        assert result.availability_slots_inserted == 3  # 3 "yes" columns
        assert not result.errors

        # Check Carol's availability rows are linked to shift instances
        from sqlalchemy import text
        carol = conn.execute(
            text("SELECT id FROM students WHERE LOWER(name)='carol white'")
        ).fetchone()
        assert carol is not None

        avail_rows = conn.execute(
            text(
                "SELECT shift_instance_id, level FROM availability "
                "WHERE student_id=:sid AND week_start_date=:wsd "
                "ORDER BY day_of_week, start_time"
            ),
            {"sid": carol[0], "wsd": WEEK},
        ).fetchall()
        assert len(avail_rows) == 3
        # All should have a shift_instance_id (not NULL) since headers match real instances
        for row in avail_rows:
            assert row.shift_instance_id is not None, (
                f"Expected shift_instance_id to be set but got None for row: {dict(row._mapping)}"
            )

    def test_import_warns_unmatched_windows(self, conn):
        # With auto_generate_shifts=False, a window with no pre-existing instance
        # should produce a warning and be recorded without a shift link.
        headers_with_bad = HEADERS + ["Wednesday Availability.08:00 AM - 09:00 AM"]
        rows = [
            ["Sophomore", "Dave Brown", "8-10", "Preferred", "", "", "", "Available"],
        ]
        path = _make_workbook(headers_with_bad, rows)
        try:
            result = import_matrix(path, WEEK, conn, auto_generate_shifts=False)
        finally:
            os.unlink(path)

        assert len(result.unmatched_windows) >= 1
        assert any("wednesday" in w.lower() for w in result.unmatched_windows)
        # Row still inserted (without shift link) so count includes it
        assert result.availability_slots_inserted >= 1
        assert not result.errors

    def test_import_multi_slot_window(self, conn):
        """When slots_per_window > 1, multiple instances share the same window."""
        from app.services.shift_generator import generate_from_templates

        # Generate with 2 slots per window
        generate_from_templates(conn, WEEK, slots_per_window=2, force=True)
        conn.commit()

        rows = [
            ["Junior", "Eve Davis", "8-10", "Available", "", "", ""],
        ]
        path = _make_workbook(HEADERS, rows)
        try:
            result = import_matrix(path, WEEK, conn)
        finally:
            os.unlink(path)

        # Monday 07:30–09:30 has 2 slot instances → 2 availability rows inserted
        assert result.availability_slots_inserted == 2
        assert not result.errors

    def test_import_summary_counts(self, conn):
        _generate_shifts(conn)

        rows = [
            ["Freshman",  "Frank Lee",  "8-10",  "Preferred", "Available",  "Preferred", "no comment"],
            ["Sophomore", "Grace Kim",  "10-12", "Available", "",           "",          ""],
            # Blank row — should be skipped
            ["",          "",           "",      "",          "",           "",          ""],
        ]
        path = _make_workbook(HEADERS, rows)
        try:
            result = import_matrix(path, WEEK, conn)
        finally:
            os.unlink(path)

        assert result.students_processed == 2  # blank row skipped
        assert result.students_created == 2
        assert result.students_updated == 0
        # Frank: 3 "yes" cells; Grace: 1
        assert result.availability_slots_inserted == 4
        assert not result.errors

    def test_import_replace_existing(self, conn):
        """Re-importing for the same student+week replaces old availability."""
        from sqlalchemy import text

        _generate_shifts(conn)

        # First import: Alice marks Monday 07:30 as Preferred
        rows1 = [["Sophomore", "Alice Replace", "8-10", "Preferred", "", "", ""]]
        path1 = _make_workbook(HEADERS, rows1)
        try:
            result1 = import_matrix(path1, WEEK, conn)
        finally:
            os.unlink(path1)

        assert result1.availability_slots_inserted == 1

        # Second import: same student, now marks Monday 09:30 as Available
        rows2 = [["Sophomore", "Alice Replace", "8-10", "", "Available", "", ""]]
        path2 = _make_workbook(HEADERS, rows2)
        try:
            result2 = import_matrix(path2, WEEK, conn)
        finally:
            os.unlink(path2)

        assert result2.students_updated == 1

        student = conn.execute(
            text("SELECT id FROM students WHERE LOWER(name)='alice replace'")
        ).fetchone()
        avail = conn.execute(
            text("SELECT level, start_time FROM availability WHERE student_id=:sid AND week_start_date=:wsd"),
            {"sid": student[0], "wsd": WEEK},
        ).fetchall()
        # Only one record — the old one was replaced
        assert len(avail) == 1
        assert avail[0].level == "available"
        assert avail[0].start_time == "09:30"

    def test_import_skips_blank_name_rows(self, conn):
        _generate_shifts(conn)

        rows = [
            ["",          "",          "",      "", "", "", ""],  # fully blank
            ["Freshman",  "",          "8-10",  "", "", "", ""],  # blank name
            ["Junior",    "Real Name", "10-12", "Preferred", "", "", ""],
        ]
        path = _make_workbook(HEADERS, rows)
        try:
            result = import_matrix(path, WEEK, conn)
        finally:
            os.unlink(path)

        assert result.students_processed == 1
        assert result.students_created == 1
