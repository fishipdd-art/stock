"""
News-to-signal matching.

Matches scraped news against:
  1. Active knowledge signals (506 known events)
  2. Search terms (70+ terms)

Returns enriched match records that downstream processors can score.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy.orm import Session

from storage.models import NewsRaw, KnowledgeSignal, SearchTerm, SignalStock
from processor.time_decay import weight_for_datetime
from processor.event_boost import (
    compute_event_boost, get_stock_codes_for_signal,
)


MATCHER_VERSION = "v3"
MAX_SIGNAL_MATCHES_PER_NEWS = 5
_GENERIC_KEYWORDS = {
    "价格", "走势", "供需", "供给", "需求", "缺口", "涨价", "降价", "上涨", "下跌", "公司",
    "集团", "行业", "板块", "股票", "市场", "最新", "今日", "政策", "利好",
    "发布", "数据", "同比", "环比", "2024", "2025", "2026",
    "source", "web", "search", "代码", "发行价", "美元", "估值",
}
_EVENT_PREDICATES = {
    "涨", "跌", "提价", "降价", "扩产", "减产", "停产", "投产", "缺货",
    "短缺", "库存", "订单", "中标", "制裁", "封锁", "回购", "并购", "重组",
    "增长", "下滑", "预增", "预减", "补贴", "处罚", "调查", "突破", "发射",
}


@dataclass
class SignalMatch:
    """A news article matched against a known signal or term."""
    news_id: int
    news_title: str
    news_url: str
    news_published_at: datetime
    matched_signal_key: str | None = None
    matched_term: str | None = None
    match_score: float = 0.0  # 0-1 confidence
    base_signal_strength: float = 0.0
    decay_weight: float = 0.0
    final_score: float = 0.0
    event_boost: float = 0.0  # 0-0.5 additive multiplier from nearby events
    event_titles: list[str] = None  # nearby event titles for context

    def __post_init__(self):
        if self.event_titles is None:
            self.event_titles = []

    @property
    def age_days(self) -> float:
        return (datetime.utcnow() - self.news_published_at).total_seconds() / 86400.0


# === Matching logic ===

def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace + strip."""
    return re.sub(r"\s+", "", (text or "").lower())


def match_news_to_signal(news: NewsRaw, signal: KnowledgeSignal) -> float:
    """Compute match score [0,1] between a news article and a signal.

    Strategy: extract distinctive keywords from signal title (>=2 chars)
    and check how many appear in the news title.
    """
    news_text = _normalize(news.title + " " + (news.summary or ""))
    sig_text = (signal.title or "").lower()

    # Extract keywords (>=2 chars, alphanumeric+CJK)
    sig_keywords = {
        token for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", sig_text)
        if token not in _GENERIC_KEYWORDS
        and not token.isdigit()
        and (len(token) >= 2)
    }
    if not sig_keywords:
        return 0.0

    matched = sum(1 for kw in sig_keywords if kw in news_text)
    # Multi-entity signals need more than one coincidental token.  A single
    # entity signal is allowed only when the article also states an event.
    if len(sig_keywords) >= 3 and matched < 2:
        return 0.0
    if len(sig_keywords) <= 2 and matched == 1 and not any(
        predicate in news_text for predicate in _EVENT_PREDICATES
    ):
        return 0.0
    score = matched / len(sig_keywords)
    return min(1.0, score)


def match_news_to_term(news: NewsRaw, term: str) -> bool:
    """Require a distinctive term token, not a generic market word."""
    news_text = _normalize(news.title + " " + (news.summary or ""))
    term_words = [
        token.lower()
        for token in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{2,}", term or "")
        if token.lower() not in _GENERIC_KEYWORDS
        and not token.isdigit()
        and len(token) >= 2
    ]
    if not term_words:
        return False
    matched = [word for word in term_words if word in news_text]
    if not matched:
        return False
    if len(term_words) >= 2:
        return len(matched) >= 2
    title_text = _normalize(news.title or "")
    return term_words[0] in title_text and any(
        predicate in news_text for predicate in _EVENT_PREDICATES
    )


def build_matches(
    session: Session,
    since: datetime | None = None,
    min_match_score: float = 0.34,
    top_signals: int = 506,
) -> list[SignalMatch]:
    """Match all recent news against active signals and search terms.

    Returns list of SignalMatch sorted by final_score desc.
    """
    since = since or (datetime.utcnow() - timedelta(days=7))

    # Load all news in window
    news_list = (
        session.query(NewsRaw)
        .filter(NewsRaw.published_at >= since)
        .filter(NewsRaw.keywords_matched != "")
        .order_by(NewsRaw.published_at.desc())
        .all()
    )
    if not news_list:
        logger.info("No recent news to match")
        return []

    # Load active signals
    signals = (
        session.query(KnowledgeSignal)
        .filter(KnowledgeSignal.phase == "active")
        .order_by(KnowledgeSignal.strength.desc())
        .limit(top_signals)
        .all()
    )

    # Load all enabled search terms
    terms = session.query(SearchTerm).filter(SearchTerm.enabled == True).all()

    matches: list[SignalMatch] = []
    matched_news_ids: set[int] = set()

    # Pre-compute event boost per signal (cached, signals shared across news)
    signal_boost_cache: dict[int, tuple[float, list[str]]] = {}
    for sig in signals:
        codes = get_stock_codes_for_signal(session, sig.id)
        if codes:
            boost = compute_event_boost(codes)
            if boost.has_boost:
                signal_boost_cache[sig.id] = (
                    boost.boost_factor,
                    [e.title for e in boost.matched_events[:3]],
                )

    # 1. News → Signal
    for news in news_list:
        for sig in signals:
            signal_title = (sig.title or "").lower()
            if "source: web search" in signal_title or "---" in signal_title:
                continue
            s = match_news_to_signal(news, sig)
            if s >= min_match_score:
                decay = weight_for_datetime(news.published_at)
                base_final = s * sig.strength * decay
                ev_boost, ev_titles = signal_boost_cache.get(sig.id, (0.0, []))
                final = base_final * (1.0 + ev_boost)
                matches.append(
                    SignalMatch(
                        news_id=news.id,
                        news_title=news.title,
                        news_url=news.url,
                        news_published_at=news.published_at,
                        matched_signal_key=sig.signal_key,
                        match_score=s,
                        base_signal_strength=sig.strength,
                        decay_weight=decay,
                        final_score=final,
                        event_boost=ev_boost,
                        event_titles=ev_titles,
                    )
                )
                matched_news_ids.add(news.id)

    # Cap signal fan-out per article.  Even a broad macro story should not
    # activate dozens of near-duplicate knowledge signals.
    capped: list[SignalMatch] = []
    by_news: dict[int, list[SignalMatch]] = {}
    for match in matches:
        by_news.setdefault(match.news_id, []).append(match)
    for news_matches in by_news.values():
        news_matches.sort(key=lambda item: item.final_score, reverse=True)
        capped.extend(news_matches[:MAX_SIGNAL_MATCHES_PER_NEWS])
    matches = capped
    matched_news_ids = {match.news_id for match in matches}

    # 2. News → Term (catches news not matching any known signal)
    for news in news_list:
        if news.id in matched_news_ids:
            continue
        for term in terms:
            if match_news_to_term(news, term.term):
                decay = weight_for_datetime(news.published_at)
                final = 1.0 * 1.0 * decay  # no known signal, base strength = 1
                matches.append(
                    SignalMatch(
                        news_id=news.id,
                        news_title=news.title,
                        news_url=news.url,
                        news_published_at=news.published_at,
                        matched_term=term.term,
                        match_score=0.3,
                        base_signal_strength=0.0,
                        decay_weight=decay,
                        final_score=final,
                    )
                )
                matched_news_ids.add(news.id)
                break  # one term match per news is enough

    matches.sort(key=lambda m: m.final_score, reverse=True)
    logger.info(
        f"Matched {len(news_list)} news articles -> {len(matches)} matches "
        f"({len(matched_news_ids)} news hit)"
    )
    return matches


def group_matches_by_signal(matches: Iterable[SignalMatch]) -> dict[str, list[SignalMatch]]:
    """Group matches by signal key."""
    out: dict[str, list[SignalMatch]] = {}
    for m in matches:
        key = m.matched_signal_key or f"term:{m.matched_term}"
        out.setdefault(key, []).append(m)
    return out


def group_matches_by_category(
    session: Session, matches: Iterable[SignalMatch]
) -> dict[str, list[SignalMatch]]:
    """Group matches by category via signal_key/term mapping."""
    out: dict[str, list[SignalMatch]] = {}
    for m in matches:
        # Resolve category
        cat_name = "未分类"
        if m.matched_signal_key:
            sig = (
                session.query(KnowledgeSignal)
                .filter(KnowledgeSignal.signal_key == m.matched_signal_key)
                .first()
            )
            if sig:
                # Use signal direction as proxy category bucket
                cat_name = sig.direction or "未分类"
        elif m.matched_term:
            term = (
                session.query(SearchTerm)
                .filter(SearchTerm.term == m.matched_term)
                .first()
            )
            if term and term.category:
                cat_name = term.category.name
        out.setdefault(cat_name, []).append(m)
    return out
