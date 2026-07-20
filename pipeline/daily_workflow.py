"""Serial production workflow executed behind one Dify schedule.

Each child remains a first-class PipelineRun, so the monitoring UI and Dify
can inspect, retry, and audit every stage independently.  The parent stops on
the first hard quality failure and never converts a partial chain into a
successful report.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from pipeline.time_utils import parse_business_date


WORKFLOW_VERSION = "v2"

DAILY_STEPS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("collect_futures", {"days_back": 1}),
    ("collect_stocks", {}),
    ("collect_news_high", {"hours_back": 24}),
    ("compute_hotness", {}),
    ("extract_events", {"hours_back": 24, "limit": 80}),
    ("detect_mismatch", {"hours_back": 48, "limit": 100}),
    ("score_candidates", {}),
    ("diagnose_portfolio", {"user_id": "default"}),
)


def run_persist(
    business_date: str = "",
    user_id: str = "default",
) -> dict[str, Any]:
    """Run the morning research chain in strict order.

    Morning report delivery remains a separate 08:20 Dify schedule so the
    expensive source collection can start at 05:30 without pushing a report
    before the user-requested delivery time.
    """
    from pipeline.service import create_pipeline_run, execute_pipeline_run

    day = parse_business_date(business_date)
    assert isinstance(day, date)
    day_text = day.isoformat()
    runs: list[dict[str, Any]] = []
    warned = False

    for pipeline, defaults in DAILY_STEPS:
        params = {**defaults, "business_date": day_text}
        if pipeline in {"diagnose_portfolio"}:
            params["user_id"] = user_id
        if pipeline == "score_candidates":
            params["trade_date"] = day_text
        if pipeline == "diagnose_portfolio":
            params["trade_date"] = day_text

        key = f"dify:daily_workflow:{WORKFLOW_VERSION}:{day_text}:{pipeline}"
        run, created = create_pipeline_run(
            pipeline,
            params,
            key,
            trigger_source="orchestrator",
            business_date=day,
        )
        if created or run["status"] == "queued":
            run = execute_pipeline_run(run["run_id"])
        runs.append(run)

        if run["status"] == "failed" or run["quality_status"] == "fail":
            return {
                "status": "failed",
                "quality_status": "fail",
                "count": len(runs),
                "summary": (
                    f"business_date={day_text}; stopped_at={pipeline}; "
                    f"reason={run.get('error') or run.get('result', {}).get('quality_message', '')}"
                ),
                "runs": runs,
            }
        if run["quality_status"] == "warn" or run["status"] == "degraded":
            warned = True

    return {
        "status": "degraded" if warned else "succeeded",
        "quality_status": "warn" if warned else "pass",
        "count": len(runs),
        "summary": f"business_date={day_text}; completed={len(runs)}/{len(DAILY_STEPS)}",
        "runs": runs,
    }
