"""
Job definitions for APScheduler.

Each job is a callable that performs one slice of the daily workflow.
All jobs write execution history to JobRun table for the UI to display.
"""
from __future__ import annotations

import functools
import traceback
from datetime import datetime, date, timedelta
from loguru import logger

from storage import get_db
from storage.models import JobRun
from config.settings import settings


def track_run(job_id: str, job_name: str, trigger_type: str = "scheduled"):
    """Decorator: record every job invocation to JobRun table.

    Provides error capture, duration measurement, and UI-visible status.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            db = get_db()
            run = JobRun(
                job_id=job_id,
                job_name=job_name,
                started_at=datetime.utcnow(),
                status="running",
                trigger_type=trigger_type,
            )
            with db.tx() as s:
                s.add(run)
                s.flush()
                run_id = run.id

            error = ""
            output = ""
            try:
                output = fn(*args, **kwargs) or ""
                status = "ok"
            except Exception as e:
                logger.exception(f"[JOB:{job_id}] failed: {e}")
                error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}"
                status = "error"

            finished = datetime.utcnow()
            duration = (finished - run.started_at).total_seconds()
            with db.tx() as s:
                r = s.get(JobRun, run_id)
                if r:
                    r.finished_at = finished
                    r.status = status
                    r.duration_sec = duration
                    r.error = error
                    r.output_summary = str(output)[:1000]
            # Record Prometheus metrics (no-op if prom client not installed)
            try:
                from observability.metrics import (
                    observe_job_duration, inc_job,
                )
                observe_job_duration(job_id, duration)
                inc_job(job_id, status)
            except Exception:
                pass
            return output
        return wrapper
    return decorator


@track_run("collect_futures", "Collect futures (L3)", trigger_type="scheduled")
def job_collect_futures(days_back: int = 1):
    """L3: Collect end-of-day futures prices."""
    logger.info(f"[JOB] Starting futures collection (days_back={days_back})")
    from collector.futures import FuturesCollector
    coll = FuturesCollector()
    if days_back == 1:
        n = coll.fetch_today()
    else:
        n = coll.fetch_history(days_back)
    logger.info(f"[JOB] Futures collection done: {n} prices")
    return f"{n} prices"


@track_run("collect_stocks", "Collect A-share quotes", trigger_type="scheduled")
def job_collect_stocks():
    """Collect A-share quotes for hotness calc."""
    logger.info("[JOB] Starting stock quote collection")
    from collector.stock_quote import StockQuoteCollector
    coll = StockQuoteCollector()
    n = coll.fetch_today()
    synced, missing = sync_portfolio_quotes()
    if missing:
        logger.warning(f"[JOB] Portfolio quotes missing: {','.join(missing)}")
    logger.info(f"[JOB] Portfolio positions refreshed: {synced}")
    logger.info(f"[JOB] Stock quotes done: {n}")
    return f"{n} quotes"


def sync_portfolio_quotes(trade_date: date | None = None) -> tuple[int, list[str]]:
    """Refresh user positions from the latest same-day stock/ETF quotes."""
    from storage.models import PortfolioAccount, PortfolioPosition, StockQuote

    db = get_db()
    td = trade_date or date.today()
    synced = 0
    missing: list[str] = []
    with db.tx() as s:
        positions = s.query(PortfolioPosition).all()
        if not positions:
            return 0, []
        codes = {str(position.code).zfill(6) for position in positions}
        quotes = {
            str(row.code).zfill(6): row
            for row in (
                s.query(StockQuote)
                .filter(StockQuote.trade_date == td)
                .filter(StockQuote.code.in_(codes))
                .all()
            )
        }
        now = datetime.utcnow()
        touched_users: set[str] = set()
        for position in positions:
            code = str(position.code).zfill(6)
            quote = quotes.get(code)
            if quote is None or float(quote.close or 0) <= 0:
                missing.append(code)
                continue
            price = float(quote.close)
            position.current_price = price
            position.market_value = float(position.quantity or 0) * price
            position.pnl_amount = float(position.quantity or 0) * (
                price - float(position.cost_price or 0)
            )
            position.pnl_pct = (
                price / float(position.cost_price) - 1.0
                if float(position.cost_price or 0) > 0
                else 0.0
            )
            position.as_of = now
            position.updated_at = now
            touched_users.add(position.user_id)
            synced += 1
        for user_id in touched_users:
            account = s.get(PortfolioAccount, user_id)
            if account is not None:
                account.as_of = now
                account.updated_at = now
    return synced, sorted(set(missing))


@track_run("collect_news_high", "Collect high-priority news", trigger_type="scheduled")
def job_collect_news_high(hours_back: int = 24):
    return _job_collect_news_inner("高", hours_back)


@track_run("collect_news_mid", "Collect medium-priority news", trigger_type="scheduled")
def job_collect_news_mid(hours_back: int = 24):
    return _job_collect_news_inner("中", hours_back)


@track_run(
    "collect_news_intraday",
    "Intraday news refresh (Tavily + collectors)",
    trigger_type="scheduled",
)
def job_collect_news_intraday(hours_back: int = 4):
    """Refresh the news window mid-trading-day so Tavily + surviving collectors
    catch blind spots that the 07:30 / 08:15 morning runs missed.

    Budget: 3 cron triggers × 1 credit = 3 credits/day, on top of the 2 credits
    burned by the morning cron path. Stays well under the 20/day self-imposed cap.
    """
    return _job_collect_news_inner("高", hours_back)


def _job_collect_news_inner(priority: str, hours_back: int):
    logger.info(f"[JOB] Starting news collection (priority={priority})")
    from collector.news import run_news_collection
    db = get_db()
    n = run_news_collection(db, priority=priority, hours_back=hours_back)
    # Persist signal hits so web UI sees recent activity
    if n:
        try:
            from processor.signal_hits import persist_signal_hits
            nh = persist_signal_hits(db, hours=hours_back)
            logger.info(f"[JOB] Signal hits persisted: {nh}")
        except Exception as e:
            logger.warning(f"[JOB] Signal-hit persistence failed: {e}")
    logger.info(f"[JOB] News collection done: {n}")
    return f"{n} news"


@track_run("compute_hotness", "Compute hotness", trigger_type="scheduled")
def job_compute_hotness():
    """Compute daily hotness scores."""
    logger.info("[JOB] Computing hotness")
    from collector.stock_quote import HotnessEngine
    db = get_db()
    engine = HotnessEngine(db)
    heats = engine.compute_daily_hotness(date.today())
    logger.info(f"[JOB] Hotness computed: {len(heats)} categories")
    return f"{len(heats)} categories"


@track_run("generate_report", "Generate daily report", trigger_type="scheduled")
def job_generate_report(report_type: str = "full"):
    """Generate report and notify."""
    logger.info(f"[JOB] Generating {report_type} report")
    from processor.report import (
        generate_markdown_report, generate_feishu_payload, save_report,
    )
    from notifier import get_default_notifier
    db = get_db()
    md = generate_markdown_report(db, report_date=date.today())
    payload = generate_feishu_payload(db, report_date=date.today())
    rpt = save_report(db, md, payload, report_date=date.today(), report_type=report_type)

    report_path = settings.reports_dir / f"report_{date.today()}_{report_type}.md"
    report_path.write_text(md, encoding="utf-8")

    notifier = get_default_notifier()
    notifier.send(payload)
    notifier.send({"markdown": md})

    logger.info(f"[JOB] Report generated and saved to {report_path}")
    return f"saved to {report_path}"


@track_run("weekly_backfill", "Weekly backfill", trigger_type="scheduled")
def job_weekly_backfill():
    """Weekly: deep backfill all categories."""
    logger.info("[JOB] Weekly backfill starting")
    job_collect_news_high(hours_back=24 * 7)
    job_collect_futures(days_back=7)
    job_collect_stocks()
    job_compute_hotness()
    job_generate_report(report_type="weekly_backfill")
    logger.info("[JOB] Weekly backfill done")
    return "done"


@track_run("events_detect", "Auto-detect events from news", trigger_type="scheduled")
def job_events_detect():
    """C-type: scan recent news, extract event-type announcements."""
    from events import detect_and_save
    n = detect_and_save()
    logger.info(f"[JOB] Auto-detected {n} new events from news")
    return f"{n} events"


@track_run("event_reminder", "Event calendar reminder (Feishu)", trigger_type="scheduled")
def job_event_reminder():
    """Daily: check events in next 48h, save to DB, push to Feishu."""
    from events import run_reminder_job
    n = run_reminder_job()
    return f"{n} reminders"


@track_run("events_scrape", "Scrape macro calendar (PBOC/NBS/FOMC/EIA/OPEC)", trigger_type="scheduled")
def job_events_scrape():
    """A-type: scrape macro calendar from official sources."""
    from datetime import date, timedelta
    from events import scrape_all, upsert_events
    end = date.today() + timedelta(days=180)
    start = date.today() - timedelta(days=30)
    events = scrape_all(start, end)
    n = upsert_events(events)
    return f"{len(events)} scraped, {n} new"


# ============================================================================
# Maintenance: prune old JobRun records to keep the table bounded.
# ============================================================================

def prune_job_runs(older_than_days: int = 30, job_id: str | None = None) -> int:
    """Delete JobRun rows older than ``older_than_days`` days.

    Args:
        older_than_days: keep at most this many days of history (default 30).
        job_id:          if given, only prune this job_id's records.

    Returns:
        Number of rows deleted.

    The default 30-day window keeps enough history for the
    ``/api/scheduler/status`` page to show ~30 daily runs per job.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import delete
    db = get_db()
    cutoff = datetime.utcnow() - timedelta(days=older_than_days)
    with db.tx() as s:
        stmt = delete(JobRun).where(JobRun.started_at < cutoff)
        if job_id is not None:
            stmt = stmt.where(JobRun.job_id == job_id)
        result = s.execute(stmt)
    n = int(result.rowcount or 0)
    logger.info(
        f"Pruned {n} JobRun rows older than {older_than_days} days"
        + (f" (job_id={job_id})" if job_id else "")
    )
    return n


@track_run("prune_job_runs", "Prune old JobRun records", trigger_type="scheduled")
def job_prune_job_runs(older_than_days: int = 30):
    """Scheduled job: prune JobRun records older than N days."""
    return f"{prune_job_runs(older_than_days=older_than_days)} pruned"
