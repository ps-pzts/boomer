"""
NSE equity market holiday calendar.

`NSE_HOLIDAYS` is a frozenset of dates on which NSE equity markets are closed.
All other Mon–Fri dates are regular trading days.

IMPORTANT — VERIFY BEFORE RELYING ON BACKTESTS:
  Lunar-calendar holidays (Holi, Diwali, Eid, Muharram, etc.) shift year to year.
  Before running any backtest, cross-check this list against the official NSE holiday
  calendar at: https://www.nseindia.com/products-services/equity-market-timings-holidays

  Dates marked "# verify" are best-effort estimates; confirm from the official source.
  Fixed-date holidays (Republic Day, Independence Day, Gandhi Jayanti, Christmas,
  Dr. Ambedkar Jayanti) are always correct.

Usage:
    from market_calendar.nse_holidays import is_trading_day, trading_days

    if is_trading_day(date(2024, 10, 2)):
        ...  # False — Gandhi Jayanti

    for d in trading_days(date(2024, 1, 1), date(2024, 1, 31)):
        ...  # yields each trading day in January 2024
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

# Weekday-only filter applied at the end — weekend dates are already non-trading so
# including them in the set would be misleading and the integrity test would flag them.
_RAW_NSE_HOLIDAYS: list[date] = [
    # ── 2020 ──────────────────────────────────────────────────────────────────
    date(2020, 1, 26),   # Republic Day
    date(2020, 2, 21),   # Maha Shivratri
    date(2020, 3, 10),   # Holi
    date(2020, 4, 2),    # Ram Navami
    date(2020, 4, 6),    # Mahavir Jayanti
    date(2020, 4, 10),   # Good Friday
    date(2020, 4, 14),   # Dr. Ambedkar Jayanti
    date(2020, 5, 25),   # Id-ul-Fitr (Eid al-Fitr)  # verify
    date(2020, 8, 15),   # Independence Day
    date(2020, 10, 2),   # Gandhi Jayanti
    date(2020, 10, 25),  # Dussehra  # verify
    date(2020, 11, 16),  # Diwali Laxmi Puja  # verify — actual date: check NSE calendar
    date(2020, 11, 30),  # Gurunanak Jayanti
    date(2020, 12, 25),  # Christmas

    # ── 2021 ──────────────────────────────────────────────────────────────────
    date(2021, 1, 26),   # Republic Day
    date(2021, 3, 11),   # Maha Shivratri
    date(2021, 3, 29),   # Holi
    date(2021, 4, 2),    # Good Friday
    date(2021, 4, 13),   # Gudi Padwa / Ugadi  # verify
    date(2021, 4, 14),   # Dr. Ambedkar Jayanti
    date(2021, 4, 21),   # Ram Navami  # verify
    date(2021, 5, 13),   # Id-ul-Fitr  # verify
    date(2021, 7, 21),   # Bakri Id (Eid ul-Adha)  # verify
    date(2021, 8, 15),   # Independence Day
    date(2021, 8, 19),   # Muharram  # verify
    date(2021, 10, 2),   # Gandhi Jayanti
    date(2021, 10, 15),  # Dussehra  # verify
    date(2021, 11, 4),   # Diwali Laxmi Puja  # verify
    date(2021, 11, 5),   # Diwali Balipratipada  # verify
    date(2021, 11, 19),  # Gurunanak Jayanti
    date(2021, 12, 25),  # Christmas

    # ── 2022 ──────────────────────────────────────────────────────────────────
    date(2022, 1, 26),   # Republic Day
    date(2022, 3, 1),    # Maha Shivratri
    date(2022, 3, 18),   # Holi
    date(2022, 4, 14),   # Dr. Ambedkar Jayanti
    date(2022, 4, 15),   # Good Friday
    date(2022, 5, 3),    # Id-ul-Fitr  # verify
    date(2022, 7, 10),   # Bakri Id (Eid ul-Adha)  # verify
    date(2022, 8, 9),    # Muharram  # verify
    date(2022, 8, 15),   # Independence Day
    date(2022, 10, 2),   # Gandhi Jayanti
    date(2022, 10, 5),   # Dussehra  # verify
    date(2022, 10, 24),  # Diwali Laxmi Puja  # verify
    date(2022, 10, 26),  # Diwali Balipratipada  # verify
    date(2022, 11, 8),   # Gurunanak Jayanti
    date(2022, 12, 25),  # Christmas

    # ── 2023 ──────────────────────────────────────────────────────────────────
    date(2023, 1, 26),   # Republic Day
    date(2023, 2, 18),   # Maha Shivratri
    date(2023, 3, 7),    # Holi
    date(2023, 3, 30),   # Ram Navami  # verify
    date(2023, 4, 4),    # Mahavir Jayanti
    date(2023, 4, 7),    # Good Friday
    date(2023, 4, 14),   # Dr. Ambedkar Jayanti
    date(2023, 5, 5),    # Buddha Purnima  # verify
    date(2023, 6, 29),   # Bakri Id (Eid ul-Adha)  # verify
    date(2023, 8, 15),   # Independence Day
    date(2023, 10, 2),   # Gandhi Jayanti
    date(2023, 10, 24),  # Dussehra  # verify
    date(2023, 11, 13),  # Diwali Laxmi Puja (Nov 12 = Sunday, observed Monday)  # verify
    date(2023, 11, 14),  # Diwali Balipratipada  # verify
    date(2023, 11, 27),  # Gurunanak Jayanti
    date(2023, 12, 25),  # Christmas

    # ── 2024 ──────────────────────────────────────────────────────────────────
    date(2024, 1, 22),   # Ram Mandir Consecration (one-time special holiday)
    date(2024, 1, 26),   # Republic Day
    date(2024, 3, 25),   # Holi
    date(2024, 3, 29),   # Good Friday
    date(2024, 4, 9),    # Gudi Padwa / Ugadi  # verify
    date(2024, 4, 11),   # Id-ul-Fitr
    date(2024, 4, 14),   # Dr. Ambedkar Jayanti
    date(2024, 4, 17),   # Ram Navami
    date(2024, 4, 21),   # Mahavir Jayanti
    date(2024, 6, 17),   # Bakri Id (Eid ul-Adha)
    date(2024, 7, 17),   # Muharram
    date(2024, 8, 15),   # Independence Day
    date(2024, 10, 2),   # Gandhi Jayanti
    date(2024, 11, 1),   # Diwali Laxmi Puja
    date(2024, 11, 15),  # Gurunanak Jayanti
    date(2024, 12, 25),  # Christmas

    # ── 2025 ──────────────────────────────────────────────────────────────────
    date(2025, 1, 26),   # Republic Day
    date(2025, 2, 26),   # Maha Shivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-ul-Fitr (Eid al-Fitr)  # verify
    date(2025, 4, 10),   # Ram Navami / Mahavir Jayanti  # verify exact date
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 12),   # Buddha Purnima  # verify
    date(2025, 6, 7),    # Bakri Id (Eid ul-Adha)  # verify
    date(2025, 8, 15),   # Independence Day
    date(2025, 10, 2),   # Gandhi Jayanti / Dussehra  # verify if same day
    date(2025, 10, 20),  # Diwali Laxmi Puja  # verify — Diwali 2025 ~Oct 20
    date(2025, 10, 21),  # Diwali Balipratipada  # verify
    date(2025, 11, 5),   # Gurunanak Jayanti  # verify
    date(2025, 12, 25),  # Christmas

    # ── 2026 ──────────────────────────────────────────────────────────────────
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 17),   # Maha Shivratri  # verify
    date(2026, 3, 4),    # Holi  # verify
    date(2026, 3, 20),   # Id-ul-Fitr  # verify
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 27),   # Bakri Id (Eid ul-Adha)  # verify
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 19),  # Dussehra  # verify
    date(2026, 11, 8),   # Diwali Laxmi Puja  # verify — Diwali 2026 ~Nov 8
    date(2026, 12, 25),  # Christmas

    # ── 2027 ──────────────────────────────────────────────────────────────────
    date(2027, 1, 26),   # Republic Day
    date(2027, 3, 22),   # Holi  # verify
    date(2027, 3, 26),   # Good Friday
    date(2027, 4, 14),   # Dr. Ambedkar Jayanti
    date(2027, 8, 15),   # Independence Day
    date(2027, 10, 2),   # Gandhi Jayanti
    date(2027, 10, 29),  # Diwali Laxmi Puja  # verify — Diwali 2027 ~Oct 29
    date(2027, 12, 25),  # Christmas
]

# Strip dates that fall on weekends: they are already non-trading days and
# including them would cause false signals in holiday-detection logic.
NSE_HOLIDAYS: frozenset[date] = frozenset(d for d in _RAW_NSE_HOLIDAYS if d.weekday() < 5)


def is_trading_day(d: date) -> bool:
    """Return True if NSE equity markets are open on this date."""
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def trading_days(start: date, end: date) -> Iterator[date]:
    """Yield every NSE trading day in [start, end] inclusive."""
    current = start
    while current <= end:
        if is_trading_day(current):
            yield current
        current += timedelta(days=1)


def next_trading_day(d: date) -> date:
    """Return the first trading day strictly after d."""
    candidate = d + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def prev_trading_day(d: date) -> date:
    """Return the last trading day strictly before d."""
    candidate = d - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def trading_days_between(start: date, end: date) -> int:
    """Count trading days in [start, end] inclusive."""
    return sum(1 for _ in trading_days(start, end))
