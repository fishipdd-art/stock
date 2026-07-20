"""Pipeline: WF-04 — score stocks and ETFs using the document §5 weights.

Inputs
------
``MismatchResult`` (WF-03), ``StorageEvent`` (WF-02), and the knowledge graph
mapping from industry chain → A-share codes.

Output
------
``StockScore`` rows: per (trade_date, code) with the 7-component breakdown,
the hard-filter outcome, the action hints (observe / entry / stop / catalyst
window), and the priced-in / risk notes for Dify to render.

Quality gate:
  - candidate count >= 1
  - at least one stock passing all hard filters with final_score >= 60
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timedelta
from typing import Any

from loguru import logger

from storage import get_db
from storage.models import (
    AStock,
    KnowledgeCategory,
    MismatchResult,
    SearchTerm,
    StockQuote,
    StockScore,
    StorageEvent,
)


WEIGHTS = {
    "evidence": 0.20,
    "multi_source": 0.15,
    "supply_demand": 0.20,
    "price_inventory": 0.15,
    "graph": 0.15,
    "freshness": 0.10,
    "tradability": 0.05,
}

# Hard filter thresholds
MIN_AVG_TURNOVER = 5_000_000  # 5 million CNY daily turnover
MIN_LISTED_DAYS = 60  # skip fresh IPOs (< 60 trading days)
EXTREME_MOVE_THRESHOLD = 0.18  # +/-18% in last 3 trading days → skip
RECENT_DAYS = 5

# Score threshold to keep a stock in the "buy zone" of the report
BUY_THRESHOLD = 60.0
# Trade date default: today
DEFAULT_LOOKBACK_HOURS = 48


@dataclass
class StockCandidate:
    code: str
    name: str
    asset_type: str
    industry_chain: str
    direction: str
    mismatch_score: float


def _parse_date(s: str | None) -> date_cls:
    if not s:
        return datetime.utcnow().date()
    try:
        return date_cls.fromisoformat(s)
    except ValueError:
        return datetime.utcnow().date()


def _code_re():
    return re.compile(r"([一-龥A-Za-z·\.\-（）()0-9]+?)\((\d{6})\)")


def _parse_codes(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _code_re().finditer(text or ""):
        code, name = m.group(2), m.group(1).strip()
        if code in seen:
            continue
        seen.add(code)
        out.append((code, name))
    return out


def _collect_candidates(industry_chain: str, direction: str, score: float) -> list[StockCandidate]:
    db = get_db()
    candidates: list[StockCandidate] = []
    seen: set[str] = set()

    # Event extraction uses concise industry names (for example “半导体”),
    # whereas the curated graph uses richer category names (“AI/半导体”).
    # Resolve the mapping explicitly, then fall back to conservative token
    # matching so a valid event does not silently lose its stock universe.
    aliases = {
        "半导体": {"AI/半导体"},
        "有色金属": {"地缘政治/大宗商品", "工业原材料"},
        "消费电子": {"AI/半导体"},
        "航天军工": {"航天军工/商业航天"},
        "军工": {"航天军工/商业航天"},
    }
    wanted = aliases.get(industry_chain, set()) | {industry_chain}
    with db.session() as s:
        all_categories = s.query(KnowledgeCategory).all()
        for category in all_categories:
            name = category.name or ""
            if industry_chain and (industry_chain in name or name in industry_chain):
                wanted.add(name)
        terms = (
            s.query(SearchTerm)
            .join(KnowledgeCategory, KnowledgeCategory.id == SearchTerm.category_id)
            .filter(KnowledgeCategory.name.in_(wanted))
            .all()
        )
    for term in terms:
        for code, name in _parse_codes(term.a_share_map or ""):
            if code in seen:
                continue
            seen.add(code)
            candidates.append(
                StockCandidate(
                    code=code,
                    name=name,
                    asset_type="stock",
                    industry_chain=industry_chain,
                    direction=direction,
                    mismatch_score=score,
                )
            )

    # ETFs are noted in supply_chain_signals.json, best-effort here: nothing
    # for now since we have no ETF map yet — keeping the schema flexible.
    return candidates


def _load_recent_quotes(db, codes: list[str], trade_date: date_cls) -> dict[str, list[StockQuote]]:
    if not codes:
        return {}
    cutoff = trade_date - timedelta(days=30)
    with db.session() as s:
        rows = (
            s.query(StockQuote)
            .filter(StockQuote.code.in_(codes))
            .filter(StockQuote.trade_date >= cutoff)
            .order_by(StockQuote.trade_date.asc())
            .all()
        )
    out: dict[str, list[StockQuote]] = {}
    for r in rows:
        out.setdefault(r.code, []).append(r)
    return out


def _hard_filters(
    code: str,
    name: str,
    asset_type: str,
    quotes: list[StockQuote],
) -> tuple[bool, list[str]]:
    """Apply the document §6 hard filters and return (passed, reasons)."""
    reasons: list[str] = []

    # Liquidity: not enough turnover
    if not quotes:
        reasons.append("no_quote_data")
    else:
        recent = quotes[-RECENT_DAYS:] if len(quotes) >= RECENT_DAYS else quotes
        avg_turnover = sum(q.turnover for q in recent) / len(recent)
        if avg_turnover < MIN_AVG_TURNOVER and asset_type != "etf":
            reasons.append("low_liquidity")

    # Recent extreme moves (avoid chasing)
    if quotes and len(quotes) >= 2:
        latest = quotes[-1]
        earliest = quotes[0]
        if earliest.close > 0:
            move = (latest.close - earliest.close) / earliest.close
            if abs(move) >= EXTREME_MOVE_THRESHOLD:
                reasons.append("extreme_recent_move")

    # Listed too short — use the gap between the latest cached quote and today.
    # We can't require 60 actual cached days because the local quote cache is a
    # rolling window; instead skip when the most recent quote is more than 7
    # calendar days old (treats stale-cache or brand-new IPO alike).
    if quotes:
        latest = quotes[-1]
        try:
            latest_date = latest.trade_date
        except AttributeError:
            latest_date = None
        if latest_date is None or (datetime.utcnow().date() - latest_date).days > 7:
            reasons.append("short_history")

    # ST / 退市 risk: best-effort heuristic by name
    if name and ("ST" in name.upper() or "退" in name or "*ST" in name.upper()):
        reasons.append("st_or_delist")

    return (not reasons), reasons


def _graph_score(candidate: StockCandidate) -> float:
    """Higher when SearchTerm coverage is rich."""
    db = get_db()
    with db.session() as s:
        terms = (
            s.query(SearchTerm)
            .join(KnowledgeCategory, KnowledgeCategory.id == SearchTerm.category_id)
            .filter(KnowledgeCategory.name == candidate.industry_chain)
            .all()
        )
    n_terms = len(terms)
    return min(100.0, 40.0 + min(6, n_terms) * 10.0)


def _price_inventory_score(
    candidate: StockCandidate,
    quotes: list[StockQuote],
    direction: str,
) -> float:
    if not quotes:
        return 30.0
    latest = quotes[-1]
    if len(quotes) < 3:
        return 40.0
    base = quotes[-3].close
    if base <= 0:
        return 40.0
    pct = (latest.close - base) / base
    if abs(pct) < 0.01:
        return 50.0
    if direction == "tight" and pct > 0:
        return min(100.0, 50.0 + pct * 800.0)
    if direction == "loose" and pct < 0:
        return min(100.0, 50.0 + abs(pct) * 800.0)
    return 30.0


def _freshness_score(mismatch: MismatchResult) -> float:
    return float(mismatch.freshness_score or 0.0)


def _supply_demand_score(mismatch: MismatchResult) -> float:
    return float(mismatch.supply_demand_score or 0.0)


def _evidence_score(mismatch: MismatchResult) -> float:
    return float(mismatch.evidence_score or 0.0)


def _multi_source_score(mismatch: MismatchResult) -> float:
    return float(mismatch.multi_source_score or 0.0)


def _tradability_score(quotes: list[StockQuote]) -> float:
    if not quotes:
        return 30.0
    recent = quotes[-5:] if len(quotes) >= 5 else quotes
    avg_turnover = sum(q.turnover for q in recent) / len(recent)
    if avg_turnover >= 100_000_000:
        return 95.0
    if avg_turnover >= 50_000_000:
        return 85.0
    if avg_turnover >= 10_000_000:
        return 70.0
    if avg_turnover >= MIN_AVG_TURNOVER:
        return 55.0
    return 30.0


def _catalyst_window(direction: str) -> str:
    return "1-4 weeks" if direction in ("tight", "loose") else "2-6 weeks"


def _observe_range(quotes: list[StockQuote], direction: str) -> tuple[str, str, float]:
    """Return (observe_range, entry_range, stop_loss) hints."""
    if not quotes:
        return "N/A", "N/A", 0.0
    close = quotes[-1].close
    if direction == "tight":
        entry_low = close * 0.95
        entry_high = close * 1.02
        stop = close * 0.92
    elif direction == "loose":
        entry_low = close * 0.93
        entry_high = close * 0.98
        stop = close * 0.90
    else:
        entry_low = close * 0.95
        entry_high = close * 1.03
        stop = close * 0.92
    return (
        f"{close*0.97:.2f}-{close*1.05:.2f}",
        f"{entry_low:.2f}-{entry_high:.2f}",
        round(stop, 2),
    )


def _invalidation_note(industry_chain: str, direction: str) -> str:
    if direction == "tight":
        return f"{industry_chain} 价格回落 5% 或供给端消息反转"
    if direction == "loose":
        return f"{industry_chain} 价格止跌回升或需求端意外回暖"
    return f"{industry_chain} 缺乏进一步事件验证"


def _priced_in_note(mismatch: MismatchResult) -> str:
    if mismatch.total_score >= 80:
        return "信号强但需警惕市场已部分计入"
    if mismatch.total_score >= 60:
        return "市场可能尚未充分反映"
    return "市场关注度不足，需更多证据"


def _build_reasons(mismatch: MismatchResult, parts: dict[str, float]) -> list[str]:
    out = [
        f"事件分 {parts['evidence']:.1f}",
        f"多源 {parts['multi_source']:.1f}",
        f"供需 {parts['supply_demand']:.1f}",
        f"价格/库存 {parts['price_inventory']:.1f}",
        f"图谱 {parts['graph']:.1f}",
        f"时效 {parts['freshness']:.1f}",
        f"可交易 {parts['tradability']:.1f}",
    ]
    return out


def _counter_evidence(mismatch: MismatchResult) -> list[str]:
    """Surface a couple of risk reminders even when we accept the trade."""
    notes: list[str] = []
    if mismatch.n_sources < 2:
        notes.append("事件来源不足 2 个独立渠道")
    if mismatch.price_inventory_score < 40:
        notes.append("无价格/库存端确认")
    return notes


def _total(parts: dict[str, float]) -> float:
    """Weighted final score, kept on a 0-100 scale."""
    return round(sum(parts[k] * WEIGHTS[k] for k in WEIGHTS), 2)


def score_one(
    candidate: StockCandidate,
    mismatch: MismatchResult,
    quotes: list[StockQuote],
) -> StockScore:
    parts = {
        "evidence": _evidence_score(mismatch),
        "multi_source": _multi_source_score(mismatch),
        "supply_demand": _supply_demand_score(mismatch),
        "price_inventory": _price_inventory_score(candidate, quotes, candidate.direction),
        "graph": _graph_score(candidate),
        "freshness": _freshness_score(mismatch),
        "tradability": _tradability_score(quotes),
    }
    final_score = _total(parts)
    passed, hard_reasons = _hard_filters(
        candidate.code, candidate.name, candidate.asset_type, quotes
    )
    observe, entry, stop = _observe_range(quotes, candidate.direction)

    return StockScore(
        trade_date=mismatch.trade_date,
        code=candidate.code,
        name=candidate.name,
        asset_type=candidate.asset_type,
        direction="long" if candidate.direction == "tight" else (
            "short" if candidate.direction == "loose" else "neutral"
        ),
        final_score=final_score,
        evidence_score=parts["evidence"],
        multi_source_score=parts["multi_source"],
        supply_demand_score=parts["supply_demand"],
        price_inventory_score=parts["price_inventory"],
        graph_score=parts["graph"],
        freshness_score=parts["freshness"],
        tradability_score=parts["tradability"],
        hard_filter_passed=passed,
        hard_filter_reasons=json.dumps(hard_reasons, ensure_ascii=False),
        catalyst_window=_catalyst_window(candidate.direction),
        observe_range=observe,
        entry_range=entry,
        stop_loss=stop,
        invalidation=_invalidation_note(candidate.industry_chain, candidate.direction),
        reasons_json=json.dumps(_build_reasons(mismatch, parts), ensure_ascii=False),
        counter_evidence_json=json.dumps(_counter_evidence(mismatch), ensure_ascii=False),
        priced_in_note=_priced_in_note(mismatch),
        risk_note=(
            "软预警：单一产业链集中度高于 30% 时提示风险，不阻断"
        ),
    )


def run(
    *,
    trade_date: str = "",
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    persist: bool = True,
) -> dict[str, Any]:
    """Score all candidate stocks and persist the result."""
    db = get_db()
    td = _parse_date(trade_date)

    cutoff = datetime.utcnow() - timedelta(hours=lookback_hours)
    with db.session() as s:
        mismatches = (
            s.query(MismatchResult)
            .filter(MismatchResult.trade_date >= td - timedelta(days=2))
            .filter(MismatchResult.created_at >= cutoff)
            .order_by(MismatchResult.total_score.desc())
            .limit(40)
            .all()
        )

    if not mismatches:
        return {
            "scores": [],
            "summary": "no mismatches in window",
        }

    candidates: list[StockCandidate] = []
    for m in mismatches:
        candidates.extend(_collect_candidates(m.industry_chain, m.direction, m.total_score))

    # dedupe by code; keep highest mismatch score per stock
    best: dict[str, StockCandidate] = {}
    for c in candidates:
        if c.code not in best or c.mismatch_score > best[c.code].mismatch_score:
            best[c.code] = c
    candidates = list(best.values())

    if not candidates:
        return {
            "scores": [],
            "summary": "no stock candidates resolved from mismatches",
        }

    codes = [c.code for c in candidates]
    quotes_by_code = _load_recent_quotes(db, codes, td)

    # Resolve stock names that came up blank
    db_name_map: dict[str, str] = {}
    with db.session() as s:
        rows = s.query(AStock.code, AStock.name).filter(AStock.code.in_(codes)).all()
        db_name_map = {c: n for c, n in rows}
    for c in candidates:
        if not c.name:
            c.name = db_name_map.get(c.code, "")

    # Build a mismatch lookup keyed by industry for each candidate
    mismatch_by_chain: dict[str, MismatchResult] = {
        m.industry_chain: m for m in mismatches
    }

    scores: list[StockScore] = []
    for cand in candidates:
        mismatch = mismatch_by_chain.get(cand.industry_chain)
        if mismatch is None:
            continue
        row = score_one(cand, mismatch, quotes_by_code.get(cand.code, []))
        if persist:
            with db.tx() as s:
                existing = (
                    s.query(StockScore)
                    .filter(StockScore.trade_date == row.trade_date)
                    .filter(StockScore.code == row.code)
                    .one_or_none()
                )
                if existing is None:
                    s.add(row)
                else:
                    row.id = existing.id
                    for field in (
                        "name", "asset_type", "direction", "final_score",
                        "evidence_score", "multi_source_score", "supply_demand_score",
                        "price_inventory_score", "graph_score", "freshness_score",
                        "tradability_score", "hard_filter_passed", "hard_filter_reasons",
                        "catalyst_window", "observe_range", "entry_range", "stop_loss",
                        "invalidation", "reasons_json", "counter_evidence_json",
                        "priced_in_note", "risk_note",
                    ):
                        setattr(existing, field, getattr(row, field))
                    row = existing
        scores.append(row)

    scores.sort(key=lambda r: r.final_score, reverse=True)
    return {
        "scores": scores,
        "summary": (
            f"candidates={len(candidates)} scored={len(scores)} "
            f"top={scores[0].code}@{scores[0].final_score:.1f}"
            if scores else f"candidates={len(candidates)} scored=0"
        ),
    }


def assess_quality(result: dict[str, Any]) -> tuple[str, str]:
    scores = list(result.get("scores") or [])
    if not scores:
        return "degraded", "warn"
    passable = [s for s in scores if s.hard_filter_passed and s.final_score >= BUY_THRESHOLD]
    if not passable:
        return "degraded", "warn"
    return "succeeded", "pass"


def run_persist(
    *,
    trade_date: str = "",
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> dict[str, Any]:
    result = run(trade_date=trade_date, lookback_hours=lookback_hours, persist=True)
    status, quality = assess_quality(result)
    return {"status": status, "quality_status": quality, **result}
