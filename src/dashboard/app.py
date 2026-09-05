"""Boomer dashboard — single FastAPI file with 4 views.

Views:
  GET  /             → today.html (landing)
  GET  /positions    → positions.html
  GET  /capital      → capital_risk.html
  GET  /system       → system_health.html
  POST /mode         → change bot mode
  GET  /ws           → WebSocket live feed

Auth: HTTP Basic (via BASIC_AUTH_USER / BASIC_AUTH_PASSWORD env vars or reverse-proxy).
"""

from __future__ import annotations

import datetime
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..banner import log_dashboard_online
from .queries import (
    get_capital_view,
    get_open_positions,
    get_recent_errors,
    get_recent_task_runs,
    get_today_snapshot,
)
from .websocket import live_pusher, manager

IST = ZoneInfo("Asia/Kolkata")
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Boomer Dashboard", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

security = HTTPBasic()

DB_PATH: str = os.environ.get("BOOMER_DB_PATH", "/var/lib/boomer/boomer.db")


# ─── Auth ─────────────────────────────────────────────────────────────────────


def _verify_credentials(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> str:
    expected_user = os.environ.get("BASIC_AUTH_USER", "boomer")
    expected_pass = os.environ.get("BASIC_AUTH_PASSWORD", "changeme")
    user_ok = secrets.compare_digest(credentials.username.encode(), expected_user.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), expected_pass.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


AuthDep = Annotated[str, Depends(_verify_credentials)]


# ─── Startup ──────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup() -> None:
    import asyncio

    asyncio.create_task(live_pusher(DB_PATH))
    log_dashboard_online()


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _run_date() -> str:
    return datetime.datetime.now(IST).date().isoformat()


def _write_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ─── View 1: Today ─────────────────────────────────────────────────────────────


@app.get("/boomer")
async def boomer_redirect(_: AuthDep) -> RedirectResponse:
    return RedirectResponse("/", status_code=301)


@app.get("/", response_class=HTMLResponse)
async def today(request: Request, _: AuthDep) -> HTMLResponse:
    snap = get_today_snapshot(DB_PATH, _run_date())
    return templates.TemplateResponse(request, "today.html", {"snap": snap})


# ─── View 2: Positions ─────────────────────────────────────────────────────────


@app.get("/positions", response_class=HTMLResponse)
async def positions(request: Request, _: AuthDep, page: int = 1) -> HTMLResponse:
    PAGE_SIZE = 25
    all_pos = get_open_positions(DB_PATH)
    total = len(all_pos)
    start = (page - 1) * PAGE_SIZE
    page_pos = all_pos[start : start + PAGE_SIZE]
    return templates.TemplateResponse(
        request,
        "positions.html",
        {
            "positions": page_pos,
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "has_next": (start + PAGE_SIZE) < total,
        },
    )


# ─── View 3: Capital & Risk ────────────────────────────────────────────────────


@app.get("/capital", response_class=HTMLResponse)
async def capital_risk(request: Request, _: AuthDep) -> HTMLResponse:
    view = get_capital_view(DB_PATH)
    return templates.TemplateResponse(request, "capital_risk.html", {"capital": view})


# ─── View 4: System Health ────────────────────────────────────────────────────


@app.get("/system", response_class=HTMLResponse)
async def system_health(request: Request, _: AuthDep) -> HTMLResponse:
    runs = get_recent_task_runs(DB_PATH, hours=24)
    errors = get_recent_errors(DB_PATH, limit=50)
    return templates.TemplateResponse(
        request,
        "system_health.html",
        {
            "task_runs": runs,
            "errors": errors,
        },
    )


# ─── Bot mode toggle ──────────────────────────────────────────────────────────


@app.post("/mode")
async def set_mode(mode: Annotated[str, Form()], _: AuthDep) -> RedirectResponse:
    allowed = {"auto", "paused", "emergency_stop"}
    if mode not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid mode. Allowed: {allowed}")
    now = datetime.datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")
    conn = _write_conn()
    try:
        old = conn.execute("SELECT mode FROM bot_mode WHERE id=1").fetchone()
        old_mode = old["mode"] if old else "auto"
        conn.execute(
            "UPDATE bot_mode SET mode=?, changed_at=?, changed_by='operator' WHERE id=1",
            (mode, now),
        )
        conn.execute(
            "INSERT INTO bot_mode_log (old_mode, new_mode, changed_at, changed_by)"
            " VALUES (?,?,?,'operator')",
            (old_mode, mode, now),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/", status_code=303)


# ─── Acknowledge missed critical ──────────────────────────────────────────────


@app.post("/alerts/{alert_id}/acknowledge")
async def ack_missed_alert(alert_id: int, _: AuthDep) -> dict:
    now = datetime.datetime.now(IST).replace(tzinfo=None).isoformat(timespec="seconds")
    conn = _write_conn()
    try:
        conn.execute(
            "UPDATE critical_notification_failures"
            " SET acknowledged=1, acknowledged_at=? WHERE id=?",
            (now, alert_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ─── WebSocket ────────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep connection alive; pushes come from live_pusher
    except WebSocketDisconnect:
        manager.disconnect(ws)
