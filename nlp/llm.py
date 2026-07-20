"""
LLM integration for natural language understanding.

Supports OpenAI-compatible APIs (Qwen, GLM-4, DeepSeek, etc.) for
intent classification and entity extraction. Falls back to local
rule-based system when no API key is available.

Configuration via env vars:
  LLM_API_BASE: API endpoint (e.g., https://dashscope.aliyuncs.com/compatible-mode/v1)
  LLM_API_KEY: API key
  LLM_MODEL: model name (default: qwen-turbo)
  LLM_ENABLED: set to "true" to enable (auto-enabled when key present)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

from loguru import logger

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


# LLM intent options
INTENT_OPTIONS = [
    "upcoming_events",      # 未来事件
    "search_event",         # 搜索事件
    "top_signals",          # 强信号
    "today_stocks",         # 今日股票
    "today_futures",        # 今日期货
    "latest_report",        # 最新报告
    "industry_question",    # 行业问题
    "price_prediction",     # 价格预测
    "recommend_event",      # 推荐事件
    "chitchat",             # 闲聊
    "unknown",              # 未知
]


SYSTEM_PROMPT = """你是供应链股票分析系统的 NLU 模块。
用户用自然语言查询事件/报告/股票/行业。请提取：
1. intent: 从这些选项选择 - upcoming_events, search_event, top_signals, today_stocks, today_futures, latest_report, industry_question, price_prediction, recommend_event, chitchat, unknown
2. entities: 行业名称、事件类型、股票代码、时间范围 (today/tomorrow/week/30days)、关键词
3. confidence: 0-1

严格按 JSON 格式输出：
{"intent": "...", "entities": {...}, "confidence": 0.9, "reasoning": "..."}"""


@dataclass
class LLMIntent:
    intent: str
    entities: dict
    confidence: float
    reasoning: str = ""
    source: str = "local"  # 'llm' or 'local'


def is_llm_enabled() -> bool:
    """Check if LLM is configured."""
    return bool(os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"))


def call_llm(user_query: str, timeout: float = 8.0) -> Optional[dict]:
    """Call OpenAI-compatible LLM API. Returns parsed dict or None on failure."""
    if not is_llm_enabled() or not HAS_HTTPX:
        return None

    api_base = os.environ.get("LLM_API_BASE", "https://api.openai.com/v1")
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("LLM_MODEL", "gpt-3.5-turbo")

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{api_base.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_query},
                    ],
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                },
            )
            if resp.status_code != 200:
                logger.warning(f"LLM API {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception as e:
        logger.warning(f"LLM call failed: {e!r}")
        return None


# ============================================================================
# Local fallback: sophisticated rule-based NLU
# ============================================================================

# Industry keyword mapping
_INDUSTRY_KEYWORDS = {
    "航天军工": ["航天", "火箭", "卫星", "军工", "SpaceX", "Starship", "千帆", "GW", "朱雀", "谷神星", "C919", "航空"],
    "半导体": ["半导体", "芯片", "硅片", "ASML", "TSMC", "台积电", "中芯", "长存", "长鑫", "HBM", "DRAM", "NAND", "MLCC"],
    "新能源车": ["新能源车", "电动车", "比亚迪", "特斯拉", "Tesla", "小米SU7", "小鹏", "理想", "蔚来", "问界"],
    "锂电": ["锂电", "电池", "宁德", "碳酸锂", "锂电池"],
    "光伏": ["光伏", "多晶硅", "硅料", "隆基", "通威"],
    "消费电子": ["消费电子", "手机", "iPhone", "华为", "苹果", "Mate"],
    "医药": ["医药", "药", "FDA", "NMPA", "新药", "创新药", "百济", "信达", "恒瑞"],
    "银行": ["银行", "工行", "农行", "中行", "建行", "招行", "平安"],
    "房地产": ["房地产", "地产", "万科", "保利"],
    "互联网": ["互联网", "阿里", "腾讯", "拼多多", "美团", "字节"],
    "AI": ["AI", "人工智能", "大模型", "算力", "GPU", "H100", "H200", "NVIDIA", "英伟达", "Anthropic"],
    "钢铁": ["钢铁", "螺纹钢", "铁矿石"],
    "有色": ["有色", "铜", "铝", "锌", "镍"],
    "农产品": ["农产品", "大豆", "玉米", "猪肉", "生猪"],
    "能源": ["原油", "石油", "天然气", "LNG"],
    "航运": ["航运", "集运", "集装箱", "BDI", "SCFI"],
    "稀土": ["稀土", "镨钕", "钕铁硼"],
}

# Time keyword mapping
_TIME_KEYWORDS = {
    "今天": 0, "今日": 0, "当天": 0,
    "明天": 1, "明日": 1, "今晚": 1,
    "后天": 2,
    "本周": 7, "这周": 7, "未来一周": 7,
    "下周": 7, "未来七天": 7,
    "未来30天": 30, "一个月": 30, "下月": 30, "未来1个月": 30,
    "未来90天": 90, "3个月": 90, "未来3个月": 90, "一季度": 90,
    "未来一年": 365, "一年": 365, "未来365天": 365,
}

# Event type keyword mapping
_EVENT_KEYWORDS = {
    "launch": ["发射", "升空", "火箭", "卫星", "卫星发射", "火箭发射"],
    "earnings": ["财报", "业绩", "季报", "年报", "盈利", "营收", "净利"],
    "m&a": ["收购", "并购", "重组", "借壳", "分拆"],
    "policy": ["政策", "国常会", "政治局", "中央", "国务院", "新规", "补贴"],
    "regulatory": ["出口管制", "禁令", "制裁", "FDA", "NMPA", "获批", "批准"],
    "data_release": ["CPI", "PPI", "PMI", "GDP", "M2", "社融", "非农", "FOMC", "议息", "LPR"],
    "conference": ["展会", "博览会", "论坛", "峰会", "大会"],
    "product_launch": ["发布", "上市", "推出", "新品", "新车", "新机", "首发"],
}


def _local_nlu(user_query: str) -> dict:
    """Rule-based NLU for when LLM is not available."""
    text = user_query.strip()
    text_lower = text.lower()
    entities: dict = {
        "industries": [],
        "event_types": [],
        "time_horizon": 7,  # default
        "time_keyword": "本周",
        "keywords": [],
        "stock_codes": [],
    }
    confidence = 0.6

    # Extract industries
    for industry, keywords in _INDUSTRY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                if industry not in entities["industries"]:
                    entities["industries"].append(industry)
                if kw not in entities["keywords"]:
                    entities["keywords"].append(kw)
                confidence += 0.05

    # Extract event types
    for etype, keywords in _EVENT_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                if etype not in entities["event_types"]:
                    entities["event_types"].append(etype)
                if kw not in entities["keywords"]:
                    entities["keywords"].append(kw)
                confidence += 0.05

    # Extract time horizon
    for kw, days in sorted(_TIME_KEYWORDS.items(), key=lambda x: -len(x[0])):
        if kw in text:
            entities["time_horizon"] = days
            entities["time_keyword"] = kw
            confidence += 0.1
            break

    # Extract stock codes (6-digit, starts with 0/3/6)
    for m in re.finditer(r"\b([036]\d{5})\b", text):
        code = m.group(1)
        if code not in entities["stock_codes"]:
            entities["stock_codes"].append(code)
            confidence += 0.05

    # Determine intent
    intent = "unknown"
    reasoning = ""

    if any(kw in text for kw in ["报告", "日报", "推送"]):
        intent = "latest_report"
        confidence += 0.2
    elif any(kw in text for kw in ["预测", "会涨", "会跌", "影响"]):
        intent = "price_prediction"
        confidence += 0.15
    elif any(kw in text for kw in ["推荐", "应该关注", "值得看"]):
        intent = "recommend_event"
        confidence += 0.15
    elif any(kw in text for kw in ["最强", "top", "前几", "最大", "前五", "前10"]):
        intent = "top_signals"
        confidence += 0.2
    elif any(kw in text for kw in ["期货", "大宗", "商品"]):
        intent = "today_futures"
        confidence += 0.2
    elif any(kw in text for kw in ["股票", "涨停", "跌停", "异动", "行情", "涨", "跌"]):
        intent = "today_stocks"
        confidence += 0.15
    elif entities["industries"] and any(kw in text for kw in ["最近", "未来", "接下来", "近期", "有什么"]):
        intent = "industry_question"
        confidence += 0.2
    elif any(kw in text for kw in ["事件", "大事"]):
        intent = "upcoming_events"
        confidence += 0.2
    elif entities["event_types"] or entities["industries"] or entities["stock_codes"]:
        intent = "search_event"
        confidence += 0.15
    else:
        intent = "chitchat"
        confidence = 0.3
        reasoning = "未识别到具体意图"

    confidence = min(0.95, confidence)
    return {
        "intent": intent,
        "entities": entities,
        "confidence": confidence,
        "reasoning": reasoning,
    }


# ============================================================================
# Main entry point
# ============================================================================

def parse_query(user_query: str) -> LLMIntent:
    """Parse user query using LLM if available, else local fallback.

    Returns LLMIntent with intent, entities, confidence.
    """
    user_query = (user_query or "").strip()
    if not user_query:
        return LLMIntent(
            intent="unknown", entities={}, confidence=0.0,
            source="local", reasoning="empty query",
        )

    # Try LLM first
    llm_result = call_llm(user_query)
    if llm_result:
        try:
            return LLMIntent(
                intent=llm_result.get("intent", "unknown"),
                entities=llm_result.get("entities", {}),
                confidence=float(llm_result.get("confidence", 0.5)),
                reasoning=llm_result.get("reasoning", ""),
                source="llm",
            )
        except Exception as e:
            logger.warning(f"LLM result parse failed: {e}")

    # Fallback to local
    local = _local_nlu(user_query)
    return LLMIntent(
        intent=local["intent"],
        entities=local["entities"],
        confidence=local["confidence"],
        reasoning=local["reasoning"],
        source="local",
    )