"""
News collection orchestrator + CLI.

Public surface
--------------
* :class:`NewsCollector` — runs the enabled collectors in parallel, dedupes,
  filters by search terms (multi-term OR), and persists to ``news_raw``.
* :func:`run_news_collection` — convenience entrypoint used by the scheduler
  and the CLI; resolves terms from the knowledge graph by priority.
* CLI::

      python -m collector.news collect --priority 高 --hours-back 24
      python -m collector.news stats
"""
from __future__ import annotations

import argparse
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Iterable

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collector.news.base import BaseNewsCollector, NewsItemDict
from collector.news.cls import CLSCollector
from collector.news.eastmoney import EastMoneyCollector
from collector.news.sina import SinaCollector
from collector.news.tavily import TavilyCollector
from knowledge_graph.loader import get_terms_by_priority
from storage.database import Database, init_db
from storage.models import NewsRaw, SearchTerm


# Extract distinctive keywords from a search term (CJK 2+ chars, alphanumeric 2+ chars)
_KEYWORD_RE = re.compile(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{1,}")
# Common stopwords to drop from keyword matching
_STOPWORDS = {
    "2026", "2025", "2024", "2023", "涨跌", "价格", "走势", "今日", "最新",
    "10年期", "2年期", "5年期", "今年", "明年", "3月", "4月", "5月", "6月",
    "1月", "2月", "7月", "8月", "9月", "10月", "11月", "12月",
    "供需", "缺口", "上涨", "下跌", "涨价", "降价", "政策", "利好",
    "板块", "行业", "股票", "概念股", "市场", "数据", "最新价", "现货价",
    "期货", "今日", "近期", "发布", "公司", "集团", "同比", "环比",
}
_DISTINCTIVE_SINGLE_CJK = {"铜", "铝", "锌", "镍", "钴", "锂", "钼", "钨", "金", "银", "煤", "油"}
_TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "source", "ref", "share_token", "share_source",
}
_EVENT_PREDICATES = {
    "大涨", "上涨", "回升", "下跌", "暴跌", "提价", "降价", "扩产", "扩张", "减产", "停产",
    "投产", "短缺", "缺货", "库存", "订单", "中标", "制裁", "封锁", "回购",
    "并购", "收购", "重组", "预增", "预减", "增长", "下滑", "补贴", "处罚",
    "调查", "发射", "降准", "降息", "加息", "突破", "签订", "获批",
}


def _extract_keywords(term: str) -> list[str]:
    words = _KEYWORD_RE.findall(term or "")
    out = []
    for w in words:
        if w.isdigit():
            continue
        if w in _STOPWORDS:
            continue
        if len(w) >= 2 or w in _DISTINCTIVE_SINGLE_CJK:
            out.append(w)
    return out


def _canonical_url(url: str) -> str:
    """Remove fragments and common tracking parameters for stable dedupe."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_QUERY_KEYS
        ]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))
    except ValueError:
        return raw.split("#", 1)[0]


def _normalized_title(title: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (title or "").lower())


__all__ = [
    "NewsCollector",
    "run_news_collection",
    "main",
]


# ============================================================================
# Orchestrator
# ============================================================================


class NewsCollector:
    """Run all enabled collectors, filter by terms, persist to ``news_raw``."""

    def __init__(
        self,
        db: Database,
        terms: Iterable[SearchTerm] | None = None,
        collectors: list[BaseNewsCollector] | None = None,
    ) -> None:
        self.db = db
        self.terms: list[SearchTerm] = [t for t in (terms or []) if t.enabled and t.term]
        self.collectors: list[BaseNewsCollector] = collectors or self._default_collectors()
        self.logger = logger.bind(component="news_orchestrator")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def collect(self, hours_back: int = 24) -> int:
        """Run a full collection cycle. Returns the number of newly inserted rows."""
        terms_str = [t.term for t in self.terms]
        if not terms_str:
            self.logger.warning(
                "No enabled search terms available — nothing to match against. "
                "Did you import the knowledge graph? (`python -m knowledge_graph.loader import`)"
            )
            return 0

        self.logger.info(
            f"Starting news collection: {len(terms_str)} terms, hours_back={hours_back}, "
            f"collectors={[c.source for c in self.collectors]}"
        )

        all_items = self._fetch_all(terms_str, hours_back)
        self.logger.info(f"Total fetched: {len(all_items)} items (pre-dedupe)")

        deduped = self._dedupe(all_items)
        self.logger.info(f"After dedupe: {len(deduped)} items")

        matched = self._filter_by_terms(deduped)
        self.logger.info(f"After term filter: {len(matched)} items")

        saved = self._save(matched)
        self.logger.info(f"Saved {saved} new items to news_raw")
        return saved

    def close(self) -> None:
        for c in self.collectors:
            try:
                c.close()
            except Exception:
                pass

    def __enter__(self) -> "NewsCollector":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Collectors
    # ------------------------------------------------------------------

    @staticmethod
    def _default_collectors() -> list[BaseNewsCollector]:
        return [
            CLSCollector(),
            EastMoneyCollector(),
            SinaCollector(),
            # Tavily backstops CLS/EastMoney blind spots. Drops out silently
            # when TAVILY_API_KEY is unset or the 20/day quota is exhausted.
            TavilyCollector(),
        ]

    def _fetch_all(self, terms: list[str], hours_back: int) -> list[NewsItemDict]:
        """Fan out across collectors. Failures are caught and logged."""
        results: list[NewsItemDict] = []
        if not self.collectors:
            return results

        max_workers = min(4, max(1, len(self.collectors)))
        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="news"
        ) as ex:
            future_to_collector = {
                ex.submit(_safe_fetch, c, terms, hours_back): c
                for c in self.collectors
            }
            for future in as_completed(future_to_collector):
                collector = future_to_collector[future]
                try:
                    items = future.result()
                except Exception as e:  # pragma: no cover — defensive
                    self.logger.error(
                        f"Collector {collector.source} raised unexpectedly: {e!r}"
                    )
                    continue
                if items:
                    self.logger.info(
                        f"Collector {collector.source} returned {len(items)} items"
                    )
                    results.extend(items)
        return results

    # ------------------------------------------------------------------
    # Filtering / dedupe
    # ------------------------------------------------------------------

    @staticmethod
    def _dedupe(items: list[NewsItemDict]) -> list[NewsItemDict]:
        """Dedupe canonical URLs and same-source normalized headlines."""
        seen_urls: set[str] = set()
        seen_titles: set[tuple[str, str]] = set()
        out: list[NewsItemDict] = []
        for item in items:
            url = _canonical_url(item.get("url") or "")
            source = str(item.get("source") or "")
            title_key = _normalized_title(item.get("title") or "")
            if not url or url in seen_urls:
                continue
            # Very short labels (tickers, one-letter test fixtures, terse
            # exchange notices) are not reliable title-cluster keys.
            clusterable = len(title_key) >= 8
            if clusterable and (source, title_key) in seen_titles:
                continue
            seen_urls.add(url)
            if clusterable:
                seen_titles.add((source, title_key))
            normalized: NewsItemDict = dict(item)  # type: ignore[assignment]
            normalized["url"] = url
            out.append(normalized)
        return out

    def _filter_by_terms(
        self, items: list[NewsItemDict]
    ) -> list[NewsItemDict]:
        """Keep items whose title+summary contains any keyword from a search term."""
        if not self.terms:
            return items

        # Pre-build (term_original, keywords_lower) for each term.
        norm_terms: list[tuple[str, list[str]]] = []
        for t in self.terms:
            if not t.term:
                continue
            kws = _extract_keywords(t.term)
            if kws:
                norm_terms.append((t.term, [k.lower() for k in kws]))
        if not norm_terms:
            return items

        out: list[NewsItemDict] = []
        for item in items:
            title = item.get("title") or ""
            summary = item.get("summary") or ""
            text = f"{title} {summary}".lower()
            if not text.strip():
                continue
            matched_terms = []
            for original, keywords in norm_terms:
                matched = [kw for kw in keywords if kw in text]
                if len(keywords) >= 2 and len(matched) >= 2:
                    matched_terms.append(original)
                elif len(keywords) == 1:
                    keyword = keywords[0]
                    if keyword in title.lower() and any(
                        predicate in text for predicate in _EVENT_PREDICATES
                    ):
                        matched_terms.append(original)
            if not matched_terms:
                continue
            new_item: NewsItemDict = dict(item)  # type: ignore[assignment]
            new_item["keywords_matched"] = ",".join(matched_terms)
            out.append(new_item)
        return out

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self, items: list[NewsItemDict]) -> int:
        """Idempotent insert of ``items`` into ``news_raw``.

        Existing URLs (keyed on ``news_raw.url``) are skipped, so the
        operation is safe to re-run. Returns the number of newly inserted rows.
        """
        if not items:
            return 0

        urls = [(i.get("url") or "").strip() for i in items]
        urls = [u for u in urls if u]
        if not urls:
            return 0

        with self.db.tx() as session:  # type: Session
            existing = {
                row[0]
                for row in session.execute(
                    select(NewsRaw.url).where(NewsRaw.url.in_(urls))
                ).all()
            }

            new_items = [i for i in items if (i.get("url") or "").strip() not in existing]
            if not new_items:
                return 0

            now = datetime.utcnow()
            rows = [
                {
                    "url": (i.get("url") or "").strip(),
                    "title": (i.get("title") or "")[:512],
                    "summary": i.get("summary") or "",
                    "source": i.get("source") or "",
                    "source_label": i.get("source_label") or "",
                    "published_at": i.get("published_at") or now,
                    "fetched_at": now,
                    "content": i.get("content") or "",
                    "keywords_matched": i.get("keywords_matched") or "",
                }
                for i in new_items
            ]
            session.bulk_insert_mappings(NewsRaw, rows)

        return len(new_items)


# ============================================================================
# Helpers
# ============================================================================


def _safe_fetch(
    collector: BaseNewsCollector,
    terms: list[str],
    hours_back: int,
) -> list[NewsItemDict]:
    """Wrap a single collector call so the executor never sees an exception."""
    try:
        with collector:
            return collector.fetch(terms, hours_back)
    except Exception as e:
        logger.error(f"Collector {collector.source} crashed: {e!r}")
        return []


# ============================================================================
# Convenience entrypoint
# ============================================================================


def run_news_collection(
    db: Database,
    priority: str = "高",
    hours_back: int = 24,
    terms: Iterable[SearchTerm] | None = None,
) -> int:
    """Run a collection cycle. Returns newly inserted row count.

    ``terms`` is supplied directly by callers that already loaded terms; when
    omitted, terms are fetched from ``search_terms`` filtered by ``priority``.
    """
    if terms is None:
        terms = get_terms_by_priority(db, priority)

    with NewsCollector(db, terms=terms) as nc:
        return nc.collect(hours_back=hours_back)


def reclassify_recent_news(db: Database, hours: int = 72) -> dict[str, int]:
    """Re-run the current relevance rule over already persisted recent news."""
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(hours=max(1, int(hours)))
    with db.session() as session:
        terms = session.query(SearchTerm).filter(SearchTerm.enabled.is_(True)).all()
        rows = (
            session.query(NewsRaw)
            .filter(NewsRaw.published_at >= cutoff)
            .all()
        )
    items: list[NewsItemDict] = [
        NewsItemDict(
            url=row.url,
            title=row.title,
            summary=row.summary,
            source=row.source,
            source_label=row.source_label,
            published_at=row.published_at,
            content=row.content,
        )
        for row in rows
    ]
    classifier = NewsCollector(db=db, terms=terms, collectors=[])
    matched = classifier._filter_by_terms(items)
    by_url = {item["url"]: item.get("keywords_matched", "") for item in matched}
    with db.tx() as session:
        recent = session.query(NewsRaw).filter(NewsRaw.published_at >= cutoff).all()
        for row in recent:
            row.keywords_matched = by_url.get(row.url, "")
    return {"scanned": len(rows), "matched": len(matched), "unmatched": len(rows) - len(matched)}


# ============================================================================
# CLI
# ============================================================================


def _configure_logging(verbose: bool) -> None:
    """Configure loguru sink (stdout)."""
    from config.settings import configure_logging
    configure_logging(verbose=verbose)


def _print_stats(db: Database) -> int:
    with db.session() as session:  # type: Session
        rows = session.execute(
            select(NewsRaw.source, func.count(NewsRaw.id))
            .group_by(NewsRaw.source)
            .order_by(NewsRaw.source)
        ).all()
        total = int(
            session.execute(select(func.count(NewsRaw.id))).scalar_one() or 0
        )

    width = 18
    print(f"{'source':<{width}} {'count':>10}")
    print("-" * (width + 11))
    if not rows:
        print(f"{'(empty)':<{width}} {0:>10}")
    else:
        for source, count in rows:
            print(f"{source or '(unknown)':<{width}} {int(count):>10,}")
    print("-" * (width + 11))
    print(f"{'TOTAL':<{width}} {total:>10,}")
    return 0


def _print_latest(db: Database, limit: int = 5) -> None:
    with db.session() as session:  # type: Session
        rows = session.execute(
            select(NewsRaw)
            .order_by(NewsRaw.published_at.desc())
            .limit(limit)
        ).scalars().all()
    if not rows:
        return
    print()
    print(f"Most recent {min(limit, len(rows))} items:")
    for r in rows:
        ts = r.published_at.isoformat(timespec="seconds") if r.published_at else "?"
        kw = r.keywords_matched or ""
        kw_suffix = f"  [terms: {kw}]" if kw else ""
        print(f"  - [{ts}] ({r.source}) {r.title[:80]}{kw_suffix}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.news",
        description="News collection for the supply-chain stock analysis system.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_collect = sub.add_parser("collect", help="Fetch news for the given priority.")
    p_collect.add_argument(
        "--priority",
        default="高",
        choices=["高", "中", "低"],
        help="Search-term priority bucket to filter on (default: 高).",
    )
    p_collect.add_argument(
        "--hours-back",
        type=int,
        default=24,
        help="Lookback window in hours (default: 24).",
    )

    p_stats = sub.add_parser("stats", help="Show row counts per source.")

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    db = init_db()

    if args.cmd == "collect":
        try:
            saved = run_news_collection(
                db, priority=args.priority, hours_back=args.hours_back
            )
        except Exception as e:
            logger.exception(f"Collection failed: {e}")
            return 1
        # Shell-friendly single-line summary for downstream tooling.
        print(f"COLLECT_OK priority={args.priority} hours_back={args.hours_back} saved={saved}")
        return 0

    if args.cmd == "stats":
        try:
            _print_stats(db)
            _print_latest(db)
        except Exception as e:
            logger.exception(f"Stats query failed: {e}")
            return 1
        return 0

    parser.error(f"Unknown command: {args.cmd}")
    return 1  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
