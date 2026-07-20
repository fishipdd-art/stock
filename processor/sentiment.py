"""
News sentiment analysis.

Lightweight Chinese sentiment scoring using a finance-domain dictionary.
Scores news titles/content from -1 (very negative) to +1 (very positive).

Industry-aware:
  - 涨价/紧缺/扩产 → positive for upstream commodities
  - 跌价/过剩/减产 → negative
  - 公司-specific context: 业绩/订单 positive, 亏损/诉讼 negative
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from storage import get_db
from storage.models import NewsRaw, SectorHeat


# Sentiment dictionaries (Chinese finance domain)
_POSITIVE_WORDS = {
    # General positive
    "涨": 1.0, "上涨": 1.0, "涨超": 1.2, "涨停": 1.5, "大涨": 1.5, "飙升": 1.3, "新高": 1.2,
    "增长": 0.8, "增加": 0.6, "提升": 0.7, "提高": 0.7,
    "突破": 1.0, "创新高": 1.3, "历史新高": 1.5,
    "利好": 1.5, "受益": 1.0, "看好": 0.8, "增持": 1.0, "买入": 1.2, "强烈推荐": 1.5,
    "盈利": 0.8, "利润": 0.6, "净利润": 0.7, "营收": 0.5, "业绩": 0.5,
    "中标": 1.0, "签约": 0.8, "合作": 0.7, "并购": 0.8, "重组": 0.5,
    "扩产": 0.8, "投产": 0.8, "下线": 0.7, "量产": 0.8, "上市": 0.6, "首发": 0.6,
    "订单": 0.6, "大单": 1.0, "满产": 0.8, "扩产": 0.8,
    "回升": 0.7, "反弹": 0.6, "复苏": 0.8, "回暖": 0.7, "向好": 0.7,
    "稳健": 0.4, "超预期": 1.2, "超预期": 1.2, "亮眼": 0.9,
    "获批": 0.8, "批准": 0.7, "通过": 0.5, "通过": 0.5,
    "发行": 0.4, "募集": 0.4, "回购": 0.7,
    "国产替代": 0.8, "自主可控": 0.7, "突破封锁": 1.0,
    "建成": 0.5, "投产": 0.5, "奠基": 0.5,
}

_NEGATIVE_WORDS = {
    "跌": 1.0, "下跌": 1.0, "跌超": 1.2, "跌停": 1.5, "大跌": 1.5, "暴跌": 1.5, "重挫": 1.3,
    "下降": 0.7, "下滑": 0.7, "减少": 0.5, "降低": 0.5, "下调": 0.7,
    "新低": 1.2, "历史新低": 1.5, "破位": 1.0,
    "利空": 1.5, "受损": 1.0, "承压": 0.7, "看空": 0.8, "减持": 1.0, "卖出": 1.2,
    "亏损": 1.0, "亏": 0.7, "净利润下滑": 1.0, "营收下降": 0.8,
    "减产": 0.8, "停产": 1.0, "关停": 1.2, "裁员": 0.8, "降薪": 1.0,
    "诉讼": 0.7, "违规": 1.0, "处罚": 1.0, "调查": 0.8, "退市": 1.5,
    "过剩": 0.8, "供过于求": 0.8, "滞销": 1.0, "积压": 0.8,
    "低迷": 0.6, "疲软": 0.6, "下滑": 0.7,
    "延期": 0.6, "推迟": 0.5, "终止": 1.0, "失败": 1.2, "破产": 1.5,
    "制裁": 1.2, "禁令": 1.2, "管制": 1.0, "限制": 0.7, "禁止": 1.0,
    "脱钩": 0.8, "退出": 0.6, "撤离": 0.8,
    "事故": 1.2, "灾难": 1.5, "爆炸": 1.5, "火灾": 1.2, "亏损": 1.0,
    "危机": 1.2, "衰退": 1.0, "萧条": 1.2,
    "崩": 1.5, "暴跌": 1.5, "跳水": 1.2, "闪崩": 1.5, "跌停": 1.5,
}

# Negators (flip the next word's polarity)
_NEGATORS = {"不", "没", "无", "非", "未", "别", "勿"}


@dataclass
class SentimentScore:
    """Sentiment analysis result for a single news item."""
    news_id: int
    title: str
    score: float  # -1.0 to +1.0
    label: str  # 'bullish' / 'bearish' / 'neutral'
    positive_words: list
    negative_words: list
    confidence: float


def _score_text(text: str) -> tuple[float, list, list]:
    """Score a piece of text, return (score, pos_words, neg_words)."""
    if not text:
        return 0.0, [], []

    pos_score = 0.0
    neg_score = 0.0
    pos_words: list = []
    neg_words: list = []

    for word, weight in _POSITIVE_WORDS.items():
        if word in text:
            pos_score += weight
            pos_words.append(word)
    for word, weight in _NEGATIVE_WORDS.items():
        if word in text:
            neg_score += weight
            neg_words.append(word)

    # Apply negator effect: a negator ("不"/"未" etc.) preceding a sentiment
    # word inverts that word's polarity. We use a window-based heuristic: for
    # every sentiment word found in the text, look at the 2 chars immediately
    # preceding it; if a negator is there, flip its sign.
    def _flipped_pairs(words: dict) -> set[str]:
        flipped: set[str] = set()
        for w in words:
            if w and w in text:
                idx = text.find(w)
                while idx != -1:
                    preceding = text[max(0, idx - 2):idx]
                    if any(n in preceding for n in _NEGATORS):
                        flipped.add(w)
                    idx = text.find(w, idx + 1)
        return flipped

    pos_flipped = _flipped_pairs(_POSITIVE_WORDS)
    neg_flipped = _flipped_pairs(_NEGATIVE_WORDS)

    # Subtract flipped words from their original bucket and add them to the
    # opposite bucket at half weight (soft negation — common convention).
    for w in pos_flipped:
        w_weight = _POSITIVE_WORDS[w]
        pos_score -= w_weight
        neg_score += w_weight * 0.5
    for w in neg_flipped:
        w_weight = _NEGATIVE_WORDS[w]
        neg_score -= w_weight
        pos_score += w_weight * 0.5

    total = pos_score + neg_score
    if total == 0:
        return 0.0, [], []
    # Normalize to [-1, 1] via tanh-like squash
    raw = (pos_score - neg_score) / total
    return max(-1.0, min(1.0, raw)), pos_words, neg_words


def score_news(news_id: int, title: str, summary: str = "") -> SentimentScore:
    """Score a single news item."""
    score, pos, neg = _score_text(title + " " + summary)
    if score > 0.2:
        label = "bullish"
    elif score < -0.2:
        label = "bearish"
    else:
        label = "neutral"
    # Confidence based on word count and magnitude
    confidence = min(1.0, (len(pos) + len(neg)) / 5.0) * abs(score)
    return SentimentScore(
        news_id=news_id,
        title=title,
        score=score,
        label=label,
        positive_words=pos,
        negative_words=neg,
        confidence=confidence,
    )


def score_recent_news(hours_back: int = 24, limit: int = 200) -> list[SentimentScore]:
    """Score all recent news."""
    from datetime import datetime, timedelta
    db = get_db()
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    with db.session() as s:
        news = (
            s.query(NewsRaw)
            .filter(NewsRaw.published_at >= cutoff)
            .order_by(NewsRaw.published_at.desc())
            .limit(limit)
            .all()
        )
    return [
        score_news(n.id, n.title or "", n.summary or "")
        for n in news
    ]


def aggregate_sentiment(scores: list[SentimentScore]) -> dict:
    """Aggregate sentiment scores into summary stats."""
    if not scores:
        return {"avg_score": 0, "n_bullish": 0, "n_bearish": 0, "n_neutral": 0}

    bullish = [s for s in scores if s.label == "bullish"]
    bearish = [s for s in scores if s.label == "bearish"]
    neutral = [s for s in scores if s.label == "neutral"]
    avg = sum(s.score for s in scores) / len(scores)

    # Top bullish/bearish titles
    top_bullish = sorted(bullish, key=lambda s: s.score, reverse=True)[:5]
    top_bearish = sorted(bearish, key=lambda s: s.score)[:5]

    return {
        "avg_score": round(avg, 3),
        "n_bullish": len(bullish),
        "n_bearish": len(bearish),
        "n_neutral": len(neutral),
        "total": len(scores),
        "top_bullish": [
            {"news_id": s.news_id, "title": s.title, "score": s.score}
            for s in top_bullish
        ],
        "top_bearish": [
            {"news_id": s.news_id, "title": s.title, "score": s.score}
            for s in top_bearish
        ],
    }