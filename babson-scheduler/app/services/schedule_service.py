"""
Schedule service — orchestrates the full scheduling pipeline:

1. Optionally generate shift instances for the week.
2. Run preflight diagnostics (warns but does not block unless hard findings).
3. Call the CP-SAT solver (with greedy fallback).
4. Run postflight diagnostics and persist violations.
5. Return a fully-assembled ScheduleFullOut-compatible dict.

All DB access uses ``conn.execute(text(...), params)`` / ``dict(row._mapping)``.
Lazy imports inside function bodies avoid circular-import issues.
"""

from __future__ import annotations

import json
import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import get_config
from app.models.db_models import (
    DiagSource,
    ReasonCode,
    RunStatus,
    SolverStatus,
    ViolationType,
)
from app.utils.time_utils import iso_now, parse_time_minutes, effective_end_min


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_schedule(
    conn: Connection,
    week_start_date: str,
    is_exam_period: bool = False,
    solver_time_limit_seconds: int = 60,
    slots_per_window: int = 2,
    force_regenerate: bool = False,
    schedule_mode: str = "weekly",
    term_start_date: str | None = None,
    term_end_date: str | None = None,
) -> dict:
    """
    Full scheduling pipeline for *week_start_date*.

    Returns a dict matching the shape of ``ScheduleFullOut``:
    ``{"run": {...}, "assignments": [...], "student_summaries": [...]}``.

    Raises
    ------
    ValueError
        If a published run exists for the week and *force_regenerate* is False,
        or if there are locked assignments that would be destroyed.
    RuntimeError
        If no shift instances exist after generation and none can be created.
    """
    # --- Guard: existing run? ---
    existing_run = _get_existing_run(conn, week_start_date)
    if existing_run:
        if not force_regenerate:
            raise ValueError(
                f"A schedule run already exists for week {week_start_date} "
                f"(run_id={existing_run['id']}, status={existing_run['status']}). "
                "Pass force_regenerate=True to overwrite a draft."
            )
        if existing_run["status"] == RunStatus.PUBLISHED:
            raise ValueError(
                f"Cannot overwrite a published schedule (run_id={existing_run['id']}). "
                "Unpublish it first."
            )
        # Check for locked assignments in existing run
        locked = conn.execute(
            text(
                "SELECT COUNT(*) AS cnt FROM assignments "
                "WHERE run_id = :run_id AND is_locked = 1"
            ),
            {"run_id": existing_run["id"]},
        ).fetchone()
        if locked and dict(locked._mapping)["cnt"] > 0:
            raise ValueError(
                f"Cannot regenerate: run {existing_run['id']} has locked assignments. "
                "Unlock them before regenerating."
            )
        # Delete old run data
        _delete_run(conn, existing_run["id"])

    # --- Step 1: Ensure shift instances exist ---
    _ensure_shift_instances(conn, week_start_date, is_exam_period, slots_per_window)

    # --- Step 2: Preflight diagnostics ---
    _run_preflight_if_possible(conn, week_start_date)

    # --- Step 3: Solve ---
    from app.solver.runner import solve

    result = solve(
        conn=conn,
        week_start_date=week_start_date,
        time_limit_seconds=solver_time_limit_seconds,
        schedule_mode=schedule_mode,
        term_start_date=term_start_date,
        term_end_date=term_end_date,
    )

    # --- Step 4: Postflight violations ---
    violations = _run_postflight_if_possible(conn, result.run_id, week_start_date)

    # --- Step 5: Update run status ---
    _finalize_run_status(conn, result.run_id, result.solver_status, violations)

    # --- Step 6: Assemble response ---
    return _assemble_full_out(conn, result, violations)


def get_schedule(conn: Connection, run_id: str) -> dict:
    """
    Load an existing schedule run by *run_id* and return the ScheduleFullOut dict.

    Raises KeyError if the run does not exist.
    """
    run_row = conn.execute(
        text("SELECT * FROM schedule_runs WHERE id = :id"),
        {"id": run_id},
    ).fetchone()
    if not run_row:
        raise KeyError(f"Schedule run not found: {run_id}")

    run = dict(run_row._mapping)
    violations = _load_violations(conn, run_id)
    assignments = _load_assignments_with_details(conn, run_id)
    student_summaries = _load_student_summaries(conn, run_id)

    run = _annotate_run(run, assignments, violations)
    return {
        "run": run,
        "assignments": assignments,
        "student_summaries": student_summaries,
    }


def get_latest_run_for_week(conn: Connection, week_start_date: str) -> Optional[dict]:
    """Return the most recently generated run for the week, or None."""
    row = conn.execute(
        text(
            "SELECT * FROM schedule_runs "
            "WHERE week_start_date = :wsd "
            "ORDER BY generated_at DESC LIMIT 1"
        ),
        {"wsd": week_start_date},
    ).fetchone()
    return dict(row._mapping) if row else None


def publish_run(conn: Connection, run_id: str) -> dict:
    """
    Transition a draft/under_review run to *published*.

    Raises ValueError if hard violations exist or the run is already published.
    """
    run_row = conn.execute(
        text("SELECT * FROM schedule_runs WHERE id = :id"),
        {"id": run_id},
    ).fetchone()
    if not run_row:
        raise KeyError(f"Schedule run not found: {run_id}")

    run = dict(run_row._mapping)
    if run["status"] == RunStatus.PUBLISHED:
        raise ValueError(f"Run {run_id} is already published.")

    # Block on hard violations
    hard_count = conn.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM violations "
            "WHERE run_id = :run_id AND severity = 'hard'"
        ),
        {"run_id": run_id},
    ).fetchone()
    if hard_count and dict(hard_count._mapping)["cnt"] > 0:
        raise ValueError(
            "Cannot publish: schedule has hard violations. "
            "Resolve them or apply manual overrides first."
        )

    conn.execute(
        text(
            "UPDATE schedule_runs SET status = :status, published_at = :now "
            "WHERE id = :id"
        ),
        {"status": RunStatus.PUBLISHED, "now": iso_now(), "id": run_id},
    )
    conn.commit()

    return get_schedule(conn, run_id)


def patch_assignment(
    conn: Connection,
    run_id: str,
    assignment_id: str,
    student_id: Optional[str],
    override_reason: Optional[str],
) -> dict:
    """
    Apply a manual override to a single assignment.

    Sets ``is_manual_override=1`` and ``reason_codes=[MANUAL_OVERRIDE]``.
    Unlocks the assignment if it was previously locked.
    Returns the updated assignment row dict.
    """
    row = conn.execute(
        text(
            "SELECT * FROM assignments WHERE id = :id AND run_id = :run_id"
        ),
        {"id": assignment_id, "run_id": run_id},
    ).fetchone()
    if not row:
        raise KeyError(f"Assignment {assignment_id} not found in run {run_id}.")

    conn.execute(
        text(
            "UPDATE assignments SET "
            "  student_id = :student_id, "
            "  is_manual_override = 1, "
            "  override_reason = :reason, "
            "  preference_level_used = 'available', "
            "  reason_codes = :codes, "
            "  assigned_at = :now "
            "WHERE id = :id"
        ),
        {
            "student_id": student_id,
            "reason": override_reason,
            "codes": json.dumps([ReasonCode.MANUAL_OVERRIDE]),
            "now": iso_now(),
            "id": assignment_id,
        },
    )
    conn.commit()

    updated = conn.execute(
        text("SELECT * FROM assignments WHERE id = :id"),
        {"id": assignment_id},
    ).fetchone()
    return dict(updated._mapping)


def set_assignment_lock(
    conn: Connection,
    run_id: str,
    assignment_id: str,
    locked: bool,
) -> dict:
    """Lock or unlock a single assignment. Returns updated row dict."""
    row = conn.execute(
        text(
            "SELECT id FROM assignments WHERE id = :id AND run_id = :run_id"
        ),
        {"id": assignment_id, "run_id": run_id},
    ).fetchone()
    if not row:
        raise KeyError(f"Assignment {assignment_id} not found in run {run_id}.")

    conn.execute(
        text("UPDATE assignments SET is_locked = :locked WHERE id = :id"),
        {"locked": 1 if locked else 0, "id": assignment_id},
    )
    conn.commit()

    updated = conn.execute(
        text("SELECT * FROM assignments WHERE id = :id"),
        {"id": assignment_id},
    ).fetchone()
    return dict(updated._mapping)


def list_runs(conn: Connection, week_start_date: Optional[str] = None) -> list[dict]:
    """
    List schedule runs, optionally filtered to a specific week.
    Returns list of run dicts annotated with shift/violation counts.
    """
    if week_start_date:
        rows = conn.execute(
            text(
                "SELECT * FROM schedule_runs WHERE week_start_date = :wsd "
                "ORDER BY generated_at DESC"
            ),
            {"wsd": week_start_date},
        ).fetchall()
    else:
        rows = conn.execute(
            text("SELECT * FROM schedule_runs ORDER BY generated_at DESC")
        ).fetchall()

    result = []
    for row in rows:
        run = dict(row._mapping)
        assignments = _load_assignments_with_details(conn, run["id"])
        violations = _load_violations(conn, run["id"])
        result.append(_annotate_run(run, assignments, violations))

    return result


def delete_run(conn: Connection, run_id: str) -> None:
    """
    Hard-delete a draft run and all its assignments/violations.

    Raises ValueError if the run is published.
    """
    row = conn.execute(
        text("SELECT status FROM schedule_runs WHERE id = :id"),
        {"id": run_id},
    ).fetchone()
    if not row:
        raise KeyError(f"Schedule run not found: {run_id}")
    if dict(row._mapping)["status"] == RunStatus.PUBLISHED:
        raise ValueError("Cannot delete a published run.")

    _delete_run(conn, run_id)


# ---------------------------------------------------------------------------
# Internal helpers — lifecycle
# ---------------------------------------------------------------------------


def _get_existing_run(conn: Connection, week_start_date: str) -> Optional[dict]:
    row = conn.execute(
        text(
            "SELECT * FROM schedule_runs WHERE week_start_date = :wsd "
            "ORDER BY generated_at DESC LIMIT 1"
        ),
        {"wsd": week_start_date},
    ).fetchone()
    return dict(row._mapping) if row else None


def _delete_run(conn: Connection, run_id: str) -> None:
    conn.execute(text("DELETE FROM violations WHERE run_id = :id"), {"id": run_id})
    conn.execute(text("DELETE FROM assignments WHERE run_id = :id"), {"id": run_id})
    conn.execute(text("DELETE FROM schedule_runs WHERE id = :id"), {"id": run_id})
    conn.commit()


def _ensure_shift_instances(
    conn: Connection,
    week_start_date: str,
    is_exam_period: bool,
    slots_per_window: int,
) -> None:
    """Generate shift instances if none exist for the week."""
    count_row = conn.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM shift_instances "
            "WHERE week_start_date = :wsd"
        ),
        {"wsd": week_start_date},
    ).fetchone()
    existing = dict(count_row._mapping)["cnt"] if count_row else 0

    if existing == 0:
        try:
            from app.services.shift_generator import (
                get_instances_for_week,
                generate_from_operating_hours,
            )
            if is_exam_period:
                generate_from_operating_hours(
                    conn=conn,
                    week_start_date=week_start_date,
                    slots_per_window=slots_per_window,
                    is_exam_period=True,
                )
            else:
                get_instances_for_week(
                    conn=conn,
                    week_start_date=week_start_date,
                    slots_per_window=slots_per_window,
                    force=True,
                )
        except ImportError:
            pass  # shift_generator not yet wired; caller must pre-populate

    # Final check
    count_row = conn.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM shift_instances "
            "WHERE week_start_date = :wsd"
        ),
        {"wsd": week_start_date},
    ).fetchone()
    final = dict(count_row._mapping)["cnt"] if count_row else 0
    if final == 0:
        raise RuntimeError(
            f"No shift instances found for week {week_start_date}. "
            "Generate them first via /shifts/instances or supply shift templates."
        )


def _run_preflight_if_possible(conn: Connection, week_start_date: str) -> None:
    """Attempt to run preflight diagnostics; log but never block on ImportError."""
    try:
        from app.diagnostics.postflight import run_postflight  # noqa: F401
        # Preflight is a separate module; try importing it directly
        from app.diagnostics import preflight as _pf  # type: ignore
        _pf.run_preflight(conn, week_start_date)
    except (ImportError, AttributeError):
        pass


def _run_postflight_if_possible(
    conn: Connection,
    run_id: str,
    week_start_date: str,
) -> list[dict]:
    """Run postflight checks, persist violations, and return them as dicts."""
    try:
        from app.diagnostics.postflight import run_postflight

        # run_postflight persists violations to DB and returns a PostflightReport
        # dataclass. Load the violations from the DB for a consistent dict format.
        run_postflight(conn=conn, run_id=run_id, week_start_date=week_start_date)
        return _load_violations(conn, run_id)
    except (ImportError, TypeError, Exception):
        # postflight not available yet — derive basic violations inline
        return _derive_basic_violations(conn, run_id)


def _derive_basic_violations(conn: Connection, run_id: str) -> list[dict]:
    """
    Minimal postflight: flag unfilled shifts.
    Used when the full diagnostics module is unavailable.
    """
    violations: list[dict] = []
    now_str = iso_now()

    # Unfilled shifts
    rows = conn.execute(
        text(
            "SELECT a.id AS asn_id, a.shift_instance_id, a.student_id, "
            "       si.start_time, si.end_time, si.date, si.is_hard_shift "
            "FROM assignments a "
            "JOIN shift_instances si ON si.id = a.shift_instance_id "
            "WHERE a.run_id = :run_id AND a.student_id IS NULL"
        ),
        {"run_id": run_id},
    ).fetchall()

    for row in rows:
        r = dict(row._mapping)
        vid = str(uuid.uuid4())
        vtype = ViolationType.UNFILLED_SHIFT
        desc = (
            f"Shift on {r['date']} {r['start_time']}–{r['end_time']} "
            f"has no assigned student."
        )
        conn.execute(
            text(
                "INSERT INTO violations "
                "(id, run_id, violation_type, shift_instance_id, student_id, "
                " description, severity, source) "
                "VALUES (:id, :run_id, :vtype, :si_id, NULL, :desc, :sev, :src)"
            ),
            {
                "id": vid,
                "run_id": run_id,
                "vtype": vtype,
                "si_id": r["shift_instance_id"],
                "desc": desc,
                "sev": ViolationType.severity(vtype),
                "src": DiagSource.POSTFLIGHT,
            },
        )
        violations.append(
            {
                "id": vid,
                "run_id": run_id,
                "violation_type": vtype,
                "shift_instance_id": r["shift_instance_id"],
                "student_id": None,
                "description": desc,
                "severity": ViolationType.severity(vtype),
                "source": DiagSource.POSTFLIGHT,
            }
        )

    conn.commit()
    return violations


def _finalize_run_status(
    conn: Connection,
    run_id: str,
    solver_status: str,
    violations: list[dict],
) -> None:
    """Set the run status based on solver outcome and violation severity."""
    if solver_status == SolverStatus.INFEASIBLE:
        status = RunStatus.INFEASIBLE
    else:
        hard_count = sum(1 for v in violations if v.get("severity") == "hard")
        status = RunStatus.UNDER_REVIEW if hard_count > 0 else RunStatus.DRAFT

    conn.execute(
        text("UPDATE schedule_runs SET status = :status WHERE id = :id"),
        {"status": status, "id": run_id},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Internal helpers — data loading
# ---------------------------------------------------------------------------


def _load_violations(conn: Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        text("SELECT * FROM violations WHERE run_id = :run_id"),
        {"run_id": run_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _load_assignments_with_details(conn: Connection, run_id: str) -> list[dict]:
    """
    Load assignments joined with shift_instance and student data,
    returning a list of AssignmentOut-compatible dicts.
    """
    rows = conn.execute(
        text(
            "SELECT "
            "  a.id, a.shift_instance_id, a.student_id, "
            "  a.preference_level_used, a.reason_codes, "
            "  a.is_locked, a.is_manual_override, a.override_reason, a.assigned_at, "
            "  si.date AS shift_date, si.day_of_week, si.start_time, si.end_time, "
            "  si.duration_hours, si.is_hard_shift, si.slot_index, "
            "  s.name AS student_name, s.email AS student_email, "
            "  s.seniority_date AS student_seniority_date "
            "FROM assignments a "
            "JOIN shift_instances si ON si.id = a.shift_instance_id "
            "LEFT JOIN students s ON s.id = a.student_id "
            "WHERE a.run_id = :run_id "
            "ORDER BY si.date, si.start_time, si.slot_index"
        ),
        {"run_id": run_id},
    ).fetchall()

    assignments = []
    for row in rows:
        r = dict(row._mapping)
        asn: dict = {
            "id": r["id"],
            "shift": {
                "id": r["shift_instance_id"],
                "date": r["shift_date"],
                "day_of_week": r["day_of_week"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "duration_hours": r["duration_hours"],
                "is_hard_shift": bool(r["is_hard_shift"]),
                "slot_index": r["slot_index"],
            },
            "student": None,
            "preference_level_used": r["preference_level_used"] or "unassigned",
            "reason_codes": _parse_json_list(r.get("reason_codes")),
            "is_locked": bool(r["is_locked"]),
            "is_manual_override": bool(r["is_manual_override"]),
            "override_reason": r.get("override_reason"),
            "assigned_at": r["assigned_at"],
        }
        if r.get("student_id"):
            asn["student"] = {
                "id": r["student_id"],
                "name": r["student_name"] or "",
                "email": r["student_email"] or "",
                "seniority_date": r["student_seniority_date"] or "",
            }
        assignments.append(asn)

    return assignments


def _load_student_summaries(conn: Connection, run_id: str) -> list[dict]:
    """
    Compute per-student summaries from the assignments in this run.
    Returns list of StudentSummaryOut-compatible dicts.
    """
    student_rows = conn.execute(
        text(
            "SELECT * FROM students WHERE is_active = 1 ORDER BY seniority_date"
        )
    ).fetchall()
    students = [dict(r._mapping) for r in student_rows]

    asn_rows = conn.execute(
        text(
            "SELECT a.student_id, a.preference_level_used, si.duration_hours "
            "FROM assignments a "
            "JOIN shift_instances si ON si.id = a.shift_instance_id "
            "WHERE a.run_id = :run_id"
        ),
        {"run_id": run_id},
    ).fetchall()

    # Aggregate per student
    hours_map: dict[str, float] = {}
    pref_map: dict[str, int] = {}
    avail_map: dict[str, int] = {}
    shift_count_map: dict[str, int] = {}

    for row in asn_rows:
        r = dict(row._mapping)
        s_id = r.get("student_id")
        if not s_id:
            continue
        hours_map[s_id] = hours_map.get(s_id, 0.0) + (r["duration_hours"] or 0.0)
        shift_count_map[s_id] = shift_count_map.get(s_id, 0) + 1
        level = r.get("preference_level_used", "")
        if level == "preferred":
            pref_map[s_id] = pref_map.get(s_id, 0) + 1
        elif level == "available":
            avail_map[s_id] = avail_map.get(s_id, 0) + 1

    summaries = []
    for st in students:
        s_id = st["id"]
        assigned_hours = round(hours_map.get(s_id, 0.0), 2)
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

        summaries.append(
            {
                "student_id": s_id,
                "name": st.get("name", ""),
                "email": st.get("email", ""),
                "seniority_date": st.get("seniority_date", ""),
                "min_hours": min_h,
                "target_hours": target_h,
                "max_hours": max_h,
                "assigned_hours": assigned_hours,
                "shifts_assigned": shift_count_map.get(s_id, 0),
                "preferred_shifts": pref_map.get(s_id, 0),
                "available_shifts": avail_map.get(s_id, 0),
                "hours_vs_target": round(assigned_hours - target_h, 2),
                "constraint_status": constraint_status,
            }
        )

    return summaries


def _annotate_run(
    run: dict,
    assignments: list[dict],
    violations: list[dict],
) -> dict:
    """Add aggregate counts to a run dict."""
    total = len(assignments)
    filled = sum(1 for a in assignments if a.get("student") is not None)
    hard_v = sum(1 for v in violations if v.get("severity") == "hard")
    soft_v = sum(1 for v in violations if v.get("severity") == "soft")

    run = dict(run)
    run["total_shifts"] = total
    run["filled_shifts"] = filled
    run["unfilled_shifts"] = total - filled
    run["hard_violations_count"] = hard_v
    run["soft_violations_count"] = soft_v
    return run


def _assemble_full_out(conn: Connection, result, violations: list[dict]) -> dict:
    """
    Build the final ScheduleFullOut dict from a SolveResult and violation list.
    """
    assignments = _load_assignments_with_details(conn, result.run_id)
    student_summaries = result.student_summaries

    # Reload run row to get final status
    run_row = conn.execute(
        text("SELECT * FROM schedule_runs WHERE id = :id"),
        {"id": result.run_id},
    ).fetchone()
    run = dict(run_row._mapping) if run_row else {}
    run = _annotate_run(run, assignments, violations)

    return {
        "run": run,
        "assignments": assignments,
        "student_summaries": student_summaries,
    }


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _parse_json_list(raw: Optional[str]) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ---------------------------------------------------------------------------
# Compatibility aliases — match router import names
# ---------------------------------------------------------------------------

def get_run_summary(conn: Connection, run_id: str) -> dict:
    """Alias for get_schedule."""
    return get_schedule(conn, run_id)


def get_full_schedule(conn: Connection, run_id: str) -> dict:
    """Alias: returns {run, assignments, student_summaries}.

    get_schedule() already returns the fully-assembled dict; just return it.
    """
    return get_schedule(conn, run_id)


def override_assignment(
    conn: Connection,
    run_id: str,
    assignment_id: str,
    student_id,
    override_reason,
) -> dict:
    """Alias for patch_assignment."""
    return patch_assignment(conn, run_id, assignment_id, student_id, override_reason)
