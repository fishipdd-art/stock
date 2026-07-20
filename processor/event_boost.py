"""
Event proximity boost.

Boosts signal/mismatch scores when an upcoming industry event is near.
Logic: signals whose related stocks have an upcoming high-impact event
       get a score boost proportional to event impact × proximity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from loguru import logger
from sqlalchemy.orm import Session

from storage import get_db
from storage.models import IndustryEvent, SignalStock


# Proximity factor table: days_until_event -> boost per impact_level
_PROXIMITY_FACTORS: list[tuple[int, float]] = [
    (3, 0.15),    # within 3 days: strongest boost
    (7, 0.08),    # within a week
    (14, 0.04),   # within two weeks
    (30, 0.02),   # within a month
]
MAX_BOOST = 0.5  # cap total boost at +50%


@dataclass
class EventBoost:
    """Result of computing event boost for a signal/stock cluster."""
    boost_factor: float = 0.0
    matched_events: list = field(default_factory=list)

    @property
    def has_boost(self) -> bool:
        return self.boost_factor > 0

    def summary(self) -> str:
        if not self.matched_events:
            return ""
        parts = []
        for ev in self.matched_events[:3]:
            parts.append(f"{ev.title}({ev.impact_level}⭐)")
        return " · ".join(parts)


def _proximity_factor(days: int) -> float:
    if days < 0:
        return 0.0
    for threshold, factor in _PROXIMITY_FACTORS:
        if days <= threshold:
            return factor
    return 0.0


def get_upcoming_events_for_stocks(
    stock_codes: Iterable[str], days_ahead: int = 30, min_impact: int = 1
) -> list[IndustryEvent]:
    """Find upcoming events that match any of the given A-share codes."""
    codes = [c for c in stock_codes if c]
    if not codes:
        return []
    db = get_db()
    today = date.today()
    end = today + timedelta(days=days_ahead)
    with db.session() as s:
        rows = s.query(IndustryEvent).filter(
            IndustryEvent.is_future == True,
            IndustryEvent.event_date >= today,
            IndustryEvent.event_date <= end,
            IndustryEvent.impact_level >= min_impact,
        ).all()
        out = []
        for ev in rows:
            ev_stocks = set((ev.related_stocks or "").split(","))
            ev_stocks = {x.strip() for x in ev_stocks if x.strip()}
            if ev_stocks & set(codes):
                out.append(ev)
        return out


def compute_event_boost(stock_codes: Iterable[str], today: date | None = None) -> EventBoost:
    """Compute event boost for a set of stock codes."""
    today = today or date.today()
    events = get_upcoming_events_for_stocks(stock_codes, days_ahead=30, min_impact=3)
    if not events:
        return EventBoost(0.0, [])
    total = 0.0
    for ev in events:
        days = (ev.event_date - today).days
        pf = _proximity_factor(days)
        if pf == 0:
            continue
        total += ev.impact_level * pf
    boost = min(MAX_BOOST, total)
    return EventBoost(boost, events)


def get_stock_codes_for_signal(session: Session, signal_id: int) -> list[str]:
    """All A-share codes linked to a knowledge signal."""
    rows = (
        session.query(SignalStock.stock_code)
        .filter(SignalStock.signal_id == signal_id)
        .all()
    )
    return [r[0] for r in rows if r[0]]
