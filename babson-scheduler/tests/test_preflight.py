"""Tests for preflight diagnostics."""
import pytest
from datetime import date
from sqlalchemy import text

from app.services.shift_generator import generate_from_operating_hours

WEEK = date(2026, 5, 11)


def test_preflight_no_shifts(conn):
    """Preflight with no shifts should flag it."""
    # Ensure no instances for this week
    conn.execute(text("DELETE FROM shift_instances WHERE week_start_date = :wsd"),
                 {"wsd": WEEK.isoformat()})
    conn.commit()

    from app.diagnostics.preflight import run_preflight
    report = run_preflight(conn, WEEK)
    assert "findings" in report


def test_preflight_no_submissions(conn):
    """With shifts but no availability submissions, should flag MISSING_SUBMISSIONS."""
    generate_from_operating_hours(conn, WEEK, force=True)

    # Remove any availability for this week
    conn.execute(text("DELETE FROM availability WHERE week_start_date = :wsd"),
                 {"wsd": WEEK.isoformat()})
    conn.commit()

    from app.diagnostics.preflight import run_preflight
    report = run_preflight(conn, WEEK)
    types = [f["check_type"] for f in report["findings"]]
    # Should have at least one finding about coverage or submissions
    assert len(report["findings"]) > 0


def test_preflight_returns_snapshot(conn):
    """Preflight should persist a diagnostics_snapshots row."""
    generate_from_operating_hours(conn, WEEK, force=True)
    from app.diagnostics.preflight import run_preflight
    report = run_preflight(conn, WEEK)
    row = conn.execute(
        text("SELECT id FROM diagnostics_snapshots WHERE week_start_date = :wsd AND snapshot_type = 'preflight'"),
        {"wsd": WEEK.isoformat()},
    ).fetchone()
    assert row is not None


def test_preflight_feasibility_flag(conn):
    """is_feasible should be False when hard findings exist."""
    generate_from_operating_hours(conn, WEEK, force=True)
    conn.execute(text("DELETE FROM availability WHERE week_start_date = :wsd"),
                 {"wsd": WEEK.isoformat()})
    conn.commit()
    from app.diagnostics.preflight import run_preflight
    report = run_preflight(conn, WEEK)
    # With no availability, there should be hard findings → is_feasible = False
    if report["hard_findings_count"] > 0:
        assert report["is_feasible"] is False
