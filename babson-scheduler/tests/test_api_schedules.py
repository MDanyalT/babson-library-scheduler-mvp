"""
Integration tests for the full scheduling workflow:
  students → availability → shifts → generate → violations → export
"""
import pytest
from datetime import date, timedelta

WEEK = "2026-05-04"  # A Monday


def _add_student(client, name, email, seniority="2023-01-01", target=11):
    resp = client.post("/api/v1/students", json={
        "name": name, "email": email,
        "seniority_date": seniority, "min_hours": 8, "max_hours": 20, "target_hours": target,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_generate_shifts_for_week(client):
    resp = client.post("/api/v1/shifts/instances/generate", json={
        "week_start_date": WEEK,
        "is_exam_period": False,
        "slots_per_window": 2,
        "force": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["generated"] > 0


def test_full_workflow(client):
    """
    End-to-end: add students, submit availability, generate schedule,
    check violations endpoint, check student summaries.
    """
    # 1. Create students
    s1 = _add_student(client, "Workflow Alice", "walice@babson.edu", "2022-01-01", target=11)
    s2 = _add_student(client, "Workflow Bob", "wbob@babson.edu", "2023-01-01", target=9)
    s3 = _add_student(client, "Workflow Carol", "wcarol@babson.edu", "2023-06-01", target=13)

    # 2. Generate shift instances
    client.post("/api/v1/shifts/instances/generate", json={
        "week_start_date": WEEK, "is_exam_period": False, "slots_per_window": 2, "force": True,
    })

    # 3. Fetch instances to know what to submit availability for
    inst_resp = client.get(f"/api/v1/shifts/instances?week_start_date={WEEK}")
    assert inst_resp.status_code == 200
    instances = inst_resp.json()
    assert len(instances) > 0

    # 4. Submit availability for all students — mark first 10 shifts as preferred
    for student_id in [s1, s2, s3]:
        slots = [
            {"shift_instance_id": inst["id"], "level": "preferred" if i < 10 else "available"}
            for i, inst in enumerate(instances[:30])  # cover 30 shifts
        ]
        avail_resp = client.post("/api/v1/availability", json={
            "student_id": student_id,
            "week_start_date": WEEK,
            "slots": slots,
            "import_source": "api",
        })
        assert avail_resp.status_code == 200, avail_resp.text

    # 5. Generate schedule
    gen_resp = client.post("/api/v1/schedules/generate", json={
        "week_start_date": WEEK,
        "is_exam_period": False,
        "solver_time_limit_seconds": 30,
        "force_regenerate": True,
    })
    assert gen_resp.status_code == 200, gen_resp.text
    run = gen_resp.json()
    run_id = run["id"]
    assert run["total_shifts"] > 0

    # 6. Get full schedule
    full_resp = client.get(f"/api/v1/schedules/{run_id}")
    assert full_resp.status_code == 200
    full = full_resp.json()
    assert "assignments" in full
    assert "student_summaries" in full

    # 7. Check violations endpoint
    viol_resp = client.get(f"/api/v1/violations/{run_id}")
    assert viol_resp.status_code == 200
    violations = viol_resp.json()
    assert isinstance(violations, list)

    # 8. Hard violations endpoint (IBM Orchestrate branch condition)
    hard_resp = client.get(f"/api/v1/violations/{run_id}/hard")
    assert hard_resp.status_code == 200
    hard = hard_resp.json()
    assert isinstance(hard, list)

    # 9. Student summaries
    summ_resp = client.get(f"/api/v1/schedules/{run_id}/student-summaries")
    assert summ_resp.status_code == 200
    summaries = summ_resp.json()
    assert len(summaries) >= 3


def test_coverage_heatmap(client):
    resp = client.get(f"/api/v1/availability/heatmap/{WEEK}")
    assert resp.status_code == 200
    heatmap = resp.json()
    assert isinstance(heatmap, list)
    for entry in heatmap:
        assert "coverage_risk" in entry
        assert entry["coverage_risk"] in ("none", "low", "critical")


def test_preflight_diagnostics(client):
    resp = client.post("/api/v1/diagnostics/preflight", json={
        "week_start_date": WEEK,
        "is_exam_period": False,
    })
    assert resp.status_code == 200
    report = resp.json()
    assert "is_feasible" in report
    assert "findings" in report
    assert isinstance(report["findings"], list)


def test_excel_export(client):
    # Get latest run for the week
    list_resp = client.get(f"/api/v1/schedules?week_start_date={WEEK}")
    assert list_resp.status_code == 200
    runs = list_resp.json()
    if not runs:
        pytest.skip("No schedule runs available for export test")
    run_id = runs[0]["id"]

    export_resp = client.get(f"/api/v1/export/{run_id}/excel")
    assert export_resp.status_code == 200
    assert export_resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert len(export_resp.content) > 0


def test_scheduler_config(client):
    resp = client.get("/api/v1/diagnostics/config")
    assert resp.status_code == 200
    cfg = resp.json()
    assert "min_hours_default" in cfg
    assert "max_consecutive_hours" in cfg
    assert cfg["min_hours_default"] == 8
    assert cfg["max_consecutive_hours"] == 6


def test_manual_override(client):
    # Get a run for the week
    list_resp = client.get(f"/api/v1/schedules?week_start_date={WEEK}")
    assert list_resp.status_code == 200
    runs = list_resp.json()
    if not runs:
        pytest.skip("No runs available")
    run_id = runs[0]["id"]

    # Get assignments
    assign_resp = client.get(f"/api/v1/schedules/{run_id}/assignments")
    assert assign_resp.status_code == 200
    assignments = assign_resp.json()
    if not assignments:
        pytest.skip("No assignments in run")

    # Try to clear the first assignment
    a_id = assignments[0]["id"]
    patch_resp = client.patch(
        f"/api/v1/schedules/{run_id}/assignments/{a_id}",
        json={"student_id": None, "override_reason": "Test override"}
    )
    assert patch_resp.status_code in (200, 422)  # 422 if constraints block it
