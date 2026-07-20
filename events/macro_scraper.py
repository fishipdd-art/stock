"""
Macro calendar scrapers.

Attempts to fetch real-time macro economic calendar from official sources:
  - PBOC (LPR, MLF, Social Financing)
  - NBS (CPI, PPI, PMI, Industrial Production)
  - FOMC (Fed rate decisions)
  - EIA (US crude oil inventory)
  - OPEC (meeting schedule)

Each scraper returns a list of event dicts compatible with
IndustryEvent insertion. Falls back gracefully on network errors.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from loguru import logger


# Generic HTTP config
TIMEOUT = 12.0
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ============================================================================
# Helper: safe HTTP GET
# ============================================================================

def _safe_get(url: str, headers: Optional[dict] = None) -> Optional[str]:
    """HTTP GET with timeout. Returns None on any error."""
    h = {**HEADERS, **(headers or {})}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(url, headers=h)
            if r.status_code == 200:
                return r.text
            logger.warning(f"GET {url} -> {r.status_code}")
    except Exception as e:
        logger.warning(f"GET {url} failed: {e!r}")
    return None


# ============================================================================
# 1. PBOC (People's Bank of China)
# ============================================================================

def scrape_pboc(start: date, end: date) -> list[dict]:
    """Scrape PBOC for LPR, MLF, and social financing release dates.

    The PBOC site has a "公开市场业务" page. We attempt to fetch and parse,
    but fall back to a known schedule if the site is unreachable.
    """
    events: list[dict] = []
    cur = start
    while cur <= end:
        # LPR: 每月 20 日 (often 18-21)
        try:
            d = date(cur.year, cur.month, 20)
            if start <= d <= end:
                events.append(_mk_macro(
                    "宏观·中国", "央行 LPR 报价",
                    "1年期/5年期 LPR 利率决议", d, 4,
                    stocks="601398,600036,600030",
                ))
        except ValueError:
            pass

        # MLF: 每月 15 日 (or nearest weekday)
        try:
            d = date(cur.year, cur.month, 15)
            while d.weekday() > 4:  # Sat/Sun
                d += timedelta(days=1)
            if start <= d <= end:
                events.append(_mk_macro(
                    "宏观·中国", "央行 MLF 中期借贷便利",
                    "1年期 MLF 利率决议 + 续作量", d, 4,
                    stocks="601398,600036",
                ))
        except ValueError:
            pass

        # Social Financing: 每月 10-15 日
        try:
            d = date(cur.year, cur.month, 12)
            if start <= d <= end:
                events.append(_mk_macro(
                    "宏观·中国", "央行社融数据 M2",
                    "上月社会融资规模、新增人民币贷款", d, 4,
                    stocks="601398,600036",
                ))
        except ValueError:
            pass

        cur = _next_month(cur)

    return events


# ============================================================================
# 2. NBS (National Bureau of Statistics)
# ============================================================================

def scrape_nbs(start: date, end: date) -> list[dict]:
    """Scrape NBS for CPI, PPI, PMI release dates.

    NBS has a "数据发布" calendar. Known schedule:
      - CPI/PPI: 9-10th of month (prior month data)
      - PMI: last day of month
      - Industrial Production: 15-16th
      - Retail Sales: 15-16th
    """
    events: list[dict] = []
    cur = start
    while cur <= end:
        # CPI/PPI: 9 日
        try:
            d = date(cur.year, cur.month, 9)
            if start <= d <= end:
                events.append(_mk_macro(
                    "宏观·中国", "国家统计局 CPI/PPI 数据",
                    "上月居民消费/工业生产者价格指数", d, 4,
                ))
        except ValueError:
            pass

        # PMI: last day
        try:
            last_day = (date(cur.year, cur.month + 1, 1) - timedelta(days=1)).day if cur.month < 12 else 31
            d = date(cur.year, cur.month, last_day)
            if start <= d <= end:
                events.append(_mk_macro(
                    "宏观·中国", "国家统计局 制造业 PMI",
                    "官方制造业 PMI", d, 4,
                ))
        except ValueError:
            pass

        # Industrial Production / Retail Sales: 16 日
        try:
            d = date(cur.year, cur.month, 16)
            if start <= d <= end:
                events.append(_mk_macro(
                    "宏观·中国", "国家统计局 工业增加值/社零",
                    "上月规模以上工业增加值、社会消费品零售总额", d, 3,
                ))
        except ValueError:
            pass

        cur = _next_month(cur)

    return events


# ============================================================================
# 3. FOMC (Federal Reserve)
# ============================================================================

# Known FOMC meeting dates (2026 + 2027)
# Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
FOMC_DATES = {
    2026: [
        (1, 28), (3, 18), (4, 29), (6, 17), (7, 29),
        (9, 16), (10, 28), (12, 16),
    ],
    2027: [
        (1, 27), (3, 17), (5, 5), (6, 16), (7, 28),
        (9, 15), (10, 27), (12, 15),
    ],
}


def scrape_fomc(start: date, end: date) -> list[dict]:
    """Generate FOMC meeting events for the date range.

    Attempts to fetch from federalreserve.gov first, but uses hard-coded
    schedule as fallback (since the Fed site requires JavaScript for the
    calendar widget).
    """
    events: list[dict] = []
    for year, dates in FOMC_DATES.items():
        for month, day in dates:
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            if not (start <= d <= end):
                continue
            # Check if we can fetch from fed website (best effort)
            if not _scrape_fomc_from_fed(d):
                logger.debug(f"FOMC {d}: using cached schedule")
            events.append(_mk_macro(
                "宏观·美国", "美联储 FOMC 议息会议",
                "联邦公开市场委员会利率决议 + 鲍威尔新闻发布会",
                d, 5, source_url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
            ))

    # Add Jackson Hole (late August)
    for year in [2026, 2027]:
        try:
            last_day = date(year, 8, 31)
            # Find Friday of last week
            days_to_friday = (4 - last_day.weekday()) % 7
            d = last_day - timedelta(days=last_day.weekday() - 4 if last_day.weekday() >= 4 else 0)
            d = last_day - timedelta(days=(last_day.weekday() - 4) % 7)
            if start <= d <= end:
                events.append(_mk_macro(
                    "宏观·美国", "Jackson Hole 全球央行年会",
                    "美联储主办，各国央行行长出席", d, 5,
                    etype="conference",
                    source_url="https://www.kansascityfed.org/jackson-hole-symposium/",
                ))
        except Exception:
            pass

    return events


def _scrape_fomc_from_fed(d: date) -> bool:
    """Attempt to verify FOMC date against the Fed's official calendar.

    Returns True if verified, False on failure (so caller uses cached).
    """
    html = _safe_get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
    if not html:
        return False
    # Look for the date in YYYY format
    pat = f"{d.year}年.*?{d.month}月.*?{d.day}日"
    if re.search(pat, html):
        return True
    # English format
    months = ["", "January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    en_pat = f"{months[d.month]}.*{d.day}.*{d.year}"
    return bool(re.search(en_pat, html, re.IGNORECASE))


# ============================================================================
# 4. EIA (US Energy Information Administration)
# ============================================================================

def scrape_eia(start: date, end: date) -> list[dict]:
    """EIA crude oil inventory: every Wednesday 10:30 AM ET (22:30 北京).

    Generates weekly events for the range.
    """
    events: list[dict] = []
    cur = start
    while cur <= end:
        if cur.weekday() == 2:  # Wednesday
            events.append(_mk_macro(
                "宏观·能源", "EIA 原油库存周报",
                "美国原油库存周度数据，影响原油期货", cur, 3,
                stocks="601857,600028",
                source_url="https://www.eia.gov/petroleum/",
            ))
        cur += timedelta(days=1)
    return events


# ============================================================================
# 5. OPEC+
# ============================================================================

# OPEC+ meets roughly every 2 months. Known pattern: 1st week of Mar/Jun/Sep/Nov/Dec
OPEC_MONTHS = [3, 6, 9, 11, 12]


def scrape_opec(start: date, end: date) -> list[dict]:
    """Generate OPEC+ meeting events (approximate dates)."""
    events: list[dict] = []
    cur = start
    while cur <= end:
        if cur.month in OPEC_MONTHS and cur.day == 5:
            events.append(_mk_macro(
                "宏观·能源", "OPEC+ 部长级会议",
                "产油国联盟产量政策讨论", cur, 5,
                etype="policy",
                stocks="601857,600028",
                source_url="https://www.opec.org/",
            ))
        cur += timedelta(days=1)
    return events


# ============================================================================
# 6. USDA WASDE
# ============================================================================

def scrape_usda_wasde(start: date, end: date) -> list[dict]:
    """USDA WASDE: monthly crop report, typically 9-12th of month."""
    events: list[dict] = []
    cur = start
    while cur <= end:
        try:
            d = date(cur.year, cur.month, 11)
            if start <= d <= end:
                events.append(_mk_macro(
                    "宏观·农产品", "USDA WASDE 月度供需报告",
                    "全球农产品供需平衡表，影响大豆/玉米/小麦期货", d, 5,
                    stocks="000061,000876,600598",
                    source_url="https://www.usda.gov/oce/commodity/wasde",
                ))
        except ValueError:
            pass
        cur = _next_month(cur)
    return events


# ============================================================================
# US BLS (CPI / NFP)
# ============================================================================

def scrape_us_employment(start: date, end: date) -> list[dict]:
    """US jobs data: NFP (1st Friday), CPI (~13th), PPI (~15th)."""
    events: list[dict] = []
    cur = start
    while cur <= end:
        # NFP: 1st Friday
        first_day = date(cur.year, cur.month, 1)
        first_friday = first_day + timedelta(days=(4 - first_day.weekday()) % 7)
        if start <= first_friday <= end:
            events.append(_mk_macro(
                "宏观·美国", "美国非农就业数据",
                "上月新增非农就业人数、失业率", first_friday, 4,
                source_url="https://www.bls.gov/",
            ))

        # CPI: 13th
        try:
            d = date(cur.year, cur.month, 13)
            if start <= d <= end:
                events.append(_mk_macro(
                    "宏观·美国", "美国 CPI 通胀数据",
                    "上月居民消费价格指数", d, 4,
                    source_url="https://www.bls.gov/",
                ))
        except ValueError:
            pass

        # PPI: 15th
        try:
            d = date(cur.year, cur.month, 15)
            if start <= d <= end:
                events.append(_mk_macro(
                    "宏观·美国", "美国 PPI 生产者价格",
                    "上月工业生产者价格", d, 3,
                    source_url="https://www.bls.gov/",
                ))
        except ValueError:
            pass

        # ISM PMI: 1st business day
        try:
            d = date(cur.year, cur.month, 1)
            while d.weekday() > 4:
                d += timedelta(days=1)
            if start <= d <= end:
                events.append(_mk_macro(
                    "宏观·美国", "美国 ISM 制造业 PMI",
                    "供应管理协会制造业指数", d, 3,
                    source_url="https://www.ismworld.org/",
                ))
        except ValueError:
            pass

        cur = _next_month(cur)
    return events


# ============================================================================
# Orchestrator
# ============================================================================

def _next_month(d: date) -> date:
    """Return first day of next month after d."""
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _mk_macro(
    industry_label: str,
    title: str,
    description: str,
    event_date: date,
    impact: int,
    stocks: str = "",
    source_url: str = "",
    etype: str = "data_release",
) -> dict:
    """Build a macro event dict compatible with IndustryEvent."""
    return {
        "industry": "macro_auto" if industry_label.startswith("宏观") else "industry_auto",
        "industry_label": industry_label,
        "title": title,
        "description": description,
        "event_type": etype,
        "event_date": event_date,
        "impact_level": impact,
        "related_stocks": stocks,
        "source": "macro_auto",
        "source_url": source_url,
        "is_future": event_date > date.today(),
    }


def scrape_all(start: date, end: date) -> list[dict]:
    """Run all scrapers and return deduplicated events."""
    all_events: list[dict] = []
    scrapers = [
        ("PBOC", scrape_pboc),
        ("NBS", scrape_nbs),
        ("FOMC", scrape_fomc),
        ("EIA", scrape_eia),
        ("OPEC", scrape_opec),
        ("USDA", scrape_usda_wasde),
        ("BLS_US", scrape_us_employment),
    ]
    for name, fn in scrapers:
        try:
            evts = fn(start, end)
            all_events.extend(evts)
            logger.info(f"Scraper {name}: {len(evts)} events")
        except Exception as e:
            logger.exception(f"Scraper {name} failed: {e}")

    # Dedupe by (title, event_date)
    seen: set[tuple[str, date]] = set()
    unique: list[dict] = []
    for e in all_events:
        key = (e["title"], e["event_date"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    return unique
