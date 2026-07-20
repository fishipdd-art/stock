from __future__ import annotations


def test_fetch_today_uses_history_fallback_when_spot_is_unavailable(in_memory_db, monkeypatch):
    from collector.stock_quote import StockQuoteCollector
    from collector.stock_quote import collector as collector_module
    from storage.models import AStock

    with in_memory_db.tx() as s:
        s.add(AStock(code="000001", name="测试股"))

    collector = StockQuoteCollector(db=in_memory_db)
    monkeypatch.setattr(
        collector_module.bridge,
        "fetch_all_spot_quotes",
        lambda: (_ for _ in ()).throw(ConnectionError("spot blocked")),
    )
    monkeypatch.setattr(
        collector_module.bridge,
        "fetch_sina_spot_quotes",
        lambda codes: collector_module.bridge.pd.DataFrame(),
    )
    calls = []

    def fake_history(**kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(collector, "fetch_history", fake_history)
    assert collector.fetch_today() == 1
    assert calls and calls[0]["days_back"] == 7


def test_quote_universe_includes_portfolio_etfs(in_memory_db):
    from collector.stock_quote import StockQuoteCollector
    from storage.models import PortfolioPosition

    with in_memory_db.tx() as s:
        s.add(PortfolioPosition(
            user_id="default",
            code="159206",
            name="卫星ETF",
            asset_type="etf",
            quantity=100,
            current_price=1.0,
            cost_price=1.0,
            market_value=100,
            pnl_amount=0,
            pnl_pct=0,
        ))
    assert "159206" in StockQuoteCollector(in_memory_db).get_universe()
