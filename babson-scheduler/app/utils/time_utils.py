"""
Time arithmetic helpers. All internal time representation uses
total-minutes-from-midnight as integers (may exceed 1440 for next-day times).
This avoids timezone pitfalls and keeps cross-midnight arithmetic simple.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_time_minutes(t: str) -> int:
    """
    Parse 'HH:MM' or 'HH:MM+1' → total minutes from midnight.
    '+1' adds 1440 (next calendar day).
    '24:00' = 1440 (end of day, equivalent to +1 on 00:00).
    """
    next_day = t.endswith("+1")
    clean = t.replace("+1", "").strip()
    if clean == "24:00":
        return 1440
    h, m = map(int, clean.split(":"))
    total = h * 60 + m
    if next_day:
        total += 1440
    return total


def minutes_to_time_str(minutes: int) -> str:
    """Total minutes from midnight → 'HH:MM'. Wraps at 1440."""
    m = minutes % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def time_str_to_minutes(t: str) -> int:
    """'HH:MM' → minutes from midnight (0–1439, no next-day handling)."""
    h, m = map(int, t.split(":"))
    return h * 60 + m


# ---------------------------------------------------------------------------
# Effective end-minute (cross-midnight aware)
# ---------------------------------------------------------------------------

def effective_end_min(start_time: str, end_time: str) -> int:
    """
    Given start and end as 'HH:MM' strings (no +1 suffix), return the
    effective end in minutes. If end < start, assume next-day (+1440).
    """
    s = time_str_to_minutes(start_time)
    e = time_str_to_minutes(end_time)
    if e <= s:
        e += 1440
    return e


# ---------------------------------------------------------------------------
# Overlap and coverage
# ---------------------------------------------------------------------------

def slot_covers_shift(
    slot_start: int, slot_end: int,
    shift_start: int, shift_end: int,
) -> bool:
    """True if the availability slot fully covers the shift window."""
    return slot_start <= shift_start and slot_end >= shift_end


def times_overlap(
    a_start: int, a_end: int,
    b_start: int, b_end: int,
) -> bool:
    """True if [a_start, a_end) overlaps [b_start, b_end)."""
    return a_start < b_end and a_end > b_start


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def date_for_day(week_start: date, day_of_week: int) -> date:
    """Monday of week + day_of_week offset → calendar date."""
    return week_start + timedelta(days=day_of_week)


def week_dates(week_start: date) -> list[date]:
    return [week_start + timedelta(days=i) for i in range(7)]


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Consecutive shift detection
# ---------------------------------------------------------------------------

def shifts_are_consecutive(
    prev_end_min: int, next_start_min: int, tolerance_min: int = 10
) -> bool:
    """
    True if next shift starts within tolerance_min minutes of prev shift end.
    Handles same-day and next-day minute values correctly.
    """
    return abs(next_start_min - prev_end_min) <= tolerance_min


# ---------------------------------------------------------------------------
# Overnight sequence detection
# ---------------------------------------------------------------------------

def is_overnight_end(end_min: int) -> bool:
    """
    True if a shift ends between 00:00 and 02:00 of the next calendar day.
    end_min must be in the next-day range (1440 <= end_min <= 1560).
    Includes exactly 1440 (00:00+1 / midnight).
    """
    return 1440 <= end_min <= 1560   # 00:00+1 to 02:00+1


def too_early_after_overnight(start_min: int, cutoff_hour: int = 10) -> bool:
    """True if start_min is before cutoff_hour * 60 (same-day minutes)."""
    return start_min < cutoff_hour * 60
