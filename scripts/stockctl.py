"""Convenience CLI for the local stock service running on :8000.

The Dify workflows under /Users/liyuhang/dify are thin HTTP wrappers around
this service. From a shell session it's much faster to invoke the underlying
service directly, especially for testing new idempotency keys or reading the
data the workflows just produced.

Every command prints JSON to stdout and exits non-zero on HTTP / network
errors. Idempotency keys default to ``shell-<UTC-timestamp>`` so repeat
invocations don't get coalesced into a no-op.

Usage examples (run from anywhere):

    # Trigger a high-priority news crawl and watch its run to completion
    python scripts/stockctl.py collect-news high
    python scripts/stockctl.py run-status <run_id>

    # Read news: last 48h, top 20
    python scripts/stockctl.py news --hours 48 --limit 20

    # Filter by source or knowledge-graph category
    python scripts/stockctl.py news --source cls --hours 24
    python scripts/stockctl.py news --category 半导体 --hours 72

    # Wait synchronously for a pipeline to finish (uses wait_for_completion)
    python scripts/stockctl.py extract-events --sync

    # Health / stats snapshot
    python scripts/stockctl.py health
    python scripts/stockctl.py stats
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any


DEFAULT_BASE_URL = "http://localhost:8000"

# Names that 01_stock数据采集执行器 can dispatch to. Must stay in sync with
# pipeline/service.py:PIPELINES — the Dify UI's pipeline select uses these.
COLLECT_NEWS_PIPELINES = {
    "high": "collect_news_high",
    "mid": "collect_news_mid",
}
COLLECT_PIPELINES = {
    "stocks": "collect_stocks",
    "futures": "collect_futures",
}
ADVANCED_PIPELINES = {
    "extract-events": "extract_events",
    "detect-mismatch": "detect_mismatch",
    "score-candidates": "score_candidates",
    "diagnose-portfolio": "diagnose_portfolio",
    "morning-report": "build_morning_report",
    "evening-review": "build_evening_review",
    "hotness": "compute_hotness",
    "report": "generate_report",
}


def _request(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    from urllib.parse import urlencode

    url = f"{base_url.rstrip('/')}{path}"
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        print(f"network error: {e}", file=sys.stderr)
        return 0, None


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _idem(key: str | None) -> str:
    if key:
        return key
    return "shell-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def cmd_collect_news(args: argparse.Namespace) -> int:
    pipeline = COLLECT_NEWS_PIPELINES.get(args.priority)
    if not pipeline:
        print(f"unknown priority: {args.priority}", file=sys.stderr)
        return 2
    payload: dict[str, Any] = {
        "idempotency_key": _idem(args.idempotency_key),
        "trigger_source": "stockctl",
    }
    if args.hours_back is not None:
        payload["hours_back"] = args.hours_back
    if args.sync:
        payload["wait_for_completion"] = True
    code, body = _request(
        args.base_url, "POST", f"/api/v1/pipeline/{pipeline}", body=payload, timeout=120.0,
    )
    print(f"HTTP {code} pipeline={pipeline}")
    _print_json(body)
    return 0 if code in (200, 202) else 1


def cmd_run_status(args: argparse.Namespace) -> int:
    code, body = _request(args.base_url, "GET", f"/api/v1/runs/{args.run_id}", timeout=10.0)
    print(f"HTTP {code} run_id={args.run_id}")
    _print_json(body)
    return 0 if code == 200 else 1


def cmd_runs(args: argparse.Namespace) -> int:
    params = {"limit": args.limit, "pipeline": args.pipeline}
    code, body = _request(args.base_url, "GET", "/api/v1/runs", params=params, timeout=10.0)
    print(f"HTTP {code}")
    _print_json(body)
    return 0 if code == 200 else 1


def cmd_wait(args: argparse.Namespace) -> int:
    """Poll a run until status is terminal or timeout."""
    deadline = time.monotonic() + args.timeout
    last_status = None
    while time.monotonic() < deadline:
        code, body = _request(args.base_url, "GET", f"/api/v1/runs/{args.run_id}", timeout=10.0)
        if code != 200 or not isinstance(body, dict):
            print(f"HTTP {code} body={body}", file=sys.stderr)
            return 1
        last_status = body.get("status")
        if last_status in ("succeeded", "failed", "completed", "cancelled", "error"):
            _print_json(body)
            return 0 if last_status in ("succeeded", "completed") else 2
        time.sleep(args.interval)
    print(f"timeout after {args.timeout}s; last status={last_status}", file=sys.stderr)
    _print_json(body)
    return 3


def cmd_news(args: argparse.Namespace) -> int:
    params = {
        "hours_back": args.hours,
        "limit": args.limit,
        "offset": args.offset,
        "source": args.source,
        "category": args.category,
    }
    code, body = _request(args.base_url, "GET", "/api/news", params=params, timeout=10.0)
    print(f"HTTP {code}")
    _print_json(body)
    return 0 if code == 200 else 1


def cmd_run_pipeline(args: argparse.Namespace) -> int:
    pipeline = ADVANCED_PIPELINES.get(args.pipeline)
    if not pipeline:
        print(f"unknown pipeline: {args.pipeline}", file=sys.stderr)
        return 2
    payload: dict[str, Any] = {
        "idempotency_key": _idem(args.idempotency_key),
        "trigger_source": "stockctl",
    }
    if args.sync:
        payload["wait_for_completion"] = True
    if args.params:
        try:
            payload.update(json.loads(args.params))
        except json.JSONDecodeError as e:
            print(f"invalid --params JSON: {e}", file=sys.stderr)
            return 2
    code, body = _request(
        args.base_url, "POST", f"/api/v1/pipeline/{pipeline}", body=payload, timeout=180.0,
    )
    print(f"HTTP {code} pipeline={pipeline}")
    _print_json(body)
    return 0 if code in (200, 202) else 1


def cmd_health(args: argparse.Namespace) -> int:
    code, body = _request(args.base_url, "GET", "/api/health", timeout=5.0)
    print(f"HTTP {code}")
    _print_json(body)
    return 0 if code == 200 else 1


def cmd_stats(args: argparse.Namespace) -> int:
    code, body = _request(args.base_url, "GET", "/api/stats", timeout=10.0)
    print(f"HTTP {code}")
    _print_json(body)
    return 0 if code == 200 else 1


def cmd_data_health(args: argparse.Namespace) -> int:
    code, body = _request(args.base_url, "GET", "/api/v1/health/data", timeout=10.0)
    print(f"HTTP {code}")
    _print_json(body)
    return 0 if code == 200 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "stockctl")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="stock service base URL")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("collect-news", help="trigger collect_news_high/mid")
    s.add_argument("priority", choices=sorted(COLLECT_NEWS_PIPELINES))
    s.add_argument("--hours-back", type=int, default=None)
    s.add_argument("--idempotency-key", default=None)
    s.add_argument("--sync", action="store_true", help="wait for completion")
    s.set_defaults(func=cmd_collect_news)

    s = sub.add_parser("news", help="read /api/news")
    s.add_argument("--hours", type=int, default=48)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--source", default=None, help="cls / eastmoney / rss")
    s.add_argument("--category", default=None, help="knowledge_graph category name")
    s.set_defaults(func=cmd_news)

    s = sub.add_parser("runs", help="list recent pipeline runs")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--pipeline", default=None)
    s.set_defaults(func=cmd_runs)

    s = sub.add_parser("run-status", help="fetch one run by id")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_run_status)

    s = sub.add_parser("wait", help="poll a run until terminal status")
    s.add_argument("run_id")
    s.add_argument("--timeout", type=float, default=120.0)
    s.add_argument("--interval", type=float, default=2.0)
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("run", help="trigger any advanced pipeline")
    s.add_argument("pipeline", choices=sorted(ADVANCED_PIPELINES))
    s.add_argument("--idempotency-key", default=None)
    s.add_argument("--sync", action="store_true")
    s.add_argument(
        "--params",
        default=None,
        help="extra JSON merged into request body, e.g. '{\"hours_back\": 24}'",
    )
    s.set_defaults(func=cmd_run_pipeline)

    s = sub.add_parser("health", help="GET /api/health")
    s.set_defaults(func=cmd_health)

    s = sub.add_parser("stats", help="GET /api/stats")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("data-health", help="GET /api/v1/health/data")
    s.set_defaults(func=cmd_data_health)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())