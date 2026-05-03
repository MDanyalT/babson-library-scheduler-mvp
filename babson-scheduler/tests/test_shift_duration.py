"""
Tests for shift duration calculations.

Requirements verified:
  - Same-day:      07:30–09:30 = 2.0 h
                   09:00–12:00 = 3.0 h
  - Cross-midnight 23:00–02:00 = 3.0 h
                   23:30–01:30 = 2.0 h
                   22:00–00:00 = 2.0 h
"""
import pytest
from datetime import date
from sqlalchemy import text

from app.services.shift_generator import (
    _compute_duration,
    generate_from_operating_hours,
    generate_from_templates,
    seed_default_templates,
)


# ---------------------------------------------------------------------------
# Unit tests for _compute_duration (the core arithmetic)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("start,end,expected", [
    # Same-day
    ("07:30", "09:30", 2.0),
    ("09:00", "12:00", 3.0),
    ("10:00", "14:00", 4.0),
    ("08:00", "08:30", 0.5),
    # Cross-midnight
    ("23:00", "02:00", 3.0),
    ("23:30", "01:30", 2.0),
    ("22:00", "00:00", 2.0),
    ("23:00", "01:00", 2.0),
    ("22:30", "00:30", 2.0),
    # Edge — exactly midnight as end
    ("22:00", "00:00", 2.0),
])
def test_compute_duration(start, end, expected):
    assert _compute_duration(start, end) == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# Integration: generated instances must have correct duration_hours
# ---------------------------------------------------------------------------

WEEK = date(2026, 6, 2)   # A fresh Monday not used by other test suites


def test_generate_from_operating_hours_duration(conn):
    """All instances from operating-hours generation have correct durations."""
    instances = generate_from_operating_hours(conn, WEEK, force=True)
    assert len(instances) > 0
    for inst in instances:
        expected = _compute_duration(inst["start_time"], inst["end_time"])
        assert inst["duration_hours"] == pytest.approx(expected, abs=1e-4), (
            f"Wrong duration for {inst['start_time']}–{inst['end_time']}: "
            f"got {inst['duration_hours']}, expected {expected}"
        )


def test_generate_from_templates_duration(conn):
    """All instances from template-based generation have correct durations."""
    seed_default_templates(conn)   # no-op if already seeded
    instances = generate_from_templates(conn, WEEK, force=True)
    assert len(instances) > 0
    for inst in instances:
        expected = _compute_duration(inst["start_time"], inst["end_time"])
        assert inst["duration_hours"] == pytest.approx(expected, abs=1e-4), (
            f"Wrong duration for {inst['start_time']}–{inst['end_time']}: "
            f"got {inst['duration_hours']}, expected {expected}"
        )


def test_no_duration_exceeds_shift_block(conn):
    """No single instance should exceed the maximum shift block (6 h guard)."""
    instances = generate_from_operating_hours(conn, WEEK, force=True)
    for inst in instances:
        assert inst["duration_hours"] <= 6.0, (
            f"Duration {inst['duration_hours']} h exceeds 6 h for "
            f"{inst['start_time']}–{inst['end_time']}"
        )


def test_specific_windows_duration(conn):
    """Spot-check the exact windows from the bug report."""
    instances = generate_from_operating_hours(conn, WEEK, slots_per_window=1, force=True)
    by_window = {
        (inst["start_time"], inst["end_time"]): inst["duration_hours"]
        for inst in instances
    }

    # Not every library schedule has exactly these windows, but the arithmetic
    # must be right for any window that does appear.
    checks = [
        ("07:30", "09:30", 2.0),
        ("09:00", "12:00", 3.0),
        ("23:00", "02:00", 3.0),
        ("23:30", "01:30", 2.0),
    ]
    for start, end, expected in checks:
        if (start, end) in by_window:
            assert by_window[(start, end)] == pytest.approx(expected, abs=1e-4), (
                f"Duration for {start}–{end}: got {by_window[(start, end)]}, expected {expected}"
            )
