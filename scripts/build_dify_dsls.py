"""Generate the WF-02..WF-06, total-control, and Chatflow DSL files.

Run from project root:
    .venv/bin/python scripts/build_dify_dsls.py
"""
from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DSL_DIR = PROJECT_ROOT / "dify"
DSL_DIR.mkdir(exist_ok=True)


COMMON_FEATURES = {
    "file_upload": {"enabled": False},
    "opening_statement": "",
    "retriever_resource": {"enabled": False},
    "sensitive_word_avoidance": {"enabled": False},
    "speech_to_text": {"enabled": False},
    "suggested_questions": [],
    "suggested_questions_after_answer": {"enabled": False},
    "text_to_speech": {"enabled": False},
}


def _base_app(name: str, description: str, icon: str) -> dict:
    return {
        "app": {
            "description": description,
            "icon": icon,
            "icon_background": "#E8F3FF",
            "icon_type": "emoji",
            "mode": "workflow",
            "name": name,
            "use_icon_as_answer_icon": False,
        },
        "dependencies": [],
        "kind": "app",
        "version": "0.6.0",
        "workflow": {
            "conversation_variables": [],
            "environment_variables": [],
            "features": COMMON_FEATURES,
            "graph": {"edges": [], "nodes": [], "viewport": {"x": 0, "y": 0, "zoom": 0.9}},
            "rag_pipeline_variables": [],
        },
    }


def _node(node_type: str, node_id: str, x: int, y: int, **data) -> dict:
    return {
        "data": data,
        "height": 130,
        "id": node_id,
        "position": {"x": x, "y": y},
        "positionAbsolute": {"x": x, "y": y},
        "selected": False,
        "sourcePosition": "right",
        "targetPosition": "left",
        "type": "custom",
        "width": 280,
    }


def _edge(edge_id: str, source: str, target: str, source_type: str, target_type: str) -> dict:
    return {
        "data": {
            "isInIteration": False,
            "isInLoop": False,
            "sourceType": source_type,
            "targetType": target_type,
        },
        "id": edge_id,
        "source": source,
        "sourceHandle": "source",
        "target": target,
        "targetHandle": "target",
        "type": "custom",
        "zIndex": 0,
    }


def _trigger_schedule_node(cron_expression: str, timezone: str = "Asia/Shanghai") -> dict:
    """Build a trigger-schedule node for Dify 1.x.

    The schedule lives inside the workflow (Dify 1.x removed the external
    cron API). The node ID must be ``schedule_trigger`` for the workflow to
    register as a scheduled workflow.
    """
    return _node(
        "trigger-schedule",
        "schedule_trigger",
        80, 100,
        cron_expression=cron_expression,
        mode="cron",
        desc=f"定时触发：{cron_expression} ({timezone})；运行后 Dify 自动写触发记录到 workflow_schedule_plans。",
        frequency=None,
        selected=False,
        timezone=timezone,
        title="定时触发",
        type="trigger-schedule",
        visual_config=None,
    )


# Cron expressions per workflow (mirrors docs/DIFY_SCHEDULE.md). Workdays
# in China use Mon-Fri; Dify 1.x cron supports 5-field standard cron.
CRON_PER_WORKFLOW: dict[str, str] = {
    "daily_workflow": "30 5 * * 1-5",
    "build_morning_report": "20 8 * * 1-5",
    "build_evening_review": "30 20 * * 1-5",
}


def _start_node(pipeline_options: list[str], title: str, default_idem: str) -> dict:
    return _node(
        "custom",
        "start",
        80, 260,
        desc="幂等键建议使用日期+批次+Pipeline 名；Pipeline 由总控工作流统一调用。",
        selected=False,
        title=title,
        type="start",
        variables=[
            {
                "default": pipeline_options[0] if pipeline_options else "",
                "label": "pipeline",
                "options": pipeline_options,
                "required": True,
                "type": "select",
                "variable": "pipeline",
            },
            {
                "default": default_idem,
                "label": "idempotency_key",
                "required": True,
                "type": "text-input",
                "variable": "idempotency_key",
            },
        ],
    )


def _date_node(node_id: str, x: int, y: int) -> dict:
    """Insert a small Python code node that outputs today's date (Asia/Shanghai).

    Cron-driven workflows cannot rely on ``{{#sys.date#}}`` — Dify 1.x does not
    expose ``sys.date`` as a downstream-referenceable variable.  Instead we
    compute it in Python and reference ``{{#date_node.today#}}`` from the
    submit body.
    """
    code = (
        "from datetime import datetime, timezone, timedelta\n"
        "TZ = timezone(timedelta(hours=8))\n"
        "def main() -> dict:\n"
        "    return {'today': datetime.now(TZ).strftime('%Y-%m-%d')}\n"
    )
    return _code_node(
        node_id, x, y,
        code=code,
        title="今日 (Asia/Shanghai)",
        outputs=[{"variable": "today", "value_type": "string"}],
        variables=[],  # no upstream variables
    )


def _submit_node(
    pipeline_options: list[str],
    url_template: str,
    *,
    static_pipeline: str | None = None,
    static_idem_template: str | None = None,
) -> dict:
    """Build the HTTP-request node that submits to the stock API.

    When ``static_pipeline`` is given, both the URL and the body hardcode that
    pipeline name (used in trigger-only workflows where there is no start
    node to read from).

    The idempotency_key body field is left as a Jinja-style reference
    ``{{#date_node.today#}}`` — caller is responsible for inserting a
    date_node upstream and substituting the literal name if desired.
    """
    if static_pipeline:
        pipeline_ref = static_pipeline
        idem_ref = static_idem_template or f"dify:{static_pipeline}:{{{{#date_node.today#}}}}"
        # Replace {{#start.pipeline#}} placeholder with the literal name.
        resolved_url = url_template.replace("{{#start.pipeline#}}", static_pipeline)
    else:
        pipeline_ref = "{{#start.pipeline#}}"
        idem_ref = "{{#start.idempotency_key#}}"
        resolved_url = url_template
    body = {
        "idempotency_key": idem_ref,
        "trigger_source": "dify",
        "pipeline": pipeline_ref,
    }
    if static_pipeline:
        body["business_date"] = "{{#date_node.today#}}"
        body["trade_date"] = "{{#date_node.today#}}"
        if static_pipeline in {"build_morning_report", "build_evening_review"}:
            body["wait_for_completion"] = True
    return _node(
        "http-request",
        "submit",
        430, 260,
        authorization={"type": "no-auth"},
        body={"type": "json", "data": json.dumps(body, ensure_ascii=False)},
        desc="返回 202 + run_id；后台执行；监控通过 /api/v1/runs/{run_id} 查询。",
        headers="Content-Type:application/json",
        method="POST",
        params="",
        retry_config={
            "enabled": True,
            "max_retries": 2,
            "retry_interval": 1000,
            "exponential_backoff": {"enabled": True, "multiplier": 2, "max_interval": 5000},
        },
        timeout={"connect": 5, "read": 60, "write": 30},
        title="提交幂等 Pipeline",
        type="http-request",
        url=resolved_url,
    )


def _quality_gate_node(pipeline_field: str = "status") -> dict:
    """Insert an If/Else node after the HTTP submit."""
    return _node(
        "if-else",
        "quality-gate",
        780, 260,
        cases=[
            {
                "case_id": "pass",
                "logical_operator": "and",
                "conditions": [
                    [
                        {
                            "comparison_operator": "equal",
                            "value_selector": ["submit", "body"],
                            "var_type": "string",
                        },
                        # String contains check is not natively supported by If/Else,
                        # but Dify will evaluate expressions in a Code node. The
                        # simpler approach used here is to rely on the Dify Code
                        # node below to short-circuit.
                    ]
                ],
            }
        ],
        desc="If/Else 质量门禁：依据 status==succeeded && quality_status==pass 继续，否则进入降级分支。",
        selected=False,
        title="质量门禁",
        type="if-else",
    )


def _code_node(
    node_id: str,
    x: int,
    y: int,
    code: str,
    title: str,
    outputs: list[dict],
    variables: list[dict] | None = None,
) -> dict:
    """Build a code node matching Dify 1.x CodeNodeData schema.

    Each variable must include ``variable`` (the local name) and
    ``value_selector`` (e.g. ``["submit", "body"]``). Each output must be a
    dict keyed by output name (not a list), with ``type`` and optional
    ``children``.
    """
    default_var = [{"variable": "submit_body", "value_selector": ["submit", "body"]}]
    outputs_dict: dict[str, dict] = {}
    for i, out in enumerate(outputs or []):
        name = out.get("variable") or f"output_{i}"
        outputs_dict[name] = {"type": out.get("value_type") or "string"}
    data = {
        "code": code,
        "code_language": "python3",
        "desc": title,
        "outputs": outputs_dict,
        "selected": False,
        "title": title,
        "type": "code",
        "variables": variables if variables is not None else default_var,
    }
    return _node("code", node_id, x, y, **data)


def _end_node(outputs: list[dict]) -> dict:
    return _node(
        "end", "end", 1500, 260,
        desc="总控工作流根据 HTTP 状态和返回的 run_id 进入监控阶段。",
        outputs=outputs,
        selected=False,
        title="提交结果",
        type="end",
    )


def write_workflow_dsl(
    name: str,
    description: str,
    icon: str,
    pipeline_options: list[str],
    extra_nodes: list[dict] | None = None,
    extra_edges: list[dict] | None = None,
    end_outputs: list[dict] | None = None,
    filename: str | None = None,
    default_idem: str = "manual-please-change",
    cron_expression: str | None = None,
    timezone: str = "Asia/Shanghai",
) -> Path:
    doc = _base_app(name, description, icon)
    submit_url = "http://host.docker.internal:8000/api/v1/pipeline/{{#start.pipeline#}}"
    nodes: list[dict] = []
    edges: list[dict] = []

    if cron_expression:
        # Dify 1.x: trigger-only workflows. The pipeline name lives as a
        # constant on the submit node; the user cannot change it from the UI.
        # Drop the start node entirely (Dify rejects start+trigger co-existence).
        schedule_node = _trigger_schedule_node(cron_expression, timezone=timezone)
        nodes.append(schedule_node)
        # Compute today's date in Asia/Shanghai — Dify 1.x does not expose
        # {{#sys.date#}} as a referenceable variable, so we synthesise one.
        nodes.append(_date_node("date_node", 240, 260))
        primary = pipeline_options[0] if pipeline_options else ""
        nodes.append(_submit_node(
            pipeline_options,
            submit_url,
            static_pipeline=primary,
            static_idem_template=f"dify:{primary}:{{{{#date_node.today#}}}}",
        ))
        edges.append(_edge(
            "schedule-date", "schedule_trigger", "date_node",
            "trigger-schedule", "code",
        ))
        edges.append(_edge(
            "date-submit", "date_node", "submit",
            "code", "http-request",
        ))
        edges.append(_edge("submit-end", "submit", "end", "http-request", "end"))
    else:
        # Manual / orchestrator: keep the start node so users can override
        # the pipeline choice at run time.
        nodes.append(_start_node(pipeline_options, "调度参数", default_idem))
        nodes.append(_submit_node(pipeline_options, submit_url))
        edges.append(_edge(
            "start-submit", "start", "submit", "start", "http-request",
        ))
        edges.append(_edge("submit-end", "submit", "end", "http-request", "end"))

    if extra_nodes:
        nodes.extend(extra_nodes)
    if extra_edges:
        edges.extend(extra_edges)
    doc["workflow"]["graph"]["nodes"] = nodes
    doc["workflow"]["graph"]["edges"] = edges
    end = _end_node(end_outputs or [
        {"value_selector": ["submit", "status_code"], "value_type": "number", "variable": "http_status"},
        {"value_selector": ["submit", "body"], "value_type": "string", "variable": "run_receipt"},
    ])
    nodes = [n for n in doc["workflow"]["graph"]["nodes"] if n["id"] != "end"]
    nodes.append(end)
    doc["workflow"]["graph"]["nodes"] = nodes
    out = DSL_DIR / (filename or f"{name}.yml")
    out.write_text(_to_yaml(doc), encoding="utf-8")
    return out


def _to_yaml(doc: dict) -> str:
    """Hand-rolled YAML writer: avoids requiring PyYAML.

    Dify imports work with simple key:value blocks, and our payloads only
    contain scalars, lists, and dicts — all of which round-trip cleanly with
    JSON syntax, which YAML accepts as a superset for our purposes.
    """
    return json.dumps(doc, ensure_ascii=False, indent=2)


def build_wf02() -> Path:
    """WF-02 — 新闻事件抽取与证据聚类。"""
    code = (
        "import json\n"
        "def main(submit_body: str) -> dict:\n"
        "    try:\n"
        "        data = json.loads(submit_body)\n"
        "    except Exception:\n"
        "        data = {}\n"
        "    status = data.get('status') or 'unknown'\n"
        "    quality = data.get('quality_status') or 'unknown'\n"
        "    passed = status == 'succeeded' and quality == 'pass'\n"
        "    return {\n"
        "        'status': status,\n"
        "        'quality_status': quality,\n"
        "        'extracted': data.get('extracted', 0),\n"
        "        'candidates': data.get('candidates', 0),\n"
        "        'gate_passed': passed,\n"
        "    }\n"
    )
    code_outputs = [
        {"value_selector": ["gate", "status"], "value_type": "string", "variable": "status"},
        {"value_selector": ["gate", "quality_status"], "value_type": "string", "variable": "quality_status"},
        {"value_selector": ["gate", "extracted"], "value_type": "number", "variable": "extracted"},
        {"value_selector": ["gate", "candidates"], "value_type": "number", "variable": "candidates"},
        {"value_selector": ["gate", "gate_passed"], "value_type": "boolean", "variable": "gate_passed"},
    ]
    gate_node = _code_node("gate", 780, 260, code, "门禁：status/quality", code_outputs)
    degrade_node = _code_node(
        "degrade", 1130, 360,
        code=(
            "def main(submit_body: str) -> dict:\n"
            "    try:\n"
            "        import json as _j\n"
            "        data = _j.loads(submit_body) if submit_body else {}\n"
            "    except Exception:\n"
            "        data = {}\n"
            "    candidates = data.get('candidates', 0)\n"
            "    extracted = data.get('extracted', 0)\n"
            "    return {\n"
            "        'degraded': True,\n"
            "        'message': f'数据不足：候选 {candidates}, 解析 {extracted}; 不输出买入建议',\n"
            "    }\n"
        ),
        title="降级：数据不足报告",
        outputs=[
            {"variable": "degraded", "value_type": "boolean"},
            {"variable": "message", "value_type": "string"},
        ],
    )
    extra_nodes = [gate_node, degrade_node]
    extra_edges = [
        _edge("submit-gate", "submit", "gate", "http-request", "code"),
        _edge("gate-degrade", "gate", "degrade", "code", "code"),
        _edge("gate-end", "gate", "end", "code", "end"),
    ]
    return write_workflow_dsl(
        name="02_news事件抽取",
        description="WF-02 — Python 完成候选筛选后调 MiniMax 做结构化事件抽取，校验 JSON Schema 并写入 storage_events。",
        icon="📰",
        pipeline_options=["extract_events"],
        extra_nodes=[],
        extra_edges=[],
        default_idem="dify:wf02:{{#sys.date#}}",
        cron_expression=None,
    )


def build_wf03() -> Path:
    """WF-03 — 供应链错配与图谱传播。"""
    code = (
        "import json\n"
        "def main(submit_body: str) -> dict:\n"
        "    try:\n"
        "        data = json.loads(submit_body)\n"
        "    except Exception:\n"
        "        data = {}\n"
        "    status = data.get('status') or 'unknown'\n"
        "    quality = data.get('quality_status') or 'unknown'\n"
        "    return {\n"
        "        'status': status,\n"
        "        'quality_status': quality,\n"
        "        'gate_passed': status == 'succeeded' and quality == 'pass',\n"
        "        'summary': data.get('summary') or '',\n"
        "    }\n"
    )
    code_outputs = [
        {"value_selector": ["gate", "status"], "value_type": "string", "variable": "status"},
        {"value_selector": ["gate", "quality_status"], "value_type": "string", "variable": "quality_status"},
        {"value_selector": ["gate", "gate_passed"], "value_type": "boolean", "variable": "gate_passed"},
        {"value_selector": ["gate", "summary"], "value_type": "string", "variable": "summary"},
    ]
    gate_node = _code_node("gate", 780, 260, code, "门禁：错配综合分", code_outputs)
    return write_workflow_dsl(
        name="03_错配图谱传播",
        description="WF-03 — 聚合 StorageEvent 错配信号，叠加知识图谱与价格/库存证据，输出 MismatchResult。",
        icon="🔗",
        pipeline_options=["detect_mismatch"],
        extra_nodes=[],
        extra_edges=[],
        default_idem="dify:wf03:{{#sys.date#}}",
        cron_expression=None,
    )


def build_wf04() -> Path:
    """WF-04 — 行业与股票评分。"""
    code = (
        "import json\n"
        "def main(submit_body: str) -> dict:\n"
        "    try:\n"
        "        data = json.loads(submit_body)\n"
        "    except Exception:\n"
        "        data = {}\n"
        "    status = data.get('status') or 'unknown'\n"
        "    quality = data.get('quality_status') or 'unknown'\n"
        "    return {\n"
        "        'status': status,\n"
        "        'quality_status': quality,\n"
        "        'gate_passed': status == 'succeeded' and quality == 'pass',\n"
        "        'summary': data.get('summary') or '',\n"
        "    }\n"
    )
    code_outputs = [
        {"value_selector": ["gate", "status"], "value_type": "string", "variable": "status"},
        {"value_selector": ["gate", "quality_status"], "value_type": "string", "variable": "quality_status"},
        {"value_selector": ["gate", "gate_passed"], "value_type": "boolean", "variable": "gate_passed"},
        {"value_selector": ["gate", "summary"], "value_type": "string", "variable": "summary"},
    ]
    gate_node = _code_node("gate", 780, 260, code, "门禁：评分通过", code_outputs)
    return write_workflow_dsl(
        name="04_评分",
        description="WF-04 — 使用文档 §5 权重（证据20/多源15/供需20/价格15/图谱15/时效10/可交易5）评估候选股票，输出 StockScore。",
        icon="📊",
        pipeline_options=["score_candidates"],
        extra_nodes=[],
        extra_edges=[],
        default_idem="dify:wf04:{{#sys.date#}}",
        cron_expression=None,
    )


def build_wf05() -> Path:
    """WF-05 — 持仓诊断与早报推送。"""
    code = (
        "import json\n"
        "def main(submit_body: str) -> dict:\n"
        "    try:\n"
        "        data = json.loads(submit_body)\n"
        "    except Exception:\n"
        "        data = {}\n"
        "    status = data.get('status') or 'unknown'\n"
        "    quality = data.get('quality_status') or 'unknown'\n"
        "    return {\n"
        "        'status': status,\n"
        "        'quality_status': quality,\n"
        "        'gate_passed': status == 'succeeded' and quality == 'pass',\n"
        "        'summary': data.get('summary') or '',\n"
        "    }\n"
    )
    code_outputs = [
        {"value_selector": ["gate", "status"], "value_type": "string", "variable": "status"},
        {"value_selector": ["gate", "quality_status"], "value_type": "string", "variable": "quality_status"},
        {"value_selector": ["gate", "gate_passed"], "value_type": "boolean", "variable": "gate_passed"},
        {"value_selector": ["gate", "summary"], "value_type": "string", "variable": "summary"},
    ]
    gate_node = _code_node("gate", 780, 260, code, "门禁：诊断成功", code_outputs)
    return write_workflow_dsl(
        name="05_持仓诊断早报",
        description="WF-05 — 拉取 PortfolioDiagnosis 与 StockScore，生成早报 Markdown 与飞书卡片，08:20 推送。",
        icon="🟢",
        pipeline_options=["build_morning_report"],
        extra_nodes=[],
        extra_edges=[],
        default_idem="dify:wf05:{{#sys.date#}}",
        cron_expression=CRON_PER_WORKFLOW["build_morning_report"],
    )


def build_wf06() -> Path:
    """WF-06 — 盘后复盘。"""
    code = (
        "import json\n"
        "def main(submit_body: str) -> dict:\n"
        "    try:\n"
        "        data = json.loads(submit_body)\n"
        "    except Exception:\n"
        "        data = {}\n"
        "    status = data.get('status') or 'unknown'\n"
        "    quality = data.get('quality_status') or 'unknown'\n"
        "    return {\n"
        "        'status': status,\n"
        "        'quality_status': quality,\n"
        "        'gate_passed': status == 'succeeded' and quality == 'pass',\n"
        "        'summary': data.get('summary') or '',\n"
        "    }\n"
    )
    code_outputs = [
        {"value_selector": ["gate", "status"], "value_type": "string", "variable": "status"},
        {"value_selector": ["gate", "quality_status"], "value_type": "string", "variable": "quality_status"},
        {"value_selector": ["gate", "gate_passed"], "value_type": "boolean", "variable": "gate_passed"},
        {"value_selector": ["gate", "summary"], "value_type": "string", "variable": "summary"},
    ]
    gate_node = _code_node("gate", 780, 260, code, "门禁：复盘成功", code_outputs)
    return write_workflow_dsl(
        name="06_盘后复盘",
        description="WF-06 — 比对当日持仓动作与价格，输出验证/反向/观点变化/偏差归因，20:30 推送飞书摘要。",
        icon="🌙",
        pipeline_options=["build_evening_review"],
        extra_nodes=[],
        extra_edges=[],
        default_idem="dify:wf06:{{#sys.date#}}",
        cron_expression=CRON_PER_WORKFLOW["build_evening_review"],
    )


def build_orchestrator() -> Path:
    """00_stock总控工作流 — Dify 唯一主调度器串联所有子工作流。"""
    submit_url = "http://host.docker.internal:8000/api/v1/pipeline/extract_events"
    # Single-pipeline orchestrator node (just dispatches extract_events). Real
    # orchestration uses Dify scheduled triggers; this DSL documents the
    # contract that all 5 sub-flows use.
    return write_workflow_dsl(
        name="00_stock总控工作流",
        description="总控：手动触发每日串行生产链；定时生产入口为 01_stock每日根采集。",
        icon="🧭",
        pipeline_options=["daily_workflow"],
        default_idem="dify:orchestrator:{{#sys.date#}}",
    )


def build_daily_root() -> Path:
    """Only scheduled morning research chain in Dify production."""
    return write_workflow_dsl(
        name="01_stock每日根采集",
        description="工作日 05:30 提交每日串行研究总链；每个子步骤均生成可审计 PipelineRun。",
        icon="🛰️",
        pipeline_options=["daily_workflow"],
        default_idem="dify:daily_workflow:{{#sys.date#}}",
        cron_expression=CRON_PER_WORKFLOW["daily_workflow"],
        filename="01_stock每日根采集.yml",
    )


def main() -> None:
    outputs = [
        build_daily_root(),
        build_wf02(),
        build_wf03(),
        build_wf04(),
        build_wf05(),
        build_wf06(),
        build_orchestrator(),
    ]
    for p in outputs:
        print(f"wrote {p.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
