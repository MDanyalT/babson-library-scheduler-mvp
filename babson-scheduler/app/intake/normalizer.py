"""
Availability normalizer — converts raw intake payloads (API JSON, Excel rows,
Google Form exports) into a canonical list of availability dicts ready for
validation and DB insertion.

Every output record has exactly these keys:
    student_id, week_start_date, day_of_week, start_time, end_time,
    level, import_source

The normalizer does NOT touch the database; it is a pure data-transformation
layer. Validation (constraint checking, duplicate detection, etc.) is handled
by ``app.intake.validator``.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import DAY_NAMES, DAY_OF_WEEK
from app.models.db_models import AvailLevel, ImportSource
from app.utils.time_utils import minutes_to_time_str, parse_time_minutes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_api_payload(
    payload: list[dict[str, Any]],
    student_id: str,
    week_start_date: str,
) -> tuple[list[dict], list[dict]]:
    """
    Normalize a list of availability records submitted via the REST API.

    Each element of ``payload`` should contain:
        - ``day_of_week`` (int 0–6) OR ``day_name`` (e.g. ``"monday"``)
        - ``start_time``  ``"HH:MM"``
        - ``end_time``    ``"HH:MM"`` or ``"HH:MM+1"``
        - ``level``       ``"preferred"``, ``"available"``, or ``"cannot_work"``

    Returns
    -------
    (records, errors)
        ``records`` — list of normalized availability dicts (may be empty).
        ``errors``  — list of ``{"index": i, "field": ..., "message": ...}``
                      for rows that could not be normalized.
    """
    records: list[dict] = []
    errors: list[dict] = []

    for i, item in enumerate(payload):
        try:
            rec = _normalize_single(
                item, student_id, week_start_date, ImportSource.API, index=i
            )
            records.append(rec)
        except _NormalizationError as exc:
            errors.append({"index": i, "field": exc.field, "message": exc.message})

    return records, errors


def normalize_excel_rows(
    rows: list[dict[str, Any]],
    student_id: str,
    week_start_date: str,
) -> tuple[list[dict], list[dict]]:
    """
    Normalize rows from an Excel import.

    Expected column names (case-insensitive, stripped):
        ``Day``, ``Start Time``, ``End Time``, ``Level`` / ``Availability``

    Aliases accepted:
        - Day:   ``day``, ``day_of_week``, ``weekday``
        - Start: ``start``, ``start_time``, ``from``
        - End:   ``end``, ``end_time``, ``to``
        - Level: ``level``, ``availability``, ``avail``
    """
    normalised_rows = [_normalize_excel_row_keys(r) for r in rows]
    records: list[dict] = []
    errors: list[dict] = []

    for i, item in enumerate(normalised_rows):
        try:
            rec = _normalize_single(
                item, student_id, week_start_date, ImportSource.EXCEL, index=i
            )
            records.append(rec)
        except _NormalizationError as exc:
            errors.append({"index": i, "field": exc.field, "message": exc.message})

    return records, errors


def normalize_form_export(
    rows: list[dict[str, Any]],
    student_id: str,
    week_start_date: str,
) -> tuple[list[dict], list[dict]]:
    """
    Normalize rows from a Google Form CSV export.

    Form exports often encode availability as one column per day with the
    time range and level concatenated, e.g. ``"07:30-14:00 (preferred)"``.
    Alternatively they may have separate columns that match the API shape.

    This normalizer handles both shapes:
      - If ``"day_of_week"`` / ``"day"`` is present → treated like API payload.
      - Otherwise → attempts to parse day-named columns
        (``"Monday"``, ``"Tuesday"``, …) each containing a time-range string.
    """
    records: list[dict] = []
    errors: list[dict] = []

    for i, row in enumerate(rows):
        norm_keys = {k.strip().lower(): v for k, v in row.items()}

        if "day_of_week" in norm_keys or "day" in norm_keys or "day_name" in norm_keys:
            # API-like shape
            try:
                rec = _normalize_single(
                    norm_keys, student_id, week_start_date, ImportSource.FORM, index=i
                )
                records.append(rec)
            except _NormalizationError as exc:
                errors.append({"index": i, "field": exc.field, "message": exc.message})
        else:
            # Day-column shape: one column per day
            for day_name, dow in DAY_OF_WEEK.items():
                cell_value = norm_keys.get(day_name) or norm_keys.get(day_name.capitalize())
                if not cell_value:
                    continue
                try:
                    parsed = _parse_time_range_cell(str(cell_value), day_name)
                    parsed.update({
                        "student_id": student_id,
                        "week_start_date": week_start_date,
                        "day_of_week": dow,
                        "import_source": ImportSource.FORM,
                    })
                    records.append(parsed)
                except _NormalizationError as exc:
                    errors.append({
                        "index": i,
                        "field": f"{day_name}:{exc.field}",
                        "message": exc.message,
                    })

    return records, errors


# ---------------------------------------------------------------------------
# Shared normalization core
# ---------------------------------------------------------------------------

def _normalize_single(
    item: dict[str, Any],
    student_id: str,
    week_start_date: str,
    import_source: str,
    index: int = 0,
) -> dict:
    """
    Convert one raw dict into a canonical availability record.
    Raises ``_NormalizationError`` on any field problem.
    """
    # --- day_of_week ---
    dow = _extract_day_of_week(item)

    # --- times ---
    start_raw = _get_field(item, ["start_time", "start", "from"], "start_time")
    end_raw   = _get_field(item, ["end_time", "end", "to"], "end_time")

    start_time = _normalize_time(start_raw, "start_time")
    end_time   = _normalize_time(end_raw, "end_time")

    _validate_time_order(start_time, end_time)

    # --- level ---
    level_raw = _get_field(item, ["level", "availability", "avail"], "level")
    level = _normalize_level(level_raw)

    return {
        "student_id": student_id,
        "week_start_date": week_start_date,
        "day_of_week": dow,
        "start_time": start_time,
        "end_time": end_time,
        "level": level,
        "import_source": import_source,
    }


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

def _get_field(item: dict, aliases: list[str], field_name: str) -> Any:
    """Return the value from ``item`` matching any of the given key aliases."""
    for key in aliases:
        if key in item:
            val = item[key]
            if val is not None and str(val).strip() != "":
                return val
    raise _NormalizationError(field_name, f"'{field_name}' is missing or empty")


def _extract_day_of_week(item: dict) -> int:
    """Return 0–6 from various day representations."""
    # Numeric
    for key in ("day_of_week", "day"):
        if key in item and item[key] is not None:
            val = item[key]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                dow = int(val)
                if 0 <= dow <= 6:
                    return dow
                raise _NormalizationError("day_of_week", f"day_of_week must be 0–6, got {dow}")

    # String day name
    for key in ("day_name", "day_of_week", "day", "weekday"):
        if key in item and item[key] is not None:
            raw = str(item[key]).strip().lower()
            if raw in DAY_OF_WEEK:
                return DAY_OF_WEEK[raw]
            # Try numeric string
            if raw.isdigit():
                dow = int(raw)
                if 0 <= dow <= 6:
                    return dow
                raise _NormalizationError("day_of_week", f"day_of_week must be 0–6, got {raw}")

    raise _NormalizationError("day_of_week", "day_of_week or day_name is required")


def _normalize_time(raw: Any, field_name: str) -> str:
    """
    Accept ``"HH:MM"``, ``"H:MM"``, ``"HH:MM+1"``, integer minutes, or
    ``"HH:MM AM/PM"`` and return a canonical ``"HH:MM"`` or ``"HH:MM+1"`` string.
    """
    if isinstance(raw, (int, float)):
        # Treat as minutes-from-midnight
        minutes = int(raw)
        suffix = "+1" if minutes >= 1440 else ""
        return minutes_to_time_str(minutes) + suffix

    s = str(raw).strip()

    # Already canonical with next-day marker
    if re.match(r"^\d{1,2}:\d{2}(\+1)?$", s):
        parts = s.split("+")
        hm = parts[0]
        suffix = "+1" if len(parts) > 1 else ""
        h, m = map(int, hm.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise _NormalizationError(field_name, f"Invalid time value: {s!r}")
        return f"{h:02d}:{m:02d}{suffix}"

    # 12-hour with AM/PM
    ampm_match = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", s, re.IGNORECASE)
    if ampm_match:
        h, m, meridiem = int(ampm_match.group(1)), int(ampm_match.group(2)), ampm_match.group(3).lower()
        if meridiem == "pm" and h != 12:
            h += 12
        elif meridiem == "am" and h == 12:
            h = 0
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise _NormalizationError(field_name, f"Invalid time value: {s!r}")
        return f"{h:02d}:{m:02d}"

    raise _NormalizationError(field_name, f"Cannot parse time: {s!r}")


def _validate_time_order(start: str, end: str) -> None:
    """Raise if start >= end after resolving cross-midnight."""
    s_min = parse_time_minutes(start)
    e_min = parse_time_minutes(end)
    if e_min <= s_min:
        e_min += 1440  # cross-midnight — valid
    if e_min <= s_min:
        raise _NormalizationError("end_time", "end_time must be after start_time")


def _normalize_level(raw: Any) -> str:
    """Map various level spellings to canonical AvailLevel values."""
    s = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "preferred":    AvailLevel.PREFERRED,
        "prefer":       AvailLevel.PREFERRED,
        "pref":         AvailLevel.PREFERRED,
        "p":            AvailLevel.PREFERRED,
        "available":    AvailLevel.AVAILABLE,
        "avail":        AvailLevel.AVAILABLE,
        "yes":          AvailLevel.AVAILABLE,
        "a":            AvailLevel.AVAILABLE,
        "cannot_work":  AvailLevel.CANNOT_WORK,
        "cannot":       AvailLevel.CANNOT_WORK,
        "no":           AvailLevel.CANNOT_WORK,
        "unavailable":  AvailLevel.CANNOT_WORK,
        "n":            AvailLevel.CANNOT_WORK,
        "c":            AvailLevel.CANNOT_WORK,
        "x":            AvailLevel.CANNOT_WORK,
    }
    if s in mapping:
        return mapping[s]
    raise _NormalizationError(
        "level",
        f"Invalid level {raw!r}. Must be one of: preferred, available, cannot_work",
    )


# ---------------------------------------------------------------------------
# Excel helper
# ---------------------------------------------------------------------------

_EXCEL_KEY_MAP: dict[str, str] = {
    "day":          "day_of_week",
    "day_of_week":  "day_of_week",
    "weekday":      "day_of_week",
    "day_name":     "day_of_week",
    "start":        "start_time",
    "start_time":   "start_time",
    "from":         "start_time",
    "end":          "end_time",
    "end_time":     "end_time",
    "to":           "end_time",
    "level":        "level",
    "availability": "level",
    "avail":        "level",
}


def _normalize_excel_row_keys(row: dict[str, Any]) -> dict[str, Any]:
    """Remap Excel column headers to canonical field names."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        normalized_key = k.strip().lower().replace(" ", "_")
        canonical = _EXCEL_KEY_MAP.get(normalized_key, normalized_key)
        out[canonical] = v
    return out


# ---------------------------------------------------------------------------
# Form cell parser
# ---------------------------------------------------------------------------

_TIME_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2}(?:\+1)?)\s*[-–—to]+\s*(\d{1,2}:\d{2}(?:\+1)?)"
    r"(?:\s*\(([^)]+)\))?",
    re.IGNORECASE,
)


def _parse_time_range_cell(cell: str, day_name: str) -> dict:
    """
    Parse a form cell like ``"07:30-14:00 (preferred)"`` into a partial
    availability dict (without student_id, week_start_date, day_of_week, import_source).
    """
    m = _TIME_RANGE_RE.search(cell)
    if not m:
        raise _NormalizationError("cell", f"Cannot parse time range from: {cell!r}")

    start_raw, end_raw, level_raw = m.group(1), m.group(2), m.group(3)
    start_time = _normalize_time(start_raw, "start_time")
    end_time   = _normalize_time(end_raw,   "end_time")
    level      = _normalize_level(level_raw) if level_raw else AvailLevel.AVAILABLE
    _validate_time_order(start_time, end_time)

    return {
        "start_time": start_time,
        "end_time":   end_time,
        "level":      level,
    }


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------

class _NormalizationError(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


# ---------------------------------------------------------------------------
# Compatibility aliases
# ---------------------------------------------------------------------------

def normalize_excel(
    filepath: str,
    week_start_date: str,
    student_id_column: str = "name",
) -> "NormalizeResult":
    """
    Adapter: reads a **long-format** Excel file (one row per availability slot)
    and returns a NormalizeResult-like object grouped by student.

    Expected columns (case-insensitive):
        - ``{student_id_column}``  — student identifier (UUID or name)
        - ``Day`` / ``Day of week``
        - ``Start Time`` / ``Start``
        - ``End Time`` / ``End``
        - ``Level`` / ``Availability``

    .. warning::
        This adapter is for long-format files only.  For the wide-format
        student availability matrix (one row per student, one column per
        shift window), use ``POST /api/v1/availability/import-excel`` which
        calls ``app.intake.excel_matrix.import_matrix`` instead.
    """
    import pandas as pd  # noqa: PLC0415
    from dataclasses import dataclass, field as dc_field  # noqa: PLC0415

    @dataclass
    class NormalizeResult:
        records: list = dc_field(default_factory=list)
        warnings: list = dc_field(default_factory=list)
        errors: list = dc_field(default_factory=list)
        students_found: int = 0
        slots_parsed: int = 0

    result = NormalizeResult()

    try:
        df = pd.read_excel(filepath, header=0)
    except Exception as exc:
        result.errors.append(f"Could not read Excel file: {exc}")
        return result

    rows_as_dicts = df.to_dict(orient="records")

    # Normalise column names so the lookup below is case-insensitive.
    sid_key = student_id_column.strip().lower().replace(" ", "_")

    # Group rows by student identifier.
    grouped: dict[str, list[dict]] = {}
    for raw in rows_as_dicts:
        norm = {k.strip().lower().replace(" ", "_"): v for k, v in raw.items()}
        sid_raw = norm.get(sid_key, "")
        sid = str(sid_raw).strip() if sid_raw is not None else ""
        if not sid or sid.lower() in ("none", "nan", ""):
            continue
        grouped.setdefault(sid, []).append(norm)

    for sid, sid_rows in grouped.items():
        # normalize_excel_rows expects (rows, student_id, week_start_date).
        # import_source is already hardcoded to ImportSource.EXCEL inside that function.
        normed, errs = normalize_excel_rows(sid_rows, sid, str(week_start_date))
        result.records.append({"student_id": sid, "slots": normed})
        result.errors.extend(errs)

    result.students_found = len(grouped)
    result.slots_parsed   = sum(len(r["slots"]) for r in result.records)
    return result


def parse_availability_level(cell_value) -> "str | None":
    """Alias: map a raw cell value to an availability level string."""
    try:
        return _normalize_level(cell_value)
    except Exception:
        return None


def parse_shift_column_header(header: str) -> "tuple[int, str, str] | None":
    """Alias: parse a column header like 'Monday 7:30-9:30' → (dow, start, end)."""
    if not header or not isinstance(header, str):
        return None
    import re

    DAY_MAP = {
        "mon": 0, "monday": 0,
        "tue": 1, "tuesday": 1,
        "wed": 2, "wednesday": 2,
        "thu": 3, "thursday": 3,
        "fri": 4, "friday": 4,
        "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6,
    }

    h = header.lower().strip()
    dow = None
    for key, val in DAY_MAP.items():
        if h.startswith(key):
            dow = val
            h = h[len(key):].strip()
            break

    if dow is None:
        return None

    # Extract times: match HH:MM or H:MM (with optional AM/PM)
    time_pat = r"(\d{1,2}:\d{2})\s*(?:am|pm)?"
    times = re.findall(time_pat, h, re.IGNORECASE)

    def normalize_t(t: str) -> str:
        parts = t.split(":")
        return f"{int(parts[0]):02d}:{parts[1]}"

    if len(times) >= 2:
        return dow, normalize_t(times[0]), normalize_t(times[1])
    elif len(times) == 1:
        # Default block: start + 2 hours
        from app.utils.time_utils import parse_time_minutes, minutes_to_time_str
        start_min = parse_time_minutes(normalize_t(times[0]))
        end_min = start_min + 120
        return dow, normalize_t(times[0]), minutes_to_time_str(end_min)
    return None
