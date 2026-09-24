import asyncio
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.routers import auth_routes, chat_routes, memory_routes, knowledge_routes, skill_routes, tally_routes, code_routes, agent_routes, tool_routes, google_routes, phone_routes
from app.debug_agent import log_backend_exception
from app import automation_engine

Base.metadata.create_all(bind=engine)


def _ensure_agent_custom_tool_ids_column():
    """Same idempotent pattern as _ensure_conversation_agent_id_column below,
    for Phase 16's new Agent.assigned_custom_tool_ids column (added
    2026-09-20 - see models.py). Base.metadata.create_all above already
    creates the new custom_tools table itself (a brand-new table, not an
    altered one), so only this one column needs the manual ALTER TABLE."""
    inspector = inspect(engine)
    if "agents" not in inspector.get_table_names():
        return
    existing_columns = {c["name"] for c in inspector.get_columns("agents")}
    if "assigned_custom_tool_ids" in existing_columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE agents ADD COLUMN assigned_custom_tool_ids TEXT DEFAULT '[]'"))


def _ensure_agent_allow_email_calendar_column():
    """Same idempotent pattern as _ensure_agent_custom_tool_ids_column above,
    for Phase 19's new Agent.allow_email_calendar column (added 2026-09-22 -
    see models.py). Base.metadata.create_all above already creates the new
    google_accounts table itself (a brand-new table, not an altered one), so
    only this one column needs the manual ALTER TABLE."""
    inspector = inspect(engine)
    if "agents" not in inspector.get_table_names():
        return
    existing_columns = {c["name"] for c in inspector.get_columns("agents")}
    if "allow_email_calendar" in existing_columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE agents ADD COLUMN allow_email_calendar BOOLEAN DEFAULT FALSE"))


def _ensure_conversation_phone_call_sid_column():
    """Same idempotent pattern as every other new-column-on-existing-table
    change in this project (_ensure_conversation_agent_id_column below,
    _ensure_agent_custom_tool_ids_column, _ensure_agent_allow_email_calendar_
    column) - for Phase 20's new Conversation.phone_call_sid column (added
    2026-09-22, see models.py). A plain nullable String column - unlike
    _ensure_agent_allow_email_calendar_column's real Postgres bug earlier
    this project (a BOOLEAN DEFAULT 0 literal, invalid on Postgres though
    fine on SQLite - see progress-tracker.md), a String column has no
    such type/default mismatch to worry about."""
    inspector = inspect(engine)
    if "conversations" not in inspector.get_table_names():
        return
    existing_columns = {c["name"] for c in inspector.get_columns("conversations")}
    if "phone_call_sid" in existing_columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE conversations ADD COLUMN phone_call_sid VARCHAR"))


def _ensure_conversation_agent_id_column():
    """Lightweight, idempotent startup migration (this project deliberately
    has no Alembic - see progress-tracker.md). Base.metadata.create_all above
    only creates tables that don't exist yet; it never alters an existing
    table's columns. Agent Factory v1 added Conversation.agent_id to the
    already-existing "conversations" table, so on a database that predates
    this feature that column has to be added by hand, once. Safe to run on
    every startup (checks first) and a no-op on a brand-new database, where
    create_all above already created the column as part of the table."""
    inspector = inspect(engine)
    if "conversations" not in inspector.get_table_names():
        return
    existing_columns = {c["name"] for c in inspector.get_columns("conversations")}
    if "agent_id" in existing_columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE conversations ADD COLUMN agent_id INTEGER"))


_ensure_conversation_agent_id_column()
_ensure_agent_custom_tool_ids_column()
_ensure_agent_allow_email_calendar_column()
_ensure_conversation_phone_call_sid_column()

app = FastAPI(title="JARVIS Backend")

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://localhost:3001,https://jarvis-five-xi-74.vercel.app",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_routes.router)
app.include_router(chat_routes.router)
app.include_router(memory_routes.router)
app.include_router(knowledge_routes.router)
app.include_router(skill_routes.router)
app.include_router(tally_routes.router)
app.include_router(code_routes.router)
app.include_router(agent_routes.router)
app.include_router(tool_routes.router)
app.include_router(google_routes.router)
app.include_router(phone_routes.router)


# Phase 13 "Debugging Agent" (2026-09-20): permanently logs every unhandled
# backend exception (see debug_agent.py) so JARVIS - or a future debugging
# session - has real evidence to investigate instead of a throwaway
# hand-added debug print, the same evidence-first pattern already used for
# Tally failures. Deliberately narrow: a raised HTTPException (a normal
# 404/400/403 an endpoint raises on purpose) is NOT a bug and is left to
# FastAPI's own default handling untouched below - only a genuinely
# unexpected exception is logged and turned into a plain 500.
@app.exception_handler(Exception)
async def _log_and_handle_unhandled_exception(request: Request, exc: Exception):
    if isinstance(exc, StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    log_backend_exception(request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/")
def root():
    return {"status": "JARVIS backend is running"}


# Phase 18 "Automation" (added 2026-09-20, scoped with Sudeep via
# AskUserQuestion - see progress-tracker.md and automation_engine.py).
# JARVIS's backend only runs while Sudeep has it open (there's no 24/7
# Windows service - see progress-tracker.md), so scheduling uses a "catch
# up on next open" model: _run_due_automations_once() runs once at startup
# (catches anything that came due while it was closed) and then again on
# every tick of _automation_background_loop() below, so a daily/weekly
# automation whose time arrives WHILE JARVIS is already open still fires
# close to on time, not only on the next restart.
def _run_due_automations_once() -> None:
    """One pass of automation_engine.check_and_run_due, using a fresh DB
    session - this runs outside any request, so it can never reuse a
    request-scoped session (same reasoning as chat_routes._update_memories's
    own background-task session). Never raises - an unexpected failure here
    (logged the same way a real request's unhandled exception is) should
    never crash startup or stop the loop from trying again next tick."""
    if not settings.automation_engine_enabled:
        return
    db = SessionLocal()
    try:
        automation_engine.check_and_run_due(db)
    except Exception as e:
        log_backend_exception("SCHEDULER", "/internal/automation_check", e)
    finally:
        db.close()


async def _automation_background_loop() -> None:
    while True:
        await asyncio.sleep(max(settings.automation_check_interval_seconds, 10))
        # Run in a worker thread - a due automation's run is a real chat
        # reply (a real AI provider call, possibly a web search or two),
        # which must never block the event loop the rest of the backend
        # depends on.
        await asyncio.to_thread(_run_due_automations_once)


@app.on_event("startup")
async def _start_automation_engine() -> None:
    await asyncio.to_thread(_run_due_automations_once)
    asyncio.create_task(_automation_background_loop())
