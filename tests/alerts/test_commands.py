"""Telegram operator commands (#103): /status /positions /trades /pnl /halt /ack
/pause /resume /emergency, on a migrated database.

Worked numbers: two closed trades, +40 (target) and -25 (stop gapped), one open INFY
10 @ 100 with +15 unrealised: realised +15, unrealised +15, total +30.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alerts import commands, telegram_bot
from alerts.commands import common
from db.migrations import run_migrations

IST = ZoneInfo("Asia/Kolkata")
_MIGRATIONS = Path(__file__).parents[2] / "migrations"
DAY = common.today()


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


def session(db: str, state: str, reason: str | None = None, detail: dict | None = None) -> None:
    sql(
        db,
        "INSERT INTO trading_sessions (session_date, mode, state, halt_reason, halt_detail,"
        " created_at, updated_at) VALUES (?, 'paper', ?, ?, ?, 't', 't')",
        DAY, state, reason, json.dumps(detail) if detail else None,
    )  # fmt: skip


_SEQ = iter(range(1, 10_000))


def bracket(db, sym, state, pnl=None, reason=None, qty=10) -> None:
    n = next(_SEQ)
    sql(
        db,
        "INSERT INTO intraday_brackets (bracket_id, session_date, symbol, exchange,"
        " planned_quantity, filled_quantity, entry_price, stop_price, target_price, atr,"
        " entry_order_id, entry_expires_at, state, exit_reason, realised_pnl, created_at,"
        " updated_at) VALUES (?,?,?, 'NSE', ?, ?, 100, 98, 104, 2, 'o', 'x', ?, ?, ?, ?, 't')",
        f"b{n}", DAY, sym, qty, qty, state, reason, pnl, f"2026-11-19T10:{n:02d}:00",
    )  # fmt: skip


def position(db, sym="INFY", unrealised=15.0, unprotected=0) -> None:
    sql(
        db,
        "INSERT INTO positions (position_id, symbol, exchange, track, bucket_id, broker_id,"
        " quantity, average_entry_price, current_price, unrealised_pnl, stop_loss_price,"
        " target_price, atr_at_entry, entry_order_id, unprotected_flag, is_open, entry_at)"
        " VALUES (?,?, 'NSE', 'intraday', 'intraday', 'paper', 10, 100, 101.5, ?, 98, 104, 2,"
        " 'o', ?, 1, '2026-11-19T10:00:00')",
        f"p-{sym}", sym, unrealised, unprotected,
    )  # fmt: skip


def mode_log(db: str) -> list[tuple]:
    c = sqlite3.connect(db)
    rows = c.execute("SELECT old_mode, new_mode, changed_by, reason FROM bot_mode_log").fetchall()
    c.close()
    return rows


class TestParse:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("/status", ("/status", "")),
            ("/STATUS@BoomerBot", ("/status", "")),
            ("/emergency confirm", ("/emergency", "confirm")),
            ("  /emergency   confirm ", ("/emergency", "confirm")),
        ],
    )
    def test_parse(self, text, expected):
        assert commands.parse(text) == expected

    def test_unknown_command_is_none(self, db):
        assert commands.run(db, "/nope") is None


class TestReads:
    def test_pnl_worked_example(self, db):
        bracket(db, "INFY", "closed", pnl=40.0, reason="target_hit")
        bracket(db, "TCS", "closed", pnl=-25.0, reason="stop_hit")
        position(db, unrealised=15.0)
        text = commands.run(db, "/pnl")
        assert "Realised: +₹15 over 2 closed (1 won, 1 lost)" in text
        assert "Unrealised: +₹15 on 1 open" in text
        assert "Total: +₹30" in text

    def test_pnl_with_nothing(self, db):
        text = commands.run(db, "/pnl")
        assert "Realised: ₹0 over 0 closed (0 won, 0 lost)" in text and "Total: ₹0" in text

    def test_positions_show_stop_and_target(self, db):
        position(db)
        text = commands.run(db, "/positions")
        assert "INFY" in text and "stop 98.00" in text and "target 104.00" in text
        assert "UNPROTECTED" not in text

    def test_unprotected_position_is_flagged(self, db):
        position(db, unprotected=1)
        assert "UNPROTECTED" in commands.run(db, "/positions")

    def test_no_positions(self, db):
        assert "None." in commands.run(db, "/positions")

    def test_trades_list_states_and_outcomes(self, db):
        bracket(db, "INFY", "closed", pnl=40.0, reason="target_hit")
        bracket(db, "TCS", "open")
        bracket(db, "SBIN", "cancelled", reason="entry_expired", qty=5)
        text = commands.run(db, "/trades")
        assert "Today's trades</b> (3)" in text
        assert "INFY</b> 10 closed" in text and "target_hit, +₹40" in text
        assert "TCS</b> 10 open" in text
        assert "SBIN</b> 5 cancelled" in text and "entry_expired" in text

    def test_no_trades(self, db):
        assert "None yet." in commands.run(db, "/trades")

    def test_html_in_data_is_escaped(self, db):
        bracket(db, "A<B>", "open")
        assert "A&lt;B&gt;" in commands.run(db, "/trades")


class TestStatus:
    def test_no_session_yet(self, db):
        text = commands.run(db, "/status")
        assert "not started today" in text and "Bot mode: <b>auto</b>" in text
        assert "Kite checks: none run today" in text

    def test_trading_session_with_checks_and_trades(self, db):
        session(db, "trading")
        sql(
            db,
            "INSERT INTO kite_daily_checks (session_date, check_name, ok, detail_json, source,"
            " ran_at) VALUES (?, 'preflight', 1, '{}', 's', ?)", DAY, f"{DAY}T08:55:00",
        )  # fmt: skip
        bracket(db, "INFY", "closed", pnl=40.0, reason="target_hit")
        bracket(db, "TCS", "open")
        text = commands.run(db, "/status")
        assert "Session: <b>trading</b> (PAPER)" in text
        assert "✅ preflight 08:55" in text
        assert "Trades: 1 active, 1 closed (1W/0L)" in text

    def test_halted_points_to_halt(self, db):
        session(db, "halted", "feed_stale")
        text = commands.run(db, "/status")
        assert "halted: feed_stale" in text and "/halt" in text

    def test_latest_check_wins(self, db):
        session(db, "trading")
        for ok, t in ((0, "08:55"), (1, "09:30")):
            sql(
                db,
                "INSERT INTO kite_daily_checks (session_date, check_name, ok, detail_json,"
                " source, ran_at) VALUES (?, 'canary', ?, '{}', 's', ?)", DAY, ok, f"{DAY}T{t}:00",
            )  # fmt: skip
        assert "✅ canary 09:30" in commands.run(db, "/status")


class TestHalt:
    def test_not_halted(self, db):
        session(db, "trading")
        assert "Not halted" in commands.run(db, "/halt")

    def test_no_session(self, db):
        assert "No session today" in commands.run(db, "/halt")

    def test_reconcile_mismatch_lists_failed_checks_and_asks_for_ack(self, db):
        detail = {
            "failed_checks": ["net_quantity_matches_broker"],
            "net_quantity_matches_broker": {
                "ok": False,
                "mismatch": {"INFY": {"ours": 0, "broker": 10}},
            },
        }
        session(db, "halted", "reconcile_mismatch", detail)
        text = commands.run(db, "/halt")
        assert "Halted: reconcile_mismatch" in text
        assert "net_quantity_matches_broker" in text and "broker" in text
        assert "/ack" in text and "Acknowledged: not yet" in text

    def test_kite_check_halt_points_to_canary(self, db):
        session(db, "halted", "canary_failed")
        assert "/canary" in commands.run(db, "/halt")

    def test_non_resumable_halt_says_so(self, db):
        session(db, "halted", "breaker:intraday_daily_loss")
        assert "Not resumable today" in commands.run(db, "/halt")

    def test_paused_halt_says_resume(self, db):
        session(db, "halted", "paused")
        assert "/resume" in commands.run(db, "/halt")


class TestAck:
    def test_acknowledges_a_mismatch_and_records_it(self, db):
        session(db, "halted", "reconcile_mismatch")
        assert "✅ Acknowledged" in commands.run(db, "/ack")
        c = sqlite3.connect(db)
        acked = c.execute("SELECT halt_acked_at FROM trading_sessions").fetchone()[0]
        c.close()
        assert acked
        assert "Acknowledged: yes" in commands.run(db, "/halt")

    @pytest.mark.parametrize("state, reason", [("trading", None), ("halted", "feed_stale")])
    def test_refuses_when_there_is_no_mismatch(self, db, state, reason):
        session(db, state, reason)
        assert "Nothing to acknowledge" in commands.run(db, "/ack")
        c = sqlite3.connect(db)
        assert c.execute("SELECT halt_acked_at FROM trading_sessions").fetchone()[0] is None
        c.close()

    def test_no_session(self, db):
        assert "Nothing to acknowledge" in commands.run(db, "/ack")


class TestControl:
    def test_pause_then_resume_are_audited(self, db):
        assert "Paused" in commands.run(db, "/pause")
        assert "Resumed" in commands.run(db, "/resume")
        assert [(a, b, c) for a, b, c, _ in mode_log(db)] == [
            ("auto", "paused", "telegram"),
            ("paused", "auto", "telegram"),
        ]

    def test_emergency_without_confirm_changes_nothing(self, db):
        text = commands.run(db, "/emergency")
        assert "EMERGENCY STOP" in text and "/emergency confirm" in text
        assert mode_log(db) == []

    @pytest.mark.parametrize("args", ["yes", "confirmed", "please"])
    def test_emergency_needs_exactly_confirm(self, db, args):
        commands.run(db, f"/emergency {args}")
        assert mode_log(db) == []

    def test_emergency_confirm_sets_the_mode(self, db):
        assert "EMERGENCY STOP engaged" in commands.run(db, "/emergency confirm")
        (entry,) = mode_log(db)
        assert entry[1] == "emergency_stop" and entry[2] == "telegram"
        assert "telegram /emergency confirm" in entry[3]

    def test_pause_refused_during_an_emergency_stop(self, db):
        commands.run(db, "/emergency confirm")
        assert "Emergency stop is active" in commands.run(db, "/pause")
        assert len(mode_log(db)) == 1

    def test_resume_after_emergency_notes_the_day_is_over(self, db):
        session(db, "closed")
        commands.run(db, "/emergency confirm")
        assert "already ended" in commands.run(db, "/resume")

    def test_resume_while_halted_points_to_halt(self, db):
        session(db, "halted", "feed_stale")
        assert "/halt" in commands.run(db, "/resume")


class TestBotDispatch:
    @pytest.fixture()
    def sent(self, monkeypatch):
        out: list[str] = []
        monkeypatch.setattr(telegram_bot, "_send", lambda token, chat, text, *a: out.append(text))
        return out

    def update(self, text, chat=42):
        return {"message": {"chat": {"id": chat}, "text": text}}

    def test_new_commands_are_routed(self, db, sent):
        bot = telegram_bot.TelegramBot("tok", "42", db)
        for cmd, marker in (
            ("/pnl", "P&amp;L today"),
            ("/trades", "trades"),
            ("/status@boomerbot", "Boomer"),
        ):
            bot._dispatch(self.update(cmd))
            assert marker in sent[-1]

    def test_unknown_command(self, db, sent):
        telegram_bot.TelegramBot("tok", "42", db)._dispatch(self.update("/nonsense"))
        assert "Unknown command" in sent[-1]

    def test_plain_text_is_ignored(self, db, sent):
        telegram_bot.TelegramBot("tok", "42", db)._dispatch(self.update("hello"))
        assert sent == []

    def test_other_chats_are_ignored(self, db, sent):
        telegram_bot.TelegramBot("tok", "42", db)._dispatch(
            self.update("/emergency confirm", chat=99)
        )
        assert sent == [] and mode_log(db) == []

    def test_a_failing_handler_reports_instead_of_crashing(self, db, sent, monkeypatch):
        monkeypatch.setitem(commands.HANDLERS, "/pnl", lambda d, a: 1 / 0)
        telegram_bot.TelegramBot("tok", "42", db)._dispatch(self.update("/pnl"))
        assert "/pnl failed" in sent[-1]

    def test_help_lists_every_command(self, db, sent):
        telegram_bot.TelegramBot("tok", "42", db)._dispatch(self.update("/help"))
        for cmd in ("/status", "/positions", "/trades", "/pnl", "/pause", "/resume",
                    "/emergency", "/halt", "/ack", "/mode", "/canary"):  # fmt: skip
            assert cmd in sent[-1]


class TestToggle:
    """/toggle flips the NEXT session paper <-> live through the same audited path as /mode."""

    @pytest.fixture()
    def armed(self, monkeypatch):
        monkeypatch.setenv("BOOMER_TRADING_MODE", "live")

    def flips(self, db) -> list[tuple[int, str]]:
        c = sqlite3.connect(db)
        rows = c.execute("SELECT enabled, actor FROM live_trading_authorisation").fetchall()
        c.close()
        return rows

    def test_to_live_asks_first_and_shows_the_paper_record(self, db, armed):
        text = telegram_bot.handle_toggle(db, "/toggle")
        assert "Paper so far" in text and "/toggle confirm" in text
        assert self.flips(db) == []

    def test_to_live_with_confirm_switches(self, db, armed):
        text = telegram_bot.handle_toggle(db, "/toggle confirm")
        assert text.startswith("✅") and "LIVE" in text
        assert [a for _, a in self.flips(db)] == ["telegram"]
        assert "Next session: LIVE" in telegram_bot.handle_mode(db, "/mode")

    def test_live_is_refused_when_the_server_is_not_armed(self, db, monkeypatch):
        monkeypatch.delenv("BOOMER_TRADING_MODE", raising=False)
        for text in ("/toggle", "/toggle confirm"):
            assert "not armed" in telegram_bot.handle_toggle(db, text)
        assert self.flips(db) == []

    def test_from_live_back_to_paper_needs_no_confirm(self, db, armed):
        telegram_bot.handle_toggle(db, "/toggle confirm")
        text = telegram_bot.handle_toggle(db, "/toggle")
        assert text.startswith("✅") and "PAPER" in text
        assert "Next session: PAPER" in telegram_bot.handle_mode(db, "/mode")

    def test_dispatch_routes_toggle_and_help_lists_it(self, db, armed, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(telegram_bot, "_send", lambda token, chat, text, *a: sent.append(text))
        bot = telegram_bot.TelegramBot("tok", "42", db)
        bot._dispatch({"message": {"chat": {"id": 42}, "text": "/toggle@boomerbot"}})
        assert "/toggle confirm" in sent[-1]
        bot._dispatch({"message": {"chat": {"id": 42}, "text": "/help"}})
        assert "/toggle" in sent[-1]
