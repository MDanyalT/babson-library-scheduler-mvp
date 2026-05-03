"""API tests for /api/v1/students endpoints."""
import pytest


def test_create_student(client):
    resp = client.post("/api/v1/students", json={
        "name": "Test Student",
        "email": "test@babson.edu",
        "seniority_date": "2023-09-01",
        "target_hours": 11,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test Student"
    assert data["target_hours"] == 11
    assert data["min_hours"] == 8,  "min_hours must always be stored as 8"
    assert data["max_hours"] == 20, "max_hours must always be stored as 20"
    assert data["is_active"] is True


def test_create_student_min_max_always_8_20(client):
    """
    Per project rules, min_hours and max_hours are always stored as 8 and 20
    regardless of what the caller submits.  Only target_hours varies per student.
    Even if the caller passes min_hours=15 / max_hours=10, the router overrides
    both to 8 / 20 — so the request must succeed and the DB values must be correct.
    """
    resp = client.post("/api/v1/students", json={
        "name": "Fixed Hours",
        "email": "fixedhours@babson.edu",
        "seniority_date": "2023-09-01",
        "min_hours": 15,   # caller error — should be silently corrected to 8
        "max_hours": 10,   # caller error — should be silently corrected to 20
        "target_hours": 11,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["min_hours"] == 8,  "min_hours must always be 8 (project rule)"
    assert data["max_hours"] == 20, "max_hours must always be 20 (project rule)"
    assert data["target_hours"] == 11


def test_create_student_target_hours_out_of_range(client):
    """target_hours must be between 8 and 20."""
    resp = client.post("/api/v1/students", json={
        "name": "Out Of Range",
        "email": "outofrange@babson.edu",
        "seniority_date": "2023-09-01",
        "target_hours": 5,  # below minimum — must be rejected
    })
    assert resp.status_code == 422


def test_list_students(client):
    # Create two students
    client.post("/api/v1/students", json={
        "name": "Alice", "email": "alice2@babson.edu",
        "seniority_date": "2022-01-01", "min_hours": 8, "max_hours": 20, "target_hours": 11,
    })
    client.post("/api/v1/students", json={
        "name": "Bob", "email": "bob@babson.edu",
        "seniority_date": "2023-01-01", "min_hours": 8, "max_hours": 20, "target_hours": 9,
    })
    resp = client.get("/api/v1/students")
    assert resp.status_code == 200
    names = [s["name"] for s in resp.json()]
    assert "Alice" in names
    assert "Bob" in names


def test_get_student_not_found(client):
    resp = client.get("/api/v1/students/nonexistent-uuid")
    assert resp.status_code == 404


def test_update_student(client):
    # Create then update
    create_resp = client.post("/api/v1/students", json={
        "name": "Charlie", "email": "charlie@babson.edu",
        "seniority_date": "2023-06-01", "min_hours": 8, "max_hours": 20, "target_hours": 9,
    })
    student_id = create_resp.json()["id"]

    update_resp = client.patch(f"/api/v1/students/{student_id}", json={"target_hours": 13})
    assert update_resp.status_code == 200
    assert update_resp.json()["target_hours"] == 13


def test_delete_student_soft(client):
    create_resp = client.post("/api/v1/students", json={
        "name": "Dana", "email": "dana@babson.edu",
        "seniority_date": "2024-01-01", "min_hours": 8, "max_hours": 20, "target_hours": 9,
    })
    student_id = create_resp.json()["id"]

    del_resp = client.delete(f"/api/v1/students/{student_id}")
    assert del_resp.status_code == 200

    # Should not appear in active list
    list_resp = client.get("/api/v1/students")
    ids = [s["id"] for s in list_resp.json()]
    assert student_id not in ids


def test_duplicate_email_rejected(client):
    payload = {
        "name": "Eve", "email": "eve@babson.edu",
        "seniority_date": "2023-01-01", "min_hours": 8, "max_hours": 20, "target_hours": 9,
    }
    client.post("/api/v1/students", json=payload)
    resp2 = client.post("/api/v1/students", json=payload)
    assert resp2.status_code in (409, 422, 500)  # duplicate email must be rejected
