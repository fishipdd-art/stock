"""Tests for web/sse.py rate-limiting and slow-consumer handling."""
from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture
def fresh_sse(monkeypatch):
    """Reset the SSE module's per-process state."""
    monkeypatch.setenv("SSE_RATE_LIMIT_PER_SEC", "10")
    monkeypatch.setenv("SSE_BURST", "20")
    monkeypatch.setenv("SSE_MAX_QUEUE", "5")
    monkeypatch.setenv("SSE_MAX_TOTAL_SUBSCRIBERS", "50")
    from web import sse
    importlib.reload(sse)
    sse._subscribers.clear()
    sse._buckets.clear()
    if hasattr(sse.get_stats, "_slow_dropped"):
        delattr(sse.get_stats, "_slow_dropped")
    return sse


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_burst_allowed(self, fresh_sse):
        # Burst of 20 should be allowed
        for i in range(20):
            gen = fresh_sse.subscribe(["test"], client_id="ip-1")
            await gen.__anext__()  # pull the "subscribed" hello
            await gen.aclose()
        # 21st should fail
        with pytest.raises(PermissionError):
            gen = fresh_sse.subscribe(["test"], client_id="ip-1")
            await gen.__anext__()
            await gen.aclose()

    @pytest.mark.asyncio
    async def test_refill_over_time(self, fresh_sse):
        # Burn the bucket
        for i in range(20):
            gen = fresh_sse.subscribe(["test"], client_id="ip-2")
            await gen.__anext__()
            await gen.aclose()
        # 21st fails
        with pytest.raises(PermissionError):
            gen = fresh_sse.subscribe(["test"], client_id="ip-2")
            await gen.__anext__()
            await gen.aclose()
        # Wait for refill (10 tokens/sec, need 1)
        await asyncio.sleep(0.15)
        gen = fresh_sse.subscribe(["test"], client_id="ip-2")
        await gen.__anext__()
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_different_ips_independent(self, fresh_sse):
        # ip-1 exhausts its bucket
        for i in range(20):
            gen = fresh_sse.subscribe(["test"], client_id="ip-A")
            await gen.__anext__()
            await gen.aclose()
        # ip-B has its own bucket
        gen = fresh_sse.subscribe(["test"], client_id="ip-B")
        await gen.__anext__()
        await gen.aclose()


class TestSlowConsumer:
    @pytest.mark.asyncio
    async def test_slow_consumer_dropped(self, fresh_sse):
        """A subscriber whose queue is full should be removed on next publish."""
        gen = fresh_sse.subscribe(["test"], client_id="ip-slow")
        await gen.__anext__()  # consume the hello
        # Queue is empty. Fill it by publishing more than MAX_QUEUE (5) times.
        for i in range(10):
            await fresh_sse.publish("test", {"i": i})
        # Subscriber should have been dropped
        assert len(fresh_sse._subscribers) == 0
        await gen.aclose()


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_no_subscribers(self, fresh_sse):
        n = await fresh_sse.publish("test", {"x": 1})
        assert n == 0

    @pytest.mark.asyncio
    async def test_publish_topic_mismatch(self, fresh_sse):
        gen = fresh_sse.subscribe(["only-a"], client_id="ip-pub")
        await gen.__anext__()  # hello
        n = await fresh_sse.publish("only-b", {"x": 1})
        assert n == 0
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_publish_topic_match(self, fresh_sse):
        gen = fresh_sse.subscribe(["match"], client_id="ip-pm")
        await gen.__anext__()  # hello
        n = await fresh_sse.publish("match", {"x": 1})
        assert n == 1
        # Drain the queue
        msg = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert msg["data"] == {"x": 1}
        await gen.aclose()

    @pytest.mark.asyncio
    async def test_publish_all_topic(self, fresh_sse):
        gen = fresh_sse.subscribe(["specific"], client_id="ip-all")
        await gen.__anext__()
        n = await fresh_sse.publish("all", {"x": 1})
        assert n == 1
        msg = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        await gen.aclose()


class TestStats:
    def test_initial(self, fresh_sse):
        stats = fresh_sse.get_stats()
        assert stats["subscribers"] == 0
        assert stats["max_subscribers"] == 50
        assert stats["rate_limit_per_sec"] == 10.0
        assert stats["burst"] == 20


class TestMaxSubscribers:
    @pytest.mark.asyncio
    async def test_global_cap(self, monkeypatch):
        monkeypatch.setenv("SSE_MAX_TOTAL_SUBSCRIBERS", "2")
        monkeypatch.setenv("SSE_RATE_LIMIT_PER_SEC", "100")
        monkeypatch.setenv("SSE_BURST", "100")
        from web import sse
        importlib.reload(sse)
        sse._subscribers.clear()
        sse._buckets.clear()

        # First two succeed
        g1 = sse.subscribe(["x"], client_id="ip-1")
        await g1.__anext__()
        g2 = sse.subscribe(["x"], client_id="ip-2")
        await g2.__anext__()
        # Third is rejected
        with pytest.raises(PermissionError):
            g3 = sse.subscribe(["x"], client_id="ip-3")
            await g3.__anext__()
        await g1.aclose()
        await g2.aclose()
