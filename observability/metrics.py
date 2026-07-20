"""
Prometheus metrics for the stock analysis system.

Exposes system + business metrics in Prometheus text format at /metrics.
"""
from __future__ import annotations

import os
from datetime import datetime

try:
    from prometheus_client import (
        Counter, Histogram, Gauge, Info,
        CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST,
    )
    HAS_PROM = True
except ImportError:
    HAS_PROM = False

from loguru import logger

from storage import get_db
from storage.models import (
    IndustryEvent, NewsRaw, DailyReport, JobRun,
    FuturesPrice, StockQuote, KnowledgeSignal,
)


# Custom registry (avoid polluting global default)
REGISTRY = CollectorRegistry() if HAS_PROM else None

# Counters
events_total = None
news_collected_total = None
reports_generated_total = None
job_runs_total = None
api_requests_total = None
feishu_pushes_total = None
favorites_added_total = None
signals_viewed_total = None
events_viewed_total = None
qa_questions_total = None
exports_total = None
predictions_made_total = None
reminders_triggered_total = None

# Histograms
api_request_duration = None
job_duration = None
scraper_duration = None

# Gauges
events_future_count = None
events_past_count = None
news_count = None
reports_count = None
last_job_status = None
db_size_bytes = None
scheduler_running = None

# Info
system_info = None


def init_metrics() -> None:
    """Initialize all metric objects. Call once at startup."""
    global events_total, news_collected_total, reports_generated_total
    global job_runs_total, api_requests_total, feishu_pushes_total
    global api_request_duration, job_duration, scraper_duration
    global events_future_count, events_past_count, news_count
    global reports_count, last_job_status, db_size_bytes, scheduler_running
    global system_info

    if not HAS_PROM:
        logger.warning("prometheus_client not installed, metrics disabled")
        return

    # Counters
    events_total = Counter(
        "stock_events_total", "Total events added by source",
        ["source"], registry=REGISTRY,
    )
    news_collected_total = Counter(
        "stock_news_collected_total", "Total news articles collected",
        ["source"], registry=REGISTRY,
    )
    reports_generated_total = Counter(
        "stock_reports_generated_total", "Total reports generated",
        ["report_type"], registry=REGISTRY,
    )
    job_runs_total = Counter(
        "stock_job_runs_total", "Total job runs by job_id and status",
        ["job_id", "status"], registry=REGISTRY,
    )
    api_requests_total = Counter(
        "stock_api_requests_total", "Total API requests by endpoint and method",
        ["endpoint", "method", "status"], registry=REGISTRY,
    )
    feishu_pushes_total = Counter(
        "stock_feishu_pushes_total", "Total Feishu pushes by status",
        ["status"], registry=REGISTRY,
    )
    favorites_added_total = Counter(
        "stock_favorites_added_total", "Total favorites added by user/item_type",
        ["user_id", "item_type"], registry=REGISTRY,
    )
    signals_viewed_total = Counter(
        "stock_signals_viewed_total", "Total signal detail views",
        ["grade"], registry=REGISTRY,
    )
    events_viewed_total = Counter(
        "stock_events_viewed_total", "Total event detail views",
        ["industry"], registry=REGISTRY,
    )
    qa_questions_total = Counter(
        "stock_qa_questions_total", "Total Q&A questions by intent",
        ["intent"], registry=REGISTRY,
    )
    exports_total = Counter(
        "stock_exports_total", "Total report/event exports by format",
        ["export_type", "format"], registry=REGISTRY,
    )
    predictions_made_total = Counter(
        "stock_predictions_made_total", "Total predictions by model",
        ["model"], registry=REGISTRY,
    )
    reminders_triggered_total = Counter(
        "stock_reminders_triggered_total", "Total event reminders sent",
        ["urgency"], registry=REGISTRY,
    )

    # Histograms
    api_request_duration = Histogram(
        "stock_api_request_duration_seconds", "API request duration in seconds",
        ["endpoint"], registry=REGISTRY,
    )
    job_duration = Histogram(
        "stock_job_duration_seconds", "Job duration in seconds",
        ["job_id"], registry=REGISTRY,
    )
    scraper_duration = Histogram(
        "stock_scraper_duration_seconds", "Scraper duration in seconds",
        ["scraper"], registry=REGISTRY,
    )

    # Gauges
    events_future_count = Gauge(
        "stock_events_future", "Number of future events", registry=REGISTRY,
    )
    events_past_count = Gauge(
        "stock_events_past", "Number of past events", registry=REGISTRY,
    )
    news_count = Gauge(
        "stock_news_count", "Total news articles in DB", registry=REGISTRY,
    )
    reports_count = Gauge(
        "stock_reports_count", "Total reports in DB", registry=REGISTRY,
    )
    last_job_status = Gauge(
        "stock_last_job_status", "Last job run status (1=ok, 0=error, -1=running)",
        ["job_id"], registry=REGISTRY,
    )
    db_size_bytes = Gauge(
        "stock_db_size_bytes", "SQLite database file size in bytes", registry=REGISTRY,
    )
    scheduler_running = Gauge(
        "stock_scheduler_running", "1 if scheduler is running", registry=REGISTRY,
    )

    # Info
    system_info = Info(
        "stock_system", "Stock Analysis System info", registry=REGISTRY,
    )
    system_info.info({
        "version": "0.1.0",
        "python": os.popen("python3 --version").read().strip(),
        "started_at": datetime.utcnow().isoformat(),
    })


def update_db_stats() -> None:
    """Update database statistics gauges from current DB state."""
    if not HAS_PROM:
        return
    try:
        db = get_db()
        with db.session() as s:
            events_future_count.set(
                s.query(IndustryEvent).filter(IndustryEvent.is_future == True).count()
            )
            events_past_count.set(
                s.query(IndustryEvent).filter(IndustryEvent.is_future == False).count()
            )
            news_count.set(s.query(NewsRaw).count())
            reports_count.set(s.query(DailyReport).count())

            # Last job status per job_id
            for jid in ["collect_futures", "collect_news_high", "compute_hotness",
                        "generate_report", "events_detect", "event_reminder"]:
                last = (
                    s.query(JobRun)
                    .filter(JobRun.job_id == jid)
                    .order_by(JobRun.started_at.desc())
                    .first()
                )
                if last:
                    val = 1.0 if last.status == "ok" else (0.0 if last.status == "error" else -1.0)
                    last_job_status.labels(job_id=jid).set(val)

            # DB file size
            from config.settings import settings
            if settings.db_path.exists():
                db_size_bytes.set(settings.db_path.stat().st_size)
    except Exception as e:
        logger.warning(f"Failed to update DB stats: {e}")


def render_metrics() -> tuple[bytes, str]:
    """Render current metrics in Prometheus text format.

    Returns (content, content_type).
    """
    if not HAS_PROM:
        return (
            b"# prometheus_client not installed\n",
            "text/plain; charset=utf-8",
        )
    update_db_stats()
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


# Helper functions for instrumentation
def inc_event(source: str, n: int = 1) -> None:
    if HAS_PROM and events_total:
        events_total.labels(source=source).inc(n)


def inc_news(source: str, n: int = 1) -> None:
    if HAS_PROM and news_collected_total:
        news_collected_total.labels(source=source).inc(n)


def inc_report(report_type: str) -> None:
    if HAS_PROM and reports_generated_total:
        reports_generated_total.labels(report_type=report_type).inc()


def inc_job(job_id: str, status: str) -> None:
    if HAS_PROM and job_runs_total:
        job_runs_total.labels(job_id=job_id, status=status).inc()


def observe_job_duration(job_id: str, duration_sec: float) -> None:
    if HAS_PROM and job_duration:
        job_duration.labels(job_id=job_id).observe(duration_sec)


def observe_scraper_duration(scraper: str, duration_sec: float) -> None:
    if HAS_PROM and scraper_duration:
        scraper_duration.labels(scraper=scraper).observe(duration_sec)


class ScraperTimer:
    """Context manager that records scraper duration to Prometheus.

    Usage:
        with ScraperTimer("sina"):
            ... fetch ...
    """

    __slots__ = ("scraper", "_t0")

    def __init__(self, scraper: str) -> None:
        self.scraper = scraper
        self._t0: float = 0.0

    def __enter__(self) -> "ScraperTimer":
        import time
        self._t0 = time.time()
        return self

    def __exit__(self, *exc_info) -> None:
        import time
        observe_scraper_duration(self.scraper, time.time() - self._t0)


def inc_feishu(status: str) -> None:
    if HAS_PROM and feishu_pushes_total:
        feishu_pushes_total.labels(status=status).inc()


def inc_favorite(user_id: str, item_type: str) -> None:
    if HAS_PROM and favorites_added_total:
        favorites_added_total.labels(user_id=user_id, item_type=item_type).inc()


def inc_signal_view(grade: str = "unknown") -> None:
    if HAS_PROM and signals_viewed_total:
        signals_viewed_total.labels(grade=grade).inc()


def inc_event_view(industry: str = "unknown") -> None:
    if HAS_PROM and events_viewed_total:
        events_viewed_total.labels(industry=industry).inc()


def inc_qa(intent: str) -> None:
    if HAS_PROM and qa_questions_total:
        qa_questions_total.labels(intent=intent).inc()


def inc_export(export_type: str, fmt: str) -> None:
    if HAS_PROM and exports_total:
        exports_total.labels(export_type=export_type, format=fmt).inc()


def inc_prediction(model: str) -> None:
    if HAS_PROM and predictions_made_total:
        predictions_made_total.labels(model=model).inc()


def inc_reminder(urgency: str) -> None:
    if HAS_PROM and reminders_triggered_total:
        reminders_triggered_total.labels(urgency=urgency).inc()