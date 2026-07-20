"""Pipeline: WF-05 — diagnose the user's portfolio against the latest
``StockScore`` rows and emit action recommendations.

Output: ``PortfolioDiagnosis`` rows plus a lightweight JSON summary that
``pipeline.morning_report`` consumes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, date as date_cls
from typing import Any

from loguru import logger

from accounts.portfolio import DEFAULT_USER_ID, get_portfolio
from storage import get_db
from storage.models import (
    PortfolioDiagnosis,
    PortfolioPosition,
    PipelineRun,
    StockScore,
)


# Drawdown thresholds (document §7)
DRAWDOWN_BANDS = [
    (-0.20, "critical"),
    (-0.16, "warn"),
    (-0.12, "watch"),
    (0.0, "normal"),
]


@dataclass
class DiagnosisDecision:
    action: str
    confidence: float
    industry_logic_ok: bool
    valuation_ok: bool
    drawdown_state: str
    summary: str
    reasons: list[str]
    risk_note: str
    observe_range: str
    entry_range: str
    stop_loss: float
    invalidation: str


def _classify_drawdown(pnl_pct: float) -> str:
    for threshold, label in DRAWDOWN_BANDS:
        if pnl_pct <= threshold:
            return label
    return "normal"


def _industry_logic_ok(code: str, scores: list[StockScore]) -> bool:
    """Industry logic is OK when at least one Score exists for the same chain
    OR the position is an ETF that benefits from current direction.
    """
    return any(s.final_score >= 55 for s in scores)


def _valuation_ok(latest_score: StockScore | None, position: PortfolioPosition) -> bool:
    """Don't chase: if the position is up > 18% from cost AND the latest
    score is weak, valuation is bad. If the score is strong, valuation can
    ride the trend even after a small gain.
    """
    if latest_score is None:
        # Without a score we can't argue valuation is bad; default true
        return True
    if latest_score.final_score >= 60:
        return True
    if position.pnl_pct >= 0.18:
        return False
    return True


def _decide(
    position: PortfolioPosition,
    scores_for_code: list[StockScore],
    industry_logic: bool,
    valuation: bool,
) -> DiagnosisDecision:
    latest = scores_for_code[0] if scores_for_code else None
    drawdown_state = _classify_drawdown(position.pnl_pct)
    score = latest.final_score if latest else 0.0
    reasons: list[str] = []

    if latest is None:
        reasons.append("当日评分证据链未通过质量门禁，仅保留观察，不生成交易动作")
        if drawdown_state in {"warn", "critical"}:
            reasons.append("回撤已触发风险软预警，需人工复核止损纪律")
        return DiagnosisDecision(
            action="hold",
            confidence=0.20,
            industry_logic_ok=False,
            valuation_ok=False,
            drawdown_state=drawdown_state,
            summary=f"{position.name}({position.code}) · 观察模式",
            reasons=reasons,
            risk_note="证据不足：禁止据此加仓、减仓或退出",
            observe_range="N/A",
            entry_range="N/A",
            stop_loss=0.0,
            invalidation="等待 MiniMax 复核与独立来源证据通过",
        )

    if not industry_logic:
        # No supportive industry logic → trim or exit verification
        if position.pnl_pct <= -0.18:
            action = "exit"
            reasons.append("产业逻辑缺失且浮亏接近最大回撤容忍")
        else:
            action = "trim"
            reasons.append("产业逻辑缺失")
    elif not valuation:
        action = "trim"
        reasons.append("估值已透支（浮盈 >18% 且评分弱）")
    elif score >= 75 and industry_logic and valuation:
        action = "add"
        reasons.append(f"评分 {score:.1f}、产业逻辑与估值均成立")
    elif score >= 60:
        action = "hold"
        reasons.append(f"评分 {score:.1f}，持有观察")
    elif score >= 40:
        action = "hold"
        reasons.append(f"评分 {score:.1f}，无强催化")
    else:
        action = "trim"
        reasons.append(f"评分 {score:.1f}，逻辑转弱")

    # Drawdown escalation
    if drawdown_state == "critical" and action in ("hold", "add"):
        action = "trim"
        reasons.append("回撤接近 20%，按软预警降级")
    if drawdown_state == "warn" and action == "add":
        action = "hold"
        reasons.append("回撤已达 16%，新增暂缓")

    summary_bits = [
        f"{position.name}({position.code}) {position.pnl_pct*100:.1f}%",
        f"动作={action}",
    ]
    summary = " · ".join(summary_bits)

    confidence = 0.5
    if latest is not None:
        confidence = min(0.9, 0.4 + latest.final_score / 200.0)

    if latest is not None:
        observe = latest.observe_range
        entry = latest.entry_range
        stop = latest.stop_loss
        invalidation = latest.invalidation
    else:
        observe, entry, stop, invalidation = "N/A", "N/A", 0.0, "需要新的事件证据"

    risk_note = "软预警：不设硬仓位上限；新增参考预算约 5000 元"
    return DiagnosisDecision(
        action=action,
        confidence=confidence,
        industry_logic_ok=industry_logic,
        valuation_ok=valuation,
        drawdown_state=drawdown_state,
        summary=summary,
        reasons=reasons,
        risk_note=risk_note,
        observe_range=observe,
        entry_range=entry,
        stop_loss=stop,
        invalidation=invalidation,
    )


def _collect_scores(db, codes: list[str], trade_date: date_cls) -> dict[str, list[StockScore]]:
    if not codes:
        return {}
    with db.session() as s:
        qualified = (
            s.query(PipelineRun)
            .filter(PipelineRun.pipeline == "score_candidates")
            .filter(PipelineRun.business_date == trade_date)
            .order_by(PipelineRun.created_at.desc())
            .first()
        )
        if qualified is None or not (
            qualified.status == "succeeded" and qualified.quality_status == "pass"
        ):
            return {}
        rows = (
            s.query(StockScore)
            .filter(StockScore.code.in_(codes))
            .filter(StockScore.trade_date <= trade_date)
            .order_by(StockScore.trade_date.desc(), StockScore.final_score.desc())
            .all()
        )
    out: dict[str, list[StockScore]] = {}
    for r in rows:
        out.setdefault(r.code, []).append(r)
    return out


def diagnose(
    *,
    user_id: str = DEFAULT_USER_ID,
    trade_date: str = "",
    persist: bool = True,
) -> dict[str, Any]:
    """Diagnose the user's portfolio and persist PortfolioDiagnosis rows."""
    db = get_db()
    try:
        portfolio = get_portfolio(user_id)
    except KeyError:
        return {"diagnoses": [], "summary": f"no portfolio for {user_id}"}

    td = _parse_date(trade_date)
    codes = [p["code"] for p in portfolio["positions"]]
    scores_by_code = _collect_scores(db, codes, td)

    diagnoses: list[PortfolioDiagnosis] = []
    for pos_dict in portfolio["positions"]:
        position = PortfolioPosition(
            user_id=user_id,
            code=pos_dict["code"],
            name=pos_dict["name"],
            asset_type=pos_dict["asset_type"],
            quantity=pos_dict["quantity"],
            current_price=pos_dict["current_price"],
            cost_price=pos_dict["cost_price"],
            market_value=pos_dict["market_value"],
            pnl_amount=pos_dict["pnl_amount"],
            pnl_pct=pos_dict["pnl_pct"],
            risk_bucket=pos_dict["risk_bucket"],
        )
        bucket_weight = next(
            (
                b["weight"]
                for b in portfolio.get("risk_buckets", [])
                if b["name"] == position.risk_bucket
            ),
            0.0,
        )

        scores = scores_by_code.get(position.code, [])
        industry_logic = _industry_logic_ok(position.code, scores)
        valuation = _valuation_ok(scores[0] if scores else None, position)
        decision = _decide(position, scores, industry_logic, valuation)

        row = PortfolioDiagnosis(
            user_id=user_id,
            trade_date=td,
            code=position.code,
            name=position.name,
            asset_type=position.asset_type,
            action=decision.action,
            confidence=decision.confidence,
            industry_logic_ok=decision.industry_logic_ok,
            valuation_ok=decision.valuation_ok,
            drawdown_state=decision.drawdown_state,
            bucket_exposure_pct=bucket_weight,
            observe_range=decision.observe_range,
            entry_range=decision.entry_range,
            stop_loss=decision.stop_loss,
            invalidation=decision.invalidation,
            summary=decision.summary,
            reasons_json=json.dumps(decision.reasons, ensure_ascii=False),
            risk_note=decision.risk_note,
        )
        if persist:
            with db.tx() as s:
                existing = (
                    s.query(PortfolioDiagnosis)
                    .filter(PortfolioDiagnosis.user_id == user_id)
                    .filter(PortfolioDiagnosis.trade_date == td)
                    .filter(PortfolioDiagnosis.code == position.code)
                    .one_or_none()
                )
                if existing is None:
                    s.add(row)
                else:
                    row.id = existing.id
                    for field in (
                        "name", "asset_type", "action", "confidence",
                        "industry_logic_ok", "valuation_ok", "drawdown_state",
                        "bucket_exposure_pct", "observe_range", "entry_range",
                        "stop_loss", "invalidation", "summary", "reasons_json",
                        "risk_note",
                    ):
                        setattr(existing, field, getattr(row, field))
                    row = existing
        diagnoses.append(row)

    return {
        "diagnoses": diagnoses,
        "summary": (
            f"positions={len(diagnoses)} "
            f"add={sum(1 for d in diagnoses if d.action == 'add')} "
            f"hold={sum(1 for d in diagnoses if d.action == 'hold')} "
            f"trim={sum(1 for d in diagnoses if d.action == 'trim')} "
            f"exit={sum(1 for d in diagnoses if d.action == 'exit')}"
        ),
    }


def _parse_date(s: str | None) -> date_cls:
    if not s:
        return datetime.utcnow().date()
    try:
        return date_cls.fromisoformat(s)
    except ValueError:
        return datetime.utcnow().date()


def assess_quality(result: dict[str, Any]) -> tuple[str, str]:
    diagnoses = list(result.get("diagnoses") or [])
    if not diagnoses:
        return "degraded", "warn"
    return "succeeded", "pass"


def run(user_id: str = DEFAULT_USER_ID, trade_date: str = "") -> str:
    result = diagnose(user_id=user_id, trade_date=trade_date, persist=True)
    return result["summary"]


def run_persist(user_id: str = DEFAULT_USER_ID, trade_date: str = "") -> dict[str, Any]:
    result = diagnose(user_id=user_id, trade_date=trade_date, persist=True)
    status, quality = assess_quality(result)
    return {"status": status, "quality_status": quality, **result}
