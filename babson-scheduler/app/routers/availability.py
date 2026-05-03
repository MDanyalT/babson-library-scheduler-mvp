import os
import tempfile
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import get_db
from app.intake.normalizer import normalize_excel
from app.intake.validator import validate_submission
from app.models.schemas import (
    AvailabilitySlotOut,
    AvailabilitySubmitRequest,
    CoverageHeatmapEntry,
)

router = APIRouter(prefix="/api/v1/availability", tags=["Availability"])


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/", response_model=dict)
def submit_availability(
    req: AvailabilitySubmitRequest, conn: Connection = Depends(get_db)
):
    slots_list = [s.model_dump() for s in req.slots]

    try:
        result = validate_submission(
            req.student_id, str(req.week_start_date), slots_list, conn
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if not result.is_valid:
        raise HTTPException(status_code=422, detail=result.errors)

    try:
        conn.execute(
            text(
                "DELETE FROM availability WHERE student_id=:sid AND week_start_date=:wsd"
            ),
            {"sid": req.student_id, "wsd": str(req.week_start_date)},
        )

        submitted_at = iso_now()
        import_source = getattr(req, "import_source", "manual")

        for slot in req.slots:
            slot_id = str(uuid.uuid4())
            shift_instance_id = getattr(slot, "shift_instance_id", None)
            day_of_week = getattr(slot, "day_of_week", None)
            start_time = getattr(slot, "start_time", None)
            end_time = getattr(slot, "end_time", None)
            level = getattr(slot, "level", "available")

            # When shift_instance_id is given but day/times are not,
            # look them up from the shift_instances table.
            if shift_instance_id and (day_of_week is None or start_time is None):
                si_row = conn.execute(
                    text("SELECT day_of_week, start_time, end_time FROM shift_instances WHERE id=:sid"),
                    {"sid": shift_instance_id},
                ).fetchone()
                if si_row:
                    si = dict(si_row._mapping)
                    day_of_week = si_row.day_of_week if day_of_week is None else day_of_week
                    start_time = si_row.start_time if start_time is None else start_time
                    end_time = si_row.end_time if end_time is None else end_time

            conn.execute(
                text(
                    """
                    INSERT INTO availability
                        (id, student_id, week_start_date, shift_instance_id,
                         day_of_week, start_time, end_time, level,
                         submitted_at, import_source)
                    VALUES
                        (:id, :student_id, :week_start_date, :shift_instance_id,
                         :day_of_week, :start_time, :end_time, :level,
                         :submitted_at, :import_source)
                    """
                ),
                {
                    "id": slot_id,
                    "student_id": req.student_id,
                    "week_start_date": str(req.week_start_date),
                    "shift_instance_id": shift_instance_id,
                    "day_of_week": day_of_week,
                    "start_time": str(start_time) if start_time else None,
                    "end_time": str(end_time) if end_time else None,
                    "level": level,
                    "submitted_at": submitted_at,
                    "import_source": import_source,
                },
            )

        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "message": "Availability saved",
        "slots_saved": len(req.slots),
        "student_id": req.student_id,
        "week_start_date": str(req.week_start_date),
    }


@router.get("/heatmap/{week_start_date}", response_model=list[CoverageHeatmapEntry])
def get_heatmap(week_start_date: str, conn: Connection = Depends(get_db)):
    instances = conn.execute(
        text(
            "SELECT * FROM shift_instances WHERE week_start_date=:wsd ORDER BY date, start_time"
        ),
        {"wsd": week_start_date},
    ).fetchall()

    heatmap = []
    for inst in instances:
        inst_d = dict(inst._mapping)
        preferred_row = conn.execute(
            text(
                "SELECT COUNT(*) as cnt FROM availability "
                "WHERE shift_instance_id=:siid AND level='preferred'"
            ),
            {"siid": inst_d["id"]},
        ).fetchone()
        available_row = conn.execute(
            text(
                "SELECT COUNT(*) as cnt FROM availability "
                "WHERE shift_instance_id=:siid AND level IN ('preferred', 'available')"
            ),
            {"siid": inst_d["id"]},
        ).fetchone()

        preferred_count = preferred_row.cnt if preferred_row else 0
        total_eligible = available_row.cnt if available_row else 0

        if total_eligible == 0:
            coverage_risk = "critical"
        elif total_eligible <= 2:
            coverage_risk = "low"
        else:
            coverage_risk = "none"

        heatmap.append(
            CoverageHeatmapEntry(
                shift_instance_id=inst_d["id"],
                date=inst_d["date"],
                day_of_week=inst_d["day_of_week"],
                start_time=inst_d["start_time"],
                end_time=inst_d["end_time"],
                slot_index=inst_d.get("slot_index", 0),
                is_hard_shift=bool(inst_d.get("is_hard_shift", 0)),
                preferred_count=preferred_count,
                available_count=total_eligible,
                total_eligible=total_eligible,
                coverage_risk=coverage_risk,
            )
        )

    return heatmap


@router.get("/", response_model=list[AvailabilitySlotOut])
def list_availability(
    student_id: Optional[str] = None,
    week_start_date: Optional[str] = None,
    conn: Connection = Depends(get_db),
):
    conditions = []
    params: dict = {}

    if student_id:
        conditions.append("student_id=:student_id")
        params["student_id"] = student_id
    if week_start_date:
        conditions.append("week_start_date=:week_start_date")
        params["week_start_date"] = week_start_date

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        text(f"SELECT * FROM availability {where_clause} ORDER BY week_start_date, day_of_week, start_time"),
        params,
    ).fetchall()

    return [AvailabilitySlotOut(**dict(r._mapping)) for r in rows]


@router.post("/import-excel", response_model=dict)
async def import_excel_matrix(
    file: UploadFile = File(...),
    week_start_date: date = Form(...),
    replace_existing: bool = Form(True),
    auto_generate_shifts: bool = Form(True),
    conn: Connection = Depends(get_db),
):
    """
    Import a wide-format Excel availability matrix.

    The workbook must follow the layout described in
    ``app/intake/excel_matrix.py``:
    one row per student, one column per shift window, headers encode
    day + time range in the form
    ``"{Day} Availability.{H:MM AM/PM} - {H:MM AM/PM}"``.

    Students are created or updated by name.  Availability records are linked
    to matching shift instances for the given week.
    """
    from app.intake.excel_matrix import import_matrix

    file_bytes = await file.read()
    suffix = os.path.splitext(file.filename or ".xlsx")[1] or ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        result = import_matrix(
            tmp_path, str(week_start_date), conn,
            replace_existing=replace_existing,
            auto_generate_shifts=auto_generate_shifts,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        os.unlink(tmp_path)

    if result.errors:
        raise HTTPException(status_code=422, detail=result.errors)

    return {
        "message": "Import complete",
        "week_start_date": str(week_start_date),
        "students_created": result.students_created,
        "students_updated": result.students_updated,
        "students_processed": result.students_processed,
        "availability_slots_inserted": result.availability_slots_inserted,
        "unmatched_windows": result.unmatched_windows,
        "warnings": result.warnings,
        "errors": result.errors,
    }


@router.post("/import", response_model=dict, deprecated=True)
async def import_availability(
    file: UploadFile = File(...),
    week_start_date: date = Form(...),
    student_id_column: str = Form("name"),
    conn: Connection = Depends(get_db),
):
    """
    **Deprecated — use ``POST /api/v1/availability/import-excel`` instead.**

    This endpoint accepted a *long-format* Excel file (one row per
    availability slot, one student per call) and was never fully implemented.
    It is retained only so existing integrations receive a clear 410 response
    rather than an opaque 500.

    The wide-format student availability matrix (one row per student, one
    column per shift window) is handled by ``/import-excel``.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "error": "This endpoint is deprecated and no longer functional.",
            "use_instead": "POST /api/v1/availability/import-excel",
            "description": (
                "Upload your wide-format Excel availability matrix to "
                "/api/v1/availability/import-excel with form fields: "
                "file (xlsx), week_start_date (YYYY-MM-DD), "
                "replace_existing (bool, default true)."
            ),
        },
    )

    # ------------------------------------------------------------------ #
    # Dead code kept for reference — the long-format path that was here   #
    # required normalize_excel() which had a broken call to               #
    # normalize_excel_rows().  That function has been fixed (see          #
    # app/intake/normalizer.py) but the /import endpoint itself has been  #
    # superseded by /import-excel.                                        #
    # ------------------------------------------------------------------ #
    file_bytes = await file.read()  # type: ignore[unreachable]

    suffix = os.path.splitext(file.filename or ".xlsx")[1] or ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        try:
            normalize_result = normalize_excel(
                tmp_path, str(week_start_date), student_id_column
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        submitted_at = iso_now()
        rows_inserted = 0
        errors = []

        for record in normalize_result.records:
            student_id = record.get("student_id")
            if not student_id:
                errors.append(f"Missing student_id in record: {record}")
                continue

            existing_student = conn.execute(
                text("SELECT id FROM students WHERE id=:sid OR name=:sid"),
                {"sid": student_id},
            ).fetchone()
            if not existing_student:
                errors.append(f"Student not found: {student_id}")
                continue

            resolved_student_id = existing_student.id

            conn.execute(
                text(
                    "DELETE FROM availability WHERE student_id=:sid AND week_start_date=:wsd"
                ),
                {"sid": resolved_student_id, "wsd": str(week_start_date)},
            )

            for slot in record.get("slots", []):
                slot_id = str(uuid.uuid4())
                try:
                    conn.execute(
                        text(
                            """
                            INSERT INTO availability
                                (id, student_id, week_start_date, shift_instance_id,
                                 day_of_week, start_time, end_time, level,
                                 submitted_at, import_source)
                            VALUES
                                (:id, :student_id, :week_start_date, :shift_instance_id,
                                 :day_of_week, :start_time, :end_time, :level,
                                 :submitted_at, 'excel_import')
                            """
                        ),
                        {
                            "id": slot_id,
                            "student_id": resolved_student_id,
                            "week_start_date": str(week_start_date),
                            "shift_instance_id": slot.get("shift_instance_id"),
                            "day_of_week": slot.get("day_of_week"),
                            "start_time": slot.get("start_time"),
                            "end_time": slot.get("end_time"),
                            "level": slot.get("level", slot.get("avail_level", "available")),
                            "submitted_at": submitted_at,
                        },
                    )
                    rows_inserted += 1
                except Exception as exc:
                    errors.append(str(exc))

        conn.commit()
    finally:
        os.unlink(tmp_path)

    return {
        "message": "Import complete",
        "slots_inserted": rows_inserted,
        "week_start_date": str(week_start_date),
        "students_processed": len(normalize_result.records),
        "errors": errors,
        "warnings": normalize_result.warnings if hasattr(normalize_result, "warnings") else [],
    }
