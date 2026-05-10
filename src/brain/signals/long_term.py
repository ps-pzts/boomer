from __future__ import annotations

from typing import Any

from brain.models import ContributingSignal
from brain.signals.base import BaseSignalGenerator

# Signal weights by regime (design doc Stage 2)
_WEIGHTS: dict[str, dict[str, float]] = {
    "bull_calm": {
        "promoter": 0.30, "smart_money": 0.20, "filing_sentiment": 0.15,
        "earnings_quality": 0.20, "valuation": 0.15,
    },
    "bull_volatile": {
        "promoter": 0.25, "smart_money": 0.20, "filing_sentiment": 0.20,
        "earnings_quality": 0.20, "valuation": 0.15,
    },
    "sideways": {
        "promoter": 0.35, "smart_money": 0.20, "filing_sentiment": 0.15,
        "earnings_quality": 0.20, "valuation": 0.10,
    },
    "bear": {
        "promoter": 0.40, "smart_money": 0.15, "filing_sentiment": 0.20,
        "earnings_quality": 0.20, "valuation": 0.05,
    },
}
_DEFAULT_WEIGHTS = _WEIGHTS["bear"]


class LongTermSignalGenerator(BaseSignalGenerator):
    """Stage 2 — long-term signal track (5 sub-signals)."""

    @property
    def track(self) -> str:
        return "long_term"

    @property
    def generator_version(self) -> str:
        return "1.0"

    def _score(
        self,
        features: dict[str, Any],
        regime: str,
    ) -> tuple[list[ContributingSignal], float | None]:
        weights = _WEIGHTS.get(regime, _DEFAULT_WEIGHTS)

        sub_scores: dict[str, float | None] = {
            "promoter": self._promoter_score(features),
            "smart_money": self._smart_money_score(features),
            "filing_sentiment": self._filing_sentiment_score(features),
            "earnings_quality": self._earnings_quality_score(features),
            "valuation": self._valuation_score(features),
        }

        contributors: list[ContributingSignal] = []
        raw_score = 0.0
        total_weight = 0.0

        for name, value in sub_scores.items():
            if value is None:
                continue
            w = weights[name]
            contribution = w * value
            raw_score += contribution
            total_weight += w
            contributors.append(
                ContributingSignal(name=name, weight=w, value=value, contribution=contribution)
            )

        if total_weight == 0:
            return [], None

        # Normalise to [-1, +1] if some sub-signals were absent
        if total_weight < 1.0:
            raw_score = raw_score / total_weight

        return contributors, self._clip(raw_score, -1.0, 1.0)

    def _promoter_score(self, features: dict[str, Any]) -> float | None:
        """Promoter activity score.
        Requires: promoter_holding_pct_change_90d, promoter_open_market_buy_count_90d,
                  promoter_pledge_pct_current.
        Returns None if promoter_holding_pct_change_90d is unavailable (shares_outstanding missing).
        """
        holding_change = features.get("promoter_holding_pct_change_90d")
        if holding_change is None:
            return None

        buy_count = features.get("promoter_open_market_buy_count_90d", 0.0)
        pledge_pct = features.get("promoter_pledge_pct_current", 0.0)

        holding_score = self._clip(float(holding_change) / 1.0, -1.0, 1.0)
        buy_intensity = self._clip(float(buy_count) / 3.0, 0.0, 1.0)
        pledge_penalty = -self._clip(float(pledge_pct) / 50.0, 0.0, 1.0)

        return 0.5 * holding_score + 0.3 * buy_intensity + 0.2 * pledge_penalty

    def _smart_money_score(self, features: dict[str, Any]) -> float | None:
        """Smart money (bulk deals) score."""
        net_buy_value = features.get("smart_money_net_buy_value_90d")
        if net_buy_value is None:
            return None

        buyer_count = features.get("smart_money_buyer_count_90d", 0.0)
        # ₹50 cr normaliser
        size_score = self._clip(float(net_buy_value) / 5_00_00_00_000, -1.0, 1.0)
        breadth_score = self._clip(float(buyer_count) / 3.0, 0.0, 1.0)

        return 0.7 * size_score + 0.3 * breadth_score

    def _filing_sentiment_score(self, features: dict[str, Any]) -> float | None:
        """Filing sentiment score with red-flag binary kill switch."""
        bullish = features.get("filing_bullish_count_90d")
        if bullish is None:
            return None

        bearish = float(features.get("filing_bearish_count_90d", 0.0))
        bullish = float(bullish)
        total = max(bullish + bearish, 1.0)
        net_score = (bullish - bearish) / total

        # Binary kill switch for red flags
        auditor_change = bool(features.get("has_auditor_change_90d", False))
        pledging_increase = bool(features.get("has_pledging_increase_90d", False))
        red_flag_penalty = -1.0 if (auditor_change or pledging_increase) else 0.0

        return 0.7 * net_score + 0.3 * red_flag_penalty

    def _earnings_quality_score(self, features: dict[str, Any]) -> float | None:
        """Earnings quality score.
        Returns None if fewer than 2 quarters of data are available (prevents zero-fill).
        """
        revenue_growth = features.get("revenue_growth_yoy_pct")
        if revenue_growth is None:
            return None
        opm_trend = features.get("opm_trend_4q")
        cfo_pat = features.get("cfo_pat_ratio_latest")
        if opm_trend is None or cfo_pat is None:
            return None

        revenue_score = self._clip((float(revenue_growth) - 10.0) / 30.0, -1.0, 1.0)
        margin_score = self._clip(float(opm_trend) * 5.0, -1.0, 1.0)
        cfo_score = self._clip((float(cfo_pat) - 0.7) / 0.5, -1.0, 1.0)

        return 0.4 * revenue_score + 0.3 * margin_score + 0.3 * cfo_score

    def _valuation_score(self, features: dict[str, Any]) -> float | None:
        """Valuation context score using 5-year PE percentile."""
        pe_pct = features.get("pe_percentile_5y")
        if pe_pct is None:
            return None
        return self._clip((50.0 - float(pe_pct)) / 50.0, -1.0, 1.0)
