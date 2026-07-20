"""Append additional curated events to industry_events.json (target: 100+)."""
import json
from pathlib import Path

JSON_PATH = Path("data/industry_events.json")

NEW_EVENTS = [
    # ===== Banking / Finance =====
    {
        "industry": "banking", "industry_label": "银行",
        "title": "六大行中期分红派发",
        "event_type": "earnings", "event_date": "2026-07-15",
        "impact_level": 4,
        "description": "工行/农行/中行/建行/交行/邮储 6 月集中派发 2025 年度中期分红",
        "related_stocks": "601398,601288,601988,601939,601328,601658"
    },
    {
        "industry": "banking", "industry_label": "银行",
        "title": "央行 Q2 货币政策报告",
        "event_type": "policy", "event_date": "2026-08-15",
        "impact_level": 4,
        "description": "央行发布 Q2 货币政策执行报告",
        "related_stocks": "601398,600036,601166"
    },
    {
        "industry": "banking", "industry_label": "银行",
        "title": "国有大行 H 股中期业绩",
        "event_type": "earnings", "event_date": "2026-08-28",
        "impact_level": 4,
        "description": "工建中农 4 大行 H 股中期业绩发布",
        "related_stocks": "601398,601939,601988,601288"
    },
    {
        "industry": "securities", "industry_label": "证券",
        "title": "头部券商 Q2 业绩",
        "event_type": "earnings", "event_date": "2026-08-25",
        "impact_level": 3,
        "description": "中信、华泰、国君等头部券商 Q2 业绩",
        "related_stocks": "600030,601688,601211"
    },
    {
        "industry": "securities", "industry_label": "证券",
        "title": "证监会并购重组新政",
        "event_type": "regulatory", "event_date": "2026-09-15",
        "impact_level": 4,
        "description": "证监会可能发布新一轮并购重组支持政策",
        "related_stocks": "600030,601688,601211"
    },

    # ===== Insurance =====
    {
        "industry": "insurance", "industry_label": "保险",
        "title": "上市险企 Q2 偿付能力报告",
        "event_type": "earnings", "event_date": "2026-08-20",
        "impact_level": 3,
        "description": "国寿/平安/太保 Q2 偿付能力季报",
        "related_stocks": "601628,601318,601601"
    },
    {
        "industry": "insurance", "industry_label": "保险",
        "title": "保险业新会计准则全面执行",
        "event_type": "regulatory", "event_date": "2026-09-30",
        "impact_level": 3,
        "description": "IFRS17 新会计准则全面执行，影响利润波动",
        "related_stocks": "601628,601318,601601"
    },

    # ===== Real Estate =====
    {
        "industry": "real_estate", "industry_label": "房地产",
        "title": "70 城房价数据 (6月)",
        "event_type": "data_release", "event_date": "2026-07-15",
        "impact_level": 4,
        "description": "国家统计局发布 6 月 70 城新建商品住宅价格",
        "related_stocks": "000002,600048,001979"
    },
    {
        "industry": "real_estate", "industry_label": "房地产",
        "title": "LPR 5 年期利率 (7月)",
        "event_type": "policy", "event_date": "2026-07-20",
        "impact_level": 5,
        "description": "5 年期 LPR 报价下调预期，影响房贷利率",
        "related_stocks": "000002,600048,001979,600340"
    },
    {
        "industry": "real_estate", "industry_label": "房地产",
        "title": "保交楼专项再贷款续作",
        "event_type": "policy", "event_date": "2026-08-30",
        "impact_level": 3,
        "description": "央行可能续作/扩大保交楼专项再贷款",
        "related_stocks": "000002,600048,600340"
    },
    {
        "industry": "real_estate", "industry_label": "房地产",
        "title": "城中村改造新一轮政策",
        "event_type": "policy", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "超大特大城市城中村改造扩围",
        "related_stocks": "000002,600048,601800"
    },

    # ===== Infrastructure =====
    {
        "industry": "infrastructure", "industry_label": "基建",
        "title": "万亿国债第二批项目",
        "event_type": "policy", "event_date": "2026-08-01",
        "impact_level": 4,
        "description": "万亿特别国债第二批项目落地",
        "related_stocks": "601800,601668,601390,600170"
    },
    {
        "industry": "infrastructure", "industry_label": "基建",
        "title": "雅鲁藏布江下游水电工程",
        "event_type": "other", "event_date": "2026-07-25",
        "impact_level": 5,
        "description": "雅鲁藏布江下游水电工程开工",
        "related_stocks": "600025,601727,600886"
    },
    {
        "industry": "infrastructure", "industry_label": "基建",
        "title": "新一轮基建项目集中开工",
        "event_type": "other", "event_date": "2026-09-10",
        "impact_level": 3,
        "description": "Q3 基建项目集中开工",
        "related_stocks": "601800,601668,601390"
    },

    # ===== Coal =====
    {
        "industry": "coal", "industry_label": "煤炭",
        "title": "港口煤炭库存周报",
        "event_type": "data_release", "event_date": "2026-07-10",
        "impact_level": 3,
        "description": "环渤海港口煤炭库存周度更新",
        "related_stocks": "601088,601225,601898"
    },
    {
        "industry": "coal", "industry_label": "煤炭",
        "title": "煤炭进口数据",
        "event_type": "data_release", "event_date": "2026-07-14",
        "impact_level": 3,
        "description": "6 月煤炭进口数据 (海关)",
        "related_stocks": "601088,601225,601898"
    },
    {
        "industry": "coal", "industry_label": "煤炭",
        "title": "煤炭长协合同谈判启动",
        "event_type": "other", "event_date": "2026-09-20",
        "impact_level": 3,
        "description": "2027 年煤炭中长期合同谈判启动",
        "related_stocks": "601088,601225,601898"
    },

    # ===== Telecom / 5G =====
    {
        "industry": "telecom", "industry_label": "通信",
        "title": "三大运营商半年报",
        "event_type": "earnings", "event_date": "2026-08-15",
        "impact_level": 3,
        "description": "移动/电信/联通 H1 业绩",
        "related_stocks": "600941,601728,600050"
    },
    {
        "industry": "telecom", "industry_label": "通信",
        "title": "6G 推进组新进展",
        "event_type": "other", "event_date": "2026-09-01",
        "impact_level": 3,
        "description": "工信部 6G 推进组技术试验新节点",
        "related_stocks": "600941,000063,002179"
    },
    {
        "industry": "telecom", "industry_label": "通信",
        "title": "光通信新品发布",
        "event_type": "other", "event_date": "2026-09-20",
        "impact_level": 3,
        "description": "光通信/光模块 1.6T/3.2T 新品发布",
        "related_stocks": "300502,300308,000063"
    },

    # ===== Internet =====
    {
        "industry": "internet", "industry_label": "互联网",
        "title": "阿里 Q1 财报",
        "event_type": "earnings", "event_date": "2026-08-15",
        "impact_level": 4,
        "description": "阿里 2026 财年 Q1 业绩，云业务+电商",
        "related_stocks": "9988,600050"
    },
    {
        "industry": "internet", "industry_label": "互联网",
        "title": "腾讯 Q2 财报",
        "event_type": "earnings", "event_date": "2026-08-13",
        "impact_level": 4,
        "description": "腾讯 H1 业绩，游戏+广告+金融科技",
        "related_stocks": "0700"
    },
    {
        "industry": "internet", "industry_label": "互联网",
        "title": "拼多多 Q2 财报",
        "event_type": "earnings", "event_date": "2026-08-25",
        "impact_level": 4,
        "description": "拼多多 Q2 业绩，海外 Temu 业务进展",
        "related_stocks": "PDD"
    },
    {
        "industry": "internet", "industry_label": "互联网",
        "title": "字节跳动 IPO 进展",
        "event_type": "other", "event_date": "2026-09-30",
        "impact_level": 4,
        "description": "字节跳动港股 IPO 进展更新",
        "related_stocks": ""
    },
    {
        "industry": "internet", "industry_label": "互联网",
        "title": "双 11 大促启动",
        "event_type": "other", "event_date": "2026-10-20",
        "impact_level": 4,
        "description": "2026 双 11 大促正式启动",
        "related_stocks": "9988,PDD,600050"
    },

    # ===== Gaming =====
    {
        "industry": "gaming", "industry_label": "游戏",
        "title": "腾讯网易 Q2 财报",
        "event_type": "earnings", "event_date": "2026-08-15",
        "impact_level": 3,
        "description": "腾讯/网易 Q2 业绩，重点游戏流水",
        "related_stocks": "0700,9999,002602"
    },
    {
        "industry": "gaming", "industry_label": "游戏",
        "title": "国家游戏版号发放",
        "event_type": "regulatory", "event_date": "2026-07-15",
        "impact_level": 3,
        "description": "国家新闻出版署月度游戏版号",
        "related_stocks": "002602,002624,300315"
    },
    {
        "industry": "gaming", "industry_label": "游戏",
        "title": "ChinaJoy 后行业数据",
        "event_type": "other", "event_date": "2026-08-10",
        "impact_level": 2,
        "description": "ChinaJoy 2026 后行业新品+流水跟踪",
        "related_stocks": "002602,002624"
    },

    # ===== Home Appliance =====
    {
        "industry": "home_appliance", "industry_label": "家电",
        "title": "美的/格力/海尔 Q2 业绩",
        "event_type": "earnings", "event_date": "2026-08-28",
        "impact_level": 4,
        "description": "美的、格力、海尔 Q2 业绩，海外业务",
        "related_stocks": "000333,000651,600690"
    },
    {
        "industry": "home_appliance", "industry_label": "家电",
        "title": "国家家电以旧换新政策",
        "event_type": "policy", "event_date": "2026-08-15",
        "impact_level": 4,
        "description": "新一轮家电以旧换新补贴政策",
        "related_stocks": "000333,000651,600690,000810"
    },
    {
        "industry": "home_appliance", "industry_label": "家电",
        "title": "AWE 2026 中国家电博览会",
        "event_type": "conference", "event_date": "2026-09-25",
        "impact_level": 3,
        "description": "中国家电及消费电子博览会",
        "related_stocks": "000333,000651,600690"
    },

    # ===== Apparel =====
    {
        "industry": "apparel", "industry_label": "服装",
        "title": "安踏李宁 H1 业绩",
        "event_type": "earnings", "event_date": "2026-08-20",
        "impact_level": 3,
        "description": "安踏体育、李宁、特步 H1 业绩",
        "related_stocks": "2020,2331,1368"
    },
    {
        "industry": "apparel", "industry_label": "服装",
        "title": "申洲国际 H1 业绩",
        "event_type": "earnings", "event_date": "2026-08-25",
        "impact_level": 3,
        "description": "申洲国际 H1 业绩，Nike/Adidas 订单",
        "related_stocks": "2313"
    },
    {
        "industry": "apparel", "industry_label": "服装",
        "title": "棉花期货主力合约",
        "event_type": "data_release", "event_date": "2026-09-01",
        "impact_level": 2,
        "description": "棉花主力合约价格波动",
        "related_stocks": "600400,002154"
    },

    # ===== Retail =====
    {
        "industry": "retail", "industry_label": "零售",
        "title": "社会消费品零售总额 (6月)",
        "event_type": "data_release", "event_date": "2026-07-16",
        "impact_level": 3,
        "description": "6 月社零数据",
        "related_stocks": "601933,002024,600415"
    },
    {
        "industry": "retail", "industry_label": "零售",
        "title": "黄金珠宝终端动销",
        "event_type": "other", "event_date": "2026-08-15",
        "impact_level": 3,
        "description": "周大福、老凤祥 Q2 黄金珠宝动销",
        "related_stocks": "600612,002867"
    },

    # ===== Hydrogen =====
    {
        "industry": "hydrogen", "industry_label": "氢能",
        "title": "国家氢能产业规划",
        "event_type": "policy", "event_date": "2026-09-15",
        "impact_level": 4,
        "description": "国家氢能中长期规划发布",
        "related_stocks": "002648,000338,600188"
    },
    {
        "industry": "hydrogen", "industry_label": "氢能",
        "title": "绿氢项目招标密集期",
        "event_type": "other", "event_date": "2026-09-25",
        "impact_level": 3,
        "description": "Q3 绿氢/电解槽项目招标",
        "related_stocks": "002648,000338"
    },

    # ===== Energy Storage =====
    {
        "industry": "energy_storage", "industry_label": "储能",
        "title": "工商业储能项目集中并网",
        "event_type": "other", "event_date": "2026-08-30",
        "impact_level": 3,
        "description": "夏季用电高峰，工商业储能并网加速",
        "related_stocks": "300750,002074,300014"
    },
    {
        "industry": "energy_storage", "industry_label": "储能",
        "title": "独立储能电站项目招标",
        "event_type": "other", "event_date": "2026-09-15",
        "impact_level": 3,
        "description": "Q3 独立储能电站项目集中招标",
        "related_stocks": "300750,002074,300014"
    },

    # ===== Robotics =====
    {
        "industry": "robotics", "industry_label": "机器人",
        "title": "Tesla Optimus 量产进展",
        "event_type": "other", "event_date": "2026-09-15",
        "impact_level": 4,
        "description": "Tesla Optimus 人形机器人量产进展",
        "related_stocks": "002472,300124,300607"
    },
    {
        "industry": "robotics", "industry_label": "机器人",
        "title": "中国机器人产业大会",
        "event_type": "conference", "event_date": "2026-08-20",
        "impact_level": 3,
        "description": "2026 中国机器人产业发展大会",
        "related_stocks": "002472,300124,300607"
    },
    {
        "industry": "robotics", "industry_label": "机器人",
        "title": "Figure AI / 1X 新一代人形机器人发布",
        "event_type": "other", "event_date": "2026-10-10",
        "impact_level": 4,
        "description": "Figure / 1X / 优必选新一代人形机器人发布",
        "related_stocks": "002472,300124,300607"
    },

    # ===== Satellite Internet =====
    {
        "industry": "satellite_internet", "industry_label": "卫星互联网",
        "title": "GW 星座首次组网发射",
        "event_type": "launch", "event_date": "2026-08-15",
        "impact_level": 5,
        "description": "中国星网 GW 星座 2026 年首次大规模组网",
        "related_stocks": "600118,300342,002446,600151"
    },
    {
        "industry": "satellite_internet", "industry_label": "卫星互联网",
        "title": "千帆星座 648 颗里程碑",
        "event_type": "other", "event_date": "2026-12-31",
        "impact_level": 5,
        "description": "千帆星座 648 颗卫星组网完成",
        "related_stocks": "600118,300342"
    },

    # ===== Coal & Power (extra) =====
    {
        "industry": "power", "industry_label": "电力",
        "title": "夏季用电高峰",
        "event_type": "other", "event_date": "2026-08-01",
        "impact_level": 3,
        "description": "夏季用电高峰，火电+新能源出力",
        "related_stocks": "600886,600025,600900"
    },
    {
        "industry": "power", "industry_label": "电力",
        "title": "全国电力市场化交易新规",
        "event_type": "policy", "event_date": "2026-09-30",
        "impact_level": 3,
        "description": "电力市场化改革新规发布",
        "related_stocks": "600886,600900,002028"
    },

    # ===== Defense Military (extra) =====
    {
        "industry": "defense", "industry_label": "军工",
        "title": "国防白皮书发布",
        "event_type": "policy", "event_date": "2026-10-15",
        "impact_level": 4,
        "description": "国防部发布新一期国防白皮书",
        "related_stocks": "600760,000768,600316"
    },
    {
        "industry": "defense", "industry_label": "军工",
        "title": "军工集团 Q2 业绩",
        "event_type": "earnings", "event_date": "2026-08-25",
        "impact_level": 3,
        "description": "十大军工集团 Q2 业绩",
        "related_stocks": "600760,000768,600316,600038"
    },

    # ===== Wine / Baijiu (extra) =====
    {
        "industry": "wine", "industry_label": "白酒",
        "title": "茅台 Q2 业绩 + 直销放量",
        "event_type": "earnings", "event_date": "2026-08-15",
        "impact_level": 4,
        "description": "贵州茅台 Q2 业绩， iMaohang 渠道改革",
        "related_stocks": "600519"
    },
    {
        "industry": "wine", "industry_label": "白酒",
        "title": "糖酒会秋季 (秋糖)",
        "event_type": "conference", "event_date": "2026-10-15",
        "impact_level": 3,
        "description": "秋季糖酒会，行业风向",
        "related_stocks": "600519,000858,000568,002304"
    },

    # ===== New Materials (extra) =====
    {
        "industry": "new_materials", "industry_label": "新材料",
        "title": "碳纤维国产替代关键节点",
        "event_type": "other", "event_date": "2026-09-10",
        "impact_level": 3,
        "description": "T1000 级碳纤维量产/降价节点",
        "related_stocks": "300699,002709,002297"
    },
    {
        "industry": "new_materials", "industry_label": "新材料",
        "title": "光刻胶国产化突破",
        "event_type": "other", "event_date": "2026-09-30",
        "impact_level": 4,
        "description": "ArF/KrF 光刻胶国产化进展",
        "related_stocks": "688268,300346,300285"
    },

    # ===== Smart Driving =====
    {
        "industry": "smart_driving", "industry_label": "智能驾驶",
        "title": "L3 级自动驾驶试点扩围",
        "event_type": "regulatory", "event_date": "2026-09-15",
        "impact_level": 4,
        "description": "工信部 L3/L4 试点城市扩围",
        "related_stocks": "002230,002405,002920,300496"
    },
    {
        "industry": "smart_driving", "industry_label": "智能驾驶",
        "title": "车路云一体化试点",
        "event_type": "policy", "event_date": "2026-08-15",
        "impact_level": 3,
        "description": "20 城车路云一体化试点",
        "related_stocks": "002230,002405,002920"
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
