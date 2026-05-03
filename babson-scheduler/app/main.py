from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import create_tables, engine, seed_default_config
from app.services.shift_generator import seed_default_templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    create_tables(engine)
    seed_default_config(engine)
    with engine.connect() as conn:
        seed_default_templates(conn)
    yield
    # Shutdown (nothing needed)


app = FastAPI(
    title="Babson Library Student Worker Scheduler",
    version="2.0.0",
    description="""
    Constraint-aware scheduling system for Babson Library student workers.

    Uses Google OR-Tools CP-SAT for optimization-based schedule generation.
    Designed for IBM Orchestrate integration via OpenAPI custom extension.
    """,
    lifespan=lifespan,
    servers=[
        {"url": "http://localhost:8000", "description": "Local development"},
        {"url": "https://your-deployment-host.com", "description": "Production"},
    ],
)

# ── CORS — permit browser-based admin tools and local development ─────────────
# Kept intentionally broad for localhost so Swagger UI, any future admin
# dashboard, and IBM Orchestrate's browser test-runner can all reach the API
# without additional config.  Tighten `allow_origins` before a public deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5000",
        "http://localhost:8080",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register all routers
from app.routers import admin, availability, diagnostics, export, schedules, shifts, students, violations

app.include_router(students.router)
app.include_router(availability.router)
app.include_router(shifts.router)
app.include_router(schedules.router)
app.include_router(diagnostics.router)
app.include_router(violations.router)
app.include_router(export.router)
app.include_router(admin.router)


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "version": "2.0.0"}
