"""
Orchestrator: AKShare (primary) -> Sina-direct (fallback) -> DB writes.

Idempotency
-----------
We use ``INSERT ... ON CONFLICT(trade_date, symbol) DO UPDATE`` so re-running
``fetch_today`` after the market closes simply overwrites the previous
incomplete snapshot rather than creating duplicate rows.

Fallback chain
--------------
1. AKShare returns at least one row  -> persist directly.
2. AKShare returns zero / errors out -> try Sina-direct for the same set.
3. Sina-direct still empty / errors  -> log & continue (the contract is
   simply missing for the day; we don't crash the whole batch).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

from loguru import logger
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from collector.futures import akshare_bridge, sina_fallback
from collector.futures.contracts import build_contract_name
from storage.database import get_db
from storage.models import FuturesPrice


def _to_dict(row: dict, exchange: str | None = None) -> dict:
    """Normalise a row into the exact column set of FuturesPrice."""
    symbol = row["symbol"]
    return {
        "trade_date": row["trade_date"],
        "symbol": symbol,
        "name": build_contract_name(symbol),
        "exchange": exchange or row.get("exchange") or "",
        "open": float(row.get("open") or 0.0),
        "high": float(row.get("high") or 0.0),
        "low": float(row.get("low") or 0.0),
        "close": float(row.get("close") or 0.0),
        "settle": float(row.get("settle") or 0.0),
        "volume": float(row.get("volume") or 0.0),
        "position": float(row.get("position") or 0.0),
        "change_pct": float(row.get("change_pct") or 0.0),
        "fetched_at": datetime.utcnow(),
    }


class FuturesCollector:
    """High-level entry point for collecting L3 futures prices."""

    def __init__(self) -> None:
        self._db = get_db()

    # ------------------------------------------------------------------ public

    def fetch_today(self) -> int:
        """Fetch today's snapshot for every active main contract. Returns row count."""
        today = datetime.utcnow().date()
        logger.info(f"fetch_today() trade_date={today}")
        rows = self._collect_for_date(today)
        return self._persist(rows)

    def fetch_history(self, days_back: int) -> int:
        """Backfill the last ``days_back`` days from AKShare main contract history."""
        if days_back <= 0:
            return 0
        logger.info(f"fetch_history(days_back={days_back})")

        contracts = akshare_bridge.list_main_contracts()
        all_rows: list[dict] = []
        for concrete, prefix, exchange in contracts:
            try:
                history_rows = akshare_bridge.fetch_history(concrete, days_back)
            except Exception as exc:
                logger.warning(f"akshare history failed for {concrete}: {exc!r}")
                continue
            for r in history_rows:
                r["symbol"] = concrete
                r["exchange"] = exchange
                all_rows.append(r)

        # Deduplicate by (trade_date, symbol) — keep the last one we collected.
        dedup: dict[tuple[date, str], dict] = {}
        for r in all_rows:
            key = (r["trade_date"], r["symbol"])
            dedup[key] = r

        rows = [self._normalise(r) for r in dedup.values()]
        return self._persist(rows)

    def stats(self) -> dict:
        """Return simple DB stats for the CLI ``stats`` command."""
        with self._db.tx() as s:  # type: Session
            total = s.execute(text("SELECT COUNT(*) FROM futures_prices")).scalar() or 0
            latest = s.execute(
                text("SELECT MAX(trade_date) FROM futures_prices")
            ).scalar()
            distinct_syms = s.execute(
                text("SELECT COUNT(DISTINCT symbol) FROM futures_prices")
            ).scalar() or 0
            per_exch = s.execute(
                text(
                    "SELECT exchange, COUNT(*) FROM futures_prices "
                    "GROUP BY exchange ORDER BY 2 DESC"
                )
            ).fetchall()
        if isinstance(latest, str):
            latest_str = latest
        elif latest is None:
            latest_str = None
        else:
            latest_str = latest.isoformat()
        return {
            "total_rows": int(total),
            "distinct_symbols": int(distinct_syms),
            "latest_trade_date": latest_str,
            "per_exchange": [(str(e), int(c)) for e, c in per_exch],
        }

    # ----------------------------------------------------------------- helpers

    def _collect_for_date(self, target: date) -> list[dict]:
        """Run the AKShare -> Sina fallback chain for one trade date."""
        # ---- 1. AKShare primary
        try:
            rows = akshare_bridge.fetch_main_contract_prices(target)
        except Exception as exc:
            logger.warning(f"akshare batch failed: {exc!r}")
            rows = []

        # Build a set of symbols we already have so we only fetch the rest from Sina.
        have = {r["symbol"] for r in rows}

        # ---- 2. Sina-direct fallback (only for missing symbols)
        try:
            contracts = akshare_bridge.list_main_contracts()
        except Exception as exc:
            logger.warning(f"akshare list_main_contracts failed: {exc!r}")
            contracts = []

        missing = [(c, p) for c, p, _ in contracts if c not in have]
        if missing:
            try:
                sina_rows = sina_fallback.fetch_main_contract_prices(missing, target)
                # attach exchange from our discovery list
                exch_by_sym = {c: e for c, _, e in contracts}
                for r in sina_rows:
                    r["exchange"] = exch_by_sym.get(r["symbol"], "")
                rows.extend(sina_rows)
            except Exception as exc:
                logger.warning(f"sina fallback batch failed: {exc!r}")

        return [self._normalise(r) for r in rows]

    def _normalise(self, row: dict) -> dict:
        exchange = row.get("exchange") or ""
        return _to_dict(row, exchange=exchange)

    def _persist(self, rows: Iterable[dict]) -> int:
        """INSERT OR REPLACE rows into futures_prices. Returns count persisted."""
        rows = list(rows)
        if not rows:
            logger.warning("no rows to persist")
            return 0
        with self._db.tx() as s:  # type: Session
            stmt = sqlite_insert(FuturesPrice).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["trade_date", "symbol"],
                set_={
                    "name": stmt.excluded.name,
                    "exchange": stmt.excluded.exchange,
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "settle": stmt.excluded.settle,
                    "volume": stmt.excluded.volume,
                    "position": stmt.excluded.position,
                    "change_pct": stmt.excluded.change_pct,
                    "fetched_at": stmt.excluded.fetched_at,
                },
            )
            s.execute(stmt)
        logger.info(f"persisted {len(rows)} rows")
        return len(rows)