"""
CP-SAT model builder for the Babson Library shift scheduler.

`ScheduleModelBuilder` fetches all required data from the database,
constructs the OR-Tools CP-SAT model with hard and soft constraints,
and returns the model together with all decision-variable references that
`runner.py` needs to extract a solution.

Scheduling model — one student per shift instance
--------------------------------------------------
Each ``shift_instance`` row represents exactly one desk position for a
specific time window.  The solver assigns **one student per instance**.
Staggered (overlapping) windows that appear as separate columns in the
client's Excel availability form are deliberately preserved as separate
instances — they are NOT merged or deduplicated — so that the solver can
assign a different student to each window, producing continuous desk
coverage even when individual shifts are shorter than the full open period.

No-overlap constraint scope
----------------------------
The ``_add_no_overlap_constraints`` helper enforces that **a single student**
is not assigned to two shift instances whose time windows overlap.  It does
NOT prevent two *different* students from holding overlapping windows — that
is the intended staggered-coverage pattern.  See ``_add_no_overlap_constraints``
for the exact overlap definition (strict interior overlap; touching boundaries
are not considered overlapping).
"""

from __future__ import annotations

from typing import Any

from ortools.sat.python import cp_model
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.config import (
    PREFERENCE_RANK,
    WEIGHT_CONSOLIDATION,
    WEIGHT_HARD_SHIFT_FILL,
    WEIGHT_PREFERENCE_SATISFACTION,
    WEIGHT_SENIORITY_TIEBREAK,
    WEIGHT_TARGET_HOURS_PROXIMITY,
    WEIGHT_UNFILLED_PENALTY,
)
from app.database import get_config
from app.utils.time_utils import (
    effective_end_min,
    parse_time_minutes,
    shifts_are_consecutive,
    time_str_to_minutes,
)


class ScheduleModelBuilder:
    """
    Builds and returns a CP-SAT model for the given week.

    Usage
    -----
    ::

        builder = ScheduleModelBuilder(conn, week_start_date, time_limit_seconds)
        model, vars_dict = builder.build()

    ``vars_dict`` keys:
    - ``"x"``          : ``{(student_id, shift_id): BoolVar}``
    - ``"unfilled"``   : ``{shift_id: BoolVar}``
    - ``"students"``   : list of student row dicts
    - ``"shifts"``     : list of shift_instance row dicts
    - ``"eligibility"``: ``{(student_id, shift_id): level_str}``
    """

    def __init__(
        self,
        conn: Connection,
        week_start_date: str,
        time_limit_seconds: int | None = None,
    ) -> None:
        self.conn = conn
        self.week_start_date = week_start_date
        self.config = get_config(conn)
        self.time_limit_seconds = (
            time_limit_seconds
            if time_limit_seconds is not None
            else self.config.get("solver_time_limit_seconds", 60)
        )

        # Data fetched from DB
        self.students: list[dict] = []
        self.shifts: list[dict] = []
        self.avail_by_student: dict[str, list[dict]] = {}
        self.locked_by_shift: dict[str, dict] = {}  # shift_id → assignment dict

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def build(self) -> tuple[cp_model.CpModel, dict[str, Any]]:
        """Fetch data, build model, return (model, vars_dict)."""
        self._load_data()
        eligibility = self._build_eligibility()
        model, x, unfilled = self._build_model(eligibility)
        return model, {
            "x": x,
            "unfilled": unfilled,
            "students": self.students,
            "shifts": self.shifts,
            "eligibility": eligibility,
            "locked_by_shift": self.locked_by_shift,
        }

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_data(self) -> None:
        conn = self.conn
        wsd = self.week_start_date

        students_rows = conn.execute(
            text("SELECT * FROM students WHERE is_active = 1")
        ).fetchall()
        self.students = [dict(r._mapping) for r in students_rows]

        shifts_rows = conn.execute(
            text(
                "SELECT * FROM shift_instances WHERE week_start_date = :wsd "
                "ORDER BY date, start_time, slot_index"
            ),
            {"wsd": wsd},
        ).fetchall()
        self.shifts = [dict(r._mapping) for r in shifts_rows]

        avail_rows = conn.execute(
            text("SELECT * FROM availability WHERE week_start_date = :wsd"),
            {"wsd": wsd},
        ).fetchall()
        avail_list = [dict(r._mapping) for r in avail_rows]
        for av in avail_list:
            s_id = av["student_id"]
            self.avail_by_student.setdefault(s_id, []).append(av)

        locked_rows = conn.execute(
            text(
                """
                SELECT a.* FROM assignments a
                JOIN schedule_runs sr ON sr.id = a.run_id
                WHERE sr.week_start_date = :wsd AND a.is_locked = 1
                """
            ),
            {"wsd": wsd},
        ).fetchall()
        for row in locked_rows:
            d = dict(row._mapping)
            self.locked_by_shift[d["shift_instance_id"]] = d

    # ------------------------------------------------------------------
    # Eligibility index
    # ------------------------------------------------------------------

    def _build_eligibility(self) -> dict[tuple[str, str], str]:
        """
        Returns ``{(student_id, shift_id): "preferred" | "available"}``.
        Cannot-work pairs are excluded.
        """
        eligibility: dict[tuple[str, str], str] = {}

        for sh in self.shifts:
            sh_id = sh["id"]
            sh_dow = sh["day_of_week"]
            sh_start_min = time_str_to_minutes(sh["start_time"])
            sh_end_min = effective_end_min(sh["start_time"], sh["end_time"])

            for st in self.students:
                s_id = st["id"]
                avail_records = self.avail_by_student.get(s_id, [])

                best_level: str | None = None
                blocked = False

                for av in avail_records:
                    level = av["level"]
                    # Check cannot_work via shift_instance_id match or overlap
                    if level == "cannot_work":
                        if av.get("shift_instance_id") == sh_id:
                            blocked = True
                            break
                        if av["day_of_week"] == sh_dow:
                            av_start = parse_time_minutes(av["start_time"])
                            av_end_calc = effective_end_min(
                                av["start_time"], av["end_time"]
                            )
                            if av_start <= sh_start_min and av_end_calc >= sh_end_min:
                                blocked = True
                                break
                        continue

                    # Positive availability — check if it covers this shift
                    covers = False
                    if av.get("shift_instance_id") == sh_id:
                        covers = True
                    elif av["day_of_week"] == sh_dow:
                        av_start = parse_time_minutes(av["start_time"])
                        av_end_calc = effective_end_min(
                            av["start_time"], av["end_time"]
                        )
                        if av_start <= sh_start_min and av_end_calc >= sh_end_min:
                            covers = True

                    if covers:
                        # Keep the best level (preferred > available)
                        if best_level is None:
                            best_level = level
                        elif PREFERENCE_RANK.get(level, 0) > PREFERENCE_RANK.get(
                            best_level, 0
                        ):
                            best_level = level

                if not blocked and best_level is not None:
                    eligibility[(s_id, sh_id)] = best_level

        return eligibility

    # ------------------------------------------------------------------
    # Model construction
    # ------------------------------------------------------------------

    def _build_model(
        self, eligibility: dict[tuple[str, str], str]
    ) -> tuple[cp_model.CpModel, dict, dict]:
        model = cp_model.CpModel()
        students = self.students
        shifts = self.shifts
        config = self.config

        max_consec_hours = config.get("max_consecutive_hours", 6)
        max_consec_min = int(max_consec_hours * 60)
        stagger = config.get("stagger_overlap_minutes", 5)

        # --- Decision variables ---
        x: dict[tuple[str, str], cp_model.IntVar] = {}
        for (s_id, sh_id) in eligibility:
            x[(s_id, sh_id)] = model.NewBoolVar(f"x_{s_id}_{sh_id}")

        unfilled: dict[str, cp_model.IntVar] = {}
        for sh in shifts:
            unfilled[sh["id"]] = model.NewBoolVar(f"unfilled_{sh['id']}")

        # --- Hard constraint 1: every shift must be either assigned or unfilled ---
        shift_id_set = {sh["id"] for sh in shifts}
        for sh in shifts:
            sh_id = sh["id"]
            assigned_vars = [
                x[(s_id, sh_id)]
                for s_id in [st["id"] for st in students]
                if (s_id, sh_id) in x
            ]
            model.Add(
                sum(assigned_vars) + unfilled[sh_id] == 1
            )

        # Hard constraint 2: cannot-work pairs — already excluded from eligibility,
        # so we only need to enforce that ineligible x vars are zero (they don't exist).
        # Nothing extra needed here.

        # --- Hard constraint 3: per-student max (and min) hours ---
        # Track assigned_minutes IntVar per student
        student_assigned_min: dict[str, cp_model.IntVar] = {}
        for st in students:
            s_id = st["id"]
            s_max_h = st.get("max_hours", config.get("max_hours_default", 20))
            s_max_min = int(s_max_h * 60)

            shift_vars_for_student = []
            durations_for_student = []
            for sh in shifts:
                sh_id = sh["id"]
                if (s_id, sh_id) in x:
                    dur_min = int(sh["duration_hours"] * 60)
                    shift_vars_for_student.append(x[(s_id, sh_id)])
                    durations_for_student.append(dur_min)

            if not shift_vars_for_student:
                continue

            total_min_var = model.NewIntVar(0, s_max_min * 2, f"total_min_{s_id}")
            model.Add(
                total_min_var
                == sum(
                    v * d
                    for v, d in zip(shift_vars_for_student, durations_for_student)
                )
            )
            model.Add(total_min_var <= s_max_min)
            student_assigned_min[s_id] = total_min_var

        # --- Hard constraint 4: consecutive hours ceiling ---
        _add_consecutive_hours_constraints(
            model, x, students, shifts, max_consec_min, stagger
        )

        # --- Hard constraint 5a: no-overlap per student ---
        _add_no_overlap_constraints(model, x, students, shifts)

        # --- Hard constraint 5b: locked assignments ---
        for sh_id, locked in self.locked_by_shift.items():
            s_id_locked = locked.get("student_id")
            if s_id_locked and (s_id_locked, sh_id) in x:
                model.Add(x[(s_id_locked, sh_id)] == 1)

        # --- Seniority scores (0–10, older employee → higher score) ---
        seniority_scores = _compute_seniority_scores(students)

        # --- Soft objective terms ---
        objective_terms: list[Any] = []

        # Preference satisfaction + hard shift fill + seniority tiebreak
        for (s_id, sh_id), level in eligibility.items():
            var = x[(s_id, sh_id)]
            sh = next((s for s in shifts if s["id"] == sh_id), None)
            if sh is None:
                continue

            pref_weight = (
                WEIGHT_PREFERENCE_SATISFACTION
                if level == "preferred"
                else WEIGHT_PREFERENCE_SATISFACTION // 2
            )
            objective_terms.append(pref_weight * var)

            if sh.get("is_hard_shift"):
                objective_terms.append(WEIGHT_HARD_SHIFT_FILL * var)

            sen_score = seniority_scores.get(s_id, 0)
            objective_terms.append(WEIGHT_SENIORITY_TIEBREAK * sen_score * var)

        # Target hours proximity (minimise |assigned - target| per student)
        for st in students:
            s_id = st["id"]
            if s_id not in student_assigned_min:
                continue
            target_min = int(st.get("target_hours", 8) * 60)
            deviation_var = model.NewIntVar(
                0, int(config.get("max_hours_default", 20) * 60), f"dev_{s_id}"
            )
            total_var = student_assigned_min[s_id]
            model.Add(deviation_var >= total_var - target_min)
            model.Add(deviation_var >= target_min - total_var)
            # Penalise deviation
            objective_terms.append(-WEIGHT_TARGET_HOURS_PROXIMITY * deviation_var)

        # Consolidation bonus — reward consecutive back-to-back shifts per student
        consolidation_bonuses = _add_consolidation_bonuses(
            model, x, students, shifts, stagger
        )
        for bonus_var in consolidation_bonuses:
            objective_terms.append(WEIGHT_CONSOLIDATION * bonus_var)

        # Unfilled shift penalties
        for sh in shifts:
            sh_id = sh["id"]
            penalty = WEIGHT_UNFILLED_PENALTY
            if sh.get("is_hard_shift"):
                penalty *= 2
            objective_terms.append(-penalty * unfilled[sh_id])

        if objective_terms:
            model.Maximize(sum(objective_terms))

        return model, x, unfilled


# ---------------------------------------------------------------------------
# Helper: no-overlap constraints
# ---------------------------------------------------------------------------

def _add_no_overlap_constraints(
    model: cp_model.CpModel,
    x: dict[tuple[str, str], cp_model.IntVar],
    students: list[dict],
    shifts: list[dict],
) -> None:
    """
    For every student, prevent the solver from assigning them to two shifts
    whose time windows strictly overlap.

    **Scope:** this constraint is per-student only.  Two *different* students
    are explicitly allowed to hold overlapping shift windows — that is the
    intended staggered-desk-coverage pattern where, for example, Student A
    works 09:00–11:00 while Student B works 09:30–11:30, providing a handoff
    window with both workers present.

    Overlap detection uses absolute minutes on a weekly timeline:
        abs_start = day_of_week * 1440 + start_min
        abs_end   = day_of_week * 1440 + effective_end_min(start, end)

    This handles same-day, partial, duplicate, and cross-midnight shifts
    correctly.  Adjacent shifts that merely *touch* (abs_end_A == abs_start_B)
    are NOT considered overlapping.
    """
    # Pre-compute absolute intervals once — O(shifts)
    shift_intervals: dict[str, tuple[int, int]] = {}
    for sh in shifts:
        base = sh["day_of_week"] * 1440
        abs_s = base + time_str_to_minutes(sh["start_time"])
        abs_e = base + effective_end_min(sh["start_time"], sh["end_time"])
        shift_intervals[sh["id"]] = (abs_s, abs_e)

    for st in students:
        s_id = st["id"]
        # Collect shift ids this student is eligible for
        eligible_ids = [sh_id for (sid, sh_id) in x if sid == s_id]
        if len(eligible_ids) < 2:
            continue

        # Check every pair — O(eligible²) which is small per student
        for i in range(len(eligible_ids)):
            id_a = eligible_ids[i]
            a_s, a_e = shift_intervals[id_a]
            for j in range(i + 1, len(eligible_ids)):
                id_b = eligible_ids[j]
                b_s, b_e = shift_intervals[id_b]
                # Strict overlap: intervals share interior time
                if a_s < b_e and b_s < a_e:
                    model.Add(x[(s_id, id_a)] + x[(s_id, id_b)] <= 1)


# ---------------------------------------------------------------------------
# Helper: consecutive hours constraints
# ---------------------------------------------------------------------------

def _add_consecutive_hours_constraints(
    model: cp_model.CpModel,
    x: dict[tuple[str, str], cp_model.IntVar],
    students: list[dict],
    shifts: list[dict],
    max_consec_min: int,
    stagger: int,
) -> None:
    """
    For each student, prevent any contiguous run of assigned shifts from
    exceeding ``max_consec_min`` minutes.

    Design
    ------
    1. Group eligible shifts by their *unique time window*
       ``(day_of_week, start_time, end_time)``.  Multiple ``slot_index``
       values that share the same window are bundled together — the
       no-overlap constraint already ensures a student can hold at most
       one of them, so they count as a single "window slot".

    2. Sort windows by **absolute weekly minutes**
       ``(day_of_week * 1440 + start_min)`` so cross-midnight chains
       (e.g. 23:30 Mon → 01:30 Tue) are ordered correctly even though they
       span two calendar dates.

    3. Build maximal consecutive chains: two adjacent windows are
       consecutive if the first window's absolute end is within ``stagger``
       minutes of the second window's absolute start.

    4. For every sub-chain whose total duration exceeds the limit, add:
           sum(all x vars that belong to windows in that sub-chain)
               <= (number of windows in sub-chain) - 1
       Because no-overlap caps each window at ≤1 assignment, this
       translates to "leave at least one window in this range unassigned".
    """

    for st in students:
        s_id = st["id"]

        eligible_shifts = [sh for sh in shifts if (s_id, sh["id"]) in x]
        if not eligible_shifts:
            continue

        # --- Step 1: group by unique (dow, start, end) window ---------------
        window_groups: dict[tuple, list[str]] = {}
        for sh in eligible_shifts:
            key = (sh["day_of_week"], sh["start_time"], sh["end_time"])
            window_groups.setdefault(key, []).append(sh["id"])

        # --- Step 2: sort by absolute weekly start minute -------------------
        def _abs_start(key: tuple) -> int:
            dow, st_time, _ = key
            return dow * 1440 + time_str_to_minutes(st_time)

        def _abs_interval(key: tuple) -> tuple[int, int]:
            dow, st_time, et_time = key
            base = dow * 1440
            return (
                base + time_str_to_minutes(st_time),
                base + effective_end_min(st_time, et_time),
            )

        sorted_windows = sorted(window_groups.items(), key=lambda kv: _abs_start(kv[0]))

        if len(sorted_windows) < 2:
            continue

        # --- Step 3: build direct-follows graph --------------------------------
        # Window j directly follows window i if i ends within [i.end, i.end+stagger]
        # of j's start.  Sorted-adjacency is wrong here because staggered windows
        # are interleaved (e.g. :00 and :30 series) so adjacent pairs in sorted
        # order have gaps >> stagger.  The graph approach correctly connects each
        # window only to its true successor(s).
        n = len(sorted_windows)
        intervals = [_abs_interval(sw[0]) for sw in sorted_windows]
        shift_id_lists = [sw[1] for sw in sorted_windows]

        forward: list[list[int]] = [[] for _ in range(n)]
        for i in range(n):
            _, ai_e = intervals[i]
            for j in range(n):
                if j == i:
                    continue
                aj_s, _ = intervals[j]
                if ai_e <= aj_s <= ai_e + stagger:
                    forward[i].append(j)

        # Find root nodes (no predecessors)
        has_pred = [False] * n
        for i in range(n):
            for j in forward[i]:
                has_pred[j] = True

        # Enumerate all maximal paths (DFS)
        paths: list[list[int]] = []
        path_stack: list[list[int]] = [[i] for i in range(n) if not has_pred[i]]
        while path_stack:
            path = path_stack.pop()
            cur = path[-1]
            nexts = forward[cur]
            if not nexts:
                if len(path) >= 2:
                    paths.append(path)
            else:
                for nxt in nexts:
                    path_stack.append(path + [nxt])

        # --- Step 4: add CP-SAT constraints for violating sub-paths ----------
        seen: set[frozenset] = set()
        for path in paths:
            np_ = len(path)
            for start_idx in range(np_):
                total_dur = 0
                for end_idx in range(start_idx, np_):
                    abs_s, abs_e = intervals[path[end_idx]]
                    total_dur += abs_e - abs_s

                    if total_dur > max_consec_min and end_idx > start_idx:
                        sub = frozenset(path[start_idx : end_idx + 1])
                        if sub not in seen:
                            seen.add(sub)
                            all_vars = [
                                x[(s_id, sh_id)]
                                for idx in path[start_idx : end_idx + 1]
                                for sh_id in shift_id_lists[idx]
                                if (s_id, sh_id) in x
                            ]
                            # At most (window_count - 1) of these may be assigned
                            model.Add(sum(all_vars) <= end_idx - start_idx)


# ---------------------------------------------------------------------------
# Helper: consolidation bonuses
# ---------------------------------------------------------------------------

def _add_consolidation_bonuses(
    model: cp_model.CpModel,
    x: dict[tuple[str, str], cp_model.IntVar],
    students: list[dict],
    shifts: list[dict],
    stagger: int,
) -> list[cp_model.IntVar]:
    """
    For each student, for each consecutive shift pair on the same day,
    add a BoolVar that is 1 iff both shifts are assigned to that student.
    Returns the list of those BoolVars (to be added to the objective).
    """
    bonus_vars: list[cp_model.IntVar] = []

    for st in students:
        s_id = st["id"]
        eligible_shifts = [sh for sh in shifts if (s_id, sh["id"]) in x]
        if len(eligible_shifts) < 2:
            continue

        by_date: dict[str, list[dict]] = {}
        for sh in eligible_shifts:
            by_date.setdefault(sh.get("date", ""), []).append(sh)

        for date_str, day_shifts in by_date.items():
            day_sorted = sorted(day_shifts, key=lambda s: s["start_time"])
            for i in range(len(day_sorted) - 1):
                sh_i = day_sorted[i]
                sh_j = day_sorted[i + 1]
                end_i = effective_end_min(sh_i["start_time"], sh_i["end_time"])
                start_j = time_str_to_minutes(sh_j["start_time"])
                if not shifts_are_consecutive(end_i, start_j, stagger):
                    continue
                if (s_id, sh_i["id"]) not in x or (s_id, sh_j["id"]) not in x:
                    continue

                bonus = model.NewBoolVar(
                    f"consec_{s_id}_{sh_i['id']}_{sh_j['id']}"
                )
                xi = x[(s_id, sh_i["id"])]
                xj = x[(s_id, sh_j["id"])]
                # bonus == 1  ↔  xi == 1 AND xj == 1
                model.AddBoolAnd([xi, xj]).OnlyEnforceIf(bonus)
                model.AddBoolOr([xi.Not(), xj.Not()]).OnlyEnforceIf(bonus.Not())
                bonus_vars.append(bonus)

    return bonus_vars


# ---------------------------------------------------------------------------
# Helper: seniority scoring
# ---------------------------------------------------------------------------

def _compute_seniority_scores(students: list[dict]) -> dict[str, int]:
    """
    Normalise seniority_date to a 0–10 integer score.
    Older (earlier) date → higher score (more senior → preferred in tiebreak).
    Returns ``{student_id: score}``.
    """
    if not students:
        return {}
    if len(students) == 1:
        return {students[0]["id"]: 10}

    dates = [s.get("seniority_date", "9999-01-01") for s in students]
    min_date = min(dates)
    max_date = max(dates)
    if min_date == max_date:
        return {s["id"]: 10 for s in students}

    from datetime import date as dt_date

    try:
        min_d = dt_date.fromisoformat(min_date)
        max_d = dt_date.fromisoformat(max_date)
    except ValueError:
        return {s["id"]: 5 for s in students}

    date_range_days = max((max_d - min_d).days, 1)

    scores: dict[str, int] = {}
    for st in students:
        try:
            d = dt_date.fromisoformat(st.get("seniority_date", "9999-01-01"))
        except ValueError:
            scores[st["id"]] = 5
            continue
        # Older (closer to min_date) → score closer to 10
        delta = (d - min_d).days
        score = 10 - round(10 * delta / date_range_days)
        scores[st["id"]] = max(0, min(10, score))

    return scores
