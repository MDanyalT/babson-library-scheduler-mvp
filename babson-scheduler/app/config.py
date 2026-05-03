"""
Central configuration — library operating hours, scheduling rule defaults,
and hard-shift window definitions.

All values here are defaults. The live system reads runtime values from the
`scheduler_config` table so staff can adjust constraints without code changes.
These constants are used only for initial DB seeding and unit tests.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Library Operating Hours (defaults)
# Format: (open_time, close_time)
# "+1" suffix means the window crosses midnight into the next calendar day.
# ---------------------------------------------------------------------------
LIBRARY_HOURS: dict[str, tuple[str, str]] = {
    "sunday":    ("10:00", "02:00+1"),
    "monday":    ("07:30", "02:00+1"),
    "tuesday":   ("07:30", "02:00+1"),
    "wednesday": ("07:30", "02:00+1"),
    "thursday":  ("07:30", "02:00+1"),
    "friday":    ("07:30", "21:00"),
    "saturday":  ("10:00", "21:00"),
}

EXAM_LIBRARY_HOURS: dict[str, tuple[str, str]] = {
    day: ("00:00", "24:00") for day in
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
}

# ---------------------------------------------------------------------------
# Hard Shift Windows
# Shifts overlapping these windows are flagged is_hard_shift=True.
# NOTE: These are defaults. Actual values come from scheduler_config.
# ---------------------------------------------------------------------------
HARD_SHIFT_WINDOWS: list[tuple[str, str]] = [
    ("23:00", "02:00+1"),  # late-night
    ("07:30", "09:00"),    # early opening
]

# ---------------------------------------------------------------------------
# Scheduling Constraint Defaults
# All of these are seeded into scheduler_config at startup.
# ---------------------------------------------------------------------------
DEFAULT_MIN_HOURS: int = 8
DEFAULT_MAX_HOURS: int = 20
DEFAULT_MAX_CONSECUTIVE_HOURS: int = 6

# Shift block size and stagger overlap.
# Staggered/overlapping windows are the intentional desk-coverage strategy:
# consecutive windows overlap by a few minutes so there is always at least one
# worker at the desk during the handoff.  Two different students can hold
# overlapping windows — the no-overlap constraint only applies within a single
# student's schedule.  These are seeded defaults; the client Excel form is the
# primary source of truth for actual window boundaries when auto_generate_shifts=True.
DEFAULT_SHIFT_BLOCK_MINUTES: int = 120       # [ASSUMPTION — confirm with client]
DEFAULT_STAGGER_OVERLAP_MINUTES: int = 5     # [ASSUMPTION — confirm with client]

# Overnight-sequence constraint
OVERNIGHT_END_HOUR: int = 2      # shifts ending ≤ 02:00 next day are "overnight"
MORNING_CUTOFF_HOUR: int = 10    # cannot start before 10:00 after an overnight

# Exam-period defaults
DEFAULT_EXAM_MIN_HOURS: int = 12
DEFAULT_EXAM_MAX_HOURS: int = 20

# Solver
DEFAULT_SOLVER_TIME_LIMIT_SECONDS: int = 60

# ---------------------------------------------------------------------------
# Objective Weights (CP-SAT)
# ---------------------------------------------------------------------------
WEIGHT_PREFERENCE_SATISFACTION: int = 100
WEIGHT_TARGET_HOURS_PROXIMITY: int = 50
WEIGHT_HARD_SHIFT_FILL: int = 200
WEIGHT_FAIRNESS: int = 30
WEIGHT_CONSOLIDATION: int = 20
WEIGHT_UNFILLED_PENALTY: int = 500
WEIGHT_OVERNIGHT_PENALTY: int = 150
WEIGHT_SENIORITY_TIEBREAK: int = 3

# ---------------------------------------------------------------------------
# Availability Level Ranks (higher = more desirable)
# ---------------------------------------------------------------------------
PREFERENCE_RANK: dict[str, int] = {
    "preferred":    2,
    "available":    1,
    "cannot_work":  0,
}

# ---------------------------------------------------------------------------
# Day of Week
# ---------------------------------------------------------------------------
DAY_NAMES: dict[int, str] = {
    0: "monday",
    1: "tuesday",
    2: "wednesday",
    3: "thursday",
    4: "friday",
    5: "saturday",
    6: "sunday",
}

DAY_OF_WEEK: dict[str, int] = {v: k for k, v in DAY_NAMES.items()}

# ---------------------------------------------------------------------------
# Target Hours Band → target_hours mapping
#
# Students express a preferred hours band; we store only the midpoint as
# target_hours.  min_hours and max_hours are ALWAYS 8 and 20 regardless of
# the band — the band does NOT become a hard scheduling floor/ceiling.
# The scheduler uses target_hours as a soft objective: it tries to schedule
# each student close to their stated preference while staying within 8–20 h.
#
# Suggested mapping (per project brief):
#   8–10  → target 9   (midpoint)
#   10–12 → target 11  (midpoint)
#   12–14 → target 13  (midpoint)
#   15+   → target 15  (lower bound; no stated upper bound)
# ---------------------------------------------------------------------------
TARGET_HOURS_BANDS: dict[str, int] = {
    "8-10":          9,
    "10-12":         11,
    "12-14":         13,
    "15+":           15,   # lower bound of open-ended range
    "no_preference": DEFAULT_MIN_HOURS,
}
