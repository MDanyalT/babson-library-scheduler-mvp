"""
Admin utilities — database reset, status, one-shot workflow, and simple admin UI.

CAUTION: The reset endpoint permanently deletes all student / scheduling data.
Intended for demo setup and development only.  Shift templates and scheduler
config are preserved (they are seeded system data, not user data).

MVP scope — term_recurring mode
--------------------------------
``POST /run-weekly-workflow`` with ``schedule_mode=term_recurring`` creates ONE
optimised standard weekly schedule that staff can treat as the repeating pattern
for the semester.  The solver runs once against the representative week; it does
NOT generate 14 separate per-week schedules.

Future layers (holidays, exam periods, one-off absences) are planned as separate
exception-override endpoints and are not part of this MVP.
"""

import tempfile
import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.database import get_db
from app.models.db_models import ScheduleMode

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])

# Tables cleared on reset, in FK-safe order (children before parents)
_RESET_TABLES = [
    "violations",
    "assignments",
    "schedule_runs",
    "availability",
    "shift_instances",
    "students",
]


@router.post("/reset", response_model=dict)
def reset_data(
    confirm: str = Query(..., description="Must be the string 'RESET' to proceed"),
    conn: Connection = Depends(get_db),
):
    """
    Delete all scheduling data for a clean demo run.

    **Tables cleared:** violations, assignments, schedule_runs, availability,
    shift_instances, students.

    **Tables retained:** shift_templates, scheduler_config.

    Pass ``?confirm=RESET`` to proceed.  Any other value returns 422.
    """
    if confirm != "RESET":
        raise HTTPException(
            status_code=422,
            detail=(
                "Safety check failed. "
                "Pass ?confirm=RESET to confirm permanent deletion of all scheduling data."
            ),
        )

    rows_deleted: dict[str, int] = {}
    try:
        for table in _RESET_TABLES:
            row = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).fetchone()
            rows_deleted[table] = row[0] if row else 0
            conn.execute(text(f"DELETE FROM {table}"))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Reset failed: {exc}")

    return {
        "message": "Database reset complete. All scheduling data cleared.",
        "rows_deleted": rows_deleted,
        "tables_retained": ["shift_templates", "scheduler_config"],
    }


@router.get("/status", response_model=dict)
def database_status(conn: Connection = Depends(get_db)):
    """
    Row counts for every table — use this to verify a clean state before a demo.
    """
    all_tables = [
        "students",
        "shift_templates",
        "shift_instances",
        "availability",
        "schedule_runs",
        "assignments",
        "violations",
        "scheduler_config",
    ]
    counts: dict[str, int] = {}
    for table in all_tables:
        row = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).fetchone()
        counts[table] = row[0] if row else 0
    return {"table_counts": counts}


# ---------------------------------------------------------------------------
# POST /run-weekly-workflow
# ---------------------------------------------------------------------------

@router.post("/run-weekly-workflow", response_model=dict)
async def run_weekly_workflow(
    file: UploadFile = File(..., description="Client availability Excel (.xlsx)"),
    week_start_date: str = Form(..., description="Representative Monday (YYYY-MM-DD)"),
    schedule_mode: str = Form(
        ScheduleMode.TERM_RECURRING,
        description=(
            "'term_recurring' — one optimised standard weekly schedule for the semester. "
            "'weekly' — a single one-week schedule."
        ),
    ),
    term_start_date: str = Form(
        None,
        description="First day of the semester (YYYY-MM-DD). Informational; stored on the run.",
    ),
    term_end_date: str = Form(
        None,
        description="Last day of the semester (YYYY-MM-DD). Informational; stored on the run.",
    ),
    replace_existing: bool = Form(True),
    conn: Connection = Depends(get_db),
):
    """
    One-shot workflow: import availability Excel → generate schedule → return run ID.

    In **term_recurring** mode the solver runs once against the representative week
    and produces a standard weekly pattern for the semester.  It does NOT generate
    per-week schedules.  Holidays, breaks, and one-off absences are a future layer.

    In **weekly** mode the behaviour is identical to calling
    ``POST /api/v1/availability/import-excel`` then
    ``POST /api/v1/schedules/generate`` manually.

    Returns ``run_id`` plus a summary so the caller can immediately fetch the
    Excel export from ``GET /api/v1/export/{run_id}/excel``.
    """
    if schedule_mode not in (ScheduleMode.WEEKLY, ScheduleMode.TERM_RECURRING):
        raise HTTPException(
            status_code=422,
            detail=f"schedule_mode must be 'weekly' or 'term_recurring', got '{schedule_mode}'",
        )

    # --- 1. Save upload to a temp file ---
    suffix = os.path.splitext(file.filename or ".xlsx")[1] or ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        # --- 2. Import availability ---
        from app.intake.excel_matrix import import_matrix
        import_result = import_matrix(
            tmp_path,
            week_start_date,
            conn,
            replace_existing=replace_existing,
            auto_generate_shifts=True,
        )
    finally:
        os.unlink(tmp_path)

    if import_result.errors:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Availability import failed",
                "errors": import_result.errors,
            },
        )

    # --- 3. Generate schedule ---
    from app.services.schedule_service import generate_schedule
    try:
        result = generate_schedule(
            conn,
            week_start_date,
            force_regenerate=True,
            schedule_mode=schedule_mode,
            term_start_date=term_start_date or None,
            term_end_date=term_end_date or None,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    run = result.get("run", {}) if isinstance(result, dict) else {}
    run_id = run.get("id") if isinstance(run, dict) else None

    return {
        "run_id": run_id,
        "schedule_mode": schedule_mode,
        "week_start_date": week_start_date,
        "term_start_date": term_start_date,
        "term_end_date": term_end_date,
        "students_imported": import_result.students_processed,
        "students_created": import_result.students_created,
        "students_updated": import_result.students_updated,
        "availability_slots_inserted": import_result.availability_slots_inserted,
        "unmatched_windows": import_result.unmatched_windows,
        "status": run.get("status") if isinstance(run, dict) else None,
        "solver_status": run.get("solver_status") if isinstance(run, dict) else None,
        "download_url": f"/api/v1/export/{run_id}/excel" if run_id else None,
    }


# ---------------------------------------------------------------------------
# GET /ui  — simple browser admin page
# ---------------------------------------------------------------------------

@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def admin_ui():
    """Minimal browser admin page for the one-shot workflow."""
    return HTMLResponse(_ADMIN_HTML)


_ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Babson Scheduler — Admin</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:780px;margin:40px auto;padding:0 20px;background:#f5f7fa;color:#222}
  h1{color:#1f3864;margin-bottom:4px}
  h2{color:#2e4a7a;font-size:1rem;margin:28px 0 8px}
  p.sub{color:#555;font-size:.9rem;margin:0 0 20px}
  .card{background:#fff;border-radius:8px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,.1);margin-bottom:24px}
  label{display:block;font-size:.88rem;font-weight:600;margin:14px 0 4px;color:#333}
  input[type=text],input[type=date],select,input[type=file]{
    width:100%;padding:8px 10px;border:1px solid #ccc;border-radius:5px;font-size:.93rem;box-sizing:border-box}
  input[type=file]{padding:5px}
  .hint{font-size:.8rem;color:#666;margin:3px 0 0}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .hidden{display:none}
  button{background:#1f3864;color:#fff;border:none;padding:10px 28px;border-radius:5px;
         font-size:1rem;cursor:pointer;margin-top:18px}
  button:hover{background:#2e4a7a}
  button.danger{background:#c0392b}
  button.danger:hover{background:#922b21}
  #result{background:#eaf4ea;border:1px solid #27ae60;border-radius:5px;padding:14px;
           margin-top:18px;white-space:pre-wrap;font-size:.85rem;font-family:monospace;display:none}
  #result.error{background:#fdecea;border-color:#e74c3c}
  #status-table{width:100%;border-collapse:collapse;font-size:.9rem}
  #status-table th{background:#1f3864;color:#fff;padding:6px 10px;text-align:left}
  #status-table td{padding:5px 10px;border-bottom:1px solid #eee}
  #status-table tr:nth-child(even) td{background:#f7f9fc}
  .notice{background:#fff3cd;border:1px solid #ffc107;border-radius:5px;padding:10px 14px;
           font-size:.85rem;margin-bottom:12px}
</style>
</head>
<body>
<h1>Babson Library Scheduler</h1>
<p class="sub">Admin workflow — Generate Schedule</p>

<!-- ── Schedule form ─────────────────────────────────────────────────────── -->
<div class="card">
  <h2>Generate Schedule</h2>
  <div class="notice">
    <strong>Recurring semester schedule</strong> runs the solver once against the
    representative week and produces a standard weekly pattern for the full term.
    Holidays, exam periods, and one-off absences are a future exception layer.
  </div>

  <form id="workflow-form">
    <label>Availability Excel File (.xlsx)
      <input type="file" name="file" id="file" accept=".xlsx" required/>
    </label>

    <label>Schedule Type
      <select name="schedule_mode" id="schedule_mode" onchange="onModeChange()">
        <option value="term_recurring" selected>Recurring semester schedule</option>
        <option value="weekly">One-week schedule</option>
      </select>
    </label>
    <p class="hint">
      <span id="mode-hint">Creates one optimised weekly pattern repeated across the term.</span>
    </p>

    <label>Representative Week Start (Monday)
      <input type="date" name="week_start_date" id="week_start_date" required/>
    </label>
    <p class="hint">The Monday whose shift windows are used as the schedule template.</p>

    <div id="term-fields">
      <div class="row">
        <div>
          <label>Term Start Date (optional)
            <input type="date" name="term_start_date" id="term_start_date"/>
          </label>
          <p class="hint">Stored on the schedule run for reference.</p>
        </div>
        <div>
          <label>Term End Date (optional)
            <input type="date" name="term_end_date" id="term_end_date"/>
          </label>
        </div>
      </div>
    </div>

    <button type="submit" id="submit-btn">Generate Schedule</button>
  </form>
  <div id="result"></div>
</div>

<!-- ── DB status ─────────────────────────────────────────────────────────── -->
<div class="card">
  <h2>Database Status</h2>
  <button type="button" onclick="loadStatus()" style="margin-top:0;padding:6px 16px;font-size:.85rem">Refresh</button>
  <div id="status-area" style="margin-top:12px"></div>
</div>

<!-- ── Reset ─────────────────────────────────────────────────────────────── -->
<div class="card">
  <h2>Reset All Data</h2>
  <p class="hint">Clears students, availability, shift instances, schedules, and violations.
     Retains shift templates and scheduler config.</p>
  <button class="danger" onclick="resetDB()">Reset Database</button>
  <div id="reset-result" style="margin-top:12px;font-size:.85rem"></div>
</div>

<script>
function onModeChange(){
  const mode = document.getElementById('schedule_mode').value;
  const termFields = document.getElementById('term-fields');
  const hint = document.getElementById('mode-hint');
  if(mode === 'term_recurring'){
    termFields.classList.remove('hidden');
    hint.textContent = 'Creates one optimised weekly pattern repeated across the term.';
  } else {
    termFields.classList.add('hidden');
    hint.textContent = 'Generates a schedule for the single representative week only.';
  }
}

document.getElementById('workflow-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('submit-btn');
  const resultEl = document.getElementById('result');
  btn.disabled = true;
  btn.textContent = 'Running…';
  resultEl.style.display = 'none';

  const fd = new FormData(e.target);

  // Remove empty optional fields so the server sees None not ""
  ['term_start_date','term_end_date'].forEach(k => {
    if(!fd.get(k)) fd.delete(k);
  });

  try {
    const resp = await fetch('/api/v1/admin/run-weekly-workflow', {method:'POST', body:fd});
    const data = await resp.json();
    resultEl.className = resp.ok ? '' : 'error';
    if(resp.ok){
      // Build the absolute download URL, handling both relative and absolute server values
      const rawUrl = data.download_url || data.export_url || '';
      const dlUrl = rawUrl.startsWith('http') ? rawUrl : window.location.origin + rawUrl;

      // Stats lines (no "Status: draft" clutter)
      const statLines = [
        ['Run ID', data.run_id],
        ['Mode', data.schedule_mode === 'term_recurring' ? 'Recurring semester schedule' : 'One-week schedule'],
        ['Solver', data.solver_status],
        ['Students imported', data.students_imported],
        ['Availability slots', data.availability_slots_inserted],
      ];
      if(data.term_start_date)
        statLines.splice(2, 0, ['Term', data.term_start_date + ' → ' + (data.term_end_date || '?')]);

      const statsHtml = statLines.map(([k,v]) =>
        `<span style="display:inline-block;min-width:170px;font-weight:600">${k}:</span>${v}`
      ).join('<br>');

      const warnHtml = (data.unmatched_windows && data.unmatched_windows.length)
        ? `<div style="margin-top:12px;padding:8px 12px;background:#fff3cd;border:1px solid #ffc107;
               border-radius:4px;font-size:.82rem">
             <strong>⚠ Unmatched windows</strong> (no shift instance created):<br>
             ${data.unmatched_windows.map(w => `<code>${w}</code>`).join('<br>')}
           </div>`
        : '';

      resultEl.innerHTML = `
        <div style="font-size:.95rem;font-weight:700;margin-bottom:10px;color:#155724">✓ Schedule generated</div>
        <div style="font-family:monospace;font-size:.83rem;line-height:1.7">${statsHtml}</div>
        ${warnHtml}
        <div style="margin-top:18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap">
          <a href="${dlUrl}" target="_blank"
             style="display:inline-block;background:#1f3864;color:#fff;padding:10px 22px;
                    border-radius:5px;text-decoration:none;font-weight:600;font-size:.95rem;
                    transition:background .15s"
             onmouseover="this.style.background='#2e4a7a'"
             onmouseout="this.style.background='#1f3864'">
            ⬇ Download Schedule Workbook
          </a>
          <a href="/docs" target="_blank"
             style="display:inline-block;background:#e8f0fe;color:#1f3864;padding:10px 18px;
                    border-radius:5px;text-decoration:none;font-weight:600;font-size:.88rem;border:1px solid #c5d3f5">
            Open Swagger Docs
          </a>
        </div>
        <div style="margin-top:8px;font-size:.75rem;color:#666">${dlUrl}</div>`;
    } else {
      resultEl.className = 'error';
      resultEl.textContent = JSON.stringify(data, null, 2);
    }
  } catch(err){
    resultEl.className = 'error';
    resultEl.textContent = 'Network error: ' + err.message;
  }
  resultEl.style.display = 'block';
  btn.disabled = false;
  btn.textContent = 'Generate Schedule';
  loadStatus();
});

async function loadStatus(){
  const area = document.getElementById('status-area');
  try {
    const resp = await fetch('/api/v1/admin/status');
    const data = await resp.json();
    const counts = data.table_counts;
    const rows = Object.entries(counts).map(([t,c]) =>
      `<tr><td>${t}</td><td>${c}</td></tr>`).join('');
    area.innerHTML = `<table id="status-table"><thead><tr><th>Table</th><th>Rows</th></tr></thead>
                      <tbody>${rows}</tbody></table>`;
  } catch(e){
    area.textContent = 'Could not load status.';
  }
}

async function resetDB(){
  if(!confirm('Reset ALL scheduling data? This cannot be undone.')) return;
  const el = document.getElementById('reset-result');
  try {
    const resp = await fetch('/api/v1/admin/reset?confirm=RESET', {method:'POST'});
    const data = await resp.json();
    el.style.color = resp.ok ? '#27ae60' : '#c0392b';
    el.textContent = resp.ok ? '✓ ' + data.message : JSON.stringify(data);
  } catch(e){
    el.textContent = 'Error: ' + e.message;
  }
  loadStatus();
}

loadStatus();
</script>
</body>
</html>"""
