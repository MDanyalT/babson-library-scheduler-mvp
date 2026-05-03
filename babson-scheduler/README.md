# Babson Library Student Worker Scheduler — Backend

Constraint-aware scheduling engine for Babson College Library student workers.
Built with FastAPI + Google OR-Tools CP-SAT. Designed for IBM Orchestrate integration.

---

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Interactive API docs
open http://localhost:8000/docs
```

The server auto-seeds shift templates and scheduler configuration on first launch.

---

## Project Structure

```
app/
├── main.py                   # FastAPI entry point + CORS
├── config.py                 # Library hours, scheduling constants
├── database.py               # SQLite via SQLAlchemy Core
├── models/
│   ├── db_models.py          # Table definitions
│   └── schemas.py            # Pydantic request/response schemas
├── routers/                  # API route handlers
├── services/
│   ├── shift_generator.py    # Slice operating hours into shift blocks
│   ├── schedule_service.py   # Schedule run orchestration
│   └── exporter.py           # Multi-sheet Excel workbook builder
├── solver/
│   └── builder.py            # CP-SAT constraint model
├── diagnostics/
│   ├── preflight.py          # Pre-schedule feasibility checks
│   └── postflight.py         # Post-schedule constraint validation
└── utils/
    └── time_utils.py         # Cross-midnight time arithmetic helpers

orchestrate/
└── openapi.orchestrate.json  # IBM Orchestrate custom extension spec

archive/
└── babson-scheduler-frontend/  # Paused Next.js student availability page
                                # (input source TBD — see §Input Source below)
tests/                        # pytest suite (81 tests)
```

---

## IBM Orchestrate Integration

### What the IBM Orchestrate Layer Does

IBM Orchestrate acts as the **librarian / admin assistant** layer on top of this
scheduling engine. A librarian can prompt Orchestrate in plain English to:

- Run a preflight readiness check before scheduling
- Generate a schedule for a target week
- Ask why the schedule cannot be published
- List students who are under their minimum hours
- Download the schedule as a formatted Excel file

IBM does **not** handle student availability submission — that is a separate
input process (source TBD; see §Input Source below).

### Architecture

```
Librarian (natural language)
        │
        ▼
IBM Orchestrate (chat + workflow engine)
        │  calls REST API
        ▼
FastAPI Backend  ←──────────────── SQLite DB
        │                               │
        ├─ CP-SAT Solver                ├─ students
        ├─ Preflight / Postflight       ├─ shift_instances
        └─ Excel Exporter               ├─ availability
                                        ├─ schedule_runs
                                        └─ violations
```

### Importing the Custom Extension

1. Open **IBM watsonx Orchestrate** → *Skills & apps* → *Add skills* →
   *Import from file* (or *OpenAPI*).
2. Upload `orchestrate/openapi.orchestrate.json`.
3. Set the **server URL** to your deployed backend (or `http://localhost:8000`
   when testing locally via ngrok / IBM's test tunnel).
4. Enable all operations and publish the extension.

> **No authentication is configured by default.** Before a production deploy,
> add API-key or OAuth2 security to both the FastAPI app and the OpenAPI spec.

### Endpoints Included in the Extension

| Operation | Method | Path | Purpose |
|---|---|---|---|
| `checkHealth` | GET | `/health` | Connectivity probe |
| `listStudents` | GET | `/api/v1/students/` | See who is in the roster |
| `generateShiftInstances` | POST | `/api/v1/shifts/instances/generate` | Build shift blocks for a week |
| `runPreflightCheck` | POST | `/api/v1/diagnostics/preflight` | Feasibility check before scheduling |
| `getPreflightSnapshot` | GET | `/api/v1/diagnostics/preflight/{week}` | Retrieve saved preflight report |
| `generateSchedule` | POST | `/api/v1/schedules/generate` | Run the CP-SAT solver |
| `listScheduleRuns` | GET | `/api/v1/schedules/` | List all runs |
| `getSchedule` | GET | `/api/v1/schedules/{run_id}` | Full schedule with assignments |
| `getAssignments` | GET | `/api/v1/schedules/{run_id}/assignments` | Shift-by-shift assignment list |
| `getStudentSummaries` | GET | `/api/v1/schedules/{run_id}/student-summaries` | Per-student hour summaries |
| `getViolations` | GET | `/api/v1/violations/{run_id}` | All violations (hard + soft) |
| `getHardViolations` | GET | `/api/v1/violations/{run_id}/hard` | Hard violations only — branch gate |
| `exportScheduleExcel` | GET | `/api/v1/export/{run_id}/excel` | Download formatted workbook |

### Sample Admin Prompts

These are example natural-language prompts that map to the above operations
once the extension is imported into Orchestrate:

```
"Run a preflight check for the week of May 4, 2026."
→ POST /api/v1/diagnostics/preflight  { week_start_date: "2026-05-04" }

"Generate next week's schedule."
→ POST /api/v1/shifts/instances/generate  +  POST /api/v1/schedules/generate

"Show me hard violations for the latest schedule run."
→ GET /api/v1/schedules/  (find latest run_id)
→ GET /api/v1/violations/{run_id}/hard

"Which students are below minimum hours?"
→ GET /api/v1/schedules/{run_id}/student-summaries
  (filter where constraint_status == "under_minimum")

"Export the schedule to Excel."
→ GET /api/v1/export/{run_id}/excel

"Summarize why the schedule cannot be published."
→ GET /api/v1/violations/{run_id}/hard
  (Orchestrate synthesizes the violation descriptions into a plain summary)

"How many shifts are unfilled this week?"
→ GET /api/v1/schedules/{run_id}  → run.unfilled_shifts

"Is the schedule ready to publish?"
→ GET /api/v1/violations/{run_id}/hard
  → empty list = yes; non-empty = list blocking issues
```

### Testing IBM Admin Actions Locally

```bash
# 1. Start the backend
uvicorn app.main:app --reload --port 8000

# 2. Verify connectivity
curl http://localhost:8000/health

# 3. Generate shifts for the week of 2026-05-04
curl -X POST http://localhost:8000/api/v1/shifts/instances/generate \
  -H "Content-Type: application/json" \
  -d '{"week_start_date":"2026-05-04","slots_per_window":2,"force":true}'

# 4. Run preflight
curl -X POST http://localhost:8000/api/v1/diagnostics/preflight \
  -H "Content-Type: application/json" \
  -d '{"week_start_date":"2026-05-04"}'

# 5. Generate schedule
curl -X POST http://localhost:8000/api/v1/schedules/generate \
  -H "Content-Type: application/json" \
  -d '{"week_start_date":"2026-05-04","solver_time_limit_seconds":60}'

# 6. Check hard violations (copy run_id from step 5 output)
curl http://localhost:8000/api/v1/violations/<run_id>/hard

# 7. Download Excel
curl -o schedule.xlsx http://localhost:8000/api/v1/export/<run_id>/excel

# Or use Swagger UI for a browser-based walkthrough:
open http://localhost:8000/docs
```

---

## Input Source (TBD — Blocked)

The method for **collecting student availability** is not yet confirmed.
Candidates include:

| Option | Status |
|---|---|
| Microsoft Forms → Excel export | Under discussion |
| Google Forms → Sheets export | Under discussion |
| Custom Next.js availability page | **Paused** — code archived at `archive/babson-scheduler-frontend/` |
| Direct API submission (`POST /api/v1/availability/`) | Available now, format confirmed |

The backend availability endpoint (`POST /api/v1/availability/`) is already
implemented and accepts `import_source: "api" | "excel" | "form"`. The IBM
extension will include an availability import operation once the input source
is confirmed.

**Do not implement the importer until the client confirms their workflow.**

---

## Scheduling Engine Notes

- **Solver:** Google OR-Tools CP-SAT (constraint programming, not greedy)
- **Hard constraints:** no overlap, max 6h consecutive, no overnight-to-early sequences, min/max weekly hours
- **Soft constraints:** prefer `preferred` over `available` availability; seniority tiebreak; hours-deficit fairness
- **Postflight validation:** independent of the solver — re-checks all hard and soft constraints after assignment and records violations
- **Hard-shift priority:** early-morning (07:30–09:00) and late-night (23:00–02:00) slots are scheduled first, assigned to the most constrained eligible student

---

## Running Tests

```bash
pytest tests/ -v
# Expected: 81 passed
```
