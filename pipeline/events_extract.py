"""Pipeline: WF-02 — extract structured supply-chain events from news.

Python performs candidate selection, normalization, dedup and obvious-noise
filtering before calling the LLM. The LLM (MiniMax M2.7 Highspeed) returns a
strict-JSON event payload, which is validated against ``EVENT_SCHEMA`` and
persisted into ``storage_events``.

Quality gate (mirrored by Dify's If/Else node):
  - candidate_count >= 1
  - extract_success_rate >= 0.5
  - at least one event has confidence >= 0.5 OR magnitude >= 3

When the gate fails, ``execute_pipeline_run`` records ``quality_status = warn``
or ``fail`` and persists 0 rows. The Dify branch then renders a degraded
report instead of a buy recommendation.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from storage import get_db
from storage.models import NewsRaw, StorageEvent
from nlp.llm_supply_chain import (
    EVENT_SCHEMA_VERSION,
    extract_event_safely,
)


# Reasonable defaults for a single Dify-triggered pass
DEFAULT_HOURS_BACK = 24
DEFAULT_LIMIT = 50
MIN_EXTRACT_SUCCESS_RATE = 0.5
MIN_CONFIDENCE = 0.5
MIN_MAGNITUDE = 3.0

# Words that flag obvious noise / promo content
_NOISE_TOKENS = (
    "广告", "推广", "限时", "优惠", "红包", "抽奖",
    "点击", "扫码", "直播间", "微商", "代理",
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _is_obvious_noise(news: NewsRaw) -> bool:
    title = news.title or ""
    summary = news.summary or ""
    body = news.content or ""
    text = title + summary + body
    if not text.strip():
        return True
    if any(tok in text for tok in _NOISE_TOKENS):
        return True
    if len(title) < 6 and len(body) < 30:
        return True
    return False


def _is_too_old(news: NewsRaw, cutoff: datetime) -> bool:
    if not news.published_at:
        return True
    return news.published_at < cutoff


def _event_key(news_id: int, schema_version: str) -> str:
    raw = f"{news_id}:{schema_version}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def _news_to_dict(news: NewsRaw) -> dict[str, Any]:
    return {
        "id": news.id,
        "title": news.title,
        "summary": news.summary,
        "content": news.content,
        "url": news.url,
        "source": news.source,
        "source_label": news.source_label,
        "published_at": news.published_at.isoformat() if news.published_at else None,
    }


def select_candidates(
    db,
    *,
    hours_back: int = DEFAULT_HOURS_BACK,
    limit: int = DEFAULT_LIMIT,
) -> list[NewsRaw]:
    """Pick the most promising news articles for LLM extraction.

    Filter chain:
      1. within ``hours_back`` window
      2. non-empty title + body (>= 30 chars combined)
      3. not obvious noise (promo / ad)
      4. ordered by recency, capped at ``limit``
    """
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    with db.session() as s:
        rows = (
            s.query(NewsRaw)
            .filter(NewsRaw.published_at >= cutoff)
            .filter(NewsRaw.keywords_matched != "")
            .order_by(NewsRaw.published_at.desc())
            .limit(limit * 3)  # over-fetch then trim after noise filtering
            .all()
        )
    cleaned: list[NewsRaw] = []
    for row in rows:
        if _is_too_old(row, cutoff):
            continue
        if _is_obvious_noise(row):
            continue
        cleaned.append(row)
        if len(cleaned) >= limit:
            break
    return cleaned


def extract_events(
    *,
    hours_back: int = DEFAULT_HOURS_BACK,
    limit: int = DEFAULT_LIMIT,
    persist: bool = True,
) -> dict[str, Any]:
    """Run end-to-end event extraction.

    Returns a dict with ``events`` (persisted rows), ``candidates`` (input
    count), ``extracted`` (LLM-success count) and ``summary``. When
    ``persist=False`` the DB is not written — useful for unit tests.
    """
    db = get_db()
    candidates = select_candidates(db, hours_back=hours_back, limit=limit)
    candidate_count = len(candidates)
    if not candidates:
        return {
            "events": [],
            "candidates": 0,
            "extracted": 0,
            "skipped": 0,
            "source_breakdown": {"llm": 0, "heuristic": 0},
            "summary": "no candidates within window",
        }

    persisted: list[StorageEvent] = []
    source_breakdown = {"llm": 0, "heuristic": 0}
    skipped = 0

    for news in candidates:
        try:
            payload, source = extract_event_safely(_news_to_dict(news))
            source_breakdown[source] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"extract_event failed for news {news.id}: {exc!r}")
            skipped += 1
            continue

        # Only persist events that pass minimum evidence threshold
        if not payload.get("evidence") or float(payload.get("confidence", 0) or 0) <= 0:
            skipped += 1
            continue

        if not persist:
            persisted.append(_payload_to_dict(news.id, payload, source))
            continue

        with db.tx() as s:
            row = (
                s.query(StorageEvent)
                .filter(StorageEvent.news_id == news.id)
                .filter(StorageEvent.schema_version == EVENT_SCHEMA_VERSION)
                .one_or_none()
            )
            if row is None:
                row = StorageEvent(
                    news_id=news.id,
                    event_key=_event_key(news.id, EVENT_SCHEMA_VERSION),
                    schema_version=EVENT_SCHEMA_VERSION,
                )
                s.add(row)
            _apply_payload(row, payload, source)
            persisted.append(row)

    extracted = len(persisted)
    summary = (
        f"candidates={candidate_count} extracted={extracted} skipped={skipped} "
        f"llm={source_breakdown['llm']} heuristic={source_breakdown['heuristic']}"
    )
    return {
        "events": persisted,
        "candidates": candidate_count,
        "extracted": extracted,
        "skipped": skipped,
        "source_breakdown": source_breakdown,
        "summary": summary,
    }


def _apply_payload(row: StorageEvent, payload: dict[str, Any], source: str) -> None:
    row.title = payload.get("title") or row.title or ""
    row.entities = json.dumps(payload.get("entities") or [], ensure_ascii=False)
    row.products = json.dumps(payload.get("products") or [], ensure_ascii=False)
    row.industry_chain = payload.get("industry_chain") or ""
    row.region = payload.get("region") or ""
    row.event_type = payload.get("event_type") or "other"
    row.supply_direction = payload.get("supply_direction") or "neutral"
    row.demand_direction = payload.get("demand_direction") or "neutral"
    try:
        row.magnitude = float(payload.get("magnitude") or 0.0)
    except (TypeError, ValueError):
        row.magnitude = 0.0
    try:
        row.confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        row.confidence = 0.0
    start_at = payload.get("start_at")
    if isinstance(start_at, str) and start_at:
        try:
            row.start_at = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        except ValueError:
            row.start_at = None
    end_at = payload.get("end_at")
    if isinstance(end_at, str) and end_at:
        try:
            row.end_at = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
        except ValueError:
            row.end_at = None
    row.evidence_json = json.dumps(payload.get("evidence") or [], ensure_ascii=False)
    row.counter_evidence_json = json.dumps(payload.get("counter_evidence") or [], ensure_ascii=False)
    row.payload_json = json.dumps({**payload, "_source": source}, ensure_ascii=False)


def _payload_to_dict(news_id: int, payload: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "news_id": news_id,
        "title": payload.get("title") or "",
        "industry_chain": payload.get("industry_chain") or "",
        "event_type": payload.get("event_type") or "other",
        "supply_direction": payload.get("supply_direction") or "neutral",
        "demand_direction": payload.get("demand_direction") or "neutral",
        "magnitude": float(payload.get("magnitude") or 0.0),
        "confidence": float(payload.get("confidence") or 0.0),
        "source": source,
    }


def assess_quality(result: dict[str, Any]) -> tuple[str, str]:
    """Map an extraction result to (status, quality_status).

    Mirrors the If/Else gate that Dify's WF-02 node uses. Returning
    ``(succeeded, pass)`` means the workflow should continue; ``(degraded, warn)``
    or ``(failed, fail)`` forces the degraded branch.
    """
    candidates = int(result.get("candidates") or 0)
    extracted = int(result.get("extracted") or 0)
    source_breakdown = result.get("source_breakdown") or {}

    if candidates == 0:
        return "degraded", "warn"

    success_rate = extracted / max(candidates, 1)
    if success_rate < MIN_EXTRACT_SUCCESS_RATE:
        return "degraded", "warn"

    # Check if any event has confidence / magnitude worth flagging
    has_signal = False
    for ev in result.get("events") or []:
        payload = getattr(ev, "payload_json", None)
        conf = float(getattr(ev, "confidence", 0.0) or 0.0)
        mag = float(getattr(ev, "magnitude", 0.0) or 0.0)
        if conf >= MIN_CONFIDENCE or mag >= MIN_MAGNITUDE:
            has_signal = True
            break
        if payload:
            try:
                parsed = json.loads(payload)
                if float(parsed.get("confidence", 0) or 0) >= MIN_CONFIDENCE:
                    has_signal = True
                    break
            except (TypeError, ValueError):
                pass

    if extracted == 0:
        return "degraded", "warn"
    # Heuristics are useful for observation, but cannot independently pass a
    # production evidence gate or create a strong recommendation.
    if int(source_breakdown.get("llm") or 0) == 0:
        return "degraded", "warn"
    if not has_signal:
        return "degraded", "warn"
    return "succeeded", "pass"


def run(hours_back: int = DEFAULT_HOURS_BACK, limit: int = DEFAULT_LIMIT) -> str:
    """Scheduler-style entry. Returns a single-line summary string."""
    result = extract_events(hours_back=hours_back, limit=limit, persist=True)
    return result["summary"]


def run_persist(hours_back: int = DEFAULT_HOURS_BACK, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Dify-friendly entry that returns both result and quality assessment."""
    result = extract_events(hours_back=hours_back, limit=limit, persist=True)
    status, quality = assess_quality(result)
    return {"status": status, "quality_status": quality, **result}
