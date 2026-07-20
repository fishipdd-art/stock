"""Collector base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from loguru import logger


class BaseCollector(ABC):
    """Base class for all data collectors."""

    name: str = "base"
    enabled: bool = True

    @abstractmethod
    def fetch(self, *args, **kwargs) -> Any:
        """Fetch data."""

    def safe_run(self, *args, **kwargs):
        """Run with try/except wrapper that logs errors."""
        try:
            return self.fetch(*args, **kwargs)
        except Exception as e:
            logger.exception(f"[{self.name}] fetch failed: {e}")
            return None