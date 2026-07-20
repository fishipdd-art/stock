"""
Event reminder system.

Checks events happening in the next 24h and pushes them to Feishu.
Even without Feishu credentials, reminders are stored in DB
and shown in the UI dashboard.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import desc

from storage import get_db
from storage.models import IndustryEvent, EventReminder


@dataclass
class UpcomingReminder:
    """An event happening in the next 24-48h."""
    event: IndustryEvent
    days_until: int
    urgency: str  # 'today' / 'tomorrow' / 'this_week'


def get_reminders(days_ahead: int = 2, min_impact: int = 2) -> list[UpcomingReminder]:
    """Get events in the next `days_ahead` days, sorted by date."""
    db = get_db()
    today = date.today()
    end = today + timedelta(days=days_ahead)
    with db.session() as s:
        rows = (
            s.query(IndustryEvent)
            .filter(
                IndustryEvent.is_future == True,
                IndustryEvent.event_date >= today,
                IndustryEvent.event_date <= end,
                IndustryEvent.impact_level >= min_impact,
            )
            .order_by(IndustryEvent.event_date.asc(), desc(IndustryEvent.impact_level))
            .all()
        )
    out = []
    for ev in rows:
        days = (ev.event_date - today).days
        if days == 0:
            urgency = "today"
        elif days == 1:
            urgency = "tomorrow"
        else:
            urgency = "this_week"
        out.append(UpcomingReminder(event=ev, days_until=days, urgency=urgency))
    return out


def render_reminder_payload(reminders: list[UpcomingReminder]) -> dict:
    """Render reminders as a Feishu card payload."""
    today_rows = [r for r in reminders if r.urgency == "today"]
    tomorrow_rows = [r for r in reminders if r.urgency == "tomorrow"]
    week_rows = [r for r in reminders if r.urgency == "this_week"]

    elements = []

    if today_rows:
        lines = ["**🚨 今日事件**"]
        for r in today_rows:
            ev = r.event
            stars = "⭐" * ev.impact_level
            lines.append(f"  • {ev.title}  {stars}  `{ev.industry_label}`")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
        })

    if tomorrow_rows:
        lines = ["**⏰ 明日预告**"]
        for r in tomorrow_rows:
            ev = r.event
            stars = "⭐" * ev.impact_level
            lines.append(f"  • {ev.title}  {stars}  `{ev.industry_label}`")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
        })

    if week_rows:
        lines = ["**📅 本周后续**"]
        for r in week_rows[:5]:
            ev = r.event
            stars = "⭐" * ev.impact_level
            lines.append(f"  • {ev.event_date} {ev.title}  {stars}")
        if len(week_rows) > 5:
            lines.append(f"  · 还有 {len(week_rows) - 5} 个事件...")
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
        })

    if not elements:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "_未来 48h 无重要事件_"},
        })

    today_str = date.today().isoformat()
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "orange",
                "title": {
                    "tag": "plain_text",
                    "content": f"📅 事件日历提醒 · {today_str}",
                },
            },
            "elements": elements,
        },
    }


def save_reminders(reminders: list[UpcomingReminder]) -> int:
    """Persist reminders to DB (idempotent: dedupe by event_id + reminder_date).

    Returns count newly written.
    """
    if not reminders:
        return 0
    db = get_db()
    today = date.today()
    written = 0
    with db.tx() as session:
        for r in reminders:
            ev = r.event
            existing = (
                session.query(EventReminder)
                .filter(EventReminder.event_id == ev.id)
                .filter(EventReminder.reminder_date == today)
                .first()
            )
            if existing:
                continue
            row = EventReminder(
                event_id=ev.id,
                reminder_date=today,
                urgency=r.urgency,
                days_until=r.days_until,
                delivered=False,
                channel="",
            )
            session.add(row)
            written += 1
    return written


def push_reminders_to_feishu(reminders: list[UpcomingReminder]) -> bool:
    """Push reminders via Feishu notifier. Returns True if delivered."""
    from notifier import get_default_notifier
    if not reminders:
        return True
    payload = render_reminder_payload(reminders)
    notifier = get_default_notifier()
    return notifier.send(payload)


def run_reminder_job() -> int:
    """Daily job: collect reminders, save to DB, push to Feishu.

    Runs daily (e.g., 08:00). Returns count of newly saved reminders.
    """
    reminders = get_reminders(days_ahead=2, min_impact=2)
    n_saved = save_reminders(reminders)
    if reminders:
        delivered = push_reminders_to_feishu(reminders)
        logger.info(f"Reminders: {len(reminders)} found, {n_saved} new, feishu={'OK' if delivered else 'stub'}")
        # Mark as delivered in DB
        if delivered:
            db = get_db()
            today = date.today()
            with db.tx() as session:
                rs = (
                    session.query(EventReminder)
                    .filter(EventReminder.reminder_date == today)
                    .filter(EventReminder.delivered == False)
                    .all()
                )
                for r in rs:
                    r.delivered = True
                    r.channel = "feishu"
                    r.delivered_at = datetime.utcnow()
    else:
        logger.info("No upcoming reminders")
    return n_saved


def get_recent_reminders(limit: int = 20) -> list[EventReminder]:
    """Recent reminder records for UI display."""
    db = get_db()
    with db.session() as s:
        return (
            s.query(EventReminder)
            .order_by(desc(EventReminder.reminder_date))
            .limit(limit)
            .all()
        )