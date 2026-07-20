"""
Smart Q&A system.

Pattern-matching natural language query interface. No LLM required.

Supported queries (Chinese):
  明天有什么事件？      → upcoming events next 1 day
  本周有什么 AI 事件？   → upcoming events filtered by industry
  朱雀三号什么时候？     → search event by keyword
  今天报告             → latest daily report
  LPR 什么时候公布？     → search event by keyword
  航天军工最近有什么？   → upcoming events by industry
  最强的信号是什么？     → top active signals
  今日股票             → today's hotness ranking

Architecture:
  - Pattern matching with regex (no LLM, no API)
  - Falls back to "no match" with helpful suggestions
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import desc, or_

from storage import get_db
from storage.models import (
    IndustryEvent, KnowledgeSignal, SectorHeat, DailyReport,
)


@dataclass
class QAResponse:
    """Result of a natural language query."""
    query: str
    matched_pattern: str
    intent: str  # 'upcoming_events' / 'search_event' / 'latest_report' / etc.
    answer: str  # human-readable answer
    data: dict   # structured data for UI rendering
    confidence: float  # 0-1


# ============================================================================
# Pattern definitions
# ============================================================================

_PATTERNS: list[tuple[re.Pattern, str, str, callable]] = [
    # (regex, intent_name, matched_pattern, handler)
    # Use re.IGNORECASE for flexibility
]


def _register_pattern(pattern: str, intent: str, handler):
    """Register a query pattern."""
    _PATTERNS.append((re.compile(pattern, re.IGNORECASE), intent, pattern, handler))


# Register all patterns
_register_pattern(
    r"(今天|今日|当天).*(报告|日报|推送)",
    "latest_report",
    lambda m: _handle_latest_report(),
)
_register_pattern(
    r"(昨天|昨日).*(报告|日报)",
    "previous_report",
    lambda m: _handle_previous_report(),
)
_register_pattern(
    r"(明天|明日|今晚).*(事件|大事件|有什么事|什么)",
    "upcoming_tomorrow",
    lambda m: _handle_upcoming(days=1),
)
_register_pattern(
    r"(今天|今日).*(事件|大事件|有什么事|什么)",
    "upcoming_today",
    lambda m: _handle_upcoming(days=0),
)
_register_pattern(
    r"(本周|这周|未来\s*7|未来\s*一周).*(事件|大事件)",
    "upcoming_week",
    lambda m: _handle_upcoming(days=7),
)
_register_pattern(
    r"(未来\s*\d+|接下来).*(事件|大事件)",
    "upcoming_n_days",
    lambda m: _handle_upcoming_n_days(m),
)
_register_pattern(
    r"(最强|最大|前几|前\s*[1-9]|top).*(信号|事件)",
    "top_signals",
    lambda m: _handle_top_signals(),
)
_register_pattern(
    r"今天.*(股票|涨|跌|涨跌|异动|行情)",
    "today_stocks",
    lambda m: _handle_today_stocks(),
)
_register_pattern(
    r"今天.*(期货|大宗|商品)",
    "today_futures",
    lambda m: _handle_today_futures(),
)
_register_pattern(
    r"最新.*(报告|日报)",
    "latest_report",
    lambda m: _handle_latest_report(),
)
# Industry + 时间 pattern (e.g., "航天军工最近有什么事件")
# 限制为 CJK/ASCII 字符，避免误匹配普通语句
_register_pattern(
    r"([\u4e00-\u9fffA-Za-z0-9]{2,10})\s*(最近|未来|接下来|近期)\s*(事件|大事|有什么)",
    "industry_upcoming",
    lambda m: _handle_industry_upcoming(m),
)
# Keyword search (fallback)
# Will be matched as last resort


# ============================================================================
# Handlers
# ============================================================================

def _handle_latest_report() -> QAResponse:
    db = get_db()
    with db.session() as s:
        report = (
            s.query(DailyReport)
            .order_by(desc(DailyReport.report_date))
            .first()
        )
    if not report:
        return QAResponse(
            query="", matched_pattern="latest_report", intent="latest_report",
            answer="还没有生成任何报告。", data={}, confidence=1.0,
        )
    return QAResponse(
        query="", matched_pattern="latest_report", intent="latest_report",
        answer=f"最新报告: {report.report_date} ({report.report_type})",
        data={
            "report_date": report.report_date.isoformat(),
            "report_type": report.report_type,
            "n_signals": report.n_signals,
            "n_news": report.n_news,
            "n_top_categories": report.n_top_categories,
            "markdown": report.markdown[:500] + "...",
        },
        confidence=1.0,
    )


def _handle_previous_report() -> QAResponse:
    db = get_db()
    with db.session() as s:
        report = (
            s.query(DailyReport)
            .filter(DailyReport.report_date < date.today())
            .order_by(desc(DailyReport.report_date))
            .first()
        )
    if not report:
        return _handle_latest_report()
    return QAResponse(
        query="", matched_pattern="previous_report", intent="latest_report",
        answer=f"上一份报告: {report.report_date}",
        data={"report_date": report.report_date.isoformat(), "report_type": report.report_type},
        confidence=0.9,
    )


def _handle_upcoming(days: int = 7) -> QAResponse:
    db = get_db()
    today = date.today()
    if days == 0:
        start, end = today, today
    else:
        start = today
        end = today + timedelta(days=days)

    with db.session() as s:
        events = (
            s.query(IndustryEvent)
            .filter(
                IndustryEvent.is_future == True,
                IndustryEvent.event_date >= start,
                IndustryEvent.event_date <= end,
                IndustryEvent.impact_level >= 2,
            )
            .order_by(IndustryEvent.event_date.asc())
            .all()
        )

    label = "今日" if days == 0 else (f"未来 {days} 天" if days > 1 else "明日")
    if not events:
        return QAResponse(
            query="", matched_pattern="upcoming", intent="upcoming_events",
            answer=f"{label}无事件 (impact ≥ 2)。",
            data={"events": []}, confidence=1.0,
        )

    lines = [f"{label}共 {len(events)} 个重要事件:"]
    for e in events[:10]:
        stars = "⭐" * e.impact_level
        lines.append(f"  • {e.event_date} {stars} {e.title} ({e.industry_label})")

    return QAResponse(
        query="", matched_pattern="upcoming", intent="upcoming_events",
        answer="\n".join(lines),
        data={
            "events": [
                {
                    "id": e.id, "title": e.title,
                    "event_date": e.event_date.isoformat(),
                    "impact_level": e.impact_level,
                    "industry_label": e.industry_label,
                    "event_type": e.event_type,
                }
                for e in events
            ]
        },
        confidence=1.0,
    )


def _handle_upcoming_n_days(m) -> QAResponse:
    text = m.string if hasattr(m, 'string') else m.group(0)
    n_match = re.search(r"\d+", text)
    n = int(n_match.group()) if n_match else 7
    n = min(90, max(1, n))
    return _handle_upcoming(days=n)


def _handle_top_signals() -> QAResponse:
    db = get_db()
    with db.session() as s:
        sigs = (
            s.query(KnowledgeSignal)
            .filter(
                KnowledgeSignal.phase == "active",
                KnowledgeSignal.strength >= 4.0,
            )
            .order_by(desc(KnowledgeSignal.strength))
            .limit(5)
            .all()
        )

    if not sigs:
        return QAResponse(
            query="", matched_pattern="top_signals", intent="top_signals",
            answer="暂无高强度活跃信号 (strength ≥ 4)。",
            data={"signals": []}, confidence=1.0,
        )

    lines = [f"Top {len(sigs)} 强信号:"]
    for s in sigs:
        stocks = ",".join(st.stock_code for st in s.stocks[:3])
        lines.append(f"  • {s.grade} {s.title} (⭐{s.strength:.1f}, {s.direction}, {stocks})")

    return QAResponse(
        query="", matched_pattern="top_signals", intent="top_signals",
        answer="\n".join(lines),
        data={
            "signals": [
                {
                    "id": s.id, "title": s.title, "grade": s.grade,
                    "strength": s.strength, "direction": s.direction,
                    "stocks": [st.stock_code for st in s.stocks[:5]],
                }
                for s in sigs
            ]
        },
        confidence=1.0,
    )


def _handle_today_stocks() -> QAResponse:
    db = get_db()
    today = date.today()
    with db.session() as s:
        heats = (
            s.query(SectorHeat)
            .filter(SectorHeat.trade_date == today)
            .order_by(SectorHeat.hotness_score.desc())
            .limit(10)
            .all()
        )
    if not heats:
        return QAResponse(
            query="", matched_pattern="today_stocks", intent="today_stocks",
            answer="今日暂无行业热度数据。",
            data={"heats": []}, confidence=0.8,
        )

    lines = ["今日行业热度 TOP 10:"]
    for h in heats:
        lines.append(f"  {h.rank}. {h.category_name} (热度 {h.hotness_score:.1f}, {h.n_stocks}股)")

    return QAResponse(
        query="", matched_pattern="today_stocks", intent="today_stocks",
        answer="\n".join(lines),
        data={"heats": [
            {
                "category_name": h.category_name,
                "hotness_score": h.hotness_score,
                "rank": h.rank,
                "n_stocks": h.n_stocks,
            } for h in heats
        ]},
        confidence=0.8,
    )


def _handle_today_futures() -> QAResponse:
    db = get_db()
    today = date.today()
    with db.session() as s:
        from storage.models import FuturesPrice
        prices = (
            s.query(FuturesPrice)
            .filter(FuturesPrice.trade_date == today)
            .order_by(desc(FuturesPrice.change_pct.abs()))
            .limit(10)
            .all()
        )
    if not prices:
        return QAResponse(
            query="", matched_pattern="today_futures", intent="today_futures",
            answer="今日暂无期货数据。",
            data={}, confidence=0.5,
        )

    lines = ["今日期货异动 TOP 10:"]
    for f in prices:
        arrow = "↑" if f.change_pct > 0 else "↓"
        lines.append(f"  {arrow} {f.name} {f.change_pct:+.2f}% (close {f.close:.2f})")

    return QAResponse(
        query="", matched_pattern="today_futures", intent="today_futures",
        answer="\n".join(lines),
        data={"futures": [
            {"name": f.name, "symbol": f.symbol, "change_pct": f.change_pct, "close": f.close}
            for f in prices
        ]},
        confidence=0.8,
    )


def _handle_industry_upcoming(m) -> QAResponse:
    """Handle '航天军工最近有什么事件' style queries."""
    industry_keyword = m.group(1)
    db = get_db()
    today = date.today()
    end = today + timedelta(days=30)

    with db.session() as s:
        events = (
            s.query(IndustryEvent)
            .filter(
                IndustryEvent.is_future == True,
                IndustryEvent.event_date >= today,
                IndustryEvent.event_date <= end,
                IndustryEvent.impact_level >= 2,
                or_(
                    IndustryEvent.industry_label.like(f"%{industry_keyword}%"),
                    IndustryEvent.title.like(f"%{industry_keyword}%"),
                )
            )
            .order_by(IndustryEvent.event_date.asc())
            .all()
        )

    if not events:
        return QAResponse(
            query=m.group(0), matched_pattern="industry_upcoming", intent="industry_upcoming",
            answer=f"未来 30 天 {industry_keyword} 行业暂无重要事件。",
            data={}, confidence=0.5,
        )

    lines = [f"未来 30 天 {industry_keyword} 行业有 {len(events)} 个事件:"]
    for e in events[:8]:
        stars = "⭐" * e.impact_level
        lines.append(f"  • {e.event_date} {stars} {e.title} ({e.event_type})")

    return QAResponse(
        query=m.group(0), matched_pattern="industry_upcoming", intent="industry_upcoming",
        answer="\n".join(lines),
        data={"events": [
            {
                "id": e.id, "title": e.title, "event_date": e.event_date.isoformat(),
                "impact_level": e.impact_level, "industry_label": e.industry_label,
            } for e in events
        ]},
        confidence=0.9,
    )


def _handle_keyword_search(query: str) -> QAResponse:
    """Fallback: search by keyword in events."""
    db = get_db()
    today = date.today()
    end = today + timedelta(days=180)

    with db.session() as s:
        events = (
            s.query(IndustryEvent)
            .filter(
                IndustryEvent.is_future == True,
                IndustryEvent.event_date >= today,
                IndustryEvent.event_date <= end,
                or_(
                    IndustryEvent.title.like(f"%{query}%"),
                    IndustryEvent.industry_label.like(f"%{query}%"),
                )
            )
            .order_by(IndustryEvent.event_date.asc())
            .limit(10)
            .all()
        )

    if not events:
        return QAResponse(
            query=query, matched_pattern="keyword_search", intent="keyword_search",
            answer=f"没找到包含「{query}」的事件。\n\n试试：\n- 「明天有什么事件」\n- 「航天军工最近有什么」\n- 「今天报告」",
            data={}, confidence=0.3,
        )

    lines = [f"找到 {len(events)} 个相关事件:"]
    for e in events:
        stars = "⭐" * e.impact_level
        lines.append(f"  • {e.event_date} {stars} {e.title} ({e.industry_label})")

    return QAResponse(
        query=query, matched_pattern="keyword_search", intent="keyword_search",
        answer="\n".join(lines),
        data={"events": [
            {
                "id": e.id, "title": e.title, "event_date": e.event_date.isoformat(),
                "impact_level": e.impact_level, "industry_label": e.industry_label,
            } for e in events
        ]},
        confidence=0.6,
    )


# ============================================================================
# Main entry point
# ============================================================================

def ask(query: str) -> QAResponse:
    """Process a natural language query and return a response.

    If LLM is enabled, it is tried first for intent parsing. Falls back
    to pattern matching, then keyword search.

    Args:
        query: user input (Chinese or English)

    Returns:
        QAResponse with answer text + structured data
    """
    query = (query or "").strip()
    if not query:
        return QAResponse(
            query=query, matched_pattern="empty", intent="empty",
            answer="请输入您的问题。\n\n试试：\n- 明天有什么事件\n- 航天军工最近有什么\n- 今天报告\n- 最强的信号",
            data={}, confidence=1.0,
        )

    # ---- Try LLM first if enabled ----
    llm_intent: LLMIntent | None = None
    from nlp.llm import is_llm_enabled, parse_query as llm_parse
    if is_llm_enabled():
        try:
            llm_intent = llm_parse(query)
            if llm_intent and llm_intent.confidence >= 0.7:
                # Map LLM intent to a handler + synthetic match
                handler = _llm_intent_to_handler(llm_intent, query)
                if handler:
                    try:
                        resp = handler(query)
                        resp.query = query
                        return resp
                    except Exception as e:
                        logger.warning(f"LLM-routed handler failed: {e}")
        except Exception as e:
            logger.warning(f"LLM parse failed, falling back to patterns: {e}")

    # ---- Pattern matching ----
    for pattern, intent, matched_str, handler in _PATTERNS:
        m = pattern.search(query)
        if m:
            logger.debug(f"Query matched pattern: {matched_str}")
            try:
                resp = handler(m)
                resp.query = query
                return resp
            except Exception as e:
                logger.warning(f"Handler {intent} failed: {e}")
                continue

    # ---- Fallback: keyword search ----
    try:
        if llm_intent and llm_intent.intent != "unknown":
            return _handle_keyword_search_with_context(query, llm_intent)
        return _handle_keyword_search(query)
    except Exception as e:
        logger.error(f"Keyword search handler failed: {e}")
        return QAResponse(
            query=query, matched_pattern="error", intent="error",
            answer=f"处理您的问题时出错，请稍后重试。\n错误: {e}",
            data={}, confidence=0.0,
        )


def _llm_intent_to_handler(llm_intent: LLMIntent, query: str) -> callable | None:
    """Map an LLM-parsed intent to a QA handler function.

    Returns a callable that takes the query string (not a regex match)
    and returns a QAResponse, or None if no handler matches.
    """
    from nlp.llm import LLMIntent
    intent = llm_intent.intent
    entities = llm_intent.entities

    if intent == "latest_report":
        return lambda q: _handle_latest_report()
    if intent == "top_signals":
        return lambda q: _handle_top_signals()
    if intent == "today_stocks":
        return lambda q: _handle_today_stocks()
    if intent == "today_futures":
        return lambda q: _handle_today_futures()
    if intent == "upcoming_events":
        days = 7
        if entities.get("time_horizon"):
            days = int(entities["time_horizon"])
        if entities.get("time_keyword") in ("今天", "今日"):
            days = 0
        elif entities.get("time_keyword") in ("明天", "明日"):
            days = 1
        return lambda q, _days=days: _handle_upcoming(days=_days)
    if intent == "industry_question":
        industries = entities.get("industries", [])
        if industries:
            kw = industries[0]
            # Build a synthetic regex match so _handle_industry_upcoming works
            class _FakeMatch:
                group = lambda self, g: kw if g == 1 else (
                    entities.get("time_keyword", "最近"))
                string = query
            m = _FakeMatch()
            return lambda q, _m=m: _handle_industry_upcoming(_m)
    return None


def _handle_keyword_search_with_context(query: str, llm_intent: LLMIntent) -> QAResponse:
    """Enhanced keyword search that incorporates LLM intent context."""
    from nlp.llm import LLMIntent
    entities = llm_intent.entities

    keywords = entities.get("keywords", [])
    industries = entities.get("industries", [])

    search_terms = [query]
    if industries:
        search_terms.extend(industries)
    if keywords:
        search_terms.extend(keywords[:3])

    resp = _handle_keyword_search(" ".join(search_terms))
    if resp.confidence < 0.4:
        resp.answer += f"\n\n💡 您似乎想查询「{llm_intent.intent}」相关问题，但未找到匹配数据。"
    return resp


SUGGESTED_QUERIES = [
    "明天有什么事件",
    "本周有什么 AI 事件",
    "今天报告",
    "航天军工最近有什么事件",
    "最强的信号是什么",
    "今天股票涨跌幅",
    "LPR 什么时候公布",
    "朱雀三号什么时候发射",
]