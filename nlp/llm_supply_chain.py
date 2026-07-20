"""
LLM-based structured extraction for the supply-chain workflow.

Wraps the project's MiniMax M2.7 Highspeed endpoint (OpenAI-compatible) with
two helpers used by the pipeline modules:

  * ``extract_event(news)`` — single-pass structured event extraction with a
    strict JSON Schema. Used by WF-02.
  * ``propagate_mismatch(mismatch_summary, candidates)`` — chain-of-thought
    graph propagation, used by WF-03.

The module deliberately avoids writing to the DB or running side effects; that
keeps it cheap to unit-test and easy to mock. Callers (``pipeline.events_extract``
etc.) are responsible for persistence and quality gates.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from loguru import logger


MINIMAX_BASE_URL = "https://api.minimaxi.com/anthropic/v1/messages"
MINIMAX_MODEL = "MiniMax-M2.7-highspeed"

EVENT_SCHEMA_VERSION = "v1"


# ---------------------------------------------------------------------------
# JSON Schema definitions (referenced by the Dify validation node too)
# ---------------------------------------------------------------------------

EVENT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SupplyChainEvent",
    "type": "object",
    "required": [
        "title", "industry_chain", "event_type", "supply_direction",
        "demand_direction", "magnitude", "confidence", "evidence",
        "counter_evidence",
    ],
    "properties": {
        "title": {"type": "string", "minLength": 4, "maxLength": 120},
        "entities": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
        },
        "products": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
        },
        "industry_chain": {"type": "string", "minLength": 2, "maxLength": 64},
        "region": {"type": "string", "maxLength": 64},
        "event_type": {
            "type": "string",
            "enum": [
                "supply_tight", "supply_loose", "demand_pickup", "demand_drop",
                "capacity_expansion", "capacity_cut", "price_move", "policy",
                "regulatory", "other",
            ],
        },
        "supply_direction": {
            "type": "string",
            "enum": ["tight", "loose", "neutral"],
        },
        "demand_direction": {
            "type": "string",
            "enum": ["up", "down", "neutral"],
        },
        "magnitude": {"type": "number", "minimum": 0, "maximum": 10},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "start_at": {"type": ["string", "null"], "format": "date-time"},
        "end_at": {"type": ["string", "null"], "format": "date-time"},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["claim"],
                "properties": {
                    "claim": {"type": "string"},
                    "source": {"type": "string"},
                    "url": {"type": "string"},
                },
            },
        },
        "counter_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["claim"],
                "properties": {
                    "claim": {"type": "string"},
                    "source": {"type": "string"},
                },
            },
        },
        "supply_chain_path": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["from", "to", "kind"],
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "kind": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string", "maxLength": 400},
    },
}


SYSTEM_PROMPT = (
    "你是A股供应链错配分析系统的结构化事件抽取器。"
    "输入是一篇已经过粗筛的中文财经新闻。"
    "请严格按照 JSON Schema 输出一个事件对象：实体、产品、产业环节、地区、"
    "事件类型、供需方向、影响幅度、置信度、证据、反向证据、传导路径。"
    "若信息不足，将 confidence 设为 0.0 并在 counter_evidence 中说明原因。"
    "禁止输出 Schema 之外的字段。"
)


EXTRACTION_TEMPLATE = (
    "新闻标题：{title}\n"
    "发布时间：{published_at}\n"
    "来源：{source}\n"
    "正文摘要：{summary}\n"
    "正文片段：{body}\n\n"
    "请基于以上内容输出严格 JSON，禁止任何 markdown 或解释。"
)


# ---------------------------------------------------------------------------
# Lightweight OpenAI-compatible client (no extra SDK dependency required)
# ---------------------------------------------------------------------------

@dataclass
class LLMResult:
    ok: bool
    data: dict[str, Any]
    raw: str
    error: str = ""


def _api_key() -> str:
    return os.environ.get("MINIMAX_API_KEY", "").strip()


def is_llm_enabled() -> bool:
    return bool(_api_key())


def _coerce_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                elif "content" in item:
                    parts.append(str(item["content"]))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content or "")


def _extract_json_block(text: str) -> str:
    """Tolerantly pull the first JSON object out of a model reply."""
    text = (text or "").strip()
    if not text:
        return ""
    if text.startswith("{") and text.endswith("}"):
        return text
    match = re.search(r"\{[\s\S]*\}", text)
    return match.group(0) if match else text


def call_minimax_json(
    user_prompt: str,
    *,
    system: str = SYSTEM_PROMPT,
    timeout: float = 25.0,
    max_tokens: int = 1200,
    model: str = MINIMAX_MODEL,
    base_url: str = MINIMAX_BASE_URL,
) -> LLMResult:
    """Single-call MiniMax chat completion that must return JSON.

    Returns ``LLMResult`` so the caller can decide whether to retry / degrade.
    """
    api_key = _api_key()
    if not api_key:
        return LLMResult(ok=False, data={}, raw="", error="MINIMAX_API_KEY not set")

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
    }

    try:
        import httpx  # local import keeps startup cost minimal
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                base_url,
                headers={
                    "X-Api-Key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except Exception as exc:
        logger.warning(f"MiniMax call failed: {exc!r}")
        return LLMResult(ok=False, data={}, raw="", error=str(exc))

    if resp.status_code != 200:
        msg = f"http {resp.status_code}: {resp.text[:200]}"
        logger.warning(f"MiniMax non-200: {msg}")
        return LLMResult(ok=False, data={}, raw=resp.text, error=msg)

    try:
        payload = resp.json()
        content = payload.get("content")
        if content is not None:
            text = _coerce_text(content)
        else:
            text = _coerce_text(payload.get("choices", [{}])[0].get("message", {}).get("content", ""))
        json_text = _extract_json_block(text)
        data = json.loads(json_text) if json_text else {}
    except Exception as exc:
        logger.warning(f"MiniMax response parse failed: {exc!r}")
        return LLMResult(ok=False, data={}, raw=resp.text, error=str(exc))

    return LLMResult(ok=True, data=data, raw=text)


# ---------------------------------------------------------------------------
# Schema validation (lightweight, no external lib dependency)
# ---------------------------------------------------------------------------

def _validate_event(data: dict[str, Any]) -> tuple[bool, list[str]]:
    if not isinstance(data, dict):
        return False, ["payload is not an object"]
    errors: list[str] = []
    for key in EVENT_SCHEMA["required"]:
        if key not in data:
            errors.append(f"missing field: {key}")
    et = data.get("event_type")
    if et is not None and et not in EVENT_SCHEMA["properties"]["event_type"]["enum"]:
        errors.append(f"invalid event_type: {et}")
    for field in ("supply_direction", "demand_direction"):
        v = data.get(field)
        if v is not None and v not in EVENT_SCHEMA["properties"][field]["enum"]:
            errors.append(f"invalid {field}: {v}")
    for field in ("magnitude", "confidence"):
        v = data.get(field)
        if isinstance(v, (int, float)):
            if not 0 <= v <= EVENT_SCHEMA["properties"][field]["maximum"]:
                errors.append(f"{field} out of range: {v}")
    evidence = data.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("evidence must be a non-empty list")
    return (not errors), errors


def extract_event(
    news: dict[str, Any],
    *,
    timeout: float = 25.0,
) -> LLMResult:
    """Extract a structured event from one news dict.

    ``news`` keys used: ``title``, ``summary``, ``content``, ``source``,
    ``published_at``.
    """
    prompt = EXTRACTION_TEMPLATE.format(
        title=(news.get("title") or "").strip(),
        summary=(news.get("summary") or "").strip(),
        body=(news.get("content") or news.get("summary") or "")[:1500],
        source=(news.get("source") or news.get("source_label") or "unknown"),
        published_at=str(news.get("published_at") or ""),
    )
    result = call_minimax_json(prompt, timeout=timeout, max_tokens=1200)
    if not result.ok:
        return result
    ok, errors = _validate_event(result.data)
    if not ok:
        return LLMResult(
            ok=False, data=result.data, raw=result.raw,
            error=f"schema: {'; '.join(errors)}",
        )
    return result


# ---------------------------------------------------------------------------
# Heuristic / fallback extractor (used when MINIMAX_API_KEY is absent)
# ---------------------------------------------------------------------------

_INDUSTRY_KEYWORDS = {
    "有色金属": ["铜", "铝", "锌", "镍", "钴", "稀土", "镨钕", "钕铁硼"],
    "钢铁": ["钢铁", "螺纹钢", "铁矿石", "焦煤", "焦炭"],
    "原油石化": ["原油", "石油", "天然气", "LNG", "PX", "PTA", "MEG"],
    "半导体": ["半导体", "芯片", "硅片", "HBM", "DRAM", "NAND", "MLCC", "先进封装"],
    "AI算力": ["AI", "算力", "GPU", "液冷", "HVLP", "铜箔", "PCB", "覆铜板"],
    "光伏": ["光伏", "多晶硅", "硅料", "组件"],
    "锂电": ["锂电", "电池", "碳酸锂", "正极", "电解液", "隔膜"],
    "军工航天": ["航天", "火箭", "卫星", "商业航天", "C919", "航空", "导弹"],
    "消费电子": ["手机", "iPhone", "Mate", "苹果", "华为"],
    "新能源车": ["新能源车", "电动车", "比亚迪", "特斯拉", "小米SU7", "理想", "蔚来"],
    "医药": ["医药", "药", "FDA", "NMPA", "创新药"],
    "化工": ["化工", "TMA", "MMA", "EVA", "POE"],
    "电网": ["变压器", "特高压", "储能", "绿电"],
}


def heuristic_event(news: dict[str, Any]) -> dict[str, Any]:
    """Pure-rule event extractor used when LLM is disabled.

    The output schema matches ``EVENT_SCHEMA`` so downstream code is
    agnostic to which path produced the event.
    """
    text = " ".join([
        str(news.get("title") or ""),
        str(news.get("summary") or ""),
        str(news.get("content") or ""),
    ])
    industry_chain = "其他"
    for name, kws in _INDUSTRY_KEYWORDS.items():
        if any(kw in text for kw in kws):
            industry_chain = name
            break

    supply_direction = "neutral"
    demand_direction = "neutral"
    event_type = "other"
    magnitude = 0.0
    if any(k in text for k in ["紧缺", "短缺", "断供", "供给紧张", "供不应求"]):
        supply_direction = "tight"
        event_type = "supply_tight"
        magnitude = 5.0
    elif any(k in text for k in ["扩产", "新增产能", "投产", "放量"]):
        supply_direction = "loose"
        event_type = "capacity_expansion"
        magnitude = 4.0
    if any(k in text for k in ["涨价", "提价", "上调", "价格大涨"]):
        magnitude = max(magnitude, 4.0)
        if event_type == "other":
            event_type = "price_move"
    if any(k in text for k in ["需求", "订单", "出货", "补库", "采购"]):
        if any(k in text for k in ["增长", "提升", "旺盛", "爆发"]):
            demand_direction = "up"
            magnitude = max(magnitude, 3.0)
        elif any(k in text for k in ["下滑", "放缓", "疲软", "萎缩"]):
            demand_direction = "down"
            magnitude = max(magnitude, 3.0)

    confidence = 0.35 if event_type == "other" else 0.55
    if industry_chain == "其他":
        confidence = min(confidence, 0.4)

    published = news.get("published_at")
    start_at = None
    if isinstance(published, str) and published:
        try:
            start_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            start_at = None
    elif isinstance(published, datetime):
        start_at = published

    summary = (news.get("summary") or news.get("title") or "")[:300]
    return {
        "title": (news.get("title") or "未命名事件")[:120],
        "entities": [],
        "products": [],
        "industry_chain": industry_chain,
        "region": "",
        "event_type": event_type,
        "supply_direction": supply_direction,
        "demand_direction": demand_direction,
        "magnitude": magnitude,
        "confidence": confidence,
        "start_at": start_at.isoformat() if start_at else None,
        "end_at": None,
        "evidence": [
            {
                "claim": summary or news.get("title") or "",
                "source": news.get("source") or news.get("source_label") or "unknown",
                "url": news.get("url") or "",
            }
        ],
        "counter_evidence": [
            {
                "claim": "该事件仅由规则抽取，尚未经过 MiniMax 语义复核与独立反证检索",
                "source": "system",
            }
        ],
        "supply_chain_path": [],
        "summary": summary,
    }


def extract_event_safely(news: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Run ``extract_event`` with heuristic fallback.

    Returns ``(event, source)`` where ``source ∈ {llm, heuristic}``. Never
    raises; on a malformed LLM reply, returns the heuristic event tagged
    with a ``fallback_reason``.
    """
    if is_llm_enabled():
        result = extract_event(news)
        if result.ok:
            return result.data, "llm"
        fallback = heuristic_event(news)
        fallback["counter_evidence"] = fallback.get("counter_evidence", []) + [
            {"claim": f"LLM unavailable: {result.error}", "source": "system"},
        ]
        return fallback, "heuristic"
    return heuristic_event(news), "heuristic"


# ---------------------------------------------------------------------------
# WF-03 helper: produce a propagation summary from a candidate mismatch
# ---------------------------------------------------------------------------

def format_mismatch_prompt(
    *,
    industry_chain: str,
    event_type: str,
    supply_direction: str,
    demand_direction: str,
    candidates: Iterable[dict[str, Any]],
    news_summaries: Iterable[str],
) -> str:
    return (
        f"行业: {industry_chain}\n"
        f"事件类型: {event_type}\n"
        f"供给方向: {supply_direction}\n"
        f"需求方向: {demand_direction}\n"
        f"新闻摘要:\n- " + "\n- ".join(list(news_summaries)[:8]) + "\n\n"
        f"候选股票: {json.dumps(list(candidates)[:30], ensure_ascii=False)}\n\n"
        "请基于以上材料输出严格 JSON，结构为：\n"
        "{\"summary\": \"...\", \"path\": [{\"from\": \"...\", \"to\": \"...\", \"kind\": \"...\"}], "
        "\"beneficiaries\": [\"行业/产品\"], \"at_risk\": [\"行业/产品\"], "
        "\"priced_in\": \"是否已计价及理由\"}。"
    )


def default_event_horizon(hours: int = 24) -> datetime:
    """The freshness window used by WF-03 freshness scoring."""
    return datetime.utcnow() - timedelta(hours=hours)
