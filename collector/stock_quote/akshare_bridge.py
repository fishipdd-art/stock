"""
akshare bridge for A-share spot quotes and historical bars.

Wraps:
  - ak.stock_zh_a_spot_em()   : snapshot quotes for ~5800 A-shares
  - ak.stock_zh_a_hist()      : per-stock historical OHLCV

The data is mapped to the columns of storage.models.StockQuote so the
collector layer can persist it without translation.
"""
from __future__ import annotations

import math
import random
import re
import time
from datetime import date, datetime
from typing import Callable, Iterable, TypeVar

import pandas as pd
import httpx
from loguru import logger

try:
    import akshare as ak
    import httpx
except Exception as e:  # pragma: no cover - import-time guard
    ak = None  # type: ignore[assignment]
    _AK_IMPORT_ERR = e
else:
    _AK_IMPORT_ERR = None

# Network errors that should trigger a retry. Note: httpx errors do NOT
# inherit from built-in ConnectionError, so we list them explicitly.
_RETRIABLE: tuple = (
    ConnectionError,
    TimeoutError,
    OSError,
    httpx.HTTPError,
)


T = TypeVar("T")


def _retry(fn: Callable[[], T], attempts: int = 3, base_delay: float = 4.0) -> T:
    """Retry `fn()` with exponential backoff and small jitter.

    Retries on any httpx / socket / connection error. Other exceptions
    (ValueError, KeyError, etc.) bubble up immediately so we don't mask bugs.
    """
    last_err = None
    for i in range(attempts):
        try:
            return fn()
        except _RETRIABLE as e:
            last_err = e
            if i == attempts - 1:
                break
            delay = base_delay * (2 ** i) + random.uniform(0, 1)
            logger.warning(
                f"attempt {i+1}/{attempts} failed: {type(e).__name__}: {e!r}; "
                f"retrying in {delay:.1f}s"
            )
            time.sleep(delay)
    raise last_err  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Column mapping
# ---------------------------------------------------------------------------

# snapshot column renames (ak.stock_zh_a_spot_em -> StockQuote)
_SNAPSHOT_MAP = {
    "代码": "code",
    "名称": "name",
    "最新价": "close",
    "今开": "open",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "turnover",
    "涨跌幅": "change_pct",
    "涨跌额": "change_amt",
}

# hist column renames (ak.stock_zh_a_hist -> StockQuote)
_HIST_MAP = {
    "日期": "trade_date",
    "股票代码": "code",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "turnover",
    "涨跌幅": "change_pct",
    "涨跌额": "change_amt",
}

_BJ_PREFIX_RE = re.compile(r"^\d{6}$")


def _require_ak() -> None:
    if ak is None:
        raise RuntimeError(
            f"akshare is not importable: {_AK_IMPORT_ERR!r}. "
            "Install with `pip install akshare>=1.15.0`."
        )


# ---------------------------------------------------------------------------
# Snapshot (all A-shares)
# ---------------------------------------------------------------------------

def fetch_all_spot_quotes() -> pd.DataFrame:
    """Fetch the full A-share spot quote table from Eastmoney via akshare.

    Returns a DataFrame with our canonical column names. Rows with no
    code or NaN-only prices are dropped. Numeric columns are coerced
    to float (errors become NaN, then 0).

    Wraps the upstream call with retry+backoff since Eastmoney frequently
    drops mid-pagination (ConnectionError / RemoteDisconnected).
    """
    _require_ak()
    logger.info("fetch_all_spot_quotes: calling ak.stock_zh_a_spot_em() (with retry)")
    df = _retry(lambda: ak.stock_zh_a_spot_em(), attempts=3, base_delay=4.0)
    if df is None or df.empty:
        logger.warning("fetch_all_spot_quotes: empty DataFrame returned")
        return pd.DataFrame(columns=list(_SNAPSHOT_MAP.values()))

    keep = {c: _SNAPSHOT_MAP[c] for c in _SNAPSHOT_MAP if c in df.columns}
    missing = set(_SNAPSHOT_MAP) - set(keep)
    if missing:
        logger.warning(f"fetch_all_spot_quotes: missing cols from akshare: {missing}")
    df = df[list(keep.keys())].rename(columns=keep)

    df = _coerce_numeric(df)
    df = df.dropna(subset=["code"])
    df = df[df["code"].astype(str).str.match(_BJ_PREFIX_RE)]
    df = df.drop_duplicates(subset=["code"], keep="last")
    return df.reset_index(drop=True)


def fetch_sina_spot_quotes(codes: Iterable[str]) -> pd.DataFrame:
    """Fetch a batch of spot quotes from Sina as an Eastmoney fallback.

    Sina accepts comma-separated ``sh/sz/bj`` symbols and returns a compact
    GBK text payload.  This endpoint is intentionally kept independent of
    AkShare so a single upstream outage does not blank the whole dashboard.
    """
    symbols: list[str] = []
    for raw in codes:
        code = str(raw).zfill(6)
        if code.startswith(("600", "601", "603", "605", "688")):
            prefix = "sh"
        elif code.startswith(("000", "001", "002", "003", "159", "300", "301")):
            prefix = "sz"
        elif code.startswith(("510", "511", "512", "513", "515", "516", "517", "518", "560", "561", "562", "563", "588")):
            prefix = "sh"
        elif code.startswith(("430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872")):
            prefix = "bj"
        else:
            continue
        symbols.append(prefix + code)
    if not symbols:
        return pd.DataFrame(columns=list(_SNAPSHOT_MAP.values()))
    url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
    try:
        response = httpx.get(
            url,
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=15,
        )
        response.raise_for_status()
        text = response.content.decode("gbk", errors="ignore")
    except Exception as exc:
        logger.warning(f"fetch_sina_spot_quotes failed: {exc!r}")
        return pd.DataFrame(columns=list(_SNAPSHOT_MAP.values()))

    rows: list[dict] = []
    for match in re.finditer(r'var hq_str_(?:sh|sz|bj)(\d{6})="([^"]*)";', text):
        code, raw = match.groups()
        fields = raw.split(",")
        if len(fields) < 32:
            continue
        rows.append({
            "code": code,
            "name": fields[0],
            "open": _f(fields[1]),
            "close": _f(fields[3]),
            "high": _f(fields[4]),
            "low": _f(fields[5]),
            "volume": _f(fields[8]),
            "turnover": _f(fields[9]),
            "change_pct": ((_f(fields[3]) - _f(fields[2])) / _f(fields[2]) * 100) if _f(fields[2]) else 0.0,
            "change_amt": _f(fields[3]) - _f(fields[2]),
        })
    return pd.DataFrame(rows, columns=list(_SNAPSHOT_MAP.values()))


def filter_to_universe(df: pd.DataFrame, codes: Iterable[str]) -> pd.DataFrame:
    """Return only rows whose code is in the given iterable (set-like).

    The iterable is converted to a set internally, so passing a list of
    148 codes is O(N).
    """
    if df is None or df.empty:
        return df
    code_set = {str(c).zfill(6) for c in codes}
    if not code_set:
        return df.iloc[0:0]
    mask = df["code"].astype(str).str.zfill(6).isin(code_set)
    return df.loc[mask].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Historical bars (per stock)
# ---------------------------------------------------------------------------

def fetch_history_bars(
    code: str,
    start_date: str,
    end_date: str,
    adjust: str = "qfq",
) -> pd.DataFrame:
    """Fetch historical OHLCV bars for a single stock.

    Args:
        code: 6-digit stock code (e.g. '000001').
        start_date / end_date: 'YYYYMMDD' strings, akshare convention.
        adjust: 'qfq' / 'hfq' / '' (no adjust).

    Returns:
        DataFrame with canonical column names; an empty DataFrame on error
        (errors are logged but never raised, so a single bad stock won't
        break a backfill run).
    """
    _require_ak()
    code = str(code).zfill(6)
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
    except Exception as e:
        logger.warning(f"fetch_history_bars({code}): {e!r}")
        return pd.DataFrame(columns=list(_HIST_MAP.values()))

    if df is None or df.empty:
        return pd.DataFrame(columns=list(_HIST_MAP.values()))

    keep = {c: _HIST_MAP[c] for c in _HIST_MAP if c in df.columns}
    missing = set(_HIST_MAP) - set(keep)
    if missing:
        logger.warning(f"fetch_history_bars({code}): missing cols {missing}")
    df = df[list(keep.keys())].rename(columns=keep)

    df = _coerce_numeric(df)
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date
    df = df.dropna(subset=["trade_date", "code"])
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def quote_rows_for_trade_date(
    df: pd.DataFrame,
    trade_date: date,
) -> list[dict]:
    """Build a list of dicts ready for StockQuote insertion from a snapshot
    DataFrame. The snapshot is a single trading day, so we stamp trade_date
    on every row.
    """
    if df is None or df.empty:
        return []
    out: list[dict] = []
    for _, r in df.iterrows():
        out.append(
            {
                "trade_date": trade_date,
                "code": str(r["code"]).zfill(6),
                "name": str(r.get("name") or ""),
                "open": _f(r.get("open")),
                "close": _f(r.get("close")),
                "high": _f(r.get("high")),
                "low": _f(r.get("low")),
                "volume": _f(r.get("volume")),
                "turnover": _f(r.get("turnover")),
                "change_pct": _f(r.get("change_pct")),
                "change_amt": _f(r.get("change_amt")),
            }
        )
    return out


def hist_rows_for_insert(df: pd.DataFrame) -> list[dict]:
    """Build dicts from a history DataFrame (already has trade_date)."""
    if df is None or df.empty:
        return []
    out: list[dict] = []
    for _, r in df.iterrows():
        out.append(
            {
                "trade_date": r["trade_date"],
                "code": str(r["code"]).zfill(6),
                "name": "",
                "open": _f(r.get("open")),
                "close": _f(r.get("close")),
                "high": _f(r.get("high")),
                "low": _f(r.get("low")),
                "volume": _f(r.get("volume")),
                "turnover": _f(r.get("turnover")),
                "change_pct": _f(r.get("change_pct")),
                "change_amt": _f(r.get("change_amt")),
            }
        )
    return out


def _coerce_numeric(df: pd.DataFrame) -> pd.DataFrame:
    num_cols = [c for c in [
        "open", "close", "high", "low",
        "volume", "turnover", "change_pct", "change_amt",
    ] if c in df.columns]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.fillna({c: 0.0 for c in num_cols})


def _f(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def today_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def today_date() -> date:
    return datetime.now().date()
