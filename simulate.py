"""
Phase 1 + Phase 2 simulation — two cases.

Case 1 (BULL): LIC bulk deal + positive Q4 results filing → trade APPROVED
Case 2 (BEAR): Fraud filing + circuit breaker tripped → trade REJECTED

Run: python simulate.py
"""

import sqlite3
import tempfile
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from capital.circuit_breakers import evaluate_circuit_breakers
from capital.models import (
    BotMode,
    CapitalLedgerRow,
    Regime,
    RiskConfig,
    Track,
    TradeRequest,
)
from capital.pre_trade import PreTradeChecker
from capital.risk_config import RiskConfigStore
from collector.sentiment import SentimentPipeline, apply_sentiment_to_filings

# ── helpers ────────────────────────────────────────────────────────────────────

SEP = "─" * 60


def make_db() -> tuple[sqlite3.Connection, str]:
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    for f in ["migrations/0001_initial_schema.sql", "migrations/0002_collector_schema.sql"]:
        with open(f) as fh:
            conn.executescript(fh.read())
    return conn, tmp


def seed_capital(db_path: str) -> RiskConfig:
    store = RiskConfigStore(db_path)
    return store.seed_defaults(effective_from=date(2026, 5, 9))


def make_ledger(
    total_capital: Decimal,
    swing_deployed: Decimal = Decimal("0"),
    long_term_deployed: Decimal = Decimal("0"),
) -> CapitalLedgerRow:
    """Build a ledger row with steady-state allocation (capital > ₹2.5L)."""
    return CapitalLedgerRow(
        ledger_id=str(uuid.uuid4()),
        as_of_date=date(2026, 5, 9),
        total_capital=total_capital,
        total_cash=total_capital - swing_deployed - long_term_deployed,
        long_term_allocated_pct=Decimal("0.70"),
        swing_allocated_pct=Decimal("0.15"),
        intraday_allocated_pct=Decimal("0.15"),
        long_term_deployed=long_term_deployed,
        swing_deployed=swing_deployed,
        intraday_deployed=Decimal("0"),
        high_water_mark=total_capital,
        eod_drawdown_pct=Decimal("0"),
        consecutive_loss_days=0,
        peak_date=date(2026, 5, 9),
    )


def insert_filing(conn, filing_id, stock_symbol, headline, category="other"):
    raw_id = f"raw-{filing_id}"
    conn.execute(
        "INSERT OR IGNORE INTO raw_archive "
        "(raw_id, source, fetched_at, request_url, response_status, "
        "content_hash, content_path, parse_status) "
        "VALUES (?, 'bse_filings', '2026-05-09T10:00:00Z', 'x', 200, ?, 'p', 'success')",
        (raw_id, raw_id),
    )
    conn.execute(
        "INSERT INTO filings "
        "(filing_id, raw_id, parser_version, stock_symbol, exchange, "
        "filing_date, observed_at, category, headline, is_corrected, parse_deps_met) "
        "VALUES (?, ?, 'v1', ?, 'BSE', '2026-05-09', '2026-05-09T10:00:00Z', ?, ?, 0, 1)",
        (filing_id, raw_id, stock_symbol, category, headline),
    )
    conn.commit()


def insert_bulk_deal(conn, stock_symbol, client_name, qty, price, is_smart_money):
    conn.execute(
        "INSERT OR IGNORE INTO raw_archive "
        "(raw_id, source, fetched_at, request_url, response_status, "
        "content_hash, content_path, parse_status) "
        "VALUES ('raw-bd-001', 'bse_bulk_deals', '2026-05-09T16:00:00Z', "
        "'x', 200, 'h', 'p', 'success')",
    )
    conn.execute(
        "INSERT INTO bulk_deals "
        "(deal_id, deal_date, observed_at, exchange, stock_symbol, "
        "client_name, transaction_type, quantity, price, value, "
        "is_smart_money, raw_id, parser_version) "
        "VALUES (?, '2026-05-09', '2026-05-09T16:00:00Z', 'BSE', ?, "
        "?, 'BUY', ?, ?, ?, ?, 'raw-bd-001', 'v1')",
        (
            str(uuid.uuid4()),
            stock_symbol,
            client_name,
            qty,
            float(price),
            float(qty * price),
            is_smart_money,
        ),
    )
    conn.commit()


def run_sentiment(conn, config: RiskConfig, filing_ids: list, mock_results: list):
    pipeline = SentimentPipeline()
    pipeline._pipeline = MagicMock(return_value=mock_results)
    threshold = float(config.sentiment_confidence_threshold)
    return apply_sentiment_to_filings(conn, pipeline, confidence_threshold=threshold)


def make_checker(config, ledger, all_clear=True, portfolio_loss_today=Decimal("0")):
    breakers = evaluate_circuit_breakers(
        intraday_realised_pnl_today=Decimal("0"),
        intraday_bucket_capital=ledger.bucket_capital(Track.INTRADAY),
        intraday_consecutive_losses_today=0,
        swing_realised_pnl_this_week=-portfolio_loss_today,
        swing_bucket_capital=ledger.bucket_capital(Track.SWING),
        swing_losing_trades_30d=0,
        portfolio_realised_pnl_today=-portfolio_loss_today,
        total_capital=ledger.total_capital,
        portfolio_realised_pnl_this_week=-portfolio_loss_today,
        live_drawdown_pct=Decimal("0"),
        nifty_intraday_move_pct=Decimal("0"),
        current_time_ist_hour=10,
        current_time_ist_minute=30,
        black_swan_manually_tripped=False,
        config=config,
    )

    # Stub concentration: no existing holdings
    class NullConcentration:
        def sector_deployed(self, sector):
            return Decimal("0")

        def stock_deployed(self, symbol):
            return Decimal("0")

        def correlation_cluster_deployed(self, symbol, exchange):
            return Decimal("0")

    return PreTradeChecker(
        config=config,
        breakers=breakers,
        ledger=ledger,
        concentration=NullConcentration(),
        live_total_capital=ledger.total_capital,
        live_drawdown_pct=Decimal("0"),
        live_intraday_pnl=Decimal("0"),
        bot_mode=BotMode.AUTO,
    )


# ── CASE 1: Bull signal ────────────────────────────────────────────────────────


def case1_bull():
    print(f"\n{SEP}")
    print("CASE 1 — BULL: LIC bulk deal + Q4 results → trade APPROVED")
    print(SEP)

    conn, db_path = make_db()
    config = seed_capital(db_path)

    # ── Phase 2: collector writes market events ─────────────────────────────

    print("\n[Phase 2] BSE filings received at 10:00 AM:")
    insert_filing(
        conn,
        "f-001",
        "RELIANCE",
        "Q4 FY26 Financial Results — PAT ₹19,407 Cr, up 12% YoY",
        category="quarterly_results",
    )
    print("  Filing: RELIANCE — Q4 results announcement")

    print("\n[Phase 2] Bulk deals published at 6 PM:")
    insert_bulk_deal(
        conn,
        "RELIANCE",
        "LIC OF INDIA",
        qty=500_000,
        price=Decimal("2920"),
        is_smart_money=1,
    )
    print("  Bulk deal: LIC OF INDIA bought 5,00,000 shares @ ₹2,920 = ₹146 Cr")

    # ── Phase 2: sentiment pipeline ─────────────────────────────────────────
    print("\n[Phase 2] FinBERT sentiment inference:")
    run_sentiment(
        conn,
        config,
        ["f-001"],
        mock_results=[[{"label": "positive", "score": 0.91}]],
    )
    row = conn.execute(
        "SELECT sentiment_label, sentiment_confidence FROM filings WHERE filing_id='f-001'"
    ).fetchone()
    headline = conn.execute(
        "SELECT headline FROM filings WHERE filing_id=?", ("f-001",)
    ).fetchone()[0]
    print(f"  '{headline}'")
    print(f"  → Sentiment: {row[0].upper()}  (confidence {row[1]:.0%})")

    # ── Phase 1: pre-trade check ─────────────────────────────────────────────
    print("\n[Phase 1] Pre-trade check — swing entry on RELIANCE:")
    total_capital = Decimal("1000000")  # ₹10,00,000
    ledger = make_ledger(total_capital)
    checker = make_checker(config, ledger)

    req = TradeRequest(
        stock_symbol="RELIANCE",
        exchange="BSE",
        track=Track.SWING,
        entry_price=Decimal("2920"),
        stop_loss_price=Decimal("2800"),  # ₹120 risk per share (2× ATR)
        target_price=Decimal("3100"),  # ₹180 reward → RR = 1.5
        signal_confidence=Decimal("0.72"),
        sector="energy",
        current_regime=Regime.BULL_CALM,
        requested_at=datetime.now(UTC),
    )

    swing_bucket = ledger.bucket_capital(Track.SWING)
    risk_budget = swing_bucket * config.risk_per_swing_trade_pct
    risk_per_share = req.entry_price - req.stop_loss_price
    expected_qty = int(risk_budget / risk_per_share)

    print(f"  Capital:        ₹{total_capital:,.0f}")
    print(f"  Swing bucket (15%): ₹{swing_bucket:,.0f}")
    print(f"  Risk budget (1%):   ₹{risk_budget:,.0f}")
    print(f"  Entry: ₹{req.entry_price}  Stop: ₹{req.stop_loss_price}  Target: ₹{req.target_price}")
    print(
        f"  Risk per share: ₹{risk_per_share}  →  "
        f"Qty = ₹{risk_budget:.0f} / ₹{risk_per_share} = {expected_qty} shares"
    )
    print(f"  Regime: {req.current_regime}  (scale 100%)")

    perm = checker.check(req)
    status = "✓ APPROVED" if perm.approved else "✗ REJECTED"
    print(f"\n  Result: {status}")
    if perm.approved:
        pos_value = perm.position_size_shares * req.entry_price
        print(
            f"  Position: {perm.position_size_shares} shares × ₹{req.entry_price}"
            f" = ₹{pos_value:,.0f}"
        )
        print(f"  Max loss on trade: ₹{perm.risk_per_trade_rupees:,.0f}")
    else:
        print(f"  Failed check: {perm.failed_check}")
        print(f"  Reason: {perm.reason}")

    conn.close()


# ── CASE 2: Bear / fraud signal ───────────────────────────────────────────────


def case2_bear():
    print(f"\n{SEP}")
    print("CASE 2 — BEAR: Fraud filing + swing weekly loss → trade REJECTED")
    print(SEP)

    conn, db_path = make_db()
    config = seed_capital(db_path)

    # ── Phase 2: collector writes market events ─────────────────────────────

    print("\n[Phase 2] BSE filings received:")
    insert_filing(
        conn,
        "f-002",
        "ZOMATO",
        "Auditor resignation — statutory auditor cites inability to verify receivables",
        category="auditor_change",
    )
    insert_filing(
        conn,
        "f-003",
        "ZOMATO",
        "Promoter pledges 8% of total shareholding",
        category="pledging",
    )
    print("  Filing 1: ZOMATO — auditor resignation")
    print("  Filing 2: ZOMATO — promoter pledges 8% of shares")

    # ── Phase 2: sentiment pipeline ─────────────────────────────────────────
    print("\n[Phase 2] FinBERT sentiment inference:")
    run_sentiment(
        conn,
        config,
        ["f-002", "f-003"],
        mock_results=[
            [{"label": "negative", "score": 0.95}],
            [{"label": "negative", "score": 0.88}],
        ],
    )
    for fid, headline_short in [("f-002", "auditor resignation"), ("f-003", "promoter pledge")]:
        row = conn.execute(
            "SELECT sentiment_label, sentiment_confidence FROM filings WHERE filing_id=?", (fid,)
        ).fetchone()
        print(f"  '{headline_short}' → {row[0].upper()} ({row[1]:.0%})")

    # ── Phase 1: circuit breakers — swing had a bad week ────────────────────
    print("\n[Phase 1] Circuit breaker evaluation:")
    total_capital = Decimal("1000000")
    # Swing already lost ₹10,000 this week = 6.7% of swing bucket (₹1,50,000)
    # Threshold is 4% → breaker trips
    swing_loss_this_week = Decimal("10000")
    ledger = make_ledger(total_capital, swing_deployed=Decimal("80000"))

    swing_bucket = ledger.bucket_capital(Track.SWING)
    loss_pct = swing_loss_this_week / swing_bucket
    threshold = config.swing_weekly_loss_limit_pct
    print(f"  Swing bucket: ₹{swing_bucket:,.0f}")
    print(f"  Swing loss this week: ₹{swing_loss_this_week:,.0f}  ({loss_pct:.1%})")
    print(f"  Swing weekly loss limit: {threshold:.1%}")
    print(f"  Breaker: {'TRIPPED ✗' if loss_pct >= threshold else 'CLEAR ✓'}")

    checker = make_checker(config, ledger, portfolio_loss_today=swing_loss_this_week)

    # ── Phase 1: pre-trade check ─────────────────────────────────────────────
    print("\n[Phase 1] Pre-trade check — swing BUY on ZOMATO (ignoring the red flags):")
    req = TradeRequest(
        stock_symbol="ZOMATO",
        exchange="BSE",
        track=Track.SWING,
        entry_price=Decimal("195"),
        stop_loss_price=Decimal("180"),
        target_price=Decimal("225"),
        signal_confidence=Decimal("0.60"),
        sector="consumer",
        current_regime=Regime.SIDEWAYS,
        requested_at=datetime.now(UTC),
    )

    perm = checker.check(req)
    status = "✓ APPROVED" if perm.approved else "✗ REJECTED"
    print(f"\n  Result: {status}")
    if perm.approved:
        print(f"  Position: {perm.position_size_shares} shares")
    else:
        print(f"  Failed check: {perm.failed_check}")
        print(f"  Reason: {perm.reason}")

    print("\n[Summary] The circuit breaker stops the trade independently of the filing data.")
    print("  Phase 3 (Brain) will add a second layer: negative-sentiment filings")
    print("  actively suppress signal confidence, so this trade wouldn't even reach")
    print("  the pre-trade check with a passing confidence score.")

    conn.close()


# ── run both ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    case1_bull()
    case2_bear()
    print(f"\n{SEP}")
    print("Simulation complete.")
    print(SEP)
