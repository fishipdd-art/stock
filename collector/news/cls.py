"""
财联社 (CLS) flash-news collector.

Source: m.cls.cn/telegraph — Next.js SSR page.
The mobile telegraph page embeds the latest ~20 telegraphs as a JSON payload
inside ``<script id="__NEXT_DATA__">…</script>``. We extract
``props.initialState.roll_data`` and convert each item into a NewsItemDict.

Why this endpoint and not the desktop API:
  - Desktop /nodeapi/updateTelegraphList has been 404 since CLS removed public
    API access in favour of their app.
  - All desktop nodeapi paths now 302-redirect to s.cls.cn/openapp/open.html.
  - m.cls.cn/telegraph returns 200 with full telegraph data server-rendered.

Limitations:
  - No pagination: every fetch returns the latest 20 telegraphs regardless of
    query parameters. Historical backfill beyond the homepage is not
    possible via the public site. Downstream collectors (Sina / EastMoney /
    Tavily) cover that gap.
  - URL-dedupe in the orchestrator keeps re-runs from inserting duplicates.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

import httpx

from collector.news.base import (
    BaseNewsCollector,
    NewsItemDict,
    parse_unix_seconds,
    strip_html,
)


# Desktop endpoint kept for documentation / future restoration. Currently 404.
CLS_TELEGRAPH_API = "https://www.cls.cn/nodeapi/updateTelegraphList"
# Mobile telegraph listing — the only public surface that still serves data.
CLS_TELEGRAPH_PAGE = "https://m.cls.cn/telegraph"
CLS_DETAIL_URL = "https://www.cls.cn/detail/{id}"

# Approximate count of telegraphs CLS serves per SSR pass. Verify against the
# live payload; if CLS changes the layout, _parse_next_data() will log a
# warning and return [].
_EXPECTED_ROLL_LEN = 20


class CLSCollector(BaseNewsCollector):
    """Collects 财联社 flash news via m.cls.cn SSR payload."""

    source = "cls"
    source_label = "财联社"

    # Accept either Next.js's modern inline ``<script>__NEXT_DATA__ = {...}</script>``
    # or the legacy ``<script id="__NEXT_DATA__">`` form.
    _NEXT_DATA_RE = re.compile(
        r'__NEXT_DATA__\s*=\s*(\{)',
    )

    def fetch(self, terms: list[str], hours_back: int) -> list[NewsItemDict]:
        from observability.metrics import ScraperTimer

        cutoff = datetime.utcnow() - timedelta(hours=max(1, int(hours_back)))
        results: list[NewsItemDict] = []

        with ScraperTimer(self.source):
            try:
                resp = self.request(
                    "GET",
                    CLS_TELEGRAPH_PAGE,
                    throttle=2.0,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                            "Version/17.0 Mobile/15E148 Safari/604.1"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://m.cls.cn/",
                    },
                )
            except Exception as e:
                self.logger.warning(f"CLS mobile page request failed: {e!r}")
                return results

            roll_data = self._parse_next_data(resp.text)
            if not roll_data:
                self.logger.warning("CLS mobile page returned no roll_data")
                return results

            for item in roll_data:
                kept = self._extract_item(item, results, cutoff)
                _ = kept  # each call appends in-place; nothing to track

        self.logger.info(
            f"CLS fetched {len(results)} telegraphs from {CLS_TELEGRAPH_PAGE}"
        )
        return results

    # ------------------------------------------------------------------
    # Payload extraction
    # ------------------------------------------------------------------

    def _parse_next_data(self, html: str) -> list[dict[str, Any]]:
        """Extract props.initialState.roll_data from the SSR ``__NEXT_DATA__``.

        Returns an empty list on any failure (no exception propagates).
        """
        m = self._NEXT_DATA_RE.search(html)
        if not m:
            self.logger.warning("CLS: __NEXT_DATA__ assignment not found")
            return []

        # Brace-count to capture the full object — Next.js inlines the whole
        # payload as a JS assignment, not strict JSON.
        start = m.start(1)
        depth = 0
        in_str = False
        esc = False
        end = start
        for i in range(start, len(html)):
            c = html[i]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        raw = html[start : end + 1]

        # Next.js uses bareword `undefined`; strict JSON rejects it.
        try:
            payload = json.loads(re.sub(r"\bundefined\b", "null", raw))
        except json.JSONDecodeError as e:
            self.logger.warning(f"CLS: malformed __NEXT_DATA__ payload: {e!r}")
            return []

        try:
            roll = payload["props"]["initialState"]["roll_data"]
        except (KeyError, TypeError):
            self.logger.warning("CLS: roll_data missing from __NEXT_DATA__")
            return []

        if not isinstance(roll, list):
            return []
        return roll

    # ------------------------------------------------------------------
    # Item extraction
    # ------------------------------------------------------------------

    def _extract_item(
        self,
        item: dict[str, Any],
        results: list[NewsItemDict],
        cutoff: datetime,
    ) -> bool:
        ts = parse_unix_seconds(item.get("ctime"))
        if ts is None or ts < cutoff:
            return False

        item_id = item.get("id")
        if not item_id:
            return False
        url = CLS_DETAIL_URL.format(id=item_id)

        title = strip_html(item.get("title") or "")
        content = strip_html(item.get("content") or "")
        brief = strip_html(item.get("brief") or "")

        if not title and not content:
            return False
        if not title:
            title = content[:80]

        summary = brief or (content if content != title else "")

        results.append(
            NewsItemDict(
                url=url,
                title=title[:512],
                summary=summary[:4000],
                source=self.source,
                source_label=self.source_label,
                published_at=ts,
                content=content[:8000],
            )
        )
        return True

    # ------------------------------------------------------------------
    # Item extraction
    # ------------------------------------------------------------------

    def _extract_item(
        self,
        item: dict[str, Any],
        results: list[NewsItemDict],
        cutoff: datetime,
    ) -> bool:
        """Convert one API record to a :class:`NewsItemDict` and append it.

        Returns True if the item was kept.
        """
        ts = parse_unix_seconds(item.get("ctime"))
        if ts is None or ts < cutoff:
            return False

        item_id = item.get("id")
        if not item_id:
            return False
        url = CLS_DETAIL_URL.format(id=item_id)

        title = strip_html(item.get("title") or "")
        content = strip_html(item.get("content") or "")
        brief = strip_html(item.get("brief") or "")

        if not title and not content:
            return False
        if not title:
            title = content[:80]

        summary = brief or (content if content != title else "")

        results.append(
            NewsItemDict(
                url=url,
                title=title[:512],
                summary=summary[:4000],
                source=self.source,
                source_label=self.source_label,
                published_at=ts,
                content=content[:8000],
            )
        )
        return True