"""Idempotent pipeline execution and data-quality gates for Dify.

Dify owns production scheduling.  This module gives each visible Dify node a
small HTTP-callable operation with a durable ``run_id`` that can be polled.
"""
from __future__ import annotations

import importlib
import inspect
import json
import re
import traceback
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, or_
from sqlalchemy.exc import IntegrityError

from storage import get_db
from storage.models import (
    AStock,
    DataQualitySnapshot,
    FuturesPrice,
    NewsRaw,
    PipelineRun,
    PortfolioPosition,
    StockQuote,
)
from pipeline.time_utils import (
    SHANGHAI,
    current_business_date,
    parse_business_date,
    utc_bounds_for_business_date,
)


PIPELINES: dict[str, tuple[str, str, dict[str, Any], int, str]] = {
    # name: module, callable, defaults, minimum useful output, dataset
    "collect_futures": ("scheduler.jobs", "job_collect_futures", {"days_back": 1}, 20, "futures"),
    "collect_stocks": ("scheduler.jobs", "job_collect_stocks", {}, 9, "stock_quotes"),
    "collect_news_high": ("scheduler.jobs", "job_collect_news_high", {"hours_back": 24}, 1, "news_high"),
    "collect_news_mid": ("scheduler.jobs", "job_collect_news_mid", {"hours_back": 24}, 0, "news_mid"),
    "daily_collect": ("pipeline.daily_collect", "run_persist", {}, 1, "daily_collection"),
    "daily_workflow": ("pipeline.daily_workflow", "run_persist", {}, 1, "daily_workflow"),
    "compute_hotness": ("scheduler.jobs", "job_compute_hotness", {}, 21, "sector_heat"),
    "generate_report": ("scheduler.jobs", "job_generate_report", {"report_type": "full"}, 1, "daily_report"),
    # These modules expose ``run_persist`` specifically so the caller can
    # consume their real domain-quality assessment instead of inferring a
    # result from a formatted summary string.
    "extract_events": ("pipeline.events_extract", "run_persist", {"hours_back": 24, "limit": 50}, 0, "storage_events"),
    "detect_mismatch": ("pipeline.mismatch", "run_persist", {"hours_back": 48, "limit": 80}, 1, "mismatch_results"),
    "score_candidates": ("pipeline.score", "run_persist", {"trade_date": ""}, 1, "stock_scores"),
    "diagnose_portfolio": ("pipeline.portfolio_diagnose", "run_persist", {"user_id": "default"}, 1, "portfolio_diagnosis"),
    "build_morning_report": ("pipeline.morning_report", "run_persist", {"user_id": "default"}, 1, "morning_report"),
    "build_evening_review": ("pipeline.evening_review", "run_persist", {}, 1, "evening_review"),
}
PIPELINE_NAMES = tuple(PIPELINES)

# Production order.  A downstream node must never silently consume the last
# successful result from an earlier day.  ``None`` means the step is a root
# collector and can run independently.
PIPELINE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "extract_events": ("collect_news_high",),
    "compute_hotness": ("collect_stocks", "collect_news_high"),
    "detect_mismatch": ("extract_events", "compute_hotness"),
    "score_candidates": ("detect_mismatch", "compute_hotness"),
    "diagnose_portfolio": ("collect_stocks",),
    "build_morning_report": ("diagnose_portfolio", "score_candidates"),
    "build_evening_review": ("diagnose_portfolio", "collect_stocks"),
    "generate_report": ("compute_hotness", "collect_news_high"),
}


class _DependencyBlocked(RuntimeError):
    """Internal control-flow marker for a clean dependency-gate failure."""


def recover_stale_pipeline_runs(max_age_seconds: int = 2700) -> int:
    """Fail abandoned queued/running rows so monitoring never hangs forever."""
    db = get_db()
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=max(60, int(max_age_seconds)))
    recovered = 0
    with db.tx() as s:
        rows = (
            s.query(PipelineRun)
            .filter(PipelineRun.status.in_(("queued", "running")))
            .filter(or_(
                PipelineRun.started_at < cutoff,
                (PipelineRun.started_at.is_(None) & (PipelineRun.created_at < cutoff)),
            ))
            .all()
        )
        for row in rows:
            row.status = "failed"
            row.quality_status = "fail"
            row.error = "stale pipeline run recovered by watchdog"
            row.result_json = _json({
                "status": "failed",
                "quality_status": "fail",
                "quality_message": row.error,
            })
            row.finished_at = now
            recovered += 1
    return recovered


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _decode(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (TypeError, ValueError):
        return {}


def _serialize(row: PipelineRun) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "idempotency_key": row.idempotency_key,
        "pipeline": row.pipeline,
        "status": row.status,
        "quality_status": row.quality_status,
        "trigger_source": row.trigger_source,
        "business_date": row.business_date.isoformat() if row.business_date else None,
        "request": _decode(row.request_json),
        "result": _decode(row.result_json),
        "item_count": row.item_count,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def create_pipeline_run(
    pipeline: str,
    params: dict[str, Any] | None,
    idempotency_key: str,
    trigger_source: str = "dify",
    business_date: date | str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create a queued run, returning ``(run, created)``.

    Reusing an idempotency key returns the existing run and never executes the
    underlying collector twice.
    """
    if pipeline not in PIPELINES:
        raise ValueError(f"unknown pipeline: {pipeline}")
    key = (idempotency_key or "").strip()
    if not key:
        raise ValueError("idempotency_key is required")
    if len(key) > 160:
        raise ValueError("idempotency_key is too long")

    requested_date = parse_business_date(
        business_date or (params or {}).get("business_date")
    )
    db = get_db()
    with db.session() as s:
        existing = s.query(PipelineRun).filter(PipelineRun.idempotency_key == key).first()
        if existing:
            if existing.business_date is None:
                existing.business_date = requested_date
                s.commit()
            # A worker/process can be killed after marking a run running. Do
            # not let Dify reuse that ghost forever; recover it explicitly so
            # the next scheduled day can proceed and monitoring can alert.
            if existing.status in {"queued", "running"} and existing.started_at:
                age = (datetime.utcnow() - existing.started_at).total_seconds()
                if age > 900:
                    existing.status = "failed"
                    existing.quality_status = "fail"
                    existing.error = "stale worker run recovered by watchdog"
                    existing.result_json = _json({"status": "failed", "quality_status": "fail", "quality_message": existing.error})
                    existing.finished_at = datetime.utcnow()
                    s.commit()
            return _serialize(existing), False

    row = PipelineRun(
        run_id=str(uuid.uuid4()),
        idempotency_key=key,
        pipeline=pipeline,
        status="queued",
        quality_status="pending",
        trigger_source=trigger_source,
        business_date=requested_date,
        request_json=_json(params or {}),
    )
    try:
        with db.tx() as s:
            s.add(row)
    except IntegrityError:
        with db.session() as s:
            existing = s.query(PipelineRun).filter(PipelineRun.idempotency_key == key).one()
            return _serialize(existing), False
    return _serialize(row), True


def _extract_count(output: Any, pipeline: str) -> int:
    if isinstance(output, dict):
        for key in ("extracted", "diagnosed", "count"):
            value = output.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        for key in ("mismatches", "scores", "diagnoses"):
            value = output.get(key)
            if isinstance(value, (list, tuple)):
                return len(value)
        for key in ("report", "review"):
            if output.get(key) is not None:
                return 1
    if pipeline == "generate_report":
        return 1 if output else 0
    match = re.search(r"(^|\D)(\d+)\s", str(output or ""))
    return int(match.group(2)) if match else 0


def _quality(count: int, minimum: int) -> tuple[str, str, str]:
    if count >= minimum:
        return "succeeded", "pass", f"{count} items; minimum {minimum}"
    if minimum == 0:
        return "succeeded", "pass", f"{count} items; zero allowed"
    if count == 0:
        return "failed", "fail", f"zero items; minimum {minimum}"
    return "degraded", "warn", f"{count} items below minimum {minimum}"


def _freshness_gate(pipeline: str) -> tuple[str, str] | None:
    """Reject apparently successful market-data runs that are stale.

    Calendar-day tolerances leave room for weekends/holidays while catching
    a collector that silently returned zero rows or a process running old
    code.  This is deliberately evaluated after the collector has persisted
    data, so Dify receives a truthful business-quality result.
    """
    model = {"collect_stocks": StockQuote, "collect_futures": FuturesPrice}.get(pipeline)
    if model is None:
        return None
    db = get_db()
    with db.session() as s:
        latest = s.query(model.trade_date).order_by(model.trade_date.desc()).first()
    latest_date = latest[0] if latest else None
    if isinstance(latest_date, datetime):
        latest_date = latest_date.date()
    if not isinstance(latest_date, date):
        return "failed", f"no persisted {pipeline} data"
    tolerance = 3 if pipeline == "collect_stocks" else 5
    age = (date.today() - latest_date).days
    if age > tolerance:
        return "failed", f"stale {pipeline} data: latest={latest_date.isoformat()} age={age}d"
    return None


def _market_data_is_stale() -> bool:
    return _freshness_gate("collect_stocks") is not None or _freshness_gate("collect_futures") is not None


def _dependency_gate(
    pipeline: str,
    business_date: date,
    trigger_source: str = "dify",
) -> tuple[str, str] | None:
    """Require same-day successful upstream PipelineRuns.

    This prevents a failed morning collector from being masked by yesterday's
    rows in SQLite.  Dependency failures are recorded on the downstream run,
    so Dify can branch and alert without executing stale business logic.
    """
    # Unit/in-memory callers may exercise an individual module deliberately;
    # production sources (Dify, repair, scheduled) always use the gate.
    if trigger_source not in {"dify", "repair", "scheduled", "dify_chat", "orchestrator"}:
        return None
    dependencies = PIPELINE_DEPENDENCIES.get(pipeline, ())
    if not dependencies:
        return None
    db = get_db()
    utc_start, utc_end = utc_bounds_for_business_date(business_date)
    with db.session() as s:
        for dependency in dependencies:
            row = (
                s.query(PipelineRun)
                .filter(PipelineRun.pipeline == dependency)
                .filter(or_(
                    PipelineRun.business_date == business_date,
                    (
                        PipelineRun.business_date.is_(None)
                        & (PipelineRun.created_at >= utc_start)
                        & (PipelineRun.created_at < utc_end)
                    ),
                ))
                .order_by(desc(PipelineRun.created_at))
                .first()
            )
            if row is None:
                return "failed", (
                    f"upstream not run for business_date={business_date.isoformat()}: "
                    f"{dependency}"
                )
            quality_ok = row.quality_status == "pass"
            # Evidence stages can be intentionally degraded (for example,
            # single-source mismatch evidence).  Let downstream scoring run
            # so it can carry an observation-only warning; hard failures still
            # block the chain.
            if dependency in {"extract_events", "detect_mismatch", "score_candidates"} and row.status in {"succeeded", "degraded"} and row.quality_status == "warn":
                quality_ok = True
            if row.status not in {"succeeded", "degraded"} or not quality_ok:
                return "failed", (
                    f"upstream not qualified: {dependency} "
                    f"status={row.status} quality={row.quality_status}"
                )
    return None


def _dependency_warning(
    pipeline: str,
    business_date: date,
    trigger_source: str = "dify",
) -> str:
    """Return a warning when an allowed upstream is observation-only."""
    if trigger_source not in {"dify", "repair", "scheduled", "dify_chat", "orchestrator"}:
        return ""
    dependencies = PIPELINE_DEPENDENCIES.get(pipeline, ())
    if not dependencies:
        return ""
    utc_start, utc_end = utc_bounds_for_business_date(business_date)
    db = get_db()
    warnings: list[str] = []
    with db.session() as s:
        for dependency in dependencies:
            row = (
                s.query(PipelineRun)
                .filter(PipelineRun.pipeline == dependency)
                .filter(or_(
                    PipelineRun.business_date == business_date,
                    (
                        PipelineRun.business_date.is_(None)
                        & (PipelineRun.created_at >= utc_start)
                        & (PipelineRun.created_at < utc_end)
                    ),
                ))
                .order_by(desc(PipelineRun.created_at))
                .first()
            )
            if row is not None and (
                row.quality_status == "warn" or row.status == "degraded"
            ):
                warnings.append(f"{dependency}={row.status}/{row.quality_status}")
    return ", ".join(warnings)


def _stock_coverage_gate(business_date: date) -> tuple[str, str] | None:
    """Require broad research-universe coverage and complete holdings coverage."""
    db = get_db()
    with db.session() as s:
        master = {str(code).zfill(6) for (code,) in s.query(AStock.code).all() if code}
        holdings = {
            str(code).zfill(6)
            for (code,) in s.query(PortfolioPosition.code).distinct().all()
            if code
        }
        expected = master | holdings
        available = {
            str(code).zfill(6)
            for (code,) in (
                s.query(StockQuote.code)
                .filter(StockQuote.trade_date == business_date)
                .distinct()
                .all()
            )
            if code
        }
    if not expected:
        return "failed", "stock universe is empty"
    universe_coverage = len(available & expected) / len(expected)
    missing_holdings = sorted(holdings - available)
    if missing_holdings:
        return "failed", (
            f"portfolio quote coverage incomplete: "
            f"{len(holdings) - len(missing_holdings)}/{len(holdings)}; "
            f"missing={','.join(missing_holdings)}"
        )
    if universe_coverage < 0.95:
        return "failed", (
            f"stock universe coverage {universe_coverage:.1%}; "
            f"required >=95% ({len(available & expected)}/{len(expected)})"
        )
    return None


def _news_source_gate(business_date: date) -> tuple[str, str] | None:
    """Ensure the collection window contains more than one independent source."""
    utc_start, utc_end = utc_bounds_for_business_date(business_date)
    db = get_db()
    with db.session() as s:
        rows = (
            s.query(NewsRaw.source, func.count(NewsRaw.id))
            .filter(NewsRaw.fetched_at >= utc_start, NewsRaw.fetched_at < utc_end)
            .group_by(NewsRaw.source)
            .all()
        )
    sources = {str(source) for source, count in rows if source and int(count or 0) > 0}
    total = sum(int(count or 0) for _, count in rows)
    if total == 0:
        return "failed", "no news fetched for business date"
    if len(sources) < 2:
        return "degraded", f"single-source news window: {','.join(sorted(sources))}"
    return None


def _result_payload(output: Any, message: str) -> dict[str, Any]:
    """Keep structured pipeline output available to Dify without ORM leaks."""
    if isinstance(output, dict):
        payload: dict[str, Any] = {
            "summary": str(output.get("summary", "")),
            "quality_message": message,
        }
        for key in ("status", "quality_status", "candidates", "extracted"):
            if key in output:
                payload[key] = output[key]
        for key in ("mismatches", "scores", "diagnoses"):
            value = output.get(key)
            if isinstance(value, (list, tuple)):
                payload[f"{key}_count"] = len(value)
        payload["output_type"] = "structured"
        return payload
    return {"output": str(output or ""), "quality_message": message}


def execute_pipeline_run(run_id: str) -> dict[str, Any]:
    """Execute one queued run and persist both execution and quality status."""
    db = get_db()
    with db.tx() as s:
        row = s.query(PipelineRun).filter(PipelineRun.run_id == run_id).one_or_none()
        if row is None:
            raise KeyError(run_id)
        if row.status not in {"queued"}:
            return _serialize(row)
        row.status = "running"
        row.started_at = datetime.utcnow()
        pipeline = row.pipeline
        trigger_source = row.trigger_source
        business_date = row.business_date or current_business_date()
        params = _decode(row.request_json)

    module_name, function_name, defaults, minimum, dataset = PIPELINES[pipeline]
    requested_kwargs = {**defaults, **params}
    kwargs: dict[str, Any] = {}
    try:
        dependency_failure = _dependency_gate(pipeline, business_date, trigger_source)
        if dependency_failure is not None:
            status, message = dependency_failure
            quality_status = "fail"
            count = 0
            result = {"status": status, "quality_status": quality_status, "quality_message": message}
            error = message
            raise _DependencyBlocked(message)
        upstream_warning = _dependency_warning(pipeline, business_date, trigger_source)
        if upstream_warning and pipeline in {
            "build_morning_report", "build_evening_review", "generate_report"
        }:
            requested_kwargs["push"] = False
        module = importlib.import_module(module_name)
        fn = getattr(module, function_name)
        # The public scheduler functions have legacy JobRun decorators.  Dify
        # runs use PipelineRun as their source of truth, so call the wrapped
        # implementation to avoid double accounting.
        target = getattr(fn, "__wrapped__", fn)
        signature = inspect.signature(target)
        accepts_var_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        # Pipeline endpoints are intentionally generic so Dify can attach
        # operational controls such as ``push=false``.  Do not leak a control
        # intended for one pipeline into another pipeline's strict function
        # signature (for example portfolio diagnosis does not send Feishu).
        kwargs = (
            requested_kwargs
            if accepts_var_kwargs
            else {key: value for key, value in requested_kwargs.items() if key in signature.parameters}
        )
        output = target(**kwargs)
        count = _extract_count(output, pipeline)
        if isinstance(output, dict) and output.get("status") and output.get("quality_status"):
            status = str(output["status"])
            quality_status = str(output["quality_status"])
            message = str(output.get("summary") or f"{count} items; pipeline assessment")
        else:
            status, quality_status, message = _quality(count, minimum)
        if upstream_warning and quality_status == "pass":
            status = "degraded"
            quality_status = "warn"
            message = f"upstream observation-only: {upstream_warning}; {message}"
        freshness = _freshness_gate(pipeline)
        if freshness is not None and quality_status == "pass":
            status, message = freshness
            quality_status = "fail"
        if pipeline == "collect_stocks" and quality_status == "pass":
            coverage = _stock_coverage_gate(business_date)
            if coverage is not None:
                status, message = coverage
                quality_status = "fail" if status == "failed" else "warn"
        if pipeline in {"collect_news_high", "collect_news_mid"} and quality_status == "pass":
            source_quality = _news_source_gate(business_date)
            if source_quality is not None:
                status, message = source_quality
                quality_status = "fail" if status == "failed" else "warn"
        if pipeline in {"score_candidates", "diagnose_portfolio", "build_morning_report", "build_evening_review", "generate_report"}:
            if _market_data_is_stale() and quality_status == "pass":
                status = "degraded"
                quality_status = "warn"
                message = f"market data stale; research output is observation-only ({message})"
        result = _result_payload(output, message)
        # Expose the effective gate decision, not the domain function's
        # pre-gate assessment, to Dify's JSON parser.
        result["status"] = status
        result["quality_status"] = quality_status
        error = "" if quality_status != "fail" else message
    except _DependencyBlocked:
        # The structured failure above is already complete; do not replace it
        # with a traceback that hides the missing upstream run.
        pass
    except Exception as exc:
        count = 0
        status, quality_status = "failed", "fail"
        message = f"{type(exc).__name__}: {exc}"
        result = {"quality_message": message}
        error = message + "\n" + traceback.format_exc()[-3000:]

    finished = datetime.utcnow()
    with db.tx() as s:
        row = s.query(PipelineRun).filter(PipelineRun.run_id == run_id).one()
        row.status = status
        row.quality_status = quality_status
        row.item_count = count
        row.result_json = _json(result)
        row.error = error
        row.finished_at = finished
        s.add(DataQualitySnapshot(
            run_id=run_id,
            dataset=dataset,
            status=quality_status,
            item_count=count,
            min_expected=minimum,
            message=message,
            details_json=_json({"pipeline": pipeline, "params": kwargs, "requested_params": requested_kwargs}),
            checked_at=finished,
        ))
        if pipeline == "daily_collect":
            # The composite root collector must refresh the same dataset
            # health cards as the individual pipeline endpoints; otherwise
            # the UI would keep showing yesterday's news snapshot despite a
            # successful morning collection.
            news_count = s.query(NewsRaw).filter(
                NewsRaw.fetched_at >= finished - timedelta(hours=24)
            ).count()
            latest_stock_date = s.query(func.max(StockQuote.trade_date)).scalar()
            latest_futures_date = s.query(func.max(FuturesPrice.trade_date)).scalar()
            stock_count = s.query(StockQuote).filter(StockQuote.trade_date == latest_stock_date).count() if latest_stock_date else 0
            futures_count = s.query(FuturesPrice).filter(FuturesPrice.trade_date == latest_futures_date).count() if latest_futures_date else 0
            for dataset_name, item_count, minimum in (
                ("news_high", news_count, 1),
                ("stock_quotes", stock_count, 9),
                ("futures", futures_count, 20),
            ):
                snapshot_status = "pass" if item_count >= minimum else "fail"
                s.add(DataQualitySnapshot(
                    run_id=run_id,
                    dataset=dataset_name,
                    status=snapshot_status,
                    item_count=item_count,
                    min_expected=minimum,
                    message=f"daily_collect: {item_count} items; minimum {minimum}",
                    details_json=_json({"pipeline": pipeline, "composite": True}),
                    checked_at=finished,
                ))
    return get_pipeline_run(run_id)


def get_pipeline_run(run_id: str) -> dict[str, Any]:
    db = get_db()
    with db.session() as s:
        row = s.query(PipelineRun).filter(PipelineRun.run_id == run_id).one_or_none()
        if row is None:
            raise KeyError(run_id)
        return _serialize(row)


def list_pipeline_runs(limit: int = 50, pipeline: str | None = None) -> list[dict[str, Any]]:
    db = get_db()
    with db.session() as s:
        q = s.query(PipelineRun)
        if pipeline:
            q = q.filter(PipelineRun.pipeline == pipeline)
        rows = q.order_by(desc(PipelineRun.created_at)).limit(max(1, min(limit, 200))).all()
        return [_serialize(row) for row in rows]


def quality_health() -> dict[str, Any]:
    """Return current-business-date health without hiding historical results.

    The latest snapshot for every dataset remains visible for diagnostics, but
    an old failure must not permanently poison today's aggregate status. Only
    snapshots belonging to today's Shanghai business date and freshness checks
    participate in ``overall``. Scheduled outputs become failures only after
    their configured local due time.
    """
    recover_stale_pipeline_runs()
    db = get_db()
    local_now = datetime.now(SHANGHAI)
    today = current_business_date(local_now)
    with db.session() as s:
        rows = s.query(DataQualitySnapshot).order_by(desc(DataQualitySnapshot.checked_at)).all()
        latest: dict[str, DataQualitySnapshot] = {}
        for row in rows:
            latest.setdefault(row.dataset, row)
        run_ids = {row.run_id for row in latest.values()}
        run_dates = {
            row.run_id: row.business_date
            for row in s.query(PipelineRun).filter(PipelineRun.run_id.in_(run_ids)).all()
        } if run_ids else {}
        datasets = {
            name: {
                "status": row.status,
                "item_count": row.item_count,
                "min_expected": row.min_expected,
                "message": row.message,
                "run_id": row.run_id,
                "checked_at": row.checked_at.isoformat() if row.checked_at else None,
                "business_date": run_dates.get(row.run_id).isoformat()
                if run_dates.get(row.run_id) else None,
                "current": run_dates.get(row.run_id) == today,
                "included_in_overall": run_dates.get(row.run_id) == today,
            }
            for name, row in latest.items()
        }
        # A stale market-data snapshot must remain visible even if the most
        # recent pipeline execution was reported as technically successful.
        freshness: dict[str, Any] = {}
        for name, model, tolerance in (
            ("stock_quotes", StockQuote, 3),
            ("futures", FuturesPrice, 5),
        ):
            latest_row = s.query(model.trade_date).order_by(model.trade_date.desc()).first()
            latest_date = latest_row[0] if latest_row else None
            if isinstance(latest_date, datetime):
                latest_date = latest_date.date()
            age = (today - latest_date).days if isinstance(latest_date, date) else None
            stale = age is None or age > tolerance
            freshness[name] = {
                "status": "fail" if stale else "pass",
                "latest_trade_date": latest_date.isoformat() if latest_date else None,
                "age_days": age,
                "max_age_days": tolerance,
                "message": "no data" if latest_date is None else ("stale" if stale else "fresh"),
            }
        statuses = {
            d["status"] for d in datasets.values() if d["included_in_overall"]
        } | {
            d["status"] for d in freshness.values()
        }

        # Missing scheduled outputs should only alert after their local due
        # time. This keeps yesterday's evening failure visible in history while
        # preventing it from turning today's pre-20:30 health red.
        schedule_expectations: dict[str, Any] = {}
        if today.weekday() < 5:
            for dataset, due_at in (
                ("daily_workflow", (5, 30)),
                ("morning_report", (8, 20)),
                ("evening_review", (20, 30)),
            ):
                due = local_now.replace(
                    hour=due_at[0], minute=due_at[1], second=0, microsecond=0,
                )
                is_due = local_now >= due
                has_current = bool(datasets.get(dataset, {}).get("current"))
                expectation_status = "pass" if has_current else "fail" if is_due else "pending"
                schedule_expectations[dataset] = {
                    "due_at": due.isoformat(),
                    "due": is_due,
                    "has_current_result": has_current,
                    "status": expectation_status,
                }
                if expectation_status == "fail":
                    statuses.add("fail")
        overall = "unknown"
        if datasets or freshness:
            overall = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
        return {
            "overall": overall,
            "business_date": today.isoformat(),
            "datasets": datasets,
            "freshness": freshness,
            "schedule_expectations": schedule_expectations,
            "pipeline_names": list(PIPELINE_NAMES),
        }
