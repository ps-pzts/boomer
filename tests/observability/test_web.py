"""#105: the observability web app: auth that fails closed, read-only routes, the
WebSocket push, and the security headers. FastAPI TestClient, real paper-day database."""

from __future__ import annotations

import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from observability.web import app as web
from tests.integration.harness import Hostile
from tests.integration.test_scenarios import open_protected

PW = "s3cret-pass"


def auth(pw: str = PW, user: str = "me") -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture()
def day(tmp_path, monkeypatch):
    h = Hostile(tmp_path)
    open_protected(h)
    h.tick(104.2)
    h.step()
    h.step()
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()  # the app uses the real day
    for table in ("events", "intraday_brackets"):
        h.db.execute(f"UPDATE {table} SET session_date=?", (today,))  # noqa: S608
    h.db.commit()
    h.today = today
    path = h.db.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("BOOMER_DB_PATH", path)
    monkeypatch.setenv("BOOMER_DASHBOARD_PASSWORD", PW)
    monkeypatch.delenv("BASIC_AUTH_PASSWORD", raising=False)
    return h


@pytest.fixture()
def client(day):
    return TestClient(web.app)


class TestAuth:
    @pytest.mark.parametrize("path", ["/", "/api/live", "/api/days", "/api/session/2026-11-19"])
    def test_no_credentials_is_401(self, client, path):
        r = client.get(path)
        assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]

    def test_wrong_password_is_401(self, client):
        assert client.get("/api/live", headers=auth("nope")).status_code == 401

    def test_right_password_any_username(self, client):
        assert client.get("/api/live", headers=auth(user="anyone")).status_code == 200

    def test_no_password_configured_refuses_everything(self, client, monkeypatch):
        monkeypatch.delenv("BOOMER_DASHBOARD_PASSWORD")
        assert client.get("/api/live", headers=auth("")).status_code == 401
        assert client.get("/api/live", headers=auth(PW)).status_code == 401

    def test_the_older_variable_still_works(self, client, monkeypatch):
        monkeypatch.delenv("BOOMER_DASHBOARD_PASSWORD")
        monkeypatch.setenv("BASIC_AUTH_PASSWORD", "legacy")
        assert client.get("/api/live", headers=auth("legacy")).status_code == 200

    def test_health_check_needs_no_login_and_shows_no_data(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True}


class TestRoutes:
    def test_live(self, client):
        d = client.get("/api/live", headers=auth()).json()
        assert d["kpis"]["realised"] == 40.0 and d["trades"][0]["state"] == "closed"
        assert client.get("/api/live", headers=auth()).headers["cache-control"] == "no-store"

    def test_trade_timeline_and_404s(self, client):
        trade = client.get("/api/live", headers=auth()).json()["trades"][0]["id"]
        t = client.get(f"/api/trade/{trade}", headers=auth()).json()
        assert t["trade"]["exit_reason"] == "target_hit" and t["events"]
        assert (
            client.get(
                "/api/trade/00000000-0000-0000-0000-000000000000", headers=auth()
            ).status_code
            == 404
        )
        assert client.get("/api/trade/..%2F..%2Fetc", headers=auth()).status_code == 404
        assert client.get("/api/trade/a'; DROP TABLE events;--", headers=auth()).status_code == 404

    def test_session_timeline_validates_the_date(self, client, day):
        ok = client.get(f"/api/session/{day.today}", headers=auth())
        assert ok.status_code == 200 and "events" in ok.json()
        assert client.get("/api/session/yesterday", headers=auth()).status_code == 404
        assert client.get("/api/session/2026-11-19'--", headers=auth()).status_code == 404

    def test_days(self, client):
        rows = client.get("/api/days?n=5", headers=auth()).json()
        assert rows and rows[0]["realised"] == 40.0

    def test_nothing_can_be_changed(self, client):
        for method in ("post", "put", "patch", "delete"):
            r = getattr(client, method)("/api/live", headers=auth())
            assert r.status_code in (404, 405)

    def test_the_app_shell_and_assets(self, client):
        page = client.get("/", headers=auth())
        assert (
            "text/html" in page.headers["content-type"]
            and "<title>Boomer Live</title>" in page.text
        )
        assert 'name="viewport"' in page.text and "viewport-fit=cover" in page.text
        assert client.get("/static/js/main.js").status_code == 200
        assert client.get("/static/app.css").status_code == 200

    def test_security_headers(self, client):
        r = client.get("/", headers=auth())
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "unsafe-inline" not in r.headers["content-security-policy"]

    def test_the_page_uses_no_inline_script_or_style(self, client):
        page = client.get("/", headers=auth()).text
        assert "<script>" not in page and "style=" not in page


class TestWebSocket:
    def test_push_on_connect(self, client):
        with client.websocket_connect("/ws/live", headers=auth()) as ws:
            snap = ws.receive_json()
        assert snap["kpis"]["realised"] == 40.0 and "stream" in snap

    def test_refused_without_the_password(self, client):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as e, client.websocket_connect("/ws/live"):
            pass
        assert e.value.code == 1008

    def test_refused_with_the_wrong_password(self, client):
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws/live", headers=auth("x")),
        ):
            pass

    def test_too_many_sockets_are_turned_away(self, client, monkeypatch):
        from starlette.websockets import WebSocketDisconnect

        monkeypatch.setattr(web, "_sockets", web.MAX_SOCKETS)
        with (
            pytest.raises(WebSocketDisconnect) as e,
            client.websocket_connect("/ws/live", headers=auth()),
        ):
            pass
        assert e.value.code == 1013

    def test_the_socket_count_returns_to_zero(self, client):
        before = web._sockets
        with client.websocket_connect("/ws/live", headers=auth()) as ws:
            ws.receive_json()
        assert web._sockets == before


def test_helper_rejects_a_malformed_basic_header():
    assert web._basic_ok("Basic !!!not-base64") is False
    assert web._basic_ok("Bearer abc") is False
    assert web._basic_ok("") is False
