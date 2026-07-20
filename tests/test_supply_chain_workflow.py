"""End-to-end tests for WF-02..WF-06 pipeline modules.

Each test seeds the in-memory DB with the minimal fixture (NewsRaw,
KnowledgeCategory, SearchTerm, AStock, PortfolioAccount/Position) and runs
the pipeline with ``persist=True``. The shared conftest ``in_memory_db``
fixture handles DB isolation; tests build their own domain rows so we never
depend on the production seed data.
"""
from __future__ import annotations

import json
from datetime import datetime, date, timedelta

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_basics(db, *, with_categories: bool = True):
    """Seed KnowledgeCategory, SearchTerm, AStock rows used by all pipelines."""
    from storage.models import (
        AStock, KnowledgeCategory, SearchTerm, NewsRaw, PortfolioAccount,
        PortfolioPosition, StockQuote, IndustryEvent, MismatchResult,
        StorageEvent, PortfolioDiagnosis, StockScore, MorningReport,
        EveningReview, FeishuPush,
    )

    cat = None
    if with_categories:
        with db.tx() as s:
            cat = KnowledgeCategory(name="半导体", signal_type="demand_pickup")
            s.add(cat)
            s.flush()
            term = SearchTerm(
                term="HBM 存储芯片",
                category_id=cat.id,
                priority="高",
                transmission_logic="需求 → 涨价",
                a_share_map="江波龙(301308) 三环集团(300408)",
                a_share_codes="301308,300408",
            )
            s.add(term)
            s.flush()
            s.add_all([
                AStock(code="301308", name="江波龙", sector_tags="存储", supply_exposure="高", tier=1),
                AStock(code="300408", name="三环集团", sector_tags="电子元件", supply_exposure="中", tier=1),
            ])

    with db.tx() as s:
        account = PortfolioAccount(
            user_id="default",
            total_assets=500_000.0,
            max_drawdown_tolerance=0.20,
            external_assets_included=False,
            as_of=datetime.utcnow(),
        )
        s.add(account)
        s.flush()
        positions = [
            PortfolioPosition(
                user_id="default", code="301308", name="江波龙",
                asset_type="stock", quantity=100, available_quantity=100,
                current_price=587.60, cost_price=600.1172,
                market_value=58_760.0, pnl_amount=-1_251.72, pnl_pct=-0.0209,
                risk_bucket="存储芯片",
            ),
            PortfolioPosition(
                user_id="default", code="300408", name="三环集团",
                asset_type="stock", quantity=400, available_quantity=400,
                current_price=127.05, cost_price=145.1184,
                market_value=50_820.0, pnl_amount=-7_227.35, pnl_pct=-0.1245,
                risk_bucket="电子元件",
            ),
        ]
        s.add_all(positions)


def _make_news(db, **kwargs) -> int:
    from storage.models import NewsRaw
    defaults = {
        "url": kwargs.pop("url", "https://example.com/news/" + datetime.utcnow().isoformat()),
        "title": kwargs.pop("title", "半导体板块异动：HBM 需求超出预期"),
        "summary": kwargs.pop("summary", "DRAM 现货价格本周上涨，HBM 订单可见度延伸至 2027。"),
        "content": kwargs.pop("content", "详细正文：HBM 需求超预期，江波龙/三环集团有望受益。"),
        "source": kwargs.pop("source", "cls"),
        "source_label": kwargs.pop("source_label", "财联社"),
        "published_at": kwargs.pop("published_at", datetime.utcnow()),
        # Event extraction only consumes news that passed the collection-time
        # relevance gate. Keep test fixtures aligned with that production
        # contract; noise tests still exercise the extractor's own filter.
        "keywords_matched": kwargs.pop("keywords_matched", "HBM 存储芯片"),
    }
    defaults.update(kwargs)
    with db.tx() as s:
        row = NewsRaw(**defaults)
        s.add(row)
        s.flush()
        return row.id


# ---------------------------------------------------------------------------
# WF-02 — event extraction
# ---------------------------------------------------------------------------


def test_extract_events_heuristic_path_when_no_api_key(in_memory_db, monkeypatch):
    """When MINIMAX_API_KEY is unset, the heuristic extractor is used and
    returns a StorageEvent row with at least industry_chain set."""
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    db = in_memory_db
    _seed_basics(db)
    news_id = _make_news(
        db,
        title="HBM 存储芯片需求超预期",
        summary="DRAM 与 HBM 价格上行",
        content="江波龙、三环集团等供应链公司有望受益",
    )

    from pipeline.events_extract import extract_events
    result = extract_events(hours_back=24, limit=10, persist=True)
    assert result["candidates"] >= 1
    assert result["extracted"] >= 1
    sources = result["source_breakdown"]
    assert sources["heuristic"] >= 1
    assert sources["llm"] == 0  # no key configured

    # Validate persistence
    from storage.models import StorageEvent
    with db.session() as s:
        rows = s.query(StorageEvent).filter(StorageEvent.news_id == news_id).all()
        assert len(rows) == 1
        assert rows[0].industry_chain
        assert rows[0].event_type


def test_extract_events_quality_assess(in_memory_db, monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    db = in_memory_db
    _seed_basics(db)

    from pipeline.events_extract import extract_events, assess_quality
    result = extract_events(hours_back=24, limit=10, persist=True)
    status, quality = assess_quality(result)
    assert status in {"succeeded", "degraded"}
    assert quality in {"pass", "warn", "fail"}


def test_extract_events_skips_noise(in_memory_db, monkeypatch):
    """Promo / ad copy is filtered before reaching the extractor."""
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    db = in_memory_db
    _seed_basics(db)
    _make_news(
        db,
        title="限时优惠 扫码抽奖",
        summary="直播间优惠",
        content="点击领取红包",
    )

    from pipeline.events_extract import extract_events
    result = extract_events(hours_back=24, limit=10, persist=True)
    assert result["candidates"] == 0


# ---------------------------------------------------------------------------
# WF-03 — mismatch detection
# ---------------------------------------------------------------------------


def _seed_storage_events(db):
    from storage.models import StorageEvent, NewsRaw
    with db.tx() as s:
        news = NewsRaw(
            url="https://example.com/m1",
            title="HBM 紧缺",
            summary="缺货涨价",
            content="...",
            source="cls",
            published_at=datetime.utcnow(),
        )
        s.add(news)
        s.flush()
        s.add(StorageEvent(
            news_id=news.id,
            event_key="ev1",
            schema_version="v1",
            title="HBM 紧缺",
            industry_chain="半导体",
            event_type="supply_tight",
            supply_direction="tight",
            demand_direction="up",
            magnitude=7.0,
            confidence=0.7,
            evidence_json=json.dumps([{"claim": "HBM 订单可见", "source": "cls", "url": "x"}]),
            counter_evidence_json=json.dumps([]),
        ))


def test_detect_mismatch_persists_and_quality(in_memory_db, monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    db = in_memory_db
    _seed_basics(db)
    _seed_storage_events(db)

    from pipeline.mismatch import detect, assess_quality
    result = detect(hours_back=48, limit=10, persist=True)
    assert result["summary"].startswith("buckets=")
    mismatches = list(result["mismatches"])
    assert len(mismatches) >= 1
    top = mismatches[0]
    assert top.industry_chain == "半导体"
    assert top.direction == "tight"
    assert top.total_score > 0
    assert top.total_score <= 100, "mismatch score must stay on the 0-100 scale"

    # 受益列表应包含 301308 / 300408
    beneficiaries = json.loads(top.beneficiaries_json)
    assert any("301308" in b for b in beneficiaries)
    assert any("300408" in b for b in beneficiaries)

    status, quality = assess_quality(result)
    # 一条事件 + 单一来源会触发 warn（多源不足），这是预期行为
    assert status in {"degraded", "succeeded"}
    assert quality in {"warn", "pass"}


# ---------------------------------------------------------------------------
# WF-04 — scoring
# ---------------------------------------------------------------------------


def _seed_quotes(db, code: str, days: int = 5, base_price: float = 100.0):
    from storage.models import StockQuote
    today = date.today()
    with db.tx() as s:
        for i in range(days):
            q = StockQuote(
                trade_date=today - timedelta(days=days - 1 - i),
                code=code, name="x",
                open=base_price + i, close=base_price + i + 1,
                high=base_price + i + 2, low=base_price + i - 1,
                volume=100_000, turnover=10_000_000.0,
                change_pct=1.0, change_amt=1.0,
            )
            s.add(q)


def test_score_candidates_hard_filters_and_scoring(in_memory_db, monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    db = in_memory_db
    _seed_basics(db)
    _seed_storage_events(db)
    _seed_quotes(db, "301308", days=5, base_price=580)
    _seed_quotes(db, "300408", days=5, base_price=120)

    # 触发 WF-03 落 mismatch 结果，再触发 WF-04
    from pipeline.mismatch import detect
    detect(hours_back=48, limit=10, persist=True)

    from pipeline.score import run, assess_quality
    result = run(persist=True)
    print("SCORE SUMMARY:", result["summary"], "n_scores=", len(result["scores"]))
    for s in result["scores"]:
        print(s.code, "final=", s.final_score, "passed=", s.hard_filter_passed,
              "reasons=", s.hard_filter_reasons, "graph=", s.graph_score,
              "fresh=", s.freshness_score, "trad=", s.tradability_score)
    assert result["summary"].startswith("candidates=")
    scores = list(result["scores"])
    assert scores, "expected at least one stock to score"
    # 候选中至少有一个通过硬过滤
    passed = [s for s in scores if s.hard_filter_passed]
    assert passed, "at least one candidate must clear hard filters"
    assert all(0 <= s.final_score <= 100 for s in scores)

    status, quality = assess_quality(result)
    assert status in {"degraded", "succeeded"}


# ---------------------------------------------------------------------------
# WF-05 — diagnose + morning report
# ---------------------------------------------------------------------------


def _seed_diagnoses_and_scores(db):
    today = date.today()
    from storage.models import StockScore, PortfolioDiagnosis, MismatchResult, NewsRaw, StorageEvent
    with db.tx() as s:
        news = NewsRaw(
            url="https://example.com/m2",
            title="HBM",
            summary="x",
            content="...",
            source="cls",
            published_at=datetime.utcnow(),
        )
        s.add(news)
        s.flush()
        s.add(StorageEvent(
            news_id=news.id,
            event_key="ev2",
            schema_version="v1",
            title="HBM",
            industry_chain="半导体",
            event_type="supply_tight",
            supply_direction="tight",
            demand_direction="up",
            magnitude=7.0,
            confidence=0.7,
            evidence_json=json.dumps([{"claim": "x", "source": "cls", "url": "x"}]),
            counter_evidence_json=json.dumps([]),
        ))
        s.add(MismatchResult(
            result_key="abcd",
            industry_chain="半导体",
            direction="tight",
            total_score=70.0,
            evidence_score=80.0,
            multi_source_score=80.0,
            supply_demand_score=80.0,
            price_inventory_score=60.0,
            graph_score=70.0,
            freshness_score=80.0,
            tradability_score=70.0,
            n_events=2, n_sources=2,
            beneficiaries_json=json.dumps(["江波龙(301308)"]),
            at_risk_json=json.dumps([]),
            summary="半导体 tight",
            trade_date=today,
        ))
        s.add(StockScore(
            trade_date=today, code="301308", name="江波龙",
            asset_type="stock", direction="long",
            final_score=72.0,
            evidence_score=80, multi_source_score=80, supply_demand_score=70,
            price_inventory_score=60, graph_score=70,
            freshness_score=80, tradability_score=70,
            hard_filter_passed=True,
            catalyst_window="1-4 weeks",
            observe_range="580-610", entry_range="580-600",
            stop_loss=540.0,
            invalidation="价格回落 5% 或供给端消息反转",
        ))
        s.add_all([
            PortfolioDiagnosis(
                user_id="default", trade_date=today, code="301308", name="江波龙",
                asset_type="stock", action="hold", confidence=0.6,
                industry_logic_ok=True, valuation_ok=True,
                drawdown_state="normal", bucket_exposure_pct=0.10,
                observe_range="580-610", entry_range="580-600",
                stop_loss=540.0,
                invalidation="价格回落 5%",
                summary="江波龙 hold",
                reasons_json=json.dumps(["评分 72.0"]),
                risk_note="软预警",
            ),
            PortfolioDiagnosis(
                user_id="default", trade_date=today, code="300408", name="三环集团",
                asset_type="stock", action="hold", confidence=0.55,
                industry_logic_ok=True, valuation_ok=True,
                drawdown_state="watch", bucket_exposure_pct=0.10,
                observe_range="120-130", entry_range="120-125",
                stop_loss=110.0,
                invalidation="需求反转",
                summary="三环集团 hold",
                reasons_json=json.dumps(["评分中性"]),
                risk_note="软预警",
            ),
        ])


def test_diagnose_portfolio_writes_actions(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed_basics(db)
    _seed_diagnoses_and_scores(db)

    from pipeline.portfolio_diagnose import diagnose, assess_quality
    result = diagnose(user_id="default", persist=True)
    diagnoses = list(result["diagnoses"])
    assert len(diagnoses) == 2
    actions = {d.code: d.action for d in diagnoses}
    assert set(actions) == {"301308", "300408"}
    status, quality = assess_quality(result)
    assert status == "succeeded"
    assert quality == "pass"


def test_morning_report_creates_card_and_avoids_double_push(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed_basics(db)
    _seed_diagnoses_and_scores(db)

    # Disable Feishu so no actual network call is made
    from notifier import feishu as feishu_mod
    monkeypatch.setattr(feishu_mod.FeishuNotifier, "is_configured", lambda self: False)

    from pipeline.morning_report import build, assess_quality
    result = build(user_id="default", push=True)
    report = result["report"]
    assert report is not None
    assert "持仓动作" in report.feishu_card_json or "持仓" in report.feishu_card_json
    status, quality = assess_quality(result)
    assert status == "succeeded"
    assert quality == "pass"

    # Re-running with the same trade_date should not double-push (no rows
    # inserted into FeishuPush because notifier is disabled).
    result2 = build(user_id="default", push=True)
    assert result2["report"].id == report.id


# ---------------------------------------------------------------------------
# WF-06 — evening review
# ---------------------------------------------------------------------------


def test_evening_review_builds_card_and_persists(in_memory_db, monkeypatch):
    db = in_memory_db
    _seed_basics(db)
    _seed_diagnoses_and_scores(db)
    # Add stock quotes for verification
    _seed_quotes(db, "301308", days=5, base_price=580)
    _seed_quotes(db, "300408", days=5, base_price=120)

    from notifier import feishu as feishu_mod
    monkeypatch.setattr(feishu_mod.FeishuNotifier, "is_configured", lambda self: False)

    from pipeline.evening_review import build, assess_quality
    result = build(trade_date=date.today().isoformat(), push=True)
    review = result["review"]
    assert review is not None
    assert review.markdown
    assert "复盘" in review.markdown
    status, quality = assess_quality(result)
    assert status == "succeeded"
    assert quality == "pass"


# ---------------------------------------------------------------------------
# Pipeline service registration
# ---------------------------------------------------------------------------


def test_pipeline_service_registers_new_pipelines():
    from pipeline import service
    expected = {
        "extract_events", "detect_mismatch", "score_candidates",
        "diagnose_portfolio", "build_morning_report", "build_evening_review",
    }
    assert expected.issubset(set(service.PIPELINE_NAMES))


def test_pipeline_routes_extract_events(in_memory_db, monkeypatch):
    """End-to-end: POST-style idempotent creation + execution via service."""
    from pipeline import service

    run, created = service.create_pipeline_run(
        "extract_events", {"hours_back": 24, "limit": 5}, "unit-test:extract_events",
        trigger_source="unit",
    )
    assert created is True
    detail = service.execute_pipeline_run(run["run_id"])
    # Without seeded news, status is degraded but execution succeeded
    assert detail["status"] in {"degraded", "succeeded"}
    assert detail["quality_status"] in {"warn", "pass"}
