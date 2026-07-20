"""
Cross-platform scheduler using APScheduler.

Daily schedule (Asia/Shanghai):
  06:00  Futures prices
  06:15  A-share spot quotes
  07:30  High-priority news scan (target finish before 08:00)
  08:00  Hotness computation
  08:15  Medium-priority news
  08:30  Report generation + notify (target before 09:00)
  Sat 10:00  Weekly deep backfill
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from loguru import logger
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.background import BackgroundScheduler

from config.settings import settings
from storage import get_db
from storage.models import SystemState
from scheduler.jobs import (
    job_collect_futures, job_collect_stocks,
    job_collect_news_high, job_collect_news_mid, job_collect_news_intraday,
    job_compute_hotness, job_generate_report, job_weekly_backfill,
    job_events_detect, job_event_reminder, job_events_scrape,
    job_prune_job_runs,
)


def build_scheduler(timezone: str = "Asia/Shanghai", blocking: bool = True) -> object:
    """Build the main scheduler with all daily jobs.

    If blocking=True, returns BlockingScheduler (use run_forever()).
    If blocking=False, returns BackgroundScheduler (use start() to run in background thread).
    """
    cls = BlockingScheduler if blocking else BackgroundScheduler
    scheduler = cls(timezone=timezone)

    scheduler.add_job(
        job_collect_futures, "cron",
        hour=6, minute=0,
        id="collect_futures",
        name="Collect futures (L3)",
        replace_existing=True,
        misfire_grace_time=600,
        kwargs={"days_back": 1},
    )
    scheduler.add_job(
        job_collect_stocks, "cron",
        hour=6, minute=15,
        id="collect_stocks",
        name="Collect A-share quotes",
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        job_collect_news_high, "cron",
        hour=7, minute=30,
        id="collect_news_high",
        name="Collect high-priority news",
        replace_existing=True,
        misfire_grace_time=600,
        kwargs={"hours_back": 24},
    )
    scheduler.add_job(
        job_compute_hotness, "cron",
        hour=8, minute=0,
        id="compute_hotness",
        name="Compute hotness",
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        job_collect_news_mid, "cron",
        hour=8, minute=15,
        id="collect_news_mid",
        name="Collect medium-priority news",
        replace_existing=True,
        misfire_grace_time=600,
        kwargs={"hours_back": 24},
    )
    # Intraday refresh fires 3× per trading day (11:30, 13:30, 15:30 — lunch
    # skip is implicit via the gap from 11:30 to 13:30). Runs the full
    # default-collector fan-out, so Tavily contributes one composite query
    # per fire. Combined with the morning cron path that's 5 credits/day,
    # well under the 20/day cap.
    scheduler.add_job(
        job_collect_news_intraday, "cron",
        hour="11,13,15", minute="30",
        id="collect_news_intraday",
        name="Intraday news refresh (Tavily + collectors)",
        replace_existing=True,
        misfire_grace_time=600,
        kwargs={"hours_back": 4},
    )
    scheduler.add_job(
        job_generate_report, "cron",
        hour=8, minute=30,
        id="generate_report",
        name="Generate daily report",
        replace_existing=True,
        misfire_grace_time=600,
        kwargs={"report_type": "full"},
    )
    scheduler.add_job(
        job_weekly_backfill, "cron",
        day_of_week="sat", hour=10, minute=0,
        id="weekly_backfill",
        name="Weekly backfill",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        job_events_detect, "cron",
        hour=7, minute=0,
        id="events_detect",
        name="Auto-detect events from news",
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        job_event_reminder, "cron",
        hour=8, minute=15,
        id="event_reminder",
        name="Event calendar reminder (Feishu)",
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        job_events_scrape, "cron",
        hour=5, minute=30,
        id="events_scrape",
        name="Scrape macro calendar (A-type)",
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        job_prune_job_runs, "cron",
        day_of_week="sun", hour=3, minute=0,
        id="prune_job_runs",
        name="Prune old JobRun records (30d)",
        replace_existing=True,
        misfire_grace_time=3600,
        kwargs={"older_than_days": 30},
    )

    return scheduler


def mark_scheduler_state(state: str, extra: str = ""):
    """Update system_state to indicate scheduler is running."""
    try:
        db = get_db()
        with db.tx() as s:
            row = s.get(SystemState, "scheduler_state")
            payload = f"{state}|{datetime.utcnow().isoformat()}|{extra}"
            if row:
                row.value = payload
                row.updated_at = datetime.utcnow()
            else:
                s.add(SystemState(key="scheduler_state", value=payload))
    except Exception as e:
        logger.warning(f"Could not update scheduler state: {e}")


def run_forever():
    """Block and run scheduler in foreground (production use)."""
    if settings.scheduler_owner.strip().lower() != "python":
        mark_scheduler_state("disabled", "owner=dify")
        logger.warning(
            "Python scheduler is disabled because SCHEDULER_OWNER is not "
            "'python'. Dify is the production scheduler."
        )
        return
    logger.info("=" * 60)
    logger.info("Stock Analysis System Scheduler Starting")
    logger.info(f"Timezone: Asia/Shanghai")
    logger.info(f"Project root: {settings.project_root}")
    logger.info(f"DB: {settings.db_path}")
    logger.info("=" * 60)

    scheduler = build_scheduler(blocking=True)

    for j in scheduler.get_jobs():
        logger.info(f"  Job: {j.name} (id={j.id})")

    mark_scheduler_state("running", "blocking")
    logger.info("Scheduler running. Ctrl+C to exit.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
        mark_scheduler_state("stopped", "")


if __name__ == "__main__":
    run_forever()
