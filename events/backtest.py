"""
Event vs price-move backtest.

For each past event in the database, find related stock/futures price
data and measure price change before/after the event. Aggregate by
event_type + industry to find which events have historically driven
the largest moves.

Output: top correlated (event_type, industry, direction) pairs.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from statistics import mean, stdev
from typing import Optional

from loguru import logger
from sqlalchemy import and_

from storage import get_db
from storage.models import (
    IndustryEvent, FuturesPrice, StockQuote,
)


WINDOW_BEFORE_DAYS = 5    # baseline: avg price over prior N days
WINDOW_AFTER_DAYS = 3     # reaction: avg price over next N days
MIN_DATA_POINTS = 3       # need at least N quotes to compute


@dataclass
class EventBacktest:
    """Result of measuring price reaction to a single event."""
    event_id: int
    event_title: str
    event_type: str
    industry: str
    industry_label: str
    event_date: date
    impact_level: int
    related_stocks: list[str]
    avg_price_before: float | None = None
    avg_price_after: float | None = None
    price_change_pct: float | None = None
    data_points_before: int = 0
    data_points_after: int = 0
    sample_size: int = 0  # number of stocks used


@dataclass
class AggregateCorrelation:
    """Aggregated correlation for (event_type, industry) bucket."""
    event_type: str
    industry: str
    industry_label: str
    sample_size: int
    avg_change_pct: float
    median_change_pct: float
    std_change_pct: float
    direction: str  # 'up' / 'down' / 'mixed'
    confidence: str  # 'high' / 'medium' / 'low' (based on sample size)


def _compute_stock_change(
    session, code: str, event_date: date
) -> tuple[Optional[float], int, int]:
    """Compute avg price before/after event for a single stock.

    Returns (change_pct, n_before, n_after).
    """
    before_start = event_date - timedelta(days=WINDOW_BEFORE_DAYS)
    after_end = event_date + timedelta(days=WINDOW_AFTER_DAYS)

    quotes = (
        session.query(StockQuote)
        .filter(
            and_(
                StockQuote.code == code,
                StockQuote.trade_date >= before_start,
                StockQuote.trade_date <= after_end,
            )
        )
        .all()
    )
    if len(quotes) < MIN_DATA_POINTS:
        return None, 0, 0

    before_quotes = [q.close for q in quotes if q.trade_date < event_date]
    after_quotes = [q.close for q in quotes if q.trade_date >= event_date]

    if not before_quotes or not after_quotes:
        return None, 0, 0

    avg_before = mean(before_quotes)
    avg_after = mean(after_quotes)
    if avg_before == 0:
        return None, len(before_quotes), len(after_quotes)

    change = (avg_after - avg_before) / avg_before * 100
    return change, len(before_quotes), len(after_quotes)


def _compute_futures_change(
    session, event: IndustryEvent
) -> tuple[Optional[float], int]:
    """Try futures price change as a proxy when no stock data available.

    Maps event industry to relevant futures symbols.
    """
    industry_futures_map: dict[str, list[str]] = {
        "aerospace": [],
        "semiconductor": [],
        "ne_vehicle": [],
        "lithium_battery": ["LC", "SI"],  # lithium carbonate, silicon
        "solar": ["SI"],
        "steel": ["RB", "HC", "I"],  # rebar, HRC, iron ore
        "non_ferrous": ["CU", "AL", "ZN", "NI", "PB", "SN"],
        "rare_earth": [],
        "agriculture": ["A", "M", "Y", "P", "C", "SR", "CF", "OI", "RM"],
        "energy": ["SC", "FU", "BU", "LU"],
        "chemicals": ["TA", "MA", "EG", "L", "PP", "V", "EB"],
        "consumer": [],
        "agriculture_global": ["A", "M", "Y"],
        "shipping": [],
        "energy_global": ["SC", "FU", "BU"],
        "agriculture_us": ["A", "M", "Y"],
        "rare_earth_global": [],
    }

    symbols = industry_futures_map.get(event.industry, [])
    if not symbols:
        return None, 0

    before_start = event.event_date - timedelta(days=WINDOW_BEFORE_DAYS)
    after_end = event.event_date + timedelta(days=WINDOW_AFTER_DAYS)

    changes: list[float] = []
    for sym in symbols:
        prices = (
            session.query(FuturesPrice)
            .filter(
                and_(
                    FuturesPrice.symbol.like(f"{sym}%"),
                    FuturesPrice.trade_date >= before_start,
                    FuturesPrice.trade_date <= after_end,
                )
            )
            .all()
        )
        before = [p.close for p in prices if p.trade_date < event.event_date]
        after = [p.close for p in prices if p.trade_date >= event.event_date]
        if before and after and mean(before) > 0:
            changes.append((mean(after) - mean(before)) / mean(before) * 100)

    if not changes:
        return None, 0
    return mean(changes), len(changes)


def backtest_event(session, event: IndustryEvent) -> Optional[EventBacktest]:
    """Run backtest for a single event."""
    codes = [c.strip() for c in (event.related_stocks or "").split(",") if c.strip()]

    changes: list[float] = []
    n_before_total = 0
    n_after_total = 0
    used_stocks: list[str] = []
    data_source = "stock"

    if codes:
        for code in codes:
            change, n_b, n_a = _compute_stock_change(session, code, event.event_date)
            if change is not None:
                changes.append(change)
                used_stocks.append(code)
                n_before_total += n_b
                n_after_total += n_a

    # Fallback to futures if no stock data
    if not changes:
        change, n_fut = _compute_futures_change(session, event)
        if change is not None:
            changes.append(change)
            n_before_total = n_fut
            n_after_total = n_fut
            data_source = "futures"

    if not changes:
        return None

    avg_change = mean(changes)
    return EventBacktest(
        event_id=event.id,
        event_title=event.title,
        event_type=event.event_type,
        industry=event.industry,
        industry_label=event.industry_label,
        event_date=event.event_date,
        impact_level=event.impact_level,
        related_stocks=used_stocks,
        price_change_pct=avg_change,
        data_points_before=n_before_total,
        data_points_after=n_after_total,
        sample_size=len(changes),
    )


def aggregate_correlations(backtests: list[EventBacktest]) -> list[AggregateCorrelation]:
    """Group backtests by (event_type, industry) and compute stats."""
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    labels: dict[tuple[str, str], str] = {}

    for b in backtests:
        if b.price_change_pct is None:
            continue
        key = (b.event_type, b.industry)
        buckets[key].append(b.price_change_pct)
        labels[key] = b.industry_label

    out = []
    for key, changes in buckets.items():
        if len(changes) < 2:
            continue
        event_type, industry = key
        avg = mean(changes)
        med = sorted(changes)[len(changes) // 2]
        sd = stdev(changes) if len(changes) > 1 else 0.0

        if abs(avg) > 2:
            direction = "up" if avg > 0 else "down"
        else:
            direction = "mixed"

        if len(changes) >= 10:
            confidence = "high"
        elif len(changes) >= 5:
            confidence = "medium"
        else:
            confidence = "low"

        out.append(AggregateCorrelation(
            event_type=event_type,
            industry=industry,
            industry_label=labels.get(key, industry),
            sample_size=len(changes),
            avg_change_pct=avg,
            median_change_pct=med,
            std_change_pct=sd,
            direction=direction,
            confidence=confidence,
        ))

    out.sort(key=lambda x: abs(x.avg_change_pct) * (1 + x.sample_size / 10), reverse=True)
    return out


def run_backtest(
    days: int = 365,
    min_impact: int = 2,
    max_events: int = 500,
) -> dict:
    """Run backtest on past N days of events.

    Returns dict with individual backtests + aggregated correlations.
    """
    db = get_db()
    cutoff = date.today() - timedelta(days=days)

    with db.session() as s:
        events = (
            s.query(IndustryEvent)
            .filter(
                IndustryEvent.is_future == False,
                IndustryEvent.event_date >= cutoff,
                IndustryEvent.impact_level >= min_impact,
            )
            .order_by(IndustryEvent.event_date.desc())
            .limit(max_events)
            .all()
        )
        logger.info(f"Backtest: {len(events)} past events in last {days} days")

        backtests: list[EventBacktest] = []
        for ev in events:
            try:
                bt = backtest_event(s, ev)
                if bt and bt.price_change_pct is not None:
                    backtests.append(bt)
            except Exception as e:
                logger.warning(f"Backtest event {ev.id} failed: {e}")

        correlations = aggregate_correlations(backtests)

    logger.info(
        f"Backtest done: {len(backtests)} measurable events, "
        f"{len(correlations)} (event_type, industry) correlations"
    )

    return {
        "n_events_analyzed": len(events),
        "n_measurable": len(backtests),
        "n_correlations": len(correlations),
        "individual_backtests": [asdict(b) for b in backtests[:100]],
        "top_correlations": [asdict(c) for c in correlations[:30]],
    }


def format_backtest_report(result: dict) -> str:
    """Format backtest result as readable markdown."""
    lines = []
    lines.append("# 📊 事件 vs 价格异动回测")
    lines.append("")
    lines.append(f"分析事件数: {result['n_events_analyzed']}")
    lines.append(f"有价格数据的: {result['n_measurable']}")
    lines.append(f"有效相关性: {result['n_correlations']}")
    lines.append("")
    if result["top_correlations"]:
        lines.append("## 强相关性 (事件类型 × 行业)")
        lines.append("")
        lines.append("| 事件类型 | 行业 | 样本数 | 平均涨跌 | 中位数 | 方向 | 置信度 |")
        lines.append("|---------|------|--------|----------|--------|------|--------|")
        for c in result["top_correlations"][:20]:
            lines.append(
                f"| {c['event_type']} | {c['industry_label']} | "
                f"{c['sample_size']} | {c['avg_change_pct']:+.2f}% | "
                f"{c['median_change_pct']:+.2f}% | {c['direction']} | "
                f"{c['confidence']} |"
            )
    lines.append("")
    if result["individual_backtests"]:
        lines.append("## 个别事件反应 (top 20)")
        lines.append("")
        lines.append("| 事件 | 日期 | 行业 | 涨跌 | 样本 |")
        lines.append("|------|------|------|------|------|")
        for b in result["individual_backtests"][:20]:
            title = b["event_title"][:50]
            lines.append(
                f"| {title} | {b['event_date']} | {b['industry_label']} | "
                f"{b['price_change_pct']:+.2f}% | {b['sample_size']} |"
            )
    return "\n".join(lines)