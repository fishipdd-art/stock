"""Append more curated events to industry_events.json (target: 150+)."""
import json
from pathlib import Path

JSON_PATH = Path("data/industry_events.json")

NEW_EVENTS = [
    # ===== Education =====
    {
        "industry": "education", "industry_label": "教育",
        "title": "K12 暑期招生季数据",
        "event_type": "data_release", "event_date": "2026-09-01",
        "impact_level": 3,
        "description": "K12 教育公司暑期招生数据陆续披露",
        "related_stocks": "002607,300192,002659"
    },
    {
        "industry": "education", "industry_label": "教育",
        "title": "新东方/好未来 Q1 财报",
        "event_type": "earnings", "event_date": "2026-10-15",
        "impact_level": 3,
        "description": "新东方、好未来 Q1 财报",
        "related_stocks": "EDU,TAL"
    },

    # ===== Tourism =====
    {
        "industry": "tourism", "industry_label": "旅游",
        "title": "国庆旅游数据",
        "event_type": "data_release", "event_date": "2026-10-08",
        "impact_level": 4,
        "description": "国庆假期旅游人次/收入数据",
        "related_stocks": "601888,300144,600054"
    },
    {
        "industry": "tourism", "industry_label": "旅游",
        "title": "暑期旅游高峰",
        "event_type": "other", "event_date": "2026-08-01",
        "impact_level": 3,
        "description": "暑期旅游出行高峰",
        "related_stocks": "601888,300144,600054"
    },
    {
        "industry": "tourism", "industry_label": "旅游",
        "title": "中国旅游集团合并预期",
        "event_type": "m&a", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "中国旅游集团/中免整合预期",
        "related_stocks": "601888,300144"
    },

    # ===== Cosmetics =====
    {
        "industry": "cosmetics", "industry_label": "化妆品",
        "title": "618 化妆品销售数据",
        "event_type": "data_release", "event_date": "2026-07-10",
        "impact_level": 3,
        "description": "618 电商节化妆品类目 GMV 增长",
        "related_stocks": "603605,300740,300957"
    },
    {
        "industry": "cosmetics", "industry_label": "化妆品",
        "title": "珀莱雅/贝泰妮 H1 业绩",
        "event_type": "earnings", "event_date": "2026-08-25",
        "impact_level": 3,
        "description": "珀莱雅、贝泰妮 H1 业绩，抖音/天猫渠道",
        "related_stocks": "603605,300740,300957"
    },

    # ===== Food & Beverage (extra) =====
    {
        "industry": "food_beverage", "industry_label": "食品饮料",
        "title": "伊利/蒙牛 H1 业绩",
        "event_type": "earnings", "event_date": "2026-08-30",
        "impact_level": 3,
        "description": "伊利、蒙牛 H1 业绩，奶价走势",
        "related_stocks": "600887,600597"
    },
    {
        "industry": "food_beverage", "industry_label": "食品饮料",
        "title": "海天味业/双汇 H1 业绩",
        "event_type": "earnings", "event_date": "2026-08-25",
        "impact_level": 3,
        "description": "海天味业、双汇发展 H1 业绩",
        "related_stocks": "603288,000895"
    },
    {
        "industry": "food_beverage", "industry_label": "食品饮料",
        "title": "东鹏/养元 H1 业绩",
        "event_type": "earnings", "event_date": "2026-08-20",
        "impact_level": 3,
        "description": "东鹏饮料、养元饮品 H1 业绩",
        "related_stocks": "605499,603156"
    },

    # ===== E-sports =====
    {
        "industry": "esports", "industry_label": "电竞",
        "title": "英雄联盟全球总决赛",
        "event_type": "other", "event_date": "2026-10-25",
        "impact_level": 3,
        "description": "S15 英雄联盟全球总决赛",
        "related_stocks": "002602,002624"
    },
    {
        "industry": "esports", "industry_label": "电竞",
        "title": "DOTA2 国际邀请赛 TI",
        "event_type": "other", "event_date": "2026-09-10",
        "impact_level": 3,
        "description": "DOTA2 TI 国际邀请赛",
        "related_stocks": "002602,002624"
    },

    # ===== AR/VR =====
    {
        "industry": "ar_vr", "industry_label": "AR/VR",
        "title": "Apple Vision Pro 中国上市",
        "event_type": "other", "event_date": "2026-09-15",
        "impact_level": 4,
        "description": "Apple Vision Pro 国行版正式上市",
        "related_stocks": "002475,300433,002241,300115"
    },
    {
        "industry": "ar_vr", "industry_label": "AR/VR",
        "title": "PICO 5 新品发布",
        "event_type": "other", "event_date": "2026-10-15",
        "impact_level": 3,
        "description": "字节跳动 PICO 5 VR 头显发布",
        "related_stocks": "002475,300433"
    },

    # ===== Hydrogen (extra) =====
    {
        "industry": "hydrogen", "industry_label": "氢能",
        "title": "氢能国标发布",
        "event_type": "regulatory", "event_date": "2026-08-20",
        "impact_level": 4,
        "description": "氢能制储输用全产业链国家标准发布",
        "related_stocks": "002648,000338,600188"
    },

    # ===== Sensors =====
    {
        "industry": "sensors", "industry_label": "传感器",
        "title": "CMOS 图像传感器新品",
        "event_type": "other", "event_date": "2026-09-20",
        "impact_level": 3,
        "description": "索尼/三星/豪威 CMOS 图像传感器新品",
        "related_stocks": "603501,002241,300672"
    },

    # ===== Plastic =====
    {
        "industry": "plastic", "industry_label": "塑料",
        "title": "PVC/PE/PP 期货新合约",
        "event_type": "other", "event_date": "2026-09-01",
        "impact_level": 2,
        "description": "大商所/郑商所化工品新合约上市",
        "related_stocks": "600309,002648"
    },

    # ===== Environmental =====
    {
        "industry": "environmental", "industry_label": "环保",
        "title": "全国碳市场扩容",
        "event_type": "policy", "event_date": "2026-10-01",
        "impact_level": 4,
        "description": "全国碳排放权交易市场扩容，纳入钢铁/水泥/铝",
        "related_stocks": "300070,300152,002340"
    },
    {
        "industry": "environmental", "industry_label": "环保",
        "title": "CCER 重启",
        "event_type": "policy", "event_date": "2026-08-15",
        "impact_level": 3,
        "description": "中国核证自愿减排量 (CCER) 重启",
        "related_stocks": "300070,300152"
    },

    # ===== Coal Chemical =====
    {
        "industry": "coal_chem", "industry_label": "煤化工",
        "title": "煤化工产品涨价",
        "event_type": "price_change", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "甲醇/尿素/醋酸等煤化工产品涨价",
        "related_stocks": "600188,600691,600426"
    },

    # ===== Machine Tool =====
    {
        "industry": "machine_tool", "industry_label": "机床",
        "title": "CIMT 2026 中国国际机床展",
        "event_type": "conference", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "CIMT 2026 机床展，五轴联动机床/工业母机",
        "related_stocks": "000837,002008,300083"
    },

    # ===== Military Aircraft =====
    {
        "industry": "aviation", "industry_label": "航空",
        "title": "C919 国产大飞机交付",
        "event_type": "other", "event_date": "2026-09-30",
        "impact_level": 5,
        "description": "C919 国产大飞机月度交付量更新",
        "related_stocks": "600029,600316,000768"
    },
    {
        "industry": "aviation", "industry_label": "航空",
        "title": "C929 中俄宽体机进展",
        "event_type": "other", "event_date": "2026-10-15",
        "impact_level": 4,
        "description": "C929 中俄宽体大飞机研制进展",
        "related_stocks": "600029,600316"
    },
    {
        "industry": "aviation", "industry_label": "航空",
        "title": "国产航空发动机 CJ-1000A",
        "event_type": "other", "event_date": "2026-08-15",
        "impact_level": 4,
        "description": "国产 CJ-1000A 航空发动机装机/试飞",
        "related_stocks": "600893,000738,600391"
    },

    # ===== Cyberspace =====
    {
        "industry": "cyberspace", "industry_label": "网络安全",
        "title": "国家网络安全宣传周",
        "event_type": "other", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "国家网络安全宣传周开幕",
        "related_stocks": "300454,300033,002439"
    },
    {
        "industry": "cyberspace", "industry_label": "网络安全",
        "title": "等保 2.0 新规修订",
        "event_type": "regulatory", "event_date": "2026-10-15",
        "impact_level": 3,
        "description": "等级保护 2.0 标准修订发布",
        "related_stocks": "300454,300033,002439"
    },

    # ===== Printing =====
    {
        "industry": "printing", "industry_label": "印刷包装",
        "title": "白卡纸涨价函",
        "event_type": "price_change", "event_date": "2026-08-20",
        "impact_level": 3,
        "description": "白卡纸头部企业发布涨价函",
        "related_stocks": "600308,002521"
    },

    # ===== High-end Equipment =====
    {
        "industry": "high_end_equip", "industry_label": "高端装备",
        "title": "工业母机国产替代加速",
        "event_type": "policy", "event_date": "2026-09-20",
        "impact_level": 3,
        "description": "工信部推进工业母机国产替代",
        "related_stocks": "000837,002008"
    },

    # ===== LNG (extra) =====
    {
        "industry": "lng", "industry_label": "LNG",
        "title": "LNG 接收站新项目",
        "event_type": "other", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "LNG 接收站新项目获批/投产",
        "related_stocks": "600188,601808,600583"
    },
    {
        "industry": "lng", "industry_label": "LNG",
        "title": "全球 LNG 现货价",
        "event_type": "data_release", "event_date": "2026-08-30",
        "impact_level": 3,
        "description": "全球 LNG 现货价格周度",
        "related_stocks": "600188,601808"
    },

    # ===== Battery Materials (extra) =====
    {
        "industry": "battery_material", "industry_label": "电池材料",
        "title": "磷酸铁锂正极涨价",
        "event_type": "price_change", "event_date": "2026-08-15",
        "impact_level": 4,
        "description": "磷酸铁锂正极材料价格上调",
        "related_stocks": "002460,002466,300769"
    },
    {
        "industry": "battery_material", "industry_label": "电池材料",
        "title": "电解液溶剂 EC/DMC",
        "event_type": "price_change", "event_date": "2026-09-10",
        "impact_level": 3,
        "description": "锂电电解液溶剂 EC/DMC 价格",
        "related_stocks": "002709,300037"
    },

    # ===== Coal-to-Chemicals (extra) =====
    {
        "industry": "ctc", "industry_label": "煤制烯烃",
        "title": "CTO/MTO 装置检修季",
        "event_type": "other", "event_date": "2026-09-01",
        "impact_level": 3,
        "description": "煤制烯烃 CTO/MTO 装置秋季检修",
        "related_stocks": "600188,002092"
    },

    # ===== Innovative Drug BD =====
    {
        "industry": "bd_deal", "industry_label": "创新药出海",
        "title": "国产创新药海外授权 (BD) 窗口期",
        "event_type": "other", "event_date": "2026-09-30",
        "impact_level": 4,
        "description": "Q3 国产创新药海外授权 (BD) 集中签约",
        "related_stocks": "688180,688578,688506,600276"
    },

    # ===== Wind (extra) =====
    {
        "industry": "wind", "industry_label": "风电",
        "title": "海上风电 18MW 风机下线",
        "event_type": "other", "event_date": "2026-08-30",
        "impact_level": 4,
        "description": "金风/明阳/电气风电 18MW 海上风机下线",
        "related_stocks": "002202,300772,600416"
    },
    {
        "industry": "wind", "industry_label": "风电",
        "title": "风电齿轮箱/轴承涨价",
        "event_type": "price_change", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "风电齿轮箱/轴承涨价",
        "related_stocks": "002472,300707,300718"
    },

    # ===== Nuclear =====
    {
        "industry": "nuclear", "industry_label": "核电",
        "title": "核电核准新机组",
        "event_type": "policy", "event_date": "2026-08-15",
        "impact_level": 4,
        "description": "国务院核准新一批核电机组",
        "related_stocks": "601985,002167,002438"
    },

    # ===== Smart Home =====
    {
        "industry": "smart_home", "industry_label": "智能家居",
        "title": "Matter 协议新版本",
        "event_type": "other", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "Matter 智能家居协议新版本发布",
        "related_stocks": "002241,300433,002420"
    },
]


def main():
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    existing_keys = {(e["title"], e["event_date"]) for e in data["events"]}
    added = 0
    for ev in NEW_EVENTS:
        key = (ev["title"], ev["event_date"])
        if key in existing_keys:
            continue
        ev.setdefault("source_url", "")
        data["events"].append(ev)
        added += 1
    if added:
        JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Added {added} new events. Total: {len(data['events'])}")


if __name__ == "__main__":
    main()
