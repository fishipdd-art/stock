"""
Event auto-detection from news.

Scraped news often contains event announcements ("X将于7月15日发射").
This module extracts structured events from news titles/content and
stores them in the events table with source='auto_detected'.

Detection strategy:
  1. Regex patterns for event types (launch, earnings, M&A, policy, etc.)
  2. Date extraction (Chinese date formats)
  3. Industry classification via keyword matching
  4. Impact scoring based on event type + keywords (e.g., "亿元" = high)
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from typing import Iterable

from loguru import logger
from sqlalchemy import desc

from storage import get_db
from storage.models import NewsRaw, IndustryEvent


# =====================================================================
# Event type patterns: (regex, event_type, default_impact)
# =====================================================================

_EVENT_PATTERNS: list[tuple[str, str, int]] = [
    # Rocket / satellite launches
    (r'(火箭|卫星|飞船|探测器|载人飞船).*?(发射|升空|回收|着陆)', 'launch', 5),
    (r'(发射|升空).*?(火箭|卫星|飞船)', 'launch', 5),

    # Product launches
    (r'(发布|推出|上市|亮相|首发).*?(新品|新机|新车|新药|新版本|新车型)', 'product_launch', 3),
    (r'(新车|新机|新药).*?(发布|上市|亮相)', 'product_launch', 3),

    # Earnings
    (r'(财报|业绩|季报|年报).*?(发布|出炉|披露)', 'earnings', 4),
    (r'(净利润|营收).*?(增长|下滑|同比)', 'earnings', 3),

    # M&A
    (r'(收购|并购|重组|借壳|分拆).*?(\d+[万亿]?元|\d+\.\d+亿)', 'm&a', 5),
    (r'(战略投资|入股|控股|股权转让).*?(\d+[万亿]?元)', 'm&a', 4),

    # Capacity / facility
    (r'(扩产|投产|开工|建成|封顶|下线).*?(\d+[万亿]?元|\d+万吨|\d+GWh|\d+GW)', 'capacity', 4),
    (r'(工厂|产线|基地|项目).*?(投产|开工|建成|奠基)', 'capacity', 3),

    # Price changes
    (r'(涨价|提价|调价|上调).*?(\d+%|\d+元/吨|\d+元/件)', 'price_change', 3),
    (r'(跌价|降价|下调|下调价格).*?(\d+%|\d+元/吨|\d+元/件)', 'price_change', 3),

    # Regulatory
    (r'(出口管制|禁令|制裁|反制|加征关税)', 'regulatory', 5),
    (r'(获批|批准|通过|核准|许可).*?(临床|新药|上市|资质)', 'regulatory', 4),
    (r'(FDA|NMPA|欧盟CE).*?(批准|通过|受理)', 'regulatory', 4),

    # Policy
    (r'(国常会|政治局会议|中央经济工作会议)', 'policy', 5),
    (r'(补贴|扶持|减税|退税|产业政策)', 'policy', 4),
    (r'(限产|减产|产能控制|去产能)', 'policy', 4),

    # Contract / order
    (r'(签订|签署|中标|获得).*?(订单|合同|协议).*?(\d+[万亿]?元)', 'contract', 4),
    (r'(\d+[万亿]?元).*?(订单|合同|大单)', 'contract', 3),

    # Conferences / exhibitions
    (r'(展会|博览会|论坛|峰会).*?(开幕|召开|举办)', 'conference', 3),

    # Macro data
    (r'(CPI|PPI|PMI|GDP|M2|社融|非农|FOMC|议息).*?(数据|会议|决议)', 'data_release', 4),
]


# =====================================================================
# Date extraction (Chinese date formats)
# =====================================================================

# "2026年7月15日", "7月15日", "7.15", "7-15"
_DATE_PATTERNS = [
    (r'(\d{4})年(\d{1,2})月(\d{1,2})日', 'full'),
    (r'(\d{4})[./-](\d{1,2})[./-](\d{1,2})', 'iso'),
    (r'(\d{1,2})月(\d{1,2})日', 'short'),
    (r'(\d{1,2})[./-](\d{1,2})(?!\d)', 'short_dash'),
]


def _extract_date(text: str, today: date) -> date | None:
    """Extract first future or recent date from Chinese text."""
    for pat, kind in _DATE_PATTERNS:
        m = re.search(pat, text)
        if not m:
            continue
        try:
            if kind == 'full':
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif kind == 'iso':
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                y = today.year
                mo, d = int(m.group(1)), int(m.group(2))
            dt = date(y, mo, d)
            # Only consider dates within ±90 days of today
            if abs((dt - today).days) <= 90:
                return dt
        except (ValueError, OverflowError):
            continue
    return None


# =====================================================================
# Industry classification
# =====================================================================

_INDUSTRY_KEYWORDS: dict[str, list[str]] = {
    "aerospace": ["火箭", "卫星", "飞船", "载人", "发射", "航天", "SpaceX", "Starship", "千帆", "GW星座"],
    "semiconductor": ["芯片", "半导体", "硅片", "光刻机", "ASML", "TSMC", "台积电", "中芯", "长存", "长鑫", "HBM", "DRAM", "NAND", "MLCC", "PCB"],
    "ne_vehicle": ["新能源车", "电动车", "比亚迪", "特斯拉", "Tesla", "小米SU7", "小鹏", "理想", "蔚来", "问界"],
    "lithium_battery": ["锂电", "电池", "宁德", "比亚迪电池", "碳酸锂", "锂电池"],
    "solar": ["光伏", "多晶硅", "硅料", "组件", "逆变器", "隆基", "通威", "HJT", "TOPCon"],
    "consumer_electronics": ["手机", "iPhone", "华为", "苹果", "Mate", "小米", "OPPO", "vivo"],
    "pharmaceutical": ["药", "FDA", "NMPA", "新药", "临床", "创新药", "百济", "信达", "恒瑞"],
    "agriculture": ["大豆", "玉米", "小麦", "水稻", "猪肉", "生猪", "饲料"],
    "rare_earth": ["稀土", "镨钕", "氧化镨钕", "钕铁硼"],
    "shipping": ["航运", "集运", "集装箱", "BDI", "SCFI", "运价"],
    "non_ferrous": ["铜", "铝", "锌", "铅", "镍", "锡"],
    "steel": ["钢铁", "螺纹钢", "热卷", "铁矿石"],
    "energy": ["原油", "石油", "OPEC", "天然气", "LNG"],
    "ai_tech": ["AI", "大模型", "算力", "GPU", "H100", "H200", "NVIDIA", "英伟达"],
    "consumer": ["白酒", "茅台", "五粮液", "消费", "电商", "618", "双11"],
    "automotive": ["汽车", "乘用车", "商用车", "重卡", "长城", "吉利", "奇瑞"],
    "chemicals": ["化工", "MDI", "TDI", "聚氨酯", "万华"],
    "logistics": ["快递", "物流", "顺丰", "京东物流", "中通"],
}


def _classify_industry(text: str) -> tuple[str, str]:
    """Classify text to industry code + label. Returns (code, label)."""
    for code, keywords in _INDUSTRY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return code, _label_for(code)
    return "unknown", "未知"


_LABELS = {
    "aerospace": "航天军工", "semiconductor": "半导体", "ne_vehicle": "新能源车",
    "lithium_battery": "锂电", "solar": "光伏", "consumer_electronics": "消费电子",
    "pharmaceutical": "医药", "agriculture": "农产品", "rare_earth": "稀土",
    "shipping": "航运", "non_ferrous": "有色", "steel": "钢铁", "energy": "能源",
    "ai_tech": "AI", "consumer": "消费", "automotive": "汽车", "chemicals": "化工",
    "logistics": "物流",
}


def _label_for(code: str) -> str:
    return _LABELS.get(code, code)


# =====================================================================
# Main detection
# =====================================================================

@dataclass
class DetectedEvent:
    """A candidate event extracted from a news article."""
    news_id: int
    news_title: str
    news_url: str
    event_type: str
    industry: str
    industry_label: str
    title: str
    description: str
    event_date: date | None
    impact_level: int
    related_keywords: list[str]


def detect_from_news(
    session,
    since: datetime | None = None,
    min_impact: int = 3,
    max_results: int = 100,
) -> list[DetectedEvent]:
    """Scan recent news for event-type announcements.

    Returns list of DetectedEvent candidates. Does NOT write to DB;
    caller is responsible for storing.
    """
    since = since or (datetime.utcnow() - timedelta(days=3))
    today = date.today()

    news_list = (
        session.query(NewsRaw)
        .filter(NewsRaw.published_at >= since)
        .order_by(desc(NewsRaw.published_at))
        .limit(500)
        .all()
    )

    candidates: list[DetectedEvent] = []
    for n in news_list:
        text = n.title + " " + (n.summary or "")
        for pat, etype, base_impact in _EVENT_PATTERNS:
            m = re.search(pat, text)
            if not m:
                continue
            if base_impact < min_impact:
                continue

            industry, label = _classify_industry(text)
            if industry == "unknown":
                continue

            # Boost impact if money amounts are involved
            impact = base_impact
            if re.search(r'\d+亿', text):
                impact = min(5, impact + 1)
            if re.search(r'\d+万亿', text):
                impact = 5

            # Extract event date
            ev_date = _extract_date(text, today)

            # Build event title
            title = n.title[:80]
            # Use the matched phrase for clarity
            match_text = m.group(0)

            candidates.append(DetectedEvent(
                news_id=n.id,
                news_title=n.title,
                news_url=n.url,
                event_type=etype,
                industry=industry,
                industry_label=label,
                title=title,
                description=match_text,
                event_date=ev_date,
                impact_level=impact,
                related_keywords=[m.group(0)[:30]],
            ))
            break  # one event per news is enough

    # Dedupe by (title prefix, industry, event_type)
    seen: set[tuple[str, str, str]] = set()
    unique: list[DetectedEvent] = []
    for c in candidates:
        key = (c.title[:30], c.industry, c.event_type)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
        if len(unique) >= max_results:
            break

    return unique


def save_detected_events(candidates: list[DetectedEvent]) -> int:
    """Persist detected events. Idempotent: dedupe by (title, event_date)."""
    if not candidates:
        return 0
    db = get_db()
    written = 0
    today = date.today()
    with db.tx() as session:
        for c in candidates:
            ev_date = c.event_date or today
            existing = (
                session.query(IndustryEvent)
                .filter(IndustryEvent.title == c.title[:256])
                .filter(IndustryEvent.event_date == ev_date)
                .filter(IndustryEvent.source == "auto_detected")
                .first()
            )
            if existing:
                continue
            row = IndustryEvent(
                industry=c.industry,
                industry_label=c.industry_label,
                title=c.title[:256],
                description=(c.description + " [来源: " + c.news_title[:100] + "]")[:2000],
                event_type=c.event_type,
                event_date=ev_date,
                impact_level=c.impact_level,
                related_stocks="",
                source="auto_detected",
                source_url=c.news_url[:512],
                is_future=ev_date >= today,
            )
            session.add(row)
            written += 1
    return written


def detect_and_save(since: datetime | None = None) -> int:
    """One-shot: detect events from news and save to DB."""
    db = get_db()
    with db.session() as s:
        candidates = detect_from_news(s, since=since)
    n = save_detected_events(candidates)
    logger.info(f"Auto-detected {n} new events from news")
    return n
