"""Deterministic root-source collection used by the daily Dify trigger.

The three source collectors are intentionally run in order.  A single durable
PipelineRun gives the scheduler one idempotency boundary while preserving each
collector's own quality snapshot and logs.
"""
from __future__ import annotations

from scheduler.jobs import job_collect_futures, job_collect_stocks, job_collect_news_high
import re


def _count(value: object) -> int:
    match = re.search(r"(\d+)", str(value or ""))
    return int(match.group(1)) if match else 0


def run_persist() -> dict:
    outputs = {}
    outputs["collect_futures"] = job_collect_futures(days_back=1)
    outputs["collect_stocks"] = job_collect_stocks()
    outputs["collect_news_high"] = job_collect_news_high(hours_back=24)
    counts = {name: _count(value) for name, value in outputs.items()}
    minimums = {"collect_futures": 20, "collect_stocks": 9, "collect_news_high": 1}
    failed = [name for name, minimum in minimums.items() if counts[name] < minimum]
    status = "failed" if failed else "succeeded"
    quality = "fail" if failed else "pass"
    return {
        "status": status,
        "quality_status": quality,
        "summary": "; ".join(f"{k}={v}" for k, v in outputs.items())
        + (f"; failed={','.join(failed)}" if failed else ""),
        "count": sum(counts.values()),
        "counts": counts,
        "outputs": outputs,
    }
