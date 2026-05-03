"""
Database setup — SQLAlchemy Core, SQLite (default) or PostgreSQL.
Tables are created via CREATE TABLE IF NOT EXISTS on startup.
Switch backends by changing DATABASE_URL in .env — no other code changes required.
"""

from __future__ import annotations

import json
import os
from typing import Generator

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

load_dotenv()

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./scheduler.db")
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine: Engine = create_engine(DATABASE_URL, connect_args=_connect_args)


# ---------------------------------------------------------------------------
# Table creation
# ---------------------------------------------------------------------------

def create_tables(eng: Engine | None = None) -> None:
    """Create all tables. Safe to call multiple times (IF NOT EXISTS)."""
    target = eng or engine
    with target.connect() as conn:
        # ---- students ----
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS students (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                email           TEXT NOT NULL UNIQUE,
                seniority_date  TEXT NOT NULL,
                min_hours       INTEGER NOT NULL DEFAULT 8,
                max_hours       INTEGER NOT NULL DEFAULT 20,
                target_hours    INTEGER NOT NULL DEFAULT 8,
                is_active       INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT NOT NULL
            )
        """))

        # ---- shift_templates ----
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS shift_templates (
                id              TEXT PRIMARY KEY,
                day_of_week     INTEGER NOT NULL,
                start_time      TEXT NOT NULL,
                end_time        TEXT NOT NULL,
                duration_hours  REAL NOT NULL,
                is_hard_shift   INTEGER NOT NULL DEFAULT 0,
                label           TEXT,
                created_by      TEXT NOT NULL DEFAULT 'system',
                is_active       INTEGER NOT NULL DEFAULT 1
            )
        """))

        # ---- shift_instances ----
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS shift_instances (
                id                  TEXT PRIMARY KEY,
                template_id         TEXT,
                week_start_date     TEXT NOT NULL,
                date                TEXT NOT NULL,
                day_of_week         INTEGER NOT NULL,
                start_time          TEXT NOT NULL,
                end_time            TEXT NOT NULL,
                duration_hours      REAL NOT NULL,
                is_hard_shift       INTEGER NOT NULL DEFAULT 0,
                is_exam_period      INTEGER NOT NULL DEFAULT 0,
                slot_index          INTEGER NOT NULL DEFAULT 0,
                coverage_required   INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (template_id) REFERENCES shift_templates(id)
            )
        """))

        # ---- availability ----
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS availability (
                id                  TEXT PRIMARY KEY,
                student_id          TEXT NOT NULL,
                week_start_date     TEXT NOT NULL,
                shift_instance_id   TEXT,
                day_of_week         INTEGER NOT NULL,
                start_time          TEXT NOT NULL,
                end_time            TEXT NOT NULL,
                level               TEXT NOT NULL,
                submitted_at        TEXT NOT NULL,
                import_source       TEXT NOT NULL DEFAULT 'api',
                FOREIGN KEY (student_id) REFERENCES students(id),
                FOREIGN KEY (shift_instance_id) REFERENCES shift_instances(id)
            )
        """))

        # ---- schedule_runs ----
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS schedule_runs (
                id              TEXT PRIMARY KEY,
                week_start_date TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'draft',
                solver_status   TEXT,
                solve_time_ms   INTEGER,
                objective_score REAL,
                generated_at    TEXT NOT NULL,
                published_at    TEXT,
                notes           TEXT,
                schedule_mode   TEXT NOT NULL DEFAULT 'weekly',
                term_start_date TEXT,
                term_end_date   TEXT
            )
        """))

        # ---- assignments ----
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS assignments (
                id                      TEXT PRIMARY KEY,
                run_id                  TEXT NOT NULL,
                shift_instance_id       TEXT NOT NULL,
                student_id              TEXT,
                preference_level_used   TEXT,
                reason_codes            TEXT,
                is_locked               INTEGER NOT NULL DEFAULT 0,
                is_manual_override      INTEGER NOT NULL DEFAULT 0,
                override_reason         TEXT,
                assigned_at             TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES schedule_runs(id),
                FOREIGN KEY (shift_instance_id) REFERENCES shift_instances(id),
                FOREIGN KEY (student_id) REFERENCES students(id)
            )
        """))

        # ---- violations ----
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS violations (
                id                  TEXT PRIMARY KEY,
                run_id              TEXT NOT NULL,
                violation_type      TEXT NOT NULL,
                shift_instance_id   TEXT,
                student_id          TEXT,
                description         TEXT NOT NULL,
                severity            TEXT NOT NULL,
                source              TEXT NOT NULL DEFAULT 'postflight',
                FOREIGN KEY (run_id) REFERENCES schedule_runs(id)
            )
        """))

        # ---- diagnostics_snapshots ----
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS diagnostics_snapshots (
                id              TEXT PRIMARY KEY,
                week_start_date TEXT NOT NULL,
                run_id          TEXT,
                snapshot_type   TEXT NOT NULL,
                findings        TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )
        """))

        # ---- scheduler_config ----
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS scheduler_config (
                id                          INTEGER PRIMARY KEY DEFAULT 1,
                min_hours_default           INTEGER NOT NULL DEFAULT 8,
                max_hours_default           INTEGER NOT NULL DEFAULT 20,
                max_consecutive_hours       INTEGER NOT NULL DEFAULT 6,
                shift_block_minutes         INTEGER NOT NULL DEFAULT 120,
                stagger_overlap_minutes     INTEGER NOT NULL DEFAULT 5,
                hard_shift_windows          TEXT NOT NULL DEFAULT '[]',
                exam_min_hours              INTEGER NOT NULL DEFAULT 12,
                exam_max_hours              INTEGER NOT NULL DEFAULT 20,
                solver_time_limit_seconds   INTEGER NOT NULL DEFAULT 60,
                updated_at                  TEXT NOT NULL
            )
        """))
        conn.commit()

    # Apply incremental migrations for databases that predate new columns.
    _migrate_schedule_runs(target)


def _migrate_schedule_runs(eng: Engine) -> None:
    """Add schedule_mode / term_start_date / term_end_date if absent."""
    new_columns = [
        ("schedule_mode",   "TEXT NOT NULL DEFAULT 'weekly'"),
        ("term_start_date", "TEXT"),
        ("term_end_date",   "TEXT"),
    ]
    with eng.connect() as conn:
        existing = {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(schedule_runs)")
            ).fetchall()
        }
        for col_name, col_def in new_columns:
            if col_name not in existing:
                conn.execute(
                    text(f"ALTER TABLE schedule_runs ADD COLUMN {col_name} {col_def}")
                )
        conn.commit()


def seed_default_config(eng: Engine | None = None) -> None:
    """Insert default scheduler_config row if absent."""
    from app.config import (
        DEFAULT_MAX_CONSECUTIVE_HOURS,
        DEFAULT_MAX_HOURS,
        DEFAULT_MIN_HOURS,
        DEFAULT_SHIFT_BLOCK_MINUTES,
        DEFAULT_SOLVER_TIME_LIMIT_SECONDS,
        DEFAULT_STAGGER_OVERLAP_MINUTES,
        DEFAULT_EXAM_MIN_HOURS,
        DEFAULT_EXAM_MAX_HOURS,
        HARD_SHIFT_WINDOWS,
    )
    from app.utils.time_utils import iso_now

    target = eng or engine
    with target.connect() as conn:
        exists = conn.execute(
            text("SELECT id FROM scheduler_config WHERE id = 1")
        ).fetchone()
        if not exists:
            conn.execute(text("""
                INSERT INTO scheduler_config
                    (id, min_hours_default, max_hours_default, max_consecutive_hours,
                     shift_block_minutes, stagger_overlap_minutes, hard_shift_windows,
                     exam_min_hours, exam_max_hours, solver_time_limit_seconds, updated_at)
                VALUES
                    (1, :min_h, :max_h, :max_consec, :block_min, :stagger,
                     :hard_windows, :exam_min, :exam_max, :solver_limit, :now)
            """), {
                "min_h": DEFAULT_MIN_HOURS,
                "max_h": DEFAULT_MAX_HOURS,
                "max_consec": DEFAULT_MAX_CONSECUTIVE_HOURS,
                "block_min": DEFAULT_SHIFT_BLOCK_MINUTES,
                "stagger": DEFAULT_STAGGER_OVERLAP_MINUTES,
                "hard_windows": json.dumps(HARD_SHIFT_WINDOWS),
                "exam_min": DEFAULT_EXAM_MIN_HOURS,
                "exam_max": DEFAULT_EXAM_MAX_HOURS,
                "solver_limit": DEFAULT_SOLVER_TIME_LIMIT_SECONDS,
                "now": iso_now(),
            })
            conn.commit()


def get_config(conn: Connection) -> dict:
    """Fetch the live scheduler_config row as a dict."""
    import json
    row = conn.execute(
        text("SELECT * FROM scheduler_config WHERE id = 1")
    ).fetchone()
    if not row:
        raise RuntimeError("scheduler_config is empty — run seed_default_config() at startup")
    d = dict(row._mapping)
    d["hard_shift_windows"] = json.loads(d["hard_shift_windows"])
    return d


def get_db() -> Generator[Connection, None, None]:
    """FastAPI dependency yielding a SQLAlchemy connection."""
    with engine.connect() as conn:
        yield conn
