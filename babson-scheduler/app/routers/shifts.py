import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import get_db
from app.models.schemas import (
    ShiftInstanceGenerateRequest,
    ShiftInstanceOut,
    ShiftTemplateCreate,
    ShiftTemplateOut,
)
from app.services.shift_generator import (
    generate_from_operating_hours,
    generate_from_templates,
    seed_default_templates,
)

router = APIRouter(prefix="/api/v1/shifts", tags=["Shifts"])

# Hard shift windows: (start_hour_inclusive, end_hour_inclusive) pairs
HARD_SHIFT_WINDOWS = [
    (8, 9),    # early morning open
    (20, 22),  # late evening close
]


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _time_to_minutes(t: str) -> int:
    """Convert HH:MM string to minutes since midnight."""
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _compute_duration_hours(start_time: str, end_time: str) -> float:
    start_min = _time_to_minutes(start_time)
    end_min = _time_to_minutes(end_time)
    # Handle overnight shifts
    if end_min <= start_min:
        end_min += 24 * 60
    return (end_min - start_min) / 60.0


def _is_hard_shift(start_time: str, end_time: str) -> bool:
    start_hour = int(start_time.split(":")[0])
    end_hour = int(end_time.split(":")[0])
    for (ws, we) in HARD_SHIFT_WINDOWS:
        if start_hour <= ws or end_hour >= we:
            return True
    return False


def _row_to_template_out(row) -> ShiftTemplateOut:
    return ShiftTemplateOut(**dict(row._mapping))


def _row_to_instance_out(row) -> ShiftInstanceOut:
    return ShiftInstanceOut(**dict(row._mapping))


@router.get("/templates", response_model=list[ShiftTemplateOut])
def list_templates(conn: Connection = Depends(get_db)):
    rows = conn.execute(
        text("SELECT * FROM shift_templates WHERE is_active=1 ORDER BY day_of_week, start_time")
    ).fetchall()
    return [_row_to_template_out(r) for r in rows]


@router.post("/templates", response_model=ShiftTemplateOut, status_code=status.HTTP_201_CREATED)
def create_template(body: ShiftTemplateCreate, conn: Connection = Depends(get_db)):
    template_id = str(uuid.uuid4())

    start_time = str(body.start_time)
    end_time = str(body.end_time)

    duration_hours = _compute_duration_hours(start_time, end_time)
    # Honour the caller's is_hard_shift override if provided, else auto-detect
    is_hard = body.is_hard_shift if body.is_hard_shift else _is_hard_shift(start_time, end_time)

    try:
        conn.execute(
            text(
                """
                INSERT INTO shift_templates
                    (id, day_of_week, start_time, end_time,
                     duration_hours, is_hard_shift, label, created_by, is_active)
                VALUES
                    (:id, :day_of_week, :start_time, :end_time,
                     :duration_hours, :is_hard_shift, :label, 'api', 1)
                """
            ),
            {
                "id": template_id,
                "day_of_week": body.day_of_week,
                "start_time": start_time,
                "end_time": end_time,
                "duration_hours": duration_hours,
                "is_hard_shift": 1 if is_hard else 0,
                "label": getattr(body, "label", None),
            },
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(exc))

    row = conn.execute(
        text("SELECT * FROM shift_templates WHERE id=:id"), {"id": template_id}
    ).fetchone()
    return _row_to_template_out(row)


@router.delete("/templates/{template_id}", response_model=dict)
def deactivate_template(template_id: str, conn: Connection = Depends(get_db)):
    existing = conn.execute(
        text("SELECT id FROM shift_templates WHERE id=:id"), {"id": template_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")

    try:
        conn.execute(
            text("UPDATE shift_templates SET is_active=0 WHERE id=:id"),
            {"id": template_id},
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(exc))

    return {"message": "Template deactivated", "id": template_id}


@router.post("/instances/generate", response_model=dict)
def generate_instances(body: ShiftInstanceGenerateRequest, conn: Connection = Depends(get_db)):
    week_start_date = str(body.week_start_date)
    is_exam_period = getattr(body, "is_exam_period", False) or False
    slots_per_window = getattr(body, "slots_per_window", 1) or 1
    force = getattr(body, "force", True)
    if force is None:
        force = True

    try:
        # Check if active templates exist
        row = conn.execute(
            text("SELECT COUNT(*) FROM shift_templates WHERE is_active=1")
        ).fetchone()
        template_count = row[0] if row else 0

        if template_count > 0:
            generated = generate_from_templates(
                conn, week_start_date,
                is_exam_period=is_exam_period,
                slots_per_window=slots_per_window,
                force=force,
            )
        else:
            generated = generate_from_operating_hours(
                conn, week_start_date,
                is_exam_period=is_exam_period,
                slots_per_window=slots_per_window,
                force=force,
            )

        count = len(generated) if isinstance(generated, list) else int(generated)

    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "generated": count,
        "week_start_date": week_start_date,
        "slots_per_window": slots_per_window,
        "is_exam_period": is_exam_period,
    }


@router.get("/instances", response_model=list[ShiftInstanceOut])
def list_instances(week_start_date: str, conn: Connection = Depends(get_db)):
    rows = conn.execute(
        text(
            "SELECT * FROM shift_instances WHERE week_start_date=:wsd ORDER BY date, start_time"
        ),
        {"wsd": week_start_date},
    ).fetchall()
    return [_row_to_instance_out(r) for r in rows]


@router.post("/seed-templates", response_model=dict)
def seed_templates(conn: Connection = Depends(get_db)):
    try:
        result = seed_default_templates(conn)
        seeded = result if isinstance(result, int) else getattr(result, "count", 0)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"seeded": seeded}
