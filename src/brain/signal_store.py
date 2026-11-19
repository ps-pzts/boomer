"""Persist signals and trade plans (migration 0003 tables) for audit and replay (#69).

Design §3.4: live features are never written to the date-granular `features`
table (lookahead). The full merged feature dict a decision used is kept per
signal in `signals.feature_snapshot` instead.
"""

from __future__ import annotations

import json
import sqlite3

from brain.models import SignalRecord, TradePlan


def _ts(dt) -> str:
    return dt.replace(tzinfo=None).isoformat()


def save_signal(conn: sqlite3.Connection, s: SignalRecord) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO signals (signal_id, stock_symbol, exchange, track, direction,"
        " raw_score, confidence, regime_at_signal, contributing_signals, feature_snapshot,"
        " generated_at, generator_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            s.signal_id,
            s.stock_symbol,
            s.exchange,
            s.track,
            str(s.direction),
            s.raw_score,
            s.confidence,
            s.regime_at_signal,
            json.dumps(
                [
                    {
                        "name": c.name,
                        "weight": c.weight,
                        "value": c.value,
                        "contribution": c.contribution,
                    }
                    for c in s.contributing_signals
                ]
            ),
            json.dumps(s.feature_snapshot, default=str),
            _ts(s.generated_at),
            s.generator_version,
        ),
    )


def save_plan(conn: sqlite3.Connection, p: TradePlan, strategy: str | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO trade_plans (plan_id, signal_id, stock_symbol, exchange, track,"
        " direction, entry_zone_low, entry_zone_high, stop_loss_price, target_price,"
        " expected_reward_per_share, expected_risk_per_share, reward_to_risk,"
        " expected_value_per_share, decision, skip_reason, entry_strategy_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            p.plan_id,
            p.signal_id,
            p.stock_symbol,
            p.exchange,
            p.track,
            str(p.direction),
            float(p.entry_zone_low),
            float(p.entry_zone_high),
            float(p.stop_loss_price),
            float(p.target_price),
            float(p.expected_reward_per_share),
            float(p.expected_risk_per_share),
            float(p.reward_to_risk),
            float(p.expected_value_per_share),
            p.decision,
            p.skip_reason.value if p.skip_reason else None,
            strategy or (p.entry_strategy_id.value if p.entry_strategy_id else None),
            _ts(p.created_at),
        ),
    )
