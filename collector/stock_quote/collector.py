"""
A-share quote collector.

StockQuoteCollector pulls live snapshots for the 148 stocks in the
knowledge graph and persists them to storage.models.StockQuote with an
idempotent INSERT-or-REPLACE strategy keyed on (trade_date, code).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

from loguru import logger
import pandas as pd
from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from config.settings import settings
from storage.database import get_db, init_db
from storage.models import AStock, PortfolioPosition, StockQuote

from . import akshare_bridge as bridge


class StockQuoteCollector:
    """Collect A-share quotes for stocks in the knowledge graph."""

    def __init__(self, db=None):
        self.db = db or get_db()
        self.db.init_schema()
        self._universe_cache: set[str] | None = None

    # ------------------------------------------------------------------
    # Universe
    # ------------------------------------------------------------------

    def get_universe(self) -> set[str]:
        """Return the research graph plus every user-held stock/ETF code."""
        if self._universe_cache is not None:
            return self._universe_cache
        with self.db.session() as s:
            rows = s.query(AStock.code).all()
            holdings = s.query(PortfolioPosition.code).distinct().all()
        codes = {
            str(r[0]).zfill(6)
            for r in [*rows, *holdings]
            if r[0]
        }
        self._universe_cache = codes
        logger.info(f"get_universe: {len(codes)} A-share codes loaded")
        return codes

    # ------------------------------------------------------------------
    # Today
    # ------------------------------------------------------------------

    def fetch_today(self, trade_date: date | None = None) -> int:
        """Fetch live spot quotes and persist today's rows.

        Returns the number of rows actually written (inserted or replaced).
        """
        td = trade_date or bridge.today_date()
        universe = self.get_universe()
        if not universe:
            logger.warning("fetch_today: empty AStock universe, skipping")
            return 0

        logger.info(f"fetch_today: {td} universe={len(universe)}")
        try:
            df = bridge.fetch_all_spot_quotes()
        except Exception as e:
            logger.error(f"fetch_today: spot bridge failed: {e!r}; trying Sina fallback")
            df = bridge.fetch_sina_spot_quotes(universe)

        if df is None or df.empty:
            logger.warning("fetch_today: bulk spot empty; trying Sina fallback")
            df = bridge.fetch_sina_spot_quotes(universe)
        df = bridge.filter_to_universe(df, universe)
        found = set(df["code"].astype(str).str.zfill(6)) if not df.empty else set()
        missing = universe - found
        if missing:
            fallback = bridge.fetch_sina_spot_quotes(missing)
            if fallback is not None and not fallback.empty:
                df = pd.concat([df, fallback], ignore_index=True)
                df = bridge.filter_to_universe(df, universe)
                df = df.drop_duplicates(subset=["code"], keep="last")
        if df.empty:
            logger.warning("fetch_today: no spot overlap with universe; trying history fallback")
            return self._fallback_history(universe)

        rows = bridge.quote_rows_for_trade_date(df, td)
        written = self._upsert(rows, td)
        return written if written else self._fallback_history(universe)

    def _fallback_history(self, universe: set[str], days_back: int = 7) -> int:
        """Use per-symbol daily bars when the bulk spot endpoint is blocked.

        This fallback preserves real market dates and never fabricates a
        quote.  The freshness gate decides whether the resulting snapshot is
        recent enough for downstream investment outputs.
        """
        logger.warning(f"fetch_today: using per-symbol history fallback for {len(universe)} stocks")
        return self.fetch_history(days_back=days_back, codes=universe, max_workers=4)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def fetch_history(
        self,
        days_back: int = 30,
        adjust: str = "qfq",
        codes: Iterable[str] | None = None,
        max_workers: int = 6,
    ) -> int:
        """Backfill N days of history for each stock in the universe.

        Implementation: fetch each stock's bars concurrently using a
        thread pool. AKShare is sync (blocking I/O), but releases the
        GIL during HTTP, so threads work well here. ``max_workers``
        caps concurrency to avoid hammering Eastmoney's anti-bot.

        Falls back to serial if the bridge layer isn't importable.
        """
        universe = set(codes) if codes is not None else self.get_universe()
        if not universe:
            logger.warning("fetch_history: empty universe")
            return 0

        end = bridge.today_yyyymmdd()
        start = (bridge.today_date() - timedelta(days=days_back)).strftime("%Y%m%d")

        sorted_codes = sorted(universe)
        n = len(sorted_codes)

        def _fetch_one(code: str) -> int:
            """Fetch + upsert one stock. Returns rows written."""
            try:
                df = bridge.fetch_history_bars(code, start, end, adjust=adjust)
            except Exception as e:
                logger.warning(f"fetch_history: {code} failed: {e!r}")
                return 0
            if df.empty:
                return 0
            rows = bridge.hist_rows_for_insert(df)
            return self._upsert(rows)

        # Run concurrently. ``max_workers=6`` keeps us under typical
        # 6-req/s rate limits while cutting wall-clock time ~6x.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        total = 0
        completed = 0
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = {ex.submit(_fetch_one, code): code for code in sorted_codes}
                for fut in as_completed(futures):
                    code = futures[fut]
                    completed += 1
                    try:
                        written = fut.result()
                    except Exception as e:
                        logger.warning(f"fetch_history: {code} raised: {e!r}")
                        continue
                    total += written
                    if completed % 20 == 0 or completed == n:
                        logger.info(
                            f"fetch_history: {completed}/{n} stocks, "
                            f"{total} rows so far"
                        )
        except Exception as e:
            logger.warning(
                f"fetch_history: concurrent fetch failed ({e!r}); "
                "falling back to serial"
            )
            # Last-ditch serial fallback
            for code in sorted_codes:
                total += _fetch_one(code)

        logger.info(f"fetch_history: done, {total} rows for {n} stocks")
        return total

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _upsert(self, rows: list[dict], trade_date: date | None = None) -> int:
        """INSERT OR REPLACE on (trade_date, code). Returns row count."""
        if not rows:
            return 0
        stmt = sqlite_insert(StockQuote).values(rows)
        # When (trade_date, code) already exists, overwrite every column
        # except id and fetched_at.
        update_cols = {
            c.name: stmt.excluded[c.name]
            for c in StockQuote.__table__.columns
            if c.name not in {"id", "fetched_at"}
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["trade_date", "code"],
            set_=update_cols,
        )
        with self.db.tx() as s:  # type: Session
            result = s.execute(stmt)
        return result.rowcount or len(rows)

    def delete_for_date(self, trade_date: date) -> int:
        """Hard-delete all quotes for a given trade date (used in tests)."""
        with self.db.tx() as s:
            n = s.execute(
                delete(StockQuote).where(StockQuote.trade_date == trade_date)
            ).rowcount
        return int(n or 0)
