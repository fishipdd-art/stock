"""
Multi-user profile and personalization system.

Each user has:
  - id (string, e.g. 'default', 'user_123')
  - display_name
  - preferences (industry focus, risk tolerance, etc.)
  - favorites (starred events, signals, stocks)
  - dashboard_config (which sections to show)

Storage: UserProfile + UserFavorite tables.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from loguru import logger
from sqlalchemy import desc, or_

from storage import get_db
from storage.models import (
    UserProfile, UserFavorite, IndustryEvent, KnowledgeSignal,
    SectorHeat,
)


DEFAULT_USER = "default"


def get_or_create_user(user_id: str = DEFAULT_USER) -> dict:
    """Get user profile or create default if not exists."""
    db = get_db()
    with db.session() as s:
        user = s.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        if not user:
            user = UserProfile(
                user_id=user_id,
                display_name=user_id.capitalize(),
                preferences_json=json.dumps({
                    "industries": [],  # empty = all
                    "event_types": [],
                    "min_impact": 3,
                    "horizon_days": 30,
                    "risk_level": "medium",
                }),
            )
            s.add(user)
            s.commit()
            s.refresh(user)
        return {
            "user_id": user.user_id,
            "display_name": user.display_name,
            "preferences": json.loads(user.preferences_json or "{}"),
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }


def update_preferences(user_id: str, preferences: dict) -> dict:
    """Update user preferences."""
    db = get_db()
    user = get_or_create_user(user_id)
    with db.tx() as s:
        u = s.query(UserProfile).filter(UserProfile.user_id == user_id).first()
        u.preferences_json = json.dumps(preferences, ensure_ascii=False)
    return get_or_create_user(user_id)


def add_favorite(user_id: str, item_type: str, item_id: int, note: str = "") -> dict:
    """Add an item to user favorites."""
    db = get_db()
    with db.tx() as s:
        existing = (
            s.query(UserFavorite)
            .filter(
                UserFavorite.user_id == user_id,
                UserFavorite.item_type == item_type,
                UserFavorite.item_id == item_id,
            )
            .first()
        )
        if existing:
            return {"status": "exists"}
        f = UserFavorite(
            user_id=user_id,
            item_type=item_type,
            item_id=item_id,
            note=note,
        )
        s.add(f)
    return {"status": "added"}


def remove_favorite(user_id: str, item_type: str, item_id: int) -> dict:
    db = get_db()
    with db.tx() as s:
        s.query(UserFavorite).filter(
            UserFavorite.user_id == user_id,
            UserFavorite.item_type == item_type,
            UserFavorite.item_id == item_id,
        ).delete()
    return {"status": "removed"}


def get_favorites(user_id: str, item_type: str = None) -> list[dict]:
    """Get user's favorite items, optionally filtered by type."""
    db = get_db()
    with db.session() as s:
        q = s.query(UserFavorite).filter(UserFavorite.user_id == user_id)
        if item_type:
            q = q.filter(UserFavorite.item_type == item_type)
        rows = q.order_by(desc(UserFavorite.created_at)).all()
        out = []
        for r in rows:
            out.append({
                "id": r.id,
                "item_type": r.item_type,
                "item_id": r.item_id,
                "note": r.note,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return out


def get_user_dashboard(user_id: str = DEFAULT_USER, days_ahead: int = 7) -> dict:
    """Personalized dashboard: user's prefs → filtered events + favorites + hotness."""
    user = get_or_create_user(user_id)
    prefs = user.get("preferences", {})

    db = get_db()
    today = date.today()
    end = today + timedelta(days=prefs.get("horizon_days", days_ahead))
    min_impact = prefs.get("min_impact", 3)
    industries = prefs.get("industries", [])

    # Upcoming events matching prefs
    with db.session() as s:
        events_q = s.query(IndustryEvent).filter(
            IndustryEvent.is_future == True,
            IndustryEvent.event_date >= today,
            IndustryEvent.event_date <= end,
            IndustryEvent.impact_level >= min_impact,
        )
        if industries:
            events_q = events_q.filter(IndustryEvent.industry.in_(industries))
        events = events_q.order_by(IndustryEvent.event_date.asc()).limit(15).all()

        events_data = [
            {
                "id": e.id, "title": e.title, "event_date": e.event_date.isoformat(),
                "impact_level": e.impact_level, "industry_label": e.industry_label,
            }
            for e in events
        ]

        # Hotness for today
        heats = (
            s.query(SectorHeat)
            .filter(SectorHeat.trade_date == today)
            .order_by(SectorHeat.hotness_score.desc())
            .limit(5)
            .all()
        )
        heats_data = [
            {
                "category_name": h.category_name,
                "hotness_score": h.hotness_score,
                "rank": h.rank,
            }
            for h in heats
        ]

    # Favorites
    favs = get_favorites(user_id)

    return {
        "user": user,
        "upcoming_events": events_data,
        "today_hotness": heats_data,
        "favorites": favs,
    }