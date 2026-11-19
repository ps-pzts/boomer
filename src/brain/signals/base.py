from __future__ import annotations

import math
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from brain.models import ContributingSignal, Direction, SignalRecord


class BaseSignalGenerator(ABC):
    """Common contract for all three signal tracks (Stage 2)."""

    @property
    @abstractmethod
    def track(self) -> str: ...

    @property
    @abstractmethod
    def generator_version(self) -> str: ...

    def generate(
        self,
        stock_symbol: str,
        exchange: str,
        features: dict[str, Any],
        regime: str,
        generated_at: datetime,
    ) -> SignalRecord | None:
        """Return a signal, or None if the data is insufficient to score.

        Scoring only. Liquidity and price floors are eligibility rules owned by
        risk_config and enforced by the watchlist and TradePlanGenerator (#38),
        so there is exactly one configurable floor.
        """
        contributors, raw_score = self._score(features, regime)
        if raw_score is None:
            return None

        direction = (
            Direction.LONG
            if raw_score > 0
            else (Direction.SHORT if raw_score < 0 else Direction.NEUTRAL)
        )
        confidence = self._confidence(raw_score, contributors, features)

        return SignalRecord(
            signal_id=str(uuid.uuid4()),
            stock_symbol=stock_symbol,
            exchange=exchange,
            track=self.track,
            direction=direction,
            raw_score=raw_score,
            confidence=confidence,
            regime_at_signal=regime,
            contributing_signals=contributors,
            feature_snapshot=dict(features),
            generated_at=generated_at,
            generator_version=self.generator_version,
        )

    @abstractmethod
    def _score(
        self,
        features: dict[str, Any],
        regime: str,
    ) -> tuple[list[ContributingSignal], float | None]:
        """Return (contributors, raw_score) or ([], None) if data is insufficient."""
        ...

    def _confidence(
        self,
        raw_score: float,
        contributing_signals: list[ContributingSignal],
        features: dict[str, Any],
    ) -> float:
        """Confidence formula from Stage 2 design:
        confidence = 0.5×|raw_score| + 0.3×agreement + 0.2×freshness
        """
        score_component = 0.5 * abs(raw_score)

        total = len(contributing_signals)
        if total == 0:
            agreement_component = 0.0
        else:
            aligned = sum(
                1
                for c in contributing_signals
                if ((raw_score >= 0 and c.value >= 0) or (raw_score < 0 and c.value < 0))
            )
            agreement_component = 0.3 * (aligned / total)

        days_since = features.get("days_since_max_observed", 0.0)
        decay_constant = 0.5  # intraday signals decay fast
        freshness = math.exp(-float(days_since) / decay_constant)
        freshness_component = 0.2 * freshness

        return min(1.0, max(0.0, score_component + agreement_component + freshness_component))

    @staticmethod
    def _clip(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))
