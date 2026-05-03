"""
Pydantic v2 schemas — the OpenAPI contract consumed by IBM Orchestrate.
Every field carries a `description` so the generated spec is self-documenting.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------------

class StudentCreate(BaseModel):
    name: str = Field(..., description="Full name")
    email: str = Field(..., description="Unique institutional email")
    seniority_date: date = Field(..., description="Employment start date (YYYY-MM-DD). Earlier = higher seniority.")
    target_hours: int = Field(
        8, ge=8, le=20,
        description=(
            "Student's preferred weekly hours target — the scheduler tries to schedule "
            "each student close to this number as a soft objective. "
            "Use the midpoint of the student's stated preference band: "
            "8–10 → 9, 10–12 → 11, 12–14 → 13, 15+ → 15. "
            "min_hours and max_hours are always fixed at 8 and 20 per project rules "
            "and do not need to be supplied."
        ),
    )
    # Per project brief: min and max are system-wide constants (8 and 20).
    # They are accepted here for API compatibility but are always overridden
    # to 8 / 20 at the router layer — the student's preference band only
    # influences target_hours, not the hard floor/ceiling.
    min_hours: int = Field(8, ge=8, le=20, description="Always 8 (project rule — do not change)")
    max_hours: int = Field(20, ge=8, le=20, description="Always 20 (project rule — do not change)")

    @model_validator(mode="after")
    def validate_hours_order(self):
        if not (8 <= self.target_hours <= 20):
            raise ValueError("target_hours must be between 8 and 20")
        return self


class StudentUpdate(BaseModel):
    name: Optional[str] = None
    seniority_date: Optional[date] = None
    min_hours: Optional[int] = Field(None, ge=8, le=20)
    max_hours: Optional[int] = Field(None, ge=8, le=20)
    target_hours: Optional[int] = Field(None, ge=8, le=20)
    is_active: Optional[bool] = None


class StudentOut(BaseModel):
    id: str
    name: str
    email: str
    seniority_date: str
    min_hours: int
    max_hours: int
    target_hours: int
    is_active: bool
    created_at: str


# ---------------------------------------------------------------------------
# Shift Templates
# ---------------------------------------------------------------------------

class ShiftTemplateCreate(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6, description="0=Monday … 6=Sunday")
    start_time: str = Field(..., description="HH:MM")
    end_time: str = Field(..., description="HH:MM (use 00:00–02:00 for cross-midnight)")
    label: Optional[str] = Field(None, description="Human label e.g. 'Late Night', 'Opening'")
    is_hard_shift: bool = Field(False, description="Override hard-shift flag")


class ShiftTemplateOut(BaseModel):
    id: str
    day_of_week: int
    start_time: str
    end_time: str
    duration_hours: float
    is_hard_shift: bool
    label: Optional[str]
    created_by: str
    is_active: bool


# ---------------------------------------------------------------------------
# Shift Instances
# ---------------------------------------------------------------------------

class ShiftInstanceGenerateRequest(BaseModel):
    week_start_date: date = Field(..., description="Monday of the target week")
    is_exam_period: bool = Field(False, description="Use 24/7 exam-period hours")
    slots_per_window: int = Field(
        1,
        ge=1,
        le=4,
        description=(
            "Number of shift instances created per time window, each filled by exactly "
            "one student.  The project default is 1 (one student assigned per window). "
            "Set to 2+ only when a specific window genuinely requires multiple workers "
            "at the desk simultaneously.  Note: overlapping windows across different "
            "students are handled by the stagger model (separate instances with separate "
            "time windows) — not by increasing slots_per_window."
        ),
    )
    force: bool = Field(True, description="Delete and regenerate if instances already exist")


class ShiftInstanceOut(BaseModel):
    id: str
    template_id: Optional[str]
    week_start_date: str
    date: str
    day_of_week: int
    start_time: str
    end_time: str
    duration_hours: float
    is_hard_shift: bool
    is_exam_period: bool
    slot_index: int


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

class AvailabilitySlot(BaseModel):
    shift_instance_id: Optional[str] = Field(
        None, description="Preferred: link directly to shift_instance. Provide this OR (day_of_week + times)."
    )
    day_of_week: Optional[int] = Field(None, ge=0, le=6, description="0=Monday … 6=Sunday")
    start_time: Optional[str] = Field(None, description="HH:MM")
    end_time: Optional[str] = Field(None, description="HH:MM")
    level: Literal["preferred", "available", "cannot_work"] = Field(
        ..., description="Availability level for this slot"
    )

    @model_validator(mode="after")
    def must_have_shift_or_window(self):
        has_instance = self.shift_instance_id is not None
        has_window = all(x is not None for x in [self.day_of_week, self.start_time, self.end_time])
        if not has_instance and not has_window:
            raise ValueError("Provide either shift_instance_id or (day_of_week + start_time + end_time)")
        return self


class AvailabilitySubmitRequest(BaseModel):
    student_id: str = Field(..., description="UUID of the submitting student")
    week_start_date: date = Field(..., description="Monday of the target week")
    slots: list[AvailabilitySlot] = Field(
        ..., description="All availability slots. Atomically replaces any previous submission for this student+week."
    )
    import_source: Literal["api", "excel", "form"] = Field("api")


class AvailabilitySlotOut(BaseModel):
    id: str
    student_id: str
    week_start_date: str
    shift_instance_id: Optional[str]
    day_of_week: int
    start_time: str
    end_time: str
    level: str
    submitted_at: str
    import_source: str


class CoverageHeatmapEntry(BaseModel):
    shift_instance_id: str
    date: str
    day_of_week: int
    start_time: str
    end_time: str
    slot_index: int
    is_hard_shift: bool
    preferred_count: int
    available_count: int
    total_eligible: int
    coverage_risk: Literal["none", "low", "critical"] = Field(
        ..., description="none=3+ eligible, low=1-2, critical=0"
    )


# ---------------------------------------------------------------------------
# Diagnostics / Preflight
# ---------------------------------------------------------------------------

class DiagnosticFinding(BaseModel):
    check_type: str
    severity: Literal["hard", "warning", "info"]
    shift_instance_id: Optional[str] = None
    shift_date: Optional[str] = None
    shift_time: Optional[str] = None
    student_id: Optional[str] = None
    student_name: Optional[str] = None
    available_count: Optional[int] = None
    description: str
    recommended_action: str


class PreflightReportOut(BaseModel):
    week_start_date: str
    snapshot_id: str
    created_at: str
    is_feasible: bool = Field(..., description="False if any hard diagnostic findings exist")
    hard_findings_count: int
    warning_findings_count: int
    findings: list[DiagnosticFinding]


# ---------------------------------------------------------------------------
# Schedule Generation
# ---------------------------------------------------------------------------

class ScheduleGenerateRequest(BaseModel):
    week_start_date: date
    is_exam_period: bool = False
    solver_time_limit_seconds: int = Field(60, ge=5, le=300)
    force_regenerate: bool = Field(
        False,
        description="Overwrite existing draft. Blocked if run has locked assignments (returns 409)."
    )


class AssignmentShiftInfo(BaseModel):
    id: str
    date: str
    day_of_week: int
    start_time: str
    end_time: str
    duration_hours: float
    is_hard_shift: bool
    slot_index: int


class AssignmentStudentInfo(BaseModel):
    id: str
    name: str
    email: str
    seniority_date: str


class AssignmentOut(BaseModel):
    id: str
    shift: AssignmentShiftInfo
    student: Optional[AssignmentStudentInfo] = None
    preference_level_used: str
    reason_codes: list[str] = Field(default_factory=list, description="Explains why this assignment was made")
    is_locked: bool
    is_manual_override: bool
    override_reason: Optional[str] = None
    assigned_at: str


class StudentSummaryOut(BaseModel):
    student_id: str
    name: str
    email: str
    seniority_date: str
    min_hours: int
    target_hours: int
    max_hours: int
    assigned_hours: float
    shifts_assigned: int
    preferred_shifts: int
    available_shifts: int
    hours_vs_target: float = Field(..., description="assigned_hours − target_hours; negative = under target")
    constraint_status: Literal["ok", "under_minimum", "over_maximum", "at_target"]


class ScheduleRunOut(BaseModel):
    id: str
    week_start_date: str
    status: str
    solver_status: Optional[str]
    solve_time_ms: Optional[int]
    objective_score: Optional[float]
    generated_at: str
    published_at: Optional[str]
    notes: Optional[str]
    schedule_mode: str = Field(
        "weekly",
        description=(
            "'weekly' — one-week schedule. "
            "'term_recurring' — standard recurring schedule for the semester; "
            "week_start_date is the representative week used for shift instances."
        ),
    )
    term_start_date: Optional[str] = Field(
        None, description="First day of the term (YYYY-MM-DD). Informational for term_recurring mode."
    )
    term_end_date: Optional[str] = Field(
        None, description="Last day of the term (YYYY-MM-DD). Informational for term_recurring mode."
    )
    total_shifts: int = 0
    filled_shifts: int = 0
    unfilled_shifts: int = 0
    hard_violations_count: int = 0
    soft_violations_count: int = 0


class ScheduleFullOut(BaseModel):
    run: ScheduleRunOut
    assignments: list[AssignmentOut]
    student_summaries: list[StudentSummaryOut]


# ---------------------------------------------------------------------------
# Manual Override
# ---------------------------------------------------------------------------

class AssignmentPatchRequest(BaseModel):
    student_id: Optional[str] = Field(None, description="New student UUID, or null to clear")
    override_reason: Optional[str] = Field(None, description="Staff note explaining the change")


class AssignmentLockRequest(BaseModel):
    locked: bool = Field(..., description="True to lock, False to unlock")


# ---------------------------------------------------------------------------
# Violations
# ---------------------------------------------------------------------------

class ViolationOut(BaseModel):
    id: str
    violation_type: str
    severity: Literal["hard", "soft"]
    source: str
    shift: Optional[AssignmentShiftInfo] = None
    student: Optional[AssignmentStudentInfo] = None
    description: str


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class SchedulerConfigOut(BaseModel):
    min_hours_default: int
    max_hours_default: int
    max_consecutive_hours: int
    shift_block_minutes: int
    stagger_overlap_minutes: int
    hard_shift_windows: list
    exam_min_hours: int
    exam_max_hours: int
    solver_time_limit_seconds: int
    updated_at: str


class SchedulerConfigUpdate(BaseModel):
    min_hours_default: Optional[int] = Field(None, ge=1, le=40)
    max_hours_default: Optional[int] = Field(None, ge=1, le=40)
    max_consecutive_hours: Optional[int] = Field(None, ge=1, le=12)
    shift_block_minutes: Optional[int] = Field(None, ge=30, le=360)
    stagger_overlap_minutes: Optional[int] = Field(None, ge=0, le=30)
    exam_min_hours: Optional[int] = Field(None, ge=1, le=40)
    exam_max_hours: Optional[int] = Field(None, ge=1, le=40)
    solver_time_limit_seconds: Optional[int] = Field(None, ge=5, le=300)
