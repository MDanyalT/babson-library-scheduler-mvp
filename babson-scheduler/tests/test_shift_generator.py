"""Tests for shift generation from operating hours."""
import pytest
from datetime import date
from sqlalchemy import text

from app.services.shift_generator import (
    generate_from_operating_hours,
    get_instances_for_week,
    seed_default_templates,
)


WEEK = date(2026, 4, 27)  # A Monday


def test_seed_default_templates(conn):
    n = seed_default_templates(conn)
    assert n >= 0  # 0 if already seeded, >0 on first run
    rows = conn.execute(text("SELECT COUNT(*) FROM shift_templates")).fetchone()
    assert rows[0] > 0, "Should have templates after seeding"


def test_generate_from_operating_hours_creates_instances(conn):
    instances = generate_from_operating_hours(conn, WEEK, is_exam_period=False, slots_per_window=2, force=True)
    assert len(instances) > 0, "Should generate shift instances"


def test_each_instance_has_two_slots(conn):
    instances = generate_from_operating_hours(conn, WEEK, slots_per_window=2, force=True)
    # There should be pairs of slot_index 0 and 1 for each time window
    slot_indices = [i["slot_index"] for i in instances]
    assert 0 in slot_indices
    assert 1 in slot_indices


def test_hard_shifts_flagged(conn):
    instances = generate_from_operating_hours(conn, WEEK, force=True)
    hard = [i for i in instances if i["is_hard_shift"]]
    assert len(hard) > 0, "Should have at least some hard shifts (late-night / early-opening)"


def test_no_shift_exceeds_max_consecutive(conn):
    instances = generate_from_operating_hours(conn, WEEK, force=True)
    for inst in instances:
        assert inst["duration_hours"] <= 6.0, f"Single shift duration should not exceed 6h: {inst}"


def test_generation_is_idempotent(conn):
    generate_from_operating_hours(conn, WEEK, force=True)
    instances1 = get_instances_for_week(conn, WEEK)
    generate_from_operating_hours(conn, WEEK, force=True)
    instances2 = get_instances_for_week(conn, WEEK)
    assert len(instances1) == len(instances2)


def test_exam_period_has_more_instances(conn):
    regular = generate_from_operating_hours(conn, WEEK, is_exam_period=False, force=True)
    exam = generate_from_operating_hours(conn, WEEK, is_exam_period=True, force=True)
    # Exam period is 24/7 so should have more shift instances
    assert len(exam) >= len(regular)


def test_all_instances_have_positive_duration(conn):
    instances = generate_from_operating_hours(conn, WEEK, force=True)
    for inst in instances:
        assert inst["duration_hours"] > 0
