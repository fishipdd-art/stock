"""
Synthetic data seeder for the stock_quote package.

The hotness engine needs 21 categories, ~70 search terms, 148 A-shares,
plus some signals and stock_quotes to produce meaningful rankings. When
the project is freshly cloned and the knowledge-graph JSON files are
not yet loaded, calling `seed_demo()` will populate the DB with a
realistic synthetic dataset that exercises every code path of the
hotness formula.

This is *deliberately* idempotent: every row is upserted, so calling
`seed_demo()` twice gives the same final state. It is also safe to call
alongside real data — the synthetic rows are keyed on well-known
prefixes (categories named like '示例-...', stocks 000001-000148, etc.)
that you can recognise and drop when you want to swap in real data.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from config.settings import settings
from storage.database import get_db
from storage.models import (
    AStock,
    KnowledgeCategory,
    KnowledgeSignal,
    NewsRaw,
    SearchTerm,
    SectorHeat,
    SignalStock,
    StockQuote,
)


# 21 supply-chain categories. Names are conventional so the daily report
# can render them without translation.
CATEGORIES: list[dict] = [
    {"name": "原油及石油化工", "signal_type": "supply_tight", "n_terms": 4},
    {"name": "天然气与LNG", "signal_type": "supply_tight", "n_terms": 3},
    {"name": "煤炭与煤化工", "signal_type": "supply_tight", "n_terms": 3},
    {"name": "电力与新能源", "signal_type": "policy", "n_terms": 4},
    {"name": "有色金属-铜", "signal_type": "supply_tight", "n_terms": 3},
    {"name": "有色金属-铝", "signal_type": "supply_tight", "n_terms": 3},
    {"name": "稀有金属与小金属", "signal_type": "supply_tight", "n_terms": 3},
    {"name": "钢铁与铁矿业", "signal_type": "supply_tight", "n_terms": 3},
    {"name": "化工与化纤", "signal_type": "supply_tight", "n_terms": 3},
    {"name": "半导体与芯片", "signal_type": "policy", "n_terms": 4},
    {"name": "消费电子与AI硬件", "signal_type": "policy", "n_terms": 4},
    {"name": "新能源车与锂电池", "signal_type": "policy", "n_terms": 4},
    {"name": "光伏与储能", "signal_type": "policy", "n_terms": 4},
    {"name": "医药与CXO", "signal_type": "policy", "n_terms": 3},
    {"name": "食品饮料与白酒", "signal_type": "demand", "n_terms": 3},
    {"name": "房地产与建材", "signal_type": "policy", "n_terms": 3},
    {"name": "基建与工程机械", "signal_type": "policy", "n_terms": 3},
    {"name": "汽车与零部件", "signal_type": "demand", "n_terms": 3},
    {"name": "军工与航天", "signal_type": "policy", "n_terms": 3},
    {"name": "航运港口与造船", "signal_type": "supply_tight", "n_terms": 3},
    {"name": "金融与券商", "signal_type": "policy", "n_terms": 3},
]

# A handful of search terms per category, hand-picked for plausible
# fuzzy matches against the synthetic signal titles below.
TERMS: list[tuple[str, str, list[str]]] = [
    # (category, term, [a-share codes])
    ("原油及石油化工", "原油价格", ["600028", "601857", "600938", "000301"]),
    ("原油及石油化工", "国际油价", ["600028", "601857"]),
    ("原油及石油化工", "石油化工", ["600028", "601857", "000301"]),
    ("原油及石油化工", "炼化", ["600028", "601857"]),
    ("天然气与LNG", "天然气价格", ["600583", "601808"]),
    ("天然气与LNG", "LNG", ["600583", "601808"]),
    ("天然气与LNG", "燃气供应", ["600583"]),
    ("煤炭与煤化工", "煤价上涨", ["601225", "601088", "601898"]),
    ("煤炭与煤化工", "动力煤", ["601225", "601088"]),
    ("煤炭与煤化工", "煤化工", ["601898"]),
    ("电力与新能源", "电力改革", ["600886", "601985", "600905"]),
    ("电力与新能源", "新能源发电", ["600905", "002129"]),
    ("电力与新能源", "风电光伏", ["002129", "600905"]),
    ("电力与新能源", "核电", ["601985"]),
    ("有色金属-铜", "铜价上涨", ["601899", "600362", "000630"]),
    ("有色金属-铜", "电解铜", ["601899", "000630"]),
    ("有色金属-铜", "铜矿", ["601899"]),
    ("有色金属-铝", "铝价上涨", ["601600", "000807", "002532"]),
    ("有色金属-铝", "电解铝", ["601600", "000807"]),
    ("有色金属-铝", "铝土矿", ["601600"]),
    ("稀有金属与小金属", "稀土", ["000831", "600111", "002460"]),
    ("稀有金属与小金属", "钨钼", ["000831", "600111"]),
    ("稀有金属与小金属", "锂矿", ["002460", "002466"]),
    ("钢铁与铁矿业", "钢铁去产能", ["600019", "000708", "000932"]),
    ("钢铁与铁矿业", "铁矿石", ["600019", "000708"]),
    ("钢铁与铁矿业", "粗钢产量", ["600019"]),
    ("化工与化纤", "化工景气", ["600309", "000301", "600346"]),
    ("化工与化纤", "化纤涨价", ["600346", "000301"]),
    ("化工与化纤", "PTA", ["600346"]),
    ("半导体与芯片", "半导体国产化", ["002371", "603501", "688981", "688012"]),
    ("半导体与芯片", "芯片缺货", ["002371", "603501", "688981"]),
    ("半导体与芯片", "光刻机", ["688981"]),
    ("半导体与芯片", "存储芯片", ["603501", "688012"]),
    ("消费电子与AI硬件", "AI算力", ["002241", "002475", "300433", "002230"]),
    ("消费电子与AI硬件", "消费电子", ["002475", "300433"]),
    ("消费电子与AI硬件", "苹果产业链", ["002475", "002241"]),
    ("消费电子与AI硬件", "PCB", ["002475"]),
    ("新能源车与锂电池", "新能源车销量", ["300750", "002594", "300014", "002460"]),
    ("新能源车与锂电池", "锂电池", ["300750", "002460", "300014"]),
    ("新能源车与锂电池", "动力电池", ["300750", "300014"]),
    ("新能源车与锂电池", "隔膜", ["002812"]),
    ("光伏与储能", "光伏装机", ["601012", "002459", "688223", "300274"]),
    ("光伏与储能", "硅料价格", ["601012", "002459"]),
    ("光伏与储能", "储能政策", ["300274", "688223"]),
    ("光伏与储能", "逆变器", ["300274"]),
    ("医药与CXO", "创新药", ["600276", "000538", "603259"]),
    ("医药与CXO", "CXO", ["603259", "300347"]),
    ("医药与CXO", "医疗器械", ["300760"]),
    ("食品饮料与白酒", "白酒提价", ["600519", "000858", "000568"]),
    ("食品饮料与白酒", "高端白酒", ["600519", "000858"]),
    ("食品饮料与白酒", "乳制品", ["600887"]),
    ("房地产与建材", "房地产政策", ["000002", "001979", "600585", "000877"]),
    ("房地产与建材", "水泥价格", ["600585", "000877"]),
    ("房地产与建材", "玻璃", ["601636"]),
    ("基建与工程机械", "基建投资", ["600031", "000425", "000528"]),
    ("基建与工程机械", "挖掘机销量", ["000528", "000425"]),
    ("基建与工程机械", "工程机械", ["000528"]),
    ("汽车与零部件", "汽车销量", ["600104", "000625", "601238", "002048"]),
    ("汽车与零部件", "新能源车", ["002594", "600104"]),
    ("汽车与零部件", "汽车零部件", ["002048", "601238"]),
    ("军工与航天", "军工订单", ["600760", "000768", "002025"]),
    ("军工与航天", "航空航天", ["600760", "002025"]),
    ("军工与航天", "导弹", ["000768"]),
    ("航运港口与造船", "BDI指数", ["601872", "601018", "600018", "601808"]),
    ("航运港口与造船", "造船周期", ["600150", "601808"]),
    ("航运港口与造船", "港口", ["601018"]),
    ("金融与券商", "券商合并", ["600030", "000776", "601066", "601995"]),
    ("金融与券商", "资本市场改革", ["600030", "601995"]),
    ("金融与券商", "保险", ["601318", "601628"]),
]


# 148 stocks. The first 70 codes are pulled from the term/category lists
# so each category already has 1+ stocks; the remaining 78 are random
# codes to round out the universe.
def _stock_seed() -> list[dict]:
    rng = random.Random(20250101)  # deterministic
    used: set[str] = set()
    rows: list[dict] = []
    for _, _, codes in TERMS:
        for c in codes:
            if c in used:
                continue
            used.add(c)
            rows.append(
                {
                    "code": c,
                    "name": f"示例股-{c}",
                    "sector_tags": "示例",
                    "supply_exposure": rng.choice(["低", "中", "高"]),
                    "tier": rng.choice([1, 2, 3]),
                }
            )
    base = int(used and max(int(c) for c in used) or 0)
    while len(rows) < 148:
        code = f"{base + len(rows):06d}"
        if code in used:
            continue
        used.add(code)
        rows.append(
            {
                "code": code,
                "name": f"示例股-{code}",
                "sector_tags": "示例",
                "supply_exposure": rng.choice(["低", "中", "高"]),
                "tier": rng.choice([1, 2, 3]),
            }
        )
    return rows


# Synthetic signals: 1-2 per category, with the right term mentioned in
# the title so the fuzzy matcher triggers.
def _signal_seed() -> list[dict]:
    rows: list[dict] = []
    rng = random.Random(42)
    for cat_name, term, codes in TERMS:
        rows.append(
            {
                "signal_key": f"demo-{cat_name}-{term}",
                "title": f"{term}近期表现引发{cat_name}关注",
                "description": f"示例描述：{term}出现显著变化，可能影响{codes}。",
                "grade": rng.choice(["A", "B", "C"]),
                "direction": "supply_tight",
                "strength": round(rng.uniform(0.3, 0.9), 3),
                "signal_date": date.today().isoformat(),
            }
        )
    return rows


def seed_demo(
    trade_date: date | None = None,
    with_quotes: bool = True,
    with_news: bool = True,
) -> dict:
    """Populate the DB with a synthetic, self-consistent demo dataset.

    Idempotent: every row uses upsert semantics. Safe to call repeatedly.
    """
    db = get_db()
    db.init_schema()
    td = trade_date or date.today()
    counts = {"categories": 0, "terms": 0, "stocks": 0, "signals": 0, "quotes": 0, "news": 0}

    # 1. categories
    with db.tx() as s:
        stmt = sqlite_insert(KnowledgeCategory).values(CATEGORIES)
        cols = {c.name: stmt.excluded[c.name] for c in KnowledgeCategory.__table__.columns if c.name != "id"}
        s.execute(stmt.on_conflict_do_update(index_elements=["name"], set_=cols))
        counts["categories"] = len(CATEGORIES)

    # 2. terms (need category_id mapping)
    with db.session() as s:
        cat_rows = s.execute(select(KnowledgeCategory)).scalars().all()
    cat_id_by_name = {c.name: c.id for c in cat_rows}
    term_rows: list[dict] = []
    for cat_name, term, codes in TERMS:
        term_rows.append(
            {
                "term": term,
                "category_id": cat_id_by_name[cat_name],
                "priority": "中",
                "transmission_logic": f"示例：{term} -> {cat_name} 受益",
                "a_share_map": ",".join(codes),
                "a_share_codes": ",".join(codes),
                "enabled": True,
            }
        )
    with db.tx() as s:
        stmt = sqlite_insert(SearchTerm).values(term_rows)
        cols = {c.name: stmt.excluded[c.name] for c in SearchTerm.__table__.columns if c.name not in {"id", "created_at"}}
        s.execute(stmt.on_conflict_do_update(index_elements=["term", "category_id"], set_=cols))
        counts["terms"] = len(term_rows)

    # 3. stocks
    stock_rows = _stock_seed()
    with db.tx() as s:
        stmt = sqlite_insert(AStock).values(stock_rows)
        cols = {c.name: stmt.excluded[c.name] for c in AStock.__table__.columns if c.name not in {"code", "last_seen_at"}}
        # last_seen_at should refresh each call
        cols["last_seen_at"] = datetime.utcnow()
        s.execute(stmt.on_conflict_do_update(index_elements=["code"], set_=cols))
        counts["stocks"] = len(stock_rows)

    # 4. signals (need signal_id mapping for SignalStock)
    signal_rows = _signal_seed()
    with db.tx() as s:
        stmt = sqlite_insert(KnowledgeSignal).values(signal_rows)
        cols = {c.name: stmt.excluded[c.name] for c in KnowledgeSignal.__table__.columns if c.name not in {"id", "created_at", "updated_at"}}
        s.execute(stmt.on_conflict_do_update(index_elements=["signal_key"], set_=cols))
    with db.session() as s:
        sig_rows = s.execute(select(KnowledgeSignal)).scalars().all()
    sig_id_by_key = {sig.signal_key: sig.id for sig in sig_rows}
    counts["signals"] = len(signal_rows)

    # 5. signal_stocks (link each signal to the codes in its term)
    link_rows: list[dict] = []
    for cat_name, term, codes in TERMS:
        sig_id = sig_id_by_key.get(f"demo-{cat_name}-{term}")
        if sig_id is None:
            continue
        for c in codes:
            link_rows.append({"signal_id": sig_id, "stock_code": c, "strength": 0.5})
    if link_rows:
        with db.tx() as s:
            stmt = sqlite_insert(SignalStock).values(link_rows)
            cols = {c.name: stmt.excluded[c.name] for c in SignalStock.__table__.columns if c.name not in {"id"}}
            s.execute(stmt.on_conflict_do_update(index_elements=["signal_id", "stock_code"], set_=cols))

    # 6. stock_quotes (synthetic, with a per-category hotness signal)
    if with_quotes:
        rng = random.Random(td.toordinal())
        quote_rows: list[dict] = []
        for cat_name, term, codes in TERMS:
            for c in codes:
                base_change = rng.uniform(-3.0, 6.0)
                if "新能源" in cat_name or "半导体" in cat_name or "光伏" in cat_name or "AI" in cat_name:
                    base_change += rng.uniform(1.0, 4.0)  # pump hot categories
                close = round(rng.uniform(8.0, 120.0), 2)
                change_pct = round(base_change, 2)
                turnover = round(rng.uniform(5e7, 8e9), 2)
                open_ = round(close * (1 - change_pct / 100 * 0.4), 2)
                high_ = round(max(close, open_) * (1 + rng.uniform(0, 0.02)), 2)
                low_ = round(min(close, open_) * (1 - rng.uniform(0, 0.02)), 2)
                quote_rows.append(
                    {
                        "trade_date": td,
                        "code": c,
                        "name": f"示例股-{c}",
                        "open": open_,
                        "close": close,
                        "high": high_,
                        "low": low_,
                        "volume": round(turnover / max(close, 1.0), 0),
                        "turnover": turnover,
                        "change_pct": change_pct,
                        "change_amt": round(close - open_, 2),
                    }
                )
        with db.tx() as s:
            stmt = sqlite_insert(StockQuote).values(quote_rows)
            cols = {c.name: stmt.excluded[c.name] for c in StockQuote.__table__.columns if c.name not in {"id", "fetched_at"}}
            s.execute(stmt.on_conflict_do_update(index_elements=["trade_date", "code"], set_=cols))
            counts["quotes"] = len(quote_rows)

    # 7. news (synthetic, a few per category)
    if with_news:
        rng = random.Random(td.toordinal() + 1)
        news_rows: list[dict] = []
        for cat_name, term, _ in TERMS[:10]:  # first 10 categories get news
            for i in range(rng.randint(1, 3)):
                title = f"{term}快讯：{cat_name}相关品种变动 (示例新闻 {i+1})"
                news_rows.append(
                    {
                        "url": f"https://example.com/news/{td.isoformat()}/{cat_name}/{term}/{i}",
                        "title": title,
                        "summary": f"示例：{term}对{category_count_for(cat_name)}只股票有影响。",
                        "source": "demo",
                        "source_label": "示例源",
                        "published_at": datetime.combine(td, datetime.min.time()) + timedelta(hours=9 + i),
                        "fetched_at": datetime.utcnow(),
                        "content": title,
                        "keywords_matched": term,
                    }
                )
        if news_rows:
            with db.tx() as s:
                stmt = sqlite_insert(NewsRaw).values(news_rows)
                cols = {c.name: stmt.excluded[c.name] for c in NewsRaw.__table__.columns if c.name not in {"id", "fetched_at"}}
                s.execute(stmt.on_conflict_do_update(index_elements=["url"], set_=cols))
                counts["news"] = len(news_rows)

    logger.info(f"seed_demo: {counts}")
    return counts


def category_count_for(cat_name: str) -> int:
    return sum(1 for c, _, _ in TERMS if c == cat_name)
