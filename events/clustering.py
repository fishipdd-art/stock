"""
Multi-event clustering and stacking analysis.

When multiple events hit the same industry in a short time window
(e.g., 3+ events in a week), their effects can compound.

Strategy:
  1. Group upcoming events by (industry, week)
  2. For clusters with N >= 2 events, compute combined effect
  3. Combined effect is non-linear: sqrt(N) scaling for direction
     (events in same direction compound, opposite directions cancel)
  4. Flag clusters as "high attention" when stacking detected
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import mean
from typing import Optional

from loguru import logger
from sqlalchemy import desc

from storage import get_db
from storage.models import IndustryEvent
from events.predictor import predict_impact, ImpactPrediction


WINDOW_DAYS = 7  # events within 7 days stack
MIN_STACK_SIZE = 2  # minimum to flag as cluster


@dataclass
class EventCluster:
    """A cluster of events hitting the same industry in a time window."""
    industry: str
    industry_label: str
    window_start: date
    window_end: date
    events: list[ImpactPrediction] = field(default_factory=list)
    combined_change_pct: float = 0.0
    direction: str = "neutral"  # 'bullish' / 'bearish' / 'mixed' / 'neutral'
    attention_level: str = "low"  # 'low' / 'medium' / 'high' / 'critical'
    dominant_event_type: str = ""
    risk_note: str = ""


def find_clusters(
    days_ahead: int = 30,
    min_impact: int = 3,
) -> list[EventCluster]:
    """Find clusters of events by (industry, 7-day window)."""
    db = get_db()
    today = date.today()
    end = today + timedelta(days=days_ahead)

    with db.session() as s:
        events = (
            s.query(IndustryEvent)
            .filter(
                IndustryEvent.is_future == True,
                IndustryEvent.event_date >= today,
                IndustryEvent.event_date <= end,
                IndustryEvent.impact_level >= min_impact,
            )
            .order_by(IndustryEvent.event_date.asc())
            .all()
        )

    if not events:
        return []

    # Predict impact for each event
    predictions: list[ImpactPrediction] = []
    for ev in events:
        try:
            pred = predict_impact(ev)
            predictions.append(pred)
        except Exception as e:
            logger.warning(f"Predict failed for event {ev.id}: {e}")

    # Group by (industry, 7-day window)
    # Window = floor((event_date - today) / 7) * 7
    buckets: dict[tuple[str, int], list[ImpactPrediction]] = defaultdict(list)
    for p in predictions:
        days = (p.event_date - today).days
        window_idx = days // WINDOW_DAYS
        buckets[(p.industry, window_idx)].append(p)

    clusters: list[EventCluster] = []
    for (industry, _), preds in buckets.items():
        if len(preds) < MIN_STACK_SIZE:
            continue

        # Compute combined effect
        changes = [p.predicted_change_pct for p in preds]
        positive = [c for c in changes if c > 0]
        negative = [c for c in changes if c < 0]

        if positive and not negative:
            # All bullish
            combined = mean(positive) * (1 + 0.3 * (len(preds) - 1))
            direction = "bullish"
        elif negative and not positive:
            combined = mean(negative) * (1 + 0.3 * (len(preds) - 1))
            direction = "bearish"
        elif positive and negative:
            avg_pos = mean(positive)
            avg_neg = mean(negative)
            # Net: positive minus negative magnitude
            combined = abs(avg_pos) * len(positive) - abs(avg_neg) * len(negative)
            if abs(combined) < 0.5:
                direction = "mixed"
            elif combined > 0:
                direction = "bullish"
                combined = combined / len(preds)
            else:
                direction = "bearish"
                combined = combined / len(preds)
        else:
            combined = 0
            direction = "neutral"

        # Cap
        combined = max(-25.0, min(25.0, combined))

        # Attention level
        if len(preds) >= 5:
            attention = "critical"
        elif len(preds) >= 3 and abs(combined) > 2:
            attention = "high"
        elif len(preds) >= 2 and abs(combined) > 3:
            attention = "high"
        else:
            attention = "medium"

        # Dominant event type
        type_counts: dict[str, int] = defaultdict(int)
        for p in preds:
            type_counts[p.event_type] += 1
        dominant = max(type_counts.items(), key=lambda x: x[1])[0] if type_counts else ""

        # Risk note
        if direction == "mixed" and len(preds) >= 3:
            risk = "⚠️ 多空事件并存，结果不确定"
        elif attention == "critical":
            risk = "🚨 同行业 5+ 事件聚集，重点关注"
        elif attention == "high" and direction in ("bullish", "bearish"):
            risk = f"📌 多事件 {direction} 叠加"
        else:
            risk = ""

        clusters.append(EventCluster(
            industry=industry,
            industry_label=preds[0].industry_label,
            window_start=min(p.event_date for p in preds),
            window_end=max(p.event_date for p in preds),
            events=preds,
            combined_change_pct=combined,
            direction=direction,
            attention_level=attention,
            dominant_event_type=dominant,
            risk_note=risk,
        ))

    # Sort by attention then magnitude
    attention_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    clusters.sort(
        key=lambda c: (attention_order.get(c.attention_level, 0), abs(c.combined_change_pct)),
        reverse=True,
    )
    return clusters


def summarize_clusters(clusters: list[EventCluster]) -> str:
    """Markdown summary of top clusters."""
    if not clusters:
        return "_未来 30 天无多事件叠加_"

    lines = ["## 🔥 多事件叠加分析 (top 10)", ""]
    lines.append("| 行业 | 时间窗 | 事件数 | 主导类型 | 合并涨跌 | 方向 | 关注度 |")
    lines.append("|------|--------|--------|----------|----------|------|--------|")
    for c in clusters[:10]:
        n_events = len(c.events)
        days = (c.window_end - c.window_start).days
        window = f"{c.window_start} ~ {c.window_end}" if days > 0 else c.window_start.isoformat()
        arrow = "↑" if c.combined_change_pct > 0 else "↓"
        color = "🟢" if c.direction == "bullish" else ("🔴" if c.direction == "bearish" else "🟡")
        attn_icon = {"critical": "🚨", "high": "⚠️", "medium": "📌", "low": ""}.get(c.attention_level, "")
        lines.append(
            f"| {c.industry_label} | {window} | {n_events} | {c.dominant_event_type} | "
            f"{arrow} {abs(c.combined_change_pct):.2f}% | {color} {c.direction} | {attn_icon} {c.attention_level} |"
        )
    return "\n".join(lines)