from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import get_db
from app.models.schemas import (
    AssignmentLockRequest,
    AssignmentOut,
    AssignmentPatchRequest,
    ScheduleFullOut,
    ScheduleGenerateRequest,
    ScheduleRunOut,
    StudentSummaryOut,
)
from app.services.schedule_service import (
    generate_schedule,
    get_full_schedule,
    get_run_summary,
    override_assignment,
    publish_run,
    set_assignment_lock,
    _load_assignments_with_details,
    _annotate_run,
)

router = APIRouter(prefix="/api/v1/schedules", tags=["Schedules"])


@router.post("/generate", response_model=ScheduleRunOut)
def run_generate(body: ScheduleGenerateRequest, conn: Connection = Depends(get_db)):
    try:
        result = generate_schedule(
            conn,
            str(body.week_start_date),
            is_exam_period=body.is_exam_period,
            solver_time_limit_seconds=body.solver_time_limit_seconds,
            force_regenerate=body.force_regenerate,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # generate_schedule returns {"run": {...}, "assignments": [...], ...}
    if isinstance(result, dict):
        run_dict = result.get("run", result)
        run_id = run_dict.get("id") if isinstance(run_dict, dict) else None
    elif isinstance(result, str):
        run_id = result
    else:
        run_id = getattr(result, "id", None) or getattr(result, "run_id", None)

    if run_id is None:
        raise HTTPException(status_code=500, detail="Schedule generation did not return a run ID")

    if isinstance(result, dict) and "run" in result:
        run_out = result["run"]
    else:
        try:
            full = get_run_summary(conn, run_id)
            run_out = full.get("run", full) if isinstance(full, dict) and "run" in full else full
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found after generation: {exc}")

    if isinstance(run_out, dict):
        return ScheduleRunOut(**run_out)
    return ScheduleRunOut(**dict(run_out))


@router.get("/", response_model=list[ScheduleRunOut])
def list_runs(
    week_start_date: Optional[str] = None,
    status: Optional[str] = None,
    conn: Connection = Depends(get_db),
):
    conditions = []
    params: dict = {}

    if week_start_date:
        conditions.append("week_start_date=:week_start_date")
        params["week_start_date"] = week_start_date
    if status:
        conditions.append("status=:status")
        params["status"] = status

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        text(f"SELECT * FROM schedule_runs {where_clause} ORDER BY generated_at DESC"),
        params,
    ).fetchall()

    return [ScheduleRunOut(**dict(r._mapping)) for r in rows]


@router.get("/{run_id}", response_model=ScheduleFullOut)
def get_schedule(run_id: str, conn: Connection = Depends(get_db)):
    try:
        full = get_full_schedule(conn, run_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not full:
        raise HTTPException(status_code=404, detail=f"Schedule run '{run_id}' not found")

    return full


@router.get("/{run_id}/assignments", response_model=list[AssignmentOut])
def get_assignments(run_id: str, conn: Connection = Depends(get_db)):
    run = conn.execute(
        text("SELECT id FROM schedule_runs WHERE id=:rid"), {"rid": run_id}
    ).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail=f"Schedule run '{run_id}' not found")

    return _load_assignments_with_details(conn, run_id)


@router.get("/{run_id}/student-summaries", response_model=list[StudentSummaryOut])
def get_student_summaries(run_id: str, conn: Connection = Depends(get_db)):
    try:
        full = get_full_schedule(conn, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if not full:
        raise HTTPException(status_code=404, detail=f"Schedule run '{run_id}' not found")

    if isinstance(full, dict):
        summaries = full.get("student_summaries", [])
    else:
        summaries = getattr(full, "student_summaries", [])
    return summaries


@router.patch("/{run_id}/assignments/{assignment_id}", response_model=dict)
def patch_assignment(
    run_id: str,
    assignment_id: str,
    body: AssignmentPatchRequest,
    conn: Connection = Depends(get_db),
):
    try:
        updated = override_assignment(
            conn,
            run_id,
            assignment_id,
            body.student_id,
            body.override_reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if not updated:
        raise HTTPException(status_code=404, detail=f"Assignment '{assignment_id}' not found")

    return updated


@router.post("/{run_id}/assignments/{assignment_id}/lock", response_model=dict)
def lock_assignment(
    run_id: str,
    assignment_id: str,
    conn: Connection = Depends(get_db),
):
    try:
        set_assignment_lock(conn, assignment_id, True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {"locked": True, "assignment_id": assignment_id}


@router.delete("/{run_id}/assignments/{assignment_id}/lock", response_model=dict)
def unlock_assignment(
    run_id: str,
    assignment_id: str,
    conn: Connection = Depends(get_db),
):
    try:
        set_assignment_lock(conn, assignment_id, False)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {"locked": False, "assignment_id": assignment_id}


@router.post("/{run_id}/publish", response_model=ScheduleRunOut)
def publish_schedule(run_id: str, conn: Connection = Depends(get_db)):
    run = conn.execute(
        text("SELECT id FROM schedule_runs WHERE id=:rid"), {"rid": run_id}
    ).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail=f"Schedule run '{run_id}' not found")

    try:
        publish_run(conn, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    full = get_run_summary(conn, run_id)
    run_dict = full.get("run", full) if isinstance(full, dict) and "run" in full else full
    if isinstance(run_dict, dict):
        return ScheduleRunOut(**run_dict)
    return ScheduleRunOut(**dict(run_dict))
