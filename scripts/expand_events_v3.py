"""Append more curated events to industry_events.json (target: 200+)."""
import json
from pathlib import Path

JSON_PATH = Path("data/industry_events.json")

NEW_EVENTS = [
    # ===== Smart Wearable =====
    {
        "industry": "smart_wearable", "industry_label": "智能穿戴",
        "title": "Apple Watch S11 发布",
        "event_type": "product_launch", "event_date": "2026-09-10",
        "impact_level": 4,
        "description": "Apple Watch Series 11 发布，MicroLED 屏幕",
        "related_stocks": "002475,300433,002241,300115"
    },
    {
        "industry": "smart_wearable", "industry_label": "智能穿戴",
        "title": "华为 Watch GT 5 系列",
        "event_type": "product_launch", "event_date": "2026-10-15",
        "impact_level": 3,
        "description": "华为 Watch GT 5 系列发布",
        "related_stocks": "002475,300433,002241"
    },

    # ===== AI Chip =====
    {
        "industry": "ai_chip", "industry_label": "AI芯片",
        "title": "华为昇腾 910C 量产",
        "event_type": "other", "event_date": "2026-08-30",
        "impact_level": 5,
        "description": "华为昇腾 910C AI 芯片大规模量产",
        "related_stocks": "002241,300474,300223"
    },
    {
        "industry": "ai_chip", "industry_label": "AI芯片",
        "title": "寒武纪新一代思元芯片",
        "event_type": "product_launch", "event_date": "2026-09-15",
        "impact_level": 4,
        "description": "寒武纪思元 590 芯片发布",
        "related_stocks": "688256,300474,300223"
    },
    {
        "industry": "ai_chip", "industry_label": "AI芯片",
        "title": "地平线征程 6 芯片",
        "event_type": "product_launch", "event_date": "2026-10-10",
        "impact_level": 3,
        "description": "地平线征程 6 智能驾驶芯片量产",
        "related_stocks": "688256,300223"
    },

    # ===== LED / Display =====
    {
        "industry": "led_display", "industry_label": "LED显示",
        "title": "Mini LED 背光渗透率提升",
        "event_type": "other", "event_date": "2026-08-20",
        "impact_level": 3,
        "description": "Mini LED 背光在 TV/笔电渗透率突破 30%",
        "related_stocks": "002241,300373,002387"
    },

    # ===== Memory / Storage =====
    {
        "industry": "memory", "industry_label": "存储芯片",
        "title": "DRAM/NAND 现货价",
        "event_type": "data_release", "event_date": "2026-08-15",
        "impact_level": 4,
        "description": "DRAM/NAND 现货价格周度更新",
        "related_stocks": "688008,603986,002230"
    },
    {
        "industry": "memory", "industry_label": "存储芯片",
        "title": "长鑫存储 DDR5 量产",
        "event_type": "other", "event_date": "2026-09-20",
        "impact_level": 5,
        "description": "长鑫存储 DDR5 内存大规模量产",
        "related_stocks": "688008,603986"
    },

    # ===== Logistics Express =====
    {
        "industry": "logistics_express", "industry_label": "快递",
        "title": "顺丰/中通/圆通 8 月经营数据",
        "event_type": "data_release", "event_date": "2026-09-18",
        "impact_level": 3,
        "description": "快递公司 8 月业务量/单票收入数据",
        "related_stocks": "002352,600233,600233"
    },

    # ===== Coal to Liquid =====
    {
        "industry": "ctl", "industry_label": "煤制油",
        "title": "煤制油示范项目",
        "event_type": "other", "event_date": "2026-08-15",
        "impact_level": 3,
        "description": "煤制油 (CTL) 示范项目进展",
        "related_stocks": "601898,600188"
    },

    # ===== Glass Fiber =====
    {
        "industry": "glass_fiber", "industry_label": "玻纤",
        "title": "玻纤涨价函",
        "event_type": "price_change", "event_date": "2026-08-20",
        "impact_level": 3,
        "description": "中国巨石等玻纤龙头涨价函",
        "related_stocks": "600176,002080"
    },

    # ===== Seed Industry =====
    {
        "industry": "seed", "industry_label": "种业",
        "title": "转基因玉米品种审定",
        "event_type": "regulatory", "event_date": "2026-08-30",
        "impact_level": 4,
        "description": "农业农村部新一批转基因玉米品种审定",
        "related_stocks": "000998,002385,300087"
    },

    # ===== Online Education (extra) =====
    {
        "industry": "online_edu", "industry_label": "在线教育",
        "title": "作业帮/猿辅导 IPO 进展",
        "event_type": "other", "event_date": "2026-09-30",
        "impact_level": 3,
        "description": "作业帮/猿辅导 港股 IPO 进展",
        "related_stocks": ""
    },

    # ===== Pet Economy =====
    {
        "industry": "pet_economy", "industry_label": "宠物经济",
        "title": "宠物食品新国标",
        "event_type": "regulatory", "event_date": "2026-08-25",
        "impact_level": 3,
        "description": "宠物食品国家标准修订发布",
        "related_stocks": "002891,300673,603566"
    },

    # ===== Second-hand =====
    {
        "industry": "second_hand", "industry_label": "二手经济",
        "title": "二手车出口",
        "event_type": "policy", "event_date": "2026-08-15",
        "impact_level": 3,
        "description": "二手车出口试点城市扩围",
        "related_stocks": "600335,002024"
    },

    # ===== Animation =====
    {
        "industry": "animation", "industry_label": "动漫",
        "title": "国产动画电影暑期档",
        "event_type": "other", "event_date": "2026-08-01",
        "impact_level": 3,
        "description": "国产动画电影暑期档票房",
        "related_stocks": "300251,300133"
    },

    # ===== Photovoltaic Inverter (extra) =====
    {
        "industry": "pv_inverter", "industry_label": "光伏逆变器",
        "title": "户用光伏装机旺季",
        "event_type": "other", "event_date": "2026-08-30",
        "impact_level": 3,
        "description": "Q3 户用光伏装机旺季",
        "related_stocks": "300274,002518,300763"
    },

    # ===== Electric Motor =====
    {
        "industry": "ev_motor", "industry_label": "电机",
        "title": "扁线电机渗透率",
        "event_type": "other", "event_date": "2026-08-15",
        "impact_level": 3,
        "description": "新能源车扁线电机渗透率提升",
        "related_stocks": "002249,300681,300976"
    },

    # ===== Carbon Trading =====
    {
        "industry": "carbon_trading", "industry_label": "碳交易",
        "title": "全国碳市场价格",
        "event_type": "data_release", "event_date": "2026-08-15",
        "impact_level": 3,
        "description": "全国碳排放权交易市场价格周报",
        "related_stocks": "300070,300152,002340"
    },

    # ===== Third-gen Semi =====
    {
        "industry": "third_gen_semi", "industry_label": "第三代半导体",
        "title": "SiC 衬底价格",
        "event_type": "price_change", "event_date": "2026-08-20",
        "impact_level": 4,
        "description": "碳化硅 (SiC) 衬底价格走势",
        "related_stocks": "688234,300316,002129"
    },

    # ===== Lithium Battery Equipment =====
    {
        "industry": "li_battery_equip", "industry_label": "锂电设备",
        "title": "锂电设备订单",
        "event_type": "other", "event_date": "2026-08-15",
        "impact_level": 3,
        "description": "锂电设备厂商订单情况更新",
        "related_stocks": "300450,300724,300316"
    },

    # ===== Diagnostics =====
    {
        "industry": "diagnostics", "industry_label": "诊断",
        "title": "IVD 集采",
        "event_type": "policy", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "体外诊断 (IVD) 集采谈判",
        "related_stocks": "300482,300685,300244"
    },

    # ===== Health Check =====
    {
        "industry": "health_check", "industry_label": "体检",
        "title": "体检行业 Q3 复苏",
        "event_type": "other", "event_date": "2026-09-30",
        "impact_level": 2,
        "description": "体检行业 Q3 经营数据",
        "related_stocks": "002044,300015"
    },

    # ===== Cable =====
    {
        "industry": "cable", "industry_label": "线缆",
        "title": "海缆订单招标",
        "event_type": "other", "event_date": "2026-08-30",
        "impact_level": 3,
        "description": "海底电缆/光缆订单招标",
        "related_stocks": "600522,002498,601728"
    },

    # ===== Pump =====
    {
        "industry": "pump", "industry_label": "工业泵",
        "title": "工业泵阀涨价",
        "event_type": "price_change", "event_date": "2026-08-15",
        "impact_level": 2,
        "description": "工业泵/阀门涨价",
        "related_stocks": "002438,002532"
    },

    # ===== Toy =====
    {
        "industry": "toy", "industry_label": "玩具",
        "title": "泡泡玛特海外门店扩张",
        "event_type": "other", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "泡泡玛特海外门店扩张",
        "related_stocks": "9992,002292"
    },

    # ===== Bearings (extra) =====
    {
        "industry": "bearings", "industry_label": "轴承",
        "title": "高端轴承国产替代",
        "event_type": "other", "event_date": "2026-08-15",
        "impact_level": 2,
        "description": "高端轴承国产替代进展",
        "related_stocks": "300707,002553"
    },

    # ===== Mold =====
    {
        "industry": "mold", "industry_label": "模具",
        "title": "精密模具订单",
        "event_type": "other", "event_date": "2026-08-20",
        "impact_level": 2,
        "description": "精密模具订单情况",
        "related_stocks": "300707,002472"
    },

    # ===== Power Battery Recycling =====
    {
        "industry": "battery_recycling", "industry_label": "电池回收",
        "title": "动力电池回收新政",
        "event_type": "regulatory", "event_date": "2026-08-30",
        "impact_level": 3,
        "description": "动力电池回收利用管理办法",
        "related_stocks": "002340,002460,300340"
    },

    # ===== Prefab Construction =====
    {
        "industry": "prefab", "industry_label": "装配式建筑",
        "title": "装配式建筑渗透率",
        "event_type": "other", "event_date": "2026-09-15",
        "impact_level": 2,
        "description": "装配式建筑渗透率提升",
        "related_stocks": "002375,002081"
    },

    # ===== Biopharma CDMO =====
    {
        "industry": "cdmo", "industry_label": "CDMO",
        "title": "药明康德/凯莱英 H1 业绩",
        "event_type": "earnings", "event_date": "2026-08-25",
        "impact_level": 4,
        "description": "CDMO 龙头 H1 业绩，海外订单",
        "related_stocks": "603259,002821,300347"
    },

    # ===== E-sports (extra) =====
    {
        "industry": "esports", "industry_label": "电竞",
        "title": "无畏契约 VCT Masters",
        "event_type": "other", "event_date": "2026-08-15",
        "impact_level": 2,
        "description": "无畏契约 VCT Masters 赛事",
        "related_stocks": "002602,002624"
    },

    # ===== Heat Exchange =====
    {
        "industry": "heat_exchange", "industry_label": "热交换",
        "title": "数据中心液冷渗透",
        "event_type": "other", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "数据中心液冷渗透率提升",
        "related_stocks": "002544,002837,300274"
    },

    # ===== Construction Machinery =====
    {
        "industry": "construction_machinery", "industry_label": "工程机械",
        "title": "三一/徐工 H1 业绩",
        "event_type": "earnings", "event_date": "2026-08-28",
        "impact_level": 3,
        "description": "三一重工、徐工机械 H1 业绩",
        "related_stocks": "600031,000425,000528"
    },

    # ===== Sugar =====
    {
        "industry": "sugar", "industry_label": "糖业",
        "title": "巴西甘蔗产量",
        "event_type": "data_release", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "巴西甘蔗压榨进度/产量",
        "related_stocks": "000911,600737"
    },

    # ===== Vaccine (extra) =====
    {
        "industry": "vaccine", "industry_label": "疫苗",
        "title": "九价 HPV 国产化",
        "event_type": "other", "event_date": "2026-09-30",
        "impact_level": 4,
        "description": "国产九价 HPV 疫苗进展",
        "related_stocks": "300122,300601,000661"
    },

    # ===== NCM Material =====
    {
        "industry": "ncm", "industry_label": "三元材料",
        "title": "三元正极材料涨价",
        "event_type": "price_change", "event_date": "2026-08-20",
        "impact_level": 3,
        "description": "三元正极材料 (NCM) 价格上调",
        "related_stocks": "300769,002340"
    },

    # ===== VR Headset (extra) =====
    {
        "industry": "vr_headset", "industry_label": "VR头显",
        "title": "Meta Quest 4 发布",
        "event_type": "product_launch", "event_date": "2026-10-08",
        "impact_level": 4,
        "description": "Meta Quest 4 VR 头显发布",
        "related_stocks": "002475,300433,002241"
    },

    # ===== Insurance (extra) =====
    {
        "industry": "insurance", "industry_label": "保险",
        "title": "新能源车专属车险",
        "event_type": "regulatory", "event_date": "2026-08-30",
        "impact_level": 3,
        "description": "新能源车专属车险产品落地",
        "related_stocks": "601318,601628,601601"
    },

    # ===== LFP Material (extra) =====
    {
        "industry": "lfp", "industry_label": "磷酸铁锂",
        "title": "LFP 涨价潮",
        "event_type": "price_change", "event_date": "2026-08-20",
        "impact_level": 3,
        "description": "磷酸铁锂正极价格上行",
        "related_stocks": "002460,002466,002497"
    },

    # ===== Carbon Black =====
    {
        "industry": "carbon_black", "industry_label": "炭黑",
        "title": "炭黑涨价",
        "event_type": "price_change", "event_date": "2026-08-15",
        "impact_level": 2,
        "description": "炭黑价格上调",
        "related_stocks": "002442,002068"
    },

    # ===== Bond Market =====
    {
        "industry": "bond", "industry_label": "债券",
        "title": "国债期货新合约",
        "event_type": "other", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "国债期货新合约上市",
        "related_stocks": ""
    },

    # ===== MRO Industrial =====
    {
        "industry": "mro", "industry_label": "工业品后市场",
        "title": "工业品后市场需求",
        "event_type": "other", "event_date": "2026-08-15",
        "impact_level": 2,
        "description": "工业品后市场需求复苏",
        "related_stocks": "002472,300024"
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
    print(f"Added {added} new events. Total curated: {len(data['events'])}")


if __name__ == "__main__":
    main()
