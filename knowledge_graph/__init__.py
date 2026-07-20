"""Knowledge graph subpackage — loads the pre-built supply-chain knowledge
graph (categories, terms, signals, A-share metadata) into the local SQLite DB.

CLI:

    python -m knowledge_graph.loader import   # load (idempotent)
    python -m knowledge_graph.loader stats    # print row counts
"""
from __future__ import annotations

from .loader import (  # noqa: F401
    import_all,
    load_categories,
    load_terms,
    load_signals,
    load_stocks,
    load_pending_terms,
    extract_zip_if_needed,
    resolve_knowledge_graph_dir,
    get_all_categories,
    get_terms_by_category,
    get_terms_by_priority,
    get_all_active_signals,
    get_signal_by_key,
    get_signals_by_stock,
    search_signals_by_keyword,
    get_stocks_for_signal,
    print_stats,
)
