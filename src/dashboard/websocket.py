"""WebSocket broadcast manager for live dashboard updates.

Single broadcaster instance pushed to by background tasks.
Clients subscribe on connect and receive JSON messages with
the current bot mode and today's snapshot every 5 seconds.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.append(ws)
        logger.info("ws_connect total=%d", len(self._active))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._active:
            self._active.remove(ws)
        logger.info("ws_disconnect total=%d", len(self._active))

    async def broadcast(self, payload: dict) -> None:
        text = json.dumps(payload)
        dead: list[WebSocket] = []
        for ws in list(self._active):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


async def live_pusher(db_path: str, interval_seconds: int = 5) -> None:
    """Background coroutine — pushes today snapshot to all connected clients."""
    import datetime
    from zoneinfo import ZoneInfo

    from .queries import get_today_snapshot

    IST = ZoneInfo("Asia/Kolkata")
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            run_date = datetime.datetime.now(IST).date().isoformat()
            snap = get_today_snapshot(db_path, run_date)
            await manager.broadcast({
                "type": "snapshot",
                "bot_mode": snap.bot_mode,
                "total_pnl": snap.total_pnl,
                "trades_placed": snap.trades_placed,
                "approvals_waiting": snap.approvals_waiting,
                "circuit_breakers_tripped": snap.circuit_breakers_tripped,
                "missed_critical_alerts": snap.missed_critical_alerts,
            })
        except Exception as exc:
            logger.error("live_pusher_error error=%s", exc)
