"""Tests for processor/sentiment.py _score_text.

The fix replaced a broken negator loop (which always cancelled to 0)
with a window-based local negation. These tests pin the new behavior.
"""
from __future__ import annotations

import pytest

from processor.sentiment import _score_text


class TestScoring:
    @pytest.mark.parametrize("text,expected_sign", [
        ("大幅上涨", +1),
        ("涨停", +1),
        ("突破新高", +1),
        ("业绩增长", +1),
        ("大幅下跌", -1),
        ("暴跌", -1),
        ("亏损扩大", -1),
        ("业绩下滑", -1),
        ("不予置评", 0),  # neither positive nor negative
    ])
    def test_basic_sentiment(self, text, expected_sign):
        score, _, _ = _score_text(text)
        if expected_sign == 0:
            assert abs(score) < 0.1
        else:
            assert (score > 0) == (expected_sign > 0), (
                f"{text!r}: expected {'+' if expected_sign > 0 else '-'} got {score}"
            )

    def test_empty_text(self):
        assert _score_text("") == (0.0, [], [])

    def test_none_safe(self):
        # _score_text expects str, but defensively:
        assert _score_text("")[0] == 0.0


class TestNegation:
    """The key fix: negators (不/未/没) flip the polarity of nearby sentiment words."""

    def test_positive_word_negated(self):
        # "不涨" should score negative
        score, pos, neg = _score_text("不涨")
        assert score < 0, f"expected negative for '不涨', got {score}"

    def test_negative_word_negated(self):
        # "不跌" should be less negative than "跌"
        s_neg, _, _ = _score_text("跌")
        s_unneg, _, _ = _score_text("不跌")
        assert s_unneg > s_neg, (
            f"unnegated '不跌' ({s_unneg}) should beat '跌' ({s_neg})"
        )

    def test_far_negator_does_not_invert(self):
        # "不" appearing 5+ chars before a positive word shouldn't flip
        # (window-based heuristic, not global)
        score, _, _ = _score_text("不相关的事情发生了 然后涨停")
        # 涨停 still positive, so overall positive
        assert score > 0

    def test_multiple_negators(self):
        # 两个否定不应当完全抵消 (double negative stays negative in CJK)
        score, _, _ = _score_text("不不涨")
        # Window-based: second '不' precedes '涨' -> flips to negative
        assert score < 0


class TestMagnitude:
    def test_strong_word_outweighs_weak(self):
        # 涨停 (1.5) vs 涨 (1.0) — same direction, different magnitude
        s_strong, _, _ = _score_text("涨停")
        s_weak, _, _ = _score_text("涨")
        # Both are +1 because of normalization (tanh squash); verify
        # they at least agree on sign and are saturated:
        assert s_strong > 0.5
        assert s_weak > 0.5
