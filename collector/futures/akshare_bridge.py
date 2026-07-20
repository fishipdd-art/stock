"""
AKShare adapter for daily futures prices (L3, end-of-day).

Strategy
--------
1. Discover the active main contract per exchange via
   ``ak.futures_display_main_sina()``  ->  ``{symbol: "CU0", exchange: "shfe", name: "铜连续"}``.
2. Resolve to the *concrete* main contract codes via
   ``ak.match_main_contract(symbol=<exchange>)``  ->  e.g. ``"CU2608"``.
3. Pull OHLCV from ``ak.futures_main_sina(symbol=<prefix>0)`` (the continuous
   contract series). The last row corresponds to the most recent trade day.
4. Compute ``change_pct`` from the previous trading day's close because AKShare
   does not return it directly.

Notes
-----
* Each AKShare call is wrapped in try/except so a broken signature on one
  function does not crash the entire collection run.
* All datetime math uses the local Asia/Shanghai trading calendar; we never
  assume US/EU timestamps.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from loguru import logger


# ---------------------------------------------------------------------------
# Cached discovery of the main contract list
# ---------------------------------------------------------------------------

# (continuous symbol like "CU0", commodity prefix "CU", exchange "SHFE")
MainContractMeta = tuple[str, str, str]
# (concrete symbol like "CU2608", commodity prefix "CU", exchange "SHFE")
ConcreteContractMeta = tuple[str, str, str]


def _safe_call(fn, *args: Any, **kwargs: Any) -> Any:
    """Call an akshare function with logging on failure.

    Returns ``None`` on exception so the caller can branch cleanly.
    """
    name = getattr(fn, "__name__", str(fn))
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.warning(f"akshare.{name} failed: {type(exc).__name__}: {exc}")
        return None


def list_main_contracts() -> list[ConcreteContractMeta]:
    """Return the active main contract for every Chinese commodity.

    Each entry is ``(concrete_code, commodity_prefix, exchange_code)`` where
    ``concrete_code`` looks like ``CU2608`` and ``commodity_prefix`` is
    ``CU``. We prefer ``ak.match_main_contract`` (concrete contract) over the
    continuous series because the storage model expects a real contract code.
    """
    from collector.futures.contracts import COMMODITY_TO_EXCHANGE

    display_df = _safe_call(_ak_display_main_sina)
    if display_df is None or display_df.empty:
        logger.warning("ak.futures_display_main_sina returned empty; falling back to static map")
        return _fallback_main_contracts()

    # Map continuous symbol (e.g. "CU0") -> exchange code ("SHFE")
    continuous_to_exch: dict[str, str] = {}
    for _, row in display_df.iterrows():
        sym = str(row.get("symbol", "")).strip()
        exch_raw = str(row.get("exchange", "")).strip().lower()
        exch = {
            "shfe": "SHFE", "dce": "DCE", "czce": "CZCE",
            "cffex": "CFFEX", "ine": "INE", "gfex": "GFEX",
        }.get(exch_raw, "")
        if sym and exch:
            continuous_to_exch[sym] = exch

    exchange_to_continuous: dict[str, list[str]] = {}
    for sym, exch in continuous_to_exch.items():
        exchange_to_continuous.setdefault(exch, []).append(sym)

    # Now resolve concrete contract codes per exchange.
    out: list[ConcreteContractMeta] = []
    for exchange, symbols in exchange_to_continuous.items():
        exchange_lower = {
            "SHFE": "shfe", "DCE": "dce", "CZCE": "czce",
            "CFFEX": "cffex", "INE": "ine", "GFEX": "gfex",
        }.get(exchange, exchange.lower())
        raw = _safe_call(_ak_match_main_contract, exchange_lower)
        if not raw or not isinstance(raw, str):
            for s in symbols:
                prefix = _strip_trailing_digits(s)
                out.append((s, prefix, exchange))
            continue
        last_line = raw.strip().splitlines()[-1].strip()
        if last_line.endswith("主力合约获取成功") or not last_line:
            for s in symbols:
                prefix = _strip_trailing_digits(s)
                out.append((s, prefix, exchange))
            continue
        concrete = [c.strip() for c in last_line.split(",") if c.strip()]
        for code in concrete:
            prefix = _strip_trailing_digits(code)
            exch_for_prefix = COMMODITY_TO_EXCHANGE.get(prefix, exchange)
            out.append((code, prefix, exch_for_prefix))

    # de-dup
    seen: set[str] = set()
    unique: list[ConcreteContractMeta] = []
    for code, prefix, exch in out:
        if code in seen:
            continue
        seen.add(code)
        unique.append((code, prefix, exch))
    return unique


def _fallback_main_contracts() -> list[ConcreteContractMeta]:
    """Static fallback when AKShare is unreachable.

    Returns the *continuous* contract codes (e.g. ``CU0``) which can still be
    used by ``futures_main_sina``. Less precise than the concrete contract
    codes, but keeps the system running.
    """
    from collector.futures.contracts import COMMODITY_TO_EXCHANGE
    out: list[ConcreteContractMeta] = []
    for prefix, exch in COMMODITY_TO_EXCHANGE.items():
        out.append((f"{prefix}0", prefix, exch))
    return out


# ---------------------------------------------------------------------------
# Low-level AKShare wrappers (kept thin so failures are easy to spot)
# ---------------------------------------------------------------------------

def _ak_display_main_sina():
    import akshare as ak
    return ak.futures_display_main_sina()


def _ak_match_main_contract(exchange: str):
    import akshare as ak
    return ak.match_main_contract(symbol=exchange)


def _ak_main_sina(symbol: str, start_date: str, end_date: str):
    import akshare as ak
    return ak.futures_main_sina(symbol=symbol, start_date=start_date, end_date=end_date)


def _ak_foreign_commodity_subscribe_symbol():
    import akshare as ak
    return ak.futures_foreign_commodity_subscribe_exchange_symbol()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_main_contract_prices(trade_date: date) -> list[dict]:
    """Fetch daily OHLCV for every active main contract via AKShare.

    Returns a list of dicts whose keys map to ``FuturesPrice`` columns.
    Any per-contract failure is logged and skipped so a single broken call
    does not poison the whole batch.
    """
    contracts = list_main_contracts()
    logger.info(f"AKShare: {len(contracts)} main contracts discovered")

    rows: list[dict] = []
    for concrete_code, prefix, exchange in contracts:
        try:
            # Pull a few extra days so we can compute change_pct vs prev close.
            start = (trade_date - timedelta(days=10)).strftime("%Y%m%d")
            end = trade_date.strftime("%Y%m%d")
            df = _safe_call(_ak_main_sina, f"{prefix}0", start, end)
            if df is None or df.empty:
                logger.debug(f"akshare empty: {prefix}0 ({concrete_code})")
                continue

            row_dict = _row_from_akshare_main(df, trade_date)
            if row_dict is None:
                continue

            row_dict["symbol"] = concrete_code
            row_dict["exchange"] = exchange
            rows.append(row_dict)
        except Exception as exc:
            logger.warning(f"unexpected error for {concrete_code}: {exc!r}")

    logger.info(f"AKShare: collected {len(rows)}/{len(contracts)} contracts")
    return rows


def _row_from_akshare_main(df: pd.DataFrame, trade_date: date) -> dict | None:
    """Convert a futures_main_sina dataframe into a FuturesPrice dict for one day.

    Columns: ['日期', '开盘价', '最高价', '最低价', '收盘价', '成交量', '持仓量', '动态结算价']
    """
    if df.empty:
        return None

    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"]).dt.date
    df = df.sort_values("日期").reset_index(drop=True)

    available = df[df["日期"] <= trade_date]
    if available.empty:
        return None
    rows_list = list(available.to_dict("records"))
    today_row = rows_list[-1]
    prev_row = rows_list[-2] if len(rows_list) >= 2 else None
    actual_date = today_row["日期"]

    close = float(today_row.get("收盘价") or 0.0)
    if prev_row is not None:
        prev_close = float(prev_row.get("收盘价") or 0.0)
        change_pct = (close - prev_close) / prev_close * 100.0 if prev_close else 0.0
    else:
        change_pct = 0.0

    return {
        "trade_date": actual_date,
        "open": float(today_row.get("开盘价") or 0.0),
        "high": float(today_row.get("最高价") or 0.0),
        "low": float(today_row.get("最低价") or 0.0),
        "close": close,
        "settle": float(today_row.get("动态结算价") or 0.0),
        "volume": float(today_row.get("成交量") or 0.0),
        "position": float(today_row.get("持仓量") or 0.0),
        "change_pct": round(change_pct, 4),
    }


# ---------------------------------------------------------------------------
# Historical backfill (multi-day)
# ---------------------------------------------------------------------------

def fetch_history(contract_code: str, days_back: int) -> list[dict]:
    """Fetch up to ``days_back`` recent trading days for one main contract.

    Returns one dict per trading day, mapped to ``FuturesPrice`` columns.
    """
    prefix = _strip_trailing_digits(contract_code)
    end = datetime.utcnow().date()
    start = end - timedelta(days=days_back + 10)

    df = _safe_call(_ak_main_sina, f"{prefix}0", start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if df is None or df.empty:
        return []

    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"]).dt.date
    df = df.sort_values("日期").reset_index(drop=True).tail(days_back + 1)

    out: list[dict] = []
    rows_list = list(df.to_dict("records"))
    for i, row in enumerate(rows_list):
        prev = rows_list[i - 1] if i > 0 else None
        close = float(row.get("收盘价") or 0.0)
        if prev is not None:
            prev_close = float(prev.get("收盘价") or 0.0)
            change_pct = (close - prev_close) / prev_close * 100.0 if prev_close else 0.0
        else:
            change_pct = 0.0
        out.append({
            "trade_date": row["日期"],
            "open": float(row.get("开盘价") or 0.0),
            "high": float(row.get("最高价") or 0.0),
            "low": float(row.get("最低价") or 0.0),
            "close": close,
            "settle": float(row.get("动态结算价") or 0.0),
            "volume": float(row.get("成交量") or 0.0),
            "position": float(row.get("持仓量") or 0.0),
            "change_pct": round(change_pct, 4),
        })
    return out


def _strip_trailing_digits(s: str) -> str:
    i = 0
    while i < len(s) and s[i].isalpha():
        i += 1
    return s[:i].upper()