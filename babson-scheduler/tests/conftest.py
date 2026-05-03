"""
Shared pytest fixtures.
All tests use an in-memory SQLite database injected via FastAPI dependency override.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.database import create_tables, get_db, seed_default_config
from app.main import app
from app.services.shift_generator import seed_default_templates


@pytest.fixture(scope="session")
def test_engine():
    """In-memory SQLite engine, shared across the test session.

    StaticPool ensures every connection (including those opened by the
    TestClient) reuses the same underlying SQLite connection, so all
    connections see the same schema and data.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_tables(eng)
    seed_default_config(eng)
    with eng.connect() as conn:
        seed_default_templates(conn)
    return eng


@pytest.fixture()
def conn(test_engine):
    """Per-test connection, rolled back after each test."""
    with test_engine.connect() as c:
        yield c


@pytest.fixture()
def client(test_engine):
    """TestClient with DB dependency overridden to use in-memory engine."""

    def override_get_db():
        with test_engine.connect() as c:
            yield c

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Shared test data helpers
# ---------------------------------------------------------------------------

def make_student(client, name="Alice", email=None, seniority_date="2023-09-01",
                 min_hours=8, max_hours=20, target_hours=11):
    email = email or f"{name.lower()}@test.edu"
    resp = client.post("/api/v1/students", json={
        "name": name,
        "email": email,
        "seniority_date": seniority_date,
        "min_hours": min_hours,
        "max_hours": max_hours,
        "target_hours": target_hours,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()
