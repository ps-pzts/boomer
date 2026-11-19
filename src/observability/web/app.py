"""Observability web app (#105): the mobile-first live view of the system.

    uvicorn src.observability.web.app:app --host 0.0.0.0 --port 8080

Read-only: every query runs on a `query_only` connection, and there is no route that
changes anything (steering is Telegram's job: /pause, /emergency, /ack, /toggle).
HTTP Basic auth with BOOMER_DASHBOARD_PASSWORD (or the older BASIC_AUTH_PASSWORD);
with no password set every request is refused, never waved through.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from observability import projection

logger = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
PUSH_SECONDS = 2.0
MAX_SOCKETS = 8
_ID = re.compile(r"^[0-9A-Za-z-]{8,64}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self' ws: wss:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

security = HTTPBasic(auto_error=False)
app = FastAPI(title="Boomer observability", docs_url=None, redoc_url=None, openapi_url=None)
_sockets = 0


def db_path() -> str:
    return os.environ.get("BOOMER_DB_PATH", "/var/lib/boomer/boomer.db")


def _password() -> str:
    return os.environ.get("BOOMER_DASHBOARD_PASSWORD") or os.environ.get("BASIC_AUTH_PASSWORD", "")


def _ok(password: str) -> bool:
    expected = _password()
    return bool(expected) and secrets.compare_digest(password.encode(), expected.encode())


def require_auth(creds: Annotated[HTTPBasicCredentials | None, Depends(security)]) -> None:
    if creds is None or not _ok(creds.password):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Boomer"'},
        )


@app.middleware("http")
async def headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache"  # revalidate: a deploy shows at once
    return response


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}  # unauthenticated, no data: for the deploy check


@app.get("/", include_in_schema=False, dependencies=[Depends(require_auth)])
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", media_type="text/html")


def _waiting(exc: Exception) -> dict:
    return {"ready": False, "reason": f"Waiting for the database: {exc}"}


@app.get("/api/live", dependencies=[Depends(require_auth)])
def api_live() -> dict:
    try:
        return projection.live(db_path())
    except projection.NotReady as exc:
        return _waiting(exc)


@app.get("/api/trade/{trade_id}", dependencies=[Depends(require_auth)])
def api_trade(trade_id: str) -> dict:
    try:
        found = projection.trade_timeline(db_path(), trade_id) if _ID.match(trade_id) else None
    except projection.NotReady as exc:
        raise HTTPException(status_code=503, detail=_waiting(exc)["reason"]) from exc
    if found is None:
        raise HTTPException(status_code=404, detail="No such trade")
    return found


@app.get("/api/session/{day}", dependencies=[Depends(require_auth)])
def api_session(day: str) -> dict:
    if not _DAY.match(day):
        raise HTTPException(status_code=404, detail="Dates look like 2026-11-19")
    try:
        return projection.session_timeline(db_path(), day)
    except projection.NotReady as exc:
        raise HTTPException(status_code=503, detail=_waiting(exc)["reason"]) from exc


@app.get("/api/days", dependencies=[Depends(require_auth)])
def api_days(n: int = 14) -> list[dict]:
    try:
        return projection.days(db_path(), n)
    except projection.NotReady as exc:
        raise HTTPException(status_code=503, detail=_waiting(exc)["reason"]) from exc


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    """Pushes the projection whenever it changes, and at least every 30 s so the
    browser can tell a quiet system from a dead connection."""
    global _sockets
    if not _basic_ok(ws.headers.get("authorization", "")):
        await ws.close(code=1008)
        return
    if _sockets >= MAX_SOCKETS:
        await ws.close(code=1013)
        return
    await ws.accept()
    _sockets += 1
    last, quiet = "", 0.0
    try:
        while True:
            try:
                snap = await asyncio.to_thread(projection.live, db_path())
            except projection.NotReady as exc:
                snap = _waiting(exc)
            fp = projection.fingerprint(snap)
            if fp != last or quiet >= 30:
                await ws.send_json(snap)
                last, quiet = fp, 0.0
            await asyncio.sleep(PUSH_SECONDS)
            quiet += PUSH_SECONDS
    except Exception:  # client went away or the socket closed: nothing to clean up
        logger.debug("ws_live_closed", exc_info=True)
    finally:
        _sockets -= 1


def _basic_ok(header: str) -> bool:
    if not header.lower().startswith("basic "):
        return False
    try:
        _, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except ValueError:
        return False
    return _ok(password)


@app.exception_handler(404)
async def not_found(request: Request, exc) -> JSONResponse:
    return JSONResponse({"detail": getattr(exc, "detail", "Not found")}, status_code=404)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
