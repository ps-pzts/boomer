"""Paper -> live readiness report (#119): read-only evidence per design Q5 criterion.

Worked example (hand-verifiable), two paper trades of 100 shares, entry fill 500.00:
  A: target sell fills 510.00, realised +1,000.   buy 50,000, sell 51,000
     brokerage 15 + 15.30 = 30.30; STT 0.025% x 51,000 = 12.75; exchange 0.00322% x 101,000
     = 3.2522; GST 18% x (30.30 + 3.2522) = 6.0394; SEBI 0.0001% x 101,000 = 0.101;
     stamp 0.003% x 50,000 = 1.50  ->  costs 53.9426, net +946.0574
  B: stop sell fills 495.00, realised -500.       buy 50,000, sell 49,500
     brokerage 15 + 14.85 = 29.85; STT 12.375; exchange 3.2039; GST 5.9491; SEBI 0.0995;
     stamp 1.50  ->  costs 52.9775, net -552.9775
  expectancy = (946.0574 - 552.9775) / 2 = +196.54 per trade, win rate 50%.

Slippage: a SELL stop (trigger 495) filling at 494.63 is (495-494.63)/495 x 10,000 = 7.475 bps,
inside the backtester's 5 bps x 1.5 = 7.5 bps; filling at 494.50 is 10.10 bps, outside.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alerts import commands
from backtester.slippage import SlippageModel
from db.migrations import run_migrations
from executor import live_readiness as lr
from executor.live_readiness import Status

_MIGRATIONS = Path(__file__).parents[2] / "migrations"
_SEQ = iter(range(1, 100000))


@pytest.fixture()
def db(tmp_path) -> str:
    path = str(tmp_path / "t.db")
    run_migrations(path, _MIGRATIONS)
    return path


def sql(db: str, text: str, *args) -> None:
    c = sqlite3.connect(db)
    c.execute(text, args)
    c.commit()
    c.close()


def session(db, day, mode="paper", state="closed") -> None:
    sql(
        db,
        "INSERT INTO trading_sessions (session_date, mode, state, created_at, updated_at)"
        " VALUES (?,?,?,'t','t')",
        day,
        mode,
        state,
    )


def order(db, oid, side, otype, price, trigger, fill, qty=100) -> None:
    sql(
        db,
        "INSERT INTO orders (order_id, symbol, exchange, side, order_type, quantity,"
        " filled_quantity, product, price, trigger_price, average_fill_price, status,"
        " broker_id, created_at, updated_at) VALUES (?, 'INFY','NSE',?,?,?,?,'mis',?,?,?,"
        " 'filled','paper','t','t')",
        oid,
        side,
        otype,
        qty,
        qty,
        price,
        trigger,
        fill,
    )


def bracket(db, day, *, pnl=None, state="closed", stop="s", target="t", exits="[]") -> str:
    n = next(_SEQ)
    sql(
        db,
        "INSERT INTO intraday_brackets (bracket_id, session_date, symbol, exchange,"
        " planned_quantity, filled_quantity, entry_price, stop_price, target_price, atr,"
        " entry_order_id, stop_order_id, target_order_id, exit_order_ids, entry_expires_at,"
        " state, realised_pnl, created_at, updated_at) VALUES (?,?, 'INFY','NSE',100,100,500,"
        " 495, 510, 5, ?, ?, ?, ?, 'x', ?, ?, 't','t')",
        f"b{n}",
        day,
        f"e{n}",
        stop and f"{stop}{n}",
        target and f"{target}{n}",
        exits,
        state,
        pnl,
    )
    return str(n)


def trade(db, day, kind, stop_fill=495.0) -> None:
    """Closed paper trade: kind 'target' (+1,000) or 'stop' (-500); entry fills at 500."""
    n = bracket(db, day, pnl=1000.0 if kind == "target" else -500.0)
    order(db, f"e{n}", "buy", "limit", 500.0, 0.0, 500.0)
    if kind == "target":
        order(db, f"t{n}", "sell", "limit", 510.0, 0.0, 510.0)
    else:
        order(db, f"s{n}", "sell", "sl", 0.0, 495.0, stop_fill)


def status(db, name, b4=None) -> Status:
    rep = lr.build_report(db, b4_env=b4 if b4 is not None else "")
    return next(c for c in rep.criteria if c.name == name).status


def crit(db, name):
    return next(c for c in lr.build_report(db, b4_env="").criteria if c.name == name)


class TestZeroData:
    def test_empty_db_is_all_insufficient_and_not_ready(self, db):
        rep = lr.build_report(db, b4_env="")
        assert [c.status for c in rep.criteria] == [Status.INSUFFICIENT_DATA] * 5
        assert not rep.ready
        assert "NOT ready" in lr.render(rep)


class TestSessions:
    def test_threshold_and_only_closed_paper_count(self, db):
        for i in range(19):
            session(db, f"2026-10-{i + 1:02d}")
        session(db, "2026-10-25", mode="live")  # live: ignored
        session(db, "2026-10-26", state="trading")  # not closed: ignored
        c = crit(db, "paper_sessions")
        assert c.status == Status.INSUFFICIENT_DATA and c.details["closed_paper_sessions"] == 19
        session(db, "2026-10-27")
        c = crit(db, "paper_sessions")
        assert c.status == Status.PASS and c.details["closed_paper_sessions"] == 20


class TestProtection:
    def test_clean_paper_session_passes(self, db):
        session(db, "2026-11-02")
        assert status(db, "zero_unprotected") == Status.PASS

    @pytest.mark.parametrize("reason", ["stop_rejected", "stop_cancelled", "stop_not_submitted"])
    def test_trade_forced_out_by_missing_stop_fails(self, db, reason):
        session(db, "2026-11-02")
        n = bracket(db, "2026-11-02", pnl=-10.0)
        sql(db, "UPDATE intraday_brackets SET exit_reason=? WHERE bracket_id=?", reason, f"b{n}")
        c = crit(db, "zero_unprotected")
        assert c.status == Status.FAIL and c.details["forced_exits"] == 1

    def test_live_session_and_normal_exits_ignored(self, db):
        session(db, "2026-11-02")
        session(db, "2026-11-03", mode="live")
        for day, reason in (("2026-11-02", "stop_hit"), ("2026-11-02", "target_hit"),
                            ("2026-11-03", "stop_rejected")):  # fmt: skip
            n = bracket(db, day, pnl=-10.0)
            sql(
                db, "UPDATE intraday_brackets SET exit_reason=? WHERE bracket_id=?", reason, f"b{n}"
            )
        assert status(db, "zero_unprotected") == Status.PASS

    def test_open_trade_without_stop_fails(self, db):
        session(db, "2026-11-02")
        bracket(db, "2026-11-02", state="open", stop=None)
        assert crit(db, "zero_unprotected").details["open_without_stop"] == 1

    def test_flagged_position_fails_only_on_paper_dates(self, db):
        session(db, "2026-11-02")
        session(db, "2026-11-03", mode="live")
        for pid, ts in (("p1", "2026-11-02T10:00:00"), ("p2", "2026-11-03T10:00:00")):
            sql(
                db,
                "INSERT INTO positions (position_id, symbol, exchange, track, bucket_id,"
                " broker_id, quantity, average_entry_price, stop_loss_price, target_price,"
                " atr_at_entry, entry_order_id, unprotected_flag, entry_at) VALUES"
                " (?, 'INFY','NSE','intraday','intraday','paper',1,500,495,510,5,'o',1,?)",
                pid,
                ts,
            )
        assert crit(db, "zero_unprotected").details["positions_flagged"] == 1


class TestSlippage:
    def test_assumption_comes_from_the_backtester(self):
        a = lr.slippage_assumptions_bps()
        assert a["limit"] == 0.0
        assert a["stop"] == pytest.approx(
            SlippageModel.BASE_SLIPPAGE_PCT * SlippageModel.STOP_SLIPPAGE_FACTOR * 10_000
        )
        assert a["stop"] == pytest.approx(7.5)

    def test_adverse_bps_worked_examples(self):
        assert lr.adverse_bps("buy", 500.25, 500.0) == pytest.approx(5.0)
        assert lr.adverse_bps("sell", 199.70, 200.0) == pytest.approx(15.0)
        assert lr.adverse_bps("sell", 200.10, 200.0) == pytest.approx(-5.0)  # improvement

    def test_within_assumption_passes(self, db):
        session(db, "2026-11-02")
        trade(db, "2026-11-02", "stop", stop_fill=494.63)  # 7.475 bps
        c = crit(db, "slippage")
        assert c.status == Status.PASS
        assert c.details["mean_adverse_bps"]["stop"] == pytest.approx(7.475, abs=0.01)
        assert c.details["mean_adverse_bps"]["limit"] == 0.0

    def test_worse_than_assumption_fails(self, db):
        session(db, "2026-11-02")
        trade(db, "2026-11-02", "stop", stop_fill=494.50)  # 10.10 bps
        c = crit(db, "slippage")
        assert c.status == Status.FAIL
        assert c.details["mean_adverse_bps"]["stop"] == pytest.approx(10.101, abs=0.01)

    def test_limit_fill_worse_than_limit_fails(self, db):
        session(db, "2026-11-02")
        n = bracket(db, "2026-11-02", pnl=0.0, stop=None, target=None)
        order(db, f"e{n}", "buy", "limit", 500.0, 0.0, 500.5)  # +10 bps vs a 0 bps assumption
        assert crit(db, "slippage").status == Status.FAIL

    def test_market_exit_counted_not_judged(self, db):
        session(db, "2026-11-02")
        n = bracket(db, "2026-11-02", pnl=0.0, stop=None, target=None, exits='["x1"]')
        order(db, f"e{n}", "buy", "limit", 500.0, 0.0, 500.0)
        c = crit(db, "slippage")
        assert c.status == Status.PASS and c.details["market_or_unreferenced_legs"] == 1

    def test_no_fills_is_insufficient_and_live_sessions_excluded(self, db):
        session(db, "2026-11-02", mode="live")
        trade(db, "2026-11-02", "stop", stop_fill=480.0)  # terrible, but live
        assert status(db, "slippage") == Status.INSUFFICIENT_DATA


class TestExpectancy:
    def test_worked_example(self, db):
        session(db, "2026-11-02")
        trade(db, "2026-11-02", "target")
        trade(db, "2026-11-02", "stop")
        c = crit(db, "expectancy")
        assert c.status == Status.PASS
        d = c.details
        assert d["trades"] == 2 and d["win_rate"] == 0.5
        assert d["gross_pnl"] == 500.0
        assert d["costs"] == pytest.approx(53.9426 + 52.9775, abs=0.01)
        assert d["expectancy_per_trade"] == pytest.approx(196.54, abs=0.01)

    def test_negative_after_costs_fails_even_when_gross_positive(self, db):
        # gross +20 on 50,000 buy; costs ~ 50 -> net negative
        session(db, "2026-11-02")
        n = bracket(db, "2026-11-02", pnl=20.0)
        order(db, f"e{n}", "buy", "limit", 500.0, 0.0, 500.0)
        c = crit(db, "expectancy")
        assert c.status == Status.FAIL and c.details["expectancy_per_trade"] < 0

    def test_open_and_live_trades_ignored(self, db):
        session(db, "2026-11-02")
        session(db, "2026-11-03", mode="live")
        trade(db, "2026-11-03", "target")
        n = bracket(db, "2026-11-02", state="open")  # not closed
        order(db, f"e{n}", "buy", "limit", 500.0, 0.0, 500.0)
        assert status(db, "expectancy") == Status.INSUFFICIENT_DATA


class TestB4:
    @pytest.mark.parametrize("v", ["1", "true", "YES"])
    def test_truthy_passes(self, v):
        assert lr.check_b4(v).status == Status.PASS

    @pytest.mark.parametrize("v", [None, "", "0", "no"])
    def test_unset_is_insufficient(self, v):
        assert lr.check_b4(v).status == Status.INSUFFICIENT_DATA


class TestReadOnly:
    def test_report_does_not_modify_the_database(self, db):
        session(db, "2026-11-02")
        trade(db, "2026-11-02", "target")
        before = Path(db).read_bytes()
        lr.build_report(db, b4_env="1")
        assert Path(db).read_bytes() == before

    def test_connection_rejects_writes(self, db):
        c = lr._connect_ro(db)
        with pytest.raises(sqlite3.OperationalError):
            c.execute("DELETE FROM trading_sessions")
        c.close()

    def test_module_cannot_enable_live(self):
        src = Path(lr.__file__).read_text()
        assert "set_enabled" not in src and "LiveTradingAuthorisationStore" not in src

    def test_all_met_is_ready(self, db):
        for i in range(20):
            session(db, f"2026-10-{i + 1:02d}")
        trade(db, "2026-10-01", "target")
        rep = lr.build_report(db, b4_env="1")
        assert rep.ready and "all criteria met" in lr.render(rep)


class TestSurfaces:
    def test_cli_prints_report(self, db, capsys):
        assert lr.main(["--db", db]) == 0
        out = capsys.readouterr().out
        assert "evidence only" in out and "paper_sessions" in out

    def test_telegram_command(self, db):
        session(db, "2026-11-02")
        reply = commands.run(db, "/readiness")
        assert reply is not None and "paper_sessions" in reply and "NOT ready" in reply
        assert "/readiness" in commands.HELP

    def test_telegram_command_survives_a_missing_db(self, tmp_path):
        reply = commands.run(str(tmp_path / "missing.db"), "/readiness")
        assert reply is not None and "unavailable" in reply
