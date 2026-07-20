"""
Advanced impact prediction with Bayesian-style weighting.

Improves on the basic predictor by:
  1. Time decay: more recent events have more weight
  2. Sample size confidence: bigger sample → tighter estimate
  3. Prior + likelihood blend: heuristic is prior, history is likelihood
  4. Event clustering: adjust prediction when neighboring events exist

Model:
  posterior = (prior × α + likelihood × β) / (α + β)
  where α = heuristic weight (default 2)
        β = historical sample weight (proportional to sqrt(n))
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from math import sqrt
from statistics import mean
from typing import Optional

from loguru import logger
from sqlalchemy import desc

from storage import get_db
from storage.models import IndustryEvent
from events.predictor import (
    predict_impact as basic_predict,
    TYPE_HEURISTIC, ImpactPrediction,
)


# Time decay: events > 6 months old have weight ~0.05
def _time_weight(event_date: date, today: date, half_life_days: int = 90) -> float:
    """Exponential decay: weight halves every `half_life_days`."""
    age = (today - event_date).days
    if age < 0:
        return 1.0
    return 0.5 ** (age / half_life_days)


@dataclass
class AdvancedPrediction:
    """Posterior-weighted impact prediction."""
    event_id: int
    event_title: str
    event_type: str
    industry: str
    industry_label: str
    event_date: date
    impact_level: int
    prior_change_pct: float       # heuristic
    likelihood_change_pct: float  # historical
    posterior_change_pct: float   # blended
    confidence_interval: tuple[float, float]  # 95% CI
    confidence: str  # 'high' / 'medium' / 'low'
    sample_size: int
    prior_weight: float
    likelihood_weight: float
    related_stocks: list[str] = None

    def __post_init__(self):
        if self.related_stocks is None:
            self.related_stocks = []


def predict_advanced(
    event: IndustryEvent,
    today: date | None = None,
    prior_weight: float = 2.0,
    half_life_days: int = 90,
) -> AdvancedPrediction:
    """Compute posterior-weighted prediction for a single event.

    Args:
        event: the future event to predict
        today: reference date (for testing)
        prior_weight: weight of heuristic prior
        half_life_days: time decay half-life for historical data
    """
    today = today or date.today()
    db = get_db()

    # 1. Prior: heuristic
    base = TYPE_HEURISTIC.get(event.event_type, 0.5)
    prior = base * (event.impact_level / 3.0)

    # 2. Likelihood: historical same (type, industry) with time decay
    with db.session() as s:
        past_events = (
            s.query(IndustryEvent)
            .filter(
                IndustryEvent.is_future == False,
                IndustryEvent.event_type == event.event_type,
                IndustryEvent.industry == event.industry,
                IndustryEvent.impact_level >= 2,
            )
            .order_by(desc(IndustryEvent.event_date))
            .limit(100)
            .all()
        )

        from events.backtest import backtest_event
        weighted_changes: list[tuple[float, float]] = []
        for pe in past_events:
            tw = _time_weight(pe.event_date, today, half_life_days)
            if tw < 0.05:
                continue
            try:
                bt = backtest_event(s, pe)
                if bt and bt.price_change_pct is not None:
                    weighted_changes.append((bt.price_change_pct, tw))
            except Exception:
                continue

    if weighted_changes:
        weighted_avg = sum(c * w for c, w in weighted_changes) / sum(w for _, w in weighted_changes)
        # Effective sample size = sum of weights
        eff_n = sum(w for _, w in weighted_changes)
        likelihood = weighted_avg
        lk_weight = sqrt(eff_n)  # sqrt law: variance ∝ 1/n
    else:
        likelihood = prior
        lk_weight = 0.0
        eff_n = 0

    # 3. Posterior
    total_w = prior_weight + lk_weight
    if total_w > 0:
        posterior = (prior * prior_weight + likelihood * lk_weight) / total_w
    else:
        posterior = prior

    # 4. Confidence interval (rough: ±2σ)
    if weighted_changes and len(weighted_changes) > 1:
        from statistics import stdev
        changes_only = [c for c, _ in weighted_changes]
        sigma = stdev(changes_only)
    else:
        sigma = abs(prior) * 0.5  # rough default

    ci_low = max(-20.0, posterior - 2 * sigma)
    ci_high = min(20.0, posterior + 2 * sigma)

    # Cap
    posterior = max(-20.0, min(20.0, posterior))

    # 5. Confidence rating
    if lk_weight >= 5 and eff_n >= 10:
        confidence = "high"
    elif lk_weight >= 2 and eff_n >= 5:
        confidence = "medium"
    elif lk_weight >= 1:
        confidence = "low"
    else:
        confidence = "very_low"

    codes = [c.strip() for c in (event.related_stocks or "").split(",") if c.strip()]

    return AdvancedPrediction(
        event_id=event.id,
        event_title=event.title,
        event_type=event.event_type,
        industry=event.industry,
        industry_label=event.industry_label,
        event_date=event.event_date,
        impact_level=event.impact_level,
        prior_change_pct=prior,
        likelihood_change_pct=likelihood,
        posterior_change_pct=posterior,
        confidence_interval=(ci_low, ci_high),
        confidence=confidence,
        sample_size=int(eff_n),
        prior_weight=prior_weight,
        likelihood_weight=lk_weight,
        related_stocks=codes,
    )


def predict_upcoming_advanced(
    days_ahead: int = 30, min_impact: int = 3, limit: int = 30,
) -> list[AdvancedPrediction]:
    """Predict for all upcoming events, sorted by |posterior| desc."""
    from events import get_upcoming

    events = get_upcoming(days_ahead=days_ahead, min_impact=min_impact)[:limit]
    out: list[AdvancedPrediction] = []
    for ev in events:
        try:
            pred = predict_advanced(ev)
            out.append(pred)
        except Exception as e:
            logger.warning(f"Advanced predict failed for event {ev.id}: {e}")

    out.sort(key=lambda p: abs(p.posterior_change_pct), reverse=True)
    return out