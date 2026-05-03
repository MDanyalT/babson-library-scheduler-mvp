"""
Tests for the no-overlap constraint in the CP-SAT solver and the
postflight OVERLAPPING_ASSIGNMENT hard-violation check.

Unit tests cover the overlap-detection logic directly by calling
_check_overlapping_assignments with crafted shift/assignment data.

Integration tests run a full solve and confirm the solver never
produces overlapping assignments.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.diagnostics.postflight import _check_overlapping_assignments
from app.models.db_models import ViolationType
from app.services.shift_generator import generate_from_operating_hours


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_shift(sh_id: str, dow: int, start: str, end: str,
                duration_hours: float | None = None) -> dict:
    if duration_hours is None:
        from app.utils.time_utils import effective_end_min, time_str_to_minutes
        s = time_str_to_minutes(start)
        e = effective_end_min(start, end)
        duration_hours = (e - s) / 60.0
    return {
        "id": sh_id,
        "day_of_week": dow,
        "start_time": start,
        "end_time": end,
        "duration_hours": duration_hours,
        "date": "2026-06-02",
        "label": f"{start}-{end}",
    }


def _make_student(sid: str = None, name: str = "Test") -> dict:
    return {"id": sid or str(uuid.uuid4()), "name": name}


def _make_asn(student_id: str, shift_id: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "student_id": student_id,
        "shift_instance_id": shift_id,
    }


# ---------------------------------------------------------------------------
# Unit tests: _check_overlapping_assignments
# ---------------------------------------------------------------------------

class TestCheckOverlappingAssignments:
    """Tests for the postflight overlap detector."""

    def _run(self, student, shift_ids, shifts):
        sid = student["id"]
        asns = [_make_asn(sid, sh_id) for sh_id in shift_ids]
        asns_by_student = {sid: asns}
        shift_by_id = {sh["id"]: sh for sh in shifts}
        return _check_overlapping_assignments([student], asns_by_student, shift_by_id)

    # --- 1. Same-day overlapping shifts ---
    def test_same_day_full_overlap(self):
        """07:30–09:30 and 07:30–09:00 share the entire first interval → overlap."""
        st = _make_student()
        sh_a = _make_shift("a", dow=0, start="07:30", end="09:30")
        sh_b = _make_shift("b", dow=0, start="07:30", end="09:00")
        viols = self._run(st, ["a", "b"], [sh_a, sh_b])
        assert len(viols) == 1
        assert viols[0].violation_type == ViolationType.OVERLAPPING_ASSIGNMENT
        assert viols[0].severity == "hard"
        assert viols[0].student_id == st["id"]

    # --- 2. Partially overlapping same-day shifts ---
    def test_partial_overlap_same_day(self):
        """09:00–12:00 and 09:30–11:30 partially overlap → flagged."""
        st = _make_student()
        sh_a = _make_shift("a", dow=1, start="09:00", end="12:00")
        sh_b = _make_shift("b", dow=1, start="09:30", end="11:30")
        viols = self._run(st, ["a", "b"], [sh_a, sh_b])
        assert len(viols) == 1

    # --- 3. Cross-midnight overlapping shifts ---
    def test_cross_midnight_overlap(self):
        """23:00–02:00 and 23:30–01:30 overlap → flagged."""
        st = _make_student()
        sh_a = _make_shift("a", dow=2, start="23:00", end="02:00")
        sh_b = _make_shift("b", dow=2, start="23:30", end="01:30")
        viols = self._run(st, ["a", "b"], [sh_a, sh_b])
        assert len(viols) == 1

    # --- 4. Adjacent shifts (touching at a single point) — NOT overlapping ---
    def test_adjacent_shifts_not_flagged(self):
        """09:00–12:00 followed by 12:00–14:00: touch at 12:00 exactly → allowed."""
        st = _make_student()
        sh_a = _make_shift("a", dow=3, start="09:00", end="12:00")
        sh_b = _make_shift("b", dow=3, start="12:00", end="14:00")
        viols = self._run(st, ["a", "b"], [sh_a, sh_b])
        assert viols == [], f"Adjacent shifts must NOT be flagged: {viols}"

    # --- 5. Duplicate slot (same start/end, same day) — overlap with itself ---
    def test_duplicate_slot_same_window(self):
        """Two slot_index instances of the same window (07:30–09:30) → overlap."""
        st = _make_student()
        sh_a = _make_shift("slot0", dow=0, start="07:30", end="09:30")
        sh_b = _make_shift("slot1", dow=0, start="07:30", end="09:30")
        viols = self._run(st, ["slot0", "slot1"], [sh_a, sh_b])
        assert len(viols) == 1

    # --- 6. Different days — no overlap ---
    def test_different_days_not_flagged(self):
        """Same clock times on different days of the week → never overlap."""
        st = _make_student()
        sh_a = _make_shift("a", dow=0, start="09:00", end="12:00")
        sh_b = _make_shift("b", dow=1, start="09:00", end="12:00")
        viols = self._run(st, ["a", "b"], [sh_a, sh_b])
        assert viols == []

    # --- 7. Non-overlapping same-day shifts with a gap ---
    def test_gap_between_shifts_not_flagged(self):
        """07:30–09:30 and 10:00–13:00 — gap of 30 min → not overlapping."""
        st = _make_student()
        sh_a = _make_shift("a", dow=4, start="07:30", end="09:30")
        sh_b = _make_shift("b", dow=4, start="10:00", end="13:00")
        viols = self._run(st, ["a", "b"], [sh_a, sh_b])
        assert viols == []

    # --- 8. Only one assigned shift — no pairs to check ---
    def test_single_shift_no_violation(self):
        st = _make_student()
        sh_a = _make_shift("a", dow=0, start="09:00", end="12:00")
        viols = self._run(st, ["a"], [sh_a])
        assert viols == []

    # --- 9. Each pair reported only once ---
    def test_no_duplicate_violations(self):
        """A single overlapping pair should produce exactly one violation."""
        st = _make_student()
        sh_a = _make_shift("a", dow=0, start="09:00", end="12:00")
        sh_b = _make_shift("b", dow=0, start="09:30", end="11:30")
        viols = self._run(st, ["a", "b"], [sh_a, sh_b])
        assert len(viols) == 1


# ---------------------------------------------------------------------------
# Integration: solver must not produce overlapping assignments
# ---------------------------------------------------------------------------

WEEK = date(2026, 6, 9)   # A fresh Monday not used by other suites


def test_solver_no_overlapping_assignments(client):
    """
    Generate shift instances, add students + availability, run the scheduler,
    and assert that no student has two overlapping assigned shifts.
    """
    from app.utils.time_utils import effective_end_min, time_str_to_minutes

    # Create students
    student_ids = []
    for i in range(4):
        resp = client.post("/api/v1/students", json={
            "name": f"NoOverlapStudent{i}",
            "email": f"noo{i}@test.edu",
            "seniority_date": "2023-09-01",
            "min_hours": 8,
            "max_hours": 20,
            "target_hours": 10,
        })
        assert resp.status_code == 200, resp.text
        student_ids.append(resp.json()["id"])

    wsd = str(WEEK)

    # Generate shifts
    resp = client.post("/api/v1/shifts/instances/generate", json={
        "week_start_date": wsd, "force": True
    })
    assert resp.status_code == 200, resp.text

    # List generated shift instances
    resp = client.get(f"/api/v1/shifts/instances?week_start_date={wsd}")
    assert resp.status_code == 200
    shift_instances = resp.json()
    assert len(shift_instances) > 0

    # Mark all students as available for the entire week (generic wide window)
    days_of_week = list(range(7))
    for sid in student_ids:
        for dow in days_of_week:
            client.post("/api/v1/availability", json={
                "student_id": sid,
                "week_start_date": wsd,
                "day_of_week": dow,
                "start_time": "07:00",
                "end_time": "02:00",
                "level": "preferred",
            })

    # Run the scheduler
    resp = client.post("/api/v1/schedules/generate", json={
        "week_start_date": wsd,
        "force_regenerate": True,
    })
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["id"]

    # Fetch assignments
    resp = client.get(f"/api/v1/schedules/{run_id}/assignments")
    assert resp.status_code == 200
    assignments = resp.json()

    # Build per-student assignment map
    by_student: dict[str, list[dict]] = {}
    for asn in assignments:
        sid = asn.get("student_id") or (asn.get("student") or {}).get("id")
        if not sid:
            continue
        by_student.setdefault(sid, []).append(asn)

    # Build shift lookup
    shift_map = {sh["id"]: sh for sh in shift_instances}

    def get_absolute_interval(asn):
        sh_id = asn.get("shift_instance_id") or (asn.get("shift") or {}).get("id")
        sh = shift_map.get(sh_id)
        if not sh:
            return None
        dow = sh["day_of_week"]
        base = dow * 1440
        abs_s = base + time_str_to_minutes(sh["start_time"])
        abs_e = base + effective_end_min(sh["start_time"], sh["end_time"])
        return abs_s, abs_e, sh

    # Assert no overlaps
    overlaps_found = []
    for sid, asns in by_student.items():
        intervals = [get_absolute_interval(a) for a in asns]
        intervals = [iv for iv in intervals if iv is not None]
        for i in range(len(intervals)):
            a_s, a_e, sh_a = intervals[i]
            for j in range(i + 1, len(intervals)):
                b_s, b_e, sh_b = intervals[j]
                if a_s < b_e and b_s < a_e:
                    overlaps_found.append(
                        f"Student {sid}: {sh_a['start_time']}–{sh_a['end_time']} (day {sh_a['day_of_week']}) "
                        f"overlaps {sh_b['start_time']}–{sh_b['end_time']} (day {sh_b['day_of_week']})"
                    )

    assert overlaps_found == [], (
        f"Solver produced {len(overlaps_found)} overlapping assignment(s):\n"
        + "\n".join(overlaps_found)
    )
