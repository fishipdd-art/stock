"""Confirmed portfolio storage and Dify-facing summary."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from storage import get_db
from storage.models import PortfolioAccount, PortfolioPosition


DEFAULT_USER_ID = "default"


CONFIRMED_2026_07_12 = [
    {"code": "000878", "name": "云南铜业", "asset_type": "stock", "quantity": 2000, "current_price": 15.240, "cost_price": 17.6835, "market_value": 30480.00, "pnl_amount": -4886.91, "pnl_pct": -0.1382, "risk_bucket": "有色金属"},
    {"code": "002149", "name": "西部材料", "asset_type": "stock", "quantity": 1000, "current_price": 45.200, "cost_price": 54.2806, "market_value": 45200.00, "pnl_amount": -9080.64, "pnl_pct": -0.1673, "risk_bucket": "军工材料"},
    {"code": "002859", "name": "洁美科技", "asset_type": "stock", "quantity": 1100, "current_price": 78.400, "cost_price": 91.9309, "market_value": 86240.00, "pnl_amount": -14883.99, "pnl_pct": -0.1472, "risk_bucket": "电子元件"},
    {"code": "159206", "name": "卫星ETF永赢", "asset_type": "etf", "quantity": 34400, "current_price": 1.795, "cost_price": 1.6384, "market_value": 61748.00, "pnl_amount": 5388.39, "pnl_pct": 0.0956, "risk_bucket": "军工航天"},
    {"code": "300004", "name": "南风股份", "asset_type": "stock", "quantity": 4000, "current_price": 9.350, "cost_price": 11.8032, "market_value": 37400.00, "pnl_amount": -9812.74, "pnl_pct": -0.2078, "risk_bucket": "军工装备"},
    {"code": "300408", "name": "三环集团", "asset_type": "stock", "quantity": 400, "current_price": 127.050, "cost_price": 145.1184, "market_value": 50820.00, "pnl_amount": -7227.35, "pnl_pct": -0.1245, "risk_bucket": "电子元件"},
    {"code": "300502", "name": "新易盛", "asset_type": "stock", "quantity": 100, "current_price": 523.050, "cost_price": 509.0995, "market_value": 52305.00, "pnl_amount": 1395.05, "pnl_pct": 0.0274, "risk_bucket": "AI算力"},
    {"code": "301308", "name": "江波龙", "asset_type": "stock", "quantity": 100, "current_price": 587.600, "cost_price": 600.1172, "market_value": 58760.00, "pnl_amount": -1251.72, "pnl_pct": -0.0209, "risk_bucket": "存储芯片"},
    {"code": "563380", "name": "航空航天ETF", "asset_type": "etf", "quantity": 40000, "current_price": 1.052, "cost_price": 1.0892, "market_value": 42080.00, "pnl_amount": -1488.08, "pnl_pct": -0.0342, "risk_bucket": "军工航天"},
]


def upsert_confirmed_portfolio(
    positions: list[dict[str, Any]] | None = None,
    *,
    user_id: str = DEFAULT_USER_ID,
    total_assets: float = 500_000.0,
    max_drawdown_tolerance: float = 0.20,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    positions = positions or CONFIRMED_2026_07_12
    as_of = as_of or datetime(2026, 7, 12, 14, 49)
    db = get_db()
    with db.tx() as s:
        account = s.get(PortfolioAccount, user_id)
        if account is None:
            account = PortfolioAccount(user_id=user_id)
            s.add(account)
        account.total_assets = total_assets
        account.max_drawdown_tolerance = max_drawdown_tolerance
        account.external_assets_included = False
        account.as_of = as_of

        known_codes = {str(p["code"]) for p in positions}
        s.query(PortfolioPosition).filter(
            PortfolioPosition.user_id == user_id,
            ~PortfolioPosition.code.in_(known_codes),
        ).delete(synchronize_session=False)
        for item in positions:
            code = str(item["code"])
            row = s.query(PortfolioPosition).filter_by(user_id=user_id, code=code).one_or_none()
            if row is None:
                row = PortfolioPosition(user_id=user_id, code=code)
                s.add(row)
            for field in (
                "name", "asset_type", "quantity", "current_price", "cost_price",
                "market_value", "pnl_amount", "pnl_pct", "risk_bucket",
            ):
                setattr(row, field, item[field])
            row.available_quantity = float(item.get("available_quantity", item["quantity"]))
            row.source = "user_confirmed"
            row.as_of = as_of
    return get_portfolio(user_id)


def get_portfolio(user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
    db = get_db()
    with db.session() as s:
        account = s.get(PortfolioAccount, user_id)
        if account is None:
            raise KeyError(user_id)
        rows = s.query(PortfolioPosition).filter_by(user_id=user_id).order_by(
            PortfolioPosition.market_value.desc()
        ).all()

        invested = sum(row.market_value for row in rows)
        total_pnl = sum(row.pnl_amount for row in rows)
        cash = max(0.0, account.total_assets - invested)
        buckets: dict[str, float] = defaultdict(float)
        positions = []
        for row in rows:
            weight = row.market_value / account.total_assets if account.total_assets else 0.0
            buckets[row.risk_bucket or "其他"] += row.market_value
            positions.append({
                "code": row.code,
                "name": row.name,
                "asset_type": row.asset_type,
                "quantity": row.quantity,
                "available_quantity": row.available_quantity,
                "current_price": row.current_price,
                "cost_price": row.cost_price,
                "market_value": row.market_value,
                "pnl_amount": row.pnl_amount,
                "pnl_pct": row.pnl_pct,
                "weight": round(weight, 6),
                "risk_bucket": row.risk_bucket,
                "as_of": row.as_of.isoformat() if row.as_of else None,
            })
        return {
            "user_id": user_id,
            "total_assets": account.total_assets,
            "invested_market_value": round(invested, 2),
            "cash": round(cash, 2),
            "invested_pct": round(invested / account.total_assets, 6) if account.total_assets else 0.0,
            "total_position_pnl": round(total_pnl, 2),
            "max_drawdown_tolerance": account.max_drawdown_tolerance,
            "external_assets_included": account.external_assets_included,
            "as_of": account.as_of.isoformat() if account.as_of else None,
            "positions": positions,
            "risk_buckets": [
                {
                    "name": name,
                    "market_value": round(value, 2),
                    "weight": round(value / account.total_assets, 6) if account.total_assets else 0.0,
                }
                for name, value in sorted(buckets.items(), key=lambda x: x[1], reverse=True)
            ],
            "rules": {"hard_position_caps": False, "risk_warnings_only": True},
        }
