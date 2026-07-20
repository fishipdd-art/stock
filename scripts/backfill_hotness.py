"""Backfill missing SectorHeat rows for the recent N-day window.

Days where StockQuote / NewsRaw are absent (weekends, collector downtime,
holidays) currently leave the heat chart blank. Run this once after
collecting data, or schedule it after the collectors, to guarantee a
continuous timeline.

Usage:
    /Users/liyuhang/Documents/stock/.venv/bin/python -m scripts.backfill_hotness --days 14
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from collector.stock_quote.hotness import HotnessEngine
from loguru import logger
from storage import get_db
from storage.models import SectorHeat


def _missing_dates(db, today: date, window: int) -> list[date]:
    """Return dates in [today-window, today] that have zero SectorHeat rows."""
    cutoff = today - timedelta(days=window)
    with db.session() as s:
        present = {
            r[0]
            for r in s.query(SectorHeat.trade_date)
            .filter(SectorHeat.trade_date >= cutoff)
            .distinct()
            .all()
        }
    return [cutoff + timedelta(days=i) for i in range(window + 1) if (cutoff + timedelta(days=i)) not in present]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14, help="backfill window in days")
    args = ap.parse_args()

    today = date.today()
    db = get_db()
    engine = HotnessEngine(db)

    missing = _missing_dates(db, today, args.days)
    if not missing:
        logger.info(f"No missing dates in last {args.days} days — nothing to backfill.")
        return

    logger.info(f"Backfilling {len(missing)} dates: {[d.isoformat() for d in missing]}")
    for d in missing:
        try:
            rows = engine.compute_daily_hotness(d)
            top1 = max(rows, key=lambda r: r.hotness_score) if rows else None
            logger.info(
                f"  {d}: wrote {len(rows)} rows"
                + (f" (top1={top1.category_name}={top1.hotness_score:.2f})" if top1 else "")
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"  {d}: failed — {e}")


if __name__ == "__main__":
    main()