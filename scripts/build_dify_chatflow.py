"""Generate the CF-01 Chatflow DSL (Dify conversation flow).

CF-01 is the user-facing conversational entrypoint: it pulls the latest
PortfolioDiagnosis + MorningReport + PipelineRun status from stock API
and answers questions like "今天运行正常吗" or "为什么建议加仓 X".
"""
from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DSL_DIR = PROJECT_ROOT / "dify"


def build_chatflow() -> Path:
    name = "cf_01_供应链投资控制台"
    description = (
        "CF-01 — 用户私聊入口；查询运行状态、持仓诊断、候选机会、重跑工作流。"
        "所有问题由 stock API 提供事实数据，避免 LLM 编造。"
    )

    # CHATFLOW differs from WORKFLOW: conversation_variables, opening_statement,
    # suggested_questions and 'question-classifier' are the typical structure.
    doc = {
        "app": {
            "description": description,
            "icon": "💬",
            "icon_background": "#FFE7E7",
            "icon_type": "emoji",
            "mode": "advanced-chat",
            "name": name,
            "use_icon_as_answer_icon": False,
        },
        "dependencies": [],
        "kind": "app",
        "version": "0.6.0",
        "workflow": {
            "conversation_variables": [
                {"name": "user_id", "value_type": "string", "description": "默认 default", "value": "default"},
            ],
            "environment_variables": [],
            "features": {
                "file_upload": {"enabled": False},
                "opening_statement": "你好，我是你的供应链投资研究助手。",
                "retriever_resource": {"enabled": False},
                "sensitive_word_avoidance": {"enabled": False},
                "speech_to_text": {"enabled": False},
                "suggested_questions": [
                    "今天运行正常吗？",
                    "我的持仓最新诊断是什么？",
                    "为什么建议加仓 300502？",
                    "重跑 WF-04 评分",
                ],
                "suggested_questions_after_answer": {"enabled": True},
                "text_to_speech": {"enabled": False},
            },
            "graph": {
                "edges": [
                    {
                        "data": {"isInIteration": False, "isInLoop": False, "sourceType": "start", "targetType": "question-classifier"},
                        "id": "start-classify",
                        "source": "start",
                        "sourceHandle": "source",
                        "target": "classify",
                        "targetHandle": "target",
                        "type": "custom",
                        "zIndex": 0,
                    },
                    {
                        "data": {"isInIteration": False, "isInLoop": False, "sourceType": "question-classifier", "targetType": "http-request"},
                        "id": "classify-health",
                        "source": "classify",
                        "sourceHandle": "health",
                        "target": "health",
                        "targetHandle": "target",
                        "type": "custom",
                        "zIndex": 0,
                    },
                    {
                        "data": {"isInIteration": False, "isInLoop": False, "sourceType": "http-request", "targetType": "answer"},
                        "id": "health-answer",
                        "source": "health",
                        "sourceHandle": "source",
                        "target": "answer",
                        "targetHandle": "target",
                        "type": "custom",
                        "zIndex": 0,
                    },
                    {
                        "data": {"isInIteration": False, "isInLoop": False, "sourceType": "question-classifier", "targetType": "http-request"},
                        "id": "classify-portfolio",
                        "source": "classify",
                        "sourceHandle": "portfolio",
                        "target": "portfolio",
                        "targetHandle": "target",
                        "type": "custom",
                        "zIndex": 0,
                    },
                    {
                        "data": {"isInIteration": False, "isInLoop": False, "sourceType": "http-request", "targetType": "answer"},
                        "id": "portfolio-answer",
                        "source": "portfolio",
                        "sourceHandle": "source",
                        "target": "answer",
                        "targetHandle": "target",
                        "type": "custom",
                        "zIndex": 0,
                    },
                    {
                        "data": {"isInIteration": False, "isInLoop": False, "sourceType": "question-classifier", "targetType": "http-request"},
                        "id": "classify-rerun",
                        "source": "classify",
                        "sourceHandle": "rerun",
                        "target": "rerun",
                        "targetHandle": "target",
                        "type": "custom",
                        "zIndex": 0,
                    },
                    {
                        "data": {"isInIteration": False, "isInLoop": False, "sourceType": "http-request", "targetType": "answer"},
                        "id": "rerun-answer",
                        "source": "rerun",
                        "sourceHandle": "source",
                        "target": "answer",
                        "targetHandle": "target",
                        "type": "custom",
                        "zIndex": 0,
                    },
                    {
                        "data": {"isInIteration": False, "isInLoop": False, "sourceType": "question-classifier", "targetType": "llm"},
                        "id": "classify-other",
                        "source": "classify",
                        "sourceHandle": "other",
                        "target": "fallback",
                        "targetHandle": "target",
                        "type": "custom",
                        "zIndex": 0,
                    },
                    {
                        "data": {"isInIteration": False, "isInLoop": False, "sourceType": "llm", "targetType": "answer"},
                        "id": "fallback-answer",
                        "source": "fallback",
                        "sourceHandle": "source",
                        "target": "answer",
                        "targetHandle": "target",
                        "type": "custom",
                        "zIndex": 0,
                    },
                ],
                "nodes": [
                    {
                        "data": {
                            "desc": "Chatflow 入口；sys.query 由 Dify 注入。",
                            "selected": False,
                            "title": "用户问题",
                            "type": "start",
                            "variables": [
                                {
                                    "default": "",
                                    "label": "user_id",
                                    "required": False,
                                    "type": "text-input",
                                    "variable": "user_id",
                                }
                            ],
                        },
                        "height": 90,
                        "id": "start",
                        "position": {"x": 80, "y": 260},
                        "positionAbsolute": {"x": 80, "y": 260},
                        "selected": False,
                        "sourcePosition": "right",
                        "targetPosition": "left",
                        "type": "custom",
                        "width": 240,
                    },
                    {
                        "data": {
                            "desc": "问题分类：健康 / 持仓 / 重跑 / 其他。",
                            "instruction": "根据用户问题归类：health=查询运行/质量；portfolio=查询持仓诊断或推荐；rerun=重跑子工作流；other=其他。",
                            "model": {
                                "provider": "langgenius/minimax/minimax",
                                "name": "MiniMax-M2.7-highspeed",
                                "mode": "chat",
                                "completion_params": {"temperature": 0.0, "max_tokens": 64},
                            },
                            "query_variable_selector": ["sys", "query"],
                            "selected": False,
                            "title": "问题分类",
                            "topics": [
                                {"id": "health", "name": "运行监控"},
                                {"id": "portfolio", "name": "持仓诊断"},
                                {"id": "rerun", "name": "重跑子工作流"},
                                {"id": "other", "name": "其他"},
                            ],
                            "type": "question-classifier",
                            "vision": {"enabled": False},
                        },
                        "height": 160,
                        "id": "classify",
                        "position": {"x": 380, "y": 260},
                        "positionAbsolute": {"x": 380, "y": 260},
                        "selected": False,
                        "sourcePosition": "right",
                        "targetPosition": "left",
                        "type": "custom",
                        "width": 260,
                    },
                    {
                        "data": {
                            "authorization": {"type": "no-auth"},
                            "body": {"type": "json", "data": "{}"},
                            "desc": "调用 /api/v1/health/data 拉取最新质量状态。",
                            "headers": "Content-Type:application/json",
                            "method": "GET",
                            "params": "",
                            "retry_config": {"enabled": True, "max_retries": 2, "retry_interval": 1000, "exponential_backoff": {"enabled": False}},
                            "timeout": {"connect": 5, "read": 15, "write": 15},
                            "title": "拉取运行质量",
                            "type": "http-request",
                            "url": "http://host.docker.internal:8000/api/v1/health/data",
                        },
                        "height": 120,
                        "id": "health",
                        "position": {"x": 720, "y": 100},
                        "positionAbsolute": {"x": 720, "y": 100},
                        "selected": False,
                        "sourcePosition": "right",
                        "targetPosition": "left",
                        "type": "custom",
                        "width": 260,
                    },
                    {
                        "data": {
                            "authorization": {"type": "no-auth"},
                            "body": {"type": "json", "data": "{}"},
                            "desc": "调用 /api/v1/portfolio/default 返回持仓。",
                            "headers": "Content-Type:application/json",
                            "method": "GET",
                            "params": "",
                            "retry_config": {"enabled": True, "max_retries": 2, "retry_interval": 1000, "exponential_backoff": {"enabled": False}},
                            "timeout": {"connect": 5, "read": 15, "write": 15},
                            "title": "拉取持仓",
                            "type": "http-request",
                            "url": "http://host.docker.internal:8000/api/v1/portfolio/default",
                        },
                        "height": 120,
                        "id": "portfolio",
                        "position": {"x": 720, "y": 280},
                        "positionAbsolute": {"x": 720, "y": 280},
                        "selected": False,
                        "sourcePosition": "right",
                        "targetPosition": "left",
                        "type": "custom",
                        "width": 260,
                    },
                    {
                        "data": {
                            "authorization": {"type": "no-auth"},
                            "body": {
                                "type": "json",
                                "data": "{\"idempotency_key\":\"cf01:{{#sys.user_id#}}:{{#sys.timestamp#}}\",\"trigger_source\":\"dify_chat\"}",
                            },
                            "desc": "用户要求重跑时调用对应 Pipeline。",
                            "headers": "Content-Type:application/json",
                            "method": "POST",
                            "params": "",
                            "retry_config": {"enabled": True, "max_retries": 2, "retry_interval": 1000, "exponential_backoff": {"enabled": False}},
                            "timeout": {"connect": 5, "read": 30, "write": 30},
                            "title": "触发重跑",
                            "type": "http-request",
                            "url": "http://host.docker.internal:8000/api/v1/pipeline/score_candidates",
                        },
                        "height": 120,
                        "id": "rerun",
                        "position": {"x": 720, "y": 460},
                        "positionAbsolute": {"x": 720, "y": 460},
                        "selected": False,
                        "sourcePosition": "right",
                        "targetPosition": "left",
                        "type": "custom",
                        "width": 260,
                    },
                    {
                        "data": {
                            "desc": "兜底：自由回答时引用上一步 HTTP 返回值。",
                            "model": {
                                "provider": "langgenius/minimax/minimax",
                                "name": "MiniMax-M2.7-highspeed",
                                "mode": "chat",
                                "completion_params": {"temperature": 0.2, "max_tokens": 600},
                            },
                            "prompt_template": [
                                {"role": "system", "text": "你是用户的供应链投资研究助手。请基于 stock API 返回的事实数据回答，不要编造。如果上下文不足请明确告诉用户。"},
                                {"role": "user", "text": "{{#sys.query#}}"},
                            ],
                            "selected": False,
                            "title": "兜底 LLM",
                            "type": "llm",
                            "vision": {"enabled": False},
                        },
                        "height": 130,
                        "id": "fallback",
                        "position": {"x": 1080, "y": 460},
                        "positionAbsolute": {"x": 1080, "y": 460},
                        "selected": False,
                        "sourcePosition": "right",
                        "targetPosition": "left",
                        "type": "custom",
                        "width": 260,
                    },
                    {
                        "data": {
                            "answer": "{{#classify.topic_id#}} 分支结果",
                            "desc": "直接回答用户。",
                            "selected": False,
                            "title": "回答",
                            "type": "answer",
                            "variables": [],
                        },
                        "height": 90,
                        "id": "answer",
                        "position": {"x": 1430, "y": 260},
                        "positionAbsolute": {"x": 1430, "y": 260},
                        "selected": False,
                        "sourcePosition": "right",
                        "targetPosition": "left",
                        "type": "custom",
                        "width": 240,
                    },
                ],
                "viewport": {"x": 0, "y": 0, "zoom": 0.85},
            },
            "rag_pipeline_variables": [],
        },
    }

    out = DSL_DIR / "cf_01_投资控制台.yml"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    out = build_chatflow()
    print(f"wrote {out.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()