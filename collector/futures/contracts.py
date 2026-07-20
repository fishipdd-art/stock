"""
Static mapping of futures commodity symbols to supply-chain industry categories.

The industry labels mirror the categories used in
``data/knowledge_graph/supply_chain_terms.json`` so futures price moves can be
joined with sector hotness / A-share impact analysis downstream.

This module is intentionally *pure* (no DB, no network) so it can be imported
cheaply by hot-path code (e.g. realtime tagger, report generator).
"""
from __future__ import annotations

from typing import Final


# ---------------------------------------------------------------------------
# Symbol prefix -> industry category (Chinese label)
# ---------------------------------------------------------------------------
COMMODITY_TO_INDUSTRY: Final[dict[str, str]] = {
    # 有色金属
    "CU": "有色金属", "AL": "有色金属", "ZN": "有色金属", "NI": "有色金属",
    "PB": "有色金属", "SN": "有色金属", "BC": "有色金属", "SS": "不锈钢",
    "AO": "有色金属", "AD": "有色金属",
    # 贵金属
    "AU": "贵金属", "AG": "贵金属", "PT": "贵金属", "PD": "贵金属",
    # 黑色系
    "RB": "黑色系", "I": "黑色系", "J": "黑色系", "JM": "黑色系",
    "HC": "黑色系", "SF": "黑色系", "SM": "黑色系", "WR": "黑色系",
    # 农产品
    "SR": "农产品", "CF": "农产品", "OI": "农产品", "RM": "农产品",
    "M": "农产品", "Y": "农产品", "P": "农产品", "C": "农产品",
    "A": "农产品", "B": "农产品", "JD": "农产品", "FB": "农产品",
    "BB": "农产品", "CS": "农产品", "RR": "农产品", "LH": "农产品",
    "PM": "农产品", "WH": "农产品", "RI": "农产品", "LR": "农产品",
    "JR": "农产品", "RS": "农产品", "AP": "农产品", "CJ": "农产品",
    "CY": "农产品", "PK": "农产品",
    # 能化
    "L": "能化", "PP": "能化", "V": "能化", "TA": "能化", "MA": "能化",
    "EG": "能化", "SA": "能化", "FG": "能化",
    "FU": "能化", "BU": "能化", "RU": "能化", "NR": "能化", "BR": "能化",
    "LU": "能化", "SC": "能化", "EC": "能化", "EB": "能化", "PG": "能化",
    "BZ": "能化", "PF": "能化", "PX": "能化", "PR": "能化", "PL": "能化",
    "SH": "能化", "UR": "能化", "LG": "能化",
    # 新能源 / 电池材料
    "SI": "新能源", "LC": "新能源", "PS": "新能源",
    # 金融期货 (cffex)
    "IF": "金融期货", "IH": "金融期货", "IC": "金融期货", "IM": "金融期货",
    "TF": "金融期货", "TS": "金融期货", "T": "金融期货", "TL": "金融期货",
}


# ---------------------------------------------------------------------------
# Commodity prefix -> display name (Chinese, exchange-prefixed).
# Used to build the ``name`` field on FuturesPrice, e.g. "沪铜2608".
# For CZCE symbols the ``郑`` prefix is added; for SHFE/DCE/INE the standard
# ``沪``/``大商`` prefix; for CFFEX no prefix.
# ---------------------------------------------------------------------------
COMMODITY_DISPLAY_NAME: Final[dict[str, str]] = {
    # SHFE / INE
    "CU": "沪铜", "AL": "沪铝", "ZN": "沪锌", "NI": "沪镍", "PB": "沪铅",
    "SN": "沪锡", "AU": "黄金", "AG": "白银", "RB": "螺纹钢", "HC": "热卷",
    "WR": "线材", "SS": "不锈钢", "FU": "燃油", "BU": "沥青", "RU": "橡胶",
    "NR": "20号胶", "BR": "丁二烯橡胶", "SP": "纸浆", "AO": "氧化铝",
    "AD": "铸造铝合金", "BC": "国际铜", "SC": "原油", "LU": "低硫燃油",
    "EC": "集运指数",
    # DCE
    "I": "铁矿石", "J": "焦炭", "JM": "焦煤", "A": "豆一", "B": "豆二",
    "M": "豆粕", "Y": "豆油", "P": "棕榈油", "C": "玉米", "CS": "淀粉",
    "JD": "鸡蛋", "LH": "生猪", "L": "塑料", "PP": "聚丙烯", "V": "PVC",
    "EG": "乙二醇", "EB": "苯乙烯", "PG": "液化石油气", "FB": "纤维板",
    "BB": "胶合板", "RR": "粳米", "BZ": "纯苯", "LG": "原木",
    # CZCE (郑商所 names are the commodity itself, no exchange prefix)
    "SR": "白糖", "CF": "棉花", "OI": "菜油", "RM": "菜粕", "RS": "菜籽",
    "PM": "普麦", "WH": "强麦", "RI": "早籼稻", "LR": "晚籼稻", "JR": "粳稻",
    "MA": "甲醇", "TA": "PTA", "FG": "玻璃", "SA": "纯碱", "UR": "尿素",
    "SF": "硅铁", "SM": "锰硅", "CY": "棉纱", "AP": "苹果", "CJ": "红枣",
    "PK": "花生", "PF": "短纤", "PX": "对二甲苯", "PR": "瓶片", "PL": "丙烯",
    "SH": "烧碱",
    # CFFEX
    "IF": "沪深300", "IH": "上证50", "IC": "中证500", "IM": "中证1000",
    "TF": "5年期国债", "T": "10年期国债", "TS": "2年期国债", "TL": "30年期国债",
    # GFEX
    "SI": "工业硅", "LC": "碳酸锂", "PS": "多晶硅", "PT": "铂", "PD": "钯",
}


# ---------------------------------------------------------------------------
# Commodity prefix -> exchange code (matches the storage column)
# ---------------------------------------------------------------------------
COMMODITY_TO_EXCHANGE: Final[dict[str, str]] = {
    # SHFE
    "CU": "SHFE", "AL": "SHFE", "ZN": "SHFE", "NI": "SHFE", "PB": "SHFE",
    "SN": "SHFE", "AU": "SHFE", "AG": "SHFE", "RB": "SHFE", "HC": "SHFE",
    "WR": "SHFE", "SS": "SHFE", "FU": "SHFE", "BU": "SHFE", "RU": "SHFE",
    "NR": "SHFE", "BR": "SHFE", "SP": "SHFE", "AO": "SHFE", "AD": "SHFE",
    # INE (上海国际能源交易中心)
    "SC": "INE", "BC": "INE", "LU": "INE", "EC": "INE", "NR": "INE",
    # DCE
    "I": "DCE", "J": "DCE", "JM": "DCE", "A": "DCE", "B": "DCE",
    "M": "DCE", "Y": "DCE", "P": "DCE", "C": "DCE", "CS": "DCE",
    "JD": "DCE", "LH": "DCE", "L": "DCE", "PP": "DCE", "V": "DCE",
    "EG": "DCE", "EB": "DCE", "PG": "DCE", "FB": "DCE", "BB": "DCE",
    "RR": "DCE", "BZ": "DCE", "LG": "DCE",
    # CZCE
    "SR": "CZCE", "CF": "CZCE", "OI": "CZCE", "RM": "CZCE", "RS": "CZCE",
    "PM": "CZCE", "WH": "CZCE", "RI": "CZCE", "LR": "CZCE", "JR": "CZCE",
    "MA": "CZCE", "TA": "CZCE", "FG": "CZCE", "SA": "CZCE", "UR": "CZCE",
    "SF": "CZCE", "SM": "CZCE", "CY": "CZCE", "AP": "CZCE", "CJ": "CZCE",
    "PK": "CZCE", "PF": "CZCE", "PX": "CZCE", "PR": "CZCE", "PL": "CZCE",
    "SH": "CZCE",
    # CFFEX
    "IF": "CFFEX", "IH": "CFFEX", "IC": "CFFEX", "IM": "CFFEX",
    "TF": "CFFEX", "T": "CFFEX", "TS": "CFFEX", "TL": "CFFEX",
    # GFEX
    "SI": "GFEX", "LC": "GFEX", "PS": "GFEX", "PT": "GFEX", "PD": "GFEX",
}


def _strip_trailing_digits(s: str) -> str:
    """Extract commodity prefix from a contract code (e.g. ``CU2608`` -> ``CU``).

    We strip from the *last* digit cluster. This handles both
    letter-only-with-numeric-tail (``RB2610``) and any letter-tail variants.
    """
    i = 0
    while i < len(s) and s[i].isalpha():
        i += 1
    return s[:i].upper()


def symbol_to_industry(symbol: str) -> str:
    """Return the industry category for a futures contract code.

    >>> symbol_to_industry("CU2608")
    '有色金属'
    >>> symbol_to_industry("M2509")
    '农产品'
    >>> symbol_to_industry("XYZ9999")
    '其他'
    """
    if not symbol:
        return "其他"
    prefix = _strip_trailing_digits(symbol.strip())
    return COMMODITY_TO_INDUSTRY.get(prefix, "其他")


def symbol_to_exchange(symbol: str) -> str:
    """Return the exchange code (SHFE/DCE/CZCE/CFFEX/INE/GFEX) for a contract."""
    if not symbol:
        return ""
    prefix = _strip_trailing_digits(symbol.strip())
    return COMMODITY_TO_EXCHANGE.get(prefix, "")


def symbol_to_display_name(symbol: str) -> str:
    """Return the Chinese display name for a contract code (e.g. ``CU2608`` -> ``沪铜``)."""
    if not symbol:
        return ""
    prefix = _strip_trailing_digits(symbol.strip())
    return COMMODITY_DISPLAY_NAME.get(prefix, prefix)


def build_contract_name(symbol: str) -> str:
    """Build a human-readable ``name`` for the FuturesPrice row.

    Examples:
      ``CU2608`` -> ``沪铜2608``
      ``M2509``  -> ``豆粕2509``
      ``RB2610`` -> ``螺纹钢2610``
    """
    base = symbol_to_display_name(symbol)
    if not base:
        return symbol.upper()
    digits = ""
    for ch in symbol:
        if ch.isdigit():
            digits += ch
    return f"{base}{digits}" if digits else base