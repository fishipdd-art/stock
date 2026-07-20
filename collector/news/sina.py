"""
新浪财经 (Sina) news collector — backup source.

Endpoint: ``https://feed.mix.sina.com.cn/api/roll/get``
Params: ``pageid=153`` (财经 channel), ``lid`` selects the column, ``num``
controls page size, ``page`` the page index.

We pull two columns per run — 国内财经 (lid=1686) and 国际财经 (lid=2516) — to
broaden coverage. Times come back as unix-milliseconds.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from collector.news.base import (
    BaseNewsCollector,
    NewsItemDict,
    parse_unix_millis,
)


SINA_ROLL_API = "https://feed.mix.sina.com.cn/api/roll/get"

# (lid, label) — two complementary finance columns.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("1686", "财经-国内"),
    ("2516", "财经-国际"),
)

_PAGE_SIZE = 50
_MAX_PAGES = 2  # 100 items per column


class SinaCollector(BaseNewsCollector):
    """Collects 新浪财经 rolling news."""

    source = "sina"
    source_label = "新浪财经"

    def fetch(self, terms: list[str], hours_back: int) -> list[NewsItemDict]:
        from observability.metrics import ScraperTimer
        cutoff = datetime.utcnow() - timedelta(hours=max(1, int(hours_back)))
        results: list[NewsItemDict] = []

        with ScraperTimer(self.source):
            for lid, column_label in _COLUMNS:
                stream_results = self._fetch_column(lid, column_label, cutoff)
                self.logger.debug(
                    f"Sina column {lid} ({column_label}) got {len(stream_results)} items"
                )
                results.extend(stream_results)

        self.logger.info(f"Sina fetched {len(results)} items within {hours_back}h")
        return results

    # ------------------------------------------------------------------
    # One column — paginate
    # ------------------------------------------------------------------

    def _fetch_column(
        self,
        lid: str,
        column_label: str,
        cutoff: datetime,
    ) -> list[NewsItemDict]:
        results: list[NewsItemDict] = []
        seen_urls: set[str] = set()

        for page in range(1, _MAX_PAGES + 1):
            params = {
                "pageid": "153",
                "lid": lid,
                "num": str(_PAGE_SIZE),
                "page": str(page),
                "versionNumber": "1.2.4",
            }
            try:
                resp = self.request(
                    "GET",
                    SINA_ROLL_API,
                    throttle=1.0,
                    params=params,
                    headers={"Referer": "https://finance.sina.com.cn/"},
                )
                data = resp.json()
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                self.logger.warning(
                    f"Sina column {lid} page {page} request failed: {e!r}"
                )
                break
            except ValueError as e:
                self.logger.warning(
                    f"Sina column {lid} page {page} returned non-JSON: {e!r}"
                )
                break

            outer = data.get("result") or {}
            items = outer.get("data") or []
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
                ts = parse_unix_millis(raw.get("ctime"))
                if ts and (page_oldest is None or ts < page_oldest):
                    page_oldest = ts

            self.logger.debug(
                f"Sina lid={lid} page={page}: "
                f"{len(items)} received, {page_kept} kept, "
                f"oldest={page_oldest.isoformat() if page_oldest else 'n/a'}"
            )

            if page_oldest is not None and page_oldest < cutoff:
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
        url = (raw.get("url") or raw.get("wapurl") or "").strip()
        if not url or url in seen_urls:
            return False

        title = (raw.get("title") or "").strip()
        if not title:
            return False

        ts = parse_unix_millis(raw.get("ctime"))
        if ts is None:
            return False
        if ts < cutoff:
            return False

        seen_urls.add(url)
        intro = (raw.get("intro") or "").strip()
        media = (raw.get("media_name") or "").strip()

        results.append(
            NewsItemDict(
                url=url,
                title=title[:512],
                summary=intro[:4000],
                source=self.source,
                source_label=self.source_label or media,
                published_at=ts,
                content="",
            )
        )
        return True