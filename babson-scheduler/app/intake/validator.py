"""
Availability validator — checks normalized availability records against
business rules before DB insertion.

Validation checks performed:
  1. Student exists and is active.
  2. week_start_date is a Monday.
  3. day_of_week is in 0–6.
  4. start_time < end_time (cross-midnight handled).
  5. level is a valid AvailLevel value.
  6. The time window is within the library's operating hours for that day
     (warning only — soft violation).
  7. Duplicate detection: a record for (student, week, day, start, end) already
     exists in the DB.
  8. Overnight-sequence feasibility: a student marking a shift that ends after
     midnight and also marking a next-day shift that starts before
     MORNING_CUTOFF_HOUR generates an advisory warning.

Returns a ``ValidationResult`` with separate lists for hard errors (block
insertion) and soft warnings (insert with advisory).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import (
    DAY_NAMES,
    LIBRARY_HOURS,
    MORNING_CUTOFF_HOUR,
    OVERNIGHT_END_HOUR,
)
from app.models.db_models import AvailLevel
from app.utils.time_utils import (
    is_overnight_end,
    parse_time_minutes,
    too_early_after_overnight,
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    """Result of validating a batch of normalized availability records."""

    valid: list[dict] = field(default_factory=list)
    """Records that passed all hard checks (may have soft warnings attached)."""

    errors: list[dict] = field(default_factory=list)
    """Hard errors — records that must NOT be inserted. Each dict has keys:
    ``index``, ``field``, ``message``, ``record``."""

    warnings: list[dict] = field(default_factory=list)
    """Soft advisories attached to valid records. Each dict has keys:
    ``index``, ``field``, ``message``."""

    duplicates: list[dict] = field(default_factory=list)
    """Records that already exist in the DB and were skipped."""

    @property
    def has_hard_errors(self) -> bool:
        return len(self.errors) > 0

    def summary(self) -> dict:
        return {
            "valid": len(self.valid),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "duplicates": len(self.duplicates),
            "has_hard_errors": self.has_hard_errors,
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_availability_batch(
    conn: Connection,
    records: list[dict],
    skip_duplicates: bool = True,
) -> ValidationResult:
    """
    Validate a list of normalized availability records.

    Parameters
    ----------
    conn
        Open SQLAlchemy connection (read-only during validation).
    records
        Output from ``app.intake.normalizer`` — each dict has at minimum:
        student_id, week_start_date, day_of_week, start_time, end_time, level.
    skip_duplicates
        When True, duplicate records go into ``result.duplicates`` and are
        excluded from ``result.valid``.  When False, duplicates are treated as
        hard errors.

    Returns
    -------
    ValidationResult
    """
    result = ValidationResult()

    # Cache student lookups to avoid N+1 queries
    _student_cache: dict[str, dict | None] = {}

    for i, rec in enumerate(records):
        hard_errors: list[dict] = []
        soft_warnings: list[dict] = []

        # --- 1. Student existence ---
        student_id = rec.get("student_id", "")
        if student_id not in _student_cache:
            _student_cache[student_id] = _fetch_student(conn, student_id)
        student = _student_cache[student_id]

        if student is None:
            hard_errors.append({
                "index": i,
                "field": "student_id",
                "message": f"Student '{student_id}' not found",
                "record": rec,
            })
        elif not student.get("is_active", 1):
            hard_errors.append({
                "index": i,
                "field": "student_id",
                "message": f"Student '{student_id}' is inactive",
                "record": rec,
            })

        # --- 2. week_start_date is a Monday ---
        week_start_date: str = rec.get("week_start_date", "")
        try:
            wsd = date.fromisoformat(week_start_date)
            if wsd.weekday() != 0:
                hard_errors.append({
                    "index": i,
                    "field": "week_start_date",
                    "message": (
                        f"week_start_date {week_start_date!r} is not a Monday "
                        f"(weekday={wsd.weekday()})"
                    ),
                    "record": rec,
                })
        except (ValueError, TypeError):
            hard_errors.append({
                "index": i,
                "field": "week_start_date",
                "message": f"Invalid week_start_date: {week_start_date!r}",
                "record": rec,
            })
            wsd = None

        # --- 3. day_of_week ---
        dow = rec.get("day_of_week")
        if not isinstance(dow, int) or not (0 <= dow <= 6):
            hard_errors.append({
                "index": i,
                "field": "day_of_week",
                "message": f"day_of_week must be integer 0–6, got {dow!r}",
                "record": rec,
            })
            dow = None

        # --- 4. Time window validity ---
        start_time: str = rec.get("start_time", "")
        end_time: str = rec.get("end_time", "")
        s_min = e_min = None
        try:
            s_min = parse_time_minutes(start_time)
            e_min = parse_time_minutes(end_time)
            if e_min <= s_min:
                e_min += 1440
            if e_min <= s_min:
                raise ValueError("end before start")
        except (ValueError, AttributeError):
            hard_errors.append({
                "index": i,
                "field": "end_time",
                "message": f"end_time ({end_time!r}) must be after start_time ({start_time!r})",
                "record": rec,
            })

        # --- 5. level ---
        level = rec.get("level", "")
        if level not in AvailLevel.ALL:
            hard_errors.append({
                "index": i,
                "field": "level",
                "message": (
                    f"level {level!r} is invalid; must be one of {AvailLevel.ALL}"
                ),
                "record": rec,
            })

        # If there are hard errors already, no point continuing for this record
        if hard_errors:
            result.errors.extend(hard_errors)
            continue

        # --- 6. Operating-hours soft check ---
        if dow is not None and s_min is not None:
            day_name = DAY_NAMES.get(dow, "")
            hours = LIBRARY_HOURS.get(day_name)
            if hours:
                lib_open = parse_time_minutes(hours[0])
                lib_close = parse_time_minutes(hours[1])
                if lib_close <= lib_open:
                    lib_close += 1440
                if s_min < lib_open or (e_min is not None and e_min > lib_close):
                    soft_warnings.append({
                        "index": i,
                        "field": "start_time",
                        "message": (
                            f"Availability window {start_time}–{end_time} on "
                            f"{day_name} extends outside library hours "
                            f"{hours[0]}–{hours[1]}"
                        ),
                    })

        # --- 7. Duplicate detection ---
        if skip_duplicates:
            is_dup = _is_duplicate(conn, rec)
            if is_dup:
                result.duplicates.append(rec)
                continue

        # --- 8. Overnight sequence advisory ---
        if e_min is not None and is_overnight_end(e_min) and dow is not None:
            next_dow = (dow + 1) % 7
            next_day_name = DAY_NAMES.get(next_dow, "")
            # Look for a next-day record in the same batch that starts too early
            for other in records:
                if (
                    other.get("student_id") == student_id
                    and other.get("week_start_date") == week_start_date
                    and other.get("day_of_week") == next_dow
                    and other.get("level") != AvailLevel.CANNOT_WORK
                ):
                    other_s = parse_time_minutes(other.get("start_time", "00:00"))
                    if too_early_after_overnight(other_s, MORNING_CUTOFF_HOUR):
                        soft_warnings.append({
                            "index": i,
                            "field": "start_time",
                            "message": (
                                f"Overnight shift ending {end_time} followed by "
                                f"{next_day_name} shift starting "
                                f"{other['start_time']} — student may not be able "
                                f"to work before {MORNING_CUTOFF_HOUR:02d}:00"
                            ),
                        })

        result.valid.append(rec)
        result.warnings.extend(soft_warnings)

    return result


# ---------------------------------------------------------------------------
# DB insertion (after validation)
# ---------------------------------------------------------------------------

def insert_availability_batch(
    conn: Connection,
    records: list[dict],
) -> int:
    """
    Insert validated availability records into the DB.

    Parameters
    ----------
    records
        Items from ``ValidationResult.valid``.

    Returns
    -------
    Number of rows inserted.
    """
    import uuid
    from app.utils.time_utils import iso_now

    now = iso_now()
    count = 0
    for rec in records:
        avail_id = str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO availability
                    (id, student_id, week_start_date, shift_instance_id,
                     day_of_week, start_time, end_time, level,
                     submitted_at, import_source)
                VALUES
                    (:id, :sid, :wsd, :shift_id,
                     :dow, :start, :end, :level,
                     :now, :src)
            """),
            {
                "id": avail_id,
                "sid": rec["student_id"],
                "wsd": rec["week_start_date"],
                "shift_id": rec.get("shift_instance_id"),
                "dow": rec["day_of_week"],
                "start": rec["start_time"],
                "end": rec["end_time"],
                "level": rec["level"],
                "now": now,
                "src": rec.get("import_source", "api"),
            },
        )
        count += 1

    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_student(conn: Connection, student_id: str) -> dict | None:
    """Return student row as dict, or None if not found."""
    row = conn.execute(
        text("SELECT * FROM students WHERE id = :id"),
        {"id": student_id},
    ).fetchone()
    return dict(row._mapping) if row else None


def _is_duplicate(conn: Connection, rec: dict) -> bool:
    """
    Return True if an identical (student, week, day, start, end) availability
    record already exists in the DB.
    """
    row = conn.execute(
        text("""
            SELECT id FROM availability
            WHERE student_id      = :sid
              AND week_start_date = :wsd
              AND day_of_week     = :dow
              AND start_time      = :start
              AND end_time        = :end
            LIMIT 1
        """),
        {
            "sid":   rec["student_id"],
            "wsd":   rec["week_start_date"],
            "dow":   rec["day_of_week"],
            "start": rec["start_time"],
            "end":   rec["end_time"],
        },
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Compatibility alias
# ---------------------------------------------------------------------------

def validate_submission(
    student_id: str,
    week_start_date: str,
    slots: list,
    conn,
) -> "ValidationResult":
    """
    Adapter: validate a single student's availability submission.
    Returns a ValidationResult with .is_valid, .errors, .warnings.
    """
    from dataclasses import dataclass, field as dc_field

    @dataclass
    class SimpleResult:
        is_valid: bool = True
        errors: list = dc_field(default_factory=list)
        warnings: list = dc_field(default_factory=list)

    result = SimpleResult()

    # Check student exists and is active
    from sqlalchemy import text
    row = conn.execute(
        text("SELECT id, is_active FROM students WHERE id = :sid"),
        {"sid": student_id},
    ).fetchone()
    if not row:
        result.is_valid = False
        result.errors.append(f"Student {student_id} not found.")
        return result
    if not row.is_active:
        result.is_valid = False
        result.errors.append(f"Student {student_id} is not active.")
        return result

    # Check week_start_date is a Monday
    try:
        from datetime import date
        wsd = date.fromisoformat(week_start_date)
        if wsd.weekday() != 0:
            result.errors.append(f"week_start_date {week_start_date} is not a Monday.")
            result.is_valid = False
    except ValueError:
        result.errors.append(f"Invalid date: {week_start_date}")
        result.is_valid = False
        return result

    eligible_slots = [s for s in slots if s.get("level") in ("preferred", "available")]
    cannot_slots = [s for s in slots if s.get("level") == "cannot_work"]

    if len(eligible_slots) < 3:
        result.warnings.append(
            "Student has fewer than 3 available/preferred slots. "
            "They may not be able to reach their minimum hours."
        )
    if len(slots) > 0 and len(cannot_slots) == len(slots):
        result.is_valid = False
        result.errors.append("All submitted slots are cannot_work. Student has no available windows.")

    # Warn if existing availability will be replaced
    existing = conn.execute(
        text("SELECT COUNT(*) FROM availability WHERE student_id=:sid AND week_start_date=:wsd"),
        {"sid": student_id, "wsd": week_start_date},
    ).fetchone()
    if existing and existing[0] > 0:
        result.warnings.append(
            f"This submission will replace {existing[0]} existing availability slot(s)."
        )

    return result
