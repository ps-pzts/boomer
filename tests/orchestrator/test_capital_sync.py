"""Tests for capital_sync.sync_eod_capital.

Worked example (verified by hand):
  Kite mock cash:   ₹50,000
  Fyers mock cash:  ₹30,000
  Open swing position: 10 shares × ₹200 entry = ₹2,000 deployed
  total_cash   = 80,000
  total_capital = 82,000
"""

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).parents[2] / "migrations"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    from src.db.migrations import run_migrations

    run_migrations(str(path), MIGRATIONS_DIR)
    return path


def _seed_position(db_path: Path, track: str, qty: int, entry_price: float) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        """INSERT INTO positions
           (position_id, symbol, exchange, track, bucket_id, broker_id, quantity,
            average_entry_price, current_price, stop_loss_price, target_price,
            atr_at_entry, entry_order_id, entry_at, is_open, realised_pnl)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0)""",
        (
            f"pos-{track}",
            "RELIANCE",
            "NSE",
            track,
            f"bucket-{track}",
            "mock",
            qty,
            entry_price,
            entry_price,
            entry_price * 0.95,
            entry_price * 1.10,
            entry_price * 0.02,
            "order-fake",
            "2026-05-11T09:30:00",
        ),
    )
    conn.commit()
    conn.close()


class TestSyncEodCapital:
    def test_initialises_ledger_on_first_run(self, db_path: Path) -> None:
        from executor.brokers.mock_broker import MockBroker
        from src.orchestrator.capital_sync import sync_eod_capital
        from src.capital.state import CapitalStateManager

        broker = MockBroker(initial_cash=80_000.0)
        sync_eod_capital(str(db_path), [broker], "2026-05-11")

        mgr = CapitalStateManager(db_path)
        ledger = mgr.latest_ledger()
        assert ledger is not None
        assert ledger.total_cash == Decimal("80000.0")
        assert ledger.total_capital == Decimal("80000.0")

    def test_sums_cash_across_two_brokers(self, db_path: Path) -> None:
        from executor.brokers.mock_broker import MockBroker
        from src.orchestrator.capital_sync import sync_eod_capital
        from src.capital.state import CapitalStateManager

        kite = MockBroker(initial_cash=50_000.0)
        fyers = MockBroker(initial_cash=30_000.0)
        sync_eod_capital(str(db_path), [kite, fyers], "2026-05-11")

        mgr = CapitalStateManager(db_path)
        ledger = mgr.latest_ledger()
        assert ledger is not None
        assert ledger.total_cash == Decimal("80000.0")

    def test_deployed_capital_included_in_total(self, db_path: Path) -> None:
        """₹80k cash + 10 shares × ₹200 = ₹82k total capital."""
        from executor.brokers.mock_broker import MockBroker
        from src.orchestrator.capital_sync import sync_eod_capital
        from src.capital.state import CapitalStateManager

        _seed_position(db_path, "swing", qty=10, entry_price=200.0)

        broker = MockBroker(initial_cash=80_000.0)
        sync_eod_capital(str(db_path), [broker], "2026-05-11")

        mgr = CapitalStateManager(db_path)
        ledger = mgr.latest_ledger()
        assert ledger is not None
        assert ledger.total_capital == Decimal("82000.0")
        assert ledger.swing_deployed == Decimal("2000.0")
        assert ledger.long_term_deployed == Decimal("0")
        assert ledger.intraday_deployed == Decimal("0")

    def test_updates_existing_ledger(self, db_path: Path) -> None:
        from executor.brokers.mock_broker import MockBroker
        from src.orchestrator.capital_sync import sync_eod_capital
        from src.capital.state import CapitalStateManager

        # First run: initialise
        broker = MockBroker(initial_cash=80_000.0)
        sync_eod_capital(str(db_path), [broker], "2026-05-10")

        # Second run: update — capital grew
        broker2 = MockBroker(initial_cash=85_000.0)
        sync_eod_capital(str(db_path), [broker2], "2026-05-11")

        mgr = CapitalStateManager(db_path)
        ledger = mgr.latest_ledger()
        assert ledger is not None
        assert ledger.total_cash == Decimal("85000.0")
        assert ledger.as_of_date.isoformat() == "2026-05-11"

    def test_broker_failure_does_not_crash(self, db_path: Path) -> None:
        """A broker that raises on get_funds() is skipped; other brokers still counted."""
        from executor.brokers.mock_broker import MockBroker
        from src.orchestrator.capital_sync import sync_eod_capital
        from src.capital.state import CapitalStateManager

        class FailingBroker(MockBroker):
            def get_funds(self):
                raise RuntimeError("API down")

        bad = FailingBroker(initial_cash=999.0)
        good = MockBroker(initial_cash=50_000.0)
        sync_eod_capital(str(db_path), [bad, good], "2026-05-11")

        mgr = CapitalStateManager(db_path)
        ledger = mgr.latest_ledger()
        assert ledger is not None
        assert ledger.total_cash == Decimal("50000.0")

    def test_no_brokers_skips_write(self, db_path: Path) -> None:
        from src.orchestrator.capital_sync import sync_eod_capital
        from src.capital.state import CapitalStateManager

        sync_eod_capital(str(db_path), [], "2026-05-11")

        mgr = CapitalStateManager(db_path)
        # No brokers → total_cash stays 0 → still initialises with 0
        ledger = mgr.latest_ledger()
        assert ledger is not None
        assert ledger.total_cash == Decimal("0")
