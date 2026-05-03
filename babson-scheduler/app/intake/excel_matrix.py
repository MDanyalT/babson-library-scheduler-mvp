"""
Wide-format Excel availability matrix importer.

Scheduling model — how the client Excel form drives shift instances
-------------------------------------------------------------------
Each availability column in the client's Excel form represents a
**schedulable shift window**, not merely a student preference window.
The column header encodes the shift start/end time::

    "{Day} Availability.{H:MM AM/PM} - {H:MM AM/PM}"
    e.g. "Monday Availability.07:30 AM - 09:30 AM"

When ``auto_generate_shifts=True`` (the default), every distinct
``(day_of_week, start_time, end_time)`` triple extracted from the
column headers is immediately materialised as a ``shift_instance`` row
for the target week.  This means **the client Excel form is the primary
source of truth for shift windows** — seeded templates are only a
fallback for weeks that are generated independently of an import.

Staggered / overlapping windows are intentional and must be preserved
----------------------------------------------------------------------
The library's scheduling strategy uses staggered shift windows to ensure
continuous desk coverage while students change over.  For example::

    07:30 – 09:30   (student A)
    08:30 – 10:30   (student B)

Both windows are present in the Excel form as separate columns.  The
importer creates one ``shift_instance`` per column; the solver assigns
exactly **one student per shift instance**.  The resulting overlap of
the two assigned students is the intended two-person desk coverage during
the hand-off window.

Do NOT deduplicate, merge, or filter overlapping windows.  The solver's
no-overlap constraint is per-student (a single student cannot be assigned
to two overlapping shifts), so staggered windows across different students
are fully compatible.

Expected workbook layout
------------------------
- One sheet (first/active sheet is used).
- Row 1: Column headers.
- Required header columns (case-insensitive partial match):
    "Name"
    "Seniority Level"          (optional — falls back to freshman date)
    "Number of hours …"        (optional — any header containing "hour")
- Each data row = one student.

Import flow
-----------
1. Parse all availability column headers → (dow, start_24h, end_24h).
2. If ``auto_generate_shifts=True``, call ``_ensure_shift_instances()``
   to create any missing shift_instance rows for those exact windows.
3. For each student row:
   a. Determine name, seniority level, hours preference.
   b. Upsert student record (create if new, update hours/seniority if returning).
   c. For each availability cell:
      - Map cell value → level (preferred / available / cannot_work / blank).
      - Look up the matching shift_instance row(s) for this week+window.
      - Write availability rows linked to the shift_instance IDs.
4. Commit and return MatrixImportResult with counts + warnings.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Hours band → scheduling hours mapping.
#: Per project brief: min_hours=8 and max_hours=20 are fixed system-wide constants.
#: Only target_hours (the soft scheduling objective) varies by preference band.
HOURS_BANDS: dict[str, dict[str, int]] = {
    "8-10":  {"min_hours": 8, "target_hours": 9,  "max_hours": 20},
    "10-12": {"min_hours": 8, "target_hours": 11, "max_hours": 20},
    "12-14": {"min_hours": 8, "target_hours": 13, "max_hours": 20},
    "15+":   {"min_hours": 8, "target_hours": 15, "max_hours": 20},
}

_DEFAULT_HOURS: dict[str, int] = {"min_hours": 8, "target_hours": 9, "max_hours": 20}

#: Seniority class label → employment start date (earlier = higher seniority).
SENIORITY_DATE_MAP: dict[str, str] = {
    "senior":    "2021-09-01",
    "junior":    "2022-09-01",
    "sophomore": "2023-09-01",
    "freshman":  "2024-09-01",
}

#: Numeric seniority (1 = most senior class).
_NUMERIC_SENIORITY: dict[int, str] = {
    1: "2021-09-01",
    2: "2022-09-01",
    3: "2023-09-01",
    4: "2024-09-01",
    5: "2025-09-01",
}

_DEFAULT_SENIORITY_DATE = "2024-09-01"

_DOW_MAP: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_DOW_NAMES: list[str] = list(_DOW_MAP.keys())

# Regex: "Monday Availability.07:30 AM - 09:30 AM"
# Accepts hyphens, en-dashes, em-dashes between times.
_HEADER_RE = re.compile(
    r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"\s+availability\s*[.]\s*"
    r"(\d{1,2}:\d{2}\s*(?:am|pm))"
    r"\s*[-–—]\s*"
    r"(\d{1,2}:\d{2}\s*(?:am|pm))",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class MatrixImportResult:
    students_created: int = 0
    students_updated: int = 0
    students_processed: int = 0
    availability_slots_inserted: int = 0
    unmatched_windows: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def import_matrix(
    filepath: str,
    week_start_date: str,
    conn: Connection,
    replace_existing: bool = True,
    auto_generate_shifts: bool = True,
) -> MatrixImportResult:
    """
    Parse a wide-format Excel availability matrix and write students +
    availability records to the database.

    Parameters
    ----------
    filepath:
        Path to the .xlsx workbook.
    week_start_date:
        ISO date string for the Monday of the target scheduling week
        (e.g. ``"2025-01-06"``).
    conn:
        Live SQLAlchemy connection.  The function calls ``conn.commit()``
        on success; the caller should handle rollback on exception.
    replace_existing:
        If True (default), delete any existing availability for each
        student+week before inserting new rows (atomic replacement).
    auto_generate_shifts:
        If True (default), automatically create shift instances for every
        availability window found in the Excel headers that does not already
        have a matching instance.  After creation the NULL shift_instance_id
        records are updated to point to the new instances.  This ensures all
        imported availability is solver-ready.

    Returns
    -------
    :class:`MatrixImportResult` with counts and any warnings/errors.
    """
    try:
        import openpyxl  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        result = MatrixImportResult()
        result.errors.append(f"openpyxl not installed: {exc}")
        return result

    result = MatrixImportResult()

    try:
        wb = openpyxl.load_workbook(filepath, data_only=True)
    except Exception as exc:
        result.errors.append(f"Cannot open workbook: {exc}")
        return result

    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    if not all_rows:
        result.errors.append("Workbook is empty")
        return result

    # Normalise header row to strings
    headers = [str(h).strip() if h is not None else "" for h in all_rows[0]]

    # --- locate required / optional columns ---
    name_col      = _find_col(headers, ["name"])
    seniority_col = _find_col(headers, ["seniority"])
    hours_col     = _find_col(headers, ["hour"])  # "hours you would ideally…"

    if name_col is None:
        result.errors.append("No 'Name' column found in header row")
        return result

    # --- parse availability column headers ---
    # avail_cols: list of (col_idx, dow, start_24h, end_24h)
    avail_cols: list[tuple[int, int, str, str]] = []
    for col_idx, header in enumerate(headers):
        parsed = parse_header(header)
        if parsed is not None:
            dow, start_24h, end_24h = parsed
            avail_cols.append((col_idx, dow, start_24h, end_24h))

    # --- optionally pre-create shift instances for all Excel windows ---
    # This must happen BEFORE student rows are processed so that the
    # instance lookup below finds the newly created rows.
    if auto_generate_shifts and avail_cols:
        windows = {(dow, s, e) for _, dow, s, e in avail_cols}
        _ensure_shift_instances(conn, week_start_date, windows)

    submitted_at = _iso_now()

    # --- process each student row ---
    for row_num, raw_row in enumerate(all_rows[1:], start=2):
        row = [str(v).strip() if v is not None else "" for v in raw_row]

        # Skip completely blank rows
        if not any(row):
            continue

        name = row[name_col] if name_col < len(row) else ""
        if not name or name.lower() in ("none", "nan"):
            result.warnings.append(f"Row {row_num}: skipped — no name")
            continue

        # Seniority level
        seniority_raw = (
            row[seniority_col]
            if seniority_col is not None and seniority_col < len(row)
            else ""
        )
        seniority_date = _seniority_to_date(seniority_raw)

        # Hours preference
        hours_raw = (
            row[hours_col]
            if hours_col is not None and hours_col < len(row)
            else ""
        )
        hours = parse_hours_band(hours_raw)

        # Upsert student
        try:
            student_id, created = _upsert_student(
                name, seniority_date, hours, conn, submitted_at
            )
        except Exception as exc:
            result.errors.append(f"Row {row_num} ({name!r}): failed to upsert student — {exc}")
            continue

        if created:
            result.students_created += 1
        else:
            result.students_updated += 1
        result.students_processed += 1

        # Replace existing availability for this student+week
        if replace_existing:
            conn.execute(
                text(
                    "DELETE FROM availability "
                    "WHERE student_id=:sid AND week_start_date=:wsd"
                ),
                {"sid": student_id, "wsd": week_start_date},
            )

        # Insert availability slots
        for col_idx, dow, start_24h, end_24h in avail_cols:
            cell_val = row[col_idx] if col_idx < len(row) else ""
            level = _normalize_level(cell_val)
            if level is None:
                # Blank / no-response / cannot_work — skip (no record needed)
                continue

            # Find matching shift_instance(s) for this week+day+window
            si_rows = conn.execute(
                text(
                    "SELECT id FROM shift_instances "
                    "WHERE week_start_date=:wsd "
                    "  AND day_of_week=:dow "
                    "  AND start_time=:st "
                    "  AND end_time=:et"
                ),
                {"wsd": week_start_date, "dow": dow, "st": start_24h, "et": end_24h},
            ).fetchall()

            if not si_rows:
                # auto_generate_shifts=False path: warn and record without link
                window_key = f"{_DOW_NAMES[dow]} {start_24h}-{end_24h}"
                if window_key not in result.unmatched_windows:
                    result.unmatched_windows.append(window_key)
                    result.warnings.append(
                        f"No shift instances found for {window_key} "
                        f"(week {week_start_date}) — "
                        f"availability recorded without shift link"
                    )
                _insert_avail(
                    conn, student_id, week_start_date, None,
                    dow, start_24h, end_24h, level, submitted_at,
                )
                result.availability_slots_inserted += 1
            else:
                for si_row in si_rows:
                    _insert_avail(
                        conn, student_id, week_start_date, si_row[0],
                        dow, start_24h, end_24h, level, submitted_at,
                    )
                    result.availability_slots_inserted += 1

    conn.commit()
    return result


# ---------------------------------------------------------------------------
# Public parsing helpers (unit-tested independently)
# ---------------------------------------------------------------------------

def parse_header(header: str) -> Optional[tuple[int, str, str]]:
    """
    Parse an availability column header.

    Accepts the format::

        "{Day} Availability.{H:MM AM/PM} - {H:MM AM/PM}"

    Returns ``(day_of_week, start_24h, end_24h)`` or ``None`` if the header
    does not match the availability pattern.

    Examples
    --------
    >>> parse_header("Monday Availability.07:30 AM - 09:30 AM")
    (0, '07:30', '09:30')
    >>> parse_header("Name")
    None
    """
    if not header or not isinstance(header, str):
        return None
    m = _HEADER_RE.match(header.strip())
    if not m:
        return None
    day_str  = m.group(1)
    start_24 = parse_time_ampm(m.group(2).strip())
    end_24   = parse_time_ampm(m.group(3).strip())
    return _DOW_MAP[day_str.lower()], start_24, end_24


def parse_time_ampm(time_str: str) -> str:
    """
    Convert a 12-hour time string to ``"HH:MM"`` 24-hour format.

    Special cases
    -------------
    - ``"12:xx AM"`` → ``"00:xx"``  (midnight hour)
    - ``"12:xx PM"`` → ``"12:xx"``  (noon hour — unchanged)

    Raises
    ------
    ValueError
        If the input cannot be parsed.

    Examples
    --------
    >>> parse_time_ampm("7:30 AM")
    '07:30'
    >>> parse_time_ampm("1:30 PM")
    '13:30'
    >>> parse_time_ampm("12:00 AM")
    '00:00'
    >>> parse_time_ampm("12:00 PM")
    '12:00'
    """
    s = time_str.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", s, re.IGNORECASE)
    if not m:
        raise ValueError(f"Cannot parse time: {s!r}")
    hour     = int(m.group(1))
    minute   = int(m.group(2))
    meridiem = m.group(3).lower()

    if meridiem == "am":
        if hour == 12:
            hour = 0          # 12:xx AM → midnight
    else:                      # pm
        if hour != 12:
            hour += 12         # 1–11 PM → 13–23; 12 PM stays 12

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time after conversion: {s!r}")
    return f"{hour:02d}:{minute:02d}"


def parse_hours_band(band_str: Optional[str]) -> dict[str, int]:
    """
    Map a free-text hours preference string to a scheduling hours dict.

    Recognised band keys: ``"8-10"``, ``"10-12"``, ``"12-14"``, ``"15+"``.
    Trailing noise is ignored, e.g. ``"8-10 hours per week"`` → ``"8-10"``.

    Returns the matched :data:`HOURS_BANDS` entry (copied), or
    :data:`_DEFAULT_HOURS` if unrecognised or absent.

    Examples
    --------
    >>> parse_hours_band("8-10")
    {'min_hours': 8, 'target_hours': 9, 'max_hours': 10}
    >>> parse_hours_band("15+")
    {'min_hours': 15, 'target_hours': 15, 'max_hours': 20}
    >>> parse_hours_band(None)
    {'min_hours': 8, 'target_hours': 10, 'max_hours': 20}
    """
    if not band_str or str(band_str).strip().lower() in ("", "none", "nan"):
        return dict(_DEFAULT_HOURS)

    s = str(band_str).strip()

    # Exact / prefix match for each known band key
    for key in HOURS_BANDS:
        pattern = r"^\s*" + re.escape(key) + r"(\s|$|[^0-9])"
        if re.match(pattern, s, re.IGNORECASE):
            return dict(HOURS_BANDS[key])

    # Try to extract a numeric range: "8 - 10" → "8-10"
    range_m = re.match(r"^(\d+)\s*[-–]\s*(\d+)", s)
    if range_m:
        lo, hi = int(range_m.group(1)), int(range_m.group(2))
        key = f"{lo}-{hi}"
        if key in HOURS_BANDS:
            return dict(HOURS_BANDS[key])

    # "15 +" (with space before plus) — only map the exact band value 15
    plus_m = re.match(r"^(\d+)\s*\+", s)
    if plus_m and int(plus_m.group(1)) == 15:
        return dict(HOURS_BANDS["15+"])

    return dict(_DEFAULT_HOURS)


# ---------------------------------------------------------------------------
# Shift-instance auto-generation helpers
# ---------------------------------------------------------------------------

# Hard-shift windows in minutes from midnight: (start_min, end_min inclusive).
# Late-night: 23:00–02:00+1;  early opening: 07:30–09:00.
_HARD_WINDOWS_MIN: list[tuple[int, int]] = [
    (23 * 60,      26 * 60),   # 23:00 – 02:00+1 (1380 – 1560)
    (7 * 60 + 30,  9 * 60),    # 07:30 – 09:00   (450  – 540)
]


def _window_duration_hours(start: str, end: str) -> float:
    """Return duration in hours for a HH:MM–HH:MM window (cross-midnight aware)."""
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2])   * 60 + int(end[3:])
    if e <= s:
        e += 1440
    return (e - s) / 60.0


def _window_is_hard(start: str, end: str) -> bool:
    """Return True if the window overlaps any hard-shift period."""
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2])   * 60 + int(end[3:])
    if e <= s:
        e += 1440
    for hw_s, hw_e in _HARD_WINDOWS_MIN:
        if s < hw_e and e > hw_s:
            return True
    return False


def _ensure_shift_instances(
    conn: Connection,
    week_start_date: str,
    windows: set[tuple[int, str, str]],
) -> None:
    """
    Ensure exactly one shift instance exists per (dow, start, end) window for
    the given week.  Creates missing instances; leaves existing ones untouched.

    Parameters
    ----------
    windows : set of (day_of_week, start_24h, end_24h) triples from the Excel headers.
    """
    week_dt = _date.fromisoformat(week_start_date)
    submitted_at = _iso_now()

    for dow, start, end in sorted(windows):
        existing = conn.execute(
            text(
                "SELECT id FROM shift_instances "
                "WHERE week_start_date=:wsd AND day_of_week=:dow "
                "  AND start_time=:st AND end_time=:et"
            ),
            {"wsd": week_start_date, "dow": dow, "st": start, "et": end},
        ).fetchone()

        if existing:
            continue

        shift_date = week_dt + timedelta(days=dow)
        si_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO shift_instances "
                "    (id, template_id, week_start_date, date, day_of_week, "
                "     start_time, end_time, duration_hours, is_hard_shift, "
                "     is_exam_period, slot_index, coverage_required) "
                "VALUES "
                "    (:id, NULL, :wsd, :date, :dow, "
                "     :st, :et, :dur, :hard, "
                "     0, 0, 1)"
            ),
            {
                "id":   si_id,
                "wsd":  week_start_date,
                "date": str(shift_date),
                "dow":  dow,
                "st":   start,
                "et":   end,
                "dur":  _window_duration_hours(start, end),
                "hard": int(_window_is_hard(start, end)),
            },
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _find_col(headers: list[str], keywords: list[str]) -> Optional[int]:
    """Return the index of the first header containing any keyword (case-insensitive)."""
    for idx, h in enumerate(headers):
        hl = h.lower()
        if any(kw.lower() in hl for kw in keywords):
            return idx
    return None


def _seniority_to_date(raw: str) -> str:
    """Convert a seniority label or numeric string to an ISO date string."""
    s = raw.strip().lower()
    if s in SENIORITY_DATE_MAP:
        return SENIORITY_DATE_MAP[s]
    try:
        n = int(float(s))
        if n in _NUMERIC_SENIORITY:
            return _NUMERIC_SENIORITY[n]
    except (ValueError, TypeError):
        pass
    return _DEFAULT_SENIORITY_DATE


def _normalize_name_to_email(name: str) -> str:
    """Generate a placeholder @babson.edu email from a student's full name."""
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", name).strip().lower()
    parts   = cleaned.split()
    if len(parts) >= 2:
        local = f"{parts[0]}.{parts[-1]}"
    elif parts:
        local = parts[0]
    else:
        local = str(uuid.uuid4())[:8]
    local = re.sub(r"[^a-z0-9._-]", "", local)
    return f"{local}@babson.edu"


def _normalize_level(cell_val: str) -> Optional[str]:
    """
    Map a cell value to a canonical availability level, or ``None`` if blank/
    cannot_work.

    Cells with no response, "No", "N", "X", or empty strings are returned as
    ``None`` so no availability row is written (saves DB space; the scheduler
    treats missing rows as cannot_work).

    Mappings
    --------
    - ``"Preferred"`` / ``"P"`` / ``"pref"``  → ``"preferred"``
    - ``"Yes"`` / ``"Y"`` / ``"Available"``   → ``"available"``
    - ``""`` / ``"No"`` / ``"N"`` / ``"X"``   → ``None``
    - Unrecognised non-blank                  → ``"available"`` (permissive)
    """
    s = str(cell_val).strip().lower()
    if s in ("", "none", "nan", "no", "n", "x", "cannot", "unavailable", "cannot_work"):
        return None
    if s in ("preferred", "prefer", "pref", "p"):
        return "preferred"
    if s in ("yes", "y", "available", "avail", "a"):
        return "available"
    # Non-blank unrecognised → treat as available
    return "available"


def _upsert_student(
    name: str,
    seniority_date: str,
    hours: dict[str, int],
    conn: Connection,
    submitted_at: str,
) -> tuple[str, bool]:
    """
    Find or create a student matched by name (case-insensitive).

    Returns ``(student_id, created)`` where ``created`` is True for new rows.

    Existing students have their hours and seniority_date refreshed from the
    import data.  New students get a generated ``@babson.edu`` email.
    """
    existing = conn.execute(
        text("SELECT id FROM students WHERE LOWER(name)=LOWER(:name)"),
        {"name": name},
    ).fetchone()

    if existing:
        student_id = existing[0]
        conn.execute(
            text(
                "UPDATE students "
                "SET min_hours=:min_h, target_hours=:tgt, max_hours=:max_h, "
                "    seniority_date=:sen_date "
                "WHERE id=:id"
            ),
            {
                "min_h":    hours["min_hours"],
                "tgt":      hours["target_hours"],
                "max_h":    hours["max_hours"],
                "sen_date": seniority_date,
                "id":       student_id,
            },
        )
        return student_id, False

    # New student — generate email, handle UNIQUE collisions with suffix
    student_id  = str(uuid.uuid4())
    base_email  = _normalize_name_to_email(name)
    base_local  = base_email.split("@")[0]
    email       = base_email

    for attempt in range(6):
        try:
            conn.execute(
                text(
                    "INSERT INTO students "
                    "    (id, name, email, seniority_date, "
                    "     min_hours, max_hours, target_hours, "
                    "     is_active, created_at) "
                    "VALUES "
                    "    (:id, :name, :email, :sen_date, "
                    "     :min_h, :max_h, :tgt, "
                    "     1, :created_at)"
                ),
                {
                    "id":         student_id,
                    "name":       name,
                    "email":      email,
                    "sen_date":   seniority_date,
                    "min_h":      hours["min_hours"],
                    "max_h":      hours["max_hours"],
                    "tgt":        hours["target_hours"],
                    "created_at": submitted_at,
                },
            )
            return student_id, True
        except Exception as exc:
            if "UNIQUE" in str(exc).upper() and attempt < 5:
                email = f"{base_local}{attempt + 1}@babson.edu"
            else:
                raise

    raise RuntimeError(f"Could not insert student {name!r} — email collision unresolvable")


def _insert_avail(
    conn: Connection,
    student_id: str,
    week_start_date: str,
    shift_instance_id: Optional[str],
    day_of_week: int,
    start_time: str,
    end_time: str,
    level: str,
    submitted_at: str,
) -> None:
    conn.execute(
        text(
            "INSERT INTO availability "
            "    (id, student_id, week_start_date, shift_instance_id, "
            "     day_of_week, start_time, end_time, level, "
            "     submitted_at, import_source) "
            "VALUES "
            "    (:id, :student_id, :wsd, :siid, "
            "     :dow, :st, :et, :level, "
            "     :submitted_at, 'client_excel')"
        ),
        {
            "id":         str(uuid.uuid4()),
            "student_id": student_id,
            "wsd":        week_start_date,
            "siid":       shift_instance_id,
            "dow":        day_of_week,
            "st":         start_time,
            "et":         end_time,
            "level":      level,
            "submitted_at": submitted_at,
        },
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
