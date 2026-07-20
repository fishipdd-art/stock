"""Collector package.

Re-exports the shared BaseCollector (for all data collectors). Concrete
collectors live in subpackages: news/, futures/, stock_quote/.
"""
from __future__ import annotations

try:
    from .base import BaseCollector  # noqa: F401
except Exception:  # pragma: no cover - tolerant of missing/partial base
    BaseCollector = None  # type: ignore[assignment]
