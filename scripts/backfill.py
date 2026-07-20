"""
Historical data backfill.

Fetches N days of:
  - futures prices
  - A-share stock quotes
  - news (for each day, the past 48h)
  - hotness per day
  - daily report per day

Usage:
  python -m scripts.backfill --days 7
  python -m scripts.backfill --days 30 --skip-news   # faster
"""
from __future__ import annotations

import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from loguru import logger
import argparse

from storage import get_db
from storage.models import SectorHeat, DailyReport
from config.settings import settings
from scheduler.jobs import (
    job_collect_futures, job_collect_news_high, job_collect_news_mid,
    job_compute_hotness, job_generate_report,
)


def compute_hotness_for_date(target_date: date) -> int:
    """Compute hotness for a specific date using that day's data."""
    from collector.stock_quote import HotnessEngine
    db = get_db()
    engine = HotnessEngine(db)
    heats = engine.compute_daily_hotness(target_date)
    return len(heats)


def generate_report_for_date(target_date: date, report_type: str = "backfill") -> bool:
    """Generate report for a specific past date."""
    from processor.report import (
        generate_markdown_report, generate_feishu_payload, save_report,
    )
    db = get_db()
    try:
        md = generate_markdown_report(db, report_date=target_date)
        payload = generate_feishu_payload(db, report_date=target_date)
        save_report(db, md, payload, report_date=target_date, report_type=report_type)

        # Also write to file
        report_path = settings.reports_dir / f"report_{target_date}_{report_type}.md"
        report_path.write_text(md, encoding="utf-8")
        return True
    except Exception as e:
        logger.error(f"Report for {target_date} failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Backfill historical data and reports.")
    parser.add_argument("--days", type=int, default=7, help="Number of days to backfill (default 7)")
    parser.add_argument("--skip-news", action="store_true", help="Skip news collection (faster)")
    parser.add_argument("--skip-futures", action="store_true", help="Skip futures backfill")
    parser.add_argument("--skip-stocks", action="store_true", help="Skip stock quotes backfill")
    parser.add_argument("--skip-report", action="store_true", help="Skip report generation")
    parser.add_argument("--end-date", type=str, default=None, help="End date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    end_date = date.today()
    if args.end_date:
        try:
            end_date = date.fromisoformat(args.end_date)
        except ValueError:
            print(f"Invalid --end-date: {args.end_date}")
            return 1

    start_date = end_date - timedelta(days=args.days - 1)
    print(f">> Backfilling {args.days} days: {start_date} -> {end_date}")
    print(f">> Settings: skip_news={args.skip_news}, skip_futures={args.skip_futures}, "
          f"skip_stocks={args.skip_stocks}, skip_report={args.skip_report}")

    # 1. Futures (single batch call)
    if not args.skip_futures:
        print(">> [1/4] Collecting futures prices (L3)...")
        try:
            job_collect_futures(days_back=args.days)
        except Exception as e:
            print(f"   WARN: futures backfill failed: {e}")

    # 2. A-share history (slow due to per-stock fetch)
    if not args.skip_stocks:
        print(">> [2/4] Collecting A-share historical quotes (slow)...")
        try:
            from collector.stock_quote import StockQuoteCollector
            coll = StockQuoteCollector()
            n = coll.fetch_history(days_back=args.days)
            print(f"   -> {n} rows for {args.days} days")
        except Exception as e:
            print(f"   WARN: stocks backfill failed: {e}")

    # 3. News (only last 7 days typically — older news often inaccessible)
    if not args.skip_news:
        print(f">> [3/4] Collecting news (last {min(args.days, 7)} days)...")
        try:
            job_collect_news_high(hours_back=min(args.days * 24, 168))
        except Exception as e:
            print(f"   WARN: news backfill failed: {e}")

    # 4. Per-day hotness + report
    print(f">> [4/4] Computing hotness + reports for {args.days} days...")
    n_heats = 0
    n_reports = 0
    for offset in range(args.days):
        target = end_date - timedelta(days=offset)
        # Hotness
        try:
            n_heats += compute_hotness_for_date(target)
            print(f"   hotness {target}: {n_heats} categories total")
        except Exception as e:
            print(f"   hotness {target} failed: {e}")

        # Report
        if not args.skip_report:
            ok = generate_report_for_date(target)
            if ok:
                n_reports += 1
                print(f"   report {target}: OK")
            else:
                print(f"   report {target}: FAILED")

    print("=" * 60)
    print(f">> Backfill complete: {n_heats} hotness records, {n_reports} reports generated")
    return 0


if __name__ == "__main__":
    sys.exit(main())