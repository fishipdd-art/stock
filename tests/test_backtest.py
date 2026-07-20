"""Tests for backtest/engine.py, especially _infer_direction.

The _infer_direction helper chooses 'long' vs 'short' from the event
title's keyword set. The bug we fixed was that the negative keyword
list was too short, so bearish events (业绩下滑 / 亏损扩大) were
incorrectly classified as 'long'.
"""
from __future__ import annotations

import pytest

from backtest.engine import _infer_direction
from storage.models import IndustryEvent


def _event(title: str, etype: str = "earnings", impact: int = 3) -> IndustryEvent:
    return IndustryEvent(
        id=1,
        industry="test",
        industry_label="测试",
        title=title,
        event_type=etype,
        event_date=__import__("datetime").date.today(),
        impact_level=impact,
        source="curated",
        is_future=False,
    )


class TestInferDirection:
    @pytest.mark.parametrize("title", [
        "股价暴跌", "净利润下滑", "公司亏损扩大", "营收下降",
        "出口禁令", "被制裁", "产能过剩", "产品滞销",
        "工厂停产", "高管降薪", "大规模裁员", "退市风险",
        "重大事故", "项目终止", "并购失败", "经济萧条",
    ])
    def test_negative_titles_are_short(self, title):
        assert _infer_direction(_event(title)) == "short"

    @pytest.mark.parametrize("title", [
        "卫星成功发射", "新产品发布", "业绩大涨", "营收增长",
        "签订大单", "工厂投产", "获得FDA批准",
    ])
    def test_positive_event_types_are_long(self, title):
        # These have positive event types AND no negative keywords
        assert _infer_direction(_event(title)) == "long"

    def test_regulatory_is_short_by_default(self):
        # regulatory event type defaults to short unless positive words
        e = _event("新政策出台", etype="regulatory", impact=4)
        assert _infer_direction(e) == "short"

    def test_launch_is_long(self):
        e = _event("火箭发射", etype="launch", impact=5)
        assert _infer_direction(e) == "long"

    def test_default_is_long(self):
        e = _event("某事件", etype="other", impact=3)
        # No negative keywords, no bullish type -> long
        assert _infer_direction(e) == "long"

    def test_negative_keyword_overrides_positive_type(self):
        # Even if event_type is bullish, negative title forces short
        e = _event("业绩下滑但仍获订单", etype="earnings", impact=3)
        assert _infer_direction(e) == "short"
