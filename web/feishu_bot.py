"""
Feishu bot — receive IM messages and respond with system data.

Flow:
  1. User sends message to the bot in Feishu
  2. Feishu event callback → POST /api/feishu/event
  3. This module handles the event, parses the query,
     queries system data, and sends a reply.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import desc, func

from config.settings import settings
from storage import get_db

# ── token cache ──────────────────────────────────────────────────────────────

_token: str | None = None
_token_expire: float = 0

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_SEND_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
_USER_INFO_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{}/read_users"


def _get_token() -> str | None:
    global _token, _token_expire
    if _token and time.time() < _token_expire - 120:
        return _token
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return None
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.post(_TOKEN_URL, json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            })
            data = resp.json()
            if data.get("code") == 0:
                _token = data["tenant_access_token"]
                _token_expire = time.time() + int(data.get("expire", 7200))
                return _token
            logger.error(f"Feishu bot token error: {data}")
    except Exception as e:
        logger.error(f"Feishu bot token fetch failed: {e}")
    return None


# ── send message back to a chat ──────────────────────────────────────────────

def send_text(chat_id: str, text: str) -> bool:
    """Send a plain-text message to a Feishu chat."""
    token = _get_token()
    if not token:
        return False
    body = {
        "receive_id": chat_id,
        "receive_id_type": "chat_id",
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    try:
        with httpx.Client(timeout=15) as c:
            resp = c.post(
                _SEND_MSG_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=body,
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("code") == 0:
                return True
            logger.error(f"Feishu send failed: {resp.status_code} {str(data)[:300]}")
    except Exception as e:
        logger.error(f"Feishu send error: {e}")
    return False


def send_card(chat_id: str, card: dict) -> bool:
    """Send an interactive card to a Feishu chat."""
    token = _get_token()
    if not token:
        return False
    body = {
        "receive_id": chat_id,
        "receive_id_type": "chat_id",
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }
    try:
        with httpx.Client(timeout=15) as c:
            resp = c.post(
                _SEND_MSG_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=body,
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("code") == 0:
                return True
            logger.error(f"Feishu card send failed: {resp.status_code} {str(data)[:300]}")
    except Exception as e:
        logger.error(f"Feishu card send error: {e}")
    return False


# ── chat registration ─────────────────────────────────────────────────────────

def _register_chat(chat_id: str, name: str = "") -> None:
    """Register a chat so the FeishuNotifier (report broadcast) knows about it."""
    from notifier.feishu import FeishuNotifier
    try:
        n = FeishuNotifier()
        if n.has_app_creds and n._enabled:
            n.register_chat(chat_id=chat_id, name=name or chat_id, enabled=True)
    except Exception as e:
        logger.debug(f"[FeishuBot] register_chat error: {e}")


# ── event handler ────────────────────────────────────────────────────────────

def handle_event(body: dict[str, Any]) -> dict[str, Any] | None:
    """Route a Feishu event callback to the right handler.

    Returns a response dict for the webhook (e.g. challenge response),
    or None if the event was processed asynchronously.
    """
    # URL verification
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge", "")}

    # Event callbacks
    event_type = body.get("type")  # "event_callback" or "im.message.receive_v1"
    event = body.get("event", {})
    # For newer event format, type might be the event type directly
    if not event and "header" in body:
        # New Feishu event subscription format (v2.0)
        header = body.get("header", {})
        event_type = header.get("event_type", "")
        event = body.get("event", {})

    if event_type in ("im.message.receive_v1", "event_callback"):
        msg = event.get("message", event)
        msg_type = msg.get("message_type", "")
        chat_id = msg.get("chat_id", "") or event.get("chat_id", "")
        sender = event.get("sender", {})
        sender_id = (sender.get("sender_id", {}) or {}).get("open_id", "") or \
                    (event.get("operator_type") == "user" and event.get("operator_id", {}).get("open_id", ""))

        if msg_type == "text":
            content_raw = msg.get("content", "{}")
            try:
                content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
                user_text = (content.get("text") or "").strip()
            except (json.JSONDecodeError, AttributeError):
                user_text = str(content_raw).strip()

            if user_text:
                logger.info(f"[FeishuBot] from={sender_id} chat={chat_id} text={user_text[:80]}")
                _handle_query(chat_id, user_text)

    return None


def _handle_query(chat_id: str, text: str) -> None:
    """Parse user query, look up data, send reply."""
    # Auto-register this chat so the daily report etc. can be sent to it
    _register_chat(chat_id)

    # Quick keyword routing
    text_lower = text.lower()
    reply = ""

    # ── 报告 / 日报 ──
    if any(kw in text for kw in ("报告", "日报", "今日报告")):
        reply = _query_report()
    # ── 信号 / 信号匹配 ──
    elif any(kw in text for kw in ("信号", "信号匹配")):
        reply = _query_signals()
    # ── 热度 / 行业热度 ──
    elif any(kw in text for kw in ("热度", "热力")):
        reply = _query_hotness()
    # ── 期货 / 行情 ──
    elif any(kw in text for kw in ("期货", "行情")):
        reply = _query_futures()
    # ── 新闻 ──
    elif any(kw in text for kw in ("新闻", "资讯")):
        reply = _query_news()
    # ── 股票 ──
    elif any(kw in text for kw in ("股票", "a股", "个股")):
        reply = _query_stocks(text)
    # ── 概况 / 总览 ──
    elif any(kw in text for kw in ("概况", "总览", "概览", "状态")):
        reply = _query_overview()
    # ── 帮助 ──
    elif any(kw in text for kw in ("帮助", "help", "功能", "能做什么")):
        reply = _help_text()
    # ── 默认：LLM QA or overview ──
    else:
        reply = _query_overview()

    if reply:
        send_text(chat_id, reply)
    else:
        send_text(chat_id, '抱歉，没有查到相关数据。试试发送「帮助」查看我能回答什么。')


# ── query helpers ────────────────────────────────────────────────────────────

def _query_overview() -> str:
    """System overview."""
    db = get_db()
    with db.session() as s:
        n_signal = s.query(KnowledgeSignal).filter(KnowledgeSignal.phase == "active").count()
        n_futures = s.query(FuturesPrice).count()
        n_news = s.query(NewsRaw).count()
        n_stocks = s.query(StockQuote).count()
        latest_futures = s.query(func.max(FuturesPrice.trade_date)).scalar()
        latest_news = s.query(func.max(NewsRaw.published_at)).scalar()
        try:
            n_hits = s.query(SignalHit).filter(
                SignalHit.hit_at >= datetime.utcnow() - timedelta(hours=72)
            ).count()
        except Exception:
            n_hits = 0
    return (
        f"📊 系统概况\n"
        f"• 信号: {n_signal} 个活跃 | 近72h {n_hits} 次匹配\n"
        f"• 期货: {n_futures} 条 (最新 {latest_futures})\n"
        f"• 新闻: {n_news} 条 (最新 {latest_news.date() if latest_news else '-'})\n"
        f"• A股: {n_stocks} 条\n\n"
        f"试试发送：期货、信号、热度、新闻、报告"
    )


def _query_signals() -> str:
    """Top signals by strength."""
    db = get_db()
    with db.session() as s:
        rows = s.query(KnowledgeSignal).filter(
            KnowledgeSignal.phase == "active"
        ).order_by(KnowledgeSignal.strength.desc()).limit(10).all()
        # Also get recent hits
        try:
            hits = s.query(SignalHit).filter(
                SignalHit.hit_at >= datetime.utcnow() - timedelta(hours=48)
            ).order_by(SignalHit.final_score.desc()).limit(5).all()
        except Exception:
            hits = []
    lines = [f"🚨 强信号 TOP 10"]
    for r in rows:
        lines.append(f"  [{r.grade}] {r.title[:40]} (强度 {r.strength})")
    if hits:
        lines.append(f"\n📡 最近匹配 ({len(hits)} 条):")
        for h in hits:
            lines.append(f"  → {h.news_title[:40]}")
    return "\n".join(lines)


def _query_hotness() -> str:
    """Industry hotness rankings."""
    db = get_db()
    with db.session() as s:
        latest = s.query(func.max(SectorHeat.trade_date)).scalar()
        if not latest:
            return "暂无热度数据"
        rows = s.query(SectorHeat).filter(
            SectorHeat.trade_date == latest
        ).order_by(SectorHeat.rank).limit(10).all()
    lines = [f"🔥 行业热度排名 ({latest})"]
    for r in rows:
        lines.append(f"  #{r.rank} {r.category_name[:20]} — {r.hotness_score:.1f}")
    return "\n".join(lines)


def _query_futures() -> str:
    """Futures prices sorted by change %."""
    db = get_db()
    with db.session() as s:
        latest = s.query(func.max(FuturesPrice.trade_date)).scalar()
        if not latest:
            return "暂无期货数据"
        rows = s.query(FuturesPrice).filter(
            FuturesPrice.trade_date == latest
        ).order_by(desc(func.abs(FuturesPrice.change_pct))).limit(10).all()
    lines = [f"💹 期货行情 ({latest})"]
    for r in rows:
        arrow = "🔴" if r.change_pct > 0 else "🟢" if r.change_pct < 0 else "⚪"
        lines.append(
            f"  {arrow} {r.name[:12]} {r.close:.1f} "
            f"({r.change_pct:+.2f}%)"
        )
    return "\n".join(lines)


def _query_news() -> str:
    """Latest news."""
    db = get_db()
    with db.session() as s:
        rows = s.query(NewsRaw).order_by(
            NewsRaw.published_at.desc()
        ).limit(8).all()
    if not rows:
        return "暂无新闻"
    lines = [f"📰 最新新闻"]
    for n in rows:
        age = (datetime.utcnow() - n.published_at).total_seconds() / 3600
        lines.append(f"  • {n.title[:50]} ({age:.0f}h前)")
    return "\n".join(lines)


def _query_stocks(text: str) -> str:
    """Query stock data by code/name."""
    import re
    codes = re.findall(r"\d{6}", text)
    db = get_db()
    with db.session() as s:
        if codes:
            rows = s.query(StockQuote).filter(
                StockQuote.code.in_(codes)
            ).order_by(StockQuote.trade_date.desc()).limit(5).all()
        else:
            rows = s.query(StockQuote).order_by(
                StockQuote.trade_date.desc(), desc(abs(StockQuote.change_pct))
            ).limit(10).all()
    if not rows:
        return "暂无股票数据"
    latest = rows[0].trade_date
    lines = [f"📈 A股行情 ({latest})"]
    seen = set()
    for r in rows:
        if r.code in seen:
            continue
        seen.add(r.code)
        arrow = "🔴" if r.change_pct > 0 else "🟢" if r.change_pct < 0 else "⚪"
        lines.append(f"  {arrow} {r.code} {r.name[:10]} {r.close:.2f} ({r.change_pct:+.2f}%)")
    return "\n".join(lines)


def _query_report() -> str:
    """Latest daily report."""
    db = get_db()
    with db.session() as s:
        r = s.query(DailyReport).order_by(
            DailyReport.report_date.desc()
        ).first()
    if not r:
        return "暂无报告"
    return (
        f"📄 最新报告 ({r.report_date})\n"
        f"类型: {r.report_type}\n"
        f"信号: {r.n_signals} 条 | 新闻: {r.n_news} 条 | 类别: {r.n_top_categories}\n"
        f"飞书发送: {'✅' if r.feishu_sent else '❌'}\n\n"
        f"（完整报告请在 Web UI 查看）"
    )


def _help_text() -> str:
    return (
        "🤖 供应链错配日报 · 飞书助手\n\n"
        "你可以问我：\n"
        "• 概况 — 系统总览\n"
        "• 信号 — 强信号 TOP 10 + 最近匹配\n"
        "• 热度 — 行业热度排名\n"
        "• 期货 — 期货行情\n"
        "• 新闻 — 最新资讯\n"
        "• 股票 — A股行情\n"
        "• 报告 — 最新日报\n"
        "• 帮助 — 显示此消息"
    )


# Lazy imports to avoid circular deps
from storage.models import (  # noqa: E402
    KnowledgeSignal, FuturesPrice, NewsRaw, StockQuote,
    SectorHeat, DailyReport, SignalHit,
)
