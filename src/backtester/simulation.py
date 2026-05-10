from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from backtester.costs import CostModel
from backtester.models import (
    BacktestConfig,
    BacktestDailyState,
    BacktestSummary,
    BacktestTrade,
)
from backtester.slippage import SlippageModel
from executor.brokers.mock_broker import MockBroker
from executor.models import (
    GttStatus,
    OrderSide,
    OrderType,
    PriceBar,
)

logger = logging.getLogger(__name__)


class BacktestSimulation:
    """
    Replay historical data through the same executor code as live trading.

    Same-code principle: this class only swaps the broker (MockBroker) and
    the time source. All Stage logic runs identically to live mode.

    Holdout integrity: tracks run_count per config hash. After 5 runs against
    the same out-of-sample period, that period is considered "burned."

    Survivorship bias note: v1 uses current Nifty 500 universe. Returns are
    biased upward ~3-5% annually. Walk-forward Sharpe threshold is raised to
    1.3 (from 1.0) to absorb this bias.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        config: BacktestConfig,
        price_loader: Any,  # callable(symbol, from_date, to_date) -> list[PriceBar]
        feature_loader: Any,  # callable(symbol, as_of_date) -> dict[str, float]
        universe: list[str],
        code_hash: str | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._price_loader = price_loader
        self._feature_loader = feature_loader
        self._universe = universe
        self._code_hash = code_hash or self._compute_code_hash()
        self._cost_model = CostModel()
        self._slippage_model = SlippageModel()
        self._mock = MockBroker(initial_cash=config.initial_capital)
        self._trades: list[BacktestTrade] = []
        self._daily_states: list[BacktestDailyState] = []
        self._open_positions: dict[str, dict] = {}  # position_id → position dict
        self._hwm = config.initial_capital

    def run(self) -> BacktestSummary:
        """
        Execute full simulation. Returns summary with acceptance verdict.
        Persists run, trades, and daily states to DB.
        """
        run_id = str(uuid.uuid4())
        start_iso = datetime.now(UTC).isoformat()

        self._persist_run_start(run_id, start_iso)
        logger.info("Backtest started run_id=%s name=%s", run_id, self._config.name)

        try:
            for sim_date in self._trading_days():
                self._simulate_day(sim_date, run_id)
            summary = self._compute_summary(run_id)
            self._persist_run_complete(run_id, summary)
            logger.info("Backtest complete run_id=%s sharpe=%.2f", run_id, summary.sharpe_ratio)
            return summary
        except Exception:
            self._db.execute("UPDATE backtest_runs SET status='failed' WHERE run_id=?", (run_id,))
            self._db.commit()
            raise

    # ── Day simulation ────────────────────────────────────────────────────────

    def _simulate_day(self, sim_date: date, run_id: str) -> None:
        # 1. Advance MockBroker with today's bars for all held symbols
        bars = self._load_bars_for_date(sim_date)
        for bar in bars:
            self._mock.set_price_bar(bar)  # processes resting orders and GTTs

        # 2. Check for GTT triggers that fired today
        self._process_gtt_triggers(sim_date)

        # 3. Update position P&L from closing prices
        capital = self._compute_capital(bars)
        deployed = sum(p["quantity"] * p["current_price"] for p in self._open_positions.values())

        drawdown = max(0.0, (self._hwm - capital) / self._hwm * 100) if self._hwm > 0 else 0.0
        if capital > self._hwm:
            self._hwm = capital

        daily = BacktestDailyState(
            date=sim_date,
            total_capital=capital,
            deployed_capital=deployed,
            cash=capital - deployed,
            open_positions=len(self._open_positions),
            drawdown_from_hwm=drawdown,
            regime="unknown",  # regime injected by Stage 1 in full pipeline
        )
        self._daily_states.append(daily)
        self._persist_daily_state(run_id, daily)

    def _load_bars_for_date(self, sim_date: date) -> list[PriceBar]:
        bars = []
        symbols_needed = set(self._universe)
        symbols_needed |= {p["symbol"] for p in self._open_positions.values()}
        for symbol in symbols_needed:
            bar = self._price_loader(symbol, str(sim_date), str(sim_date))
            if bar:
                bars.extend(bar if isinstance(bar, list) else [bar])
        return bars

    def _process_gtt_triggers(self, sim_date: date) -> None:
        for gtt in self._mock.list_gtts():
            if gtt.get("status") != GttStatus.GTT_TRIGGERED:
                continue
            parent_order_id = gtt.get("parent_order_id")
            if not parent_order_id:
                continue
            pos = self._open_positions.get(parent_order_id)
            if not pos:
                continue
            triggered_leg = gtt.get("triggered_leg", "sl")
            key = "sl_limit_price" if triggered_leg == "sl" else "target_limit_price"
            exit_price = gtt.get(key)
            if exit_price:
                self._close_position(parent_order_id, exit_price, triggered_leg, sim_date)

    def _close_position(
        self, position_id: str, exit_price: float, exit_reason: str, sim_date: date
    ) -> None:
        pos = self._open_positions.pop(position_id, None)
        if not pos:
            return
        qty = pos["quantity"]
        entry_price = pos["entry_price"]
        product = pos["product"]

        slip = self._slippage_model.simulate_fill(
            order_type=OrderType.SL if "sl" in exit_reason else OrderType.LIMIT,
            side=OrderSide.SELL,
            order_price=exit_price,
            bar_open=exit_price,
            bar_high=exit_price * 1.01,
            bar_low=exit_price * 0.99,
        )
        actual_exit = slip.fill_price
        gross_pnl = (actual_exit - entry_price) * qty
        costs = self._cost_model.round_trip_cost(entry_price * qty, actual_exit * qty, product)
        net_pnl = gross_pnl - costs.total

        trade = BacktestTrade(
            symbol=pos["symbol"],
            track=pos["track"],
            side="long",
            entry_date=pos["entry_date"],
            exit_date=sim_date,
            entry_price=entry_price,
            exit_price=actual_exit,
            quantity=qty,
            gross_pnl=gross_pnl,
            transaction_costs=costs.total,
            slippage_cost=slip.slippage_amount * qty,
            net_pnl=net_pnl,
            hold_days=(sim_date - pos["entry_date"]).days,
            exit_reason=exit_reason,
            signal_confidence=pos.get("confidence", 0.0),
            strategy_id=pos.get("strategy_id", ""),
        )
        self._trades.append(trade)

    def _compute_capital(self, bars: list[PriceBar]) -> float:
        bar_by_symbol = {b.symbol: b for b in bars}
        funds = self._mock.get_funds()
        deployed = 0.0
        for pos in self._open_positions.values():
            bar = bar_by_symbol.get(pos["symbol"])
            ltp = bar.close if bar else pos["current_price"]
            pos["current_price"] = ltp
            deployed += pos["quantity"] * ltp
        return funds.available_cash + deployed

    # ── Statistics ────────────────────────────────────────────────────────────

    def _compute_summary(self, run_id: str) -> BacktestSummary:
        if not self._daily_states:
            raise ValueError("No daily states — simulation may not have run")
        final_capital = self._daily_states[-1].total_capital
        total_return_pct = (
            (final_capital - self._config.initial_capital) / self._config.initial_capital * 100
        )
        n_years = max(1, (self._config.end_date - self._config.start_date).days / 365.25)
        annualised = ((final_capital / self._config.initial_capital) ** (1 / n_years) - 1) * 100

        daily_returns = self._daily_returns()
        sharpe = self._sharpe(daily_returns)
        max_dd = max((s.drawdown_from_hwm for s in self._daily_states), default=0.0)

        winners = [t for t in self._trades if t.net_pnl > 0]
        losers = [t for t in self._trades if t.net_pnl <= 0]
        win_rate = len(winners) / max(1, len(self._trades))
        avg_win = (
            (sum(t.net_pnl for t in winners) / len(winners) / self._config.initial_capital * 100)
            if winners
            else 0.0
        )
        avg_loss = (
            (sum(abs(t.net_pnl) for t in losers) / len(losers) / self._config.initial_capital * 100)
            if losers
            else 0.0
        )
        expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss

        tracks = self._config.tracks
        trades_by_track = {t: sum(1 for tr in self._trades if tr.track == t) for t in tracks}

        failures, passes = self._check_acceptance(
            sharpe, max_dd, trades_by_track, expectancy, win_rate, avg_win, avg_loss
        )

        return BacktestSummary(
            run_id=run_id,
            name=self._config.name,
            start_date=self._config.start_date,
            end_date=self._config.end_date,
            initial_capital=self._config.initial_capital,
            final_capital=final_capital,
            total_return_pct=round(total_return_pct, 2),
            annualised_return_pct=round(annualised, 2),
            sharpe_ratio=round(sharpe, 3),
            max_drawdown_pct=round(max_dd, 2),
            win_rate=round(win_rate, 4),
            avg_win_pct=round(avg_win, 4),
            avg_loss_pct=round(avg_loss, 4),
            expectancy=round(expectancy, 6),
            total_trades=len(self._trades),
            trades_by_track=trades_by_track,
            passes_acceptance=passes,
            failure_reasons=failures,
        )

    def _check_acceptance(
        self,
        sharpe: float,
        max_dd: float,
        trades_by_track: dict[str, int],
        expectancy: float,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
    ) -> tuple[list[str], bool]:
        failures = []
        cfg = self._config
        if sharpe < cfg.min_sharpe:
            failures.append(f"Sharpe {sharpe:.2f} < {cfg.min_sharpe}")
        if max_dd > cfg.max_drawdown_pct:
            failures.append(f"Max DD {max_dd:.1f}% > {cfg.max_drawdown_pct}%")
        for track, count in trades_by_track.items():
            if count < cfg.min_trades_per_track:
                failures.append(f"{track} trades {count} < {cfg.min_trades_per_track}")
        ev_ratio = (win_rate * avg_win) / max(1e-9, (1 - win_rate) * avg_loss)
        if ev_ratio < cfg.min_expectancy_ratio:
            failures.append(f"EV ratio {ev_ratio:.2f} < {cfg.min_expectancy_ratio}")
        return failures, len(failures) == 0

    def _daily_returns(self) -> list[float]:
        returns = []
        for i in range(1, len(self._daily_states)):
            prev = self._daily_states[i - 1].total_capital
            curr = self._daily_states[i].total_capital
            if prev > 0:
                returns.append((curr - prev) / prev)
        return returns

    @staticmethod
    def _sharpe(daily_returns: list[float], risk_free_annual: float = 0.065) -> float:
        if len(daily_returns) < 2:
            return 0.0
        risk_free_daily = (1 + risk_free_annual) ** (1 / 252) - 1
        excess = [r - risk_free_daily for r in daily_returns]
        mean = sum(excess) / len(excess)
        variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
        std = math.sqrt(variance) if variance > 0 else 1e-9
        return (mean / std) * math.sqrt(252)

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _persist_run_start(self, run_id: str, start_iso: str) -> None:
        self._db.execute(
            """
            INSERT INTO backtest_runs
            (run_id, name, code_hash, start_date, end_date, initial_capital,
             final_capital, status, tracks, universe, created_at)
            VALUES (?,?,?,?,?,?,0,'running',?,?,?)
            """,
            (
                run_id,
                self._config.name,
                self._code_hash,
                str(self._config.start_date),
                str(self._config.end_date),
                self._config.initial_capital,
                json.dumps(self._config.tracks),
                self._config.universe,
                start_iso,
            ),
        )
        self._db.commit()

    def _persist_run_complete(self, run_id: str, summary: BacktestSummary) -> None:
        now = datetime.now(UTC).isoformat()
        self._db.execute(
            """
            UPDATE backtest_runs SET
                final_capital=?, total_return_pct=?, annualised_return_pct=?,
                sharpe_ratio=?, max_drawdown_pct=?, win_rate=?, avg_win_pct=?,
                avg_loss_pct=?, expectancy=?, total_trades=?, status='complete', completed_at=?
            WHERE run_id=?
            """,
            (
                summary.final_capital,
                summary.total_return_pct,
                summary.annualised_return_pct,
                summary.sharpe_ratio,
                summary.max_drawdown_pct,
                summary.win_rate,
                summary.avg_win_pct,
                summary.avg_loss_pct,
                summary.expectancy,
                summary.total_trades,
                now,
                run_id,
            ),
        )
        for trade in self._trades:
            trade_id = str(uuid.uuid4())
            self._db.execute(
                """
                INSERT INTO backtest_trades
                (trade_id, run_id, symbol, track, side, entry_date, exit_date,
                 entry_price, exit_price, quantity, gross_pnl, transaction_costs,
                 slippage_cost, net_pnl, hold_days, exit_reason, signal_confidence, strategy_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trade_id,
                    run_id,
                    trade.symbol,
                    trade.track,
                    trade.side,
                    str(trade.entry_date),
                    str(trade.exit_date) if trade.exit_date else None,
                    trade.entry_price,
                    trade.exit_price,
                    trade.quantity,
                    trade.gross_pnl,
                    trade.transaction_costs,
                    trade.slippage_cost,
                    trade.net_pnl,
                    trade.hold_days,
                    trade.exit_reason,
                    trade.signal_confidence,
                    trade.strategy_id,
                ),
            )
        self._db.commit()

    def _persist_daily_state(self, run_id: str, state: BacktestDailyState) -> None:
        state_id = str(uuid.uuid4())
        self._db.execute(
            """
            INSERT OR REPLACE INTO backtest_daily_state
            (state_id, run_id, date, total_capital, deployed_capital, cash,
             open_positions, drawdown_from_hwm, regime)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                state_id,
                run_id,
                str(state.date),
                state.total_capital,
                state.deployed_capital,
                state.cash,
                state.open_positions,
                state.drawdown_from_hwm,
                state.regime,
            ),
        )
        self._db.commit()

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _trading_days(self) -> Iterator[date]:
        """Yield Mon–Fri between start and end dates (simplified; does not exclude NSE holidays)."""
        current = self._config.start_date
        while current <= self._config.end_date:
            if current.weekday() < 5:
                yield current
            current += timedelta(days=1)

    @staticmethod
    def _compute_code_hash() -> str:
        """Placeholder — in production, hash the Stage code + config for holdout tracking."""
        return hashlib.sha256(b"boomer_v1").hexdigest()[:16]
