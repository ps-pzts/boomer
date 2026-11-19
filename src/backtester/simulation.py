from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from datetime import date, datetime
from zoneinfo import ZoneInfo

from backtester import acceptance, run_store
from backtester.bar_broker import BarBroker
from backtester.costs import CostModel
from backtester.models import (
    BacktestConfig,
    BacktestDailyState,
    BacktestSummary,
    BacktestTrade,
    EntryDecision,
)
from backtester.slippage import SlippageModel
from executor.models import (
    GttRequest,
    GttType,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    PriceBar,
)
from executor.ticks import DEFAULT_TICK, round_to_tick
from market_calendar.nse_holidays import trading_days

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)

# Callable type aliases (not enforced at runtime, aid readability)
PriceLoader = Callable[[str, str, str], list[PriceBar]]  # (symbol, from_date, to_date)
FeatureLoader = Callable[[str, str], dict[str, float]]  # (symbol, as_of_date)
EntryDecider = Callable[[str, date, dict[str, float], PriceBar], EntryDecision | None]
RegimeLoader = Callable[[date], str]  # date → regime string


class BacktestSimulation:
    """
    Replay historical data through the same executor code as live trading.

    Same-code principle: orders go through the real KiteBroker on KiteSim, driven
    by the day's bars (backtester/bar_broker.py, #86); only time is simulated.
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
        self._bars = BarBroker(cash=config.initial_capital)
        self._cash = config.initial_capital  # the backtest's own ledger: fills, costs
        self._gtt_positions: dict[str, str] = {}  # broker trigger_id -> position_id
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

        # 2. Walk each bar through KiteSim: resting orders fill, GTTs trigger
        self._bars.start_day(sim_date)
        for bar in bars:
            self._bars.walk(bar)

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
        self._bars.end_day()  # DAY orders still working expire at the close

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
            available = self._cash
            ref_price = decision.entry_price if decision.entry_price > 0 else bar.open
            if ref_price <= 0:
                continue

            max_value = available * cfg.max_position_pct
            quantity = max(1, int(max_value / ref_price))
            required_cash = quantity * ref_price

            # Keep a 2% cash buffer to absorb rounding and costs
            if required_cash > available * 0.98:
                continue

            # Place the buy at the bar's open through KiteBroker -> KiteSim
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
            order_status = self._bars.enter(buy_req, bar)

            if order_status.get("status") != OrderStatus.FILLED:
                continue  # limit not reached today: it expires at the close

            fill_price = order_status["average_fill_price"]
            self._cash -= quantity * fill_price
            position_id = str(uuid.uuid4())
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

            if decision.sl_price > 0 and decision.target_price > 0:
                self._protect(position_id, quantity, decision, sim_date, bar)

            logger.debug(
                "Entry: %s qty=%d fill=%.2f sl=%.2f target=%.2f track=%s",
                symbol,
                quantity,
                fill_price,
                decision.sl_price,
                decision.target_price,
                decision.track,
            )

    def _protect(
        self,
        position_id: str,
        quantity: int,
        decision: EntryDecision,
        sim_date: date,
        bar: PriceBar,
    ) -> None:
        """OCO GTT (stop + target), prices on the tick: Kite refuses off-tick ones.

        Kite also refuses an OCO whose stop is at or above the last price, or whose
        target is at or below it (#86; the pre-#86 simulator accepted both and held
        the position). A leg is then already breached as the position is opened, so
        it is closed at once at the close, as the live system force-closes an
        unprotected position: exit_reason `sl_breached` / `target_breached`."""
        sl = round_to_tick(decision.sl_price, DEFAULT_TICK, up=True)
        target = round_to_tick(decision.target_price, DEFAULT_TICK, up=True)
        gtt_req = GttRequest(
            symbol=bar.symbol,
            exchange="NSE",
            gtt_type=GttType.OCO,
            quantity=quantity,
            sl_trigger_price=sl,
            sl_limit_price=round_to_tick(sl * 0.99, DEFAULT_TICK, up=False),  # 1% below
            target_trigger_price=target,
            target_limit_price=target,
            parent_order_id=position_id,
        )
        try:
            self._gtt_positions[self._bars.place_gtt(gtt_req)] = position_id
        except Exception as exc:
            logger.warning(
                "backtest_gtt_refused symbol=%s sl=%.2f close=%.2f error=%s",
                bar.symbol,
                sl,
                bar.close,
                exc,
            )
            reason = "sl_breached" if bar.close <= sl else "target_breached"
            self._close_position(position_id, bar.close, reason, sim_date, bar)

    # ── GTT trigger processing ────────────────────────────────────────────────

    def _process_gtt_triggers(self, sim_date: date, bar_by_symbol: dict[str, PriceBar]) -> None:
        """Close positions whose OCO GTT fired today (Kite's `triggered` status;
        the leg with a `result` is the one that fired)."""
        for trigger_id, leg, exit_price in self._bars.triggered_gtts():
            position_id = self._gtt_positions.pop(trigger_id, None)
            if position_id is None or position_id not in self._open_positions:
                continue  # already handled
            triggered_leg = "sl" if leg == 0 else "target"
            pos = self._open_positions[position_id]
            bar = bar_by_symbol.get(pos["symbol"])
            self._close_position(position_id, exit_price, triggered_leg, sim_date, bar)

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

        # Credit exit proceeds (minus costs) back into the ledger
        self._cash += actual_exit * qty - costs.total

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
        deployed = 0.0
        for pos in self._open_positions.values():
            bar = bar_by_symbol.get(pos["symbol"])
            ltp = bar.close if bar is not None else pos["current_price"]
            pos["current_price"] = ltp
            deployed += pos["quantity"] * ltp
        return self._cash + deployed

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

        win_rate, avg_win, avg_loss, expectancy = acceptance.trade_stats(
            self._trades, self._config.initial_capital
        )

        tracks = self._config.tracks
        trades_by_track = {t: sum(1 for tr in self._trades if tr.track == t) for t in tracks}

        failures, passes = self._check_acceptance(
            sharpe,
            max_dd,
            trades_by_track,
            expectancy,
            win_rate,
            avg_win,
            avg_loss,
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
        return acceptance.check(
            self._config,
            sharpe,
            max_dd,
            trades_by_track,
            win_rate,
            avg_win,
            avg_loss,
            in_sample_sharpe,
        )

    def _daily_returns(self) -> list[float]:
        return acceptance.daily_returns([d.total_capital for d in self._daily_states])

    @staticmethod
    def _sharpe(daily_returns: list[float], risk_free_annual: float = 0.065) -> float:
        return acceptance.sharpe(daily_returns, risk_free_annual)

    # ── Persistence (SQL lives in backtester/run_store.py) ────────────────────

    def _persist_run_start(self, run_id: str, start_iso: str) -> None:
        run_store.persist_run_start(self._db, run_id, self._config, self._code_hash, start_iso)

    def _persist_run_complete(self, run_id: str, summary: BacktestSummary) -> None:
        run_store.persist_run_complete(
            self._db, run_id, summary, self._trades, self._daily_state_buffer
        )

    def _buffer_daily_state(self, run_id: str, state: BacktestDailyState) -> None:
        """Queue a daily state row; flushed in bulk at run end to avoid per-row commits."""
        self._daily_state_buffer.append(run_store.daily_state_row(run_id, state))

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
        return run_store.code_hash()
