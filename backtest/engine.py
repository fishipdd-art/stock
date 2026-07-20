"""
Event-driven backtest engine.

Simulates trading based on event signals:
  - For each past event, on T-1 (event date), "buy" related stocks
    if predicted change is positive, "short" if negative
  - Hold for N days (configurable)
  - Track P&L, win rate, Sharpe ratio
  - Compare to buy-and-hold baseline

Two modes:
  - 'signal'   : pure event-driven, only trades on events
  - 'baseline' : buy-and-hold, ignore events (control)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean, stdev
from typing import Optional

from loguru import logger
from sqlalchemy import and_, desc

from storage import get_db
from storage.models import IndustryEvent, StockQuote, FuturesPrice


# Config
DEFAULT_HOLD_DAYS = 5
DEFAULT_POSITION_SIZE = 0.1  # 10% of capital per trade
DEFAULT_CAPITAL = 1_000_000  # 1M initial


@dataclass
class Trade:
    """A single simulated trade."""
    event_id: int
    event_title: str
    code: str
    side: str  # 'long' / 'short'
    entry_date: date
    entry_price: float
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    pnl_pct: Optional[float] = None
    pnl_amount: Optional[float] = None
    reason: str = ""


@dataclass
class BacktestResult:
    """Aggregate results of a backtest run."""
    start_date: date
    end_date: date
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    win_rate: float = 0.0
    total_pnl_pct: float = 0.0
    total_pnl_amount: float = 0.0
    avg_pnl_per_trade: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    baseline_pnl: float = 0.0
    excess_return: float = 0.0
    trades: list[Trade] = field(default_factory=list)


def _infer_direction(event: IndustryEvent) -> str:
    """Infer trade direction from event_type and impact."""
    et = event.event_type or ""
    # Event types that typically mean bullish for related stocks
    bullish_types = {"launch", "product_launch", "earnings", "m&a", "contract", "capacity"}
    # Event types that typically mean bearish
    bearish_types = {"regulatory"}  # e.g., export curbs

    title_lower = (event.title or "").lower()
    # Check title for negative keywords
    negative_kw = [
        "跌价", "降价", "下调", "禁令", "制裁", "过剩", "滞销", "减产",
        "下滑", "下降", "亏损", "衰退", "萧条", "危机", "退市", "停产",
        "关停", "诉讼", "违规", "处罚", "调查", "终止", "失败", "事故",
        "跳水", "暴跌", "闪崩", "跌停", "裁员", "降薪", "利空",
    ]
    if any(kw in event.title for kw in negative_kw):
        return "short"

    if et in bearish_types:
        return "short"
    if et in bullish_types:
        return "long"
    # Default: long (events generally have positive bias in research data)
    return "long"


def _get_quote_on_date(session, code: str, on_date: date) -> Optional[float]:
    """Get stock close price on or near a date."""
    # Try exact date first
    q = (
        session.query(StockQuote)
        .filter(StockQuote.code == code, StockQuote.trade_date == on_date)
        .first()
    )
    if q:
        return q.close

    # Try nearby dates
    for delta in [1, -1, 2, -2, 3, -3]:
        q = (
            session.query(StockQuote)
            .filter(StockQuote.code == code, StockQuote.trade_date == on_date + timedelta(days=delta))
            .first()
        )
        if q:
            return q.close
    return None


def run_backtest(
    days_back: int = 180,
    hold_days: int = DEFAULT_HOLD_DAYS,
    position_size: float = DEFAULT_POSITION_SIZE,
    capital: float = DEFAULT_CAPITAL,
    min_impact: int = 3,
    min_event_change: float = 0.3,
) -> BacktestResult:
    """Run event-driven backtest on past N days of events."""
    db = get_db()
    today = date.today()
    start = today - timedelta(days=days_back)

    result = BacktestResult(start_date=start, end_date=today)

    with db.session() as s:
        events = (
            s.query(IndustryEvent)
            .filter(
                IndustryEvent.is_future == False,
                IndustryEvent.event_date >= start,
                IndustryEvent.event_date <= today,
                IndustryEvent.impact_level >= min_impact,
            )
            .order_by(IndustryEvent.event_date.asc())
            .all()
        )

        logger.info(f"Backtest: {len(events)} events in last {days_back} days")

        for ev in events:
            codes = [c.strip() for c in (ev.related_stocks or "").split(",") if c.strip()]
            if not codes:
                continue

            direction = _infer_direction(ev)

            # Skip events whose inferred impact magnitude is below the floor
            # (i.e. the event is too weak to justify a trade).
            impact_magnitude = abs(ev.impact_level or 0) / 5.0
            if impact_magnitude < min_event_change:
                continue

            for code in codes[:3]:  # max 3 stocks per event
                entry_price = _get_quote_on_date(s, code, ev.event_date)
                if not entry_price or entry_price <= 0:
                    continue

                exit_date = ev.event_date + timedelta(days=hold_days)
                exit_price = _get_quote_on_date(s, code, exit_date)
                if not exit_price:
                    continue

                if direction == "long":
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                else:  # short
                    pnl_pct = (entry_price - exit_price) / entry_price * 100

                pnl_amount = pnl_pct / 100 * position_size * capital

                trade = Trade(
                    event_id=ev.id,
                    event_title=ev.title,
                    code=code,
                    side=direction,
                    entry_date=ev.event_date,
                    entry_price=entry_price,
                    exit_date=exit_date,
                    exit_price=exit_price,
                    pnl_pct=pnl_pct,
                    pnl_amount=pnl_amount,
                    reason=f"{ev.event_type}|{ev.direction}|⭐{ev.impact_level}",
                )
                result.trades.append(trade)

    # Aggregate
    result.n_trades = len(result.trades)
    if result.n_trades == 0:
        return result

    pnl_pcts = [t.pnl_pct for t in result.trades if t.pnl_pct is not None]
    result.n_wins = sum(1 for p in pnl_pcts if p > 0)
    result.n_losses = sum(1 for p in pnl_pcts if p < 0)
    result.win_rate = result.n_wins / result.n_trades * 100
    result.total_pnl_pct = sum(pnl_pcts)
    result.total_pnl_amount = sum(t.pnl_amount for t in result.trades if t.pnl_amount)
    result.avg_pnl_per_trade = mean(pnl_pcts)

    if len(pnl_pcts) > 1:
        sd = stdev(pnl_pcts)
        result.sharpe_ratio = (mean(pnl_pcts) / sd * (252 ** 0.5)) if sd > 0 else 0.0

    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for p in pnl_pcts:
        cumulative += p
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)
    result.max_drawdown = -max_dd

    # Baseline: buy-and-hold of all unique stocks over the period
    unique_codes = list(set(t.code for t in result.trades))
    baseline_returns = []
    for code in unique_codes:
        first_quote = (
            s.query(StockQuote)
            .filter(StockQuote.code == code, StockQuote.trade_date >= start)
            .order_by(StockQuote.trade_date.asc())
            .first()
        )
        last_quote = (
            s.query(StockQuote)
            .filter(StockQuote.code == code, StockQuote.trade_date <= today)
            .order_by(StockQuote.trade_date.desc())
            .first()
        )
        if first_quote and last_quote and first_quote.close > 0:
            ret = (last_quote.close - first_quote.close) / first_quote.close * 100
            baseline_returns.append(ret)

    if baseline_returns:
        result.baseline_pnl = mean(baseline_returns)
        result.excess_return = result.total_pnl_pct / result.n_trades - result.baseline_pnl

    return result


def format_backtest_report(result: BacktestResult) -> str:
    """Markdown summary."""
    lines = []
    lines.append("# 📊 事件驱动回测")
    lines.append("")
    lines.append(f"回测区间: {result.start_date} → {result.end_date}")
    lines.append(f"持仓天数: {DEFAULT_HOLD_DAYS}天")
    lines.append(f"仓位: {DEFAULT_POSITION_SIZE*100:.0f}% / 笔")
    lines.append("")
    lines.append("## 核心指标")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总交易数 | {result.n_trades} |")
    lines.append(f"| 胜率 | {result.win_rate:.1f}% ({result.n_wins}胜 / {result.n_losses}负) |")
    lines.append(f"| 总收益 | {result.total_pnl_pct:+.2f}% |")
    lines.append(f"| 单笔均收益 | {result.avg_pnl_per_trade:+.3f}% |")
    lines.append(f"| Sharpe (年化) | {result.sharpe_ratio:+.2f} |")
    lines.append(f"| 最大回撤 | {result.max_drawdown:+.2f}% |")
    lines.append(f"| 基准收益 | {result.baseline_pnl:+.2f}% |")
    lines.append(f"| 超额收益 | {result.excess_return:+.2f}% |")
    lines.append("")

    if result.trades:
        lines.append("## 最近交易")
        lines.append("")
        lines.append("| 日期 | 股票 | 方向 | 入场 | 出场 | 收益% |")
        lines.append("|------|------|------|------|------|-------|")
        for t in result.trades[-10:]:
            arrow = "↑" if t.side == "long" else "↓"
            lines.append(
                f"| {t.entry_date} | {t.code} | {arrow}{t.side} | "
                f"{t.entry_price:.2f} | {t.exit_price:.2f if t.exit_price else 0} | "
                f"{t.pnl_pct:+.2f}% |"
            )
    return "\n".join(lines)