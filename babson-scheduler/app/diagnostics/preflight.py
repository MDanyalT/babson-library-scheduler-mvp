"""
Preflight diagnostics — run BEFORE the solver to surface data-quality and
coverage-feasibility issues for a given week.

Checks performed
----------------
1. MISSING_SUBMISSIONS
   Students who have submitted zero availability records for the week.

2. SHIFT_ZERO_COVERAGE   (hard)
   Shift instances for which NO eligible student has submitted availability.

3. SHIFT_LOW_COVERAGE    (soft)
   Shift instances where eligible headcount < coverage_required * LOW_COVERAGE_RATIO.

4. HARD_SHIFT_COVERAGE_RISK  (soft)
   Hard-shift instances with fewer eligible workers than a configurable threshold.

5. INSUFFICIENT_TOTAL_COVERAGE  (hard)
   Aggregate check: total available student-hours < total required shift-hours.

6. STUDENT_INSUFFICIENT_AVAIL  (soft)
   Students whose submitted availability hours fall below their own min_hours.

7. OVERNIGHT_RISK  (soft)
   Students who closed an overnight shift and submitted an early start the next
   day (violates the MORNING_CUTOFF_HOUR rest rule).

8. EXAM_PERIOD_GAP  (hard, only when is_exam_period=True)
   Any hour in the 24-hour exam schedule with zero coverage.

The function writes a ``diagnostics_snapshots`` row and returns a structured
``PreflightReport`` dataclass.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import (
    DAY_NAMES,
    MORNING_CUTOFF_HOUR,
    OVERNIGHT_END_HOUR,
)
from app.database import get_config
from app.models.db_models import DiagnosticType, DiagSource
from app.utils.time_utils import (
    effective_end_min,
    is_overnight_end,
    iso_now,
    parse_time_minutes,
    too_early_after_overnight,
    times_overlap,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOW_COVERAGE_RATIO: float = 1.5    # flag when eligible < required * this
HARD_SHIFT_MIN_ELIGIBLE: int = 2   # flag hard shifts with fewer than this many eligible workers


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class PreflightFinding:
    diagnostic_type: str
    severity: str              # "hard" | "soft"
    shift_instance_id: str | None
    student_id: str | None
    day_of_week: int | None
    message: str
    metadata: dict = field(default_factory=dict)


@dataclass
class PreflightReport:
    week_start_date: str
    snapshot_id: str
    is_exam_period: bool
    findings: list[PreflightFinding]
    ok_to_solve: bool          # False if any hard finding is present

    def hard_findings(self) -> list[PreflightFinding]:
        return [f for f in self.findings if f.severity == "hard"]

    def soft_findings(self) -> list[PreflightFinding]:
        return [f for f in self.findings if f.severity == "soft"]

    def summary(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "week_start_date": self.week_start_date,
            "ok_to_solve": self.ok_to_solve,
            "total_findings": len(self.findings),
            "hard": len(self.hard_findings()),
            "soft": len(self.soft_findings()),
            "is_exam_period": self.is_exam_period,
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_preflight(
    conn: Connection,
    week_start_date: str,
    is_exam_period: bool = False,
    run_id: str | None = None,
) -> PreflightReport:
    """
    Execute all preflight checks for the given week and persist the results.

    Parameters
    ----------
    conn
        Open SQLAlchemy connection.
    week_start_date
        ISO date string for Monday of the target week.
    is_exam_period
        When True, exam-period gap checks are also run.
    run_id
        Optional associated schedule_run UUID (may be None at preflight time).

    Returns
    -------
    PreflightReport
    """
    config = get_config(conn)
    findings: list[PreflightFinding] = []

    # --- Load data ---
    students = _load_students(conn)
    shifts = _load_shifts(conn, week_start_date)
    availability = _load_availability(conn, week_start_date)

    # Build lookup structures
    avail_by_student: dict[str, list[dict]] = {}
    for av in availability:
        avail_by_student.setdefault(av["student_id"], []).append(av)

    # Eligibility: which students can cover each shift?
    eligibility_by_shift: dict[str, list[str]] = {sh["id"]: [] for sh in shifts}
    for av in availability:
        if av["level"] == "cannot_work":
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
            # Student is eligible if their window covers the entire shift
            if av_s <= sh_s and av_e >= sh_e:
                eligibility_by_shift[sh["id"]].append(av["student_id"])

    # --- Run checks ---
    findings += _check_missing_submissions(students, avail_by_student)
    findings += _check_shift_coverage(shifts, eligibility_by_shift, config)
    findings += _check_total_coverage(students, shifts, availability, config)
    findings += _check_student_hours(students, avail_by_student, config)
    findings += _check_overnight_risk(students, avail_by_student)

    if is_exam_period:
        findings += _check_exam_period_gaps(shifts, eligibility_by_shift)

    # --- Persist snapshot ---
    snapshot_id = str(uuid.uuid4())
    ok_to_solve = not any(f.severity == "hard" for f in findings)

    _persist_snapshot(
        conn=conn,
        snapshot_id=snapshot_id,
        week_start_date=week_start_date,
        run_id=run_id,
        findings=findings,
    )

    hard_count = sum(1 for f in findings if f.severity == "hard")
    soft_count = sum(1 for f in findings if f.severity == "soft")

    return {
        "week_start_date": str(week_start_date),
        "snapshot_id": snapshot_id,
        "created_at": iso_now(),
        "is_feasible": ok_to_solve,
        "hard_findings_count": hard_count,
        "warning_findings_count": soft_count,
        "findings": [
            {
                "check_type": f.diagnostic_type,
                "severity": f.severity,
                "shift_instance_id": f.shift_instance_id,
                "student_id": f.student_id,
                "description": f.message,
                "recommended_action": "",
            }
            for f in findings
        ],
    }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_missing_submissions(
    students: list[dict],
    avail_by_student: dict[str, list[dict]],
) -> list[PreflightFinding]:
    findings = []
    for st in students:
        if st["id"] not in avail_by_student or not avail_by_student[st["id"]]:
            findings.append(PreflightFinding(
                diagnostic_type=DiagnosticType.MISSING_SUBMISSIONS,
                severity="soft",
                shift_instance_id=None,
                student_id=st["id"],
                day_of_week=None,
                message=(
                    f"Student '{st['name']}' ({st['id']}) has submitted no "
                    f"availability for this week."
                ),
                metadata={"student_name": st["name"]},
            ))
    return findings


def _check_shift_coverage(
    shifts: list[dict],
    eligibility_by_shift: dict[str, list[str]],
    config: dict,
) -> list[PreflightFinding]:
    findings = []
    for sh in shifts:
        eligible = eligibility_by_shift.get(sh["id"], [])
        required = sh.get("coverage_required", 1)
        n_eligible = len(set(eligible))

        day_name = DAY_NAMES.get(sh["day_of_week"], f"day{sh['day_of_week']}")
        label = sh.get("label") or f"{day_name} {sh['start_time']}–{sh['end_time']}"

        if n_eligible == 0:
            findings.append(PreflightFinding(
                diagnostic_type=DiagnosticType.SHIFT_ZERO_COVERAGE,
                severity="hard",
                shift_instance_id=sh["id"],
                student_id=None,
                day_of_week=sh["day_of_week"],
                message=f"Shift '{label}' has NO eligible workers.",
                metadata={"shift_label": label, "coverage_required": required},
            ))
        elif n_eligible < required * LOW_COVERAGE_RATIO:
            findings.append(PreflightFinding(
                diagnostic_type=DiagnosticType.SHIFT_LOW_COVERAGE,
                severity="soft",
                shift_instance_id=sh["id"],
                student_id=None,
                day_of_week=sh["day_of_week"],
                message=(
                    f"Shift '{label}' has only {n_eligible} eligible worker(s) "
                    f"for {required} required slot(s) — low coverage cushion."
                ),
                metadata={
                    "shift_label": label,
                    "eligible_count": n_eligible,
                    "coverage_required": required,
                },
            ))

        if sh.get("is_hard_shift") and n_eligible < HARD_SHIFT_MIN_ELIGIBLE:
            findings.append(PreflightFinding(
                diagnostic_type=DiagnosticType.HARD_SHIFT_COVERAGE_RISK,
                severity="soft",
                shift_instance_id=sh["id"],
                student_id=None,
                day_of_week=sh["day_of_week"],
                message=(
                    f"Hard shift '{label}' has only {n_eligible} eligible worker(s) "
                    f"(minimum recommended: {HARD_SHIFT_MIN_ELIGIBLE})."
                ),
                metadata={"shift_label": label, "eligible_count": n_eligible},
            ))

    return findings


def _check_total_coverage(
    students: list[dict],
    shifts: list[dict],
    availability: list[dict],
    config: dict,
) -> list[PreflightFinding]:
    """Hard check: sum of available student-hours vs sum of required shift-hours."""
    total_shift_hours = sum(sh.get("duration_hours", 0) for sh in shifts)

    # Available student-hours = sum of window durations for non-cannot_work records
    total_avail_hours = 0.0
    for av in availability:
        if av["level"] == "cannot_work":
            continue
        s = parse_time_minutes(av["start_time"])
        e = parse_time_minutes(av["end_time"])
        if e <= s:
            e += 1440
        total_avail_hours += (e - s) / 60.0

    findings = []
    if total_avail_hours < total_shift_hours:
        findings.append(PreflightFinding(
            diagnostic_type=DiagnosticType.INSUFFICIENT_TOTAL_COVERAGE,
            severity="hard",
            shift_instance_id=None,
            student_id=None,
            day_of_week=None,
            message=(
                f"Total available student-hours ({total_avail_hours:.1f}h) is less "
                f"than total required shift-hours ({total_shift_hours:.1f}h). "
                f"The schedule may be infeasible."
            ),
            metadata={
                "total_avail_hours": round(total_avail_hours, 2),
                "total_shift_hours": round(total_shift_hours, 2),
            },
        ))
    return findings


def _check_student_hours(
    students: list[dict],
    avail_by_student: dict[str, list[dict]],
    config: dict,
) -> list[PreflightFinding]:
    findings = []
    for st in students:
        avail = [
            av for av in avail_by_student.get(st["id"], [])
            if av["level"] != "cannot_work"
        ]
        avail_hours = sum(
            (parse_time_minutes(av["end_time"]) - parse_time_minutes(av["start_time"])) / 60.0
            if parse_time_minutes(av["end_time"]) > parse_time_minutes(av["start_time"])
            else (parse_time_minutes(av["end_time"]) + 1440 - parse_time_minutes(av["start_time"])) / 60.0
            for av in avail
        )
        min_h = st.get("min_hours") or config.get("min_hours_default", 8)
        if avail_hours < min_h:
            findings.append(PreflightFinding(
                diagnostic_type=DiagnosticType.STUDENT_INSUFFICIENT_AVAIL,
                severity="soft",
                shift_instance_id=None,
                student_id=st["id"],
                day_of_week=None,
                message=(
                    f"Student '{st['name']}' submitted only {avail_hours:.1f}h of "
                    f"available time — below their minimum of {min_h}h."
                ),
                metadata={
                    "student_name": st["name"],
                    "submitted_hours": round(avail_hours, 2),
                    "min_hours": min_h,
                },
            ))
    return findings


def _check_overnight_risk(
    students: list[dict],
    avail_by_student: dict[str, list[dict]],
) -> list[PreflightFinding]:
    findings = []
    student_by_id = {st["id"]: st for st in students}

    for student_id, avail_list in avail_by_student.items():
        st = student_by_id.get(student_id)
        if not st:
            continue

        for av in avail_list:
            e_min = parse_time_minutes(av["end_time"])
            if not is_overnight_end(e_min):
                continue

            next_dow = (av["day_of_week"] + 1) % 7
            next_day_avail = [
                a for a in avail_list
                if a["day_of_week"] == next_dow and a["level"] != "cannot_work"
            ]
            for nda in next_day_avail:
                s_min = parse_time_minutes(nda["start_time"])
                if too_early_after_overnight(s_min, MORNING_CUTOFF_HOUR):
                    findings.append(PreflightFinding(
                        diagnostic_type=DiagnosticType.OVERNIGHT_RISK,
                        severity="soft",
                        shift_instance_id=None,
                        student_id=student_id,
                        day_of_week=av["day_of_week"],
                        message=(
                            f"Student '{st['name']}' is available until {av['end_time']} "
                            f"(overnight) on {DAY_NAMES.get(av['day_of_week'], '')} and "
                            f"also marks availability from {nda['start_time']} on "
                            f"{DAY_NAMES.get(next_dow, '')} — less than "
                            f"{MORNING_CUTOFF_HOUR}h rest."
                        ),
                        metadata={
                            "student_name": st["name"],
                            "overnight_end": av["end_time"],
                            "next_day_start": nda["start_time"],
                        },
                    ))
    return findings


def _check_exam_period_gaps(
    shifts: list[dict],
    eligibility_by_shift: dict[str, list[str]],
) -> list[PreflightFinding]:
    """
    For exam period, check every day for contiguous coverage across all
    shift_instances. Flag any shift with zero eligible workers as a hard gap.
    """
    findings = []
    for sh in shifts:
        eligible = eligibility_by_shift.get(sh["id"], [])
        if not eligible:
            day_name = DAY_NAMES.get(sh["day_of_week"], f"day{sh['day_of_week']}")
            label = sh.get("label") or f"{day_name} {sh['start_time']}–{sh['end_time']}"
            findings.append(PreflightFinding(
                diagnostic_type=DiagnosticType.EXAM_PERIOD_GAP,
                severity="hard",
                shift_instance_id=sh["id"],
                student_id=None,
                day_of_week=sh["day_of_week"],
                message=(
                    f"Exam-period shift '{label}' has no eligible coverage. "
                    f"24-hour scheduling requires all slots to be fillable."
                ),
                metadata={"shift_label": label},
            ))
    return findings


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
        text("SELECT * FROM shift_instances WHERE week_start_date = :wsd ORDER BY day_of_week, start_time"),
        {"wsd": week_start_date},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _load_availability(conn: Connection, week_start_date: str) -> list[dict]:
    rows = conn.execute(
        text("SELECT * FROM availability WHERE week_start_date = :wsd"),
        {"wsd": week_start_date},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _persist_snapshot(
    conn: Connection,
    snapshot_id: str,
    week_start_date: str,
    run_id: str | None,
    findings: list[PreflightFinding],
) -> None:
    findings_payload = [
        {
            "diagnostic_type": f.diagnostic_type,
            "severity": f.severity,
            "shift_instance_id": f.shift_instance_id,
            "student_id": f.student_id,
            "day_of_week": f.day_of_week,
            "message": f.message,
            "metadata": f.metadata,
        }
        for f in findings
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
            "stype": DiagSource.PREFLIGHT,
            "findings": json.dumps(findings_payload),
            "now": iso_now(),
        },
    )
    conn.commit()
