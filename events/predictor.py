"""
Event impact prediction.

Uses historical event-price correlations to predict the likely
price impact of a future event.

Approach: simple lookup of (event_type, industry) → expected_change_pct
based on past observed correlations. Falls back to impact_level-based
heuristic if no historical data available.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from loguru import logger
from sqlalchemy import desc

from storage import get_db
from storage.models import IndustryEvent


# Heuristic defaults by event_type (fallback when no historical data)
TYPE_HEURISTIC: dict[str, float] = {
    "launch": 1.5,
    "earnings": 0.8,
    "m&a": 2.0,
    "capacity": 0.5,
    "price_change": 1.2,
    "regulatory": 1.5,
    "policy": 1.0,
    "contract": 0.6,
    "conference": 0.2,
    "data_release": 0.3,
    "product_launch": 0.7,
    "other": 0.3,
    "macro": 0.3,
}


@dataclass
class ImpactPrediction:
    """Predicted price impact of a future event."""
    event_id: int
    event_title: str
    event_type: str
    industry: str
    industry_label: str
    event_date: date
    impact_level: int
    predicted_change_pct: float
    confidence: str  # 'high' / 'medium' / 'low'
    basis: str  # 'historical' / 'heuristic' / 'mixed'
    sample_size: int = 0
    related_stocks: list[str] = None

    def __post_init__(self):
        if self.related_stocks is None:
            self.related_stocks = []


def predict_impact(event: IndustryEvent) -> ImpactPrediction:
    """Predict price impact for a single event."""
    db = get_db()

    # Look for historical (event_type, industry) correlations
    avg_change = None
    sample_size = 0

    with db.session() as s:
        from events.backtest import EventBacktest
        # Find past events with same type+industry
        past_events = (
            s.query(IndustryEvent)
            .filter(
                IndustryEvent.is_future == False,
                IndustryEvent.event_type == event.event_type,
                IndustryEvent.industry == event.industry,
                IndustryEvent.impact_level >= 2,
            )
            .order_by(desc(IndustryEvent.event_date))
            .limit(50)
            .all()
        )

        # Compute changes for each
        from events.backtest import backtest_event
        changes: list[float] = []
        for pe in past_events:
            try:
                bt = backtest_event(s, pe)
                if bt and bt.price_change_pct is not None:
                    changes.append(bt.price_change_pct)
            except Exception:
                continue

        if len(changes) >= 3:
            from statistics import mean
            avg_change = mean(changes)
            sample_size = len(changes)
            basis = "historical"
        else:
            basis = "heuristic"

    # Heuristic fallback
    if avg_change is None:
        base = TYPE_HEURISTIC.get(event.event_type, 0.5)
        avg_change = base * (event.impact_level / 3.0)

    # Confidence
    if sample_size >= 10:
        confidence = "high"
    elif sample_size >= 5:
        confidence = "medium"
    elif sample_size >= 3:
        confidence = "low"
    else:
        confidence = "very_low"

    # Cap predicted change to reasonable range
    avg_change = max(-15.0, min(15.0, avg_change))

    codes = [c.strip() for c in (event.related_stocks or "").split(",") if c.strip()]

    return ImpactPrediction(
        event_id=event.id,
        event_title=event.title,
        event_type=event.event_type,
        industry=event.industry,
        industry_label=event.industry_label,
        event_date=event.event_date,
        impact_level=event.impact_level,
        predicted_change_pct=avg_change,
        confidence=confidence,
        basis=basis,
        sample_size=sample_size,
        related_stocks=codes,
    )


def predict_upcoming(days_ahead: int = 30, min_impact: int = 3) -> list[ImpactPrediction]:
    """Predict impact for all upcoming events."""
    from datetime import timedelta
    from events import get_upcoming

    db = get_db()
    today = date.today()
    end = today + timedelta(days=days_ahead)

    events = get_upcoming(days_ahead=days_ahead, min_impact=min_impact)
    predictions = []
    for ev in events:
        try:
            pred = predict_impact(ev)
            predictions.append(pred)
        except Exception as e:
            logger.warning(f"Predict event {ev.id} failed: {e}")

    predictions.sort(key=lambda p: abs(p.predicted_change_pct), reverse=True)
    return predictions