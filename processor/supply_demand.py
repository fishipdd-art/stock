"""
Supply-demand mismatch detection.

Identifies "supply chain mismatches" — situations where supply and demand
are out of balance, evidenced by:
  1. Futures/spot prices spike (上涨) or crash (下跌) beyond thresholds
  2. Active signals with direction = supply_tight / supply_surplus
  3. News matching "shortage" / "price increase" / "supply disruption" keywords
  4. Aggregated by industry category
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, date
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from config.settings import settings
from storage.models import (
    FuturesPrice, NewsRaw, KnowledgeSignal,
    SectorHeat, SearchTerm, KnowledgeCategory,
)


# Thresholds
PRICE_SPIKE_THRESHOLD = 0.03   # 3% daily move = "spike"
PRICE_CRASH_THRESHOLD = -0.03
NEWS_KEYWORDS_TIGHT = [
    "涨价", "短缺", "紧张", "供需错配", "供给紧", "缺口", "供不应求",
    "价格上涨", "产能不足", "供给受限", "供给收缩", "供应中断",
]
NEWS_KEYWORDS_SURPLUS = [
    "跌价", "过剩", "供过于求", "需求疲软", "产能过剩", "降价", "滞销",
    "需求下滑", "库存高企", "供应宽松",
]


@dataclass
class MismatchSignal:
    """One detected supply-demand mismatch."""
    category: str               # e.g. "有色金属" or "supply_tight"
    industry: str | None        # underlying industry (e.g. "MLCC") if any
    direction: str              # 'tight' / 'surplus' / 'mixed'
    strength: float             # 0-5
    summary: str                # one-line summary
    evidence: list[str]         # supporting data points (price, news titles)
    signal_key: str | None = None  # matched knowledge signal
    news_count: int = 0
    avg_price_change: float = 0.0
    final_score: float = 0.0    # combined score for ranking
    event_boost: float = 0.0    # 0-0.5 added when nearby events exist
    event_titles: list[str] = None  # nearby event titles for context

    def __post_init__(self):
        if self.event_titles is None:
            self.event_titles = []


def detect_from_futures(
    session: Session, target_date: date | None = None
) -> list[dict]:
    """Detect price spikes in today's futures."""
    target_date = target_date or date.today()
    rows = (
        session.query(FuturesPrice)
        .filter(FuturesPrice.trade_date == target_date)
        .all()
    )
    if not rows:
        return []

    out = []
    for r in rows:
        if r.change_pct >= PRICE_SPIKE_THRESHOLD:
            out.append({
                "type": "futures_spike",
                "symbol": r.symbol,
                "name": r.name,
                "exchange": r.exchange,
                "change_pct": r.change_pct,
                "close": r.close,
                "direction": "tight",
            })
        elif r.change_pct <= PRICE_CRASH_THRESHOLD:
            out.append({
                "type": "futures_crash",
                "symbol": r.symbol,
                "name": r.name,
                "exchange": r.exchange,
                "change_pct": r.change_pct,
                "close": r.close,
                "direction": "surplus",
            })
    return out


def detect_from_knowledge_signals(
    session: Session, since: datetime | None = None
) -> list[MismatchSignal]:
    """Detect active signals and convert to MismatchSignal objects."""
    since = since or (datetime.utcnow() - timedelta(days=settings.news_keep_days))
    signals = (
        session.query(KnowledgeSignal)
        .filter(KnowledgeSignal.phase == "active")
        .filter(KnowledgeSignal.signal_date >= since.strftime("%Y-%m-%d"))
        .all()
    )

    out = []
    for s in signals:
        if s.direction == "supply_tight":
            direction = "tight"
        elif s.direction in ("supply_surplus", "supply_oversupply"):
            direction = "surplus"
        else:
            direction = "mixed"

        out.append(
            MismatchSignal(
                category=s.direction or "未分类",
                industry=None,
                direction=direction,
                strength=s.strength,
                summary=s.title,
                evidence=[s.description or "", s.price_info or ""],
                signal_key=s.signal_key,
                news_count=0,
                avg_price_change=0.0,
                final_score=s.strength,
            )
        )
    return out


def detect_from_news(
    session: Session, since: datetime | None = None
) -> list[MismatchSignal]:
    """Detect supply-demand keywords in news within the window."""
    since = since or (datetime.utcnow() - timedelta(days=2))
    news = (
        session.query(NewsRaw)
        .filter(NewsRaw.published_at >= since)
        .all()
    )
    if not news:
        return []

    # Match news against knowledge terms to get categories
    terms_by_cat: dict[str, list[SearchTerm]] = {}
    terms = session.query(SearchTerm).all()
    for t in terms:
        if t.category:
            terms_by_cat.setdefault(t.category.name, []).append(t)

    # Aggregate
    tight_by_cat: dict[str, list[NewsRaw]] = {}
    surplus_by_cat: dict[str, list[NewsRaw]] = {}
    for n in news:
        text = (n.title + " " + (n.summary or "")).lower()
        is_tight = any(kw in text for kw in NEWS_KEYWORDS_TIGHT)
        is_surplus = any(kw in text for kw in NEWS_KEYWORDS_SURPLUS)

        # Find matching category via keywords_matched
        cat = "未分类"
        if n.keywords_matched:
            first_kw = n.keywords_matched.split(",")[0].strip()
            for cname, terms in terms_by_cat.items():
                for t in terms:
                    if t.term == first_kw:
                        cat = cname
                        break
                if cat != "未分类":
                    break

        if is_tight and not is_surplus:
            tight_by_cat.setdefault(cat, []).append(n)
        elif is_surplus and not is_tight:
            surplus_by_cat.setdefault(cat, []).append(n)
        elif is_tight and is_surplus:
            # both: mixed
            tight_by_cat.setdefault(cat, []).append(n)
            surplus_by_cat.setdefault(cat, []).append(n)

    out: list[MismatchSignal] = []
    # Tight
    for cat, news_list in tight_by_cat.items():
        strength = min(5.0, 1.0 + len(news_list) * 0.4)
        out.append(
            MismatchSignal(
                category=cat,
                industry=None,
                direction="tight",
                strength=strength,
                summary=f"{cat}: {len(news_list)} 条涨价/短缺相关新闻",
                evidence=[n.title for n in news_list[:3]],
                news_count=len(news_list),
                final_score=strength,
            )
        )
    # Surplus
    for cat, news_list in surplus_by_cat.items():
        strength = min(5.0, 1.0 + len(news_list) * 0.4)
        out.append(
            MismatchSignal(
                category=cat,
                industry=None,
                direction="surplus",
                strength=strength,
                summary=f"{cat}: {len(news_list)} 条跌价/过剩相关新闻",
                evidence=[n.title for n in news_list[:3]],
                news_count=len(news_list),
                final_score=strength,
            )
        )
    return out


def aggregate_mismatches(
    session: Session, target_date: date | None = None
) -> list[MismatchSignal]:
    """Run all detection layers and aggregate/dedupe by category."""
    by_key: dict[tuple[str, str], MismatchSignal] = {}

    # Layer 1: Knowledge signals (high precision)
    for m in detect_from_knowledge_signals(session):
        key = (m.category, m.direction)
        if key in by_key:
            existing = by_key[key]
            existing.strength = max(existing.strength, m.strength)
            existing.evidence.extend(m.evidence)
            existing.signal_key = m.signal_key or existing.signal_key
            existing.final_score = max(existing.final_score, m.final_score)
        else:
            by_key[key] = m

    # Layer 2: News keyword detection (medium precision)
    for m in detect_from_news(session):
        key = (m.category, m.direction)
        if key in by_key:
            existing = by_key[key]
            existing.news_count += m.news_count
            existing.evidence.extend(m.evidence)
            existing.final_score = (
                existing.final_score + m.final_score
            ) / 2
        else:
            by_key[key] = m

    # Layer 3: Futures spikes
    futures_data = detect_from_futures(session, target_date)
    for fd in futures_data:
        # bucket by exchange (rough)
        cat = "期货异动"
        if fd["exchange"] == "SHFE":
            cat = "有色金属" if any(s in fd["symbol"] for s in ["CU", "AL", "ZN", "NI", "PB", "SN"]) else (
                "贵金属" if any(s in fd["symbol"] for s in ["AU", "AG"]) else "黑色系"
            )
        elif fd["exchange"] == "DCE":
            cat = "能化" if any(s in fd["symbol"] for s in ["L", "PP", "V", "EG", "EB"]) else "农产品"
        elif fd["exchange"] == "CZCE":
            cat = "农产品" if any(s in fd["symbol"] for s in ["SR", "CF", "OI", "RM", "M", "Y", "P", "C", "WH", "PM", "RI"]) else "能化"
        key = (cat, fd["direction"])
        if key in by_key:
            existing = by_key[key]
            existing.evidence.append(
                f"{fd['name']} {fd['change_pct']:+.2f}%"
            )
        else:
            by_key[key] = MismatchSignal(
                category=cat,
                industry=None,
                direction=fd["direction"],
                strength=2.0 + abs(fd["change_pct"]) * 20,
                summary=f"{cat}: 期货异动 {fd['name']} {fd['change_pct']:+.2f}%",
                evidence=[f"{fd['name']} {fd['change_pct']:+.2f}%"],
                avg_price_change=fd["change_pct"],
                final_score=2.0 + abs(fd["change_pct"]) * 20,
            )

    # Apply event proximity boost to each mismatch signal.
    # Two paths: (a) signal_key is set -> use signal's stocks; (b) otherwise -> match by category name.
    from processor.event_boost import (
        compute_event_boost, get_stock_codes_for_signal, get_upcoming_events_for_stocks,
    )
    for m in by_key.values():
        codes: list[str] = []
        if m.signal_key:
            sig_row = (
                session.query(KnowledgeSignal)
                .filter(KnowledgeSignal.signal_key == m.signal_key)
                .first()
            )
            if sig_row:
                codes = get_stock_codes_for_signal(session, sig_row.id)
        if not codes and m.category:
            try:
                events = get_upcoming_events_for_stocks(
                    [], days_ahead=30, min_impact=3,
                )
            except Exception:
                events = []
            matched_titles = [
                ev.title for ev in events
                if m.category and m.category in (ev.industry_label or "")
            ]
            if matched_titles:
                m.event_boost = 0.05 * len(matched_titles[:3])
                m.event_titles = matched_titles[:3]
        if codes:
            boost = compute_event_boost(codes)
            if boost.has_boost:
                m.event_boost = boost.boost_factor
                m.event_titles = [e.title for e in boost.matched_events[:3]]
        if m.event_boost:
            m.final_score = m.final_score * (1.0 + m.event_boost)

    out = list(by_key.values())
    out.sort(key=lambda x: x.final_score, reverse=True)

    for m in out:
        m.evidence = m.evidence[:5]

    logger.info(f"Detected {len(out)} mismatch signals (with event boost)")
    return out