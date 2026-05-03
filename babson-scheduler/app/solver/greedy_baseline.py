"""
Greedy baseline scheduler — used as fallback when CP-SAT is infeasible or
times out without a feasible solution.

Algorithm:
  1. Collect all (student, shift) eligible pairs from the availability index.
  2. Sort shifts: hard shifts first, then by coverage scarcity (ascending), then
     by date/start_time so earlier shifts are processed first.
  3. For each shift:
     a. If the shift has a locked assignment, honour it and update state.
     b. Otherwise collect eligible candidates, apply hour/consecutive/overnight
        filters, rank by preference → running_hours (ascending) → seniority, and
        assign the best candidate.
  4. Return a list of assignment dicts ready for DB insertion.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from typing import Optional

from app.config import PREFERENCE_RANK
from app.models.db_models import PrefLevelUsed, ReasonCode
from app.utils.time_utils import (
    effective_end_min,
    iso_now,
    time_str_to_minutes,
    too_early_after_overnight,
)


def greedy_schedule(
    students: list[dict],
    shifts: list[dict],
    eligibility: dict[tuple[str, str], str],
    locked_assignments: Optional[dict[str, dict]],
    config: dict,
) -> list[dict]:
    """
    Parameters
    ----------
    students
        Rows from the `students` table (dicts with `id`, `max_hours`,
        `min_hours`, `target_hours`, `seniority_date`).
    shifts
        Rows from `shift_instances` for the target week, ordered by
        date/start_time/slot_index.
    eligibility
        Mapping of ``(student_id, shift_instance_id)`` → level string
        ("preferred" | "available").  Cannot-work pairs are absent.
    locked_assignments
        ``{shift_instance_id: assignment_dict}`` for any assignments that must
        be honoured verbatim.  Pass ``None`` or ``{}`` when there are none.
    config
        Live scheduler_config row dict (from ``database.get_config``).

    Returns
    -------
    list[dict]
        One dict per shift, each ready to INSERT into ``assignments``.
    """
    if locked_assignments is None:
        locked_assignments = {}

    stagger = config.get("stagger_overlap_minutes", 5)

    # --- Index students by id for O(1) lookup ---
    student_by_id: dict[str, dict] = {s["id"]: s for s in students}

    # --- Coverage counts per shift ---
    coverage_counts: dict[str, int] = {}
    for (s_id, sh_id) in eligibility:
        coverage_counts[sh_id] = coverage_counts.get(sh_id, 0) + 1

    # --- Sort shifts: hard first, then by scarcity, then date/time ---
    def shift_sort_key(sh: dict):
        hard = 0 if sh.get("is_hard_shift") else 1   # 0 sorts before 1
        scarcity = coverage_counts.get(sh["id"], 0)
        return (hard, scarcity, sh.get("date", ""), sh.get("start_time", ""))

    sorted_shifts = sorted(shifts, key=shift_sort_key)

    # --- Per-student state ---
    running_hours: dict[str, float] = {s["id"]: 0.0 for s in students}
    # last_end_min: effective end in minutes (may exceed 1440 for overnight)
    last_end_min: dict[str, int] = {}
    # last_date: date string of most recently assigned shift
    last_date: dict[str, str] = {}

    assignments: list[dict] = []

    for sh in sorted_shifts:
        sh_id = sh["id"]
        sh_date_str: str = sh.get("date", "")
        sh_start_str: str = sh.get("start_time", "")
        sh_end_str: str = sh.get("end_time", "")
        sh_duration: float = sh.get("duration_hours", 0.0)
        sh_start_min = time_str_to_minutes(sh_start_str)
        sh_end_min = effective_end_min(sh_start_str, sh_end_str)
        is_hard = bool(sh.get("is_hard_shift"))

        # --- Locked assignment ---
        if sh_id in locked_assignments:
            locked = locked_assignments[sh_id]
            s_id_locked = locked.get("student_id")
            if s_id_locked:
                running_hours[s_id_locked] = (
                    running_hours.get(s_id_locked, 0.0) + sh_duration
                )
                last_end_min[s_id_locked] = sh_end_min
                last_date[s_id_locked] = sh_date_str
            assignments.append({
                "id": locked.get("id", str(uuid.uuid4())),
                "shift_instance_id": sh_id,
                "student_id": s_id_locked,
                "preference_level_used": locked.get(
                    "preference_level_used", PrefLevelUsed.UNASSIGNED
                ),
                "reason_codes": locked.get(
                    "reason_codes",
                    json.dumps([ReasonCode.GREEDY_FALLBACK]),
                ),
                "is_locked": 1,
                "is_manual_override": locked.get("is_manual_override", 0),
                "override_reason": locked.get("override_reason"),
                "assigned_at": locked.get("assigned_at", iso_now()),
            })
            continue

        # --- Build candidate list from eligibility ---
        candidates: list[tuple[str, str]] = []  # [(student_id, level), ...]
        for (s_id, e_sh_id), level in eligibility.items():
            if e_sh_id == sh_id:
                candidates.append((s_id, level))

        # --- Apply filters ---
        filtered: list[tuple[str, str]] = []
        for (s_id, level) in candidates:
            student = student_by_id.get(s_id)
            if student is None:
                continue

            # Max hours check — use per-student max if available
            s_max = student.get("max_hours", config.get("max_hours_default", 20))
            if running_hours.get(s_id, 0.0) + sh_duration > s_max:
                continue

            # Overnight rest constraint:
            # If last shift for this student ended overnight (end_min > 1440)
            # and that shift was on the previous calendar day relative to this
            # shift, and this shift starts too early → skip.
            if s_id in last_end_min and s_id in last_date:
                prev_end = last_end_min[s_id]
                prev_date_str = last_date[s_id]
                if (
                    prev_end > 1440
                    and _is_previous_day(prev_date_str, sh_date_str)
                    and too_early_after_overnight(sh_start_min)
                ):
                    continue

            filtered.append((s_id, level))

        # --- Rank: preference DESC, running_hours ASC, seniority ASC ---
        def candidate_rank(item: tuple[str, str]):
            s_id, level = item
            pref_score = PREFERENCE_RANK.get(level, 0)
            hours_so_far = running_hours.get(s_id, 0.0)
            seniority_str = student_by_id[s_id].get("seniority_date", "9999-01-01")
            return (-pref_score, hours_so_far, seniority_str)

        filtered.sort(key=candidate_rank)

        if filtered:
            best_student_id, best_level = filtered[0]
            running_hours[best_student_id] = (
                running_hours.get(best_student_id, 0.0) + sh_duration
            )
            last_end_min[best_student_id] = sh_end_min
            last_date[best_student_id] = sh_date_str

            assignments.append({
                "id": str(uuid.uuid4()),
                "shift_instance_id": sh_id,
                "student_id": best_student_id,
                "preference_level_used": best_level,
                "reason_codes": json.dumps([ReasonCode.GREEDY_FALLBACK]),
                "is_locked": 0,
                "is_manual_override": 0,
                "override_reason": None,
                "assigned_at": iso_now(),
            })
        else:
            # Unassigned
            assignments.append({
                "id": str(uuid.uuid4()),
                "shift_instance_id": sh_id,
                "student_id": None,
                "preference_level_used": PrefLevelUsed.UNASSIGNED,
                "reason_codes": json.dumps([ReasonCode.UNASSIGNED]),
                "is_locked": 0,
                "is_manual_override": 0,
                "override_reason": None,
                "assigned_at": iso_now(),
            })

    return assignments


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_previous_day(prev_date_str: str, current_date_str: str) -> bool:
    """Return True if prev_date is exactly one calendar day before current_date."""
    try:
        prev = date.fromisoformat(prev_date_str)
        curr = date.fromisoformat(current_date_str)
        return (curr - prev).days == 1
    except (ValueError, TypeError):
        return False
