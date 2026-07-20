"""
Sina-direct HTTP fallback for futures prices.

When AKShare is down / blocked we hit ``hq.sinajs.cn`` directly. The URL
``https://hq.sinajs.cn/list=nf_CU0,nf_AU0,...`` returns one line per contract
in the classic ``var hq_str_nf_<SYM>="<fields>"`` JavaScript format.

Headers must include the ``Referer`` of a real Sina finance page or the
endpoint returns HTTP 403.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import httpx
from loguru import logger

from config.settings import settings


_SINA_URL = "https://hq.sinajs.cn/list={symbols}"
_DEFAULT_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# Regex: var hq_str_nf_CU0="...";
_LINE_RE = re.compile(r'var\s+hq_str_([A-Za-z0-9_]+)="(.*?)";')


def _strip_trailing_digits(s: str) -> str:
    i = 0
    while i < len(s) and s[i].isalpha():
        i += 1
    return s[:i].upper()


def _parse_field_line(symbol: str, payload: str) -> dict | None:
    """Parse one ``hq_str_nf_*=...`` line.

    Most commodity futures have 30+ fields with the trade date at index 17.
    CFFEX bond/index futures use a different layout with the date at index 36.
    We try the commodity layout first and fall back to the CFFEX layout.
    """
    fields = payload.split(",")
    if len(fields) < 8:
        return None

    # Trade date can be in either field 17 (commodity) or field 36 (CFFEX).
    trade_date_str = fields[17] if len(fields) > 17 else ""
    trade_date: date | None = None
    for candidate in (fields[17] if len(fields) > 17 else "",
                      fields[36] if len(fields) > 36 else ""):
        if not candidate:
            continue
        try:
            trade_date = datetime.strptime(candidate.strip(), "%Y-%m-%d").date()
            break
        except (ValueError, TypeError):
            continue
    if trade_date is None:
        return None

    def _f(idx: int) -> float:
        if idx >= len(fields) or not fields[idx]:
            return 0.0
        try:
            return float(fields[idx])
        except (ValueError, TypeError):
            return 0.0

    # Commodity layout (most futures):
    #   0  name
    #   1  open-time-millis
    #   2  open
    #   3  high
    #   4  low
    #   5  current / latest close
    #   6  bid
    #   7  ask
    #   8  ?
    #   9  settle
    #   10 prev_settle
    #   13 position
    #   14 volume
    # CFFEX layout (index / bond futures):
    #   0  current
    #   1  high
    #   2  low
    #   3  open
    #   ...
    is_cffex_layout = symbol.upper().startswith(("IF", "IH", "IC", "IM", "T", "TF", "TS", "TL"))
    if is_cffex_layout and len(fields) >= 20:
        open_ = _f(3)
        high = _f(1)
        low = _f(2)
        close = _f(0)
        settle = _f(7)
        prev_settle = _f(8) if len(fields) > 8 else 0.0
        position = _f(14)
        volume = _f(15)
    else:
        open_ = _f(2)
        high = _f(3)
        low = _f(4)
        close = _f(5)
        settle = _f(9)
        prev_settle = _f(10)
        position = _f(13)
        volume = _f(14)

    change_pct = (settle - prev_settle) / prev_settle * 100.0 if prev_settle else 0.0

    return {
        "trade_date": trade_date,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "settle": settle,
        "volume": volume,
        "position": position,
        "change_pct": round(change_pct, 4),
        "_raw_sina_symbol": symbol,
    }


def fetch_main_contract_prices(
    contracts: list[tuple[str, str]],
    trade_date: date | None = None,
) -> list[dict]:
    """Fetch L3 prices from Sina-direct for the given main contracts.

    Parameters
    ----------
    contracts : list of ``(concrete_code, commodity_prefix)`` tuples.
    trade_date : optional date to filter rows on. ``None`` returns every
        trade_date the API sends (typically just today / yesterday).

    Returns
    -------
    list of dicts with FuturesPrice columns (without ``symbol`` / ``exchange``
    set -- the caller maps those on).
    """
    if not contracts:
        return []

    # Sina expects the *continuous* contract symbol, e.g. "CU0", not "CU2608".
    symbols = [f"nf_{prefix}0" for _, prefix in contracts]
    # Sina caps URL length; chunk to be safe.
    chunk_size = 40
    out: list[dict] = []
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        url = _SINA_URL.format(symbols=",".join(chunk))
        try:
            resp = httpx.get(
                url,
                headers=_DEFAULT_HEADERS,
                timeout=settings.http_timeout,
            )
            if resp.status_code != 200:
                logger.warning(f"sina hq returned HTTP {resp.status_code}")
                continue
            text = resp.text
        except httpx.HTTPError as exc:
            logger.warning(f"sina hq request failed: {exc!r}")
            continue

        # Build prefix->concrete_code map for this chunk.
        prefix_to_concrete: dict[str, str] = {}
        for concrete, prefix in contracts[i:i + chunk_size]:
            prefix_to_concrete[prefix.upper()] = concrete

        for match in _LINE_RE.finditer(text):
            var_name, payload = match.group(1), match.group(2)
            # var_name is like "nf_CU0" — strip "nf_" and trailing "0"
            raw = var_name[3:] if var_name.startswith("nf_") else var_name
            prefix = _strip_trailing_digits(raw)
            row = _parse_field_line(prefix, payload)
            if row is None:
                continue
            if trade_date is not None and row["trade_date"] != trade_date:
                # L3 with 0.5-day tolerance: also accept yesterday (data may
                # arrive half a day late after market close).
                if (trade_date - row["trade_date"]).days > 1:
                    continue
            concrete = prefix_to_concrete.get(prefix)
            if concrete is None:
                continue
            row["symbol"] = concrete
            row.pop("_raw_sina_symbol", None)
            out.append(row)

    logger.info(f"Sina-direct: collected {len(out)} contracts")
    return out


def fetch_history(contract_code: str, days_back: int) -> list[dict]:
    """Sina-direct history backfill.

    The endpoint only returns the latest snapshot per contract; for multi-day
    backfills we fall back to AKShare. Kept here for API parity.
    """
    prefix = _strip_trailing_digits(contract_code)
    rows = fetch_main_contract_prices([(contract_code, prefix)])
    # Tag every row with the same symbol/exchange so callers can persist them.
    from collector.futures.contracts import COMMODITY_TO_EXCHANGE
    for r in rows:
        r["symbol"] = contract_code
        r["exchange"] = COMMODITY_TO_EXCHANGE.get(prefix, "")
    # days_back is honored only loosely; the snapshot endpoint can't return
    # multiple days, so this is just a single-point fallback.
    return rows[:1] if days_back > 0 else []