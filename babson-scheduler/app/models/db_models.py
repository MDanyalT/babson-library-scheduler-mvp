"""
Enums, table name constants, and type definitions used across service and solver layers.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Table names
# ---------------------------------------------------------------------------
T_STUDENTS = "students"
T_SHIFT_TEMPLATES = "shift_templates"
T_SHIFT_INSTANCES = "shift_instances"
T_AVAILABILITY = "availability"
T_SCHEDULE_RUNS = "schedule_runs"
T_ASSIGNMENTS = "assignments"
T_VIOLATIONS = "violations"
T_DIAGNOSTICS = "diagnostics_snapshots"
T_CONFIG = "scheduler_config"


# ---------------------------------------------------------------------------
# Availability levels
# ---------------------------------------------------------------------------
class AvailLevel:
    PREFERRED   = "preferred"
    AVAILABLE   = "available"
    CANNOT_WORK = "cannot_work"
    ALL = [PREFERRED, AVAILABLE, CANNOT_WORK]


# ---------------------------------------------------------------------------
# Schedule mode
# ---------------------------------------------------------------------------
class ScheduleMode:
    WEEKLY          = "weekly"
    TERM_RECURRING  = "term_recurring"


# ---------------------------------------------------------------------------
# Schedule run statuses
# ---------------------------------------------------------------------------
class RunStatus:
    DRAFT        = "draft"
    UNDER_REVIEW = "under_review"
    PUBLISHED    = "published"
    INFEASIBLE   = "infeasible"


# ---------------------------------------------------------------------------
# Solver statuses
# ---------------------------------------------------------------------------
class SolverStatus:
    OPTIMAL         = "optimal"
    FEASIBLE        = "feasible"
    INFEASIBLE      = "infeasible"
    TIMEOUT         = "timeout"
    GREEDY_FALLBACK = "greedy_fallback"


# ---------------------------------------------------------------------------
# Preference level used in an assignment
# ---------------------------------------------------------------------------
class PrefLevelUsed:
    PREFERRED  = "preferred"
    AVAILABLE  = "available"
    UNASSIGNED = "unassigned"


# ---------------------------------------------------------------------------
# Reason codes stored on each assignment (JSON array)
# ---------------------------------------------------------------------------
class ReasonCode:
    PREFERRED_ASSIGNMENT  = "PREFERRED_ASSIGNMENT"
    AVAILABLE_ASSIGNMENT  = "AVAILABLE_ASSIGNMENT"
    HARD_SHIFT_PRIORITY   = "HARD_SHIFT_PRIORITY"
    SCARCE_COVERAGE       = "SCARCE_COVERAGE"
    SENIORITY_TIEBREAK    = "SENIORITY_TIEBREAK"
    TARGET_HOURS_BALANCING = "TARGET_HOURS_BALANCING"
    CONSOLIDATED_RUN      = "CONSOLIDATED_RUN"
    MANUAL_OVERRIDE       = "MANUAL_OVERRIDE"
    GREEDY_FALLBACK       = "GREEDY_FALLBACK"
    UNASSIGNED            = "UNASSIGNED"


# ---------------------------------------------------------------------------
# Violation types
# ---------------------------------------------------------------------------
class ViolationType:
    # Hard — block auto-publish
    UNFILLED_SHIFT              = "UNFILLED_SHIFT"
    STUDENT_OVER_MAXIMUM        = "STUDENT_OVER_MAXIMUM"
    CONSECUTIVE_HOURS_EXCEEDED  = "CONSECUTIVE_HOURS_EXCEEDED"
    COVERAGE_GAP                = "COVERAGE_GAP"
    OVERLAPPING_ASSIGNMENT      = "OVERLAPPING_ASSIGNMENT"

    # Soft — advisory
    STUDENT_UNDER_MINIMUM       = "STUDENT_UNDER_MINIMUM"
    TARGET_HOURS_MISSED         = "TARGET_HOURS_MISSED"
    BAD_SEQUENCE_OVERNIGHT      = "BAD_SEQUENCE_OVERNIGHT"
    PREFERENCE_VIOLATED         = "PREFERENCE_VIOLATED"
    LOW_PREFERENCE_FILL         = "LOW_PREFERENCE_FILL"

    HARD: frozenset[str] = frozenset([
        UNFILLED_SHIFT, STUDENT_OVER_MAXIMUM,
        CONSECUTIVE_HOURS_EXCEEDED, COVERAGE_GAP,
        OVERLAPPING_ASSIGNMENT,
    ])
    SOFT: frozenset[str] = frozenset([
        STUDENT_UNDER_MINIMUM, TARGET_HOURS_MISSED,
        BAD_SEQUENCE_OVERNIGHT, PREFERENCE_VIOLATED, LOW_PREFERENCE_FILL,
    ])

    @classmethod
    def severity(cls, vtype: str) -> str:
        return "hard" if vtype in cls.HARD else "soft"


# ---------------------------------------------------------------------------
# Diagnostic finding types (preflight)
# ---------------------------------------------------------------------------
class DiagnosticType:
    INSUFFICIENT_TOTAL_COVERAGE  = "INSUFFICIENT_TOTAL_COVERAGE"
    SHIFT_ZERO_COVERAGE          = "SHIFT_ZERO_COVERAGE"
    SHIFT_LOW_COVERAGE           = "SHIFT_LOW_COVERAGE"
    HARD_SHIFT_COVERAGE_RISK     = "HARD_SHIFT_COVERAGE_RISK"
    STUDENT_INSUFFICIENT_AVAIL   = "STUDENT_INSUFFICIENT_AVAIL"
    OVERNIGHT_RISK               = "OVERNIGHT_RISK"
    MISSING_SUBMISSIONS          = "MISSING_SUBMISSIONS"
    EXAM_PERIOD_GAP              = "EXAM_PERIOD_GAP"

    HARD: frozenset[str] = frozenset([
        INSUFFICIENT_TOTAL_COVERAGE, SHIFT_ZERO_COVERAGE, EXAM_PERIOD_GAP,
    ])


# ---------------------------------------------------------------------------
# Diagnostic and violation sources
# ---------------------------------------------------------------------------
class DiagSource:
    PREFLIGHT  = "preflight"
    POSTFLIGHT = "postflight"
    OVERRIDE   = "override"
    SOLVER     = "solver"


# ---------------------------------------------------------------------------
# Import sources for availability records
# ---------------------------------------------------------------------------
class ImportSource:
    API   = "api"
    EXCEL = "excel"
    FORM  = "form"
