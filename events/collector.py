"""
Industry events: macro calendar generator + curated event loader.

Auto-generates recurring macro events (FOMC, PBOC LPR, NBS CPI/PPI, USDA WASDE, EIA weekly, OPEC+).
Loads curated industry events from data/industry_events.json.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Iterable

from loguru import logger

from config.settings import settings
from storage import get_db
from storage.models import IndustryEvent


# ============================================================================
# A. Macro calendar — auto-generated
# ============================================================================

def _add_months(d: date, n: int) -> date:
    """Add n months to d, clamping to last day if needed."""
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    import calendar
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last))


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the n-th ``weekday`` of (year, month).

    weekday: 0=Mon ... 6=Sun (matches ``date.weekday()``)
    n:       1-based; ``n=5`` (or higher) returns the last occurrence,
             which is the standard "last weekday of the month" semantic.

    Example: ``_nth_weekday(2026, 1, 2, 3)`` = 3rd Wednesday of Jan 2026
    = 2026-01-21.

    ``_nth_weekday(2026, 2, 0, 5)`` (5th Monday of Feb) returns the 4th
    Monday = 2026-02-23, since only 4 Mondays fit.
    """
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    first = date(year, month, 1)
    # offset from the 1st to the first occurrence of `weekday`
    delta = (weekday - first.weekday()) % 7
    day = 1 + delta + (n - 1) * 7
    if day > last_day:
        # n is past the end of the month -> fall back to the last occurrence
        last = date(year, month, last_day)
        day = last_day - ((last.weekday() - weekday) % 7)
    return date(year, month, day)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Last ``weekday`` of (year, month)."""
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)
    delta = (last.weekday() - weekday) % 7
    return last - timedelta(days=delta)


# FOMC meeting pattern: 8 per year, on roughly the 3rd/4th Wednesday of
# every other month. The Fed publishes the actual schedule a year in
# advance; this rule approximates the pattern well enough for planning
# purposes and avoids the 2027-expiry bug of the old hard-coded list.
_FOMC_MONTHS = (1, 3, 5, 6, 7, 9, 11, 12)


def _fOMC_dates_in(year: int) -> list[date]:
    """Approximate FOMC decision-day dates for a calendar year.

    The Fed's actual schedule follows the pattern of "3rd Wednesday
    every other month, with one extra meeting in months that need it."
    Our approximation: each scheduled month contributes one FOMC date
    using the 3rd-Wednesday rule. For some real years the Fed deviates
    (e.g. 4th Wed), but for our purposes (pre-event reminder, news
    scraping) being within a few days is fine.
    """
    return [
        _nth_weekday(year, m, 2, 3)  # Wednesday = 2 in Python
        for m in _FOMC_MONTHS
    ]


def generate_macro_calendar(start: date, end: date) -> list[dict]:
    """Generate recurring macro events between start and end (inclusive)."""
    events: list[dict] = []
    seen: set[tuple[str, date]] = set()  # (title, date) dedup
    cur = date(start.year, start.month, 1)

    # Iterate month by month
    while cur <= end:
        ym = (cur.year, cur.month)

        # === China ===
        # PBOC LPR (每月 20 日)
        try:
            d = date(cur.year, cur.month, 20)
            if start <= d <= end:
                events.append(_ev(
                    industry="macro_china", industry_label="宏观·中国",
                    title="央行 LPR 报价",
                    desc="1年期/5年期 LPR 利率决议，影响房贷、实体融资",
                    etype="data_release", d=d, impact=4,
                    stocks="601398,600036,600030",
                    url="http://www.pbc.gov.cn/",
                ))
        except ValueError:
            pass

        # NBS CPI/PPI (9日)
        try:
            d = date(cur.year, cur.month, 9)
            if start <= d <= end:
                events.append(_ev(
                    industry="macro_china", industry_label="宏观·中国",
                    title="国家统计局 CPI/PPI 数据",
                    desc="上月居民消费/工业生产者价格指数",
                    etype="data_release", d=d, impact=4,
                    url="https://www.stats.gov.cn/",
                ))
        except ValueError:
            pass

        # NBS PMI (月底)
        try:
            last_day = (_add_months(date(cur.year, cur.month, 1), 0) - timedelta(days=1)).day
            d = date(cur.year, cur.month, last_day)
            if start <= d <= end:
                events.append(_ev(
                    industry="macro_china", industry_label="宏观·中国",
                    title="国家统计局 制造业 PMI",
                    desc="官方制造业 PMI",
                    etype="data_release", d=d, impact=4,
                    url="https://www.stats.gov.cn/",
                ))
        except ValueError:
            pass

        # PBOC Social Financing (12日)
        try:
            d = date(cur.year, cur.month, 12)
            if start <= d <= end:
                events.append(_ev(
                    industry="macro_china", industry_label="宏观·中国",
                    title="央行社融数据 M2",
                    desc="上月社会融资规模、新增人民币贷款",
                    etype="data_release", d=d, impact=4,
                    stocks="601398,600036",
                    url="http://www.pbc.gov.cn/",
                ))
        except ValueError:
            pass

        # Customs Trade Data (14日)
        try:
            d = date(cur.year, cur.month, 14)
            if start <= d <= end:
                events.append(_ev(
                    industry="macro_china", industry_label="宏观·中国",
                    title="海关进出口数据",
                    desc="上月进出口贸易数据",
                    etype="data_release", d=d, impact=3,
                    url="http://www.customs.gov.cn/",
                ))
        except ValueError:
            pass

        # NBS Industrial Production (16日)
        try:
            d = date(cur.year, cur.month, 16)
            if start <= d <= end:
                events.append(_ev(
                    industry="macro_china", industry_label="宏观·中国",
                    title="国家统计局 工业增加值/社零",
                    desc="上月规模以上工业增加值、社会消费品零售总额",
                    etype="data_release", d=d, impact=3,
                    url="https://www.stats.gov.cn/",
                ))
        except ValueError:
            pass

        # === US ===
        # FOMC (3rd Wednesday of every other month, computed for any year)
        for d in _fOMC_dates_in(cur.year):
            if start <= d <= end:
                events.append(_ev(
                    industry="macro_us", industry_label="宏观·美国",
                    title="美联储 FOMC 议息会议",
                    desc="联邦公开市场委员会利率决议 + 鲍威尔新闻发布会",
                    etype="data_release", d=d, impact=5,
                    url="https://www.federalreserve.gov/",
                ))

        # Jackson Hole (last Friday of August — annual symposium)
        if cur.month == 8:
            try:
                d = _last_weekday(cur.year, 8, 4)  # Friday = 4
                if start <= d <= end:
                    events.append(_ev(
                        industry="macro_us", industry_label="宏观·美国",
                        title="Jackson Hole 全球央行年会",
                        desc="美联储主办，各国央行行长出席",
                        etype="conference", d=d, impact=5,
                        url="https://www.kansascityfed.org/jackson-hole-symposium/",
                    ))
            except Exception:
                pass

        # US CPI (13日)
        try:
            d = date(cur.year, cur.month, 13)
            if start <= d <= end:
                events.append(_ev(
                    industry="macro_us", industry_label="宏观·美国",
                    title="美国 CPI 通胀数据",
                    desc="上月居民消费价格指数",
                    etype="data_release", d=d, impact=4,
                    url="https://www.bls.gov/",
                ))
        except ValueError:
            pass

        # US PPI (15日)
        try:
            d = date(cur.year, cur.month, 15)
            if start <= d <= end:
                events.append(_ev(
                    industry="macro_us", industry_label="宏观·美国",
                    title="美国 PPI 生产者价格",
                    desc="上月工业生产者价格",
                    etype="data_release", d=d, impact=3,
                    url="https://www.bls.gov/",
                ))
        except ValueError:
            pass

        # US Non-Farm Payrolls (first Friday)
        try:
            d = _nth_weekday(cur.year, cur.month, 4, 1)  # 1st Friday
            if start <= d <= end:
                events.append(_ev(
                    industry="macro_us", industry_label="宏观·美国",
                    title="美国非农就业数据",
                    desc="上月新增非农就业人数、失业率",
                    etype="data_release", d=d, impact=4,
                    url="https://www.bls.gov/",
                ))
        except ValueError:
            pass

        # US ISM PMI (first business day)
        try:
            d = date(cur.year, cur.month, 1)
            while d.weekday() > 4:
                d += timedelta(days=1)
            if start <= d <= end:
                events.append(_ev(
                    industry="macro_us", industry_label="宏观·美国",
                    title="美国 ISM 制造业 PMI",
                    desc="供应管理协会制造业指数",
                    etype="data_release", d=d, impact=3,
                    url="https://www.ismworld.org/",
                ))
        except ValueError:
            pass

        # === Commodities ===
        # USDA WASDE (11日)
        try:
            d = date(cur.year, cur.month, 11)
            if start <= d <= end:
                events.append(_ev(
                    industry="agriculture_global", industry_label="宏观·农产品",
                    title="USDA WASDE 月度供需报告",
                    desc="全球农产品供需平衡表，影响大豆/玉米/小麦期货",
                    etype="data_release", d=d, impact=5,
                    stocks="000061,000876,600598",
                    url="https://www.usda.gov/oce/commodity/wasde",
                ))
        except ValueError:
            pass

        # EIA Crude Oil Inventories (every Wednesday)
        wed = date(cur.year, cur.month, 1)
        while wed.month == cur.month:
            if wed.weekday() == 2 and start <= wed <= end:
                events.append(_ev(
                    industry="energy_global", industry_label="宏观·能源",
                    title="EIA 原油库存周报",
                    desc="美国原油库存周度数据，影响原油期货",
                    etype="data_release", d=wed, impact=3,
                    stocks="601857,600028",
                    url="https://www.eia.gov/petroleum/",
                ))
            wed += timedelta(days=1)

        # OPEC+ meetings (approximate quarterly: 1st week of Mar/Jun/Sep/Nov/Dec)
        if cur.month in [3, 6, 9, 11, 12] and cur.day <= 7:
            d = date(cur.year, cur.month, 5)  # mid-first-week
            if start <= d <= end:
                events.append(_ev(
                    industry="energy_global", industry_label="宏观·能源",
                    title="OPEC+ 部长级会议",
                    desc="产油国联盟产量政策讨论",
                    etype="policy", d=d, impact=5,
                    stocks="601857,600028",
                    url="https://www.opec.org/",
                ))

        cur = _add_months(date(cur.year, cur.month, 1), 1)

    # Dedup by (title, date) — FOMC/OPEC+ checks can fire multiple times
    unique: dict[tuple[str, date], dict] = {}
    for e in events:
        key = (e["title"], e["event_date"])
        if key not in unique:
            unique[key] = e
    return list(unique.values())


def _ev(
    industry: str,
    industry_label: str,
    title: str,
    desc: str,
    etype: str,
    d: date,
    impact: int,
    stocks: str = "",
    url: str = "",
) -> dict:
    return {
        "industry": industry,
        "industry_label": industry_label,
        "title": title,
        "description": desc,
        "event_type": etype,
        "event_date": d,
        "impact_level": impact,
        "related_stocks": stocks,
        "source": "macro_auto",
        "source_url": url,
        "is_future": d > date.today(),
    }


# ============================================================================
# B & C. Curated events (from data/industry_events.json)
# ============================================================================

def load_curated_events() -> list[dict]:
    json_path = settings.data_dir / "industry_events.json"
    if not json_path.exists():
        logger.warning(f"Curated events file not found: {json_path}")
        return []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        events = data.get("events", [])
        for e in events:
            if isinstance(e.get("event_date"), str):
                e["event_date"] = date.fromisoformat(e["event_date"])
            e.setdefault("is_future", e.get("event_date", date.today()) > date.today())
            e.setdefault("source", "curated")
            e.setdefault("impact_level", 3)
        return events
    except Exception as exc:
        logger.error(f"Failed to load curated events: {exc}")
        return []


# ============================================================================
# Public API
# ============================================================================

def collect_all_events(start: date, end: date) -> list[dict]:
    macro = generate_macro_calendar(start, end)
    curated = load_curated_events()
    out = macro + curated
    out = [e for e in out if start <= e.get("event_date", date.min) <= end]
    return out


def upsert_events(events: list[dict]) -> int:
    if not events:
        return 0
    db = get_db()
    written = 0
    with db.tx() as session:
        for e in events:
            ev_date = e.get("event_date")
            if not isinstance(ev_date, date):
                continue
            existing = (
                session.query(IndustryEvent)
                .filter(IndustryEvent.title == e["title"])
                .filter(IndustryEvent.event_date == ev_date)
                .first()
            )
            if existing:
                existing.description = e.get("description", existing.description)
                existing.impact_level = e.get("impact_level", existing.impact_level)
                existing.related_stocks = e.get("related_stocks", existing.related_stocks)
                existing.source = e.get("source", existing.source)
                existing.source_url = e.get("source_url", existing.source_url)
                existing.is_future = e.get("is_future", existing.is_future)
                existing.updated_at = datetime.utcnow()
            else:
                row = IndustryEvent(
                    industry=e.get("industry", "unknown"),
                    industry_label=e.get("industry_label", ""),
                    title=e["title"],
                    description=e.get("description", ""),
                    event_type=e.get("event_type", "other"),
                    event_date=ev_date,
                    impact_level=e.get("impact_level", 3),
                    related_stocks=e.get("related_stocks", ""),
                    source=e.get("source", "unknown"),
                    source_url=e.get("source_url", ""),
                    is_future=e.get("is_future", ev_date > date.today()),
                )
                session.add(row)
                written += 1
    return written


def refresh_events(start: date | None = None, end: date | None = None) -> int:
    if start is None:
        start = date.today() - timedelta(days=365)
    if end is None:
        end = date.today() + timedelta(days=180)
    events = collect_all_events(start, end)
    n = upsert_events(events)
    logger.info(f"Refreshed events: {n} new, total in window: {len(events)}")
    return n


def get_upcoming(days_ahead: int = 7, min_impact: int = 3, industries: list[str] | None = None) -> list[IndustryEvent]:
    db = get_db()
    today = date.today()
    end = today + timedelta(days=days_ahead)
    with db.session() as s:
        q = s.query(IndustryEvent).filter(
            IndustryEvent.event_date >= today,
            IndustryEvent.event_date <= end,
            IndustryEvent.impact_level >= min_impact,
        )
        if industries:
            q = q.filter(IndustryEvent.industry.in_(industries))
        return q.order_by(IndustryEvent.event_date.asc(), IndustryEvent.impact_level.desc()).all()


def get_events(
    start: date | None = None,
    end: date | None = None,
    industries: list[str] | None = None,
    min_impact: int = 1,
    future_only: bool | None = None,
    limit: int = 200,
) -> list[IndustryEvent]:
    db = get_db()
    with db.session() as s:
        q = s.query(IndustryEvent)
        if start:
            q = q.filter(IndustryEvent.event_date >= start)
        if end:
            q = q.filter(IndustryEvent.event_date <= end)
        if industries:
            q = q.filter(IndustryEvent.industry.in_(industries))
        if min_impact > 1:
            q = q.filter(IndustryEvent.impact_level >= min_impact)
        if future_only is not None:
            q = q.filter(IndustryEvent.is_future == future_only)
        return q.order_by(IndustryEvent.event_date.asc()).limit(limit).all()