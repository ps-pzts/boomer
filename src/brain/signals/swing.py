from __future__ import annotations

from typing import Any

from brain.models import ContributingSignal
from brain.signals.base import BaseSignalGenerator

_WEIGHTS: dict[str, float] = {
    "catalyst_proximity": 0.20,
    "technical_setup": 0.25,
    "volume_confirmation": 0.20,
    "sector_momentum": 0.15,
    "news_flow": 0.10,
    "mean_rev_momentum": 0.10,
}


class SwingSignalGenerator(BaseSignalGenerator):
    """Stage 2 — swing signal track (6 sub-signals)."""

    @property
    def track(self) -> str:
        return "swing"

    @property
    def generator_version(self) -> str:
        return "1.0"

    def _score(
        self,
        features: dict[str, Any],
        regime: str,
    ) -> tuple[list[ContributingSignal], float | None]:
        sub_scores: dict[str, float | None] = {
            "catalyst_proximity": self._catalyst_proximity_score(features),
            "technical_setup": self._technical_setup_score(features),
            "volume_confirmation": self._volume_confirmation_score(features),
            "sector_momentum": self._sector_momentum_score(features),
            "news_flow": self._news_flow_score(features),
            "mean_rev_momentum": self._mean_rev_momentum_score(features),
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

    def _catalyst_proximity_score(self, features: dict[str, Any]) -> float | None:
        """Score proximity to known catalyst (results, ex-div, regulatory event)."""
        days_to_catalyst = features.get("days_to_next_catalyst")
        if days_to_catalyst is None:
            return 0.0
        days = float(days_to_catalyst)
        if days < 0:
            return 0.0
        # Max score when <7 days out, decaying to 0 at 30 days
        return self._clip(1.0 - (days / 30.0), 0.0, 1.0)

    def _technical_setup_score(self, features: dict[str, Any]) -> float | None:
        """Score chart pattern quality (flag/pennant/base breakout)."""
        pattern_score = features.get("technical_pattern_score")
        if pattern_score is None:
            return None
        return self._clip(float(pattern_score), -1.0, 1.0)

    def _volume_confirmation_score(self, features: dict[str, Any]) -> float | None:
        """Z-score of recent volume vs 20-day baseline."""
        volume_zscore = features.get("volume_zscore_5d")
        if volume_zscore is None:
            return None
        # Normalise: z-score of 2 → score of 1.0
        return self._clip(float(volume_zscore) / 2.0, -1.0, 1.0)

    def _sector_momentum_score(self, features: dict[str, Any]) -> float | None:
        """Sector index relative strength."""
        sector_rs = features.get("sector_relative_strength_20d")
        if sector_rs is None:
            return None
        return self._clip(float(sector_rs), -1.0, 1.0)

    def _news_flow_score(self, features: dict[str, Any]) -> float | None:
        """Count of filings/news last 7 days (positive flow = bullish for swing)."""
        news_count = features.get("filing_count_7d", 0.0)
        # 3+ filings = max score; 0 = neutral
        return self._clip(float(news_count) / 3.0, 0.0, 1.0)

    def _mean_rev_momentum_score(self, features: dict[str, Any]) -> float | None:
        """Classifier output: +1 momentum mode, -1 mean-reversion mode."""
        mode = features.get("price_mode_classifier")
        if mode is None:
            return 0.0
        return self._clip(float(mode), -1.0, 1.0)
