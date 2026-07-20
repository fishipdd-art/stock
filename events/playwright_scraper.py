"""
Playwright-based scrapers for JS-rendered sites.

Some sites (PBOC, NBS) render content via JavaScript, so httpx can't
extract the calendar directly. This module uses Playwright to render
the page in headless Chromium and extract the data.

Auto-fallback: if Playwright is not installed, scrapers return []
and the system uses the hard-coded fallback schedule.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Optional

from loguru import logger

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    logger.warning("Playwright not installed, JS-rendered scrapers disabled")


# Config
TIMEOUT_MS = 15000
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def _ensure_browser():
    """Context manager: returns (playwright, browser) or raises."""
    if not HAS_PLAYWRIGHT:
        return None, None
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    return pw, browser


def _parse_chinese_date(text: str) -> Optional[date]:
    """Extract YYYY年MM月DD日 from text."""
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except (ValueError, OverflowError):
            pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except (ValueError, OverflowError):
            pass
    return None


# ============================================================================
# 1. PBOC LPR / MLF Calendar
# ============================================================================

def scrape_pboc_playwright() -> list[dict]:
    """Scrape PBOC for LPR release dates from official site.

    PBOC's monetary policy page has the schedule. Falls back gracefully
    if Playwright not available or site is unreachable.
    """
    if not HAS_PLAYWRIGHT:
        return []
    events: list[dict] = []
    pw, browser = _ensure_browser()
    if not browser:
        return []
    try:
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        page.goto(
            "http://www.pbc.gov.cn/zhengwugongkai/4081330/4081344/4081395/4081691/index.html",
            timeout=TIMEOUT_MS,
        )
        page.wait_for_timeout(2000)  # let JS render

        # Extract any date + LPR/MLF mention
        content = page.content()
        # Find dates with surrounding text
        for match in re.finditer(
            r"(\d{4}年\d{1,2}月\d{1,2}日)(.{0,80})(LPR|MLF|中期借贷便利|贷款市场报价利率)",
            content,
        ):
            d = _parse_chinese_date(match.group(1))
            if d and d >= date.today() - timedelta(days=30):
                events.append(_mk_macro_event(
                    "宏观·中国",
                    f"央行 {match.group(3)}",
                    f"PBOC 官方发布: {match.group(1)}",
                    d, 4,
                    source_url="http://www.pbc.gov.cn/",
                ))
        context.close()
    except Exception as e:
        logger.warning(f"PBOC Playwright scrape failed: {e}")
    finally:
        browser.close()
        try:
            pw.stop()
        except Exception:
            pass
    return events


# ============================================================================
# 2. NBS Data Release Calendar
# ============================================================================

def scrape_nbs_playwright() -> list[dict]:
    """Scrape NBS for data release calendar from official site."""
    if not HAS_PLAYWRIGHT:
        return []
    events: list[dict] = []
    pw, browser = _ensure_browser()
    if not browser:
        return []
    try:
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        page.goto(
            "https://www.stats.gov.cn/sj/sjjd/",
            timeout=TIMEOUT_MS,
        )
        page.wait_for_timeout(2000)

        content = page.content()
        # Look for date + data type mentions
        keywords = {
            "CPI": ("国家统计局 CPI 数据", 4),
            "PPI": ("国家统计局 PPI 数据", 4),
            "PMI": ("国家统计局 PMI 数据", 4),
            "工业增加值": ("国家统计局 工业增加值", 3),
            "社会消费品零售": ("国家统计局 社零总额", 3),
            "GDP": ("国家统计局 GDP 数据", 5),
        }
        for match in re.finditer(
            r"(\d{4}年\d{1,2}月\d{1,2}日)(.{0,100})", content
        ):
            d = _parse_chinese_date(match.group(1))
            if not d or d < date.today() - timedelta(days=30):
                continue
            surrounding = match.group(2)
            for kw, (title, impact) in keywords.items():
                if kw in surrounding:
                    events.append(_mk_macro_event(
                        "宏观·中国", title, f"NBS: {match.group(1)}",
                        d, impact,
                        source_url="https://www.stats.gov.cn/sj/sjjd/",
                    ))
                    break
        context.close()
    except Exception as e:
        logger.warning(f"NBS Playwright scrape failed: {e}")
    finally:
        browser.close()
        try:
            pw.stop()
        except Exception:
            pass
    return events


# ============================================================================
# Helper
# ============================================================================

def _mk_macro_event(
    industry_label: str, title: str, description: str,
    event_date: date, impact: int, source_url: str = "",
) -> dict:
    return {
        "industry": "macro_auto",
        "industry_label": industry_label,
        "title": title,
        "description": description,
        "event_type": "data_release",
        "event_date": event_date,
        "impact_level": impact,
        "related_stocks": "",
        "source": "macro_auto_playwright",
        "source_url": source_url,
        "is_future": event_date > date.today(),
    }


def scrape_all_playwright() -> list[dict]:
    """Run all Playwright scrapers and return deduped events."""
    all_events: list[dict] = []
    scrapers = [
        ("PBOC", scrape_pboc_playwright),
        ("NBS", scrape_nbs_playwright),
    ]
    for name, fn in scrapers:
        try:
            evts = fn()
            all_events.extend(evts)
            logger.info(f"Playwright scraper {name}: {len(evts)} events")
        except Exception as e:
            logger.warning(f"Playwright scraper {name} failed: {e}")

    seen: set[tuple[str, date]] = set()
    unique: list[dict] = []
    for e in all_events:
        key = (e["title"], e["event_date"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    return unique
