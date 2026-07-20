"""
Time-weighted decay engine.

News/signals lose value as they age. We use exponential decay:
  weight = exp(-lambda * days_old)

Default lambda = 0.35 gives:
  day0 = 1.00, day1 = 0.70, day2 = 0.50, day3 = 0.35,
  day4 = 0.25, day5 = 0.17, day6 = 0.12, day7 = 0.08

News older than `news_keep_days` is discarded.
"""
from __future__ import annotations

import math
from datetime import datetime, date, timedelta
from typing import Iterable

from config.settings import settings


def age_days(published_at: datetime | date, now: datetime | None = None) -> float:
    """Compute age in days (float) between published_at and now."""
    now = now or datetime.utcnow()
    if isinstance(published_at, date) and not isinstance(published_at, datetime):
        published_at = datetime.combine(published_at, datetime.min.time())
    delta = now - published_at
    return max(0.0, delta.total_seconds() / 86400.0)


def weight_for_age(days_old: float, lam: float | None = None) -> float:
    """Exponential decay weight for `days_old` days old."""
    lam = lam if lam is not None else settings.time_decay_lambda
    if days_old > settings.news_keep_days:
        return 0.0
    return math.exp(-lam * days_old)


def weight_for_datetime(published_at: datetime, now: datetime | None = None) -> float:
    """Compute decay weight from a datetime."""
    return weight_for_age(age_days(published_at, now))


def filter_recent(
    items: Iterable, dt_attr: str = "published_at", now: datetime | None = None
) -> list:
    """Filter out items older than news_keep_days."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=settings.news_keep_days)
    out = []
    for item in items:
        dt = getattr(item, dt_attr, None)
        if dt is None:
            continue
        if isinstance(dt, date) and not isinstance(dt, datetime):
            dt = datetime.combine(dt, datetime.min.time())
        if dt >= cutoff:
            out.append(item)
    return out


def score_with_decay(
    items_with_dates: Iterable[tuple[object, datetime]],
    base_score_attr: str = "score",
    now: datetime | None = None,
) -> list[tuple[object, float]]:
    """Compute final score = base_score * decay_weight for each item.

    Returns list of (item, final_score) sorted descending by final_score.
    """
    now = now or datetime.utcnow()
    out = []
    for item, dt in items_with_dates:
        base = getattr(item, base_score_attr, 1.0) or 1.0
        w = weight_for_datetime(dt, now)
        out.append((item, base * w))
    out.sort(key=lambda x: x[1], reverse=True)
    return out