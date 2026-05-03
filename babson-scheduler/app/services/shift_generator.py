"""
Shift generator service — creates shift_instance rows for a target week.

Given a week_start_date the generator:
  1. Loads all active shift_templates.
  2. Determines whether the week is an exam period (flag passed by caller or
     detected from DB if an exam_periods table exists).
  3. For every template, inserts one shift_instance per week (Monday = day 0,
     …, Sunday = day 6) with the correct calendar date, duration, and
     hard-shift flag derived from the live scheduler_config windows.
  4. Skips templates whose day_of_week produces a shift_instance that already
     exists for that week (idempotent).

Returns a summary dict with counts of created / skipped instances.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import HARD_SHIFT_WINDOWS
from app.database import get_config
from app.utils.time_utils import (
    effective_end_min,
    iso_now,
    parse_time_minutes,
    times_overlap,
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_shifts_for_week(
    conn: Connection,
    week_start_date: str,
    is_exam_period: bool = False,
    created_by: str = "system",
) -> dict:
    """
    Generate shift_instance rows for every active shift_template for the
    given week.

    Parameters
    ----------
    conn
        Open SQLAlchemy connection — caller is responsible for transaction
        lifecycle; this function calls ``conn.commit()`` once at the end.
    week_start_date
        ISO date string for the Monday that starts the target week
        (``"YYYY-MM-DD"``).
    is_exam_period
        When ``True`` all generated instances have ``is_exam_period=1``.
    created_by
        Label stored in the ``created_by`` column of any inserted template
        rows (informational only; shift_instances have no such column).

    Returns
    -------
    dict with keys ``created``, ``skipped``, ``week_start_date``.
    """
    week_start = date.fromisoformat(week_start_date)
    config = get_config(conn)

    # Hard-shift windows from live config (fall back to code-level defaults)
    hard_windows_raw: list[list[str]] = config.get("hard_shift_windows") or HARD_SHIFT_WINDOWS
    hard_windows: list[tuple[int, int]] = _parse_hard_windows(hard_windows_raw)

    # Fetch all active templates
    template_rows = conn.execute(
        text("SELECT * FROM shift_templates WHERE is_active = 1")
    ).fetchall()
    templates = [dict(row._mapping) for row in template_rows]

    # Fetch existing instances for this week (to skip duplicates)
    existing_rows = conn.execute(
        text(
            "SELECT template_id, day_of_week FROM shift_instances "
            "WHERE week_start_date = :wsd"
        ),
        {"wsd": week_start_date},
    ).fetchall()
    existing_keys: set[tuple[str | None, int]] = {
        (dict(r._mapping)["template_id"], dict(r._mapping)["day_of_week"])
        for r in existing_rows
    }

    created = 0
    skipped = 0

    for tmpl in templates:
        tmpl_id: str = tmpl["id"]
        dow: int = tmpl["day_of_week"]

        if (tmpl_id, dow) in existing_keys:
            skipped += 1
            continue

        shift_date: date = week_start + timedelta(days=dow)
        start_time: str = tmpl["start_time"]
        end_time: str = tmpl["end_time"]

        # Duration — use template value if present, else compute
        duration_hours: float = tmpl.get("duration_hours") or _compute_duration(
            start_time, end_time
        )

        is_hard = _is_hard_shift(start_time, end_time, hard_windows)

        instance_id = str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO shift_instances
                    (id, template_id, week_start_date, date, day_of_week,
                     start_time, end_time, duration_hours, is_hard_shift,
                     is_exam_period, slot_index, coverage_required)
                VALUES
                    (:id, :tid, :wsd, :date, :dow,
                     :start, :end, :dur, :hard,
                     :exam, :slot, :cov)
            """),
            {
                "id": instance_id,
                "tid": tmpl_id,
                "wsd": week_start_date,
                "date": shift_date.isoformat(),
                "dow": dow,
                "start": start_time,
                "end": end_time,
                "dur": round(duration_hours, 4),
                "hard": 1 if is_hard else 0,
                "exam": 1 if is_exam_period else 0,
                "slot": tmpl.get("slot_index", 0),
                "cov": tmpl.get("coverage_required", 1),
            },
        )
        created += 1

    conn.commit()

    return {
        "week_start_date": week_start_date,
        "created": created,
        "skipped": skipped,
        "total_templates": len(templates),
        "is_exam_period": is_exam_period,
    }


# ---------------------------------------------------------------------------
# Template management helpers
# ---------------------------------------------------------------------------

def create_shift_template(
    conn: Connection,
    day_of_week: int,
    start_time: str,
    end_time: str,
    label: str | None = None,
    created_by: str = "system",
    coverage_required: int = 1,
) -> dict:
    """
    Insert a new shift_template and return it as a dict.

    Parameters
    ----------
    day_of_week
        0 = Monday … 6 = Sunday.
    start_time / end_time
        ``"HH:MM"`` strings (no ``+1`` suffix; cross-midnight is inferred).
    """
    config = get_config(conn)
    hard_windows_raw: list[list[str]] = config.get("hard_shift_windows") or HARD_SHIFT_WINDOWS
    hard_windows = _parse_hard_windows(hard_windows_raw)

    duration_hours = _compute_duration(start_time, end_time)
    is_hard = _is_hard_shift(start_time, end_time, hard_windows)

    tmpl_id = str(uuid.uuid4())
    conn.execute(
        text("""
            INSERT INTO shift_templates
                (id, day_of_week, start_time, end_time, duration_hours,
                 is_hard_shift, label, created_by, is_active)
            VALUES
                (:id, :dow, :start, :end, :dur,
                 :hard, :label, :created_by, 1)
        """),
        {
            "id": tmpl_id,
            "dow": day_of_week,
            "start": start_time,
            "end": end_time,
            "dur": round(duration_hours, 4),
            "hard": 1 if is_hard else 0,
            "label": label,
            "created_by": created_by,
        },
    )
    conn.commit()

    row = conn.execute(
        text("SELECT * FROM shift_templates WHERE id = :id"),
        {"id": tmpl_id},
    ).fetchone()
    return dict(row._mapping)


def deactivate_shift_template(conn: Connection, template_id: str) -> bool:
    """
    Soft-delete a shift_template by setting is_active = 0.

    Returns True if a row was updated, False if the template was not found.
    """
    result = conn.execute(
        text("UPDATE shift_templates SET is_active = 0 WHERE id = :id"),
        {"id": template_id},
    )
    conn.commit()
    return result.rowcount > 0


def list_shift_templates(conn: Connection, active_only: bool = True) -> list[dict]:
    """Return all (or only active) shift templates ordered by day and start time."""
    where = "WHERE is_active = 1" if active_only else ""
    rows = conn.execute(
        text(f"SELECT * FROM shift_templates {where} ORDER BY day_of_week, start_time")
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def list_shift_instances(
    conn: Connection,
    week_start_date: str,
    day_of_week: int | None = None,
) -> list[dict]:
    """
    Return shift_instances for a week, optionally filtered by day_of_week.
    """
    if day_of_week is not None:
        rows = conn.execute(
            text(
                "SELECT * FROM shift_instances "
                "WHERE week_start_date = :wsd AND day_of_week = :dow "
                "ORDER BY day_of_week, start_time"
            ),
            {"wsd": week_start_date, "dow": day_of_week},
        ).fetchall()
    else:
        rows = conn.execute(
            text(
                "SELECT * FROM shift_instances "
                "WHERE week_start_date = :wsd "
                "ORDER BY day_of_week, start_time"
            ),
            {"wsd": week_start_date},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_duration(start_time: str, end_time: str) -> float:
    """Compute shift duration in hours, handling cross-midnight."""
    start_min = parse_time_minutes(start_time)
    end_min = parse_time_minutes(end_time)
    if end_min <= start_min:
        end_min += 1440
    return (end_min - start_min) / 60.0


def _parse_hard_windows(
    raw: list[list[str] | tuple[str, str]],
) -> list[tuple[int, int]]:
    """
    Convert raw hard-shift-window specs (``[["HH:MM", "HH:MM+1"], ...]``) to
    ``(start_min, end_min)`` integer pairs suitable for overlap testing.
    """
    result: list[tuple[int, int]] = []
    for w in raw:
        s, e = w[0], w[1]
        s_min = parse_time_minutes(s)
        e_min = parse_time_minutes(e)
        if e_min <= s_min:
            e_min += 1440
        result.append((s_min, e_min))
    return result


def _is_hard_shift(
    start_time: str,
    end_time: str,
    hard_windows: list[tuple[int, int]],
) -> bool:
    """
    Return True if this shift overlaps any hard-shift window.
    Uses plain ``"HH:MM"`` strings (no +1 suffix handled via effective_end_min).
    """
    s_min = parse_time_minutes(start_time)
    e_min = parse_time_minutes(end_time)
    if e_min <= s_min:
        e_min += 1440

    for w_start, w_end in hard_windows:
        if times_overlap(s_min, e_min, w_start, w_end):
            return True
    return False


# ---------------------------------------------------------------------------
# Compatibility aliases — used by routers and main.py
# ---------------------------------------------------------------------------

def get_instances_for_week(conn: Connection, week_start_date) -> list[dict]:
    """Alias: list shift_instances for a week."""
    wsd = week_start_date.isoformat() if hasattr(week_start_date, "isoformat") else week_start_date
    return list_shift_instances(conn, wsd)


def seed_default_templates(conn: Connection) -> int:
    """Seed library shift templates if the table is empty. Returns number inserted."""
    from app.config import LIBRARY_HOURS, HARD_SHIFT_WINDOWS, DAY_NAMES
    from app.utils.time_utils import parse_time_minutes, minutes_to_time_str, effective_end_min, date_for_day
    from app.database import get_config
    import json

    existing = conn.execute(text("SELECT COUNT(*) FROM shift_templates")).fetchone()
    if existing[0] > 0:
        return 0

    cfg = get_config(conn)
    block_min = cfg.get("shift_block_minutes", 120)
    hard_windows_raw = cfg.get("hard_shift_windows", [])
    if isinstance(hard_windows_raw, str):
        hard_windows_raw = json.loads(hard_windows_raw)

    parsed_windows = _parse_hard_windows(hard_windows_raw)

    templates = []
    for dow, day_name in DAY_NAMES.items():
        open_str, close_str = LIBRARY_HOURS[day_name]
        open_min = parse_time_minutes(open_str)
        close_min = parse_time_minutes(close_str)
        cursor = open_min
        while cursor < close_min:
            block_end = min(cursor + block_min, close_min)
            start_t = minutes_to_time_str(cursor % 1440 if cursor >= 1440 else cursor)
            end_t = minutes_to_time_str(block_end % 1440 if block_end >= 1440 else block_end)
            dur = (block_end - cursor) / 60.0
            hard = _is_hard_shift(start_t, end_t, parsed_windows)
            templates.append({
                "id": str(uuid.uuid4()),
                "day_of_week": dow,
                "start_time": start_t,
                "end_time": end_t,
                "duration_hours": dur,
                "is_hard_shift": int(hard),
                "label": None,
                "created_by": "system",
                "is_active": 1,
            })
            cursor = block_end

    if templates:
        conn.execute(text("""
            INSERT INTO shift_templates
                (id, day_of_week, start_time, end_time, duration_hours, is_hard_shift, label, created_by, is_active)
            VALUES
                (:id, :day_of_week, :start_time, :end_time, :duration_hours, :is_hard_shift, :label, :created_by, :is_active)
        """), templates)
        conn.commit()

    return len(templates)


def generate_from_templates(
    conn: Connection,
    week_start_date,
    is_exam_period: bool = False,
    slots_per_window: int = 1,
    force: bool = True,
) -> list[dict]:
    """Generate shift_instances for a week from active shift_templates."""
    from app.utils.time_utils import effective_end_min, date_for_day
    import uuid as _uuid

    wsd = week_start_date.isoformat() if hasattr(week_start_date, "isoformat") else week_start_date
    week_date = week_start_date if hasattr(week_start_date, "isoformat") else __import__("datetime").date.fromisoformat(week_start_date)

    if force:
        conn.execute(text("DELETE FROM shift_instances WHERE week_start_date = :wsd"), {"wsd": wsd})
        conn.commit()

    templates = list_shift_templates(conn, active_only=True)
    if not templates:
        return generate_from_operating_hours(conn, week_date, is_exam_period, slots_per_window, force=False)

    instances = []
    for tmpl in templates:
        cal_date = date_for_day(week_date, tmpl["day_of_week"])
        # Always recompute duration from the time strings rather than trusting
        # the stored template value.  Historical rows written by an earlier
        # generator bug stored effective_end_min() / 60 — i.e. the end-time
        # converted to decimal hours (e.g. 9.5 h for 07:30–09:30) instead of
        # the actual interval (2.0 h).  _compute_duration() is always correct.
        dur = _compute_duration(tmpl["start_time"], tmpl["end_time"])
        for slot_idx in range(slots_per_window):
            instances.append({
                "id": str(_uuid.uuid4()),
                "template_id": tmpl["id"],
                "week_start_date": wsd,
                "date": cal_date.isoformat(),
                "day_of_week": tmpl["day_of_week"],
                "start_time": tmpl["start_time"],
                "end_time": tmpl["end_time"],
                "duration_hours": round(dur, 4),
                "is_hard_shift": tmpl["is_hard_shift"],
                "is_exam_period": int(is_exam_period),
                "slot_index": slot_idx,
                "coverage_required": 1,
            })

    if instances:
        conn.execute(text("""
            INSERT INTO shift_instances
                (id, template_id, week_start_date, date, day_of_week, start_time, end_time,
                 duration_hours, is_hard_shift, is_exam_period, slot_index, coverage_required)
            VALUES
                (:id, :template_id, :week_start_date, :date, :day_of_week, :start_time, :end_time,
                 :duration_hours, :is_hard_shift, :is_exam_period, :slot_index, :coverage_required)
        """), instances)
        conn.commit()

    return instances


def generate_from_operating_hours(
    conn: Connection,
    week_start_date,
    is_exam_period: bool = False,
    slots_per_window: int = 1,
    force: bool = True,
) -> list[dict]:
    """Generate shift_instances from library operating hours (no templates required)."""
    from app.config import LIBRARY_HOURS, EXAM_LIBRARY_HOURS, DAY_NAMES
    from app.utils.time_utils import parse_time_minutes, minutes_to_time_str, date_for_day
    from app.database import get_config
    import uuid as _uuid

    wsd = week_start_date.isoformat() if hasattr(week_start_date, "isoformat") else week_start_date
    week_date = week_start_date if hasattr(week_start_date, "isoformat") else __import__("datetime").date.fromisoformat(week_start_date)

    if force:
        conn.execute(text("DELETE FROM shift_instances WHERE week_start_date = :wsd"), {"wsd": wsd})
        conn.commit()

    cfg = get_config(conn)
    block_min = cfg.get("shift_block_minutes", 120)
    hard_windows_raw = cfg.get("hard_shift_windows", [])
    if isinstance(hard_windows_raw, str):
        import json
        hard_windows_raw = json.loads(hard_windows_raw)
    parsed_windows = _parse_hard_windows(hard_windows_raw)

    hours_map = EXAM_LIBRARY_HOURS if is_exam_period else LIBRARY_HOURS
    instances = []

    for dow in range(7):
        day_name = DAY_NAMES[dow]
        open_str, close_str = hours_map[day_name]
        open_min = parse_time_minutes(open_str)
        close_min = parse_time_minutes(close_str)
        cal_date = date_for_day(week_date, dow)

        cursor = open_min
        while cursor < close_min:
            block_end = min(cursor + block_min, close_min)
            dur = (block_end - cursor) / 60.0

            start_actual = cursor % 1440 if cursor >= 1440 else cursor
            end_actual = block_end % 1440 if block_end >= 1440 else block_end
            start_t = minutes_to_time_str(start_actual)
            end_t = minutes_to_time_str(end_actual)

            from datetime import timedelta
            actual_date = cal_date if cursor < 1440 else cal_date + timedelta(days=1)

            hard = _is_hard_shift(start_t, end_t, parsed_windows)

            for slot_idx in range(slots_per_window):
                instances.append({
                    "id": str(_uuid.uuid4()),
                    "template_id": None,
                    "week_start_date": wsd,
                    "date": actual_date.isoformat(),
                    "day_of_week": dow,
                    "start_time": start_t,
                    "end_time": end_t,
                    "duration_hours": round(dur, 4),
                    "is_hard_shift": int(hard),
                    "is_exam_period": int(is_exam_period),
                    "slot_index": slot_idx,
                    "coverage_required": 1,
                })
            cursor = block_end

    if instances:
        conn.execute(text("""
            INSERT INTO shift_instances
                (id, template_id, week_start_date, date, day_of_week, start_time, end_time,
                 duration_hours, is_hard_shift, is_exam_period, slot_index, coverage_required)
            VALUES
                (:id, :template_id, :week_start_date, :date, :day_of_week, :start_time, :end_time,
                 :duration_hours, :is_hard_shift, :is_exam_period, :slot_index, :coverage_required)
        """), instances)
        conn.commit()

    return instances
