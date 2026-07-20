"""
Sector / category hotness scoring.

For each KnowledgeCategory the engine pulls the union of stocks associated
with (a) signals whose text matches a category search term, and
(b) the A-share codes linked directly in the search term itself. It then
aggregates that day's stock_quotes into a single hotness score, ranks
all categories, and marks the top-N as `deep` (the rest are `shallow`).

The formula is deterministic and reproducible: same inputs -> same score.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from loguru import logger
from sqlalchemy import and_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from config.settings import settings
from storage.database import get_db, init_db
from storage.models import (
    AStock,
    KnowledgeCategory,
    KnowledgeSignal,
    NewsRaw,
    SearchTerm,
    SectorHeat,
    SignalStock,
    StockQuote,
)


# ---------------------------------------------------------------------------
# Tunables (kept in code; can be lifted to settings later)
# ---------------------------------------------------------------------------

# Score weights (sum to 1.0). W_ABS_CHANGE removed: a sector that is
# crashing across the board used to score high purely from |change_pct|
# sum; positive change alone is the directional signal we care about.
W_ABS_CHANGE = 0.00
W_TURNOVER = 0.20
W_POSITIVE_CHANGE = 0.50
W_NEWS_COUNT = 0.20
W_SIGNAL_STRENGTH = 0.10

# News count is multiplied by this to bring it onto a comparable scale
NEWS_COUNT_GAIN = 1.0

# Substring-match window sizes for the fuzzy term matcher. 2- and 3-grams
# work well for short Chinese phrases; we also keep the full term.
_MATCH_WINDOW_SIZES = (2, 3)


# ---------------------------------------------------------------------------
# Fuzzy match helpers
# ---------------------------------------------------------------------------

def _term_keywords(term: str) -> list[str]:
    """Split a term into matchable keywords.

    Returns the original term plus every 2- and 3-gram window. Result is
    deduped (preserves order for deterministic iteration).
    """
    term = (term or "").strip()
    if not term:
        return []
    out: list[str] = [term]
    for n in _MATCH_WINDOW_SIZES:
        for i in range(0, len(term) - n + 1):
            out.append(term[i : i + n])
    seen: set[str] = set()
    deduped: list[str] = []
    for k in out:
        if k and k not in seen:
            seen.add(k)
            deduped.append(k)
    return deduped


def _signal_matches_term(signal: KnowledgeSignal, term: str) -> bool:
    """Cheap fuzzy match: term or any 2/3-gram appears in the signal's title
    (or description as a fallback)."""
    haystack = (signal.title or "") or (signal.description or "")
    if not haystack:
        return False
    for kw in _term_keywords(term):
        if kw in haystack:
            return True
    return False


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class HotnessEngine:
    """Compute per-category hotness scores and persist them."""

    def __init__(self, db=None):
        self.db = db or get_db()
        self.db.init_schema()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_daily_hotness(self, trade_date: date) -> list[SectorHeat]:
        """Compute hotness for all categories on a given date and upsert
        the results into the sector_heat table.

        Returns the list of SectorHeat rows (one per category), ordered
        by rank ascending.
        """
        logger.info(f"compute_daily_hotness: {trade_date}")
        with self.db.session() as s:
            categories = self._load_categories(s)
            if not categories:
                logger.warning("compute_daily_hotness: no categories in DB")
                return []

            terms_by_cat = self._load_terms_by_category(
                s, [c.id for c in categories]
            )
            # One pass over signals: compute category matches and collect
            # signal->category mappings and per-category strengths.
            match = self._compute_signal_category_matches(terms_by_cat, s, trade_date)
            stocks_by_cat = self._stocks_by_category(
                terms_by_cat, match.sig_to_cats, s
            )
            quotes_by_code = self._load_quotes(s, trade_date)
            news_counts = self._load_news_counts(s, trade_date, terms_by_cat)

        results: list[dict] = []
        component_rows: list[dict] = []
        for cat in categories:
            codes = stocks_by_cat.get(cat.id, set())
            quotes = [quotes_by_code[c] for c in codes if c in quotes_by_code]
            abs_change_sum = sum(abs(q.change_pct) for q in quotes)
            turnover_sum = sum(q.turnover for q in quotes)
            pos_change_sum = sum(max(q.change_pct, 0.0) for q in quotes)
            news_count = news_counts.get(cat.id, 0)
            best_signal_strength = max(match.strengths_by_cat.get(cat.id, [0.0]))

            n_quotes = max(1, len(quotes))
            # Raw components are normalized across categories below.  Using
            # per-stock averages removes the previous category-size bias.
            component_rows.append({
                "attention_raw": math.log1p(max(0, news_count)),
                "market_raw": (
                    (pos_change_sum / n_quotes)
                    + math.log10(max(turnover_sum / n_quotes, 0.0) + 1.0)
                ),
                "evidence_raw": max(0.0, float(best_signal_strength)),
            })
            results.append({
                    "trade_date": trade_date,
                    "category_name": cat.name,
                    "hotness_score": 0.0,
                    "abs_change_sum": abs_change_sum,
                    "turnover_sum": turnover_sum,
                    "news_count": int(news_count),
                    "n_stocks": int(len(quotes)),
                    "rank": 0,
                    "processed_level": "shallow",
                    "attention_score": 0.0,
                    "market_score": 0.0,
                    "evidence_score": 0.0,
                    "calculation_version": "v2",
                })

        attention = self._percentile_scores([r["attention_raw"] for r in component_rows])
        market = self._percentile_scores([r["market_raw"] for r in component_rows])
        evidence = self._percentile_scores([r["evidence_raw"] for r in component_rows])
        for row, a_score, m_score, e_score in zip(results, attention, market, evidence):
            row["attention_score"] = round(a_score, 2)
            row["market_score"] = round(m_score, 2)
            row["evidence_score"] = round(e_score, 2)
            row["hotness_score"] = round(
                a_score * 0.30 + m_score * 0.45 + e_score * 0.25,
                2,
            )

        self._assign_ranks(results)
        self._upsert_sector_heat(results)
        saved = self._reload_sector_heat(trade_date)
        logger.info(
            f"compute_daily_hotness: wrote {len(saved)} rows "
            f"(top1={results[0]['category_name'] if results else '-'})"
        )
        return saved

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _score(
        turnover_sum: float,
        pos_change_sum: float,
        news_count: int,
        best_signal_strength: float,
    ) -> float:
        """Hotness formula (deterministic, unit-free).

        `abs_change_sum` used to be a term here but was removed: it inflated
        scores for sectors where every stock was moving hard in either
        direction, including broad sell-offs. `pos_change_sum` (with weight
        0.50) is the directional signal that matters.
        """
        turnover_term = math.log10(max(turnover_sum, 0.0) + 1.0) * 5.0
        news_term = float(news_count) * NEWS_COUNT_GAIN
        return (
            turnover_term * W_TURNOVER
            + pos_change_sum * W_POSITIVE_CHANGE
            + news_term * W_NEWS_COUNT
            + best_signal_strength * W_SIGNAL_STRENGTH
        )

    @staticmethod
    def _percentile_scores(values: list[float]) -> list[float]:
        """Tie-aware percentile normalization to a stable 0..100 scale."""
        if not values:
            return []
        if len(values) == 1:
            return [50.0]
        ordered = sorted((float(value), index) for index, value in enumerate(values))
        output = [0.0] * len(values)
        cursor = 0
        denominator = len(values) - 1
        while cursor < len(ordered):
            end = cursor + 1
            while end < len(ordered) and ordered[end][0] == ordered[cursor][0]:
                end += 1
            average_rank = (cursor + end - 1) / 2.0
            score = 100.0 * average_rank / denominator
            for _, original_index in ordered[cursor:end]:
                output[original_index] = score
            cursor = end
        return output

    @staticmethod
    def _assign_ranks(results: list[dict]) -> None:
        # Sort by hotness_score desc, tie-broken by category_name (stable)
        results.sort(key=lambda r: (-r["hotness_score"], r["category_name"]))
        top_n = max(0, int(settings.deep_process_top_n))
        for i, r in enumerate(results, 1):
            r["rank"] = i
            r["processed_level"] = "deep" if i <= top_n else "shallow"

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    @staticmethod
    def _load_categories(s: Session) -> list[KnowledgeCategory]:
        return list(
            s.execute(select(KnowledgeCategory).order_by(KnowledgeCategory.id))
            .scalars()
            .all()
        )

    @staticmethod
    def _load_terms_by_category(
        s: Session, category_ids: list[int]
    ) -> dict[int, list[SearchTerm]]:
        rows = (
            s.execute(
                select(SearchTerm).where(SearchTerm.category_id.in_(category_ids))
            )
            .scalars()
            .all()
        )
        out: dict[int, list[SearchTerm]] = defaultdict(list)
        for r in rows:
            out[r.category_id].append(r)
        return out

    def _compute_signal_category_matches(
        self,
        terms_by_cat: dict[int, list[SearchTerm]],
        s: Session,
        trade_date: date,
    ) -> "_MatchResult":
        """One pass over all signals, build:
          - strengths_by_cat: category_id -> list[float] (matched signal strengths)
          - sig_to_cats:      signal_id -> set[category_id] (which categories it hits)
        """
        signal_cutoff = trade_date - timedelta(days=7)
        all_signals = list(
            s.execute(
                select(KnowledgeSignal).where(
                    KnowledgeSignal.signal_date >= signal_cutoff.isoformat(),
                    KnowledgeSignal.signal_date <= trade_date.isoformat(),
                )
            ).scalars().all()
        )
        strengths_by_cat: dict[int, list[float]] = defaultdict(list)
        sig_to_cats: dict[int, set[int]] = defaultdict(set)
        for sig in all_signals:
            for cat_id, terms in terms_by_cat.items():
                if any(_signal_matches_term(sig, t.term) for t in terms):
                    strengths_by_cat[cat_id].append(float(sig.strength or 0.0))
                    sig_to_cats[int(sig.id)].add(cat_id)
        return _MatchResult(strengths_by_cat, sig_to_cats)

    @staticmethod
    def _stocks_by_category(
        terms_by_cat: dict[int, list[SearchTerm]],
        sig_to_cats: dict[int, set[int]],
        s: Session,
    ) -> dict[int, set[str]]:
        """For each category, union of:
          - stocks linked via SignalStock to a matched signal
          - codes declared in SearchTerm.a_share_codes for this category
        """
        out: dict[int, set[str]] = defaultdict(set)

        if sig_to_cats:
            # Only fetch SignalStock rows whose signal is in the matched set.
            signal_ids = list(sig_to_cats.keys())
            ss_rows = (
                s.execute(
                    select(SignalStock).where(SignalStock.signal_id.in_(signal_ids))
                )
                .scalars()
                .all()
            )
            for ss in ss_rows:
                for cat_id in sig_to_cats.get(int(ss.signal_id), ()):
                    out[cat_id].add(str(ss.stock_code).zfill(6))

        for cat_id, terms in terms_by_cat.items():
            for t in terms:
                for code in (t.a_share_codes or "").split(","):
                    code = code.strip()
                    if code:
                        out[cat_id].add(code.zfill(6))

        return out

    @staticmethod
    def _load_quotes(s: Session, trade_date: date) -> dict[str, StockQuote]:
        rows = (
            s.execute(select(StockQuote).where(StockQuote.trade_date == trade_date))
            .scalars()
            .all()
        )
        return {str(r.code).zfill(6): r for r in rows}

    @staticmethod
    def _load_news_counts(
        s: Session,
        trade_date: date,
        terms_by_cat: dict[int, list[SearchTerm]],
    ) -> dict[int, int]:
        """Count NewsRaw published on trade_date whose title contains
        any of the category's search terms.

        Returns 0 for every category if the news_raw table is empty
        (news collection not yet wired up).
        """
        out: dict[int, int] = {cat_id: 0 for cat_id in terms_by_cat}
        if not terms_by_cat:
            return out

        td_start = datetime.combine(trade_date, datetime.min.time())
        td_end = datetime.combine(trade_date, datetime.max.time())
        all_news = (
            s.execute(
                select(NewsRaw).where(
                    and_(NewsRaw.published_at >= td_start,
                         NewsRaw.published_at <= td_end)
                )
            )
            .scalars()
            .all()
        )
        if not all_news:
            return out

        for n in all_news:
            # Collector normalization already records matched terms.  Using
            # that field avoids losing valid news when a title is paraphrased
            # and also keeps category counts tied to the same matcher used by
            # the news pipeline.
            haystack = " ".join(filter(None, (n.title or "", n.keywords_matched or "")))
            if not haystack:
                continue
            for cat_id, terms in terms_by_cat.items():
                for t in terms:
                    if t.term and t.term in haystack:
                        out[cat_id] += 1
                        break  # one match per news per category is enough
        return out

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _upsert_sector_heat(rows: list[dict]) -> None:
        if not rows:
            return
        stmt = sqlite_insert(SectorHeat).values(rows)
        update_cols = {
            c.name: stmt.excluded[c.name]
            for c in SectorHeat.__table__.columns
            if c.name not in {"id", "created_at"}
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["trade_date", "category_name"],
            set_=update_cols,
        )
        db = get_db()
        with db.tx() as s:  # type: Session
            s.execute(stmt)

    @staticmethod
    def _reload_sector_heat(trade_date: date) -> list[SectorHeat]:
        db = get_db()
        with db.session() as s:
            saved = (
                s.execute(
                    select(SectorHeat)
                    .where(SectorHeat.trade_date == trade_date)
                    .order_by(SectorHeat.rank)
                )
                .scalars()
                .all()
            )
        return list(saved)


class _MatchResult:
    """Bundle of (strengths_by_cat, sig_to_cats) so we pass one thing
    around instead of two dicts."""

    __slots__ = ("strengths_by_cat", "sig_to_cats")

    def __init__(
        self,
        strengths_by_cat: dict[int, list[float]],
        sig_to_cats: dict[int, set[int]],
    ):
        self.strengths_by_cat = strengths_by_cat
        self.sig_to_cats = sig_to_cats
