"""
Report generation.

Produces:
  - Markdown report (full text)
  - JSON payload (for Feishu card format)
"""
from __future__ import annotations

import json
from datetime import datetime, date, timedelta
from dataclasses import asdict

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from config.settings import settings
from storage.models import (
    NewsRaw, KnowledgeSignal, SectorHeat, DailyReport,
    FuturesPrice, SearchTerm, StockQuote,
)
from processor.supply_demand import MismatchSignal, aggregate_mismatches
from processor.matcher import build_matches, SignalMatch
from processor.time_decay import weight_for_datetime
from pipeline.time_utils import utc_bounds_for_business_date


def _top_n(mismatches: list[MismatchSignal], n: int = 10) -> list[MismatchSignal]:
    return sorted(mismatches, key=lambda x: x.final_score, reverse=True)[:n]


def generate_markdown_report(
    session_or_db,
    report_date: date | None = None,
    top_categories: int = 5,
) -> str:
    """Produce full markdown report. Accepts Session or Database."""
    from storage.database import Database
    if isinstance(session_or_db, Database):
        session = session_or_db.session()
    else:
        session = session_or_db
    report_date = report_date or date.today()

    # Pull hotness ranking
    heats = (
        session.query(SectorHeat)
        .filter(SectorHeat.trade_date == report_date)
        .order_by(SectorHeat.hotness_score.desc())
        .all()
    )

    # Detect mismatches
    mismatches = aggregate_mismatches(session, report_date)

    # Active high-strength signals
    signal_cutoff = report_date - timedelta(days=7)
    strong_signals = (
        session.query(KnowledgeSignal)
        .filter(KnowledgeSignal.phase == "active")
        .filter(KnowledgeSignal.strength >= 3.0)
        .filter(KnowledgeSignal.signal_date >= signal_cutoff.isoformat())
        .filter(KnowledgeSignal.signal_date <= report_date.isoformat())
        .order_by(KnowledgeSignal.strength.desc())
        .limit(15)
        .all()
    )

    # Recent news (top by recency × decay)
    recent_start, _ = utc_bounds_for_business_date(report_date - timedelta(days=2))
    _, recent_end = utc_bounds_for_business_date(report_date)
    recent_news = (
        session.query(NewsRaw)
        .filter(NewsRaw.published_at >= recent_start)
        .filter(NewsRaw.published_at < recent_end)
        .order_by(NewsRaw.published_at.desc())
        .limit(50)
        .all()
    )
    # Never let smoke-test fixtures or placeholder links enter a production
    # report, even when they are newer than real source material.
    recent_news = [
        n for n in recent_news
        if "example.com" not in (n.url or "")
        and "smoke" not in (n.source or "").lower()
    ]
    scored_news = sorted(
        recent_news,
        key=lambda n: weight_for_datetime(n.published_at) * (1.0 + (n.keywords_matched and 1 or 0)),
        reverse=True,
    )[:20]

    lines = []
    lines.append(f"# 📊 供应链错配日报 · {report_date}")
    lines.append("")
    lines.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Section 0: Upcoming 7-day events (top of report)
    try:
        from events import get_upcoming
        upcoming = get_upcoming(days_ahead=7, min_impact=3)
        if upcoming:
            lines.append("## 📅 未来 7 日重点事件")
            lines.append("")
            lines.append("| 日期 | 事件 | 行业 | 影响 | 类型 |")
            lines.append("|------|------|------|------|------|")
            for e in upcoming[:12]:
                days = (e.event_date - report_date).days
                days_label = "今天" if days == 0 else f"+{days}天"
                stars = "⭐" * e.impact_level
                lines.append(
                    f"| {e.event_date} ({days_label}) | {e.title} | "
                    f"{e.industry_label} | {stars} | {e.event_type} |"
                )
            lines.append("")
    except Exception as exc:
        logger.warning(f"Failed to add events to report: {exc}")

    # Section 1: Top categories by hotness
    lines.append("## 🔥 当日热度 TOP 5")
    lines.append("")
    if heats:
        lines.append("| 排名 | 类别 | 热度分 | 处理级别 | 关联股票数 |")
        lines.append("|------|------|--------|----------|------------|")
        for i, h in enumerate(heats[:top_categories], 1):
            lines.append(
                f"| {i} | {h.category_name} | {h.hotness_score:.2f} | "
                f"{'🎯深度' if h.processed_level == 'deep' else '📋浅度'} | "
                f"{h.n_stocks} |"
            )
    else:
        lines.append("_暂无热度数据_")
    lines.append("")

    # Section 2: Strong active signals
    lines.append("## 🚨 高强度活跃信号 (strength ≥ 3)")
    lines.append("")
    if strong_signals:
        from processor.event_boost import (
            compute_event_boost, get_stock_codes_for_signal,
        )
        lines.append("| 信号 | 方向 | 强度 | 等级 | 关联A股 | 📅临近事件 |")
        lines.append("|------|------|------|------|---------|-----------|")
        for s in strong_signals:
            stocks_list = [st.stock_code for st in s.stocks[:3]]
            codes = get_stock_codes_for_signal(session, s.id)
            boost = compute_event_boost(codes)
            ev_cell = "—"
            if boost.has_boost:
                pct = int(boost.boost_factor * 100)
                ev_cell = f"+{pct}% · {boost.matched_events[0].title[:24]}"
            lines.append(
                f"| {s.title[:40]} | {s.direction} | "
                f"{s.strength:.1f} | {s.grade} | "
                f"{','.join(stocks_list)} | {ev_cell} |"
            )
    else:
        lines.append("_暂无_")
    lines.append("")

    # Section 3: Supply-demand mismatches
    lines.append("## ⚖️ 供应链错配识别")
    lines.append("")
    if mismatches:
        tight = [m for m in mismatches if m.direction == "tight"][:8]
        surplus = [m for m in mismatches if m.direction == "surplus"][:5]
        if tight:
            lines.append("### 🔴 紧缺方向 (tight)")
            lines.append("")
            for m in tight:
                boost_tag = f" 📅+{int(m.event_boost*100)}%" if m.event_boost else ""
                lines.append(f"- **{m.category}** (强度{m.strength:.1f}{boost_tag}): {m.summary}")
                if m.event_titles:
                    lines.append(f"  - 📅 临近: {', '.join(m.event_titles[:2])}")
                for ev in m.evidence[:2]:
                    lines.append(f"  - {ev[:120]}")
            lines.append("")
        if surplus:
            lines.append("### 🟢 过剩方向 (surplus)")
            lines.append("")
            for m in surplus:
                boost_tag = f" 📅+{int(m.event_boost*100)}%" if m.event_boost else ""
                lines.append(f"- **{m.category}** (强度{m.strength:.1f}{boost_tag}): {m.summary}")
                if m.event_titles:
                    lines.append(f"  - 📅 临近: {', '.join(m.event_titles[:2])}")
                for ev in m.evidence[:2]:
                    lines.append(f"  - {ev[:120]}")
            lines.append("")
    else:
        lines.append("_暂无错配信号_")
    lines.append("")

    # Section 4: Top news
    lines.append("## 📰 重点新闻 (按时效×关键词加权)")
    lines.append("")
    if scored_news:
        for n in scored_news[:10]:
            kw = f" `[{n.keywords_matched}]`" if n.keywords_matched else ""
            age = (datetime.utcnow() - n.published_at).total_seconds() / 3600
            lines.append(
                f"- [{n.title}]({n.url}) _{n.source_label or n.source} · {age:.1f}h ago_{kw}"
            )
    else:
        lines.append("_暂无新闻_")
    lines.append("")

    # Footer: never claim sub-day latency when market data is stale.
    latest_stock = session.query(func.max(StockQuote.trade_date)).scalar()
    latest_future = session.query(func.max(FuturesPrice.trade_date)).scalar()
    stock_age = (report_date - latest_stock).days if latest_stock else None
    future_age = (report_date - latest_future).days if latest_future else None
    if stock_age is None or stock_age > 3 or future_age is None or future_age > 5:
        lines.append("> ⚠️ 数据质量：行情数据过期或缺失，本报告仅供观察，不构成交易建议。")
    else:
        lines.append("> 数据质量：行情与新闻新鲜度通过门禁。")
    lines.append("")
    lines.append("---")
    lines.append(f"_本报告由 Stock Analysis System 自动生成 · 股票行情={latest_stock or '缺失'} · 期货={latest_future or '缺失'}_")
    return "\n".join(lines)


def generate_feishu_payload(
    session_or_db,
    report_date: date | None = None,
    top_n: int = 5,
) -> dict:
    """Generate Feishu interactive card payload. Accepts Session or Database."""
    from storage.database import Database
    if isinstance(session_or_db, Database):
        session = session_or_db.session()
    else:
        session = session_or_db
    report_date = report_date or date.today()

    heats = (
        session.query(SectorHeat)
        .filter(SectorHeat.trade_date == report_date)
        .order_by(SectorHeat.hotness_score.desc())
        .limit(top_n)
        .all()
    )

    mismatches = aggregate_mismatches(session, report_date)
    top_mismatch = _top_n(mismatches, 3)

    strong_signals = (
        session.query(KnowledgeSignal)
        .filter(KnowledgeSignal.phase == "active")
        .filter(KnowledgeSignal.strength >= 3.0)
        .order_by(KnowledgeSignal.strength.desc())
        .limit(5)
        .all()
    )

    elements = []

    # Header summary
    summary_lines = [
        f"📅 **{report_date}** 供应链错配日报",
        f"🔥 热度 TOP{top_n} | ⚖️ 错配 {len(mismatches)} 个 | 🚨 强信号 {len(strong_signals)} 个",
    ]
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": "\n".join(summary_lines)},
    })

    # Upcoming 7-day events
    try:
        from events import get_upcoming
        upcoming = get_upcoming(days_ahead=7, min_impact=3)
        if upcoming:
            rows = []
            for e in upcoming[:8]:
                days = (e.event_date - report_date).days
                days_label = "今天" if days == 0 else f"+{days}d"
                stars = "⭐" * e.impact_level
                rows.append(
                    f"**{e.event_date}** ({days_label}) {e.title}  {stars} `{e.industry_label}`"
                )
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**📅 未来 7 日重点事件**\n" + "\n".join(rows),
                },
            })
    except Exception:
        pass

    # Hotness ranking
    if heats:
        rows = []
        for i, h in enumerate(heats, 1):
            badge = "🎯" if h.processed_level == "deep" else "📋"
            rows.append(
                f"**{i}.** {badge} {h.category_name} "
                f"(热度 {h.hotness_score:.2f} · {h.n_stocks}股)"
            )
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**🔥 当日热度 TOP**\n" + "\n".join(rows),
            },
        })

    # Mismatches
    if top_mismatch:
        rows = []
        for m in top_mismatch:
            icon = "🔴" if m.direction == "tight" else ("🟢" if m.direction == "surplus" else "🟡")
            rows.append(f"{icon} **{m.category}** ({m.direction}): {m.summary[:80]}")
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**⚖️ 错配识别**\n" + "\n".join(rows),
            },
        })

    # Strong signals
    if strong_signals:
        from processor.event_boost import (
            compute_event_boost, get_stock_codes_for_signal,
        )
        rows = []
        for s in strong_signals:
            stocks = ",".join([st.stock_code for st in s.stocks[:3]])
            codes = get_stock_codes_for_signal(session, s.id)
            boost = compute_event_boost(codes)
            ev_tag = f" 📅+{int(boost.boost_factor*100)}%" if boost.has_boost else ""
            rows.append(
                f"📈 **{s.grade}** {s.title[:50]} "
                f"强度 {s.strength:.1f}{ev_tag} · {stocks}"
            )
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**🚨 高强信号**\n" + "\n".join(rows),
            },
        })

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {
                    "tag": "plain_text",
                    "content": f"📊 供应链错配日报 · {report_date}",
                },
            },
            "elements": elements,
        },
        "_meta": {
            "n_signals": len(strong_signals),
            "n_mismatches": len(mismatches),
        },
    }
    return payload


def save_report(
    session_or_db,
    markdown: str,
    payload: dict,
    report_date: date | None = None,
    report_type: str = "full",
    *,
    n_signals: int = 0,
) -> DailyReport:
    """Persist the generated report. Accepts Session or Database."""
    from storage.database import Database
    if isinstance(session_or_db, Database):
        with session_or_db.tx() as session:
            rpt = _save_report_inner(
                session, markdown, payload, report_date, report_type, n_signals=n_signals,
            )
    else:
        rpt = _save_report_inner(
            session_or_db, markdown, payload, report_date, report_type, n_signals=n_signals,
        )
    # Invalidate cache for DailyReport queries
    try:
        from cache.redis_cache import get_cache
        cache = get_cache()
        cache.delete("report:latest")
        cache.delete(f"report:{report_date}")
    except Exception:
        pass
    return rpt


def _save_report_inner(
    session: Session,
    markdown: str,
    payload: dict,
    report_date: date | None = None,
    report_type: str = "full",
    *,
    n_signals: int = 0,
) -> DailyReport:
    from storage.models import NewsRaw, SectorHeat
    report_date = report_date or date.today()
    existing = (
        session.query(DailyReport)
        .filter(DailyReport.report_date == report_date)
        .filter(DailyReport.report_type == report_type)
        .first()
    )

    news_start, news_end = utc_bounds_for_business_date(report_date)
    n_news = session.query(NewsRaw).filter(
        NewsRaw.published_at >= news_start,
        NewsRaw.published_at < news_end,
    ).count()
    n_top_categories = session.query(SectorHeat).filter(
        SectorHeat.trade_date == report_date
    ).count()
    meta = (payload or {}).get("_meta", {})
    n_signals = meta.get("n_signals", n_signals)

    if existing:
        existing.markdown = markdown
        existing.payload_json = json.dumps(payload, ensure_ascii=False)
        existing.created_at = datetime.utcnow()
        existing.n_signals = n_signals
        existing.n_news = n_news
        existing.n_top_categories = n_top_categories
        session.flush()
        logger.info(f"Updated report {report_date}/{report_type}")
        return existing

    rpt = DailyReport(
        report_date=report_date,
        report_type=report_type,
        markdown=markdown,
        payload_json=json.dumps(payload, ensure_ascii=False),
        feishu_sent=False,
        n_signals=n_signals,
        n_news=n_news,
        n_top_categories=n_top_categories,
    )
    session.add(rpt)
    session.flush()
    logger.info(f"Saved report {report_date}/{report_type}")
    return rpt
