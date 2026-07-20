"""
Server-Sent Events (SSE) for real-time event push.

Implements a simple pub/sub system with two protection layers:

  1. **Per-IP rate limit** (token bucket): prevents a single client from
     flooding subscription / publish traffic. Configurable via env
     ``SSE_RATE_LIMIT_PER_SEC`` (default 10/s) and
     ``SSE_BURST`` (default 20).

  2. **Slow-consumer detection**: a subscriber whose queue exceeds
     ``SSE_MAX_QUEUE`` (default 100) is considered stuck (browser tab
     backgrounded, mobile screen off, etc.) and is dropped on the next
     publish round, freeing the event loop.

Topics:
  - 'new_event': new event added
  - 'price_alert': significant price move detected
  - 'signal_alert': new high-impact signal
  - 'system': system status updates (heartbeat, job done)
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator


# ============================================================================
# Rate-limit config (env-driven)
# ============================================================================

RATE_LIMIT_PER_SEC = float(os.environ.get("SSE_RATE_LIMIT_PER_SEC", "10"))
RATE_BURST = int(os.environ.get("SSE_BURST", "20"))
MAX_QUEUESIZE = int(os.environ.get("SSE_MAX_QUEUE", "100"))
MAX_TOTAL_SUBSCRIBERS = int(os.environ.get("SSE_MAX_TOTAL_SUBSCRIBERS", "200"))


# ============================================================================
# In-memory pub/sub
# ============================================================================

@dataclass
class _Subscriber:
    queue: asyncio.Queue
    topics: set[str]
    client_id: str = "unknown"
    created_at: float = field(default_factory=time.time)


_subscribers: list[_Subscriber] = []


# Per-client_id token bucket. Each entry: (tokens_remaining, last_refill_ts).
_buckets: dict[str, tuple[float, float]] = {}


def _refill(client_id: str) -> float:
    """Refill a token bucket based on elapsed time, return available tokens."""
    tokens, last_ts = _buckets.get(client_id, (float(RATE_BURST), time.time()))
    now = time.time()
    elapsed = now - last_ts
    tokens = min(RATE_BURST, tokens + elapsed * RATE_LIMIT_PER_SEC)
    _buckets[client_id] = (tokens, now)
    return tokens


def _take_token(client_id: str) -> bool:
    """Try to consume 1 token. Returns True if allowed."""
    tokens = _refill(client_id)
    if tokens < 1.0:
        _buckets[client_id] = (tokens, time.time())
        return False
    _buckets[client_id] = (tokens - 1.0, time.time())
    return True


def get_subscriber_count() -> int:
    return len(_subscribers)


def get_stats() -> dict:
    """Diagnostic snapshot for /api/stream/stats."""
    return {
        "subscribers": len(_subscribers),
        "max_subscribers": MAX_TOTAL_SUBSCRIBERS,
        "rate_limit_per_sec": RATE_LIMIT_PER_SEC,
        "burst": RATE_BURST,
        "active_clients": len(_buckets),
        "slow_consumers_dropped": getattr(get_stats, "_slow_dropped", 0),
    }


async def subscribe(
    topics: list[str],
    client_id: str = "unknown",
) -> AsyncIterator[dict]:
    """Subscribe to a list of topics. Yields events as dicts.

    Raises ``PermissionError`` if the per-IP rate limit is exceeded or
    if the global subscriber cap is reached.
    """
    if not _take_token(client_id):
        raise PermissionError("rate limit exceeded")

    if len(_subscribers) >= MAX_TOTAL_SUBSCRIBERS:
        raise PermissionError(f"max subscribers ({MAX_TOTAL_SUBSCRIBERS}) reached")

    queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUESIZE)
    sub = _Subscriber(queue=queue, topics=set(topics), client_id=client_id)
    _subscribers.append(sub)
    try:
        # Send initial hello
        yield {
            "event": "system",
            "data": {"message": "subscribed", "topics": topics},
            "ts": datetime.utcnow().isoformat(),
        }
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield msg
            except asyncio.TimeoutError:
                # Heartbeat
                yield {
                    "event": "heartbeat",
                    "data": {"ts": datetime.utcnow().isoformat()},
                    "ts": datetime.utcnow().isoformat(),
                }
    finally:
        try:
            _subscribers.remove(sub)
        except ValueError:
            pass


async def publish(topic: str, data: dict) -> int:
    """Publish a message to all subscribers of the given topic.

    Slow consumers (queue full) are dropped. Returns the number of
    subscribers actually delivered to.
    """
    n = 0
    payload = {
        "event": topic,
        "data": data,
        "ts": datetime.utcnow().isoformat(),
    }
    # Snapshot the subscriber list to allow safe mutation
    for sub in list(_subscribers):
        # "all" topic from publisher means broadcast to every subscriber.
        # Otherwise, only deliver if the subscriber asked for this topic.
        if topic == "all" or topic in sub.topics:
            try:
                sub.queue.put_nowait(payload)
                n += 1
            except asyncio.QueueFull:
                # Slow consumer: drop and record
                get_stats._slow_dropped = getattr(get_stats, "_slow_dropped", 0) + 1
                try:
                    _subscribers.remove(sub)
                except ValueError:
                    pass
    return n


async def broadcast(data: dict) -> int:
    """Broadcast to all subscribers (no topic filter)."""
    return await publish("all", data)


# ============================================================================
# High-level event publishers
# ============================================================================

async def publish_new_event(event_data: dict):
    """Publish when a new event is added."""
    await publish("new_event", event_data)


async def publish_price_alert(symbol: str, change_pct: float, price: float):
    """Publish when a significant price move detected."""
    await publish("price_alert", {
        "symbol": symbol,
        "change_pct": change_pct,
        "price": price,
    })


async def publish_signal_alert(signal_data: dict):
    await publish("signal_alert", signal_data)


async def publish_system(message: str, level: str = "info"):
    await publish("system", {"message": message, "level": level})