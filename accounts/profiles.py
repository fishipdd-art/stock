"""
Multi-account / multi-portfolio profile system.

Different investors care about different industries. This module lets
users define profiles (e.g., '保守型', '激进型', 'AI主题', '新能源主题')
and get personalized event feeds.

Built-in profiles:
  - 保守型 (Conservative): 银行/食品饮料/医药/公用事业
  - 平衡型 (Balanced): 主流 10 大行业
  - 激进型 (Aggressive): 新能源/AI/半导体/航天
  - AI 主题 (AI Theme): 半导体/AI 算力/消费电子/软件
  - 新能源主题 (New Energy): 锂电/光伏/风电/新能源车
  - 周期主题 (Cyclical): 钢铁/有色/化工/煤炭
  - 消费主题 (Consumer): 食品饮料/白酒/家电/零售
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import desc

from storage import get_db
from storage.models import IndustryEvent


@dataclass
class PortfolioProfile:
    """A user-defined investment profile."""
    code: str
    name: str
    description: str
    industries: list[str] = field(default_factory=list)  # industry codes
    event_types: list[str] = field(default_factory=list)  # event types
    min_impact: int = 3
    horizon_days: int = 30
    risk_level: str = "medium"  # 'low' / 'medium' / 'high'


# Built-in profiles
BUILTIN_PROFILES: dict[str, PortfolioProfile] = {
    "conservative": PortfolioProfile(
        code="conservative", name="🛡 保守型",
        description="银行/食品饮料/医药/公用事业，低波动稳定",
        industries=["banking", "insurance", "pharmaceutical", "food_beverage", "power", "consumer", "securities"],
        event_types=["earnings", "policy", "regulatory"],
        min_impact=3, horizon_days=60, risk_level="low",
    ),
    "balanced": PortfolioProfile(
        code="balanced", name="⚖ 平衡型",
        description="主流 10 大行业均衡配置",
        industries=["banking", "consumer_electronics", "ne_vehicle", "semiconductor", "lithium_battery",
                    "pharmaceutical", "food_beverage", "real_estate", "internet", "energy"],
        event_types=[], min_impact=3, horizon_days=30, risk_level="medium",
    ),
    "aggressive": PortfolioProfile(
        code="aggressive", name="🚀 激进型",
        description="新能源/AI/半导体/航天，高成长高波动",
        industries=["ne_vehicle", "lithium_battery", "semiconductor", "ai_chip", "ai_tech", "ai",
                    "aerospace", "satellite_internet", "robotics", "ar_vr"],
        event_types=["launch", "product_launch", "earnings", "regulatory"],
        min_impact=3, horizon_days=14, risk_level="high",
    ),
    "ai_theme": PortfolioProfile(
        code="ai_theme", name="🤖 AI 主题",
        description="半导体/AI 算力/消费电子/AI 软件",
        industries=["semiconductor", "ai_chip", "ai_tech", "consumer_electronics", "cyberspace",
                    "memory", "led_display", "smart_wearable", "third_gen_semi", "ar_vr"],
        event_types=["earnings", "launch", "product_launch", "regulatory"],
        min_impact=2, horizon_days=30, risk_level="high",
    ),
    "new_energy": PortfolioProfile(
        code="new_energy", name="🔋 新能源主题",
        description="锂电/光伏/风电/新能源车",
        industries=["lithium_battery", "solar", "wind", "ne_vehicle", "energy_storage",
                    "hydrogen", "battery_material", "lfp", "pv_inverter", "battery_recycling"],
        event_types=["earnings", "policy", "price_change", "capacity"],
        min_impact=3, horizon_days=30, risk_level="medium",
    ),
    "cyclical": PortfolioProfile(
        code="cyclical", name="⛏ 周期主题",
        description="钢铁/有色/化工/煤炭",
        industries=["steel", "non_ferrous", "chemicals", "coal", "non_ferrous", "rare_earth",
                    "shipping", "logistics", "energy", "agriculture"],
        event_types=["data_release", "policy", "price_change"],
        min_impact=3, horizon_days=30, risk_level="medium",
    ),
    "consumer": PortfolioProfile(
        code="consumer", name="🛍 消费主题",
        description="食品饮料/白酒/家电/零售",
        industries=["food_beverage", "wine", "home_appliance", "retail", "consumer",
                    "apparel", "cosmetics", "tourism", "education"],
        event_types=["earnings", "policy", "data_release"],
        min_impact=2, horizon_days=60, risk_level="low",
    ),
    "tech": PortfolioProfile(
        code="tech", name="💻 科技主题",
        description="半导体/消费电子/AI/软件/通信",
        industries=["semiconductor", "ai_chip", "consumer_electronics", "cyberspace", "telecom",
                    "memory", "ai_tech", "smart_wearable", "ar_vr", "smart_home"],
        event_types=["earnings", "launch", "product_launch", "regulatory"],
        min_impact=2, horizon_days=30, risk_level="high",
    ),
}


def get_profile(code: str) -> Optional[PortfolioProfile]:
    """Get a built-in profile by code."""
    return BUILTIN_PROFILES.get(code)


def list_profiles() -> list[PortfolioProfile]:
    """List all built-in profiles."""
    return list(BUILTIN_PROFILES.values())


def filter_events_for_profile(
    profile: PortfolioProfile,
    days_ahead: int = None,
) -> list[IndustryEvent]:
    """Get upcoming events matching a profile's criteria."""
    days_ahead = days_ahead or profile.horizon_days
    db = get_db()
    today = date.today()
    end = today + timedelta(days=days_ahead)

    with db.session() as s:
        q = s.query(IndustryEvent).filter(
            IndustryEvent.is_future == True,
            IndustryEvent.event_date >= today,
            IndustryEvent.event_date <= end,
            IndustryEvent.impact_level >= profile.min_impact,
        )
        if profile.industries:
            q = q.filter(IndustryEvent.industry.in_(profile.industries))
        if profile.event_types:
            q = q.filter(IndustryEvent.event_type.in_(profile.event_types))
        return q.order_by(IndustryEvent.event_date.asc(), desc(IndustryEvent.impact_level)).all()


def compare_profiles(
    profile_codes: list[str], days_ahead: int = 30,
) -> dict[str, dict]:
    """Compare multiple profiles: how many events each, top 3 events."""
    out: dict[str, dict] = {}
    for code in profile_codes:
        profile = get_profile(code)
        if not profile:
            continue
        events = filter_events_for_profile(profile, days_ahead)
        top = events[:3]
        out[code] = {
            "name": profile.name,
            "description": profile.description,
            "n_events": len(events),
            "industries": profile.industries,
            "min_impact": profile.min_impact,
            "horizon_days": profile.horizon_days,
            "risk_level": profile.risk_level,
            "top_events": [
                {
                    "id": e.id,
                    "title": e.title,
                    "event_date": e.event_date.isoformat(),
                    "impact_level": e.impact_level,
                    "industry_label": e.industry_label,
                    "event_type": e.event_type,
                }
                for e in top
            ],
        }
    return out


def format_profile_comparison(comparison: dict[str, dict]) -> str:
    """Markdown summary of profile comparison."""
    if not comparison:
        return "_无 profile_"
    lines = ["# 👤 投资组合对比", ""]
    lines.append("| Profile | 行业数 | 匹配事件 | 风险 |")
    lines.append("|---------|--------|----------|------|")
    for code, info in comparison.items():
        lines.append(
            f"| {info['name']} | {len(info['industries'])} | "
            f"{info['n_events']} | {info['risk_level']} |"
        )
    return "\n".join(lines)