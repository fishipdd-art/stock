"""Pipeline: WF-06 — produce the evening review (verified / contradicted /
view-changes / bias attribution) and push to Feishu.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, date as date_cls, timedelta
from typing import Any

from loguru import logger

from storage import get_db
from storage.models import (
    EveningReview,
    PortfolioDiagnosis,
    PortfolioPosition,
    StockQuote,
)


VERIFY_MOVE = 0.01  # 1% move confirms a directional call


def _parse_date(s: str | None) -> date_cls:
    if not s:
        return datetime.utcnow().date()
    try:
        return date_cls.fromisoformat(s)
    except ValueError:
        return datetime.utcnow().date()


def _load_quotes(db, codes: list[str], trade_date: date_cls) -> dict[str, list[StockQuote]]:
    if not codes:
        return {}
    cutoff = trade_date - timedelta(days=10)
    with db.session() as s:
        rows = (
            s.query(StockQuote)
            .filter(StockQuote.code.in_(codes))
            .filter(StockQuote.trade_date <= trade_date)
            .filter(StockQuote.trade_date >= cutoff)
            .order_by(StockQuote.trade_date.asc())
            .all()
        )
    out: dict[str, list[StockQuote]] = {}
    for r in rows:
        out.setdefault(r.code, []).append(r)
    return out


def _verify_diagnosis(diag: PortfolioDiagnosis, quotes: list[StockQuote]) -> dict[str, Any]:
    if not quotes or len(quotes) < 2:
        return {
            "code": diag.code,
            "name": diag.name,
            "action": diag.action,
            "verified": None,
            "note": "no_quote_data",
        }
    first = quotes[0].close
    last = quotes[-1].close
    if first <= 0:
        return {
            "code": diag.code, "name": diag.name, "action": diag.action,
            "verified": None, "note": "zero_baseline",
        }
    move = (last - first) / first
    if diag.action == "add":
        verified = move >= VERIFY_MOVE
    elif diag.action == "trim":
        verified = move <= -VERIFY_MOVE
    elif diag.action == "exit":
        verified = move <= -VERIFY_MOVE
    else:  # hold
        verified = abs(move) < 0.05
    return {
        "code": diag.code,
        "name": diag.name,
        "action": diag.action,
        "verified": verified,
        "move_pct": round(move * 100, 2),
        "from": first,
        "to": last,
    }


def _view_changes(db, trade_date: date_cls) -> list[dict[str, Any]]:
    """Compare today's diagnoses with yesterday's; flag churn."""
    yesterday = trade_date - timedelta(days=1)
    with db.session() as s:
        today = (
            s.query(PortfolioDiagnosis)
            .filter(PortfolioDiagnosis.trade_date == trade_date)
            .all()
        )
        prev = (
            s.query(PortfolioDiagnosis)
            .filter(PortfolioDiagnosis.trade_date == yesterday)
            .all()
        )
    prev_map = {d.code: d.action for d in prev}
    out: list[dict[str, Any]] = []
    for d in today:
        prev_action = prev_map.get(d.code)
        if prev_action and prev_action != d.action:
            out.append({
                "code": d.code,
                "name": d.name,
                "from": prev_action,
                "to": d.action,
            })
    return out


def _bias_attribution(verifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coarse attribution buckets: data lag / over-trim / over-add / correct."""
    out: list[dict[str, Any]] = []
    for v in verifications:
        if v.get("verified") is True:
            out.append({"code": v["code"], "bias": "correct"})
        elif v.get("verified") is False and v.get("action") == "trim":
            # We trimmed but stock went up — bias toward over-trim
            out.append({"code": v["code"], "bias": "over_trim"})
        elif v.get("verified") is False and v.get("action") == "add":
            out.append({"code": v["code"], "bias": "over_add"})
        else:
            out.append({"code": v["code"], "bias": "mixed"})
    return out


def _build_markdown(
    trade_date: date_cls,
    verifications: list[dict[str, Any]],
    view_changes: list[dict[str, Any]],
    bias: list[dict[str, Any]],
    pnl_attribution: list[dict[str, Any]],
) -> str:
    lines = [f"# 复盘 {trade_date.isoformat()}", ""]
    verified_count = sum(1 for v in verifications if v.get("verified") is True)
    contradicted_count = sum(1 for v in verifications if v.get("verified") is False)
    lines.append(
        f"验证 {verified_count} / 反向 {contradicted_count} / 中性 {len(verifications)-verified_count-contradicted_count}"
    )
    lines.append("")
    lines.append("## 当日验证")
    for v in verifications:
        mark = "✅" if v.get("verified") else ("❌" if v.get("verified") is False else "·")
        move = v.get("move_pct")
        lines.append(
            f"- {mark} {v['name']}({v['code']}) {v['action']} "
            f"价格 {v.get('from','-')} → {v.get('to','-')} ({move if move is not None else '-'}%)"
        )
    if view_changes:
        lines.append("")
        lines.append("## 观点变化")
        for vc in view_changes:
            lines.append(f"- {vc['name']}({vc['code']}): {vc['from']} → {vc['to']}")
    if pnl_attribution:
        lines.append("")
        lines.append("## 当日盈亏归因")
        for p in pnl_attribution:
            lines.append(
                f"- {p['name']}({p['code']}) {p['pnl_amount']:+.0f} 元 ({p['pnl_pct']:+.2f}%)"
            )
    if bias:
        lines.append("")
        lines.append("## 偏差归因")
        for b in bias:
            lines.append(f"- {b['code']}: {b['bias']}")
    return "\n".join(lines)


def _build_card(markdown: str, trade_date: date_cls) -> dict[str, Any]:
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": "blue",
                "title": {
                    "tag": "plain_text",
                    "content": f"🌙 复盘 {trade_date.isoformat()}",
                },
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": markdown[:3000]}},
            ],
        },
    }


def _send_feishu(card: dict[str, Any], run_id: str, payload_kind: str = "evening") -> tuple[bool, str]:
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


def _pnl_attribution(db, codes: list[str], trade_date: date_cls) -> list[dict[str, Any]]:
    if not codes:
        return []
    with db.session() as s:
        positions = (
            s.query(PortfolioPosition)
            .filter(PortfolioPosition.code.in_(codes))
            .all()
        )
    out = []
    for p in positions:
        out.append({
            "code": p.code,
            "name": p.name,
            "pnl_amount": p.pnl_amount,
            "pnl_pct": p.pnl_pct,
        })
    out.sort(key=lambda r: -(r["pnl_amount"]))
    return out


def build(
    *,
    user_id: str = "default",
    trade_date: str = "",
    push: bool = True,
) -> dict[str, Any]:
    db = get_db()
    td = _parse_date(trade_date)

    with db.session() as s:
        diagnoses = (
            s.query(PortfolioDiagnosis)
            .filter(PortfolioDiagnosis.user_id == user_id)
            .filter(PortfolioDiagnosis.trade_date == td)
            .all()
        )

    codes = [d.code for d in diagnoses]
    quotes_by_code = _load_quotes(db, codes, td)
    verifications = [_verify_diagnosis(d, quotes_by_code.get(d.code, [])) for d in diagnoses]
    view_changes = _view_changes(db, td)
    bias = _bias_attribution(verifications)
    pnl_attribution = _pnl_attribution(db, codes, td)

    verified_count = sum(1 for v in verifications if v.get("verified") is True)
    contradicted_count = sum(1 for v in verifications if v.get("verified") is False)

    markdown = _build_markdown(td, verifications, view_changes, bias, pnl_attribution)
    card = _build_card(markdown, td)

    review = EveningReview(
        trade_date=td,
        verified_count=verified_count,
        contradicted_count=contradicted_count,
        pnl_attribution_json=json.dumps(pnl_attribution, ensure_ascii=False, default=str),
        view_changes_json=json.dumps(view_changes, ensure_ascii=False, default=str),
        bias_attribution_json=json.dumps(bias, ensure_ascii=False),
        summary=markdown.split("\n", 3)[2] if "\n" in markdown else markdown[:300],
        markdown=markdown,
        feishu_card_json=json.dumps(card, ensure_ascii=False),
    )
    run_id = hashlib.sha1(f"evening:{user_id}:{td.isoformat()}".encode()).hexdigest()[:32]
    review.feishu_run_id = run_id

    push_ok = False
    push_msg = ""
    if push:
        push_ok, push_msg = _send_feishu(card, run_id, "evening")
        if push_ok:
            review.feishu_pushed = True
            review.feishu_pushed_at = datetime.utcnow()

    with db.tx() as s:
        existing = (
            s.query(EveningReview)
            .filter(EveningReview.trade_date == td)
            .one_or_none()
        )
        if existing is None:
            s.add(review)
        else:
            for field in (
                "verified_count", "contradicted_count",
                "pnl_attribution_json", "view_changes_json",
                "bias_attribution_json", "summary", "markdown",
                "feishu_card_json",
            ):
                setattr(existing, field, getattr(review, field))
            if push_ok and not existing.feishu_pushed:
                existing.feishu_pushed = True
                existing.feishu_pushed_at = datetime.utcnow()
                existing.feishu_run_id = run_id
            review = existing

    return {
        "review": review,
        "summary": (
            f"verified={verified_count} contradicted={contradicted_count} "
            f"view_changes={len(view_changes)} pushed={push_ok} ({push_msg or 'ok'})"
        ),
    }


def assess_quality(result: dict[str, Any]) -> tuple[str, str]:
    review = result.get("review")
    if review is None:
        return "degraded", "warn"
    if not review.markdown:
        return "degraded", "warn"
    return "succeeded", "pass"


def run(trade_date: str = "", user_id: str = "default", push: bool = True) -> str:
    result = build(trade_date=trade_date, user_id=user_id, push=push)
    return result["summary"]


def run_persist(trade_date: str = "", user_id: str = "default", push: bool = True) -> dict[str, Any]:
    result = build(trade_date=trade_date, user_id=user_id, push=push)
    status, quality = assess_quality(result)
    return {"status": status, "quality_status": quality, **result}
