"""End-to-end smoke test for the 10 Dify schedule-table pipelines.

Exercises ``pipeline/service.create_pipeline_run`` + ``execute_pipeline_run``
+ ``get_pipeline_run`` for every pipeline listed in ``docs/DIFY_SCHEDULE.md``,
then prints the resulting ``status`` / ``quality_status`` / ``item_count``.

This script writes to a *temporary* SQLite database so it never disturbs the
production data/ directory. Run from project root:

    .venv/bin/python scripts/smoke_schedule.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Per-pipeline smoke schedule.
# Each entry: (pipeline, params, idempotency_key, expected_min_quality)
# expected_min_quality is the floor we want to see in the output:
#   "fail"    -> accept anything (network/data sources may not be configured)
#   "warn"    -> expect at least degraded
#   "pass"    -> expect succeeded
# ---------------------------------------------------------------------------
SMOKE_PIPELINES: list[tuple[str, dict, str, str]] = [
    # 05:30 — collect_futures (data source may be offline in CI)
    ("collect_futures", {"days_back": 1}, "smoke:collect_futures:2026-07-12", "fail"),
    # 06:00 — collect_stocks
    ("collect_stocks", {}, "smoke:collect_stocks:2026-07-12", "fail"),
    # 07:00 — collect_news_high
    ("collect_news_high", {"hours_back": 24}, "smoke:collect_news_high:2026-07-12", "fail"),
    # 07:00 — collect_news_mid
    ("collect_news_mid", {"hours_back": 24}, "smoke:collect_news_mid:2026-07-12", "fail"),
    # 07:15 — WF-02 extract_events (no LLM key -> heuristic; 0 candidates is ok)
    ("extract_events", {"hours_back": 24, "limit": 5}, "smoke:wf02:2026-07-12", "pass"),
    # 07:25 — WF-03 detect_mismatch (depends on StorageEvents)
    ("detect_mismatch", {"hours_back": 48, "limit": 10}, "smoke:wf03:2026-07-12", "fail"),
    # 07:35 — WF-04 score_candidates (depends on MismatchResults + StockQuotes)
    ("score_candidates", {"trade_date": ""}, "smoke:wf04:2026-07-12", "fail"),
    # 07:35 — compute_hotness (legacy pipeline; still in PIPELINES dict)
    ("compute_hotness", {}, "smoke:compute_hotness:2026-07-12", "fail"),
    # 08:05 — WF-05a diagnose_portfolio
    ("diagnose_portfolio", {"user_id": "default"}, "smoke:wf05a:2026-07-12", "fail"),
    # 08:05 — WF-05b build_morning_report
    ("build_morning_report", {"user_id": "default"}, "smoke:wf05b:2026-07-12", "fail"),
    # 20:00 — WF-06 build_evening_review
    ("build_evening_review", {}, "smoke:wf06:2026-07-12", "fail"),
    # generate_report (legacy report pipeline)
    ("generate_report", {"report_type": "full"}, "smoke:generate_report:2026-07-12", "fail"),
]


def _init_temp_db(tmp: Path):
    """Initialise a fresh Database in tmp and seed minimal fixture."""
    import os
    os.environ["STOCK_TEST_TMP"] = str(tmp)
    from storage import database
    from config.settings import settings
    settings.db_path = tmp / "smoke.db"
    settings.database_url = ""
    database._db = None
    db = database.init_db()
    return db


def _seed_minimum(db):
    """Seed enough data for the WF-02..WF-06 chain to produce non-trivial
    output. Other pipelines (collect_*, compute_hotness, generate_report) will
    likely still produce zero items because they hit real external sources.
    """
    from storage.models import (
        KnowledgeCategory, SearchTerm, AStock, PortfolioAccount,
        PortfolioPosition, NewsRaw, StorageEvent, StockQuote,
    )
    today = date.today()
    with db.tx() as s:
        cat = KnowledgeCategory(name="半导体", signal_type="demand_pickup")
        s.add(cat); s.flush()
        s.add(SearchTerm(
            term="HBM 存储芯片", category_id=cat.id, priority="高",
            transmission_logic="需求 → 涨价",
            a_share_map="江波龙(301308) 三环集团(300408)",
            a_share_codes="301308,300408",
        ))
        s.add_all([
            AStock(code="301308", name="江波龙", sector_tags="存储", tier=1),
            AStock(code="300408", name="三环集团", sector_tags="电子元件", tier=1),
        ])
        s.add(PortfolioAccount(
            user_id="default", total_assets=500_000.0,
            max_drawdown_tolerance=0.20, external_assets_included=False,
            as_of=datetime.utcnow(),
        ))
        s.add_all([
            PortfolioPosition(user_id="default", code="301308", name="江波龙",
                              asset_type="stock", quantity=100, available_quantity=100,
                              current_price=587.60, cost_price=600.1172,
                              market_value=58_760.0, pnl_amount=-1_251.72,
                              pnl_pct=-0.0209, risk_bucket="存储芯片"),
            PortfolioPosition(user_id="default", code="300408", name="三环集团",
                              asset_type="stock", quantity=400, available_quantity=400,
                              current_price=127.05, cost_price=145.1184,
                              market_value=50_820.0, pnl_amount=-7_227.35,
                              pnl_pct=-0.1245, risk_bucket="电子元件"),
        ])
        news = NewsRaw(
            url="https://example.com/smoke",
            title="HBM 需求超预期", summary="DRAM 现货涨价",
            content="江波龙、三环集团受益", source="cls",
            source_label="财联社", published_at=datetime.utcnow(),
        )
        s.add(news); s.flush()
        s.add(StorageEvent(
            news_id=news.id, event_key="smoke-evt", schema_version="v1",
            title="HBM 紧缺", industry_chain="半导体",
            event_type="supply_tight",
            supply_direction="tight", demand_direction="up",
            magnitude=7.0, confidence=0.7,
            evidence_json=json.dumps([{"claim": "HBM 订单可见", "source": "cls", "url": "x"}]),
            counter_evidence_json=json.dumps([]),
        ))
        for i in range(5):
            d = today - timedelta(days=4 - i)
            s.add(StockQuote(
                trade_date=d, code="301308", name="江波龙",
                open=580, close=580 + i, high=585, low=578,
                volume=100_000, turnover=10_000_000.0,
                change_pct=1.0, change_amt=1.0,
            ))
            s.add(StockQuote(
                trade_date=d, code="300408", name="三环集团",
                open=120, close=120 + i, high=125, low=118,
                volume=100_000, turnover=10_000_000.0,
                change_pct=1.0, change_amt=1.0,
            ))


def main() -> int:
    """Run the smoke test; return 0 on success, 1 on any unexpected failure."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        db = _init_temp_db(tmp)
        _seed_minimum(db)

        # Disable Feishu so the morning/evening pipelines don't try to send.
        try:
            from notifier import feishu as feishu_mod
            feishu_mod.FeishuNotifier.is_configured = lambda self: False
        except Exception:
            pass

        # Silence the Feishu card print that some pipelines emit on success —
        # we only want the per-pipeline status table in the smoke output.
        import io, contextlib
        _devnull = io.StringIO()

        # Disable the LLM extractor — only heuristic is exercised.
        import os
        os.environ.pop("MINIMAX_API_KEY", None)

        from pipeline import service

        print(f"{'pipeline':<24} {'status':<10} {'quality':<8} "
              f"{'items':>6}  {'idempotency_key'}")
        print("-" * 100)

        ok = 0
        total = 0
        failures: list[str] = []
        for pipeline, params, idem, expected_min in SMOKE_PIPELINES:
            total += 1
            try:
                run, created = service.create_pipeline_run(
                    pipeline, params, idem, trigger_source="smoke",
                )
                if not created:
                    print(f"{pipeline:<24} (idempotency hit: run_id={run['run_id']})")
                    sys.stdout.flush()
                    # Already-executed runs keep their final status — fine.
                    ok += 1
                    continue
                with contextlib.redirect_stdout(_devnull):
                    detail = service.execute_pipeline_run(run["run_id"])
                status = detail["status"]
                quality = detail["quality_status"]
                items = detail["item_count"]
                print(f"{pipeline:<24} {status:<10} {quality:<8} {items:>6}  {idem}")
                sys.stdout.flush()

                # Verify idempotency: a second call should return the same run.
                run2, created2 = service.create_pipeline_run(
                    pipeline, params, idem, trigger_source="smoke",
                )
                if created2 or run2["run_id"] != run["run_id"]:
                    failures.append(f"{pipeline}: idempotency broken")
                # Verify the run now appears in quality_health (no exception).
                service.quality_health()

                # Floor check
                levels = ["fail", "warn", "pass"]
                if levels.index(quality) >= levels.index(expected_min):
                    ok += 1
                else:
                    failures.append(
                        f"{pipeline}: quality={quality} < expected_min={expected_min}"
                    )
            except Exception as exc:
                print(f"{pipeline:<24} EXCEPTION: {type(exc).__name__}: {exc}")
                failures.append(f"{pipeline}: {type(exc).__name__}: {exc}")

        print("-" * 100)
        print(f"{ok}/{total} pipelines cleared their expected floor.")
        if failures:
            print("\nFAILURES:")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("All schedule-table pipelines route cleanly through service.")
        return 0


if __name__ == "__main__":
    sys.exit(main())