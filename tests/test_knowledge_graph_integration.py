"""Integration test for the full knowledge graph import.

Skipped when the 4 JSON files aren't on disk (i.e. on a fresh clone).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import settings


KG_DIR = settings.knowledge_graph_dir
REQUIRED_FILES = [
    "supply_chain_terms.json",
    "supply_chain_signals.json",
    "supply_chain_stocks.json",
    "_pending_terms.json",
]


pytestmark = pytest.mark.skipif(
    not all((KG_DIR / f).is_file() for f in REQUIRED_FILES),
    reason="knowledge_graph JSON files not present in data/knowledge_graph/",
)


class TestImportAll:
    def test_idempotent(self, in_memory_db):
        from knowledge_graph import import_all

        r1 = import_all(db=in_memory_db)
        r2 = import_all(db=in_memory_db)

        # Second import should not change row counts
        assert r1.n_categories == r2.n_categories
        assert r1.n_signals == r2.n_signals
        assert r1.n_stocks == r2.n_stocks
        assert r1.n_terms == r2.n_terms

        # Sanity: at least 1 of each (the actual numbers depend on the data)
        assert r1.n_categories >= 1
        assert r1.n_signals >= 1
        assert r1.n_stocks >= 1
        assert r1.n_terms >= 1
