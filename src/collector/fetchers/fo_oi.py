"""
F&O Open Interest daily snapshot fetcher (Category A — daily snapshot).

Source: NSE F&O bhavcopy CSV published post-settlement (~6 PM).
Fetched once per day in the nightly 02:00 collector run for previous trading day.
Freshness SLA: by 8 AM next day.

SQLite stores recent metadata rows; full history lives in the parquet lake.
Derived features (overnight_oi_change_pct, max_pain_strike, iv_percentile_252d,
put_call_ratio_oi, put_call_ratio_volume) are computed in Stage 0 — not here.
"""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
import uuid
import zipfile
from datetime import date, datetime
from pathlib import Path

from collector.base import BaseFetcher, PermanentFetchError, _fmt_dt
from collector.models import DataSource, FetchResult, InstrumentType, RawArchiveRow

logger = logging.getLogger(__name__)

# NSE F&O bhavcopy URL pattern.
# Date format in filename: DDMMMYYYY e.g. 22APR2024
_NSE_FO_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{date}_F_0000.csv.zip"
)
# Older format (pre-2023):
_NSE_FO_BHAVCOPY_URL_OLD = (
    "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{year}/{month}/fo{date}bhav.csv.zip"
)


class FoOiFetcher(BaseFetcher):
    source = DataSource.FO_OI

    def __init__(self, db: sqlite3.Connection, raw_dir: Path) -> None:
        super().__init__(db, raw_dir)
        self._session.headers.update({"Referer": "https://www.nseindia.com/"})

    def fetch_url(self, trade_date: date | None = None, **kwargs) -> str:
        d = trade_date or date.today()
        return _NSE_FO_BHAVCOPY_URL.format(date=d.strftime("%d%b%Y").upper())

    def validate(self, result: FetchResult) -> None:
        if result.status_code == 404:
            raise PermanentFetchError(
                "F&O OI: 404 — likely non-trading day or data not published yet"
            )
        if result.status_code != 200:
            raise ValueError(f"F&O OI: HTTP {result.status_code}")
        # Response is a ZIP; check magic bytes.
        if result.body[:2] != b"PK":
            raise ValueError("F&O OI: expected ZIP response, got unexpected content")

    def parse(self, raw_row: RawArchiveRow) -> int:
        body = self.load_raw_body(raw_row.content_path)
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            csv_name = next(n for n in zf.namelist() if n.endswith(".csv"))
            csv_body = zf.read(csv_name)
        return _parse_fo_bhavcopy_csv(csv_body, raw_row, self._db, self.parser_version)


# ── CSV parser ─────────────────────────────────────────────────────────────────

def _parse_fo_bhavcopy_csv(
    body: bytes,
    raw_row: RawArchiveRow,
    db: sqlite3.Connection,
    version: str,
) -> int:
    """
    NSE F&O bhavcopy columns (new format, 2023+):
    FinInstrmTp,XpryDt,TckrSymb,OpnPric,HghPric,LwPric,ClsPric,SttlmPric,
    OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,SnpshtDt,...

    Old format had: SYMBOL,INSTRUMENT,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,
    SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP
    Both formats supported.
    """
    text = body.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    use_new_format = "FinInstrmTp" in (f.strip() for f in fieldnames)

    inserted = 0
    for row in reader:
        record_id = str(uuid.uuid4())
        try:
            if use_new_format:
                instr_type_raw = (row.get("FinInstrmTp") or "").strip()
                symbol = (row.get("TckrSymb") or "").strip()
                expiry_str = (row.get("XpryDt") or "").strip()
                strike_str = (row.get("StrkPric") or row.get("strike") or "").strip()
                oi_str = (row.get("OpnIntrst") or "0").replace(",", "").strip()
                oi_chg_str = (row.get("ChngInOpnIntrst") or "0").replace(",", "").strip()
                vol_str = (row.get("TtlTradgVol") or "0").replace(",", "").strip()
                close_str = (
                    row.get("ClsPric") or row.get("SttlmPric") or "0"
                ).replace(",", "").strip()
                trade_date_str = (row.get("SnpshtDt") or "").strip()
            else:
                # Old format
                instr_raw = (row.get("INSTRUMENT") or "").strip()
                opt_type = (row.get("OPTION_TYP") or "").strip()
                instr_type_raw = instr_raw if instr_raw in ("FUTSTK", "FUTIDX") else opt_type
                symbol = (row.get("SYMBOL") or "").strip()
                expiry_str = (row.get("EXPIRY_DT") or "").strip()
                strike_str = (row.get("STRIKE_PR") or "0").strip()
                oi_str = (row.get("OPEN_INT") or "0").replace(",", "").strip()
                oi_chg_str = (row.get("CHG_IN_OI") or "0").replace(",", "").strip()
                vol_str = (row.get("CONTRACTS") or "0").replace(",", "").strip()
                close_str = (
                    row.get("SETTLE_PR") or row.get("CLOSE") or "0"
                ).replace(",", "").strip()
                trade_date_str = (row.get("TIMESTAMP") or "").strip()

            if not symbol:
                continue

            instr_type = _map_instrument_type(instr_type_raw)
            if instr_type is None:
                continue

            expiry_date = _parse_date(expiry_str)
            trade_date = _parse_date(trade_date_str)
            strike = float(strike_str) if strike_str and strike_str not in ("0", "-") else None
            oi = int(float(oi_str)) if oi_str else 0
            oi_chg = int(float(oi_chg_str)) if oi_chg_str else None
            vol = int(float(vol_str)) if vol_str else 0
            close = float(close_str) if close_str else None

        except (ValueError, TypeError) as exc:
            logger.debug("F&O OI: skipping row parse error: %s", exc)
            continue

        try:
            db.execute(
                """
                INSERT OR IGNORE INTO fo_oi_daily
                    (record_id, raw_id, parser_version, underlying_symbol, exchange,
                     instrument_type, expiry_date, strike_price, trade_date, observed_at,
                     open_interest, oi_change, volume, close_price, iv,
                     is_corrected, corrects_record_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id, raw_row.raw_id, version,
                    symbol, "NSE",
                    instr_type.value, expiry_date, strike,
                    trade_date, _fmt_dt(raw_row.fetched_at),
                    oi, oi_chg, vol, close,
                    None,  # IV computed at feature time
                    0, None,
                ),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception as exc:
            logger.warning("F&O OI: insert failed record_id=%s: %s", record_id, exc)

    db.commit()
    return inserted


def _map_instrument_type(raw: str) -> InstrumentType | None:
    raw = raw.upper().strip()
    if raw in ("FUTSTK", "FUTIDX", "FUT"):
        return InstrumentType.FUT
    if raw in ("CE", "OPTSTK", "OPTIDX") and "CE" in raw:
        return InstrumentType.CE
    if raw in ("PE",) or ("PE" in raw and raw not in ("OPTSTK",)):
        return InstrumentType.PE
    if raw == "OPTSTK" or raw == "OPTIDX":
        return None  # need option type from another field; caller handles
    return None


def _parse_date(s: str) -> str:
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d%b%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return date.today().isoformat()
