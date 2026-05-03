import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import get_db
from app.diagnostics.preflight import run_preflight
from app.models.schemas import (
    DiagnosticFinding,
    PreflightReportOut,
    SchedulerConfigOut,
    SchedulerConfigUpdate,
)

router = APIRouter(prefix="/api/v1/diagnostics", tags=["Diagnostics"])


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PreflightRequest(BaseModel):
    week_start_date: str
    is_exam_period: bool = False


@router.post("/preflight", response_model=dict)
def run_preflight_check(body: PreflightRequest, conn: Connection = Depends(get_db)):
    try:
        report = run_preflight(conn, body.week_start_date, body.is_exam_period)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return report


@router.get("/preflight/{week_start_date}", response_model=PreflightReportOut)
def get_preflight_snapshot(week_start_date: str, conn: Connection = Depends(get_db)):
    row = conn.execute(
        text(
            """
            SELECT * FROM diagnostics_snapshots
            WHERE week_start_date=:wsd AND snapshot_type='preflight'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"wsd": week_start_date},
    ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"No preflight snapshot found for week '{week_start_date}'",
        )

    data = dict(row._mapping)

    # Parse findings JSON if stored as a string
    findings_raw = data.get("findings", "[]")
    if isinstance(findings_raw, str):
        try:
            findings_list = json.loads(findings_raw)
        except (json.JSONDecodeError, TypeError):
            findings_list = []
    else:
        findings_list = findings_raw or []

    findings = [
        DiagnosticFinding(**f) if isinstance(f, dict) else f for f in findings_list
    ]

    return PreflightReportOut(
        week_start_date=data.get("week_start_date", week_start_date),
        is_ready=data.get("is_ready", False),
        findings=findings,
        student_count=data.get("student_count", 0),
        shift_count=data.get("shift_count", 0),
        availability_count=data.get("availability_count", 0),
        generated_at=data.get("created_at", iso_now()),
    )


@router.get("/config", response_model=SchedulerConfigOut)
def get_config(conn: Connection = Depends(get_db)):
    row = conn.execute(
        text("SELECT * FROM scheduler_config WHERE id=1")
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Scheduler config not found")
    data = dict(row._mapping)
    # hard_shift_windows is stored as JSON string in SQLite
    if isinstance(data.get("hard_shift_windows"), str):
        try:
            data["hard_shift_windows"] = json.loads(data["hard_shift_windows"])
        except (json.JSONDecodeError, TypeError):
            data["hard_shift_windows"] = []
    return SchedulerConfigOut(**data)


@router.patch("/config", response_model=SchedulerConfigOut)
def update_config(body: SchedulerConfigUpdate, conn: Connection = Depends(get_db)):
    existing = conn.execute(
        text("SELECT * FROM scheduler_config WHERE id=1")
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Scheduler config not found")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        data = dict(existing._mapping)
        if isinstance(data.get("hard_shift_windows"), str):
            data["hard_shift_windows"] = json.loads(data["hard_shift_windows"])
        return SchedulerConfigOut(**data)

    updates["updated_at"] = iso_now()
    set_clauses = ", ".join(f"{k}=:{k}" for k in updates)

    try:
        conn.execute(
            text(f"UPDATE scheduler_config SET {set_clauses} WHERE id=1"), updates
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(exc))

    row = conn.execute(
        text("SELECT * FROM scheduler_config WHERE id=1")
    ).fetchone()
    data = dict(row._mapping)
    if isinstance(data.get("hard_shift_windows"), str):
        data["hard_shift_windows"] = json.loads(data["hard_shift_windows"])
    return SchedulerConfigOut(**data)
