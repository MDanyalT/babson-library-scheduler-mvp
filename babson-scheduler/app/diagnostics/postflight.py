"""
Postflight diagnostics — run AFTER the solver to audit the produced schedule
and generate a ``violations`` snapshot.

Checks performed
----------------
1. UNFILLED_SHIFT  (hard)
   Any shift_instance with no assigned student.

2. STUDENT_OVER_MAXIMUM  (hard)
   Any student whose total assigned hours exceed their max_hours for the week.

3. CONSECUTIVE_HOURS_EXCEEDED  (hard)
   Any student assigned to back-to-back shifts totalling more than
   max_consecutive_hours without a break.

4. COVERAGE_GAP  (hard)
   Any shift where actual assignments < coverage_required.

5. STUDENT_UNDER_MINIMUM  (soft)
   Students assigned fewer hours than min_hours.

6. TARGET_HOURS_MISSED  (soft)
   Students whose assigned hours deviate from target_hours by more than
   a tolerance threshold.

7. BAD_SEQUENCE_OVERNIGHT  (soft)
   Students assigned an overnight shift and a next-morning shift that starts
   before MORNING_CUTOFF_HOUR.

8. PREFERENCE_VIOLATED  (soft)
   Assignments made at a lower preference level than the student marked.

9. LOW_PREFERENCE_FILL  (soft)
   Shifts filled only via "available" (not "preferred") for every slot.

Results are inserted into the ``violations`` table and a
``diagnostics_snapshots`` row is created with snapshot_type="postflight".
Returns a ``PostflightReport``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import MORNING_CUTOFF_HOUR, OVERNIGHT_END_HOUR
from app.database import get_config
from app.models.db_models import AvailLevel, DiagSource, ViolationType
from app.utils.time_utils import (
    effective_end_min,
    is_overnight_end,
    iso_now,
    parse_time_minutes,
    shifts_are_consecutive,
    too_early_after_overnight,
)


# ---------------------------------------------------------------------------
# Tolerance
# ---------------------------------------------------------------------------

TARGET_HOURS_TOLERANCE: float = 1.0   # hours deviation allowed before flagging


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class PostflightViolation:
    violation_type: str
    severity: str                  # "hard" | "soft"
    shift_instance_id: str | None
    student_id: str | None
    description: str


@dataclass
class PostflightReport:
    run_id: str
    week_start_date: str
    snapshot_id: str
    violations: list[PostflightViolation]
    blocks_publish: bool           # True if any hard violation is present

    def hard_violations(self) -> list[PostflightViolation]:
        return [v for v in self.violations if v.severity == "hard"]

    def soft_violations(self) -> list[PostflightViolation]:
        return [v for v in self.violations if v.severity == "soft"]

    def summary(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "week_start_date": self.week_start_date,
            "blocks_publish": self.blocks_publish,
            "total_violations": len(self.violations),
            "hard": len(self.hard_violations()),
            "soft": len(self.soft_violations()),
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_postflight(
    conn: Connection,
    run_id: str,
    week_start_date: str,
) -> PostflightReport:
    """
    Audit the schedule produced by ``run_id`` and persist violations.

    Parameters
    ----------
    conn
        Open SQLAlchemy connection.
    run_id
        The schedule_runs.id to audit.
    week_start_date
        ISO date string for Monday of the target week.

    Returns
    -------
    PostflightReport
    """
    config = get_config(conn)
    violations: list[PostflightViolation] = []

    # --- Load data ---
    students = _load_students(conn)
    shifts = _load_shifts(conn, week_start_date)
    assignments = _load_assignments(conn, run_id)
    availability = _load_availability(conn, week_start_date)

    # Build lookup maps
    shift_by_id: dict[str, dict] = {sh["id"]: sh for sh in shifts}
    student_by_id: dict[str, dict] = {st["id"]: st for st in students}

    # Assignments per student and per shift
    asns_by_student: dict[str, list[dict]] = {}
    asns_by_shift: dict[str, list[dict]] = {}
    for asn in assignments:
        sid = asn.get("student_id")
        shid = asn["shift_instance_id"]
        if sid:
            asns_by_student.setdefault(sid, []).append(asn)
        asns_by_shift.setdefault(shid, []).append(asn)

    # Availability level by (student, shift)
    avail_level_map: dict[tuple[str, str], str] = {}
    for av in availability:
        if av["level"] == AvailLevel.CANNOT_WORK:
            continue
        for sh in shifts:
            if sh["day_of_week"] != av["day_of_week"]:
                continue
            sh_s = parse_time_minutes(sh["start_time"])
            sh_e = parse_time_minutes(sh["end_time"])
            if sh_e <= sh_s:
                sh_e += 1440
            av_s = parse_time_minutes(av["start_time"])
            av_e = parse_time_minutes(av["end_time"])
            if av_e <= av_s:
                av_e += 1440
            if av_s <= sh_s and av_e >= sh_e:
                key = (av["student_id"], sh["id"])
                # Keep the higher-preference level if there are multiple windows
                existing = avail_level_map.get(key)
                if existing is None or (
                    existing == AvailLevel.AVAILABLE and av["level"] == AvailLevel.PREFERRED
                ):
                    avail_level_map[key] = av["level"]

    # --- Run checks ---
    violations += _check_unfilled_shifts(shifts, asns_by_shift)
    violations += _check_coverage_gaps(shifts, asns_by_shift)
    violations += _check_student_over_max(students, asns_by_student, shift_by_id, config)
    violations += _check_consecutive_hours(students, asns_by_student, shift_by_id, config)
    violations += _check_overlapping_assignments(students, asns_by_student, shift_by_id)
    violations += _check_student_under_min(students, asns_by_student, shift_by_id, config)
    violations += _check_target_hours_missed(students, asns_by_student, shift_by_id)
    violations += _check_overnight_sequences(students, asns_by_student, shift_by_id)
    violations += _check_preference_violations(assignments, avail_level_map)
    violations += _check_low_preference_fill(shifts, asns_by_shift, avail_level_map)

    # --- Persist violations ---
    _persist_violations(conn, run_id, violations)

    # --- Persist snapshot ---
    snapshot_id = str(uuid.uuid4())
    _persist_snapshot(conn, snapshot_id, week_start_date, run_id, violations)

    blocks_publish = any(v.severity == "hard" for v in violations)

    return PostflightReport(
        run_id=run_id,
        week_start_date=week_start_date,
        snapshot_id=snapshot_id,
        violations=violations,
        blocks_publish=blocks_publish,
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_unfilled_shifts(
    shifts: list[dict],
    asns_by_shift: dict[str, list[dict]],
) -> list[PostflightViolation]:
    violations = []
    for sh in shifts:
        asns = asns_by_shift.get(sh["id"], [])
        filled = [a for a in asns if a.get("student_id")]
        if not filled:
            violations.append(PostflightViolation(
                violation_type=ViolationType.UNFILLED_SHIFT,
                severity=ViolationType.severity(ViolationType.UNFILLED_SHIFT),
                shift_instance_id=sh["id"],
                student_id=None,
                description=(
                    f"Shift {sh.get('label') or sh['id']} "
                    f"({sh['start_time']}–{sh['end_time']}) has no assigned worker."
                ),
            ))
    return violations


def _check_coverage_gaps(
    shifts: list[dict],
    asns_by_shift: dict[str, list[dict]],
) -> list[PostflightViolation]:
    violations = []
    for sh in shifts:
        required = sh.get("coverage_required", 1)
        asns = asns_by_shift.get(sh["id"], [])
        filled = [a for a in asns if a.get("student_id")]
        if len(filled) < required:
            violations.append(PostflightViolation(
                violation_type=ViolationType.COVERAGE_GAP,
                severity=ViolationType.severity(ViolationType.COVERAGE_GAP),
                shift_instance_id=sh["id"],
                student_id=None,
                description=(
                    f"Shift {sh.get('label') or sh['id']} needs {required} worker(s) "
                    f"but only {len(filled)} assigned."
                ),
            ))
    return violations


def _check_student_over_max(
    students: list[dict],
    asns_by_student: dict[str, list[dict]],
    shift_by_id: dict[str, dict],
    config: dict,
) -> list[PostflightViolation]:
    violations = []
    for st in students:
        asns = asns_by_student.get(st["id"], [])
        total_hours = _sum_hours(asns, shift_by_id)
        max_h = st.get("max_hours") or config.get("max_hours_default", 20)
        if total_hours > max_h:
            violations.append(PostflightViolation(
                violation_type=ViolationType.STUDENT_OVER_MAXIMUM,
                severity=ViolationType.severity(ViolationType.STUDENT_OVER_MAXIMUM),
                shift_instance_id=None,
                student_id=st["id"],
                description=(
                    f"Student '{st['name']}' assigned {total_hours:.1f}h, "
                    f"exceeding their maximum of {max_h}h."
                ),
            ))
    return violations


def _check_consecutive_hours(
    students: list[dict],
    asns_by_student: dict[str, list[dict]],
    shift_by_id: dict[str, dict],
    config: dict,
) -> list[PostflightViolation]:
    max_consec = config.get("max_consecutive_hours", 6)
    violations = []

    for st in students:
        asns = asns_by_student.get(st["id"], [])
        if not asns:
            continue

        # Sort assigned shifts by day then start time
        assigned_shifts = sorted(
            [shift_by_id[a["shift_instance_id"]] for a in asns if a["shift_instance_id"] in shift_by_id],
            key=lambda sh: (sh["day_of_week"], parse_time_minutes(sh["start_time"])),
        )

        # Sliding window: accumulate consecutive hours
        run_hours = 0.0
        run_start_sh: dict | None = None
        prev_sh: dict | None = None

        for sh in assigned_shifts:
            sh_s = parse_time_minutes(sh["start_time"])
            sh_e = parse_time_minutes(sh["end_time"])
            if sh_e <= sh_s:
                sh_e += 1440

            if prev_sh is None:
                run_hours = (sh_e - sh_s) / 60.0
                run_start_sh = sh
            else:
                prev_e = parse_time_minutes(prev_sh["end_time"])
                if prev_sh["end_time"].endswith("+1"):
                    prev_e += 1440
                elif parse_time_minutes(prev_sh["end_time"]) <= parse_time_minutes(prev_sh["start_time"]):
                    prev_e += 1440

                same_or_next_day = (
                    sh["day_of_week"] == prev_sh["day_of_week"]
                    or sh["day_of_week"] == (prev_sh["day_of_week"] + 1) % 7
                )

                if same_or_next_day and shifts_are_consecutive(prev_e, sh_s, tolerance_min=15):
                    run_hours += (sh_e - sh_s) / 60.0
                else:
                    run_hours = (sh_e - sh_s) / 60.0
                    run_start_sh = sh

                if run_hours > max_consec:
                    violations.append(PostflightViolation(
                        violation_type=ViolationType.CONSECUTIVE_HOURS_EXCEEDED,
                        severity=ViolationType.severity(ViolationType.CONSECUTIVE_HOURS_EXCEEDED),
                        shift_instance_id=sh["id"],
                        student_id=st["id"],
                        description=(
                            f"Student '{st['name']}' has {run_hours:.1f}h of consecutive "
                            f"work (max: {max_consec}h), ending at shift "
                            f"{sh.get('label') or sh['id']}."
                        ),
                    ))
                    # Reset to avoid duplicate violations for the same run
                    run_hours = (sh_e - sh_s) / 60.0
                    run_start_sh = sh

            prev_sh = sh

    return violations


def _check_overlapping_assignments(
    students: list[dict],
    asns_by_student: dict[str, list[dict]],
    shift_by_id: dict[str, dict],
) -> list[PostflightViolation]:
    """
    Hard check: flag any student assigned to two shifts whose time windows
    strictly overlap.  Uses absolute week-minutes (dow * 1440 + minutes) so
    cross-midnight shifts are handled correctly.  Adjacent shifts that only
    touch at a single point are NOT flagged.
    """
    from app.utils.time_utils import effective_end_min, time_str_to_minutes

    violations = []
    for st in students:
        asns = asns_by_student.get(st["id"], [])
        assigned_shifts = [
            shift_by_id[a["shift_instance_id"]]
            for a in asns
            if a["shift_instance_id"] in shift_by_id
        ]
        if len(assigned_shifts) < 2:
            continue

        # Build absolute-minute intervals
        intervals: list[tuple[int, int, dict]] = []
        for sh in assigned_shifts:
            base = sh["day_of_week"] * 1440
            abs_s = base + time_str_to_minutes(sh["start_time"])
            abs_e = base + effective_end_min(sh["start_time"], sh["end_time"])
            intervals.append((abs_s, abs_e, sh))

        # Check every pair for strict overlap
        reported_pairs: set[frozenset] = set()
        for i in range(len(intervals)):
            a_s, a_e, sh_a = intervals[i]
            for j in range(i + 1, len(intervals)):
                b_s, b_e, sh_b = intervals[j]
                pair = frozenset([sh_a["id"], sh_b["id"]])
                if pair in reported_pairs:
                    continue
                if a_s < b_e and b_s < a_e:
                    reported_pairs.add(pair)
                    violations.append(PostflightViolation(
                        violation_type=ViolationType.OVERLAPPING_ASSIGNMENT,
                        severity=ViolationType.severity(ViolationType.OVERLAPPING_ASSIGNMENT),
                        shift_instance_id=sh_a["id"],
                        student_id=st["id"],
                        description=(
                            f"Student '{st['name']}' is assigned to overlapping shifts: "
                            f"{sh_a['start_time']}–{sh_a['end_time']} and "
                            f"{sh_b['start_time']}–{sh_b['end_time']} "
                            f"on day {sh_a['day_of_week']} / day {sh_b['day_of_week']}."
                        ),
                    ))
    return violations


def _check_student_under_min(
    students: list[dict],
    asns_by_student: dict[str, list[dict]],
    shift_by_id: dict[str, dict],
    config: dict,
) -> list[PostflightViolation]:
    violations = []
    for st in students:
        asns = asns_by_student.get(st["id"], [])
        total_hours = _sum_hours(asns, shift_by_id)
        min_h = st.get("min_hours") or config.get("min_hours_default", 8)
        if total_hours < min_h:
            violations.append(PostflightViolation(
                violation_type=ViolationType.STUDENT_UNDER_MINIMUM,
                severity=ViolationType.severity(ViolationType.STUDENT_UNDER_MINIMUM),
                shift_instance_id=None,
                student_id=st["id"],
                description=(
                    f"Student '{st['name']}' assigned {total_hours:.1f}h, "
                    f"below their minimum of {min_h}h."
                ),
            ))
    return violations


def _check_target_hours_missed(
    students: list[dict],
    asns_by_student: dict[str, list[dict]],
    shift_by_id: dict[str, dict],
) -> list[PostflightViolation]:
    violations = []
    for st in students:
        asns = asns_by_student.get(st["id"], [])
        total_hours = _sum_hours(asns, shift_by_id)
        target = st.get("target_hours", 0)
        if target and abs(total_hours - target) > TARGET_HOURS_TOLERANCE:
            violations.append(PostflightViolation(
                violation_type=ViolationType.TARGET_HOURS_MISSED,
                severity=ViolationType.severity(ViolationType.TARGET_HOURS_MISSED),
                shift_instance_id=None,
                student_id=st["id"],
                description=(
                    f"Student '{st['name']}' assigned {total_hours:.1f}h vs "
                    f"target {target}h (delta: {total_hours - target:+.1f}h)."
                ),
            ))
    return violations


def _check_overnight_sequences(
    students: list[dict],
    asns_by_student: dict[str, list[dict]],
    shift_by_id: dict[str, dict],
) -> list[PostflightViolation]:
    violations = []
    for st in students:
        asns = asns_by_student.get(st["id"], [])
        shifts_assigned = sorted(
            [shift_by_id[a["shift_instance_id"]] for a in asns if a["shift_instance_id"] in shift_by_id],
            key=lambda sh: (sh["day_of_week"], parse_time_minutes(sh["start_time"])),
        )

        for sh in shifts_assigned:
            e_str = sh["end_time"]
            e_min = parse_time_minutes(e_str)
            if not is_overnight_end(e_min):
                # Also check same-day cross-midnight
                s_min = parse_time_minutes(sh["start_time"])
                raw_e = parse_time_minutes(e_str.replace("+1", ""))
                if raw_e <= s_min:
                    e_min = raw_e + 1440
                    if not is_overnight_end(e_min):
                        continue
                else:
                    continue

            next_dow = (sh["day_of_week"] + 1) % 7
            next_day_shifts = [
                s for s in shifts_assigned
                if s["day_of_week"] == next_dow
            ]
            for nsh in next_day_shifts:
                ns_min = parse_time_minutes(nsh["start_time"])
                if too_early_after_overnight(ns_min, MORNING_CUTOFF_HOUR):
                    violations.append(PostflightViolation(
                        violation_type=ViolationType.BAD_SEQUENCE_OVERNIGHT,
                        severity=ViolationType.severity(ViolationType.BAD_SEQUENCE_OVERNIGHT),
                        shift_instance_id=nsh["id"],
                        student_id=st["id"],
                        description=(
                            f"Student '{st['name']}' works overnight until {e_str} "
                            f"then starts at {nsh['start_time']} the next day "
                            f"(before {MORNING_CUTOFF_HOUR:02d}:00 cutoff)."
                        ),
                    ))
    return violations


def _check_preference_violations(
    assignments: list[dict],
    avail_level_map: dict[tuple[str, str], str],
) -> list[PostflightViolation]:
    violations = []
    for asn in assignments:
        sid = asn.get("student_id")
        shid = asn["shift_instance_id"]
        if not sid:
            continue
        pref_used = asn.get("preference_level_used", "")
        actual_avail = avail_level_map.get((sid, shid))
        if actual_avail == AvailLevel.PREFERRED and pref_used == AvailLevel.AVAILABLE:
            violations.append(PostflightViolation(
                violation_type=ViolationType.PREFERENCE_VIOLATED,
                severity=ViolationType.severity(ViolationType.PREFERENCE_VIOLATED),
                shift_instance_id=shid,
                student_id=sid,
                description=(
                    f"Student {sid} was assigned to shift {shid} but marked it as "
                    f"'preferred' — assignment recorded as 'available'."
                ),
            ))
    return violations


def _check_low_preference_fill(
    shifts: list[dict],
    asns_by_shift: dict[str, list[dict]],
    avail_level_map: dict[tuple[str, str], str],
) -> list[PostflightViolation]:
    violations = []
    for sh in shifts:
        asns = [a for a in asns_by_shift.get(sh["id"], []) if a.get("student_id")]
        if not asns:
            continue
        all_available = all(
            avail_level_map.get((a["student_id"], sh["id"])) != AvailLevel.PREFERRED
            for a in asns
        )
        if all_available:
            violations.append(PostflightViolation(
                violation_type=ViolationType.LOW_PREFERENCE_FILL,
                severity=ViolationType.severity(ViolationType.LOW_PREFERENCE_FILL),
                shift_instance_id=sh["id"],
                student_id=None,
                description=(
                    f"Shift {sh.get('label') or sh['id']} "
                    f"({sh['start_time']}–{sh['end_time']}) is filled entirely by "
                    f"'available' (not 'preferred') workers."
                ),
            ))
    return violations


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _load_students(conn: Connection) -> list[dict]:
    rows = conn.execute(
        text("SELECT * FROM students WHERE is_active = 1")
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _load_shifts(conn: Connection, week_start_date: str) -> list[dict]:
    rows = conn.execute(
        text(
            "SELECT * FROM shift_instances WHERE week_start_date = :wsd "
            "ORDER BY day_of_week, start_time"
        ),
        {"wsd": week_start_date},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _load_assignments(conn: Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        text("SELECT * FROM assignments WHERE run_id = :rid"),
        {"rid": run_id},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _load_availability(conn: Connection, week_start_date: str) -> list[dict]:
    rows = conn.execute(
        text("SELECT * FROM availability WHERE week_start_date = :wsd"),
        {"wsd": week_start_date},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _sum_hours(asns: list[dict], shift_by_id: dict[str, dict]) -> float:
    return sum(
        shift_by_id[a["shift_instance_id"]]["duration_hours"]
        for a in asns
        if a["shift_instance_id"] in shift_by_id
    )


def _persist_violations(
    conn: Connection,
    run_id: str,
    violations: list[PostflightViolation],
) -> None:
    now = iso_now()
    for v in violations:
        conn.execute(
            text("""
                INSERT INTO violations
                    (id, run_id, violation_type, shift_instance_id,
                     student_id, description, severity, source)
                VALUES
                    (:id, :run_id, :vtype, :shift_id,
                     :student_id, :desc, :sev, :src)
            """),
            {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "vtype": v.violation_type,
                "shift_id": v.shift_instance_id,
                "student_id": v.student_id,
                "desc": v.description,
                "sev": v.severity,
                "src": DiagSource.POSTFLIGHT,
            },
        )
    conn.commit()


def _persist_snapshot(
    conn: Connection,
    snapshot_id: str,
    week_start_date: str,
    run_id: str,
    violations: list[PostflightViolation],
) -> None:
    payload = [
        {
            "violation_type": v.violation_type,
            "severity": v.severity,
            "shift_instance_id": v.shift_instance_id,
            "student_id": v.student_id,
            "description": v.description,
        }
        for v in violations
    ]
    conn.execute(
        text("""
            INSERT INTO diagnostics_snapshots
                (id, week_start_date, run_id, snapshot_type, findings, created_at)
            VALUES
                (:id, :wsd, :run_id, :stype, :findings, :now)
        """),
        {
            "id": snapshot_id,
            "wsd": week_start_date,
            "run_id": run_id,
            "stype": DiagSource.POSTFLIGHT,
            "findings": json.dumps(payload),
            "now": iso_now(),
        },
    )
    conn.commit()
