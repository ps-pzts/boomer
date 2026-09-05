"""Feature computer runner — executes a track's registry for one stock/date.

Usage::

    from brain.features.track_intraday import INTRADAY_COMPUTERS
    results = run_track_computers(INTRADAY_COMPUTERS, db_path, fs, "TCS", "NSE", today)
    # results: {"dma_20,high_20d,...": True, "fo_oi_overnight_change_pct,...": False, ...}
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from brain.feature_store import FeatureStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeatureComputer:
    """Descriptor for one feature computation unit.

    Fields
    ------
    fn : Callable | None
        Compute function with signature
        ``(db_path, fs, symbol, exchange, as_of_date) -> None``.
        ``None`` for live-only features — the intraday task runner injects
        those directly into the features dict at signal time.
    writes : tuple[str, ...]
        Feature keys this computer writes to the FeatureStore.
        Shown in log output so you can trace which computer produced a feature.
    essential : bool
        If ``True`` (default), an exception in ``fn`` propagates and aborts
        the entire batch for this stock.  If ``False``, the failure is logged
        as a warning and the run continues — the signal generator will receive
        ``None`` for the missing features and skip that sub-signal.
    requires_live : bool
        If ``True``, ``fn`` is ``None`` and the feature is injected at signal
        call time (broker live quote, minute-bar parquet, or wall clock).
        The runner skips these silently; they are listed in the registry purely
        for documentation and traceability.
    """

    fn: Callable | None
    writes: tuple[str, ...]
    essential: bool = True
    requires_live: bool = False


def run_track_computers(
    registry: list[FeatureComputer],
    db_path: str,
    fs: FeatureStore,
    stock_symbol: str,
    exchange: str,
    as_of_date: date,
) -> dict[str, bool]:
    """Run all batch computers in registry order for one stock on one date.

    Computers are executed in list order, so place dependencies before
    dependents (e.g. ``compute_price_features`` before
    ``compute_technical_pattern_score``).

    Live-only computers (``requires_live=True``) are skipped silently.
    Non-essential failures are logged as warnings; essential failures raise.
    All computers are idempotent — calling the same registry twice for the
    same stock/date is safe (FeatureStore supersedes old rows).

    Parameters
    ----------
    registry:     Ordered list of FeatureComputer descriptors for the track.
    db_path:      SQLite database path.
    fs:           FeatureStore instance for writing features.
    stock_symbol: NSE/BSE ticker (e.g. ``"TCS"``).
    exchange:     ``"NSE"`` or ``"BSE"``.
    as_of_date:   Point-in-time date for the computation.

    Returns
    -------
    Dict mapping the ``writes`` key string to ``True`` (success) or
    ``False`` (non-essential failure).  Live-only computers are omitted.
    """
    results: dict[str, bool] = {}

    for computer in registry:
        label = " | ".join(computer.writes) or "unknown"

        if computer.requires_live:
            log.debug("[%s] skip — live-only, injected at signal time", label)
            continue

        try:
            if computer.fn is not None:
                computer.fn(db_path, fs, stock_symbol, exchange, as_of_date)
            results[label] = True
            log.debug("[%s] ok — %s %s", label, stock_symbol, as_of_date)
        except Exception as exc:
            results[label] = False
            if computer.essential:
                log.error(
                    "[%s] ESSENTIAL FAILURE — %s %s: %s",
                    label, stock_symbol, as_of_date, exc,
                )
                raise
            log.warning(
                "[%s] non-essential failure — %s %s: %s",
                label, stock_symbol, as_of_date, exc,
            )

    return results
