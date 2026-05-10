from __future__ import annotations

from typing import Any

from brain.models import ContributingSignal
from brain.signals.base import BaseSignalGenerator

_WEIGHTS: dict[str, float] = {
    "premarket_gap": 0.25,
    "opening_range": 0.20,
    "fo_signals": 0.20,
    "news_trade_decay": 0.15,
    "index_correlation": 0.10,
    "bid_ask_quality": 0.10,
}


class IntradaySignalGenerator(BaseSignalGenerator):
    """Stage 2 — intraday signal track (6 sub-signals)."""

    @property
    def track(self) -> str:
        return "intraday"

    @property
    def generator_version(self) -> str:
        return "1.0"

    def _score(
        self,
        features: dict[str, Any],
        regime: str,
    ) -> tuple[list[ContributingSignal], float | None]:
        sub_scores: dict[str, float | None] = {
            "premarket_gap": self._premarket_gap_score(features),
            "opening_range": self._opening_range_score(features),
            "fo_signals": self._fo_signals_score(features),
            "news_trade_decay": self._news_trade_decay_score(features),
            "index_correlation": self._index_correlation_score(features),
            "bid_ask_quality": self._bid_ask_quality_score(features),
        }

        contributors: list[ContributingSignal] = []
        raw_score = 0.0
        total_weight = 0.0

        for name, value in sub_scores.items():
            if value is None:
                continue
            w = _WEIGHTS[name]
            contribution = w * value
            raw_score += contribution
            total_weight += w
            contributors.append(
                ContributingSignal(name=name, weight=w, value=value, contribution=contribution)
            )

        if total_weight == 0:
            return [], None

        if total_weight < 1.0:
            raw_score = raw_score / total_weight

        return contributors, self._clip(raw_score, -1.0, 1.0)

    def _premarket_gap_score(self, features: dict[str, Any]) -> float | None:
        """Pre-market gap (%) adjusted for overnight news context."""
        gap_pct = features.get("premarket_gap_pct")
        if gap_pct is None:
            return None
        # Large gaps (>2.5%) are unreliable — design specifies skip
        gap = float(gap_pct)
        if abs(gap) > 2.5:
            return 0.0
        news_context = float(features.get("overnight_news_sentiment", 0.0))
        gap_score = self._clip(gap / 2.5, -1.0, 1.0)
        return 0.7 * gap_score + 0.3 * self._clip(news_context, -1.0, 1.0)

    def _opening_range_score(self, features: dict[str, Any]) -> float | None:
        """Opening range (first 15 min) vs 20-day average."""
        orb_ratio = features.get("orb_range_vs_20d_avg_ratio")
        if orb_ratio is None:
            return None
        # Tight ORB relative to average = easier to trade
        return self._clip(1.0 - float(orb_ratio), -1.0, 1.0)

    def _fo_signals_score(self, features: dict[str, Any]) -> float | None:
        """Overnight OI build-up and max pain proximity."""
        oi_change_pct = features.get("fo_oi_overnight_change_pct")
        if oi_change_pct is None:
            return None
        max_pain_proximity = float(features.get("fo_max_pain_proximity_pct", 0.0))
        oi_score = self._clip(float(oi_change_pct) / 10.0, -1.0, 1.0)
        # Close to max pain → bearish for momentum continuation
        pain_penalty = (
            -self._clip(abs(max_pain_proximity) / 2.0, 0.0, 1.0) if max_pain_proximity < 0 else 0.0
        )
        return 0.7 * oi_score + 0.3 * pain_penalty

    def _news_trade_decay_score(self, features: dict[str, Any]) -> float | None:
        """Freshness of latest news — more recent = higher score."""
        minutes_since_news = features.get("minutes_since_latest_news")
        if minutes_since_news is None:
            return 0.0
        mins = float(minutes_since_news)
        if mins > 120:
            return 0.0
        return self._clip(1.0 - (mins / 120.0), 0.0, 1.0)

    def _index_correlation_score(self, features: dict[str, Any]) -> float | None:
        """Beta-adjusted entry: only score when index confirms direction."""
        index_direction = features.get("nifty_intraday_direction")
        beta = float(features.get("beta_20d", 1.0))
        if index_direction is None:
            return None
        return self._clip(float(index_direction) * min(beta, 2.0) / 2.0, -1.0, 1.0)

    def _bid_ask_quality_score(self, features: dict[str, Any]) -> float | None:
        """Tight spreads → better fills → higher score."""
        spread_pct = features.get("bid_ask_spread_pct")
        if spread_pct is None:
            return None
        # Spread > 0.5% = poor quality (score 0); spread 0% = score 1
        return self._clip(1.0 - (float(spread_pct) / 0.5), 0.0, 1.0)
