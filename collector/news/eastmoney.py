"""
东方财富 (EastMoney) news collector.

Primary endpoint: ``https://np-listapi.eastmoney.com/comm/wap/getListInfo``.
This mobile list API returns paginated news for a category, with each item
exposing title, abstract, source URL and publication time.

We fetch two streams per run — 财经要闻 (type=1) and 行业新闻 (type=2) — to
cover both macro headlines and sector-specific stories. Multiple pages are
walked back until the lookback horizon is reached.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from collector.news.base import (
    BaseNewsCollector,
    NewsItemDict,
    parse_time_string,
)


EAST_MONEY_LIST_API = "https://np-listapi.eastmoney.com/comm/wap/getListInfo"
EAST_MONEY_SEARCH_API = "https://search-api-web.eastmoney.com/search/jsonp"

# (type, label) pairs. ``type`` matches EastMoney's `type` query param.
_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("1", "财经要闻"),
    ("2", "行业新闻"),
)

_PAGE_SIZE = 50
_MAX_PAGES = 4  # 4 * 50 = 200 items per stream
_PER_STREAM_MAX = 150


class EastMoneyCollector(BaseNewsCollector):
    """Collects sector + headline news from 东方财富."""

    source = "eastmoney"
    source_label = "东方财富"

    def fetch(self, terms: list[str], hours_back: int) -> list[NewsItemDict]:
        from observability.metrics import ScraperTimer
        cutoff = datetime.utcnow() - timedelta(hours=max(1, int(hours_back)))
        results: list[NewsItemDict] = []

        with ScraperTimer(self.source):
            for type_code, type_label in _CATEGORIES:
                stream_results = self._fetch_stream(type_code, type_label, cutoff)
                self.logger.debug(
                    f"EastMoney stream type={type_code} ({type_label}) "
                    f"got {len(stream_results)} items"
                )
                results.extend(stream_results)

        self.logger.info(
            f"EastMoney fetched {len(results)} items within {hours_back}h"
        )
        return results

    # ------------------------------------------------------------------
    # One stream (one type) — walk back through pages
    # ------------------------------------------------------------------

    def _fetch_stream(
        self,
        type_code: str,
        type_label: str,
        cutoff: datetime,
    ) -> list[NewsItemDict]:
        results: list[NewsItemDict] = []
        seen_urls: set[str] = set()

        for page in range(1, _MAX_PAGES + 1):
            params = {
                "client": "wap",
                "type": type_code,
                "mTypeAndCode": "",
                "pageSize": str(_PAGE_SIZE),
                "pageIndex": str(page),
            }
            try:
                resp = self.request(
                    "GET",
                    EAST_MONEY_LIST_API,
                    throttle=1.0,
                    params=params,
                    headers={"Referer": "https://wap.eastmoney.com/"},
                )
                data = resp.json()
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                self.logger.warning(
                    f"EastMoney stream type={type_code} page={page} request failed: {e!r}"
                )
                break
            except ValueError as e:
                self.logger.warning(
                    f"EastMoney stream type={type_code} page={page} "
                    f"returned non-JSON: {e!r}"
                )
                break

            items = ((data.get("data") or {}).get("list")) or []
            if not isinstance(items, list) or not items:
                break

            page_kept = 0
            page_oldest: datetime | None = None
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                kept = self._extract_item(raw, results, cutoff, seen_urls)
                if kept:
                    page_kept += 1
                ts = parse_time_string(raw.get("Art_ShowTime"))
                if ts and (page_oldest is None or ts < page_oldest):
                    page_oldest = ts

            self.logger.debug(
                f"EastMoney type={type_code} page={page}: "
                f"{len(items)} received, {page_kept} kept, "
                f"oldest={page_oldest.isoformat() if page_oldest else 'n/a'}"
            )

            # Stop paging this stream once the entire page is older than cutoff
            # or we've hit the per-stream cap.
            if page_oldest is not None and page_oldest < cutoff:
                break
            if len(results) >= _PER_STREAM_MAX:
                break
            time.sleep(0.3)

        return results

    # ------------------------------------------------------------------
    # Item extraction
    # ------------------------------------------------------------------

    def _extract_item(
        self,
        raw: dict[str, Any],
        results: list[NewsItemDict],
        cutoff: datetime,
        seen_urls: set[str],
    ) -> bool:
        url = (raw.get("Art_Url") or "").strip()
        if not url or url in seen_urls:
            return False
        title = (raw.get("Art_Title") or "").strip()
        if not title:
            return False

        ts = parse_time_string(raw.get("Art_ShowTime"))
        if ts is None:
            # Without a timestamp we can't honour the lookback — skip.
            return False
        if ts < cutoff:
            return False

        seen_urls.add(url)
        abstract = (raw.get("Art_Abstract") or "").strip()
        media = (raw.get("Art_MediaName") or "").strip()

        results.append(
            NewsItemDict(
                url=url,
                title=title[:512],
                summary=abstract[:4000],
                source=self.source,
                source_label=self.source_label or media,
                published_at=ts,
                content="",
            )
        )
        return True