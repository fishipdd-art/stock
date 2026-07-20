"""
Cache layer with Redis + in-memory fallback.

Two-tier caching:
  - L1: in-memory dict (fast, per-process, lost on restart)
  - L2: Redis (shared, persistent, optional)

When Redis is not available, falls back to L1 only.
Configure via env: REDIS_URL=redis://localhost:6379/0
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from loguru import logger

try:
    import redis as redis_lib
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False
    logger.warning("redis package not installed, using in-memory cache only")


# In-memory cache (always available)
_l1_cache: dict[str, tuple[Any, float]] = {}  # key -> (value, expires_at)
_DEFAULT_TTL = 60  # seconds


def _is_expired(expires_at: float) -> bool:
    return time.time() > expires_at


class Cache:
    """Cache interface with L1 (in-memory) + L2 (Redis) fallback."""

    def __init__(self, redis_url: Optional[str] = None, default_ttl: int = _DEFAULT_TTL):
        self.default_ttl = default_ttl
        self._l2 = None
        if redis_url and HAS_REDIS:
            try:
                self._l2 = redis_lib.from_url(redis_url, decode_responses=True)
                self._l2.ping()
                logger.info(f"Redis cache connected: {redis_url}")
            except Exception as e:
                logger.warning(f"Redis connection failed: {e}. Falling back to in-memory only.")
                self._l2 = None
        self.hits_l1 = 0
        self.hits_l2 = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache. Checks L1 then L2."""
        # L1
        if key in _l1_cache:
            val, exp = _l1_cache[key]
            if not _is_expired(exp):
                self.hits_l1 += 1
                return val
            del _l1_cache[key]

        # L2
        if self._l2 is not None:
            try:
                raw = self._l2.get(key)
                if raw is not None:
                    self.hits_l2 += 1
                    val = json.loads(raw)
                    # Promote to L1
                    _l1_cache[key] = (val, time.time() + self.default_ttl)
                    return val
            except Exception as e:
                logger.warning(f"Redis L2 read error: {e}")

        self.misses += 1
        return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Store value in both L1 and L2."""
        ttl = ttl or self.default_ttl
        expires_at = time.time() + ttl
        _l1_cache[key] = (value, expires_at)

        if self._l2 is not None:
            try:
                self._l2.setex(key, ttl, json.dumps(value, default=str))
            except Exception as e:
                logger.warning(f"Redis L2 write error: {e}")

    def delete(self, key: str) -> None:
        _l1_cache.pop(key, None)
        if self._l2 is not None:
            try:
                self._l2.delete(key)
            except Exception as e:
                logger.warning(f"Redis L2 delete error: {e}")

    def clear(self) -> None:
        _l1_cache.clear()
        if self._l2 is not None:
            try:
                self._l2.flushdb()
            except Exception as e:
                logger.warning(f"Redis L2 clear error: {e}")

    def stats(self) -> dict:
        total = self.hits_l1 + self.hits_l2 + self.misses
        return {
            "l1_hits": self.hits_l1,
            "l2_hits": self.hits_l2,
            "misses": self.misses,
            "total": total,
            "hit_rate": (self.hits_l1 + self.hits_l2) / total if total > 0 else 0,
            "l1_size": len(_l1_cache),
            "l2_connected": self._l2 is not None,
        }


# Singleton cache instance
_cache: Optional[Cache] = None


def get_cache() -> Cache:
    """Get or create singleton cache."""
    global _cache
    if _cache is None:
        redis_url = os.environ.get("REDIS_URL")
        _cache = Cache(redis_url=redis_url)
    return _cache


# Convenience decorator
def cached(key_prefix: str, ttl: int = 60):
    """Decorator: cache function result."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            key = f"{key_prefix}:{args}:{sorted(kwargs.items())}"
            cache = get_cache()
            result = cache.get(key)
            if result is not None:
                return result
            result = fn(*args, **kwargs)
            cache.set(key, result, ttl=ttl)
            return result
        return wrapper
    return decorator