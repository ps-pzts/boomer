from __future__ import annotations

from datetime import date

from market_calendar.nse_holidays import (
    NSE_HOLIDAYS,
    is_trading_day,
    next_trading_day,
    prev_trading_day,
    trading_days,
    trading_days_between,
)


class TestIsTradingDay:
    def test_saturday_is_not_a_trading_day(self):
        # 2024-01-06 is a Saturday
        assert not is_trading_day(date(2024, 1, 6))

    def test_sunday_is_not_a_trading_day(self):
        # 2024-01-07 is a Sunday
        assert not is_trading_day(date(2024, 1, 7))

    def test_republic_day_is_not_a_trading_day(self):
        # Jan 26 every year — fixed holiday
        assert not is_trading_day(date(2024, 1, 26))
        assert not is_trading_day(date(2023, 1, 26))
        assert not is_trading_day(date(2025, 1, 26))

    def test_independence_day_is_not_a_trading_day(self):
        assert not is_trading_day(date(2024, 8, 15))

    def test_gandhi_jayanti_is_not_a_trading_day(self):
        assert not is_trading_day(date(2024, 10, 2))

    def test_christmas_is_not_a_trading_day(self):
        assert not is_trading_day(date(2024, 12, 25))

    def test_diwali_2024_is_not_a_trading_day(self):
        assert not is_trading_day(date(2024, 11, 1))

    def test_regular_weekday_is_a_trading_day(self):
        # 2024-01-15 is a Monday, not a holiday
        assert is_trading_day(date(2024, 1, 15))

    def test_holiday_on_weekend_irrelevant(self):
        # If a holiday falls on a weekend it has no effect — that date is already non-trading
        # Republic Day 2021 was on Jan 26 (Tuesday) — closed
        assert not is_trading_day(date(2021, 1, 26))


class TestTradingDays:
    def test_yields_only_weekdays(self):
        days = list(trading_days(date(2024, 1, 1), date(2024, 1, 7)))
        assert all(d.weekday() < 5 for d in days)

    def test_excludes_nse_holidays(self):
        # Week containing Republic Day 2024 (Jan 22 special + Jan 26)
        days = list(trading_days(date(2024, 1, 22), date(2024, 1, 26)))
        assert date(2024, 1, 22) not in days  # Ram Mandir holiday
        assert date(2024, 1, 26) not in days  # Republic Day

    def test_empty_range_yields_nothing(self):
        days = list(trading_days(date(2024, 1, 26), date(2024, 1, 26)))
        assert days == []  # Republic Day — no trading days in that range

    def test_full_week_without_holidays_yields_five_days(self):
        # 2024-01-08 to 2024-01-12: Mon–Fri, no holidays
        days = list(trading_days(date(2024, 1, 8), date(2024, 1, 12)))
        assert len(days) == 5

    def test_start_after_end_yields_nothing(self):
        days = list(trading_days(date(2024, 2, 1), date(2024, 1, 1)))
        assert days == []


class TestNextPrevTradingDay:
    def test_next_after_friday_skips_weekend(self):
        # 2024-01-12 is Friday → next is Monday 2024-01-15
        assert next_trading_day(date(2024, 1, 12)) == date(2024, 1, 15)

    def test_next_skips_holiday(self):
        # 2024-01-25 is Thursday; next would be Jan 26 (Republic Day) → skip to Jan 29 (Monday)
        assert next_trading_day(date(2024, 1, 25)) == date(2024, 1, 29)

    def test_prev_from_monday_goes_to_friday(self):
        # 2024-01-15 is Monday → prev is Friday 2024-01-12
        assert prev_trading_day(date(2024, 1, 15)) == date(2024, 1, 12)

    def test_prev_skips_holiday(self):
        # 2024-01-29 is Monday; prev would check Jan 26 (Republic Day) → skip to Jan 25 (Thursday)
        assert prev_trading_day(date(2024, 1, 29)) == date(2024, 1, 25)


class TestTradingDaysBetween:
    def test_single_trading_day(self):
        # 2024-01-15 is a Monday trading day
        assert trading_days_between(date(2024, 1, 15), date(2024, 1, 15)) == 1

    def test_full_week(self):
        # 2024-01-08 to 2024-01-12, no holidays
        assert trading_days_between(date(2024, 1, 8), date(2024, 1, 12)) == 5

    def test_week_with_holiday(self):
        # 2024-01-22 to 2024-01-26: Mon has Ram Mandir, Fri has Republic Day — only 3 trading days
        count = trading_days_between(date(2024, 1, 22), date(2024, 1, 26))
        assert count == 3  # Tue, Wed, Thu


class TestHolidaySetIntegrity:
    def test_no_weekends_in_holiday_set(self):
        # Every holiday should be a weekday (Sat/Sun can't be "closed" if already non-trading)
        weekends = [d for d in NSE_HOLIDAYS if d.weekday() >= 5]
        assert weekends == [], f"Weekend dates in NSE_HOLIDAYS: {weekends}"

    def test_holiday_set_is_not_empty(self):
        assert len(NSE_HOLIDAYS) > 50  # expect 10-15 per year across 2020-2027

    def test_all_years_represented(self):
        years = {d.year for d in NSE_HOLIDAYS}
        for year in range(2020, 2028):
            assert year in years, f"No holidays found for year {year}"

    def test_fixed_date_holidays_all_years(self):
        """Republic Day, Independence Day, Gandhi Jayanti and Christmas must appear each year
        on the correct fixed date (unless that date is a weekend, in which case it's absent).
        """
        for year in range(2020, 2028):
            for month, day, name in [
                (1, 26, "Republic Day"),
                (8, 15, "Independence Day"),
                (10, 2, "Gandhi Jayanti"),
                (12, 25, "Christmas"),
            ]:
                d = date(year, month, day)
                if d.weekday() < 5:
                    assert d in NSE_HOLIDAYS, f"{name} {year} ({d}) missing from NSE_HOLIDAYS"
