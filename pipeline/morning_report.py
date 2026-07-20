"""Pipeline: WF-05 (continued) — build the morning brief + Feishu card.

This module is the second half of WF-05: it consumes
``PortfolioDiagnosis`` (WF-05 first stage) and ``StockScore`` (WF-04) and
emits a ``MorningReport`` with a Markdown body, a Feishu interactive card,
and a `run_id` de-duplication record.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, date as date_cls
from typing import Any

from loguru import logger

from accounts.portfolio import DEFAULT_USER_ID, get_portfolio
from storage import get_db
from storage.models import (
    FeishuPush,
    MismatchResult,
    MorningReport,
    PipelineRun,
    PortfolioDiagnosis,
    StockScore,
)


CARD_HEADER = "🟢 早报 {date}"
CARD_COLOR = "green"


def _parse_date(s: str | None) -> date_cls:
    if not s:
        return datetime.utcnow().date()
    try:
        return date_cls.fromisoformat(s)
    except ValueError:
        return datetime.utcnow().date()


def _top_candidates(db, td: date_cls, limit: int = 5) -> list[StockScore]:
    with db.session() as s:
        qualified = (
            s.query(PipelineRun)
            .filter(PipelineRun.pipeline == "score_candidates")
            .filter(PipelineRun.business_date == td)
            .order_by(PipelineRun.created_at.desc())
            .first()
        )
        if qualified is None or not (
            qualified.status == "succeeded" and qualified.quality_status == "pass"
        ):
            return []
        return (
            s.query(StockScore)
            .filter(StockScore.trade_date == td)
            .filter(StockScore.hard_filter_passed.is_(True))
            .filter(StockScore.final_score >= 60)
            .order_by(StockScore.final_score.desc())
            .limit(limit)
            .all()
        )


def _build_markdown(portfolio: dict[str, Any], diagnoses: list[PortfolioDiagnosis], top: list[StockScore]) -> str:
    lines: list[str] = []
    td = datetime.utcnow().date().isoformat()
    lines.append(f"# 早报 {td}")
    lines.append("")
    lines.append(f"总资产 ¥{portfolio['total_assets']:.0f}，持仓市值 ¥{portfolio['invested_market_value']:.0f}，"
                 f"现金 ¥{portfolio['cash']:.0f}（占比 {portfolio['invested_pct']*100:.1f}%）")
    lines.append("")

    lines.append("## 持仓诊断")
    lines.append("")
    for d in diagnoses:
        marker = {"add": "➕", "hold": "🟰", "trim": "➖", "exit": "⛔"}.get(d.action, "·")
        reasons = json.loads(d.reasons_json or "[]")
        lines.append(
            f"- {marker} **{d.name}({d.code})** · {d.action} · "
            f"回撤={d.drawdown_state} · 桶暴露 {d.bucket_exposure_pct*100:.1f}%"
        )
        for r in reasons[:2]:
            lines.append(f"    - {r}")
    lines.append("")

    if top:
        lines.append("## 候选机会（评分≥60 且通过硬过滤）")
        lines.append("")
        for s in top:
            lines.append(
                f"- **{s.name}({s.code})** 方向={s.direction} 综合分={s.final_score:.1f} "
                f"催化={s.catalyst_window} 观察={s.observe_range} 介入={s.entry_range} 止损={s.stop_loss:.2f}"
            )
            if s.invalidation:
                lines.append(f"    - 失效条件：{s.invalidation}")
        lines.append("")

    if portfolio.get("risk_buckets"):
        lines.append("## 风险桶")
        lines.append("")
        for b in portfolio["risk_buckets"][:6]:
            lines.append(f"- {b['name']}: ¥{b['market_value']:.0f} ({b['weight']*100:.1f}%)")
        lines.append("")

    lines.append("> 评分拆解: 证据20 / 多源15 / 供需20 / 价格15 / 图谱15 / 时效10 / 可交易5")
    return "\n".join(lines)


def _build_feishu_card(
    portfolio: dict[str, Any],
    diagnoses: list[PortfolioDiagnosis],
    top: list[StockScore],
    cash_suggestion: str,
) -> dict[str, Any]:
    td = datetime.utcnow().date().isoformat()
    elements: list[dict[str, Any]] = []

    # Header summary
    summary_text = (
        f"**总资产** ¥{portfolio['total_assets']:.0f}　"
        f"**持仓** ¥{portfolio['invested_market_value']:.0f}　"
        f"**现金** ¥{portfolio['cash']:.0f}\n"
        f"**回撤容忍** {portfolio['max_drawdown_tolerance']*100:.0f}%　"
        f"**风险桶数** {len(portfolio.get('risk_buckets', []))}"
    )
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": summary_text}})

    elements.append({"tag": "hr"})

    # Diagnosis list
    diag_lines = ["**持仓动作**"]
    for d in diagnoses:
        marker = {"add": "➕", "hold": "🟰", "trim": "➖", "exit": "⛔"}.get(d.action, "·")
        diag_lines.append(
            f"{marker} {d.name}({d.code}) · {d.action} · "
            f"回撤={d.drawdown_state} · 桶 {d.bucket_exposure_pct*100:.1f}%"
        )
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(diag_lines)}})

    if top:
        elements.append({"tag": "hr"})
        cand_lines = ["**候选机会（评分≥60）**"]
        for s in top[:5]:
            cand_lines.append(
                f"• {s.name}({s.code}) {s.direction} 评分 {s.final_score:.1f}\n"
                f"  观察 {s.observe_range} · 介入 {s.entry_range} · 止损 {s.stop_loss:.2f}\n"
                f"  失效: {s.invalidation}"
            )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(cand_lines)}})

    elements.append({"tag": "hr"})
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**现金建议**: {cash_suggestion}"}})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": CARD_COLOR,
                "title": {
                    "tag": "plain_text",
                    "content": CARD_HEADER.format(date=td),
                },
            },
            "elements": elements,
        },
    }


def _cash_suggestion(portfolio: dict[str, Any], diagnoses: list[PortfolioDiagnosis]) -> str:
    cash_pct = portfolio["cash"] / portfolio["total_assets"] if portfolio["total_assets"] else 0
    bucket_weights = sorted(
        portfolio.get("risk_buckets", []), key=lambda b: -b["weight"]
    )
    high_concentration = bucket_weights and bucket_weights[0]["weight"] > 0.30
    note = []
    if cash_pct < 0.05:
        note.append("现金低于 5%，建议减仓再平衡")
    elif cash_pct > 0.20:
        note.append("现金偏高，可分批介入评分≥60 候选")
    else:
        note.append("现金比例合理")
    if high_concentration:
        note.append(
            f"风险桶 {bucket_weights[0]['name']} 占比 "
            f"{bucket_weights[0]['weight']*100:.1f}%，属于软预警"
        )
    add_targets = [d for d in diagnoses if d.action == "add"]
    if add_targets:
        names = "、".join(f"{d.name}({d.code})" for d in add_targets[:3])
        note.append(f"可加分批: {names}")
    return "；".join(note)


def _send_feishu(card: dict[str, Any], run_id: str, payload_kind: str = "morning") -> tuple[bool, str]:
    from notifier.feishu import FeishuNotifier
    notifier = FeishuNotifier()
    chats = notifier.list_chats(enabled_only=True)
    if not chats:
        return False, "no enabled chats"
    ok_any = False
    last_err = ""
    for chat in chats:
        with get_db().tx() as s:
            existing = (
                s.query(FeishuPush)
                .filter(FeishuPush.run_id == run_id)
                .filter(FeishuPush.payload_kind == payload_kind)
                .filter(FeishuPush.chat_id == chat.chat_id)
                .one_or_none()
            )
            if existing is not None and existing.success:
                logger.info(f"[feishu] already pushed run_id={run_id} chat={chat.chat_id}")
                ok_any = True
                continue
        ok = notifier.send_with_retry(card, chat_id=chat.chat_id)
        with get_db().tx() as s:
            row = (
                s.query(FeishuPush)
                .filter(FeishuPush.run_id == run_id)
                .filter(FeishuPush.payload_kind == payload_kind)
                .filter(FeishuPush.chat_id == chat.chat_id)
                .one_or_none()
            )
            if row is None:
                row = FeishuPush(run_id=run_id, payload_kind=payload_kind, chat_id=chat.chat_id)
                s.add(row)
            row.success = ok
            row.payload_hash = hashlib.sha1(json.dumps(card, ensure_ascii=False).encode()).hexdigest()
            row.error = "" if ok else "send failed after 3 attempts"
        if ok:
            ok_any = True
        else:
            last_err = "send failed after 3 attempts"
    return ok_any, last_err


def build(
    *,
    user_id: str = DEFAULT_USER_ID,
    trade_date: str = "",
    push: bool = True,
) -> dict[str, Any]:
    db = get_db()
    try:
        portfolio = get_portfolio(user_id)
    except KeyError:
        return {"report": None, "summary": f"no portfolio for {user_id}"}

    td = _parse_date(trade_date)
    with db.session() as s:
        diagnoses = (
            s.query(PortfolioDiagnosis)
            .filter(PortfolioDiagnosis.user_id == user_id)
            .filter(PortfolioDiagnosis.trade_date == td)
            .order_by(PortfolioDiagnosis.bucket_exposure_pct.desc())
            .all()
        )
    top = _top_candidates(db, td)

    cash_suggestion = _cash_suggestion(portfolio, diagnoses)
    markdown = _build_markdown(portfolio, diagnoses, top)
    card = _build_feishu_card(portfolio, diagnoses, top, cash_suggestion)

    report = MorningReport(
        user_id=user_id,
        trade_date=td,
        summary=markdown.split("\n", 3)[2] if "\n" in markdown else markdown[:300],
        portfolio_snapshot_json=json.dumps(portfolio, ensure_ascii=False, default=str),
        diagnoses_json=json.dumps(
            [
                {
                    "code": d.code, "name": d.name, "action": d.action,
                    "drawdown_state": d.drawdown_state,
                    "bucket_exposure_pct": d.bucket_exposure_pct,
                    "observe_range": d.observe_range,
                    "entry_range": d.entry_range,
                    "stop_loss": d.stop_loss,
                    "invalidation": d.invalidation,
                    "reasons": json.loads(d.reasons_json or "[]"),
                }
                for d in diagnoses
            ],
            ensure_ascii=False,
        ),
        candidates_json=json.dumps(
            [
                {
                    "code": s.code, "name": s.name, "direction": s.direction,
                    "final_score": s.final_score, "observe_range": s.observe_range,
                    "entry_range": s.entry_range, "stop_loss": s.stop_loss,
                    "invalidation": s.invalidation,
                }
                for s in top
            ],
            ensure_ascii=False,
        ),
        risk_buckets_json=json.dumps(portfolio.get("risk_buckets", []), ensure_ascii=False),
        cash_suggestion=cash_suggestion,
        markdown=markdown,
        feishu_card_json=json.dumps(card, ensure_ascii=False),
    )
    run_id = hashlib.sha1(f"morning:{user_id}:{td.isoformat()}".encode()).hexdigest()[:32]
    report.feishu_run_id = run_id

    push_ok = False
    push_msg = ""
    if push:
        push_ok, push_msg = _send_feishu(card, run_id, "morning")
        if push_ok:
            report.feishu_pushed = True
            report.feishu_pushed_at = datetime.utcnow()

    with db.tx() as s:
        existing = (
            s.query(MorningReport)
            .filter(MorningReport.user_id == user_id)
            .filter(MorningReport.trade_date == td)
            .one_or_none()
        )
        if existing is None:
            s.add(report)
        else:
            for field in (
                "summary", "portfolio_snapshot_json", "diagnoses_json",
                "candidates_json", "risk_buckets_json", "cash_suggestion",
                "markdown", "feishu_card_json",
            ):
                setattr(existing, field, getattr(report, field))
            if push_ok and not existing.feishu_pushed:
                existing.feishu_pushed = True
                existing.feishu_pushed_at = datetime.utcnow()
                existing.feishu_run_id = run_id
            report = existing

    return {
        "report": report,
        "summary": (
            f"diagnoses={len(diagnoses)} candidates={len(top)} "
            f"pushed={push_ok} ({push_msg or 'ok'})"
        ),
    }


def assess_quality(result: dict[str, Any]) -> tuple[str, str]:
    report = result.get("report")
    if report is None:
        return "degraded", "warn"
    if not report.markdown:
        return "degraded", "warn"
    return "succeeded", "pass"


def run(user_id: str = DEFAULT_USER_ID, trade_date: str = "", push: bool = True) -> str:
    result = build(user_id=user_id, trade_date=trade_date, push=push)
    return result["summary"]


def run_persist(user_id: str = DEFAULT_USER_ID, trade_date: str = "", push: bool = True) -> dict[str, Any]:
    result = build(user_id=user_id, trade_date=trade_date, push=push)
    status, quality = assess_quality(result)
    return {"status": status, "quality_status": quality, **result}
