"""Tavily web-search news collector.

Backstops the CLS / EastMoney blind spots with a single composite query per
``fetch()`` call so the 1000-credit / month free tier lasts.

Unlike CLS / EastMoney / Sina, Tavily's Python SDK owns its own HTTP session
so we deliberately bypass :meth:`BaseNewsCollector.request` and call
``TavilyClient.search`` directly. The quota counter is persisted in
``tavily_quota_log`` so manual CLI invocations, Dify pipeline runs and
cron-triggered jobs all share a single 20-call daily cap.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any

from loguru import logger

from collector.news.base import BaseNewsCollector, NewsItemDict, parse_time_string
from config.settings import settings
from storage.database import get_db
from storage.models import TavilyQuotaLog


class TavilyCollector(BaseNewsCollector):
    """Tavily-backed web-search collector.

    Issues ONE composite search per ``fetch()`` and returns the top-N most
    relevant results that are not older than ``hours_back``. Silently returns
    ``[]`` if the API key is missing, the daily cap is exhausted, or the API
    errors out — never raises.
    """

    source = "tavily"
    source_label = "Tavily"

    # 20 calls / day is a self-imposed cap well under the 1000/month quota
    # (≈ 5 calls/day automatic + ample headroom for manual CLI runs).
    DAILY_CAP = 20
    MAX_RESULTS = 10
    # Top-N priority terms joined into one composite query.
    QUERY_TERM_LIMIT = 8
    # Snippet length stored in news_raw.summary (Tavily's `content` field).
    SUMMARY_MAX_CHARS = 500

    # Event-type queries that the KG (company / category terms) doesn't cover.
    # Tavily runs a separate composite query for these alongside the main
    # KG-term query; both are subject to the same daily cap and the same
    # orchestrator-level keyword filter downstream.
    EVENT_QUERIES: tuple[str, ...] = (
        # Price / cost events
        "提价 涨价 调价 提价函 涨价函 价格上调",
        "降价 促销 降价补贴 价格战 价格下调",
        # Capacity / supply events
        "扩产 增产 产能扩张 新建产能 投产 达产",
        "减产 限产 停产 检修 关停",
        # Corporate finance events
        "回购 股份回购 注销回购",
        "股权激励 员工持股计划",
        "分红 派息 特别分红 高送转",
        # Earnings events
        "业绩超预期 业绩预增 业绩翻倍 业绩大增",
        "业绩不及预期 业绩预减 业绩下滑",
        "扭亏 为盈 摘帽",
        # M&A and capital
        "并购 收购 重组 借壳",
        "分拆上市 子公司上市",
        # Policy / regulatory
        "政策利好 行业扶持 补贴政策",
        "监管处罚 立案调查 警示函",
    )

    def __init__(self) -> None:
        super().__init__()  # validates source/source_label, binds self.logger
        self._client: Any = None

    # ------------------------------------------------------------------
    # Tavily client (lazy)
    # ------------------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            from tavily import TavilyClient

            if not settings.tavily_api_key:
                raise RuntimeError("TAVILY_API_KEY is not configured")
            self._client = TavilyClient(api_key=settings.tavily_api_key)
        return self._client

    # ------------------------------------------------------------------
    # Quota accounting (DB-backed)
    # ------------------------------------------------------------------
    @staticmethod
    def _today() -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    def _quota_used_today(self) -> int:
        with get_db().session() as s:
            row = s.get(TavilyQuotaLog, self._today())
            return int(row.calls_used) if row else 0

    def _quota_remaining(self) -> int:
        return max(0, self.DAILY_CAP - self._quota_used_today())

    def _increment_quota(self) -> None:
        today = self._today()
        now = datetime.utcnow()
        with get_db().tx() as s:
            row = s.get(TavilyQuotaLog, today)
            if row is None:
                s.add(TavilyQuotaLog(call_date=today, calls_used=1, updated_at=now))
            else:
                row.calls_used = int(row.calls_used) + 1
                row.updated_at = now
        # Track in-memory for budget gating within a single fetch.
        self._fetch_credits_spent = getattr(self, "_fetch_credits_spent", 0) + 1

    # ------------------------------------------------------------------
    # Query construction
    # ------------------------------------------------------------------
    @staticmethod
    def _build_query(terms: list[str], hours_back: int) -> str:
        """Compose a single OR-joined query from the top-N priority terms.

        The orchestrator has already filtered ``terms`` by priority, so the
        first ``QUERY_TERM_LIMIT`` entries are the highest-signal terms for
        this run.
        """
        picked = [t for t in terms[: TavilyCollector.QUERY_TERM_LIMIT] if t]
        if not picked:
            return ""
        # Tavily handles recency ranking on its own; no explicit date token
        # needed because we drop stale results server-side via published_date.
        return " OR ".join(picked)

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------
    def fetch(self, terms: list[str], hours_back: int) -> list[NewsItemDict]:
        from observability.metrics import ScraperTimer

        if not settings.tavily_api_key:
            self.logger.warning("TAVILY_API_KEY missing; skipping TavilyCollector")
            return []

        # Snapshot DB-side usage at fetch start, then track credits burned
        # in-memory. This way a single fetch never burns more than 2 credits
        # regardless of how many parallel queries it issues.
        base_used = self._quota_used_today()
        budget = self.DAILY_CAP - base_used
        if budget <= 0:
            self.logger.warning(
                f"Tavily daily cap ({self.DAILY_CAP}) exhausted; skipping"
            )
            return []
        self._fetch_credits_spent = 0

        items: list[NewsItemDict] = []

        kg_query = self._build_query(terms, hours_back)
        event_query = self._build_event_query()

        with ScraperTimer(self.source):
            if kg_query and self._fetch_credits_spent < budget:
                items.extend(self._run_query(kg_query, hours_back, label="kg"))

            if event_query and self._fetch_credits_spent < budget:
                items.extend(
                    self._run_query(event_query, hours_back, label="event")
                )

        # Local dedupe by URL — orchestrator's _dedupe will run again, but
        # doing it here keeps quota usage sane if a fetch happens to repeat
        # the same headline from two parallel queries.
        seen: set[str] = set()
        deduped: list[NewsItemDict] = []
        for it in items:
            url = (it.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(it)

        self.logger.info(
            f"Tavily fetched {len(deduped)} items within {hours_back}h "
            f"(credits used this fetch: {self._fetch_credits_spent}, "
            f"daily total: {self._quota_used_today()})"
        )
        return deduped

    def _run_query(
        self, query: str, hours_back: int, *, label: str
    ) -> list[NewsItemDict]:
        """Issue one Tavily search and convert results to NewsItemDict.

        Returns [] on any failure; only successful round-trips burn quota.
        """
        try:
            response = self.client.search(
                query=query,
                max_results=self.MAX_RESULTS,
                search_depth="basic",
                include_raw_content=False,
            )
        except Exception as e:
            self.logger.warning(f"Tavily {label} query failed: {e!r}")
            return []

        self._increment_quota()

        cutoff = datetime.utcnow() - timedelta(hours=max(1, int(hours_back)))
        results = sorted(
            response.get("results", []) or [],
            key=lambda r: float(r.get("score") or 0.0),
            reverse=True,
        )

        items: list[NewsItemDict] = []
        for r in results:
            url = (r.get("url") or "").strip()
            title = (r.get("title") or "").strip()
            if not url or not title:
                continue

            published = parse_time_string(r.get("published_date"))
            if published is None:
                published = self._infer_published_at(url, title)
            # A missing date is not evidence that a page is new.  Quarantine
            # it by excluding it from the real-time news stream.
            if published is None:
                self.logger.debug(f"Tavily undated result skipped: {url}")
                continue
            if published < cutoff or published > datetime.utcnow() + timedelta(days=1):
                continue

            summary = (r.get("content") or "")[: self.SUMMARY_MAX_CHARS]

            items.append(
                NewsItemDict(
                    url=url,
                    title=title[:512],
                    summary=summary,
                    source=self.source,
                    source_label=self.source_label,
                    published_at=published,
                    content="",
                )
            )
        return items

    @staticmethod
    def _infer_published_at(url: str, title: str) -> datetime | None:
        """Infer an explicit date embedded in a URL or title.

        We intentionally do not use page retrieval time as publication time.
        """
        text = f"{url} {title}"
        patterns = (
            r"(?<!\d)(20\d{2})[-_/](0?[1-9]|1[0-2])[-_/](0?[1-9]|[12]\d|3[01])(?!\d)",
            r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)",
            r"(?<!\d)(20\d{2})年(0?[1-9]|1[0-2])月(0?[1-9]|[12]\d|3[01])日",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            try:
                year, month, day = (int(v) for v in match.groups())
                return datetime(year, month, day, 12, 0, 0)
            except ValueError:
                continue
        return None

    @staticmethod
    def _build_event_query() -> str:
        """One composite query from the EVENT_QUERIES list.

        We rotate by UTC date so successive fetches don't always burn quota
        on the same first group — if Tavily returns nothing useful for one
        bucket, the next fetch starts with a different one.
        """
        if not TavilyCollector.EVENT_QUERIES:
            return ""
        # Pick a stable rotation based on the date.
        idx = int(datetime.utcnow().strftime("%Y%m%d")) % len(
            TavilyCollector.EVENT_QUERIES
        )
        # Take 2 adjacent buckets for breadth — one event category per fetch
        # is too narrow; the whole list in one fetch would produce noisy hits.
        first = TavilyCollector.EVENT_QUERIES[idx]
        second = TavilyCollector.EVENT_QUERIES[
            (idx + 1) % len(TavilyCollector.EVENT_QUERIES)
        ]
        return f"{first} OR {second}"

    # ------------------------------------------------------------------
    # Lifecycle override — we don't use the base httpx.Client
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._client = None
