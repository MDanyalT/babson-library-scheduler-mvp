"""
Tests for the consecutive-hours hard constraint in the CP-SAT solver.

Unit tests build a minimal model by calling _add_consecutive_hours_constraints
directly and verify feasibility / infeasibility.

Integration test runs a full schedule and checks the postflight violations.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from ortools.sat.python import cp_model

from app.solver.builder import _add_consecutive_hours_constraints
from app.utils.time_utils import effective_end_min, time_str_to_minutes


# ---------------------------------------------------------------------------
# Helpers for building minimal CP-SAT models
# ---------------------------------------------------------------------------

def _make_shift(sh_id: str, dow: int, start: str, end: str) -> dict:
    s = time_str_to_minutes(start)
    e = effective_end_min(start, end)
    return {
        "id": sh_id,
        "day_of_week": dow,
        "start_time": start,
        "end_time": end,
        "duration_hours": (e - s) / 60.0,
        "date": f"2026-06-0{dow + 2}",
        "label": f"{start}-{end}",
    }


def _make_student(sid: str = None) -> dict:
    return {"id": sid or str(uuid.uuid4()), "name": "Test Student"}


def _solve(students, shifts, *, max_consec_hours=6.0, stagger=5,
           force_assignment: dict | None = None):
    """
    Build a minimal CP-SAT model where every student is eligible for every
    shift, apply the consecutive-hours constraint, optionally force certain
    assignments, and return the solution values dict or None if infeasible.

    force_assignment: {(student_id, shift_id): 1 | 0}
    """
    model = cp_model.CpModel()
    x = {}
    for st in students:
        for sh in shifts:
            x[(st["id"], sh["id"])] = model.NewBoolVar(f"x_{st['id']}_{sh['id']}")

    # Each shift must be covered by exactly one student (or we allow zero for testing)
    # For these unit tests, don't enforce coverage — just check consecutive constraint.

    _add_consecutive_hours_constraints(
        model, x, students, shifts,
        max_consec_min=int(max_consec_hours * 60),
        stagger=stagger,
    )

    if force_assignment:
        for (sid, shid), val in force_assignment.items():
            if (sid, shid) in x:
                model.Add(x[(sid, shid)] == val)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5.0
    status = solver.Solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {key: solver.Value(var) for key, var in x.items()}
    return None   # INFEASIBLE


# ---------------------------------------------------------------------------
# 1. Exactly 6 consecutive hours → allowed
# ---------------------------------------------------------------------------

def test_exactly_six_hours_allowed():
    """
    Three back-to-back 2-hour shifts = 6 h exactly → solver must find a
    solution where the student holds all three.
    """
    st = _make_student("s1")
    shifts = [
        _make_shift("a", 0, "13:30", "15:30"),
        _make_shift("b", 0, "15:30", "17:30"),
        _make_shift("c", 0, "17:30", "19:30"),
    ]
    sol = _solve([st], shifts, max_consec_hours=6.0,
                 force_assignment={("s1", "a"): 1, ("s1", "b"): 1, ("s1", "c"): 1})
    assert sol is not None, "6h consecutive should be feasible (limit is 6h)"
    assert sol[("s1", "a")] == 1
    assert sol[("s1", "b")] == 1
    assert sol[("s1", "c")] == 1


# ---------------------------------------------------------------------------
# 2. 8 consecutive hours → rejected
# ---------------------------------------------------------------------------

def test_eight_hours_consecutive_blocked():
    """
    Four back-to-back 2-hour shifts = 8 h → forcing all four on one student
    must be infeasible.
    """
    st = _make_student("s1")
    shifts = [
        _make_shift("a", 0, "13:30", "15:30"),
        _make_shift("b", 0, "15:30", "17:30"),
        _make_shift("c", 0, "17:30", "19:30"),
        _make_shift("d", 0, "19:30", "21:30"),
    ]
    sol = _solve([st], shifts, max_consec_hours=6.0,
                 force_assignment={("s1", "a"): 1, ("s1", "b"): 1,
                                   ("s1", "c"): 1, ("s1", "d"): 1})
    assert sol is None, "8h consecutive must be infeasible"


def test_eight_hours_solver_drops_one():
    """
    Same 4-shift chain, no forced assignments — solver may assign at most 3
    (= 6h) to the student.
    """
    st = _make_student("s1")
    shifts = [
        _make_shift("a", 0, "13:30", "15:30"),
        _make_shift("b", 0, "15:30", "17:30"),
        _make_shift("c", 0, "17:30", "19:30"),
        _make_shift("d", 0, "19:30", "21:30"),
    ]
    sol = _solve([st], shifts, max_consec_hours=6.0)
    assert sol is not None
    assigned = sum(sol[(st["id"], sh["id"])] for sh in shifts)
    assert assigned <= 3, f"Student assigned {assigned} shifts (max 3 for 6h limit)"


# ---------------------------------------------------------------------------
# 3. Gap breaks the consecutive block
# ---------------------------------------------------------------------------

def test_gap_breaks_consecutive_chain():
    """
    13:30–15:30 and 17:30–19:30 have a 2-hour gap between them.
    Together they are 4h but non-consecutive — a third adjacent shift on
    either side should not trigger the 6h constraint.

    Force student onto 13:30–15:30 (2h) + 17:30–19:30 (2h) + 19:30–21:30 (2h).
    Total = 6h, but first two are separated by a gap, so the actual consecutive
    run is only 4h (17:30–21:30). Should be feasible.
    """
    st = _make_student("s1")
    shifts = [
        _make_shift("a", 0, "13:30", "15:30"),   # standalone block
        # gap: 15:30–17:30 (no shift)
        _make_shift("b", 0, "17:30", "19:30"),
        _make_shift("c", 0, "19:30", "21:30"),
    ]
    sol = _solve([st], shifts, max_consec_hours=6.0,
                 force_assignment={("s1", "a"): 1, ("s1", "b"): 1, ("s1", "c"): 1})
    assert sol is not None, (
        "Gap between 15:30 and 17:30 breaks consecutive block; "
        "max consecutive run is 4h which is under the 6h limit"
    )


def test_non_consecutive_pair_does_not_count():
    """09:00–12:00 then 14:00–17:00 — 3h gap — each block is 3h. Fine."""
    st = _make_student("s1")
    shifts = [
        _make_shift("a", 0, "09:00", "12:00"),
        _make_shift("b", 0, "14:00", "17:00"),
    ]
    sol = _solve([st], shifts, max_consec_hours=6.0,
                 force_assignment={("s1", "a"): 1, ("s1", "b"): 1})
    assert sol is not None, "Non-consecutive blocks should not be constrained together"


# ---------------------------------------------------------------------------
# 4. Cross-midnight consecutive chain
# ---------------------------------------------------------------------------

def test_cross_midnight_chain_within_limit():
    """
    21:30–23:30 (2h) + 23:30–01:30 (2h) + 01:30–02:00 (0.5h) Mon/Mon/Tue
    = 4.5h consecutive — well under 6h, must be feasible.
    """
    st = _make_student("s1")
    shifts = [
        _make_shift("a", 0, "21:30", "23:30"),   # Mon
        _make_shift("b", 0, "23:30", "01:30"),   # Mon, ends Tue
        _make_shift("c", 1, "01:30", "02:00"),   # Tue
    ]
    sol = _solve([st], shifts, max_consec_hours=6.0,
                 force_assignment={("s1", "a"): 1, ("s1", "b"): 1, ("s1", "c"): 1})
    assert sol is not None, "4.5h cross-midnight consecutive chain must be feasible"


def test_cross_midnight_chain_over_limit_blocked():
    """
    21:30–23:30 + 23:30–01:30 + 01:30–03:30 + 03:30–05:30
    = 8h consecutive across midnight — must be infeasible.
    """
    st = _make_student("s1")
    shifts = [
        _make_shift("a", 0, "21:30", "23:30"),   # Mon 2h
        _make_shift("b", 0, "23:30", "01:30"),   # Mon→Tue 2h
        _make_shift("c", 1, "01:30", "03:30"),   # Tue 2h
        _make_shift("d", 1, "03:30", "05:30"),   # Tue 2h
    ]
    sol = _solve([st], shifts, max_consec_hours=6.0,
                 force_assignment={("s1", "a"): 1, ("s1", "b"): 1,
                                   ("s1", "c"): 1, ("s1", "d"): 1})
    assert sol is None, "8h cross-midnight consecutive chain must be infeasible"


def test_cross_midnight_six_hours_allowed():
    """
    21:30–23:30 + 23:30–01:30 + 01:30–03:30 = 6h exactly → allowed.
    """
    st = _make_student("s1")
    shifts = [
        _make_shift("a", 0, "21:30", "23:30"),
        _make_shift("b", 0, "23:30", "01:30"),
        _make_shift("c", 1, "01:30", "03:30"),
    ]
    sol = _solve([st], shifts, max_consec_hours=6.0,
                 force_assignment={("s1", "a"): 1, ("s1", "b"): 1, ("s1", "c"): 1})
    assert sol is not None, "Exactly 6h cross-midnight must be feasible"


# ---------------------------------------------------------------------------
# 5. Duplicate slots (slot_index 0 and 1 for same window)
#    No-overlap already blocks same student from taking both — but confirm
#    the consecutive-hours constraint doesn't double-count the window.
# ---------------------------------------------------------------------------

def test_duplicate_slots_do_not_inflate_consecutive_hours():
    """
    Slot0 and Slot1 of 13:30–15:30 are both eligible for the student.
    The no-overlap constraint blocks assigning both.
    The consecutive-hours constraint should treat them as a single 2h window.

    Force 3 windows (6h via slot1/slot0 pairs) → must remain feasible.
    """
    st = _make_student("s1")
    # Two slots per window, three windows
    shifts = [
        _make_shift("a0", 0, "13:30", "15:30"),  # slot 0
        _make_shift("a1", 0, "13:30", "15:30"),  # slot 1
        _make_shift("b0", 0, "15:30", "17:30"),
        _make_shift("b1", 0, "15:30", "17:30"),
        _make_shift("c0", 0, "17:30", "19:30"),
        _make_shift("c1", 0, "17:30", "19:30"),
    ]

    model = cp_model.CpModel()
    x = {}
    for sh in shifts:
        x[(st["id"], sh["id"])] = model.NewBoolVar(f"x_{sh['id']}")

    # No-overlap: at most one per window
    model.Add(x[(st["id"], "a0")] + x[(st["id"], "a1")] <= 1)
    model.Add(x[(st["id"], "b0")] + x[(st["id"], "b1")] <= 1)
    model.Add(x[(st["id"], "c0")] + x[(st["id"], "c1")] <= 1)

    _add_consecutive_hours_constraints(
        model, x, [st], shifts,
        max_consec_min=360,   # 6h
        stagger=5,
    )

    # Force exactly one slot per window (= 6h total consecutive)
    model.Add(x[(st["id"], "a0")] == 1)
    model.Add(x[(st["id"], "a1")] == 0)
    model.Add(x[(st["id"], "b0")] == 1)
    model.Add(x[(st["id"], "b1")] == 0)
    model.Add(x[(st["id"], "c0")] == 1)
    model.Add(x[(st["id"], "c1")] == 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5.0
    status = solver.Solve(model)
    assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE), (
        "3 unique windows (6h) should be feasible even when slots are doubled"
    )


def test_duplicate_slots_four_windows_blocked():
    """
    Same structure but 4 windows (8h) — even with slot0 only, must be blocked.
    """
    st = _make_student("s1")
    shifts = [
        _make_shift("a0", 0, "13:30", "15:30"),
        _make_shift("a1", 0, "13:30", "15:30"),
        _make_shift("b0", 0, "15:30", "17:30"),
        _make_shift("b1", 0, "15:30", "17:30"),
        _make_shift("c0", 0, "17:30", "19:30"),
        _make_shift("c1", 0, "17:30", "19:30"),
        _make_shift("d0", 0, "19:30", "21:30"),
        _make_shift("d1", 0, "19:30", "21:30"),
    ]

    model = cp_model.CpModel()
    x = {}
    for sh in shifts:
        x[(st["id"], sh["id"])] = model.NewBoolVar(f"x_{sh['id']}")

    for pair in [("a0","a1"), ("b0","b1"), ("c0","c1"), ("d0","d1")]:
        model.Add(x[(st["id"], pair[0])] + x[(st["id"], pair[1])] <= 1)

    _add_consecutive_hours_constraints(
        model, x, [st], shifts, max_consec_min=360, stagger=5,
    )

    # Force one slot per window = 8h consecutive
    for sh_id in ["a0", "a1", "b0", "b1", "c0", "c1", "d0", "d1"]:
        model.Add(x[(st["id"], sh_id)] == (1 if sh_id.endswith("0") else 0))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5.0
    status = solver.Solve(model)
    assert status == cp_model.INFEASIBLE, (
        "4 unique windows (8h) must be infeasible"
    )


# ---------------------------------------------------------------------------
# Integration: solver output must contain no CONSECUTIVE_HOURS_EXCEEDED
# ---------------------------------------------------------------------------

WEEK = date(2026, 6, 16)   # Fresh Monday not used by other suites


def test_solver_no_consecutive_hours_violation(client):
    """
    Full workflow: generate shifts, submit wide availability, run scheduler,
    check that no CONSECUTIVE_HOURS_EXCEEDED hard violation is produced.
    """
    wsd = str(WEEK)

    student_ids = []
    for i in range(5):
        resp = client.post("/api/v1/students", json={
            "name": f"ConsecStudent{i}",
            "email": f"consec{i}@test.edu",
            "seniority_date": "2023-09-01",
            "min_hours": 8,
            "max_hours": 20,
            "target_hours": 12,
        })
        assert resp.status_code == 200, resp.text
        student_ids.append(resp.json()["id"])

    # Generate shifts
    resp = client.post("/api/v1/shifts/instances/generate",
                       json={"week_start_date": wsd, "force": True})
    assert resp.status_code == 200, resp.text

    # Wide availability: every student available all week
    for sid in student_ids:
        for dow in range(7):
            client.post("/api/v1/availability", json={
                "student_id": sid,
                "week_start_date": wsd,
                "day_of_week": dow,
                "start_time": "07:00",
                "end_time": "02:00",
                "level": "preferred",
            })

    # Generate schedule
    resp = client.post("/api/v1/schedules/generate",
                       json={"week_start_date": wsd, "force_regenerate": True})
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["id"]

    # Check violations
    resp = client.get(f"/api/v1/violations/{run_id}")
    assert resp.status_code == 200, resp.text
    violations = resp.json()

    consec_hard = [
        v for v in violations
        if v.get("violation_type") == "CONSECUTIVE_HOURS_EXCEEDED"
        and v.get("severity") == "hard"
    ]
    assert consec_hard == [], (
        f"Solver produced {len(consec_hard)} CONSECUTIVE_HOURS_EXCEEDED violation(s):\n"
        + "\n".join(v.get("description", "") for v in consec_hard)
    )
