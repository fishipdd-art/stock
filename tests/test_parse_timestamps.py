from __future__ import annotations
import pytest
import pytest
"""Tests for collector/news/base.py timestamp parsers.

Covers the parse_unix_millis bug that was silently dropping all
millisecond-epoch news from 财联社 / Sina.
"""

from datetime import datetime, timezone

from collector.news.base import parse_unix_millis, parse_unix_seconds, parse_time_string


class TestParseUnixSeconds:
    def test_positive_seconds(self):
        assert parse_unix_seconds(1_700_000_000) == datetime(2023, 11, 14, 22, 13, 20)

    def test_zero_returns_none(self):
        assert parse_unix_seconds(0) is None

    def test_negative_returns_none(self):
        assert parse_unix_seconds(-1) is None

    def test_none_returns_none(self):
        assert parse_unix_seconds(None) is None

    def test_empty_string_returns_none(self):
        assert parse_unix_seconds("") is None

    def test_garbage_returns_none(self):
        assert parse_unix_seconds("not-a-number") is None

    def test_huge_seconds_returns_none(self):
        # Without the millis heuristic, this would OverflowError.
        # parse_unix_seconds is the raw seconds path so it should yield None.
        assert parse_unix_seconds(10**15) is None


class TestParseUnixMillis:
    """The critical fix: previously returned None for any ms-epoch value."""

    def test_milliseconds_value(self):
        # 1_700_000_000_000 ms = 1_700_000_000 sec = 2023-11-14
        result = parse_unix_millis(1_700_000_000_000)
        assert result is not None
        assert result == datetime(2023, 11, 14, 22, 13, 20)

    def test_seconds_value(self):
        # <1e12 is treated as seconds
        result = parse_unix_millis(1_700_000_000)
        assert result == datetime(2023, 11, 14, 22, 13, 20)

    def test_zero_returns_none(self):
        assert parse_unix_millis(0) is None

    def test_none_returns_none(self):
        assert parse_unix_millis(None) is None

    def test_empty_string_returns_none(self):
        assert parse_unix_millis("") is None

    def test_garbage_returns_none(self):
        assert parse_unix_millis("garbage") is None

    def test_float_milliseconds(self):
        result = parse_unix_millis(1_700_000_000_000.0)
        assert result is not None

    def test_string_numeric(self):
        result = parse_unix_millis("1700000000000")
        assert result == datetime(2023, 11, 14, 22, 13, 20)


class TestParseTimeString:
    @pytest.mark.parametrize("text,expected", [
        ("2024-01-01 12:00:00", datetime(2024, 1, 1, 12, 0, 0)),
        ("2024-01-01 12:00", datetime(2024, 1, 1, 12, 0)),
        ("2024/01/01 12:00:00", datetime(2024, 1, 1, 12, 0, 0)),
        ("2024-01-01T12:00:00", datetime(2024, 1, 1, 12, 0, 0)),
        ("2024-01-01T12:00:00Z", datetime(2024, 1, 1, 12, 0, 0)),
    ])
    def test_valid(self, text, expected):
        assert parse_time_string(text) == expected

    @pytest.mark.parametrize("text", [None, "", "garbage", 123])
    def test_invalid(self, text):
        assert parse_time_string(text) is None
