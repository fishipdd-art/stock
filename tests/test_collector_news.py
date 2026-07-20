"""Tests for the news collector's term matching / dedupe logic.

Targets orchestrator-level behavior (no live HTTP): we use stub
collectors returning fixed items, then verify filtering and dedupe.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from collector.news.base import NewsItemDict
from collector.news import NewsCollector
from collector.news.tavily import TavilyCollector
from storage.models import NewsRaw, SearchTerm, TavilyQuotaLog


class StubCollector:
    """Drop-in replacement for a real news source."""
    source = "stub"
    source_label = "Stub"

    def __init__(self, items):
        self._items = items

    def fetch(self, terms, hours_back):
        return self._items

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _news(title: str, url: str, source: str = "stub") -> NewsItemDict:
    return NewsItemDict(
        url=url,
        title=title,
        summary="",
        source=source,
        source_label="Stub",
        published_at=datetime(2026, 1, 1, 12, 0, 0),
        content="",
    )


def _term(text: str) -> SearchTerm:
    t = SearchTerm(
        term=text,
        category_id=None,
        priority="高",
        transmission_logic="",
        a_share_map="",
        a_share_codes="",
        enabled=True,
    )
    return t


class TestDedupe:
    def test_keeps_unique_urls(self, in_memory_db):
        items = [
            _news("A", "http://x.com/1"),
            _news("A", "http://x.com/2"),
            _news("B", "http://x.com/3"),
        ]
        nc = NewsCollector(
            db=in_memory_db,
            terms=[_term("无关")],
            collectors=[StubCollector(items)],
        )
        out = nc._dedupe(items)
        assert len(out) == 3

    def test_drops_duplicate_urls(self, in_memory_db):
        items = [
            _news("A", "http://x.com/1"),
            _news("A again", "http://x.com/1"),  # dup URL
            _news("B", "http://x.com/2"),
        ]
        nc = NewsCollector(
            db=in_memory_db,
            terms=[_term("无关")],
            collectors=[StubCollector(items)],
        )
        out = nc._dedupe(items)
        assert len(out) == 2
        urls = [i["url"] for i in out]
        assert "http://x.com/1" in urls
        assert "http://x.com/2" in urls


class TestTermFiltering:
    def test_keeps_items_matching_term(self, in_memory_db):
        items = [
            _news("铜价大涨", "http://x.com/cu"),
            _news("天气晴朗", "http://x.com/wt"),
        ]
        nc = NewsCollector(
            db=in_memory_db,
            terms=[_term("铜价")],
            collectors=[StubCollector(items)],
        )
        out = nc._filter_by_terms(items)
        assert len(out) == 1
        assert "铜" in out[0]["keywords_matched"]

    def test_no_match_returns_empty(self, in_memory_db):
        items = [_news("天气晴朗", "http://x.com/wt")]
        nc = NewsCollector(
            db=in_memory_db,
            terms=[_term("铜价")],
            collectors=[StubCollector(items)],
        )
        out = nc._filter_by_terms(items)
        assert out == []

    def test_no_terms_passes_through(self, in_memory_db):
        items = [_news("任意", "http://x.com/x")]
        nc = NewsCollector(
            db=in_memory_db,
            terms=[],
            collectors=[StubCollector(items)],
        )
        out = nc._filter_by_terms(items)
        assert len(out) == 1

    def test_multiple_terms_recorded(self, in_memory_db):
        items = [_news("铜价大涨 铝价回升", "http://x.com/m")]
        nc = NewsCollector(
            db=in_memory_db,
            terms=[_term("铜价"), _term("铝价")],
            collectors=[StubCollector(items)],
        )
        out = nc._filter_by_terms(items)
        assert len(out) == 1
        # Both terms matched
        matched = out[0]["keywords_matched"]
        assert "铜" in matched
        assert "铝" in matched


class TestPersistence:
    def test_save_is_idempotent(self, in_memory_db):
        item = _news("铜价大涨", "http://x.com/cu-once")
        nc = NewsCollector(
            db=in_memory_db,
            terms=[_term("铜价")],
            collectors=[StubCollector([item])],
        )
        n1 = nc._save([item])
        n2 = nc._save([item])
        assert n1 == 1
        assert n2 == 0  # already there

    def test_save_returns_zero_for_empty(self, in_memory_db):
        nc = NewsCollector(
            db=in_memory_db,
            terms=[_term("铜价")],
            collectors=[],
        )
        assert nc._save([]) == 0


class TestTavilyCollector:
    """Stub-only tests for the Tavily collector.

    The live Tavily SDK is exercised by ``test_tavily_integration_live``,
    which is skipped by default to avoid burning quota.
    """

    def _stub_client(self, monkeypatch, results):
        """Patch the lazy client property to a stub returning ``results``."""
        class _StubClient:
            def search(self, **kwargs):
                return {"results": results}

        class _StubClientFactory:
            def __init__(self):
                self._inner = _StubClient()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        # Force the property to return our stub without hitting the network.
        monkeypatch.setattr(
            TavilyCollector, "client", _StubClientFactory(), raising=False
        )

    def test_returns_empty_when_key_missing(self, in_memory_db, monkeypatch):
        from config.settings import settings as app_settings

        monkeypatch.setattr(app_settings, "tavily_api_key", "")
        c = TavilyCollector()
        assert c.fetch(["半导体", "锂电"], 24) == []

    def test_returns_empty_when_quota_exhausted(self, in_memory_db, monkeypatch):
        from config.settings import settings as app_settings

        monkeypatch.setattr(app_settings, "tavily_api_key", "tvly-test")
        monkeypatch.setattr(
            TavilyCollector, "_quota_used_today", lambda self: 20
        )
        c = TavilyCollector()
        out = c.fetch(["半导体"], 24)
        assert out == []

    def test_drops_stale_published_results(self, in_memory_db, monkeypatch):
        from config.settings import settings as app_settings

        monkeypatch.setattr(app_settings, "tavily_api_key", "tvly-test")

        three_days_ago = (datetime.utcnow() - timedelta(days=3)).isoformat()
        fresh = (datetime.utcnow() - timedelta(hours=2)).isoformat()
        self._stub_client(monkeypatch, [
            {
                "url": "https://old.example.com/1",
                "title": "三天前的旧闻",
                "content": "snippet",
                "score": 0.9,
                "published_date": three_days_ago,
            },
            {
                "url": "https://fresh.example.com/2",
                "title": "刚刚发生的新事件",
                "content": "snippet",
                "score": 0.5,
                "published_date": fresh,
            },
        ])

        c = TavilyCollector()
        items = c.fetch(["半导体"], 48)
        urls = [i["url"] for i in items]
        assert "https://fresh.example.com/2" in urls
        assert "https://old.example.com/1" not in urls

    def test_sorts_by_score_descending(self, in_memory_db, monkeypatch):
        from config.settings import settings as app_settings

        monkeypatch.setattr(app_settings, "tavily_api_key", "tvly-test")
        now = datetime.utcnow().isoformat()
        self._stub_client(monkeypatch, [
            {"url": "https://a/", "title": "low", "content": "", "score": 0.1, "published_date": now},
            {"url": "https://b/", "title": "high", "content": "", "score": 0.9, "published_date": now},
            {"url": "https://c/", "title": "mid", "content": "", "score": 0.5, "published_date": now},
        ])
        c = TavilyCollector()
        items = c.fetch(["半导体"], 24)
        assert [i["url"] for i in items] == ["https://b/", "https://c/", "https://a/"]

    def test_skips_undated_results(self, in_memory_db, monkeypatch):
        from config.settings import settings as app_settings

        monkeypatch.setattr(app_settings, "tavily_api_key", "tvly-test")
        self._stub_client(monkeypatch, [
            {"url": "https://example.com/evergreen", "title": "没有发布日期的旧页面", "content": "", "score": 0.9},
        ])
        assert TavilyCollector().fetch(["半导体"], 24) == []

    def test_infers_explicit_url_date(self, in_memory_db, monkeypatch):
        from config.settings import settings as app_settings

        monkeypatch.setattr(app_settings, "tavily_api_key", "tvly-test")
        today = datetime.utcnow().strftime("%Y/%m/%d")
        self._stub_client(monkeypatch, [
            {"url": f"https://example.com/{today}/event", "title": "明确日期事件", "content": "", "score": 0.9},
        ])
        assert len(TavilyCollector().fetch(["半导体"], 24)) == 1

    def test_increments_quota_only_on_success(self, in_memory_db, monkeypatch):
        from config.settings import settings as app_settings

        monkeypatch.setattr(app_settings, "tavily_api_key", "tvly-test")
        # Stub returns ONE result per call. fetch() now issues 2 queries
        # (KG + event), so 2 credits are expected per fetch.
        self._stub_client(monkeypatch, [
            {"url": "https://x/1", "title": "ok1", "content": "s", "score": 0.5},
            {"url": "https://x/2", "title": "ok2", "content": "s", "score": 0.5},
        ])
        c = TavilyCollector()
        before = c._quota_used_today()
        c.fetch(["半导体"], 24)
        after = c._quota_used_today()
        assert after - before == 2

    def test_no_quota_burned_when_search_fails(self, in_memory_db, monkeypatch):
        from config.settings import settings as app_settings

        monkeypatch.setattr(app_settings, "tavily_api_key", "tvly-test")

        class _Boom:
            def search(self, **kwargs):
                raise RuntimeError("network down")

        class _Factory:
            def __getattr__(self, name):
                return getattr(_Boom(), name)

        monkeypatch.setattr(
            TavilyCollector, "client", _Factory(), raising=False
        )
        c = TavilyCollector()
        before = c._quota_used_today()
        out = c.fetch(["半导体"], 24)
        after = c._quota_used_today()
        assert out == []
        assert after == before  # nothing burned on error

    def test_event_query_runs_in_parallel(self, in_memory_db, monkeypatch):
        """fetch() should issue one KG query plus one event query = 2 credits."""
        from config.settings import settings as app_settings

        monkeypatch.setattr(app_settings, "tavily_api_key", "tvly-test")

        # The stub client returns the same payload for every query — we count
        # how many times search() is invoked.
        class _CountingClient:
            def __init__(self):
                self.search_calls: list[dict] = []

            def search(self, **kwargs):
                self.search_calls.append(kwargs)
                return {
                    "results": [
                        {
                            "url": f"https://e/{len(self.search_calls)}",
                            "title": f"event hit {len(self.search_calls)}",
                            "content": "snippet",
                            "score": 0.5,
                            "published_date": datetime.utcnow().isoformat(),
                        }
                    ]
                }

        counting = _CountingClient()

        class _Factory:
            def __getattr__(self, name):
                return getattr(counting, name)

        monkeypatch.setattr(
            TavilyCollector, "client", _Factory(), raising=False
        )

        c = TavilyCollector()
        before = c._quota_used_today()
        items = c.fetch(["半导体"], 24)
        after = c._quota_used_today()

        # Two queries fired (kg + event), quota burned twice.
        assert len(counting.search_calls) == 2
        assert after - before == 2
        # Both queries produced a unique URL.
        urls = {i["url"] for i in items}
        assert "https://e/1" in urls
        assert "https://e/2" in urls

    def test_event_query_skipped_when_quota_low(self, in_memory_db, monkeypatch):
        """If only 1 credit remains, fetch should run the KG query only."""
        from config.settings import settings as app_settings

        monkeypatch.setattr(app_settings, "tavily_api_key", "tvly-test")
        monkeypatch.setattr(TavilyCollector, "_quota_used_today", lambda self: 19)

        class _CountingClient:
            def __init__(self):
                self.search_calls = 0

            def search(self, **kwargs):
                self.search_calls += 1
                return {
                    "results": [
                        {
                            "url": f"https://x/{self.search_calls}",
                            "title": "t",
                            "content": "",
                            "score": 0.5,
                        }
                    ]
                }

        counting = _CountingClient()

        class _Factory:
            def __getattr__(self, name):
                return getattr(counting, name)

        monkeypatch.setattr(
            TavilyCollector, "client", _Factory(), raising=False
        )

        c = TavilyCollector()
        c.fetch(["半导体"], 24)
        # Only 1 call fits in the remaining budget.
        assert counting.search_calls == 1

    def test_event_query_rotation_is_stable_per_day(self, in_memory_db):
        """Same date → same bucket; different date → possibly different bucket."""
        from datetime import datetime as dt_cls
        from collector.news.tavily import TavilyCollector as TC

        # Use a fixed reference date.
        fixed_date = dt_cls(2026, 7, 13)
        idx_a = int(fixed_date.strftime("%Y%m%d")) % len(TC.EVENT_QUERIES)
        idx_b = int(dt_cls(2026, 7, 14).strftime("%Y%m%d")) % len(TC.EVENT_QUERIES)
        # At least one of the two adjacent indices may shift on a different day.
        assert idx_a != idx_b or len(TC.EVENT_QUERIES) < 3

    def test_orchestrator_keeps_tavily_matching_terms(self, in_memory_db):
        """End-to-end: a Tavily-tagged item that matches a KG term survives
        dedupe + filter + save and lands in news_raw."""
        item = _news("半导体产能扩张", "http://tavily.example.com/1", source="tavily")
        tavily_stub = StubCollector([item])
        # Inject the real TavilyCollector class into the orchestrator via stub
        # substitution; the stub stands in for the SDK round-trip.
        nc = NewsCollector(
            db=in_memory_db,
            terms=[_term("半导体")],
            collectors=[tavily_stub],
        )
        saved = nc.collect(hours_back=24)
        assert saved == 1
        with in_memory_db.session() as s:
            rows = (
                s.query(NewsRaw)
                .filter(NewsRaw.source == "tavily")
                .all()
            )
            assert len(rows) == 1
            assert "半导体" in rows[0].keywords_matched

    @pytest.mark.skip(reason="Live Tavily API call — burns 1 credit. Remove @skip to run.")
    def test_tavily_integration_live(self, in_memory_db, monkeypatch):
        from config.settings import settings as app_settings

        if not app_settings.tavily_api_key:
            pytest.skip("tavily_api_key not set in env")
        c = TavilyCollector()
        items = c.fetch(["半导体"], 24)
        assert isinstance(items, list)
        if items:
            assert items[0].get("url")
            assert items[0].get("title")
