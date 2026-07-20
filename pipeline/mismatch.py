"""Pipeline: WF-03 — detect supply-demand mismatches and propagate through
the supply-chain knowledge graph.

Inputs
------
``StorageEvent`` rows produced by WF-02 (``pipeline.events_extract``),
``FuturesPrice`` and ``StockQuote`` for price/inventory validation, and the
``KnowledgeCategory`` -> ``SearchTerm`` -> ``AStock`` chain for beneficiary
discovery.

Output
------
``MismatchResult`` rows: per ``(industry_chain, event_type)`` bucket,
direction (tight / loose / mixed), the six-document weight breakdown, and the
beneficiary / at-risk lists resolved through the supply-chain graph.

Quality gate (mirrors Dify's If/Else node):
  - mismatch count >= 1
  - at least one result with total_score >= 60 (out of 100)
  - at least one independent source per top bucket

A degraded result produces ``warn`` (no buy recommendation in Dify); an
exception or zero results produces ``fail``.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable

from loguru import logger

from storage import get_db
from storage.models import (
    FuturesPrice,
    KnowledgeCategory,
    KnowledgeSignal,
    MismatchResult,
    NewsRaw,
    SearchTerm,
    SignalStock,
    StorageEvent,
    StockQuote,
)


# Score weights (document §5)
WEIGHTS = {
    "evidence": 0.20,
    "multi_source": 0.15,
    "supply_demand": 0.20,
    "price_inventory": 0.15,
    "graph": 0.15,
    "freshness": 0.10,
    "tradability": 0.05,
}

# Quality thresholds (Dify branch logic)
MIN_MISMATCHES = 1
MIN_TOTAL_SCORE = 60.0
MIN_SOURCES_PER_BUCKET = 2

# Freshness window — events older than this decay linearly to zero
FRESHNESS_HOURS = 48
# Price-move confirmation threshold (percent)
PRICE_MOVE_THRESHOLD = 1.5


def _bucket_key(industry_chain: str, event_type: str) -> str:
    raw = f"{industry_chain}::{event_type}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


def _load_recent_events(db, hours_back: int) -> list[StorageEvent]:
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    with db.session() as s:
        return (
            s.query(StorageEvent)
            .filter(StorageEvent.created_at >= cutoff)
            .filter(StorageEvent.confidence > 0)
            .all()
        )


def _load_recent_futures(db, days_back: int = 7) -> dict[str, list[FuturesPrice]]:
    cutoff = datetime.utcnow().date() - timedelta(days=days_back)
    out: dict[str, list[FuturesPrice]] = defaultdict(list)
    with db.session() as s:
        rows = (
            s.query(FuturesPrice)
            .filter(FuturesPrice.trade_date >= cutoff)
            .all()
        )
    for row in rows:
        out[row.symbol].append(row)
    for sym in out:
        out[sym].sort(key=lambda r: r.trade_date)
    return out


def _industry_to_symbol_map() -> dict[str, list[tuple[str, str]]]:
    """Map industry_chain → list of (code, name) by scanning search terms."""
    db = get_db()
    mapping: dict[str, set[tuple[str, str]]] = defaultdict(set)
    with db.session() as s:
        terms = s.query(SearchTerm).all()
    for term in terms:
        text = f"{term.a_share_map or ''} {term.term or ''}"
        cat_name = ""
        if term.category:
            cat_name = term.category.name
        if not cat_name:
            continue
        for code, name in _parse_a_share_codes(text):
            mapping[cat_name].add((code, name))
    return {k: sorted(v) for k, v in mapping.items()}


_CODE_RE = __import__("re").compile(r"([一-龥A-Za-z·\.\-（）()0-9]+?)\((\d{6})\)")


def _parse_a_share_codes(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _CODE_RE.finditer(text or ""):
        name, code = m.group(1).strip(), m.group(2)
        if code in seen:
            continue
        seen.add(code)
        out.append((code, name))
    return out


def _supply_demand_score(events: list[StorageEvent]) -> float:
    """Higher score when magnitudes cluster around the same direction."""
    if not events:
        return 0.0
    directed = sum(1 for e in events if e.event_type not in ("other",))
    avg_magnitude = sum(e.magnitude for e in events) / max(len(events), 1)
    avg_confidence = sum(e.confidence for e in events) / max(len(events), 1)
    base = (directed / len(events)) * 60.0
    base += min(40.0, avg_magnitude * 6.0)
    base *= (0.5 + 0.5 * avg_confidence)
    return min(100.0, base)


def _evidence_score(events: list[StorageEvent]) -> float:
    """Reward events with rich evidence lists and rich counter-evidence."""
    if not events:
        return 0.0
    total = 0.0
    for ev in events:
        try:
            evidence = json.loads(ev.evidence_json or "[]")
            counter = json.loads(ev.counter_evidence_json or "[]")
        except (TypeError, ValueError):
            evidence, counter = [], []
        e_count = len(evidence)
        c_count = len(counter)
        ev_score = min(40.0, e_count * 10.0)
        # counter-evidence is a *bonus* here (rigour), capped at +30
        ev_score += min(30.0, c_count * 10.0)
        # diversity: distinct sources
        sources = {(item.get("source") or "") for item in evidence if isinstance(item, dict)}
        ev_score += min(30.0, len(sources) * 10.0)
        total += ev_score
    return min(100.0, total / max(len(events), 1))


def _multi_source_score(events: list[StorageEvent]) -> float:
    if not events:
        return 0.0
    # Each StorageEvent corresponds to a unique news source via NewsRaw
    db = get_db()
    sources: set[str] = set()
    with db.session() as s:
        news_ids = [e.news_id for e in events]
        if not news_ids:
            return 0.0
        rows = s.query(NewsRaw.id, NewsRaw.source, NewsRaw.source_label).filter(
            NewsRaw.id.in_(news_ids)
        ).all()
    for _, src, label in rows:
        key = (src or label or "unknown").strip()
        if key:
            sources.add(key)
    # 2 sources = 60, 3 = 80, 4+ = 100
    if not sources:
        return 0.0
    return min(100.0, 20.0 + max(1, len(sources)) * 20.0)


def _price_inventory_score(
    industry_chain: str,
    direction: str,
    futures: dict[str, list[FuturesPrice]],
) -> float:
    """Crude proxy: futures with related symbols moving in the predicted
    direction within ±2 trading days add to confidence.
    """
    if not futures:
        return 40.0  # neutral when no data
    # Map industry_chain -> likely futures symbols (best-effort)
    SYMBOL_HINTS = {
        "有色金属": ["CU", "AL", "ZN", "NI"],
        "钢铁": ["RB", "I", "J", "JM"],
        "原油石化": ["SC", "FU", "LU", "BU"],
        "锂电": ["LC"],
        "化工": ["TA", "EG", "MA", "PP"],
        "光伏": ["SI"],
    }
    hints = SYMBOL_HINTS.get(industry_chain, [])
    relevant = []
    for hint in hints:
        for sym, rows in futures.items():
            if sym.startswith(hint):
                relevant.extend(rows)
    if not relevant:
        return 40.0

    # Compare last close vs 3-day-ago close
    by_sym: dict[str, list[FuturesPrice]] = defaultdict(list)
    for r in relevant:
        by_sym[r.symbol].append(r)
    confirmations = 0
    total = 0
    for rows in by_sym.values():
        rows = sorted(rows, key=lambda r: r.trade_date)
        if len(rows) < 2:
            continue
        pct = (rows[-1].close - rows[-3].close) / rows[-3].close * 100.0 if len(rows) >= 3 and rows[-3].close else 0.0
        if abs(pct) < PRICE_MOVE_THRESHOLD:
            continue
        total += 1
        if direction == "tight" and pct > 0:
            confirmations += 1
        elif direction == "loose" and pct < 0:
            confirmations += 1
    if total == 0:
        return 40.0
    rate = confirmations / total
    return min(100.0, 40.0 + rate * 60.0)


def _graph_score(
    industry_chain: str,
    related_stocks: list[tuple[str, str]],
) -> float:
    """Reward industries with broad stock coverage in the supply-chain graph."""
    if not related_stocks:
        return 30.0
    # 5 stocks = 60, 10 = 80, 20+ = 100
    return min(100.0, 30.0 + max(1, len(related_stocks)) * 4.0)


def _freshness_score(events: list[StorageEvent]) -> float:
    if not events:
        return 0.0
    now = datetime.utcnow()
    scores = []
    for ev in events:
        ts = ev.created_at or ev.start_at
        if not ts:
            scores.append(20.0)
            continue
        age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
        # linear decay: <6h -> 100, 48h -> 0
        scores.append(max(0.0, 100.0 * (1.0 - age_hours / FRESHNESS_HOURS)))
    return min(100.0, sum(scores) / max(len(scores), 1))


def _tradability_score(events: list[StorageEvent]) -> float:
    """Heuristic proxy: long-running events (multiple news articles) are more
    tradable than one-off noise.
    """
    if not events:
        return 0.0
    if len(events) >= 5:
        return 90.0
    if len(events) >= 3:
        return 70.0
    if len(events) >= 2:
        return 55.0
    return 35.0


def _aggregate_direction(events: list[StorageEvent]) -> str:
    """Return tight / loose / mixed based on supply + demand signals."""
    tight = sum(
        1 for e in events
        if e.supply_direction == "tight" or e.demand_direction == "up"
    )
    loose = sum(
        1 for e in events
        if e.supply_direction == "loose" or e.demand_direction == "down"
    )
    if tight > loose * 1.3:
        return "tight"
    if loose > tight * 1.3:
        return "loose"
    return "mixed"


def _propagate_path(
    industry_chain: str,
    direction: str,
    related_stocks: list[tuple[str, str]],
) -> list[dict[str, str]]:
    """Build a chain-of-thought path: trigger → price → stock."""
    if not related_stocks:
        return []
    sample = related_stocks[:5]
    path = [
        {"from": f"{industry_chain}事件", "to": "上游原料/价格", "kind": "trigger"},
        {"from": "上游原料/价格", "to": "中游加工/产能", "kind": "spread"},
        {"from": "中游加工/产能", "to": ", ".join(f"{n}({c})" for c, n in sample), "kind": "exposure"},
    ]
    if direction == "tight":
        path.append({"from": "exposure", "to": "价格上行/订单饱满", "kind": "outcome"})
    elif direction == "loose":
        path.append({"from": "exposure", "to": "价格下行/竞争加剧", "kind": "outcome"})
    else:
        path.append({"from": "exposure", "to": "格局未定", "kind": "outcome"})
    return path


def _resolve_industry(industry_chain: str) -> list[tuple[str, str]]:
    """Use both the knowledge graph (SearchTerm) and signal-stock mappings."""
    db = get_db()
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    # 1) SearchTerm.a_share_map (best signal — curated by humans)
    with db.session() as s:
        terms = (
            s.query(SearchTerm)
            .join(KnowledgeCategory, KnowledgeCategory.id == SearchTerm.category_id)
            .filter(KnowledgeCategory.name == industry_chain)
            .all()
        )
    for term in terms:
        for code, name in _parse_a_share_codes(term.a_share_map or ""):
            if code in seen:
                continue
            seen.add(code)
            out.append((code, name))

    # 2) KnowledgeSignal.stocks (event→stock) for additional coverage
    with db.session() as s:
        signals = (
            s.query(KnowledgeSignal)
            .filter(KnowledgeSignal.industry_label == industry_chain)
            .all()
            if hasattr(KnowledgeSignal, "industry_label") else []
        )
        if not signals:
            signals = (
                s.query(KnowledgeSignal)
                .filter(KnowledgeSignal.direction.like(f"%{industry_chain}%"))
                .all()
            )
    for sig in signals:
        with db.session() as s:
            ss = s.query(SignalStock).filter(SignalStock.signal_id == sig.id).all()
        for stock in ss:
            if stock.stock_code in seen:
                continue
            seen.add(stock.stock_code)
            out.append((stock.stock_code, ""))

    # Resolve names where missing
    if out:
        codes = [c for c, _ in out]
        with db.session() as s:
            from storage.models import AStock
            rows = s.query(AStock.code, AStock.name).filter(AStock.code.in_(codes)).all()
        name_map = {code: name for code, name in rows}
        out = [(c, name_map.get(c, n) or n) for c, n in out]

    return out


def _summarize(
    industry_chain: str,
    event_type: str,
    direction: str,
    n_events: int,
    n_sources: int,
    score: float,
    related: list[tuple[str, str]],
) -> str:
    rel = ", ".join(f"{n}({c})" for c, n in related[:3]) if related else "无直接映射"
    return (
        f"{industry_chain}/{event_type} 方向={direction}; "
        f"事件={n_events} 来源={n_sources} 综合分={score:.1f}; "
        f"关联: {rel}"
    )


def _total_score(parts: dict[str, float]) -> float:
    """Weighted total on the same 0-100 scale as its components."""
    return round(sum(parts[k] * WEIGHTS[k] for k in WEIGHTS), 2)


def detect(
    *,
    hours_back: int = 48,
    limit: int = 80,
    persist: bool = True,
) -> dict[str, Any]:
    """Detect mismatches from StorageEvent rows."""
    db = get_db()
    events = _load_recent_events(db, hours_back=hours_back)
    if not events:
        return {"mismatches": [], "summary": "no events in window"}

    # Bucket by (industry_chain, event_type)
    buckets: dict[tuple[str, str], list[StorageEvent]] = defaultdict(list)
    for ev in events:
        if not ev.industry_chain:
            continue
        buckets[(ev.industry_chain, ev.event_type)].append(ev)
    if limit > 0:
        # Keep top buckets by event count
        buckets = dict(sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:limit])

    futures = _load_recent_futures(db)

    results: list[MismatchResult] = []
    for (industry_chain, event_type), bucket_events in buckets.items():
        direction = _aggregate_direction(bucket_events)
        related = _resolve_industry(industry_chain)

        parts = {
            "evidence": _evidence_score(bucket_events),
            "multi_source": _multi_source_score(bucket_events),
            "supply_demand": _supply_demand_score(bucket_events),
            "price_inventory": _price_inventory_score(industry_chain, direction, futures),
            "graph": _graph_score(industry_chain, related),
            "freshness": _freshness_score(bucket_events),
            "tradability": _tradability_score(bucket_events),
        }
        score = _total_score(parts)
        # Multi-source independent news id count (per §quality bar)
        news_ids = {e.news_id for e in bucket_events}
        sources = set()
        if news_ids:
            with db.session() as s:
                rows = s.query(NewsRaw.id, NewsRaw.source, NewsRaw.source_label).filter(
                    NewsRaw.id.in_(list(news_ids))
                ).all()
            for _, src, label in rows:
                key = (src or label or "unknown").strip()
                if key:
                    sources.add(key)
        n_sources = len(sources)

        path = _propagate_path(industry_chain, direction, related)
        summary = _summarize(
            industry_chain,
            event_type,
            direction,
            len(bucket_events),
            n_sources,
            score,
            related,
        )

        row = MismatchResult(
            result_key=_bucket_key(industry_chain, event_type),
            industry_chain=industry_chain,
            direction=direction,
            total_score=score,
            evidence_score=parts["evidence"],
            multi_source_score=parts["multi_source"],
            supply_demand_score=parts["supply_demand"],
            price_inventory_score=parts["price_inventory"],
            graph_score=parts["graph"],
            freshness_score=parts["freshness"],
            tradability_score=parts["tradability"],
            n_events=len(bucket_events),
            n_sources=n_sources,
            path_json=json.dumps(path, ensure_ascii=False),
            beneficiaries_json=json.dumps(
                [f"{n}({c})" for c, n in related[:10]], ensure_ascii=False
            ),
            at_risk_json=json.dumps(
                [f"{n}({c})" for c, n in related[10:20]], ensure_ascii=False
            ),
            summary=summary,
            trade_date=datetime.utcnow().date(),
        )

        if persist:
            with db.tx() as s:
                existing = (
                    s.query(MismatchResult)
                    .filter(MismatchResult.result_key == row.result_key)
                    .one_or_none()
                )
                if existing is None:
                    s.add(row)
                    # Make the just-added key visible to the next bucket in
                    # this transaction; duplicate event buckets otherwise
                    # both appear new until commit and violate UNIQUE.
                    s.flush()
                else:
                    row = existing
                    # result_key is the table's actual UNIQUE key.  Re-running
                    # the same day's mismatch must update the existing bucket
                    # instead of attempting a duplicate insert.
                    row.trade_date = datetime.utcnow().date()
                    row.total_score = score
                    row.evidence_score = parts["evidence"]
                    row.multi_source_score = parts["multi_source"]
                    row.supply_demand_score = parts["supply_demand"]
                    row.price_inventory_score = parts["price_inventory"]
                    row.graph_score = parts["graph"]
                    row.freshness_score = parts["freshness"]
                    row.tradability_score = parts["tradability"]
                    row.n_events = len(bucket_events)
                    row.n_sources = n_sources
                    row.path_json = json.dumps(path, ensure_ascii=False)
                    row.beneficiaries_json = json.dumps(
                        [f"{n}({c})" for c, n in related[:10]], ensure_ascii=False
                    )
                    row.at_risk_json = json.dumps(
                        [f"{n}({c})" for c, n in related[10:20]], ensure_ascii=False
                    )
                    row.summary = summary
                    row.direction = direction
        results.append(row)

    results.sort(key=lambda r: r.total_score, reverse=True)

    return {
        "mismatches": results,
        "summary": (
            f"buckets={len(buckets)} mismatches={len(results)} "
            f"top={results[0].industry_chain}/{results[0].direction} "
            f"score={results[0].total_score:.1f}"
            if results else "no mismatches"
        ),
    }


def assess_quality(result: dict[str, Any]) -> tuple[str, str]:
    mismatches = list(result.get("mismatches") or [])
    if not mismatches:
        return "degraded", "warn"
    top_score = max(m.total_score for m in mismatches)
    if top_score < MIN_TOTAL_SCORE:
        return "degraded", "warn"
    # require at least one bucket with multiple sources for "pass"
    multi_source = any(m.n_sources >= MIN_SOURCES_PER_BUCKET for m in mismatches)
    if not multi_source:
        return "degraded", "warn"
    return "succeeded", "pass"


def run(hours_back: int = 48, limit: int = 80) -> str:
    result = detect(hours_back=hours_back, limit=limit, persist=True)
    return result["summary"]


def run_persist(hours_back: int = 48, limit: int = 80) -> dict[str, Any]:
    result = detect(hours_back=hours_back, limit=limit, persist=True)
    status, quality = assess_quality(result)
    return {"status": status, "quality_status": quality, **result}
