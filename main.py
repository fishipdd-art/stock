#!/usr/bin/env python3
"""
Main entry point for the Supply Chain Stock Analysis System.

Usage:
  python main.py init              # init DB + import knowledge graph
  python main.py start             # start BOTH scheduler + web (recommended)
  python main.py run               # start scheduler only (foreground)
  python main.py web               # start web server only
  python main.py once              # run all jobs once (smoke test)
  python main.py collect           # collect all data once
  python main.py report            # generate today's report
  python main.py backfill --days 7 # backfill N days of history
  python main.py stats             # show DB stats
  python main.py prune --days 30    # prune old JobRun records
  python main.py import-graph      # re-import knowledge graph
"""
from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


def setup_logging():
    from config.settings import configure_logging, settings
    from loguru import logger
    log_path = settings.logs_dir / "app_{time:YYYY-MM-DD}.log"
    configure_logging(log_file=str(log_path))
    logger.add(
        str(log_path),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level="DEBUG",
        rotation=settings.log_rotation,
        retention="30 days",
        compression="zip",
    )


def cmd_init(args):
    from storage import init_db
    from knowledge_graph import import_all
    print(">> Initializing database...")
    init_db()
    print(">> Importing knowledge graph...")
    counts = import_all()
    print(f">> Done. Imported: {counts}")


def cmd_stats(args):
    from storage import get_db
    from storage.models import (
        KnowledgeCategory, SearchTerm, KnowledgeSignal,
        AStock, SignalStock, FuturesPrice, NewsRaw, StockQuote,
        SectorHeat, DailyReport, PendingTerm, JobRun, SystemState,
    )
    db = get_db()
    with db.session() as s:
        print("=" * 50)
        print("Database Statistics")
        print("=" * 50)
        for cls in [
            KnowledgeCategory, SearchTerm, KnowledgeSignal,
            AStock, SignalStock, FuturesPrice, NewsRaw,
            StockQuote, SectorHeat, DailyReport, PendingTerm,
            JobRun, SystemState,
        ]:
            n = s.query(cls).count()
            print(f"  {cls.__name__:30s} {n:>8,}")
        print("=" * 50)


def cmd_once(args):
    from scheduler.jobs import (
        job_collect_futures, job_collect_news_high,
        job_compute_hotness, job_generate_report,
    )
    print(">> [1/4] Collecting futures...")
    job_collect_futures(days_back=1)
    print(">> [2/4] Collecting news (高, 48h)...")
    job_collect_news_high(hours_back=48)
    print(">> [3/4] Computing hotness...")
    job_compute_hotness()
    print(">> [4/4] Generating report...")
    job_generate_report(report_type="smoke_test")
    print(">> All done.")


def cmd_collect(args):
    from scheduler.jobs import job_collect_futures, job_collect_stocks, job_collect_news_high
    job_collect_futures(days_back=getattr(args, 'days_back', 1))
    job_collect_stocks()
    job_collect_news_high(hours_back=getattr(args, 'hours_back', 24))


def cmd_report(args):
    from scheduler.jobs import job_generate_report
    job_generate_report(report_type=getattr(args, 'report_type', 'manual'))


def cmd_run(args):
    from scheduler import run_forever
    run_forever()


def cmd_import_graph(args):
    from knowledge_graph import import_all
    counts = import_all()
    print(f">> Imported: {counts}")


def cmd_events_refresh(args):
    """Refresh industry events (macro + curated)."""
    from events import refresh_events
    n = refresh_events()
    print(f">> Refreshed events: {n} new")


def cmd_events_detect(args):
    """Auto-detect events from news (C-type)."""
    from events import detect_and_save
    n = detect_and_save()
    print(f">> Auto-detected {n} new events from news")


def cmd_events_scrape(args):
    """Scrape macro calendar from official sources (A-type)."""
    from datetime import date, timedelta
    from events import scrape_all
    end = date.today() + timedelta(days=180)
    start = date.today() - timedelta(days=30)
    events = scrape_all(start, end)
    print(f">> Scraped {len(events)} macro events ({start} → {end})")
    from events import upsert_events
    n = upsert_events(events)
    print(f">> Saved {n} new events to DB")


def cmd_events_backtest(args):
    """Run event vs price-move backtest (historical correlation)."""
    from events.backtest import run_backtest
    n_days = getattr(args, 'days', 365)
    result = run_backtest(days=n_days)
    print(f">> Backtest complete: {result['n_events_analyzed']} events analyzed")
    print(f">> Top correlations: {len(result['top_correlations'])} event-type x industry pairs")


def cmd_prune(args):
    """Prune old JobRun records to keep the table bounded."""
    from scheduler.jobs import prune_job_runs
    days = getattr(args, "days", 30)
    job_id = getattr(args, "job_id", None)
    n = prune_job_runs(older_than_days=days, job_id=job_id)
    scope = f"job_id={job_id}" if job_id else "all jobs"
    print(f">> Pruned {n} JobRun rows older than {days} days ({scope})")


def cmd_web(args):
    from web.server import run
    host = getattr(args, 'host', '0.0.0.0')
    port = getattr(args, 'port', 8000)
    reload = getattr(args, 'reload', False)
    print(f">> Starting web server at http://{host}:{port}")
    print(f">> Open in browser: http://localhost:{port}")
    run(host=host, port=port, reload=reload)


def cmd_start(args):
    """Start the web server and, only in DR mode, the Python scheduler."""
    from scheduler import build_scheduler, mark_scheduler_state
    from web.server import run
    from config.settings import settings

    host = getattr(args, 'host', '0.0.0.0')
    port = getattr(args, 'port', 8000)

    print("=" * 60)
    print(">> Stock Analysis System - Start (Scheduler + Web)")
    print("=" * 60)

    scheduler = None
    if settings.scheduler_owner.strip().lower() == "python":
        scheduler = build_scheduler(blocking=False)
        scheduler.start()
        mark_scheduler_state("running", "python_disaster_recovery")
        print(f">> Python DR scheduler started ({len(scheduler.get_jobs())} jobs)")
        for j in scheduler.get_jobs():
            print(f"     - {j.name}")
    else:
        mark_scheduler_state("disabled", "owner=dify")
        print(">> Python scheduler disabled; Dify owns production scheduling")

    print(f">> Starting web server at http://{host}:{port}")
    print(f">> Open browser: http://localhost:{port}")
    print(f">> Press Ctrl+C to stop everything")

    try:
        run(host=host, port=port, reload=False)
    except (KeyboardInterrupt, SystemExit):
        print(">> Shutting down...")
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            mark_scheduler_state("stopped", "python_disaster_recovery")
        print(">> Done.")


def cmd_backfill(args):
    """Run historical backfill via scripts.backfill."""
    from scripts.backfill import main as backfill_main
    saved_argv = sys.argv
    sys.argv = ['backfill']
    if args.days:
        sys.argv += ['--days', str(args.days)]
    if args.skip_news:
        sys.argv += ['--skip-news']
    if args.skip_futures:
        sys.argv += ['--skip-futures']
    if args.skip_stocks:
        sys.argv += ['--skip-stocks']
    if args.skip_report:
        sys.argv += ['--skip-report']
    if getattr(args, 'end_date', None):
        sys.argv += ['--end-date', args.end_date]
    try:
        return backfill_main()
    finally:
        sys.argv = saved_argv


def main():
    setup_logging()

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "init": cmd_init,
        "stats": cmd_stats,
        "once": cmd_once,
        "collect": cmd_collect,
        "report": cmd_report,
        "run": cmd_run,
        "web": cmd_web,
        "start": cmd_start,
        "backfill": cmd_backfill,
        "import-graph": cmd_import_graph,
        "events-refresh": cmd_events_refresh,
        "events-detect": cmd_events_detect,
        "events-scrape": cmd_events_scrape,
        "events-backtest": cmd_events_backtest,
        "prune": cmd_prune,
    }
    if cmd not in commands:
        print(f"Unknown command: {cmd}")
        print("Available: " + ", ".join(commands.keys()))
        sys.exit(1)

    import argparse
    parser = argparse.ArgumentParser()
    if cmd == "collect":
        parser.add_argument("--days-back", type=int, default=1)
        parser.add_argument("--hours-back", type=int, default=24)
    if cmd in ("report",):
        parser.add_argument("--report-type", type=str, default="manual")
    if cmd in ("web", "start"):
        parser.add_argument("--host", type=str, default="0.0.0.0")
        parser.add_argument("--port", type=int, default=8000)
        if cmd == "web":
            parser.add_argument("--reload", action="store_true")
    if cmd == "backfill":
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument("--end-date", type=str, default=None)
        parser.add_argument("--skip-news", action="store_true")
        parser.add_argument("--skip-futures", action="store_true")
        parser.add_argument("--skip-stocks", action="store_true")
        parser.add_argument("--skip-report", action="store_true")
    if cmd == "events-backtest":
        parser.add_argument("--days", type=int, default=365)
    if cmd == "prune":
        parser.add_argument("--days", type=int, default=30,
                            help="Prune JobRun rows older than N days (default 30)")
        parser.add_argument("--job-id", type=str, default=None,
                            help="If set, only prune this job_id's records")
    parsed = parser.parse_args(args)
    sys.exit(commands[cmd](parsed) or 0)


if __name__ == "__main__":
    main()
