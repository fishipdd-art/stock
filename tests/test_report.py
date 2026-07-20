"""Tests for processor/report.py — feishu payload, save_report, cache invalidation."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from processor.report import (
    generate_feishu_payload,
    generate_markdown_report,
    save_report,
    _save_report_inner,
)


class TestFeishuPayloadThreshold:
    """P0 fix: feishu payload threshold changed from >=4.0 to >=3.0
    to match markdown report consistency."""

    def test_payload_contains_meta(self, in_memory_db):
        """Feishu payload should always contain _meta dict with
        n_signals and n_mismatches."""
        payload = generate_feishu_payload(in_memory_db)
        assert "_meta" in payload
        assert "n_signals" in payload["_meta"]
        assert "n_mismatches" in payload["_meta"]

    def test_payload_structure(self, in_memory_db):
        payload = generate_feishu_payload(in_memory_db)
        assert payload["msg_type"] == "interactive"
        assert "card" in payload
        assert "elements" in payload["card"]
        assert "header" in payload["card"]


class TestSaveReport:
    """P0 fix: save_report rewritten to single upsert pattern."""

    def test_save_and_load(self, in_memory_db):
        """Should persist a report and allow reading it back."""
        from storage.models import DailyReport

        markdown = "# Test Report"
        payload = {"msg_type": "interactive", "card": {"elements": []}, "_meta": {"n_signals": 3, "n_mismatches": 1}}
        report_date = date(2026, 7, 6)

        rpt = save_report(in_memory_db, markdown, payload, report_date=report_date, n_signals=3)
        assert rpt.report_date == report_date
        assert rpt.markdown == markdown
        assert rpt.n_signals == 3

        # Read back
        with in_memory_db.session() as s:
            loaded = s.query(DailyReport).filter(
                DailyReport.report_date == report_date
            ).first()
            assert loaded is not None
            assert loaded.markdown == markdown

    def test_update_existing(self, in_memory_db):
        """Same date+type should update, not duplicate."""
        from storage.models import DailyReport

        markdown = "# First"
        report_date = date(2026, 7, 6)

        rpt1 = save_report(in_memory_db, markdown, {"_meta": {"n_signals": 2, "n_mismatches": 0}}, report_date=report_date, n_signals=2)
        # Update with updated payload + n_signals
        rpt2 = save_report(in_memory_db, "# Updated", {"_meta": {"n_signals": 5, "n_mismatches": 0}}, report_date=report_date, n_signals=5)

        assert rpt2.markdown == "# Updated"
        assert rpt2.n_signals == 5
        # Should be the same row (upsert), not a new one
        assert rpt2.id == rpt1.id

    def test_meta_overrides_n_signals(self, in_memory_db):
        """_meta.n_signals in payload should take priority over kwarg."""
        payload = {"_meta": {"n_signals": 42, "n_mismatches": 0}}
        rpt = save_report(in_memory_db, "", payload, report_date=date(2026, 7, 6), n_signals=99)
        assert rpt.n_signals == 42  # meta wins

    def test_different_types_saved_separately(self, in_memory_db):
        from storage.models import DailyReport
        d = date(2026, 7, 6)
        payload = {"_meta": {"n_signals": 1, "n_mismatches": 0}}
        rpt1 = save_report(in_memory_db, "full", payload, report_date=d, report_type="full", n_signals=1)
        rpt2 = save_report(in_memory_db, "quick", payload, report_date=d, report_type="quick", n_signals=1)
        assert rpt1.id != rpt2.id

    def test_cache_invalidation_called(self, in_memory_db):
        """save_report should try to invalidate report caches."""
        with patch("cache.redis_cache.get_cache") as mock_get_cache:
            mock_cache = MagicMock()
            mock_get_cache.return_value = mock_cache
            payload = {"_meta": {"n_signals": 1, "n_mismatches": 0}}
            save_report(in_memory_db, "", payload, report_date=date(2026, 7, 6), n_signals=1)
            mock_cache.delete.assert_any_call("report:latest")

    def test_historical_report_counts_news(self, in_memory_db):
        from storage.models import NewsRaw

        report_date = date(2026, 7, 6)
        with in_memory_db.tx() as session:
            session.add(NewsRaw(
                url="https://example.test/historical-news",
                title="Historical market news",
                source="test",
                source_label="Test",
                published_at=datetime(2026, 7, 6, 8, 0),
            ))

        report = save_report(
            in_memory_db,
            "# Historical",
            {"_meta": {"n_signals": 1}},
            report_date=report_date,
            report_type="backfill",
        )

        assert report.n_news == 1


class TestMarkdownReport:
    def test_generates_string(self, in_memory_db):
        result = generate_markdown_report(in_memory_db)
        assert isinstance(result, str)
        assert len(result) > 50

    def test_contains_expected_sections(self, in_memory_db):
        result = generate_markdown_report(in_memory_db)
        assert "热度" in result or "Hotness" in result or "信号" in result
