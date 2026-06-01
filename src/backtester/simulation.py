from __future__ import annotations

import hashlib
import json
import logging
import math
import pathlib
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from datetime import date, datetime
from zoneinfo import ZoneInfo

from backtester.costs import CostModel
from backtester.models import (
    BacktestConfig,
    BacktestDailyState,
    BacktestSummary,
    BacktestTrade,
    EntryDecision,
)
from backtester.slippage import SlippageModel
from executor.brokers.mock_broker import MockBroker
from executor.models import (
    GttRequest,
    GttStatus,
    GttType,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    PriceBar,
)
from market_calendar.nse_holidays import trading_days

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)

# Callable type aliases (not enforced at runtime, aid readability)
PriceLoader = Callable[[str, str, str], list[PriceBar]]      # (symbol, from_date, to_date)
FeatureLoader = Callable[[str, str], dict[str, float]]       # (symbol, as_of_date)
EntryDecider = Callable[[str, date, dict[str, float], PriceBar], EntryDecision | None]
RegimeLoader = Callable[[date], str]                         # date → regime string


class BacktestSimulation:
    """
    Replay historical data through the same executor code as live trading.

    Same-code principle: only the broker (MockBroker) and time source are swapped.
    All cost/slippage logic runs identically to live mode.

    Holdout integrity: tracks a code hash derived from the actual backtester source
    files. After 5 runs against the same OOS period + code hash, that period is
    considered burned.

    Survivorship bias note: v1 uses current Nifty 500 universe. Returns are biased
    upward ~3-5% annually. The Sharpe acceptance threshold is raised to 1.3 (from 1.0)
    to absorb this bias.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        config: BacktestConfig,
        price_loader: PriceLoader,
        feature_loader: FeatureLoader,
        universe: list[str],
        entry_decider: EntryDecider | None = None,
        regime_loader: RegimeLoader | None = None,
        in_sample_sharpe: float | None = None,
        code_hash: str | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._price_loader = price_loader
        self._feature_loader = feature_loader
        self._universe = universe
        self._entry_decider = entry_decider
        self._regime_loader = regime_loader
        self._in_sample_sharpe = in_sample_sharpe
        self._code_hash = code_hash or self._compute_code_hash()
        self._cost_model = CostModel()
        self._slippage_model = SlippageModel()
        self._mock = MockBroker(initial_cash=config.initial_capital)
        self._trades: list[BacktestTrade] = []
        self._daily_states: list[BacktestDailyState] = []
        self._daily_state_buffer: list[tuple] = []
        # symbol → position dict; keyed by broker_order_id (= position_id)
        self._open_positions: dict[str, dict] = {}
        self._hwm = config.initial_capital

    def run(self) -> BacktestSummary:
        """
        Execute full simulation. Returns summary with acceptance verdict.
        Persists run, trades, and daily states to DB.
        """
        run_id = str(uuid.uuid4())
        start_iso = datetime.now(IST).replace(tzinfo=None).isoformat()

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
        # 1. Load price bars for held positions and universe symbols
        bars = self._load_bars_for_date(sim_date)
        bar_by_symbol: dict[str, PriceBar] = {b.symbol: b for b in bars}

        # 2. Advance MockBroker — processes resting orders and GTT triggers
        for bar in bars:
            self._mock.set_price_bar(bar)

        # 3. Process any GTTs that fired today → close matching positions
        self._process_gtt_triggers(sim_date, bar_by_symbol)

        # 4. Generate new entries if an entry_decider is wired
        if self._entry_decider is not None:
            self._generate_entries(sim_date, bar_by_symbol)

        # 5. Determine market regime for today
        regime = self._regime_loader(sim_date) if self._regime_loader is not None else "unknown"

        # 6. Mark-to-market all open positions and record daily state
        capital = self._compute_capital(bar_by_symbol)
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
            regime=regime,
        )
        self._daily_states.append(daily)
        self._buffer_daily_state(run_id, daily)

    # ── Entry generation ──────────────────────────────────────────────────────

    def _generate_entries(self, sim_date: date, bar_by_symbol: dict[str, PriceBar]) -> None:
        """Evaluate signals for each universe symbol and open new positions."""
        cfg = self._config
        open_symbols = {p["symbol"] for p in self._open_positions.values()}

        for symbol in self._universe:
            if len(self._open_positions) >= cfg.max_open_positions:
                break
            if symbol in open_symbols:
                continue  # already hold this symbol

            bar = bar_by_symbol.get(symbol)
            if bar is None:
                continue  # no data (holiday, suspension, new listing)

            features = self._feature_loader(symbol, str(sim_date))
            decision = self._entry_decider(symbol, sim_date, features, bar)  # type: ignore[misc]
            if decision is None:
                continue

            # Position sizing: max_position_pct of available cash
            available = self._mock.get_funds().available_cash
            ref_price = decision.entry_price if decision.entry_price > 0 else bar.open
            if ref_price <= 0:
                continue

            max_value = available * cfg.max_position_pct
            quantity = max(1, int(max_value / ref_price))
            required_cash = quantity * ref_price

            # Keep a 2% cash buffer to absorb rounding and costs
            if required_cash > available * 0.98:
                continue

            # Place buy order through MockBroker
            order_type = OrderType.LIMIT if decision.entry_price > 0 else OrderType.MARKET
            buy_req = OrderRequest(
                symbol=symbol,
                exchange="NSE",
                side=OrderSide.BUY,
                order_type=order_type,
                quantity=quantity,
                product=decision.product,
                price=decision.entry_price if order_type == OrderType.LIMIT else 0.0,
                trigger_price=0.0,
            )
            broker_order_id = self._mock.place_order(buy_req)
            order_status = self._mock.get_order_status(broker_order_id)

            if order_status.get("status") != OrderStatus.FILLED:
                continue  # limit order didn't fill today — leave resting

            fill_price = order_status["average_fill_price"]
            position_id = broker_order_id
            open_symbols.add(symbol)

            self._open_positions[position_id] = {
                "symbol": symbol,
                "quantity": quantity,
                "entry_price": fill_price,
                "entry_date": sim_date,
                "current_price": fill_price,
                "product": decision.product,
                "track": decision.track,
                "confidence": decision.confidence,
                "strategy_id": decision.strategy_id,
                "sl_price": decision.sl_price,
                "target_price": decision.target_price,
            }

            # Place OCO GTT; link back to position via parent_order_id
            if decision.sl_price > 0 and decision.target_price > 0:
                gtt_req = GttRequest(
                    symbol=symbol,
                    exchange="NSE",
                    gtt_type=GttType.OCO,
                    quantity=quantity,
                    sl_trigger_price=decision.sl_price,
                    sl_limit_price=round(decision.sl_price * 0.99, 2),  # 1% below trigger
                    target_trigger_price=decision.target_price,
                    target_limit_price=decision.target_price,
                    parent_order_id=position_id,
                )
                self._mock.place_gtt(gtt_req)

            logger.debug(
                "Entry: %s qty=%d fill=%.2f sl=%.2f target=%.2f track=%s",
                symbol, quantity, fill_price,
                decision.sl_price, decision.target_price, decision.track,
            )

    # ── GTT trigger processing ────────────────────────────────────────────────

    def _process_gtt_triggers(self, sim_date: date, bar_by_symbol: dict[str, PriceBar]) -> None:
        """Close positions whose OCO GTT fired today."""
        for gtt in self._mock.list_gtts():
            if gtt.get("status") != GttStatus.GTT_TRIGGERED:
                continue

            position_id = gtt.get("parent_order_id")
            if not position_id:
                continue
            if position_id not in self._open_positions:
                continue  # already closed (both GTT legs share the same parent)

            triggered_leg = gtt.get("triggered_leg", "sl")
            price_key = "sl_limit_price" if triggered_leg == "sl" else "target_limit_price"
            exit_price = gtt.get(price_key)
            if not exit_price:
                continue

            pos = self._open_positions[position_id]
            bar = bar_by_symbol.get(pos["symbol"])
            self._close_position(position_id, exit_price, triggered_leg, sim_date, bar)

            # Mark GTT cancelled so it won't re-trigger on future days
            self._mock.cancel_gtt(gtt["broker_gtt_id"])

    def _close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_reason: str,
        sim_date: date,
        bar: PriceBar | None,
    ) -> None:
        pos = self._open_positions.pop(position_id, None)
        if pos is None:
            return

        qty = pos["quantity"]
        entry_price = pos["entry_price"]
        product = pos["product"]

        # Slippage on exit — use actual OHLCV bar; fall back to ±1% synthetic if unavailable
        order_type = OrderType.SL if "sl" in exit_reason else OrderType.LIMIT
        if bar is not None:
            bar_open, bar_high, bar_low = bar.open, bar.high, bar.low
        else:
            bar_open = exit_price
            bar_high = exit_price * 1.01
            bar_low = exit_price * 0.99

        slip = self._slippage_model.simulate_fill(
            order_type=order_type,
            side=OrderSide.SELL,
            order_price=exit_price,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
        )
        actual_exit = slip.fill_price
        gross_pnl = (actual_exit - entry_price) * qty
        costs = self._cost_model.round_trip_cost(entry_price * qty, actual_exit * qty, product)
        net_pnl = gross_pnl - costs.total

        # Credit exit proceeds (minus costs) back into MockBroker's cash
        proceeds = actual_exit * qty - costs.total
        self._mock.set_cash(self._mock.get_funds().available_cash + proceeds)

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

    def _compute_capital(self, bar_by_symbol: dict[str, PriceBar]) -> float:
        funds = self._mock.get_funds()
        deployed = 0.0
        for pos in self._open_positions.values():
            bar = bar_by_symbol.get(pos["symbol"])
            ltp = bar.close if bar is not None else pos["current_price"]
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
            sharpe, max_dd, trades_by_track, expectancy, win_rate, avg_win, avg_loss,
            in_sample_sharpe=self._in_sample_sharpe,
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
        in_sample_sharpe: float | None = None,
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

        # OOS check: only evaluated when an in-sample Sharpe is provided (walk-forward run)
        if in_sample_sharpe is not None:
            min_oos = in_sample_sharpe * cfg.min_oos_pct_of_is
            if sharpe < min_oos:
                failures.append(
                    f"OOS Sharpe {sharpe:.2f} < {min_oos:.2f}"
                    f" ({cfg.min_oos_pct_of_is:.0%} of IS {in_sample_sharpe:.2f})"
                )

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
        now = datetime.now(IST).replace(tzinfo=None).isoformat()
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
                    trade_id, run_id, trade.symbol, trade.track, trade.side,
                    str(trade.entry_date),
                    str(trade.exit_date) if trade.exit_date else None,
                    trade.entry_price, trade.exit_price, trade.quantity,
                    trade.gross_pnl, trade.transaction_costs, trade.slippage_cost,
                    trade.net_pnl, trade.hold_days, trade.exit_reason,
                    trade.signal_confidence, trade.strategy_id,
                ),
            )
        # Flush all buffered daily states in one go
        for row in self._daily_state_buffer:
            self._db.execute(
                """
                INSERT OR REPLACE INTO backtest_daily_state
                (state_id, run_id, date, total_capital, deployed_capital, cash,
                 open_positions, drawdown_from_hwm, regime)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                row,
            )
        self._db.commit()

    def _buffer_daily_state(self, run_id: str, state: BacktestDailyState) -> None:
        """Queue a daily state row; flushed in bulk at run end to avoid per-row commits."""
        self._daily_state_buffer.append((
            str(uuid.uuid4()),
            run_id,
            str(state.date),
            state.total_capital,
            state.deployed_capital,
            state.cash,
            state.open_positions,
            state.drawdown_from_hwm,
            state.regime,
        ))

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _trading_days(self) -> Iterator[date]:
        """Yield NSE trading days (Mon–Fri minus NSE market holidays) in the config range."""
        return trading_days(self._config.start_date, self._config.end_date)

    def _load_bars_for_date(self, sim_date: date) -> list[PriceBar]:
        bars = []
        symbols_needed = set(self._universe)
        symbols_needed |= {p["symbol"] for p in self._open_positions.values()}
        for symbol in symbols_needed:
            raw = self._price_loader(symbol, str(sim_date), str(sim_date))
            if raw:
                bars.extend(raw if isinstance(raw, list) else [raw])
        return bars

    @staticmethod
    def _compute_code_hash() -> str:
        """Hash the backtester source files so holdout tracking resets on code changes."""
        hasher = hashlib.sha256()
        base = pathlib.Path(__file__).parent
        for fname in sorted(["simulation.py", "models.py", "costs.py", "slippage.py"]):
            p = base / fname
            if p.exists():
                hasher.update(p.read_bytes())
        return hasher.hexdigest()[:16]
