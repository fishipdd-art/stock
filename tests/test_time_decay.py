"""Tests for processor/time_decay.py.

The exponential-decay weighting is the spine of the news/signal scoring
engine. We verify monotonicity, boundary values, and that the function
clamps at the keep_days horizon.
"""
from __future__ import annotations

import math
from datetime import datetime, date, timedelta

import pytest

from processor.time_decay import (
    age_days,
    weight_for_age,
    weight_for_datetime,
    filter_recent,
)


class TestAgeDays:
    def test_zero_age(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        assert age_days(now, now=now) == 0.0

    def test_one_day(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        older = now - timedelta(days=1)
        assert abs(age_days(older, now=now) - 1.0) < 1e-6

    def test_future_clamps_to_zero(self):
        now = datetime(2026, 1, 1)
        future = datetime(2026, 1, 5)
        assert age_days(future, now=now) == 0.0

    def test_date_input_converted(self):
        now = datetime(2026, 1, 1)
        d = date(2025, 12, 31)
        assert abs(age_days(d, now=now) - 1.0) < 1e-6


class TestWeightForAge:
    def test_day0_is_one(self):
        assert weight_for_age(0.0) == 1.0

    def test_monotonically_decreasing(self):
        weights = [weight_for_age(d) for d in [0, 1, 2, 3, 5, 7]]
        for a, b in zip(weights, weights[1:]):
            assert a > b, f"weights not strictly decreasing: {weights}"

    def test_clamps_beyond_keep_days(self):
        # Default news_keep_days=7, so anything older returns 0
        assert weight_for_age(100.0) == 0.0
        assert weight_for_age(8.0) == 0.0

    def test_at_keep_days_boundary(self):
        # At exactly keep_days we still return exp(-lam*7) ~ 0.085
        # (function returns 0 for age > keep_days, not >=).
        w = weight_for_age(7.0)
        assert w > 0
        assert w < 0.2

    def test_custom_lambda(self):
        # lambda=0 -> weight always 1 (when within keep_days)
        assert abs(weight_for_age(3.0, lam=0.0) - 1.0) < 1e-6
        # lambda=1 -> weight at day 1 = e^-1 ~ 0.368
        assert abs(weight_for_age(1.0, lam=1.0) - math.exp(-1)) < 1e-6


class TestFilterRecent:
    def test_keeps_only_fresh(self):
        now = datetime(2026, 1, 1)
        items = [
            type("X", (), {"dt": now - timedelta(days=1)}),
            type("X", (), {"dt": now - timedelta(days=10)}),  # too old
            type("X", (), {"dt": now - timedelta(hours=2)}),
        ]
        kept = filter_recent(items, dt_attr="dt", now=now)
        assert len(kept) == 2

    def test_skips_missing_attr(self):
        now = datetime(2026, 1, 1)
        items = [
            type("X", (), {"dt": now}),
            type("Y", (), {}),  # no dt
        ]
        kept = filter_recent(items, dt_attr="dt", now=now)
        assert len(kept) == 1
