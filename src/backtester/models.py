from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class BacktestConfig:
    name: str
    start_date: date
    end_date: date
    initial_capital: float
    tracks: list[str] = field(default_factory=lambda: ["long_term", "swing", "intraday"])
    universe: str = "nifty500_current"

    # Acceptance thresholds (per Phase 4 design doc)
    min_sharpe: float = 1.3  # raised from 1.0 for survivorship bias adjustment
    max_drawdown_pct: float = 15.0
    min_trades_per_track: int = 100
    min_expectancy_ratio: float = 1.5  # avg_win × win_rate ≥ 1.5 × avg_loss × loss_rate
    min_oos_pct_of_is: float = 0.5  # OOS performance ≥ 50% of in-sample


@dataclass
class BacktestTrade:
    symbol: str
    track: str
    side: str
    entry_date: date
    exit_date: date | None
    entry_price: float
    exit_price: float | None
    quantity: int
    gross_pnl: float
    transaction_costs: float
    slippage_cost: float
    net_pnl: float
    hold_days: int
    exit_reason: str  # stop_hit | target_hit | thesis_broken | forced | time_based
    signal_confidence: float
    strategy_id: str

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > 0


@dataclass
class BacktestDailyState:
    date: date
    total_capital: float
    deployed_capital: float
    cash: float
    open_positions: int
    drawdown_from_hwm: float
    regime: str


@dataclass
class BacktestSummary:
    run_id: str
    name: str
    start_date: date
    end_date: date
    initial_capital: float
    final_capital: float
    total_return_pct: float
    annualised_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    expectancy: float
    total_trades: int
    trades_by_track: dict[str, int]
    passes_acceptance: bool
    failure_reasons: list[str]

    def as_report_lines(self) -> list[str]:
        lines = [
            f"Backtest: {self.name}  {self.start_date} → {self.end_date}",
            f"  Capital:    ₹{self.initial_capital:,.0f} → ₹{self.final_capital:,.0f}",
            f"  Return:     {self.total_return_pct:.1f}% total"
            f" / {self.annualised_return_pct:.1f}% annualised",
            f"  Sharpe:     {self.sharpe_ratio:.2f} (threshold ≥1.3)",
            f"  Max DD:     {self.max_drawdown_pct:.1f}% (threshold ≤15%)",
            f"  Win rate:   {self.win_rate:.1%}"
            f"  avg_win={self.avg_win_pct:.1%}  avg_loss={self.avg_loss_pct:.1%}",
            f"  Expectancy: {self.expectancy:.4f}",
            f"  Trades:     {self.total_trades} total — {self.trades_by_track}",
            f"  Passes:     {'YES ✓' if self.passes_acceptance else 'NO ✗'}",
        ]
        if self.failure_reasons:
            lines.append(f"  Failures:   {', '.join(self.failure_reasons)}")
        return lines
