from __future__ import annotations


def test_confirmed_portfolio_summary(in_memory_db):
    from accounts.portfolio import upsert_confirmed_portfolio

    result = upsert_confirmed_portfolio()
    assert result["total_assets"] == 500_000.0
    assert result["invested_market_value"] == 465_033.0
    assert result["cash"] == 34_967.0
    assert len(result["positions"]) == 9
    assert {p["code"] for p in result["positions"]} == {
        "000878", "002149", "002859", "159206", "300004",
        "300408", "300502", "301308", "563380",
    }
    assert result["rules"]["hard_position_caps"] is False
    aerospace = next(x for x in result["risk_buckets"] if x["name"] == "军工航天")
    assert aerospace["market_value"] == 103_828.0
