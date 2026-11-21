"""Tests for the NSE ASM/GSM surveillance fetcher (#50). No real HTTP.

The response shapes below are ASSUMED, not captured from the live NSE site (the
endpoint was unreachable when this was written); the parser is deliberately
schema-agnostic and these tests pin that behaviour, not NSE's format.

Worked example: ASM JSON with a long-term and a short-term group
    longterm: AAA (stage "I"), BBB (stage "II");  shortterm: AAA again, CCC
    -> 3 distinct symbols: AAA (first stage wins), BBB, CCC.
"""

from __future__ import annotations

import glob
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from collector.base import PermanentFetchError
from collector.fetchers.surveillance import SurveillanceFetcher, extract_rows
from collector.models import DataSource, FetchResult


@pytest.fixture()
def db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "b.db"))
    for f in sorted(glob.glob("migrations/*.sql")):
        with open(f) as fh:
            conn.executescript(fh.read())
    return conn


@pytest.fixture()
def fetcher(db, tmp_path) -> SurveillanceFetcher:
    return SurveillanceFetcher(db, Path(tmp_path / "raw"))


def _resp(status: int = 200, body: bytes = b"", url: str = "https://x/y") -> MagicMock:
    r = MagicMock()
    r.status_code, r.content, r.url = status, body, url
    return r


ASM_JSON = json.dumps(
    {
        "longterm": {"data": [{"symbol": "AAA", "isin": "INE1", "asmSurvIndicator": "I"}]},
        "shortterm": {
            "data": [{"symbol": "bbb", "survStage": "II"}, {"symbol": "AAA"}, {"symbol": "CCC"}]
        },
    }
).encode()


class TestExtractRows:
    def test_nested_json_distinct_symbols_uppercased(self):
        rows = extract_rows(ASM_JSON)
        assert [r[0] for r in rows] == ["AAA", "BBB", "CCC"]
        assert rows[0] == ("AAA", "INE1", "I")
        assert rows[1][2] == "II"
        assert rows[2][2] == "shortterm"  # no stage field: falls back to the group name

    def test_flat_json_list(self):
        assert [r[0] for r in extract_rows(b'[{"Symbol": "X"}, {"Symbol": " y "}]')] == ["X", "Y"]

    def test_csv(self):
        body = b"Sr No,Symbol,Company,ISIN,Stage\n1,AAA,A Ltd,INE1,III\n2,BBB,B Ltd,,\n"
        assert extract_rows(body) == [("AAA", "INE1", "III"), ("BBB", None, None)]

    def test_csv_with_bom(self):
        assert extract_rows(b"\xef\xbb\xbfSymbol\nAAA\n") == [("AAA", None, None)]

    @pytest.mark.parametrize(
        "body", [b"", b"   ", b"{}", b"[]", b'{"data": []}', b"<html>x</html>"]
    )
    def test_no_symbols(self, body):
        assert extract_rows(body) == []


class TestValidate:
    def _result(self, status, body=b""):
        return FetchResult(DataSource.SURVEILLANCE, "u", status, body, "h", None, None)  # type: ignore[arg-type]

    def test_404_is_permanent(self, fetcher):
        with pytest.raises(PermanentFetchError):
            fetcher.validate(self._result(404))

    def test_non_200_is_retryable(self, fetcher):
        with pytest.raises(ValueError, match="HTTP 403"):
            fetcher.validate(self._result(403))

    def test_empty_payload_rejected_never_recorded_as_empty_list(self, fetcher):
        """An empty/reshaped response must not look like 'nothing under surveillance'."""
        with pytest.raises(ValueError, match="no symbols"):
            fetcher.validate(self._result(200, b'{"data": []}'))

    def test_ok(self, fetcher):
        fetcher.validate(self._result(200, ASM_JSON))


class TestFetchAndParse:
    def test_cookie_handshake_then_api_then_parse(self, fetcher, db):
        calls: list[str] = []

        def get(url, **kw):
            calls.append(url)
            if url.endswith("reportASM") or url.endswith("reportGSM"):
                assert "json" in kw["headers"]["Accept"]
                return _resp(200, ASM_JSON, url)
            return _resp(200, b"<html>", url)  # home / report pages set the cookies

        fetcher._session.get = get  # type: ignore[method-assign]
        stored = fetcher.refresh()
        assert stored == {"ASM": 3, "GSM": 3}
        # handshake (home, page) precedes each API call
        assert calls[:3] == [
            "https://www.nseindia.com/",
            "https://www.nseindia.com/reports/asm",
            "https://www.nseindia.com/api/reportASM",
        ]
        rows = db.execute(
            "SELECT list_type, symbol, stage FROM surveillance_list WHERE list_type='ASM'"
            " ORDER BY symbol"
        ).fetchall()
        assert rows == [("ASM", "AAA", "I"), ("ASM", "BBB", "II"), ("ASM", "CCC", "shortterm")]
        # list_date is the IST fetch date, stamped on every row
        dates = {r[0] for r in db.execute("SELECT list_date FROM surveillance_list")}
        assert len(dates) == 1
        status = db.execute("SELECT DISTINCT parse_status FROM raw_archive").fetchall()
        assert status == [("success",)]

    def test_falls_back_to_csv_when_api_fails(self, fetcher, db):
        def get(url, **kw):
            if "api/" in url:
                return _resp(403, b"denied", url)
            if "ASM" in url:
                return _resp(200, b"Symbol,Stage\nAAA,I\n", url)
            return _resp(404, b"", url)

        fetcher._session.get = get  # type: ignore[method-assign]
        assert fetcher.refresh() == {"ASM": 1}  # GSM: API 403 and CSV 404 -> unavailable
        assert (
            db.execute("SELECT COUNT(*) FROM surveillance_list WHERE list_type='GSM'").fetchone()[0]
            == 0
        )

    def test_all_sources_down_stores_nothing(self, fetcher, db):
        fetcher._session.get = MagicMock(side_effect=ConnectionError("blocked"))  # type: ignore[method-assign]
        assert fetcher.run() is None
        assert fetcher.refresh() == {}
        assert db.execute("SELECT COUNT(*) FROM surveillance_list").fetchone()[0] == 0

    def test_unchanged_list_on_a_later_day_is_still_archived(self, fetcher, db, monkeypatch):
        """archive() dedups on content_hash: the hash includes the date so list_date advances."""
        import datetime as dt

        import collector.fetchers.surveillance as mod

        fetcher._session.get = lambda url, **kw: _resp(200, ASM_JSON, url)  # type: ignore[method-assign]
        monkeypatch.setattr(mod, "_now_ist", lambda: dt.datetime(2026, 11, 18, 7, 30))
        fetcher.refresh(("ASM",))
        monkeypatch.setattr(mod, "_now_ist", lambda: dt.datetime(2026, 11, 19, 7, 30))
        fetcher.refresh(("ASM",))
        got = {r[0] for r in db.execute("SELECT DISTINCT list_date FROM surveillance_list")}
        assert got == {"2026-11-18", "2026-11-19"}

    def test_registered_in_collector_registry(self, db, tmp_path):
        from collector.parser import build_fetcher_registry

        reg = build_fetcher_registry(db, Path(tmp_path / "r"))
        assert isinstance(reg[DataSource.SURVEILLANCE], SurveillanceFetcher)

    def test_parse_rejects_unknown_list_type(self, fetcher, db):
        from collector.models import RawArchiveRow

        row = MagicMock(spec=RawArchiveRow)
        row.request_params = json.dumps({"list_type": "XYZ"})
        with pytest.raises(ValueError, match="unknown list_type"):
            fetcher.parse(row)
