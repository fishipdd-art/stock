"""Tests for events/collector.py relative-date generators.

Covers the bug where the FOMC dates list was hard-coded to 2026/2027
and would silently stop working in 2028.
"""
from __future__ import annotations

from datetime import date

import pytest

from events.collector import (
    _nth_weekday,
    _last_weekday,
    _fOMC_dates_in,
    _add_months,
    generate_macro_calendar,
)


class TestNthWeekday:
    @pytest.mark.parametrize("year,month,weekday,n,expected", [
        # 3rd Wednesday of Jan 2026 = 2026-01-21
        (2026, 1, 2, 3, date(2026, 1, 21)),
        # 1st Friday of Feb 2026 = 2026-02-06
        (2026, 2, 4, 1, date(2026, 2, 6)),
        # Last Friday (n=5) of Mar 2026 = 2026-03-27
        (2026, 3, 4, 5, date(2026, 3, 27)),
        # 2nd Monday of Apr 2026 = 2026-04-13
        (2026, 4, 0, 2, date(2026, 4, 13)),
        # 1st Sunday of May 2026 = 2026-05-03
        (2026, 5, 6, 1, date(2026, 5, 3)),
    ])
    def test_returns_expected(self, year, month, weekday, n, expected):
        assert _nth_weekday(year, month, weekday, n) == expected

    def test_n5_clamps_to_last_occurrence(self):
        # 5th Monday of Feb 2026 doesn't exist (only 4); should give 4th
        result = _nth_weekday(2026, 2, 0, 5)
        # 4th Monday of Feb 2026 = 2026-02-23
        assert result == date(2026, 2, 23)

    def test_year_rollover(self):
        # Should work for any year, not just 2026
        d = _nth_weekday(2030, 6, 2, 2)
        assert d.year == 2030
        assert d.month == 6
        # Verify it's actually a Wednesday
        assert d.weekday() == 2


class TestLastWeekday:
    def test_last_friday_of_august(self):
        # Last Friday of Aug 2026 = 2026-08-28
        assert _last_weekday(2026, 8, 4) == date(2026, 8, 28)

    def test_last_monday_of_december(self):
        # Last Monday of Dec 2026 = 2026-12-28
        assert _last_weekday(2026, 12, 0) == date(2026, 12, 28)

    def test_works_for_any_year(self):
        d = _last_weekday(2035, 1, 4)
        assert d.year == 2035
        assert d.weekday() == 4


class TestFOMCDates:
    """The key fix: FOMC dates are no longer hard-coded to 2026/2027."""

    def test_eight_dates_per_year(self):
        dates = _fOMC_dates_in(2026)
        assert len(dates) == 8

    def test_all_dates_are_wednesdays(self):
        for d in _fOMC_dates_in(2026):
            assert d.weekday() == 2, f"{d} is not a Wednesday"

    def test_scheduled_months(self):
        dates = _fOMC_dates_in(2026)
        months = {d.month for d in dates}
        # FOMC meets 8x/year, every other month
        assert months == {1, 3, 5, 6, 7, 9, 11, 12}

    def test_works_in_2030(self):
        """The original bug: hard-coded list stopped working in 2028."""
        dates = _fOMC_dates_in(2030)
        assert len(dates) == 8
        assert all(d.year == 2030 for d in dates)

    def test_works_in_2025_and_2035(self):
        # Both future and past
        assert len(_fOMC_dates_in(2025)) == 8
        assert len(_fOMC_dates_in(2035)) == 8


class TestAddMonths:
    def test_simple(self):
        assert _add_months(date(2026, 1, 15), 1) == date(2026, 2, 15)
        assert _add_months(date(2026, 1, 15), 12) == date(2027, 1, 15)

    def test_year_rollover(self):
        assert _add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)

    def test_clamp_to_last_day(self):
        # Jan 31 + 1 month = Feb 28 (or 29)
        assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        # Leap year
        assert _add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)


class TestGenerateMacroCalendar:
    def test_includes_fomc_in_window(self):
        events = generate_macro_calendar(date(2026, 3, 1), date(2026, 3, 31))
        fomc = [e for e in events if "FOMC" in e["title"]]
        assert len(fomc) == 1
        # 3rd Wed of March 2026 = 2026-03-18
        assert fomc[0]["event_date"] == date(2026, 3, 18)

    def test_works_in_2030(self):
        """Originally this would return zero FOMC events in 2030."""
        events = generate_macro_calendar(date(2030, 1, 1), date(2030, 12, 31))
        fomc = [e for e in events if "FOMC" in e["title"]]
        assert len(fomc) == 8, f"expected 8 FOMC events, got {len(fomc)}"

    def test_includes_pbc_lpr(self):
        # PBOC LPR is on the 20th of every month
        events = generate_macro_calendar(date(2026, 5, 1), date(2026, 5, 31))
        lpr = [e for e in events if "LPR" in e["title"]]
        assert len(lpr) == 1
        assert lpr[0]["event_date"] == date(2026, 5, 20)

    def test_includes_jackson_hole(self):
        events = generate_macro_calendar(date(2026, 8, 1), date(2026, 8, 31))
        jh = [e for e in events if "Jackson Hole" in e["title"]]
        assert len(jh) == 1
        # Last Friday of Aug 2026 = 2026-08-28
        assert jh[0]["event_date"] == date(2026, 8, 28)

    def test_includes_nfp(self):
        # NFP = first Friday of month
        events = generate_macro_calendar(date(2026, 6, 1), date(2026, 6, 30))
        nfp = [e for e in events if "非农" in e["title"]]
        assert len(nfp) == 1
        # 1st Friday of June 2026 = 2026-06-05
        assert nfp[0]["event_date"] == date(2026, 6, 5)

    def test_window_filtering(self):
        # 1-day window should yield a small set
        events = generate_macro_calendar(date(2026, 5, 20), date(2026, 5, 20))
        # PBOC LPR fires on the 20th
        lpr = [e for e in events if "LPR" in e["title"]]
        assert len(lpr) == 1
        # And nothing from the 21st onwards
        later = [e for e in events if e["event_date"] > date(2026, 5, 20)]
        assert later == []
