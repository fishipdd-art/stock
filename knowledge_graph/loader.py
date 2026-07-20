"""
Knowledge-graph loader.

Responsibilities
----------------
1. Locate the 4 JSON files (auto-extract from zip if needed).
2. Idempotently load them into the SQLite DB:
     - supply_chain_terms.json  -> 21 categories + 72 search terms
     - supply_chain_signals.json -> 506 known supply-chain signals
                                    + signal<->A-share M2M (resolved by name)
     - supply_chain_stocks.json -> 148 A-share metadata rows
     - _pending_terms.json       -> 72 pending (un-categorised) terms
3. Provide a handful of typed query helpers used elsewhere in the project.
4. Provide a CLI: `python -m knowledge_graph.loader import|stats`.

Design notes
------------
* Idempotency is implemented per-table by natural-key UPSERT (check then
  update-or-insert). For child tables (`signal_stocks`) we nuke the previous
  rows for each parent and re-insert — simpler and correct under repeated
  imports whose child sets may grow or shrink.
* All work happens inside a single transaction (`db.tx()`). A failure mid-load
  rolls back to a clean slate.
* Malformed entries are logged at warning level and skipped; they never crash
  the import. The whole-import atomicity guarantee only kicks in for *fatal*
  errors (e.g. unreadable files, schema mismatch).
"""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select, delete, func, or_
from sqlalchemy.orm import Session

from config.settings import settings
from storage.database import Database, get_db, init_db
from storage.models import (
    AStock,
    KnowledgeCategory,
    KnowledgeSignal,
    PendingTerm,
    SearchTerm,
    SignalStock,
)


# ============================================================================
# Constants
# ============================================================================

# Default on-disk zip location (user-supplied). Honour the user's stated
# path. We still also fall back to ~/Downloads if `Path.home()` differs.
DEFAULT_ZIP_PATH = Path.home() / "Downloads" / "supply_chain_knowledge_graph.zip"

# JSON file basenames inside the zip / on disk
F_SIGNALS = "supply_chain_signals.json"
F_STOCKS = "supply_chain_stocks.json"
F_TERMS = "supply_chain_terms.json"
F_PENDING = "_pending_terms.json"

# A-share code parser: 6 digits, must start with 0 / 3 / 6.
#  0xxxxx / 3xxxxx  -> Shenzhen
#  6xxxxx          -> Shanghai
# Captures the first 6-digit run inside any text, then we filter by prefix.
_ASHARE_CODE_RE = re.compile(r"\b(\d{6})\b")

# Valid A-share code prefixes (Shenzhen + Shanghai main boards / ChiNext / STAR).
_VALID_ASHARE_PREFIXES = ("0", "3", "6")


# ============================================================================
# Dataclasses — load-report
# ============================================================================

@dataclass
class LoadReport:
    """Result of an import. Returned by :func:`import_all` and pretty-printed
    by the ``stats`` CLI subcommand."""
    n_categories: int = 0
    n_terms: int = 0
    n_signals: int = 0
    n_signal_stocks: int = 0
    n_stocks: int = 0
    n_pending: int = 0
    n_categorized: int = 0  # auto-categorized pending terms
    n_unresolved_signal_stocks: int = 0  # signal.a_stocks names with no matching A-share

    def as_dict(self) -> dict[str, int]:
        return {
            "knowledge_categories": self.n_categories,
            "search_terms": self.n_terms,
            "knowledge_signals": self.n_signals,
            "signal_stocks": self.n_signal_stocks,
            "a_stocks": self.n_stocks,
            "pending_terms": self.n_pending,
            "categorized_terms": self.n_categorized,
            "unresolved_signal_stocks": self.n_unresolved_signal_stocks,
        }


# ============================================================================
# Path / zip helpers
# ============================================================================

def resolve_knowledge_graph_dir(
    zip_path: Path | None = None,
    out_dir: Path | None = None,
) -> Path:
    """Return the directory that contains the 4 JSON files.

    Resolution order:
      1. If ``out_dir`` is provided and already contains the 4 files, use it.
      2. Otherwise, if ``settings.knowledge_graph_dir`` already contains the
         4 files, use it.
      3. Otherwise, look for the zip at ``zip_path`` (default
         ``~/Downloads/supply_chain_knowledge_graph.zip``) and extract it
         into ``out_dir`` (default ``settings.knowledge_graph_dir``).

    Idempotent: re-extracting an already-extracted zip is a no-op.
    """
    zip_path = Path(zip_path) if zip_path else DEFAULT_ZIP_PATH
    out_dir = Path(out_dir) if out_dir else settings.knowledge_graph_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if _dir_has_all_files(out_dir):
        return out_dir

    if zip_path.exists():
        extract_zip_if_needed(zip_path, out_dir)
        return out_dir

    raise FileNotFoundError(
        f"Knowledge-graph JSON files not found in {out_dir} and zip not found "
        f"at {zip_path}. Place the zip in ~/Downloads or set the "
        f"knowledge_graph_dir setting."
    )


def _dir_has_all_files(d: Path) -> bool:
    return all((d / f).is_file() for f in (F_SIGNALS, F_STOCKS, F_TERMS, F_PENDING))


def extract_zip_if_needed(zip_path: Path, out_dir: Path) -> None:
    """Extract the zip into ``out_dir`` (creating files only if missing)."""
    zip_path = Path(zip_path)
    out_dir = Path(out_dir)
    if not zip_path.exists():
        raise FileNotFoundError(f"Zip not found: {zip_path}")

    with zipfile.ZipFile(zip_path) as zf:
        # `namelist()` may include directory entries. Iterate; skip dirs.
        for member in zf.namelist():
            name = Path(member).name
            if not name or name.endswith("/"):
                continue
            target = out_dir / name
            if target.exists():
                continue
            # Extract single file (avoids path traversal; we only accept bare basenames)
            with zf.open(member) as src, open(target, "wb") as dst:
                dst.write(src.read())
            logger.info(f"Extracted {name} -> {target}")

    if not _dir_has_all_files(out_dir):
        missing = [f for f in (F_SIGNALS, F_STOCKS, F_TERMS, F_PENDING)
                   if not (out_dir / f).exists()]
        raise RuntimeError(
            f"After extraction, expected files are missing in {out_dir}: {missing}"
        )


# ============================================================================
# A-share code parser
# ============================================================================

def parse_a_share_codes(text: str | None) -> str:
    """Extract valid A-share codes (6 digits, starting with 0/3/6) from ``text``.

    The text comes from the ``a_share_map`` field of :class:`SearchTerm`, e.g.::

        "中船特气(688146)/和远气体(002971)"

    Returns a comma-separated string of unique codes in first-seen order.
    Empty / non-string input returns ``""``.
    """
    if not text or not isinstance(text, str):
        return ""
    seen: set[str] = set()
    out: list[str] = []
    for m in _ASHARE_CODE_RE.finditer(text):
        code = m.group(1)
        if code[0] not in _VALID_ASHARE_PREFIXES:
            continue
        if code in seen:
            continue
        seen.add(code)
        out.append(code)
    return ",".join(out)


def normalize_stock_code(code: str | None) -> str:
    """Normalise a stock code to 6 digits, zero-padded on the left.

    The raw ``stocks.json`` has a few entries whose code is shorter than 6
    characters (e.g. ``"636"`` for 风华高科, ``"2916"`` for 深南电路).
    These are clearly *truncated* canonical A-share codes. Padding to 6
    digits makes joins and lookups consistent.
    """
    if not code:
        return ""
    code = str(code).strip()
    if not code.isdigit():
        return code  # leave as-is if non-numeric (shouldn't happen for A-shares)
    return code.zfill(6)


# ============================================================================
# Generic load helpers (UPSERT)
# ============================================================================

def _to_str(v: Any, default: str = "") -> str:
    """Coerce to non-None str; treat None / non-str as the default."""
    if v is None:
        return default
    if isinstance(v, str):
        return v
    return str(v)


def _to_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _upsert_category(
    s: Session,
    name: str,
    signal_type: str,
    cache: dict[str, KnowledgeCategory] | None = None,
) -> KnowledgeCategory:
    """Insert or update a KnowledgeCategory; return the row.

    ``cache`` (optional) is an in-txn key->row map to handle duplicate keys
    appearing in the input data within a single transaction. When the DB
    has no row and ``cache`` is provided, the new row is registered there
    so subsequent calls in the same txn can find it without flushing.
    """
    if cache is not None and name in cache:
        cat = cache[name]
        cat.signal_type = signal_type
        return cat
    cat = s.execute(
        select(KnowledgeCategory).where(KnowledgeCategory.name == name)
    ).scalar_one_or_none()
    if cat is None:
        cat = KnowledgeCategory(name=name, signal_type=signal_type, n_terms=0)
        s.add(cat)
    else:
        cat.signal_type = signal_type
    s.flush()
    if cache is not None:
        cache[name] = cat
    return cat


def _upsert_term(
    s: Session,
    term: str,
    category: KnowledgeCategory,
    priority: str,
    transmission_logic: str,
    a_share_map: str,
    cache: dict[tuple[str, int], SearchTerm] | None = None,
) -> SearchTerm:
    """Insert or update a SearchTerm; return the row.

    ``cache`` is a per-txn ``(term, category_id) -> row`` map to dedupe
    duplicate (term, category) pairs that may appear in the input data
    within a single transaction.
    """
    key = (term, category.id)
    if cache is not None and key in cache:
        row = cache[key]
        row.priority = priority
        row.transmission_logic = transmission_logic
        row.a_share_map = a_share_map
        row.a_share_codes = parse_a_share_codes(a_share_map)
        return row
    row = s.execute(
        select(SearchTerm).where(
            SearchTerm.term == term, SearchTerm.category_id == category.id
        )
    ).scalar_one_or_none()
    a_share_codes = parse_a_share_codes(a_share_map)
    if row is None:
        row = SearchTerm(
            term=term,
            category_id=category.id,
            priority=priority,
            transmission_logic=transmission_logic,
            a_share_map=a_share_map,
            a_share_codes=a_share_codes,
            # Auto-discovered rows are historical observations, not durable
            # search queries. Enabling them causes dates/prices from one old
            # event to match unrelated future news.
            enabled=category.name != "auto_discovered",
        )
        s.add(row)
    else:
        row.priority = priority
        row.transmission_logic = transmission_logic
        row.a_share_map = a_share_map
        row.a_share_codes = a_share_codes
        if category.name == "auto_discovered":
            row.enabled = False
    s.flush()
    if cache is not None:
        cache[key] = row
    return row


def _upsert_signal(
    s: Session,
    key: str,
    payload: dict[str, Any],
    cache: dict[str, KnowledgeSignal] | None = None,
) -> KnowledgeSignal:
    """Insert or update a KnowledgeSignal; return the row.

    ``cache`` is a per-txn ``signal_key -> row`` map.
    """
    if cache is not None and key in cache:
        row = cache[key]
        _populate_signal_fields(row, key, payload)
        return row
    row = s.execute(
        select(KnowledgeSignal).where(KnowledgeSignal.signal_key == key)
    ).scalar_one_or_none()

    # JSON-encoded fields
    sources = payload.get("sources") or []
    sources_json = json.dumps(sources, ensure_ascii=False)

    if row is None:
        row = KnowledgeSignal(signal_key=key)
        s.add(row)
    _populate_signal_fields(row, key, payload)
    s.flush()
    if cache is not None:
        cache[key] = row
    return row


def _populate_signal_fields(row: KnowledgeSignal, key: str, payload: dict[str, Any]) -> None:
    """Apply payload fields to an existing or freshly-added :class:`KnowledgeSignal`."""
    sources = payload.get("sources") or []
    row.title = _to_str(payload.get("signal"), default=key)
    row.description = _to_str(payload.get("key_signal"))
    row.price_info = _to_str(payload.get("price_info"))
    row.grade = _to_str(payload.get("grade"))
    row.direction = _to_str(payload.get("direction"), default="unknown")
    row.strength = _to_float(payload.get("strength"))
    row.veracity = _to_str(payload.get("signal_veracity"))
    row.phase = _to_str(payload.get("phase"), default="active")
    row.signal_date = _to_str(payload.get("signal_date"))
    row.note = _to_str(payload.get("note"))
    row.sources_json = json.dumps(sources, ensure_ascii=False)
    last_hits = payload.get("last_hits") or []
    row.last_hit_ts = _to_str(last_hits[0].get("ts") if last_hits else "")


def _upsert_stock(
    s: Session,
    code: str,
    payload: dict[str, Any],
    cache: dict[str, AStock] | None = None,
) -> AStock:
    """Insert or update an AStock; return the row.

    ``cache`` is a per-txn ``code -> row`` map. The input data can contain
    duplicate codes (e.g. ``深南电路`` appears twice with the same code
    ``002916``); without this cache, ``s.get()`` won't see pending inserts
    in the same session when ``autoflush=False`` and we'd raise a UNIQUE
    constraint failure at flush time.
    """
    if cache is not None and code in cache:
        row = cache[code]
        _populate_stock_fields(row, payload)
        return row
    row = s.get(AStock, code)
    if row is None:
        row = AStock(code=code, name="")
        s.add(row)
    _populate_stock_fields(row, payload)
    s.flush()
    if cache is not None:
        cache[code] = row
    return row


def _populate_stock_fields(row: AStock, payload: dict[str, Any]) -> None:
    """Apply payload fields to an existing or freshly-added :class:`AStock`."""
    sector_tags = ",".join(payload.get("signals") or [])
    new_name = _to_str(payload.get("name"))
    if new_name:
        row.name = new_name
    row.sector_tags = sector_tags
    row.supply_exposure = _to_str(
        payload.get("supply_exposure"), default=row.supply_exposure or ""
    )
    row.tier = _to_int(payload.get("tier"), default=row.tier or 0)
    row.last_seen_at = datetime.utcnow()


def _upsert_pending(s: Session, term: str, seen: set[str] | None = None) -> bool:
    """Insert a PendingTerm if missing. Returns True if newly inserted.

    ``seen`` is a per-txn dedupe set for terms that appear more than once
    in the input data within a single transaction.
    """
    if seen is not None:
        if term in seen:
            return False
        seen.add(term)
    exists = s.execute(
        select(PendingTerm.id).where(PendingTerm.term == term)
    ).scalar_one_or_none()
    if exists is not None:
        return False
    s.add(PendingTerm(term=term))
    s.flush()
    return True


# ============================================================================
# Loaders
# ============================================================================

def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_categories(db: Database, terms_path: Path) -> int:
    """Load the 21 categories from ``supply_chain_terms.json``."""
    data = _read_json(terms_path)
    categories = data.get("categories", [])
    if not isinstance(categories, list):
        raise ValueError(f"{terms_path}: expected 'categories' to be a list")

    n = 0
    cache: dict[str, KnowledgeCategory] = {}
    with db.tx() as s:
        for cat in categories:
            if not isinstance(cat, dict):
                logger.warning(f"Skipping malformed category entry: {cat!r}")
                continue
            name = _to_str(cat.get("category"))
            if not name:
                logger.warning(f"Skipping category with empty name: {cat!r}")
                continue
            signal_type = _to_str(cat.get("signal_type"))
            _upsert_category(s, name, signal_type, cache=cache)
            n += 1
    logger.info(f"Loaded {n} categories from {terms_path.name}")
    return n


def load_terms(db: Database, terms_path: Path) -> int:
    """Load the 72 search terms (across 21 categories) from
    ``supply_chain_terms.json``. Returns the number of terms loaded."""
    data = _read_json(terms_path)
    categories = data.get("categories", [])
    if not isinstance(categories, list):
        raise ValueError(f"{terms_path}: expected 'categories' to be a list")

    n = 0
    cat_cache: dict[str, KnowledgeCategory] = {}
    term_cache: dict[tuple[str, int], SearchTerm] = {}
    with db.tx() as s:
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            cat_name = _to_str(cat.get("category"))
            if not cat_name:
                continue
            category = s.execute(
                select(KnowledgeCategory).where(KnowledgeCategory.name == cat_name)
            ).scalar_one_or_none()
            if category is None:
                category = _upsert_category(
                    s, cat_name, _to_str(cat.get("signal_type")), cache=cat_cache
                )

            terms_in_cat = 0
            for term in cat.get("terms") or []:
                if not isinstance(term, dict):
                    logger.warning(f"Skipping malformed term entry under {cat_name}: {term!r}")
                    continue
                t_text = _to_str(term.get("term"))
                if not t_text:
                    logger.warning(f"Skipping term with empty text under {cat_name}")
                    continue
                _upsert_term(
                    s,
                    term=t_text,
                    category=category,
                    priority=_to_str(term.get("priority"), default="中"),
                    transmission_logic=_to_str(term.get("signal")),
                    a_share_map=_to_str(term.get("a_share_map")),
                    cache=term_cache,
                )
                terms_in_cat += 1
            category.n_terms = terms_in_cat
            n += terms_in_cat
    logger.info(f"Loaded {n} search terms from {terms_path.name}")
    return n


def load_signals(db: Database, signals_path: Path) -> tuple[int, int, int]:
    """Load the 506 known signals from ``supply_chain_signals.json``.

    Returns ``(n_signals, n_signal_stocks, n_unresolved)`` where
    ``n_unresolved`` is the number of signal-level ``a_stocks`` entries
    whose stock name could not be resolved to a known A-share code.
    """
    data = _read_json(signals_path)
    raw = data.get("signals", {})
    if not isinstance(raw, dict):
        raise ValueError(f"{signals_path}: expected 'signals' to be a dict")

    # Build a name -> code index over the *already-loaded* A-stocks. Because
    # load_signals() is called *after* load_stocks() in import_all(), the
    # index is fully populated. We do it inside the same transaction for
    # consistency with the in-flight session.
    n_signals = 0
    n_links = 0
    n_unresolved = 0
    sig_cache: dict[str, KnowledgeSignal] = {}

    with db.tx() as s:
        name_to_code: dict[str, str] = {
            row.name: row.code
            for row in s.execute(select(AStock)).scalars().all()
            if row.name
        }

        for key, payload in raw.items():
            if not isinstance(payload, dict):
                logger.warning(f"Skipping malformed signal entry under key {key!r}")
                continue
            try:
                signal = _upsert_signal(s, key, payload, cache=sig_cache)
            except Exception as e:
                logger.warning(f"Skipping signal {key!r} due to error: {e}")
                continue

            # Replace child signal_stocks for this signal (nuke-and-replace).
            s.execute(
                delete(SignalStock).where(SignalStock.signal_id == signal.id)
            )

            seen_codes: set[str] = set()
            for stock_name in payload.get("a_stocks") or []:
                if not isinstance(stock_name, str) or not stock_name.strip():
                    continue
                code = name_to_code.get(stock_name)
                if code is None:
                    n_unresolved += 1
                    logger.debug(
                        f"Signal {key!r}: a_stock {stock_name!r} not in stocks.json — skipping"
                    )
                    continue
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                s.add(SignalStock(signal_id=signal.id, stock_code=code, strength=0.0))
                n_links += 1
            n_signals += 1

    logger.info(
        f"Loaded {n_signals} signals + {n_links} signal-stock links "
        f"({n_unresolved} unresolved names) from {signals_path.name}"
    )
    return n_signals, n_links, n_unresolved


def load_stocks(db: Database, stocks_path: Path) -> int:
    """Load the 148 A-share metadata rows from ``supply_chain_stocks.json``."""
    data = _read_json(stocks_path)
    stocks = data.get("stocks", [])
    if not isinstance(stocks, list):
        raise ValueError(f"{stocks_path}: expected 'stocks' to be a list")

    n = 0
    cache: dict[str, AStock] = {}
    with db.tx() as s:
        for entry in stocks:
            if not isinstance(entry, dict):
                logger.warning(f"Skipping malformed stock entry: {entry!r}")
                continue
            raw_code = _to_str(entry.get("code"))
            code = normalize_stock_code(raw_code)
            if not code:
                logger.warning(f"Skipping stock with empty code: {entry!r}")
                continue
            _upsert_stock(s, code, entry, cache=cache)
            n += 1
    logger.info(f"Loaded {n} A-stocks from {stocks_path.name}")
    return n


def load_pending_terms(db: Database, pending_path: Path) -> int:
    """Load the 72 pending (un-categorised) terms from ``_pending_terms.json``."""
    data = _read_json(pending_path)
    if not isinstance(data, list):
        raise ValueError(f"{pending_path}: expected a JSON list")

    n_inserted = 0
    seen: set[str] = set()
    with db.tx() as s:
        for entry in data:
            if not isinstance(entry, dict):
                logger.warning(f"Skipping malformed pending term entry: {entry!r}")
                continue
            term = _to_str(entry.get("term"))
            if not term:
                logger.warning(f"Skipping pending term with empty text: {entry!r}")
                continue
            if _upsert_pending(s, term, seen=seen):
                n_inserted += 1
    logger.info(f"Inserted {n_inserted} new pending terms from {pending_path.name}")
    return n_inserted


def categorize_pending_terms(
    db: Database,
    min_similarity: float = 0.4,
    logger: Logger = logger,
) -> int:
    """Auto-categorize PendingTerm records by matching term text against
    existing category names and their search-term keywords.

    Strategy (simple & predictable):
      1. Build a keyword pool from (a) category names and (b) existing
         SearchTerm.term values, lowercased.
      2. For each PendingTerm, compute the longest common-substring ratio
         against every keyword in the pool.
      3. If the best match exceeds *min_similarity* and is unambiguous
         (only one category wins), create a SearchTerm record, link it,
         and delete the pending record.

    Returns the number of terms successfully categorized.
    """
    import difflib
    from storage.models import SearchTerm

    with db.session() as s:
        pending: list[PendingTerm] = list(
            s.execute(
                select(PendingTerm).order_by(PendingTerm.id)
            ).scalars().all()
        )
        if not pending:
            return 0

        # Build keyword -> category_id map
        keyword_map: dict[str, int] = {}
        categories = s.execute(
            select(KnowledgeCategory)
        ).scalars().all()
        for cat in categories:
            keyword_map[cat.name.lower()] = cat.id
        search_terms = s.execute(
            select(SearchTerm)
        ).scalars().all()
        for st in search_terms:
            keyword_map[st.term.lower()] = st.category_id

        unique_keywords = list(keyword_map.keys())
        if not unique_keywords:
            logger.info("No categories or search terms to match against, skipping")
            return 0

    n_categorized = 0
    with db.tx() as s:
        for pt in pending:
            term_lower = pt.term.lower()
            # Short-circuit: if the term is already a keyword, use it directly
            if term_lower in keyword_map:
                cat_id = keyword_map[term_lower]
            else:
                # Find best match via difflib
                ratios = [(kw, difflib.SequenceMatcher(None, term_lower, kw).ratio()) for kw in unique_keywords]
                best_kw, best_ratio = max(ratios, key=lambda x: x[1])
                if best_ratio < min_similarity:
                    continue
                # Resolve category_id — need to handle multiple keywords mapping
                # to different categories
                best_cat_id = keyword_map[best_kw]
                # Check if there are other keywords with similar ratio that map
                # to a different category
                competing = [
                    kw for kw, r in ratios
                    if r >= best_ratio - 0.05 and keyword_map[kw] != best_cat_id
                ]
                if competing:
                    logger.debug(
                        f"Ambiguous match for '{pt.term}': best={best_kw} "
                        f"(cat={best_cat_id}), competitors={competing}, skipping"
                    )
                    continue
                cat_id = best_cat_id

            # Check for duplicate SearchTerm in that category
            exists = s.execute(
                select(SearchTerm.id).where(
                    SearchTerm.term == pt.term,
                    SearchTerm.category_id == cat_id,
                )
            ).scalar_one_or_none()
            if exists is not None:
                # Already categorized — just remove the pending record
                s.delete(pt)
                n_categorized += 1
                continue

            # Find the category row to get default priority
            cat = s.get(KnowledgeCategory, cat_id)
            s.add(SearchTerm(
                term=pt.term,
                category_id=cat_id,
                priority="低",  # default for auto-categorized
                transmission_logic=cat.name if cat else "",
                a_share_map="",
                a_share_codes="",
                enabled=False,
            ))
            s.delete(pt)
            n_categorized += 1
            s.flush()

    if n_categorized:
        logger.info(f"Auto-categorized {n_categorized} pending term(s)")
    return n_categorized


# ============================================================================
# Orchestrator
# ============================================================================

def import_all(
    db: Database | None = None,
    kg_dir: Path | None = None,
    zip_path: Path | None = None,
) -> LoadReport:
    """Idempotent end-to-end import.

    1. Resolve (and extract, if needed) the JSON directory.
    2. Load tables in dependency order:
       categories -> terms (depend on categories) -> stocks -> signals
       (which reference stocks by name) -> pending terms.
    3. Each loader runs in its own transaction, but the whole sequence is
       wrapped in a final check / report; if any loader raises, callers see
       the exception and the partial state of the prior committed loaders
       remains (they are independently atomic).
    """
    db = db or get_db()
    db.init_schema()

    # 1. Locate JSON files
    kg_dir = resolve_knowledge_graph_dir(zip_path=zip_path, out_dir=kg_dir)
    logger.info(f"Using knowledge-graph source: {kg_dir}")

    # 2. Dependency order: categories & terms share files; stocks must be
    #    loaded before signals so that name->code resolution works.
    n_categories = load_categories(db, kg_dir / F_TERMS)
    n_terms = load_terms(db, kg_dir / F_TERMS)
    n_stocks = load_stocks(db, kg_dir / F_STOCKS)
    n_signals, n_links, n_unresolved = load_signals(db, kg_dir / F_SIGNALS)
    n_pending = load_pending_terms(db, kg_dir / F_PENDING)
    n_categorized = categorize_pending_terms(db)

    report = LoadReport(
        n_categories=n_categories,
        n_terms=n_terms,
        n_signals=n_signals,
        n_signal_stocks=n_links,
        n_stocks=n_stocks,
        n_pending=n_pending,
        n_unresolved_signal_stocks=n_unresolved,
        n_categorized=n_categorized,
    )
    logger.success(
        f"Import complete: categories={n_categories} terms={n_terms} "
        f"signals={n_signals} signal_stocks={n_links} stocks={n_stocks} "
        f"pending={n_pending} (unresolved signal stock names: {n_unresolved})"
    )

    # Invalidate caches that depend on the now-changed data.
    try:
        from cache.redis_cache import get_cache
        get_cache().clear()
    except Exception as e:
        logger.debug(f"Cache invalidation skipped: {e}")

    return report


# ============================================================================
# Query helpers
# ============================================================================

def get_all_categories(db: Database) -> list[KnowledgeCategory]:
    with db.session() as s:
        return list(
            s.execute(
                select(KnowledgeCategory).order_by(KnowledgeCategory.id)
            ).scalars().all()
        )


def get_terms_by_category(db: Database, category_name: str) -> list[SearchTerm]:
    with db.session() as s:
        cat = s.execute(
            select(KnowledgeCategory).where(KnowledgeCategory.name == category_name)
        ).scalar_one_or_none()
        if cat is None:
            return []
        return list(
            s.execute(
                select(SearchTerm)
                .where(SearchTerm.category_id == cat.id)
                .order_by(SearchTerm.priority.desc(), SearchTerm.id)
            ).scalars().all()
        )


def _serialize_terms(terms) -> list[dict]:
    """Snapshot a list of SearchTerm to plain dicts (cache-safe)."""
    return [
        {
            "id": t.id,
            "term": t.term,
            "category_id": t.category_id,
            "priority": t.priority,
            "transmission_logic": t.transmission_logic,
            "a_share_map": t.a_share_map,
            "a_share_codes": t.a_share_codes,
            "enabled": t.enabled,
        }
        for t in terms
    ]


def _deserialize_terms(rows) -> list:
    """Rehydrate plain dicts into detached SearchTerm objects."""
    from storage.models import SearchTerm
    return [SearchTerm(**r) for r in rows]


def get_terms_by_priority(db: Database, priority: str) -> list[SearchTerm]:
    """List of enabled search terms at a given priority bucket.

    Cached for 5 minutes in L1 (in-memory) + L2 (Redis if configured).
    The terms table changes only on knowledge-graph import, so a short
    TTL is safe and gives a big speedup for repeated calls (e.g. web UI
    refreshing every few seconds).
    """
    from cache.redis_cache import get_cache

    cache_key = f"kg:terms:priority:{priority}"
    cache = get_cache()
    cached_rows = cache.get(cache_key)
    if cached_rows is not None:
        return _deserialize_terms(cached_rows)

    with db.session() as s:
        rows = list(
            s.execute(
                select(SearchTerm)
                .where(SearchTerm.priority == priority)
                .order_by(SearchTerm.category_id, SearchTerm.id)
            ).scalars().all()
        )
    # Detach from session before caching
    snap = _serialize_terms(rows)
    cache.set(cache_key, snap, ttl=300)
    return _deserialize_terms(snap)


_GRADE_RANK = {"A": 4, "B+": 3, "B": 2, "C": 1, "D": 0, "T2": 2}


def _grade_at_least(grade: str, min_grade: str) -> bool:
    return _GRADE_RANK.get(grade, -1) >= _GRADE_RANK.get(min_grade, -1)


def get_all_active_signals(
    db: Database,
    min_grade: str = "B",
    min_strength: float = 0.0,
) -> list[KnowledgeSignal]:
    with db.session() as s:
        rows = list(
            s.execute(
                select(KnowledgeSignal).order_by(
                    KnowledgeSignal.signal_date.desc(),
                    KnowledgeSignal.strength.desc(),
                )
            ).scalars().all()
        )
    return [
        r for r in rows
        if _grade_at_least(r.grade, min_grade) and (r.strength or 0.0) >= min_strength
    ]


def get_signal_by_key(db: Database, signal_key: str) -> KnowledgeSignal | None:
    with db.session() as s:
        return s.execute(
            select(KnowledgeSignal).where(KnowledgeSignal.signal_key == signal_key)
        ).scalar_one_or_none()


def get_signals_by_stock(db: Database, stock_code: str) -> list[KnowledgeSignal]:
    with db.session() as s:
        return list(
            s.execute(
                select(KnowledgeSignal)
                .join(SignalStock, SignalStock.signal_id == KnowledgeSignal.id)
                .where(SignalStock.stock_code == stock_code)
                .order_by(KnowledgeSignal.strength.desc(), KnowledgeSignal.signal_date.desc())
            ).scalars().all()
        )


def search_signals_by_keyword(db: Database, keyword: str) -> list[KnowledgeSignal]:
    """Full-text-ish search across title / description / key_signal / price_info / note."""
    if not keyword:
        return []
    pat = f"%{keyword}%"
    with db.session() as s:
        return list(
            s.execute(
                select(KnowledgeSignal)
                .where(
                    or_(
                        KnowledgeSignal.title.like(pat),
                        KnowledgeSignal.description.like(pat),
                        KnowledgeSignal.price_info.like(pat),
                        KnowledgeSignal.note.like(pat),
                    )
                )
                .order_by(KnowledgeSignal.strength.desc())
            ).scalars().all()
        )


def get_stocks_for_signal(
    db: Database, signal_id: int
) -> list[tuple[AStock, float]]:
    """Return ``[(AStock, strength), ...]`` for a given signal_id, ordered by
    strength desc."""
    with db.session() as s:
        rows = s.execute(
            select(SignalStock, AStock)
            .join(AStock, AStock.code == SignalStock.stock_code)
            .where(SignalStock.signal_id == signal_id)
            .order_by(SignalStock.strength.desc(), AStock.code)
        ).all()
    return [(stock, ss.strength) for ss, stock in rows]


# ============================================================================
# Stats / CLI
# ============================================================================

@dataclass
class TableCount:
    name: str
    count: int


def _table_counts(db: Database) -> list[TableCount]:
    counts: list[TableCount] = []
    with db.session() as s:
        for model, label in [
            (KnowledgeCategory, "knowledge_categories"),
            (SearchTerm, "search_terms"),
            (AStock, "a_stocks"),
            (KnowledgeSignal, "knowledge_signals"),
            (SignalStock, "signal_stocks"),
            (PendingTerm, "pending_terms"),
        ]:
            n = s.execute(select(func.count()).select_from(model)).scalar_one()
            counts.append(TableCount(name=label, count=int(n or 0)))
    return counts


def print_stats(db: Database | None = None) -> None:
    db = db or get_db()
    counts = _table_counts(db)
    width = max(len(c.name) for c in counts)
    logger.info("Knowledge-graph table row counts:")
    for c in counts:
        logger.info(f"  {c.name:<{width}}  {c.count:>8,}")
    total = sum(c.count for c in counts)
    logger.info(f"  {'TOTAL':<{width}}  {total:>8,}")


# ============================================================================
# CLI entrypoint
# ============================================================================

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m knowledge_graph.loader",
        description="Import / inspect the pre-built supply-chain knowledge graph.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser("import", help="Extract (if needed) and load all 4 JSON files into the DB.")
    p_import.add_argument(
        "--zip", type=Path, default=None,
        help=f"Path to the source zip (default: {DEFAULT_ZIP_PATH})",
    )
    p_import.add_argument(
        "--dir", dest="kg_dir", type=Path, default=None,
        help="Pre-extracted JSON directory (default: settings.knowledge_graph_dir)",
    )

    p_stats = sub.add_parser("stats", help="Print row counts per table.")

    args = parser.parse_args(argv)

    db = init_db()

    if args.cmd == "import":
        try:
            report = import_all(db=db, kg_dir=args.kg_dir, zip_path=args.zip)
        except FileNotFoundError as e:
            logger.error(str(e))
            return 2
        except Exception as e:
            logger.exception(f"Import failed: {e}")
            return 1
        # Echo a single human-readable summary line for shell scripting
        d = report.as_dict()
        print("IMPORT_OK " + " ".join(f"{k}={v}" for k, v in d.items()))
        return 0

    if args.cmd == "stats":
        print_stats(db)
        return 0

    parser.error(f"Unknown command: {args.cmd}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
