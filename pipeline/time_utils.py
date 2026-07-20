"""Business-date helpers for the Asia/Shanghai production calendar.

Database timestamps remain naive UTC for compatibility with the existing
schema.  Pipeline dependency decisions use an explicit business date instead
of comparing naive UTC timestamps with a local midnight.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def current_business_date(now: datetime | None = None) -> date:
    """Return the Asia/Shanghai calendar date for ``now``."""
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(SHANGHAI).date()


def parse_business_date(value: object, *, default_today: bool = True) -> date | None:
    """Parse a date-like value without silently accepting malformed input."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(SHANGHAI).date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if text:
        return date.fromisoformat(text)
    return current_business_date() if default_today else None


def utc_bounds_for_business_date(value: date) -> tuple[datetime, datetime]:
    """Return naive UTC [start, end) bounds for one Shanghai business date."""
    local_start = datetime.combine(value, time.min, tzinfo=SHANGHAI)
    local_end = datetime.combine(value.fromordinal(value.toordinal() + 1), time.min, tzinfo=SHANGHAI)
    return (
        local_start.astimezone(UTC).replace(tzinfo=None),
        local_end.astimezone(UTC).replace(tzinfo=None),
    )
