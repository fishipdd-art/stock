"""Tests for cache/redis_cache.py.

The cache is a two-tier wrapper: L1 in-memory always works, L2 Redis
is optional. The tests focus on the L1 path (which is the fallback
when Redis isn't configured).
"""
from __future__ import annotations

import time

import pytest

from cache import redis_cache as rc


@pytest.fixture(autouse=True)
def _reset_l1():
    """Wipe the module-level L1 dict before each test."""
    rc._l1_cache.clear()
    yield
    rc._l1_cache.clear()


class TestL1Cache:
    def test_set_and_get(self):
        c = rc.Cache(redis_url=None)  # L1 only
        c.set("k", {"a": 1})
        assert c.get("k") == {"a": 1}

    def test_miss_returns_none(self):
        c = rc.Cache(redis_url=None)
        assert c.get("nonexistent") is None

    def test_ttl_expiry(self):
        c = rc.Cache(redis_url=None, default_ttl=1)
        c.set("k", "v", ttl=1)
        assert c.get("k") == "v"
        time.sleep(1.2)
        assert c.get("k") is None

    def test_overwrite(self):
        c = rc.Cache(redis_url=None)
        c.set("k", "v1")
        c.set("k", "v2")
        assert c.get("k") == "v2"

    def test_delete(self):
        c = rc.Cache(redis_url=None)
        c.set("k", "v")
        c.delete("k")
        assert c.get("k") is None

    def test_clear(self):
        c = rc.Cache(redis_url=None)
        c.set("a", 1)
        c.set("b", 2)
        c.clear()
        assert c.get("a") is None
        assert c.get("b") is None

    def test_stats(self):
        c = rc.Cache(redis_url=None)
        c.set("k", "v")
        c.get("k")   # hit
        c.get("miss")  # miss
        stats = c.stats()
        assert stats["l1_hits"] == 1
        assert stats["misses"] == 1
        assert stats["total"] == 2
        assert 0 < stats["hit_rate"] < 1


class TestGetCache:
    def test_singleton(self):
        c1 = rc.get_cache()
        c2 = rc.get_cache()
        assert c1 is c2

    def test_singleton_resets_between_modules(self, monkeypatch):
        # Reset module-level singleton
        monkeypatch.setattr(rc, "_cache", None)
        c = rc.get_cache()
        assert c is not None
