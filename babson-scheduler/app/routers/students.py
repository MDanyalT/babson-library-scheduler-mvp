import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import get_db
from app.models.schemas import StudentCreate, StudentOut, StudentUpdate

router = APIRouter(prefix="/api/v1/students", tags=["Students"])


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_student_out(row) -> StudentOut:
    return StudentOut(**dict(row._mapping))


@router.post("/", response_model=StudentOut)
def create_student(body: StudentCreate, conn: Connection = Depends(get_db)):
    student_id = str(uuid.uuid4())
    created_at = iso_now()
    # Per project brief: min_hours is always 8, max_hours always 20.
    # target_hours is the only student-specific hour field (midpoint of their
    # stated preference band).  Enforce constants here regardless of payload.
    try:
        conn.execute(
            text(
                """
                INSERT INTO students
                    (id, name, email, seniority_date, min_hours, max_hours,
                     target_hours, is_active, created_at)
                VALUES
                    (:id, :name, :email, :seniority_date, 8, 20,
                     :target_hours, 1, :created_at)
                """
            ),
            {
                "id": student_id,
                "name": body.name,
                "email": body.email,
                "seniority_date": str(body.seniority_date) if body.seniority_date else None,
                "target_hours": body.target_hours,
                "created_at": created_at,
            },
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail=f"Email already registered: {body.email}")
        raise HTTPException(status_code=422, detail=str(exc))

    row = conn.execute(
        text("SELECT * FROM students WHERE id=:id"), {"id": student_id}
    ).fetchone()
    return _row_to_student_out(row)


@router.get("/", response_model=list[StudentOut])
def list_students(conn: Connection = Depends(get_db)):
    rows = conn.execute(
        text("SELECT * FROM students WHERE is_active=1 ORDER BY seniority_date")
    ).fetchall()
    return [_row_to_student_out(r) for r in rows]


@router.get("/{student_id}", response_model=StudentOut)
def get_student(student_id: str, conn: Connection = Depends(get_db)):
    row = conn.execute(
        text("SELECT * FROM students WHERE id=:id"), {"id": student_id}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found")
    return _row_to_student_out(row)


@router.patch("/{student_id}", response_model=StudentOut)
def update_student(
    student_id: str, body: StudentUpdate, conn: Connection = Depends(get_db)
):
    existing = conn.execute(
        text("SELECT * FROM students WHERE id=:id"), {"id": student_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found")

    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return _row_to_student_out(existing)

    set_clauses = ", ".join(f"{k}=:{k}" for k in updates)
    updates["id"] = student_id
    try:
        conn.execute(
            text(f"UPDATE students SET {set_clauses} WHERE id=:id"), updates
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(exc))

    row = conn.execute(
        text("SELECT * FROM students WHERE id=:id"), {"id": student_id}
    ).fetchone()
    return _row_to_student_out(row)


@router.delete("/{student_id}", response_model=dict)
def deactivate_student(student_id: str, conn: Connection = Depends(get_db)):
    existing = conn.execute(
        text("SELECT id FROM students WHERE id=:id"), {"id": student_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found")

    try:
        conn.execute(
            text("UPDATE students SET is_active=0 WHERE id=:id"), {"id": student_id}
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(exc))

    return {"message": "Student deactivated", "id": student_id}
