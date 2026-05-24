"""Tests for KiteBroker.get_ltp() fallback chain and place_gtt() LTP guard.

Regression coverage for the silent bad-fallback bug:
  place_gtt() used `get_ltp() or sl_trigger_price` which caused Kite to reject
  the order with "Trigger cannot be created with one of the trigger values equal
  to the last price." when get_ltp() returned None.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.executor.brokers.kite_broker import KiteBroker
from src.executor.models import GttRequest, GttType

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def broker() -> KiteBroker:
    b = KiteBroker()
    b._kite = MagicMock()
    b._kite.GTT_TYPE_SINGLE = "single"
    b._kite.GTT_TYPE_OCO = "two-leg"
    return b


# ── get_ltp() fallback chain ──────────────────────────────────────────────────


class TestGetLtp:
    def test_tier1_rest_ltp_used_when_available(self, broker: KiteBroker) -> None:
        broker._kite.ltp.return_value = {"NSE:KPEL": {"last_price": 380.5}}
        assert broker.get_ltp("KPEL", "NSE") == 380.5

    def test_tier2_holdings_fallback_when_rest_fails(self, broker: KiteBroker) -> None:
        broker._kite.ltp.side_effect = Exception("Insufficient permission")
        broker._kite.holdings.return_value = [
            {"tradingsymbol": "KPEL", "last_price": 375.0},
        ]
        broker._kite.positions.return_value = {"day": []}
        assert broker.get_ltp("KPEL", "NSE") == 375.0

    def test_tier2_positions_fallback_when_rest_and_holdings_miss(
        self, broker: KiteBroker
    ) -> None:
        broker._kite.ltp.side_effect = Exception("Insufficient permission")
        broker._kite.holdings.return_value = []
        broker._kite.positions.return_value = {
            "day": [{"tradingsymbol": "KPEL", "last_price": 362.0}]
        }
        assert broker.get_ltp("KPEL", "NSE") == 362.0

    def test_tier3_stale_tick_cache_used_as_last_resort(self, broker: KiteBroker) -> None:
        broker._kite.ltp.side_effect = Exception("no data")
        broker._kite.holdings.return_value = []
        broker._kite.positions.return_value = {"day": []}
        broker._ltp["KPEL"] = 355.0  # stale but present
        assert broker.get_ltp("KPEL", "NSE") == 355.0

    def test_returns_none_when_all_tiers_exhausted(self, broker: KiteBroker) -> None:
        broker._kite.ltp.side_effect = Exception("no data")
        broker._kite.holdings.return_value = []
        broker._kite.positions.return_value = {"day": []}
        assert broker.get_ltp("KPEL", "NSE") is None

    def test_zero_last_price_in_holdings_is_skipped(self, broker: KiteBroker) -> None:
        """Holdings entries with last_price=0 (delisted / not yet priced) must be skipped."""
        broker._kite.ltp.side_effect = Exception("no data")
        broker._kite.holdings.return_value = [
            {"tradingsymbol": "KPEL", "last_price": 0},
        ]
        broker._kite.positions.return_value = {"day": []}
        assert broker.get_ltp("KPEL", "NSE") is None


# ── place_gtt() LTP guard ─────────────────────────────────────────────────────


class TestPlaceGttLtpGuard:
    def _oco_request(self) -> GttRequest:
        return GttRequest(
            symbol="KPEL", exchange="NSE",
            gtt_type=GttType.OCO, quantity=18,
            sl_trigger_price=338.03, sl_limit_price=337.36,
            target_trigger_price=439.46, target_limit_price=438.58,
        )

    def test_raises_when_ltp_unavailable(self, broker: KiteBroker) -> None:
        """place_gtt must raise RuntimeError — not silently use sl_trigger as last_price."""
        broker._kite.ltp.side_effect = Exception("Insufficient permission")
        broker._kite.holdings.return_value = []
        broker._kite.positions.return_value = {"day": []}

        with pytest.raises(RuntimeError, match="LTP unavailable"):
            broker.place_gtt(self._oco_request())

        broker._kite.place_gtt.assert_not_called()

    def test_oco_uses_ltp_from_rest(self, broker: KiteBroker) -> None:
        """place_gtt passes the real LTP — not the sl_trigger — to the Kite SDK."""
        broker._kite.ltp.return_value = {"NSE:KPEL": {"last_price": 360.0}}
        broker._kite.place_gtt.return_value = {"trigger_id": 12345}

        broker.place_gtt(self._oco_request())

        call_kwargs = broker._kite.place_gtt.call_args[1]
        assert call_kwargs["last_price"] == 360.0
        # Confirm the bad-fallback value (sl_trigger) was NOT used
        assert call_kwargs["last_price"] != 338.03

    def test_oco_uses_ltp_from_holdings_fallback(self, broker: KiteBroker) -> None:
        broker._kite.ltp.side_effect = Exception("no market data")
        broker._kite.holdings.return_value = [
            {"tradingsymbol": "KPEL", "last_price": 360.0}
        ]
        broker._kite.positions.return_value = {"day": []}
        broker._kite.place_gtt.return_value = {"trigger_id": 12345}

        broker.place_gtt(self._oco_request())

        assert broker._kite.place_gtt.call_args[1]["last_price"] == 360.0

    def test_single_gtt_raises_when_ltp_unavailable(self, broker: KiteBroker) -> None:
        broker._kite.ltp.side_effect = Exception("no market data")
        broker._kite.holdings.return_value = []
        broker._kite.positions.return_value = {"day": []}

        req = GttRequest(
            symbol="KPEL", exchange="NSE",
            gtt_type=GttType.SINGLE, quantity=18,
            trigger_price=338.03, limit_price=337.36,
        )
        with pytest.raises(RuntimeError, match="LTP unavailable"):
            broker.place_gtt(req)
