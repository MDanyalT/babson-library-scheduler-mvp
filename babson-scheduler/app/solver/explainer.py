"""
Post-solve explainer — annotates each assignment with a list of reason codes
that explain *why* it was made.

All logic is pure Python (no DB calls).  The function receives the data
already fetched by `runner.py` and returns the same assignments list with
the ``reason_codes`` field populated.
"""

from __future__ import annotations

import json
from typing import Optional

from app.models.db_models import PrefLevelUsed, ReasonCode
from app.utils.time_utils import effective_end_min, shifts_are_consecutive, time_str_to_minutes


def explain_assignments(
    assignments: list[dict],
    shifts: list[dict],
    students: list[dict],
    eligibility: dict[tuple[str, str], str],
    coverage_counts: Optional[dict[str, int]] = None,
) -> list[dict]:
    """
    Populate ``reason_codes`` on each assignment dict in-place and also
    return the list for convenience.

    Parameters
    ----------
    assignments
        List of assignment dicts (as built by ``runner.py`` before DB insert).
        Each dict must have ``student_id``, ``shift_instance_id``,
        ``preference_level_used``, ``is_locked``, and ``is_manual_override``.
    shifts
        All shift_instance rows for the week (list of dicts).
    students
        All active student rows (list of dicts).
    eligibility
        ``{(student_id, shift_id): "preferred" | "available"}`` — the index
        built by ``ScheduleModelBuilder``.
    coverage_counts
        Optional pre-computed ``{shift_id: count_of_eligible_students}``.
        If ``None``, it will be derived from ``eligibility``.

    Returns
    -------
    list[dict]
        The same ``assignments`` list with ``reason_codes`` fields set.
    """
    # --- Build lookup structures ---
    shift_by_id: dict[str, dict] = {sh["id"]: sh for sh in shifts}
    student_by_id: dict[str, dict] = {st["id"]: st for st in students}

    # Coverage counts per shift (how many students are eligible)
    if coverage_counts is None:
        coverage_counts = {}
        for (s_id, sh_id) in eligibility:
            coverage_counts[sh_id] = coverage_counts.get(sh_id, 0) + 1

    # Build per-student assignment lookup for CONSOLIDATED_RUN detection
    student_assignments: dict[str, list[dict]] = {}
    for asn in assignments:
        s_id = asn.get("student_id")
        if s_id:
            student_assignments.setdefault(s_id, []).append(asn)

    for asn in assignments:
        codes: list[str] = []
        s_id: Optional[str] = asn.get("student_id")
        sh_id: str = asn["shift_instance_id"]
        pref_used: str = asn.get("preference_level_used", PrefLevelUsed.UNASSIGNED)

        # --- Unassigned ---
        if s_id is None or pref_used == PrefLevelUsed.UNASSIGNED:
            asn["reason_codes"] = json.dumps([ReasonCode.UNASSIGNED])
            continue

        # --- Manual override ---
        if asn.get("is_manual_override"):
            asn["reason_codes"] = json.dumps([ReasonCode.MANUAL_OVERRIDE])
            continue

        sh = shift_by_id.get(sh_id)

        # Preference level used
        if pref_used == PrefLevelUsed.PREFERRED:
            codes.append(ReasonCode.PREFERRED_ASSIGNMENT)
        else:
            codes.append(ReasonCode.AVAILABLE_ASSIGNMENT)

        # Hard shift priority
        if sh and sh.get("is_hard_shift"):
            codes.append(ReasonCode.HARD_SHIFT_PRIORITY)

        # Scarce coverage (2 or fewer eligible students)
        eligible_count = coverage_counts.get(sh_id, 0)
        if eligible_count <= 2:
            codes.append(ReasonCode.SCARCE_COVERAGE)

        # Target hours balancing — always present for solver-placed assignments
        codes.append(ReasonCode.TARGET_HOURS_BALANCING)

        # Consolidated run — check if this student has another assigned shift on
        # the same day that is consecutive with this one
        if sh is not None:
            other_asns = student_assignments.get(s_id, [])
            sh_start = time_str_to_minutes(sh["start_time"])
            sh_end = effective_end_min(sh["start_time"], sh["end_time"])
            sh_date = sh.get("date", "")

            for other in other_asns:
                if other["shift_instance_id"] == sh_id:
                    continue
                other_sh = shift_by_id.get(other["shift_instance_id"])
                if other_sh is None:
                    continue
                if other_sh.get("date", "") != sh_date:
                    continue
                other_start = time_str_to_minutes(other_sh["start_time"])
                other_end = effective_end_min(other_sh["start_time"], other_sh["end_time"])
                if shifts_are_consecutive(other_end, sh_start) or shifts_are_consecutive(sh_end, other_start):
                    codes.append(ReasonCode.CONSOLIDATED_RUN)
                    break

        # Seniority tiebreak — add if more than one student was eligible at the
        # same preference level for this shift (meaning seniority could be the
        # deciding factor)
        same_level_count = sum(
            1
            for (eid_s, eid_sh), level in eligibility.items()
            if eid_sh == sh_id and level == pref_used
        )
        if same_level_count > 1:
            codes.append(ReasonCode.SENIORITY_TIEBREAK)

        asn["reason_codes"] = json.dumps(codes)

    return assignments
