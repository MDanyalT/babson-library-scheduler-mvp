from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import get_db
from app.services.exporter import build_workbook, workbook_to_bytes

router = APIRouter(prefix="/api/v1/export", tags=["Export"])


@router.get("/{run_id}/excel")
def export_excel(run_id: str, conn: Connection = Depends(get_db)):
    run = conn.execute(
        text("SELECT week_start_date FROM schedule_runs WHERE id=:rid"),
        {"rid": run_id},
    ).fetchone()

    if not run:
        raise HTTPException(status_code=404, detail=f"Schedule run '{run_id}' not found")

    try:
        wb = build_workbook(conn, run_id)
        data = workbook_to_bytes(wb)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    filename = f"schedule-{run.week_start_date if run else run_id}.xlsx"

    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
