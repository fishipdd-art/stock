"""
Persist signal-hit records from news-matching results.

After each news collection run *build_matches()* produces ephemeral
*SignalMatch* objects.  This module persists them as *SignalHit* rows so
the web UI can show recent signal activity.

Usage::

    from processor.signal_hits import persist_signal_hits
    persist_signal_hits(db)           # today's news
    persist_signal_hits(db, hours=72) # wider window for backfill
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from processor.matcher import build_matches
from storage.database import Database
from storage.models import KnowledgeSignal, NewsRaw, SearchTerm, SignalHit
from processor.matcher import MATCHER_VERSION


def _match_key(news_id: int, signal_id: int | None, term: str) -> str:
    target = f"signal:{signal_id}" if signal_id is not None else f"term:{(term or '').strip().lower()}"
    return hashlib.sha256(f"{MATCHER_VERSION}|{news_id}|{target}".encode()).hexdigest()


def persist_signal_hits(
    db: Database,
    hours: int = 48,
    prune_older_than_days: int = 14,
) -> int:
    """Run news-signal matching and persist results as SignalHit rows.

    Returns the number of new hits persisted.
    """
    since = datetime.utcnow() - timedelta(hours=hours)

    with db.session() as s:
        matches = build_matches(s, since=since)
    if not matches:
        return 0

    # Build lookup: signal_key -> signal_id
    with db.session() as s:
        signals = {
            sig.signal_key: sig.id
            for sig in s.execute(
                select(KnowledgeSignal.id, KnowledgeSignal.signal_key)
            ).all()
        }
        # Build lookup: news_id -> source
        news_rows = s.execute(
            select(NewsRaw.id, NewsRaw.source)
        ).all()
        news_source = {n.id: n.source for n in news_rows}

    prepared = []
    for m in matches:
        sig_id = signals.get(m.matched_signal_key) if m.matched_signal_key else None
        prepared.append((m, sig_id, _match_key(m.news_id, sig_id, m.matched_term or "")))

    n_persisted = 0
    with db.tx() as s:
        keys = [key for _, _, key in prepared]
        existing = {
            row.match_key: row
            for row in s.query(SignalHit).filter(SignalHit.match_key.in_(keys)).all()
        }
        for m, sig_id, key in prepared:
            current = existing.get(key)
            if current is not None:
                current.hit_at = datetime.utcnow()
                current.match_score = m.match_score
                current.final_score = m.final_score
                current.news_title = m.news_title
                current.news_url = m.news_url
                current.news_source = news_source.get(m.news_id, "")
                continue
            hit = SignalHit(
                signal_id=sig_id,
                term=m.matched_term or "",
                news_id=m.news_id,
                news_title=m.news_title,
                news_url=m.news_url,
                news_source=news_source.get(m.news_id, ""),
                hit_at=datetime.utcnow(),
                match_score=m.match_score,
                final_score=m.final_score,
                match_key=key,
            )
            s.add(hit)
            n_persisted += 1
        s.flush()

    # Prune old hits
    cutoff = datetime.utcnow() - timedelta(days=prune_older_than_days)
    with db.tx() as s:
        deleted = s.execute(
            delete(SignalHit).where(SignalHit.hit_at < cutoff)
        ).rowcount

    logger.info(
        f"persist_signal_hits: {n_persisted} new, {deleted} pruned "
        f"(>{prune_older_than_days}d)"
    )
    return n_persisted


def deduplicate_signal_hits(db: Database) -> dict[str, int]:
    """Collapse historical duplicate hits and assign matcher-v2 keys."""
    removed = 0
    keyed = 0
    with db.tx() as s:
        rows = s.query(SignalHit).order_by(SignalHit.id.desc()).all()
        keep: dict[str, SignalHit] = {}
        for row in rows:
            key = _match_key(row.news_id, row.signal_id, row.term)
            if key in keep:
                s.delete(row)
                removed += 1
                continue
            row.match_key = key
            keep[key] = row
            keyed += 1
    return {"removed": removed, "keyed": keyed}


def rebuild_signal_hits(db: Database, hours: int = 72) -> dict[str, int]:
    """Rebuild the bounded, fully derived signal-hit cache."""
    with db.tx() as s:
        removed = int(s.query(SignalHit).delete(synchronize_session=False) or 0)
    inserted = persist_signal_hits(db, hours=hours)
    return {"removed": removed, "inserted": inserted}
