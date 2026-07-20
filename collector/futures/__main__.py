"""CLI entrypoint: ``python -m collector.futures ...``"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from loguru import logger

from collector.futures import FuturesCollector
from config.settings import settings
from storage.database import init_db


def _setup_logging() -> None:
    from config.settings import configure_logging
    configure_logging(verbose=settings.log_level == "DEBUG")


def cmd_fetch(args: argparse.Namespace) -> int:
    collector = FuturesCollector()
    if args.today:
        n = collector.fetch_today()
        print(f"fetched {n} rows for today")
        return 0 if n > 0 or not args.strict else 1
    if args.days_back is not None:
        n = collector.fetch_history(args.days_back)
        print(f"fetched {n} rows for last {args.days_back} days")
        return 0
    print("error: specify --today or --days-back N", file=sys.stderr)
    return 2


def cmd_stats(args: argparse.Namespace) -> int:
    collector = FuturesCollector()
    stats = collector.stats()
    print(f"total_rows:        {stats['total_rows']}")
    print(f"distinct_symbols:  {stats['distinct_symbols']}")
    print(f"latest_trade_date: {stats['latest_trade_date']}")
    if stats["per_exchange"]:
        print("per_exchange:")
        for exch, cnt in stats["per_exchange"]:
            print(f"  {exch or '(unknown)':8s} {cnt}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collector.futures")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="Fetch futures prices from AKShare/Sina")
    g = p_fetch.add_mutually_exclusive_group(required=True)
    g.add_argument("--today", action="store_true", help="Fetch today's snapshot")
    g.add_argument("--days-back", type=int, help="Backfill N days")
    p_fetch.add_argument(
        "--strict", action="store_true",
        help="Return non-zero exit if zero rows fetched",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    p_stats = sub.add_parser("stats", help="Show DB counts")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    init_db()  # ensure schema exists before any collector touches the DB
    _setup_logging()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())