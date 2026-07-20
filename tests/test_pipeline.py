from __future__ import annotations

import sys
import types
from datetime import date, datetime, timedelta


def test_pipeline_run_is_idempotent_and_quality_aware(in_memory_db, monkeypatch):
    from pipeline import service

    fake = types.ModuleType("tests.fake_pipeline_module")
    fake.collect = lambda: "0 quotes"
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    monkeypatch.setitem(
        service.PIPELINES,
        "test_zero",
        (fake.__name__, "collect", {}, 9, "test_quotes"),
    )

    first, created = service.create_pipeline_run(
        "test_zero", {}, "2026-07-12:test_zero", trigger_source="test",
    )
    assert created is True

    duplicate, created_again = service.create_pipeline_run(
        "test_zero", {}, "2026-07-12:test_zero", trigger_source="test",
    )
    assert created_again is False
    assert duplicate["run_id"] == first["run_id"]

    result = service.execute_pipeline_run(first["run_id"])
    assert result["status"] == "failed"
    assert result["quality_status"] == "fail"
    assert result["item_count"] == 0

    health = service.quality_health()
    assert health["overall"] == "fail"
    assert health["datasets"]["test_quotes"]["min_expected"] == 9
    assert health["datasets"]["test_quotes"]["current"] is True
    assert health["datasets"]["test_quotes"]["included_in_overall"] is True


def test_pipeline_degraded_when_below_minimum(in_memory_db, monkeypatch):
    from pipeline import service

    fake = types.ModuleType("tests.fake_pipeline_degraded")
    fake.collect = lambda: "3 items"
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    monkeypatch.setitem(
        service.PIPELINES,
        "test_degraded",
        (fake.__name__, "collect", {}, 5, "test_items"),
    )
    run, _ = service.create_pipeline_run("test_degraded", {}, "degraded-key")
    result = service.execute_pipeline_run(run["run_id"])
    assert result["status"] == "degraded"
    assert result["quality_status"] == "warn"
    assert result["item_count"] == 3


def test_historical_failure_remains_visible_but_is_not_current_health(in_memory_db, monkeypatch):
    from pipeline import service

    fake = types.ModuleType("tests.fake_historical_failure")
    fake.collect = lambda: "0 items"
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    monkeypatch.setitem(
        service.PIPELINES,
        "test_historical",
        (fake.__name__, "collect", {}, 1, "historical_dataset"),
    )
    run, _ = service.create_pipeline_run(
        "test_historical",
        {},
        "historical-failure",
        business_date=date.today() - timedelta(days=1),
    )
    service.execute_pipeline_run(run["run_id"])

    dataset = service.quality_health()["datasets"]["historical_dataset"]
    assert dataset["status"] == "fail"
    assert dataset["current"] is False
    assert dataset["included_in_overall"] is False


def test_pipeline_rejects_missing_idempotency_key(in_memory_db):
    from pipeline import service
    try:
        service.create_pipeline_run("collect_stocks", {}, "")
    except ValueError as exc:
        assert "idempotency_key" in str(exc)
    else:
        raise AssertionError("missing idempotency key must fail")


def test_pipeline_preserves_structured_quality_assessment(in_memory_db, monkeypatch):
    from pipeline import service

    fake = types.ModuleType("tests.fake_pipeline_structured")
    fake.run_persist = lambda: {
        "status": "degraded",
        "quality_status": "warn",
        "summary": "行情过期，未生成交易建议",
        "scores": [],
    }
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    monkeypatch.setitem(
        service.PIPELINES,
        "test_structured",
        (fake.__name__, "run_persist", {}, 1, "structured_test"),
    )
    run, _ = service.create_pipeline_run("test_structured", {}, "structured-key")
    result = service.execute_pipeline_run(run["run_id"])

    assert result["status"] == "degraded"
    assert result["quality_status"] == "warn"
    assert result["result"]["summary"] == "行情过期，未生成交易建议"


def test_pipeline_drops_unaccepted_operational_controls(in_memory_db, monkeypatch):
    """A generic Dify control such as push=false must not break strict jobs."""
    from pipeline import service

    fake = types.ModuleType("tests.fake_pipeline_controls")
    fake.run_persist = lambda user_id="default": {
        "status": "succeeded",
        "quality_status": "pass",
        "summary": f"user={user_id}",
        "diagnoses": ["one"],
    }
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    monkeypatch.setitem(
        service.PIPELINES,
        "test_controls",
        (fake.__name__, "run_persist", {"user_id": "default"}, 1, "control_test"),
    )
    run, _ = service.create_pipeline_run("test_controls", {"push": False}, "controls-key")
    result = service.execute_pipeline_run(run["run_id"])

    assert result["status"] == "succeeded"
    assert result["quality_status"] == "pass"
    assert result["result"]["summary"] == "user=default"


def test_dependency_gate_uses_explicit_business_date(in_memory_db):
    from pipeline import service
    from storage.models import PipelineRun

    with in_memory_db.tx() as s:
        s.add(PipelineRun(
            run_id="upstream-run",
            idempotency_key="upstream-key",
            pipeline="collect_stocks",
            status="succeeded",
            quality_status="pass",
            trigger_source="dify",
            business_date=date(2026, 7, 16),
            request_json="{}",
            result_json="{}",
            item_count=10,
            error="",
            created_at=datetime(2026, 7, 15, 21, 30),
        ))

    assert service._dependency_gate(
        "diagnose_portfolio", date(2026, 7, 16), "dify"
    ) is None
    blocked = service._dependency_gate(
        "diagnose_portfolio", date(2026, 7, 17), "dify"
    )
    assert blocked is not None
    assert "business_date=2026-07-17" in blocked[1]
