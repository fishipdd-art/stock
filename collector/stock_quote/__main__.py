"""
CLI entrypoint for the stock_quote package.

Usage:
  python -m collector.stock_quote fetch --today
  python -m collector.stock_quote fetch --history 30
  python -m collector.stock_quote hotness
  python -m collector.stock_quote stats
  python -m collector.stock_quote seed
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

from loguru import logger

from config.settings import settings
from storage.database import get_db, init_db
from storage.models import (
    AStock,
    KnowledgeCategory,
    KnowledgeSignal,
    NewsRaw,
    SearchTerm,
    SectorHeat,
    SignalStock,
    StockQuote,
)

from . import akshare_bridge as bridge
from .collector import StockQuoteCollector
from .hotness import HotnessEngine
from .seed import seed_demo


def _setup_logging() -> None:
    from config.settings import configure_logging
    configure_logging(verbose=settings.log_level == "DEBUG")


def _ensure_db() -> None:
    """Make sure the schema exists before any read/write."""
    init_db()


def cmd_fetch(args: argparse.Namespace) -> int:
    collector = StockQuoteCollector()
    if args.today:
        n = collector.fetch_today()
        logger.info(f"fetch --today: wrote {n} rows for {date.today()}")
        return 0
    if args.history is not None:
        n = collector.fetch_history(days_back=int(args.history))
        logger.info(f"fetch --history {args.history}: wrote {n} rows total")
        return 0
    logger.error("fetch: specify --today or --history N")
    return 2


def cmd_hotness(args: argparse.Namespace) -> int:
    engine = HotnessEngine()
    td = args.date or date.today()
    rows = engine.compute_daily_hotness(td)
    if not rows:
        logger.warning(f"hotness: no rows for {td} (is the DB empty? run `seed` first)")
        return 1
    print(f"\nHotness ranking for {td}  (top {settings.deep_process_top_n} marked DEEP)\n")
    print(f"{'rank':>4}  {'level':<8}  {'category':<24}  {'score':>8}  {'|Δ|sum':>8}  {'turnover':>14}  {'news':>4}  {'stocks':>6}")
    print("-" * 92)
    for r in rows:
        print(
            f"{r.rank:>4}  {r.processed_level:<8}  {r.category_name:<24}  "
            f"{r.hotness_score:>8.3f}  {r.abs_change_sum:>8.2f}  "
            f"{r.turnover_sum:>14,.0f}  {r.news_count:>4d}  {r.n_stocks:>6d}"
        )
    print()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    db = get_db()
    with db.session() as s:
        n_cats = s.query(KnowledgeCategory).count()
        n_terms = s.query(SearchTerm).count()
        n_stocks = s.query(AStock).count()
        n_signals = s.query(KnowledgeSignal).count()
        n_signal_stocks = s.query(SignalStock).count()
        n_news = s.query(NewsRaw).count()
        n_quotes = s.query(StockQuote).count()
        latest_quote_date = s.query(StockQuote.trade_date).order_by(
            StockQuote.trade_date.desc()
        ).first()
        n_heat = s.query(SectorHeat).count()
        latest_heat_date = s.query(SectorHeat.trade_date).order_by(
            SectorHeat.trade_date.desc()
        ).first()

    print("DB stats")
    print("--------")
    print(f"  knowledge_categories : {n_cats:>4}")
    print(f"  search_terms         : {n_terms:>4}")
    print(f"  a_stocks             : {n_stocks:>4}")
    print(f"  knowledge_signals    : {n_signals:>4}")
    print(f"  signal_stocks        : {n_signal_stocks:>4}")
    print(f"  news_raw             : {n_news:>4}")
    print(f"  stock_quotes         : {n_quotes:>4}    (latest: {latest_quote_date[0] if latest_quote_date else '-'})")
    print(f"  sector_heat          : {n_heat:>4}    (latest: {latest_heat_date[0] if latest_heat_date else '-'})")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    counts = seed_demo()
    print("seed result:", counts)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m collector.stock_quote",
        description="A-share quote collector + sector hotness engine.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="Fetch stock quotes from akshare")
    g = pf.add_mutually_exclusive_group(required=True)
    g.add_argument("--today", action="store_true", help="Fetch today's snapshot")
    g.add_argument("--history", type=int, metavar="DAYS", help="Backfill N days")
    pf.set_defaults(func=cmd_fetch)

    ph = sub.add_parser("hotness", help="Compute and persist sector hotness")
    ph.add_argument("--date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    help="Trade date (default: today)")
    ph.set_defaults(func=cmd_hotness)

    ps = sub.add_parser("stats", help="Show DB row counts")
    ps.set_defaults(func=cmd_stats)

    pseed = sub.add_parser("seed", help="Seed synthetic demo data")
    pseed.set_defaults(func=cmd_seed)

    return p


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    _ensure_db()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
