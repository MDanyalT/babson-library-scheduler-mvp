from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import get_db

router = APIRouter(prefix="/api/v1/violations", tags=["Violations"])

# DB columns in violations: id, run_id, violation_type, shift_instance_id,
#                           student_id, description, severity, source


def _violation_row_to_dict(row) -> dict:
    r = dict(row._mapping)
    return {
        "id": r["id"],
        "run_id": r["run_id"],
        "violation_type": r["violation_type"],
        "severity": r["severity"],
        "source": r.get("source", "postflight"),
        "description": r.get("description", ""),
        "shift_instance_id": r.get("shift_instance_id"),
        "student_id": r.get("student_id"),
        "shift_date": r.get("shift_date"),
        "start_time": r.get("start_time"),
        "end_time": r.get("end_time"),
        "student_name": r.get("student_name"),
    }


@router.get("/{run_id}", response_model=list[dict])
def get_violations(
    run_id: str,
    severity: Optional[str] = None,
    conn: Connection = Depends(get_db),
):
    run = conn.execute(
        text("SELECT id FROM schedule_runs WHERE id=:rid"), {"rid": run_id}
    ).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail=f"Schedule run '{run_id}' not found")

    conditions = ["v.run_id=:run_id"]
    params: dict = {"run_id": run_id}

    if severity:
        if severity not in ("hard", "soft"):
            raise HTTPException(status_code=422, detail="severity must be 'hard' or 'soft'")
        conditions.append("v.severity=:severity")
        params["severity"] = severity

    where_clause = " AND ".join(conditions)

    rows = conn.execute(
        text(
            f"""
            SELECT
                v.id, v.run_id, v.violation_type, v.severity, v.source,
                v.description, v.shift_instance_id, v.student_id,
                si.date AS shift_date, si.start_time, si.end_time,
                s.name AS student_name
            FROM violations v
            LEFT JOIN shift_instances si ON v.shift_instance_id = si.id
            LEFT JOIN students s ON v.student_id = s.id
            WHERE {where_clause}
            ORDER BY v.severity DESC, si.date, si.start_time
            """
        ),
        params,
    ).fetchall()

    return [_violation_row_to_dict(r) for r in rows]


@router.get("/{run_id}/hard", response_model=list[dict])
def get_hard_violations(run_id: str, conn: Connection = Depends(get_db)):
    """
    Shorthand for ?severity=hard.
    IBM Orchestrate branch condition: empty list = safe to publish.
    """
    run = conn.execute(
        text("SELECT id FROM schedule_runs WHERE id=:rid"), {"rid": run_id}
    ).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail=f"Schedule run '{run_id}' not found")

    rows = conn.execute(
        text(
            """
            SELECT
                v.id, v.run_id, v.violation_type, v.severity, v.source,
                v.description, v.shift_instance_id, v.student_id,
                si.date AS shift_date, si.start_time, si.end_time,
                s.name AS student_name
            FROM violations v
            LEFT JOIN shift_instances si ON v.shift_instance_id = si.id
            LEFT JOIN students s ON v.student_id = s.id
            WHERE v.run_id=:run_id AND v.severity='hard'
            ORDER BY si.date, si.start_time
            """
        ),
        {"run_id": run_id},
    ).fetchall()

    return [_violation_row_to_dict(r) for r in rows]
