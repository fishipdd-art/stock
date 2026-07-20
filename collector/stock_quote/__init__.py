"""collector.stock_quote — A-share quote + sector hotness engine."""
from .akshare_bridge import (
    fetch_all_spot_quotes,
    filter_to_universe,
    fetch_history_bars,
    quote_rows_for_trade_date,
    hist_rows_for_insert,
    today_date,
    today_yyyymmdd,
)
from .collector import StockQuoteCollector
from .hotness import HotnessEngine
from .seed import seed_demo

__all__ = [
    "fetch_all_spot_quotes",
    "filter_to_universe",
    "fetch_history_bars",
    "quote_rows_for_trade_date",
    "hist_rows_for_insert",
    "today_date",
    "today_yyyymmdd",
    "StockQuoteCollector",
    "HotnessEngine",
    "seed_demo",
]
