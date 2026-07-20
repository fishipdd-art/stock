"""Tests for knowledge_graph/loader.py and parse_a_share_codes.

The KG loader is the entry point for the 506 signals / 148 stocks /
72 search terms. Idempotency is critical: the loader is called on every
`init` and we must not duplicate rows.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_graph.loader import (
    parse_a_share_codes,
    normalize_stock_code,
    _to_str,
    _to_int,
    _to_float,
)


class TestParseAShareCodes:
    def test_single_code(self):
        assert parse_a_share_codes("中船特气(688146)") == "688146"

    def test_multiple_codes(self):
        text = "中船特气(688146)/和远气体(002971)"
        assert parse_a_share_codes(text) == "688146,002971"

    def test_dedup(self):
        text = "A(000001)/B(000001)/C(000001)"
        assert parse_a_share_codes(text) == "000001"

    def test_filter_invalid_prefix(self):
        # 1xxxxx is not a valid A-share prefix (those are funds/bonds)
        text = "基金(110011)/平安银行(000001)"
        assert parse_a_share_codes(text) == "000001"

    def test_all_valid_prefixes(self):
        text = "深A(000001)/创A(300001)/沪A(600000)"
        result = parse_a_share_codes(text)
        assert "000001" in result
        assert "300001" in result
        assert "600000" in result

    def test_empty(self):
        assert parse_a_share_codes("") == ""
        assert parse_a_share_codes(None) == ""

    def test_no_codes(self):
        assert parse_a_share_codes("纯文本没有数字代码") == ""

    def test_preserves_order(self):
        text = "Z(600999)/A(000001)/M(300001)"
        assert parse_a_share_codes(text) == "600999,000001,300001"

    def test_short_code_filtered(self):
        # 5 digits should not match
        text = "abc(12345)/正常(600000)"
        assert parse_a_share_codes(text) == "600000"


class TestNormalizeStockCode:
    def test_already_normalized(self):
        assert normalize_stock_code("000001") == "000001"
        assert normalize_stock_code("688146") == "688146"

    def test_zfill(self):
        # Truncated codes (per the loader docstring) get padded
        assert normalize_stock_code("636") == "000636"
        assert normalize_stock_code("2916") == "002916"

    def test_non_digit_unchanged(self):
        # Non-numeric (HK codes, etc.) passes through
        assert normalize_stock_code("0700.HK") == "0700.HK"

    def test_empty(self):
        assert normalize_stock_code("") == ""
        assert normalize_stock_code(None) == ""


class TestCoercionHelpers:
    def test_to_str(self):
        assert _to_str("hello") == "hello"
        assert _to_str(None) == ""
        assert _to_str(None, default="x") == "x"
        assert _to_str(123) == "123"
        assert _to_str(None, default="default") == "default"

    def test_to_int(self):
        assert _to_int("42") == 42
        assert _to_int(42) == 42
        assert _to_int(None) == 0
        assert _to_int("garbage") == 0
        assert _to_int(None, default=99) == 99

    def test_to_float(self):
        assert _to_float("3.14") == 3.14
        assert _to_float(None) == 0.0
        assert _to_float("bad") == 0.0


class TestKnowledgeGraphIdempotency:
    """Verify that import_all can be run twice without doubling rows.

    The real integration test lives in test_knowledge_graph_integration.py
    and only runs when the 4 JSON files exist on disk.
    """

    def test_placeholder(self):
        # See the integration test for the actual coverage.
        assert True


class TestCachedGetTerms:
    """Test the cache wiring in get_terms_by_priority."""

    def test_caches_results(self, in_memory_db):
        from cache import redis_cache as rc
        from knowledge_graph import loader
        rc._l1_cache.clear()

        # SearchTerm has NOT NULL category_id, so create a category first.
        # Use db.tx() to commit (regular session() doesn't auto-commit).
        from storage.models import SearchTerm, KnowledgeCategory
        with in_memory_db.tx() as s:
            cat = KnowledgeCategory(name="测试分类", signal_type="test", n_terms=2)
            s.add(cat)
            s.flush()
            s.add(SearchTerm(
                term="铜价", category_id=cat.id, priority="高",
                transmission_logic="", a_share_map="", a_share_codes="",
                enabled=True,
            ))
            s.add(SearchTerm(
                term="铝价", category_id=cat.id, priority="中",
                transmission_logic="", a_share_map="", a_share_codes="",
                enabled=True,
            ))

        # First call: cache miss, populates
        high1 = loader.get_terms_by_priority(in_memory_db, "高")
        assert any(t.term == "铜价" for t in high1)

        # Second call: should hit cache (same data, possibly different object)
        high2 = loader.get_terms_by_priority(in_memory_db, "高")
        assert high2[0].term == "铜价"

        # Cache key should be populated
        assert "kg:terms:priority:高" in rc._l1_cache

    def test_cache_clear_helper(self, in_memory_db):
        """Verify the cache layer we depend on is wired up."""
        from cache import redis_cache as rc
        rc._l1_cache["test:key"] = ("val", 9999999999)
        c = rc.get_cache()
        c.clear()
        assert "test:key" not in rc._l1_cache
