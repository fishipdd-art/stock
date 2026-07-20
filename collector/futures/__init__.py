"""Futures collector package.

Re-exports the public API so callers can do:

    from collector.futures import FuturesCollector
    from collector.futures.contracts import symbol_to_industry
"""
from .collector import FuturesCollector
from .contracts import (
    COMMODITY_TO_INDUSTRY,
    COMMODITY_TO_EXCHANGE,
    COMMODITY_DISPLAY_NAME,
    build_contract_name,
    symbol_to_display_name,
    symbol_to_exchange,
    symbol_to_industry,
)

__all__ = [
    "FuturesCollector",
    "COMMODITY_TO_INDUSTRY",
    "COMMODITY_TO_EXCHANGE",
    "COMMODITY_DISPLAY_NAME",
    "build_contract_name",
    "symbol_to_industry",
    "symbol_to_exchange",
    "symbol_to_display_name",
]