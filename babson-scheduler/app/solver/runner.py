"""
Solver runner — the single entry point for schedule generation.

Flow
----
1. Build the CP-SAT model via ``ScheduleModelBuilder``.
2. Solve with the configured time limit.
3. If optimal/feasible: extract assignments from the solution.
4. If infeasible / timeout with no solution: fall back to the greedy baseline.
5. Run ``explain_assignments`` to populate reason codes.
6. Persist the schedule_run row and all assignments to the DB.
7. Compute per-student summaries.
8. Return a ``SolveResult`` dataclass.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date

from ortools.sat.python import cp_model
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import get_config
from app.models.db_models import PrefLevelUsed, RunStatus, SolverStatus
from app.solver.builder import ScheduleModelBuilder
from app.solver.explainer import explain_assignments
from app.utils.time_utils import iso_now


# ---------------------------------------------------------------------------
# Public data structure
# ---------------------------------------------------------------------------

@dataclass
class SolveResult:
    """Returned by ``solve()`` — mirrors ``ScheduleFullOut`` in structure."""

    run_id: str
    solver_status: str
    solve_time_ms: int
    objective_score: float | None
    assignments: list[dict]
    student_summaries: list[dict]


# ---------------------------------------------------------------------------
# CP status mapping
# ---------------------------------------------------------------------------

_CP_STATUS_MAP: dict[int, str] = {
    cp_model.OPTIMAL: SolverStatus.OPTIMAL,
    cp_model.FEASIBLE: SolverStatus.FEASIBLE,
    cp_model.INFEASIBLE: SolverStatus.INFEASIBLE,
    cp_model.UNKNOWN: SolverStatus.TIMEOUT,
    cp_model.MODEL_INVALID: SolverStatus.INFEASIBLE,
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def solve(
    conn: Connection,
    week_start_date: str,
    time_limit_seconds: int | None = None,
    run_id: str | None = None,
    schedule_mode: str = "weekly",
    term_start_date: str | None = None,
    term_end_date: str | None = None,
) -> SolveResult:
    """
    Build, solve, persist and return the schedule for ``week_start_date``.

    Parameters
    ----------
    conn
        An open SQLAlchemy connection (will commit inside this function).
    week_start_date
        ISO date string for the Monday of the target week (``"YYYY-MM-DD"``).
    time_limit_seconds
        Override the configured time limit.  ``None`` uses the DB config value.
    run_id
        Optional pre-allocated UUID for the schedule_run row.

    Returns
    -------
    SolveResult
    """
    config = get_config(conn)
    effective_time_limit = (
        time_limit_seconds
        if time_limit_seconds is not None
        else config.get("solver_time_limit_seconds", 60)
    )

    # --- Build model ---
    builder = ScheduleModelBuilder(conn, week_start_date, effective_time_limit)
    model, vars_dict = builder.build()

    students: list[dict] = vars_dict["students"]
    shifts: list[dict] = vars_dict["shifts"]
    eligibility: dict[tuple[str, str], str] = vars_dict["eligibility"]
    locked_by_shift: dict[str, dict] = vars_dict.get("locked_by_shift", {})
    x: dict[tuple[str, str], object] = vars_dict["x"]
    unfilled: dict[str, object] = vars_dict["unfilled"]

    # --- Solve ---
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = effective_time_limit

    t_start = time.monotonic()
    status_code = solver.Solve(model)
    solve_time_ms = int((time.monotonic() - t_start) * 1000)

    solver_status = _CP_STATUS_MAP.get(status_code, SolverStatus.INFEASIBLE)

    # --- Extract or fall back ---
    if solver_status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE):
        objective_score: float | None = solver.ObjectiveValue()
        assignments = _extract_assignments(
            solver, x, unfilled, shifts, students, eligibility, locked_by_shift
        )
    else:
        # Greedy fallback
        solver_status = SolverStatus.GREEDY_FALLBACK
        objective_score = None
        from app.solver.greedy_baseline import greedy_schedule
        assignments = greedy_schedule(
            students=students,
            shifts=shifts,
            eligibility=eligibility,
            locked_assignments=locked_by_shift,
            config=config,
        )

    # --- Explain ---
    coverage_counts: dict[str, int] = {}
    for (s_id, sh_id) in eligibility:
        coverage_counts[sh_id] = coverage_counts.get(sh_id, 0) + 1

    assignments = explain_assignments(
        assignments=assignments,
        shifts=shifts,
        students=students,
        eligibility=eligibility,
        coverage_counts=coverage_counts,
    )

    # --- Persist ---
    run_id = run_id or str(uuid.uuid4())
    _persist(
        conn, run_id, week_start_date, solver_status,
        solve_time_ms, objective_score, assignments,
        schedule_mode=schedule_mode,
        term_start_date=term_start_date,
        term_end_date=term_end_date,
    )

    # --- Summaries ---
    student_summaries = _compute_student_summaries(assignments, students, shifts)

    return SolveResult(
        run_id=run_id,
        solver_status=solver_status,
        solve_time_ms=solve_time_ms,
        objective_score=objective_score,
        assignments=assignments,
        student_summaries=student_summaries,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_assignments(
    solver: cp_model.CpSolver,
    x: dict[tuple[str, str], object],
    unfilled: dict[str, object],
    shifts: list[dict],
    students: list[dict],
    eligibility: dict[tuple[str, str], str],
    locked_by_shift: dict[str, dict],
) -> list[dict]:
    """
    Walk every shift; find which x variable (if any) is 1 in the solution,
    and build an assignment dict.
    """
    student_ids = [st["id"] for st in students]
    assignments: list[dict] = []
    now_str = iso_now()

    for sh in shifts:
        sh_id = sh["id"]

        # Check locked first
        if sh_id in locked_by_shift:
            locked = locked_by_shift[sh_id]
            assignments.append({
                "id": locked.get("id", str(uuid.uuid4())),
                "shift_instance_id": sh_id,
                "student_id": locked.get("student_id"),
                "preference_level_used": locked.get(
                    "preference_level_used", PrefLevelUsed.UNASSIGNED
                ),
                "reason_codes": locked.get("reason_codes", json.dumps([])),
                "is_locked": 1,
                "is_manual_override": locked.get("is_manual_override", 0),
                "override_reason": locked.get("override_reason"),
                "assigned_at": locked.get("assigned_at", now_str),
            })
            continue

        assigned_student: str | None = None
        pref_used = PrefLevelUsed.UNASSIGNED

        for s_id in student_ids:
            if (s_id, sh_id) in x and solver.Value(x[(s_id, sh_id)]) == 1:
                assigned_student = s_id
                pref_used = eligibility.get((s_id, sh_id), PrefLevelUsed.AVAILABLE)
                break

        assignments.append({
            "id": str(uuid.uuid4()),
            "shift_instance_id": sh_id,
            "student_id": assigned_student,
            "preference_level_used": pref_used,
            "reason_codes": json.dumps([]),  # filled by explainer
            "is_locked": 0,
            "is_manual_override": 0,
            "override_reason": None,
            "assigned_at": now_str,
        })

    return assignments


def _persist(
    conn: Connection,
    run_id: str,
    week_start_date: str,
    solver_status: str,
    solve_time_ms: int,
    objective_score: float | None,
    assignments: list[dict],
    schedule_mode: str = "weekly",
    term_start_date: str | None = None,
    term_end_date: str | None = None,
) -> None:
    """Insert the schedule_run row and all assignment rows."""
    now_str = iso_now()

    # schedule_runs
    conn.execute(
        text("""
            INSERT INTO schedule_runs
                (id, week_start_date, status, solver_status, solve_time_ms,
                 objective_score, generated_at,
                 schedule_mode, term_start_date, term_end_date)
            VALUES
                (:id, :wsd, :status, :ss, :ms, :score, :now,
                 :schedule_mode, :term_start_date, :term_end_date)
        """),
        {
            "id": run_id,
            "wsd": week_start_date,
            "status": RunStatus.DRAFT,
            "ss": solver_status,
            "ms": solve_time_ms,
            "score": objective_score,
            "now": now_str,
            "schedule_mode": schedule_mode,
            "term_start_date": term_start_date,
            "term_end_date": term_end_date,
        },
    )

    # assignments
    for asn in assignments:
        conn.execute(
            text("""
                INSERT INTO assignments
                    (id, run_id, shift_instance_id, student_id,
                     preference_level_used, reason_codes, is_locked,
                     is_manual_override, override_reason, assigned_at)
                VALUES
                    (:id, :run_id, :shift_instance_id, :student_id,
                     :preference_level_used, :reason_codes, :is_locked,
                     :is_manual_override, :override_reason, :assigned_at)
            """),
            {
                "id": asn.get("id", str(uuid.uuid4())),
                "run_id": run_id,
                "shift_instance_id": asn["shift_instance_id"],
                "student_id": asn.get("student_id"),
                "preference_level_used": asn.get(
                    "preference_level_used", PrefLevelUsed.UNASSIGNED
                ),
                "reason_codes": asn.get("reason_codes", json.dumps([])),
                "is_locked": asn.get("is_locked", 0),
                "is_manual_override": asn.get("is_manual_override", 0),
                "override_reason": asn.get("override_reason"),
                "assigned_at": asn.get("assigned_at", iso_now()),
            },
        )

    conn.commit()


def _compute_student_summaries(
    assignments: list[dict],
    students: list[dict],
    shifts: list[dict],
) -> list[dict]:
    """
    Build one summary dict per active student describing their assigned hours,
    shift counts, and constraint status.
    """
    shift_by_id: dict[str, dict] = {sh["id"]: sh for sh in shifts}

    summaries: list[dict] = []
    for st in students:
        s_id = st["id"]

        my_asns = [
            a for a in assignments
            if a.get("student_id") == s_id
        ]

        assigned_hours = sum(
            shift_by_id[a["shift_instance_id"]]["duration_hours"]
            for a in my_asns
            if a["shift_instance_id"] in shift_by_id
        )
        preferred_count = sum(
            1 for a in my_asns
            if a.get("preference_level_used") == PrefLevelUsed.PREFERRED
        )
        available_count = sum(
            1 for a in my_asns
            if a.get("preference_level_used") == PrefLevelUsed.AVAILABLE
        )

        min_h = st.get("min_hours", 8)
        max_h = st.get("max_hours", 20)
        target_h = st.get("target_hours", 8)

        if assigned_hours < min_h:
            constraint_status = "under_minimum"
        elif assigned_hours > max_h:
            constraint_status = "over_maximum"
        elif abs(assigned_hours - target_h) < 0.1:
            constraint_status = "at_target"
        else:
            constraint_status = "ok"

        summaries.append({
            "student_id": s_id,
            "name": st.get("name", ""),
            "email": st.get("email", ""),
            "seniority_date": st.get("seniority_date", ""),
            "min_hours": min_h,
            "target_hours": target_h,
            "max_hours": max_h,
            "assigned_hours": round(assigned_hours, 2),
            "shifts_assigned": len(my_asns),
            "preferred_shifts": preferred_count,
            "available_shifts": available_count,
            "hours_vs_target": round(assigned_hours - target_h, 2),
            "constraint_status": constraint_status,
        })

    return summaries
