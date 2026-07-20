"""
SQLAlchemy ORM models for the supply chain stock analysis system.

Tables:
  - knowledge_categories:    21 supply-chain signal categories from terms.json
  - search_terms:            70+ search terms (with priority, transmission logic)
  - knowledge_signals:       506 known supply-chain signals (with stocks/grades)
  - signal_stocks:           M2M between signals and A-shares
  - a_stocks:                148 A-share metadata (code, name, sector tags)
  - futures_prices:          daily futures prices (L3 close-of-day)
  - spot_prices:             spot commodity prices (optional)
  - news_raw:                scraped news (财联社 / EastMoney)
  - news_term_match:         which search terms each news matched
  - stock_quotes:            daily A-share quotes for hotness calc
  - sector_heat_daily:       daily hotness scores per industry
  - daily_reports:           generated daily reports (text + payload)
  - feishu_pushes:           audit log for every Feishu push attempt
  - tavily_quota_log:        per-day Tavily API call counter
"""
from __future__ import annotations

from datetime import datetime, date
from sqlalchemy import (
    String, Integer, Float, DateTime, Date, Text, Boolean,
    ForeignKey, Index, UniqueConstraint, JSON,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ============================================================================
# Knowledge graph (loaded once from JSON files)
# ============================================================================

class KnowledgeCategory(Base):
    """Top-level signal categories from supply_chain_terms.json."""
    __tablename__ = "knowledge_categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    signal_type: Mapped[str] = mapped_column(String(128))  # 'policy' / 'supply_tight' etc.
    n_terms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SearchTerm(Base):
    """Search terms from supply_chain_terms.json.

    Each term is a query string used to scan daily news. The 'transmission_logic'
    field describes the supply-chain propagation pattern (e.g. "原油大涨→全球通胀").
    """
    __tablename__ = "search_terms"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(String(256), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("knowledge_categories.id"))
    priority: Mapped[str] = mapped_column(String(8))  # '高' / '中' / '低'
    transmission_logic: Mapped[str] = mapped_column(Text)  # 传导逻辑
    a_share_map: Mapped[str] = mapped_column(Text)  # 关联A股 (raw text)
    a_share_codes: Mapped[str] = mapped_column(Text, default="")  # parsed codes (comma sep)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("term", "category_id", name="uq_term_category"),
    )

    category: Mapped["KnowledgeCategory"] = relationship(lazy="joined")


class AStock(Base):
    """A-share metadata."""
    __tablename__ = "a_stocks"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    sector_tags: Mapped[str] = mapped_column(Text, default="")  # comma-sep
    supply_exposure: Mapped[str] = mapped_column(String(8), default="")  # 低/中/高
    tier: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class KnowledgeSignal(Base):
    """Known supply-chain signals from supply_chain_signals.json.

    Each signal is a 'supply chain event' (price spike, shortage, policy etc.).
    """
    __tablename__ = "knowledge_signals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    signal_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    price_info: Mapped[str] = mapped_column(Text, default="")
    grade: Mapped[str] = mapped_column(String(4))  # A / B / C / D
    direction: Mapped[str] = mapped_column(String(32))  # supply_tight / etc.
    strength: Mapped[float] = mapped_column(Float, default=0.0)
    veracity: Mapped[str] = mapped_column(String(16), default="")
    phase: Mapped[str] = mapped_column(String(16), default="active")
    signal_date: Mapped[str] = mapped_column(String(16), index=True)  # YYYY-MM-DD
    note: Mapped[str] = mapped_column(Text, default="")
    sources_json: Mapped[str] = mapped_column(Text, default="[]")  # JSON list
    last_hit_ts: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    stocks: Mapped[list["SignalStock"]] = relationship(
        back_populates="signal", cascade="all, delete-orphan"
    )


class SignalStock(Base):
    """M2M between signals and A-shares with strength."""
    __tablename__ = "signal_stocks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("knowledge_signals.id"), index=True)
    stock_code: Mapped[str] = mapped_column(String(8), index=True)
    strength: Mapped[float] = mapped_column(Float, default=0.0)

    signal: Mapped["KnowledgeSignal"] = relationship(back_populates="stocks")

    __table_args__ = (
        UniqueConstraint("signal_id", "stock_code", name="uq_signal_stock"),
    )


# ============================================================================
# Signal hits — ephemeral match results persisted for the web UI
# ============================================================================

class SignalHit(Base):
    """A news article matched against a signal or search term.

    Created after each news collection run by persist_signal_hits().
    Kept for N days (cron cleanup), then pruned.
    """
    __tablename__ = "signal_hits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_signals.id", ondelete="SET NULL"), index=True,
    )
    term: Mapped[str] = mapped_column(String(256), default="")
    news_id: Mapped[int] = mapped_column(index=True)
    news_title: Mapped[str] = mapped_column(String(512))
    news_url: Mapped[str] = mapped_column(String(512), default="")
    news_source: Mapped[str] = mapped_column(String(32), default="")
    hit_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    final_score: Mapped[float] = mapped_column(Float, default=0.0)
    # Stable idempotency key for (news, signal/term, matcher version).  It is
    # nullable so an existing database can be migrated without fabricating
    # keys before historical duplicates are reconciled.
    match_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True,
    )

    signal: Mapped["KnowledgeSignal | None"] = relationship(lazy="joined")


# ============================================================================
# Daily collected data
# ============================================================================

class FuturesPrice(Base):
    """Daily futures prices (L3 end-of-day)."""
    __tablename__ = "futures_prices"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)  # e.g. "CU2601"
    name: Mapped[str] = mapped_column(String(64))  # e.g. "沪铜2601"
    exchange: Mapped[str] = mapped_column(String(8))  # SHFE / DCE / CZCE / etc.
    open: Mapped[float] = mapped_column(Float, default=0.0)
    high: Mapped[float] = mapped_column(Float, default=0.0)
    low: Mapped[float] = mapped_column(Float, default=0.0)
    close: Mapped[float] = mapped_column(Float, default=0.0)
    settle: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    position: Mapped[float] = mapped_column(Float, default=0.0)
    change_pct: Mapped[float] = mapped_column(Float, default=0.0)  # 涨跌幅
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_futures_date_sym", "trade_date", "symbol"),
        UniqueConstraint("trade_date", "symbol", name="uq_futures_date_symbol"),
    )


class NewsRaw(Base):
    """Raw scraped news."""
    __tablename__ = "news_raw"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), index=True)  # cls / eastmoney / rss
    source_label: Mapped[str] = mapped_column(String(64), default="")
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    content: Mapped[str] = mapped_column(Text, default="")
    keywords_matched: Mapped[str] = mapped_column(Text, default="")  # comma-sep


class StockQuote(Base):
    """Daily A-share quote for hotness calculation."""
    __tablename__ = "stock_quotes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    code: Mapped[str] = mapped_column(String(8), index=True)
    name: Mapped[str] = mapped_column(String(64))
    open: Mapped[float] = mapped_column(Float, default=0.0)
    close: Mapped[float] = mapped_column(Float, default=0.0)
    high: Mapped[float] = mapped_column(Float, default=0.0)
    low: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    turnover: Mapped[float] = mapped_column(Float, default=0.0)  # 成交额
    change_pct: Mapped[float] = mapped_column(Float, default=0.0)
    change_amt: Mapped[float] = mapped_column(Float, default=0.0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_stock_date_code", "trade_date", "code"),
        UniqueConstraint("trade_date", "code", name="uq_stock_date_code"),
    )


class SectorHeat(Base):
    """Daily industry/category hotness scores."""
    __tablename__ = "sector_heat"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    category_name: Mapped[str] = mapped_column(String(128), index=True)
    hotness_score: Mapped[float] = mapped_column(Float, default=0.0)
    abs_change_sum: Mapped[float] = mapped_column(Float, default=0.0)
    turnover_sum: Mapped[float] = mapped_column(Float, default=0.0)
    news_count: Mapped[int] = mapped_column(Integer, default=0)
    n_stocks: Mapped[int] = mapped_column(Integer, default=0)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    processed_level: Mapped[str] = mapped_column(String(16), default="shallow")
    attention_score: Mapped[float] = mapped_column(Float, default=0.0)
    market_score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    calculation_version: Mapped[str] = mapped_column(String(16), default="v2")
    # 'deep' (top N) or 'shallow' (price+title only)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("trade_date", "category_name", name="uq_heat_date_cat"),
    )


# ============================================================================
# Reports (output)
# ============================================================================

class DailyReport(Base):
    """Generated daily reports (Markdown + JSON payload for feishu)."""
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    report_type: Mapped[str] = mapped_column(String(32))  # 'morning_brief' / 'full' / 'backfill' / 'manual'
    markdown: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text)  # for feishu card
    feishu_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    feishu_sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    n_signals: Mapped[int] = mapped_column(Integer, default=0)
    n_news: Mapped[int] = mapped_column(Integer, default=0)
    n_top_categories: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("report_date", "report_type", name="uq_report_date_type"),
    )


class JobRun(Base):
    """Track scheduler job execution history (for status display)."""
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    job_name: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/ok/error
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    trigger_type: Mapped[str] = mapped_column(String(16), default="scheduled")  # scheduled/manual/backfill


class PipelineRun(Base):
    """A Dify-visible, idempotent pipeline execution.

    Unlike ``JobRun``, success is not inferred only from the absence of an
    exception.  ``quality_status`` records whether the produced data passed
    the step-specific quality gate.
    """
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    pipeline: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # queued / running / succeeded / degraded / failed
    quality_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending / pass / warn / fail
    trigger_source: Mapped[str] = mapped_column(String(32), default="dify")
    # Explicit Asia/Shanghai business date.  Never derive dependency gates
    # from naive UTC created_at timestamps.
    business_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    request_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class DataQualitySnapshot(Base):
    """Latest measurable data-health result emitted by a pipeline run."""
    __tablename__ = "data_quality_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    dataset: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)  # pass / warn / fail
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    min_expected: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


# Pending terms (yet to be classified into categories)
class PendingTerm(Base):
    __tablename__ = "pending_terms"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(String(256), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SystemState(Base):
    """Key-value store for system state (e.g., scheduler status)."""
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class UserProfile(Base):
    """Per-user preferences and display name."""
    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    preferences_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class UserFavorite(Base):
    """Items starred by a user (events, signals, stocks)."""
    __tablename__ = "user_favorites"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    item_type: Mapped[str] = mapped_column(String(32), index=True)  # 'event' / 'signal' / 'stock'
    item_id: Mapped[int] = mapped_column(Integer, index=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "item_type", "item_id", name="uq_user_fav"),
    )


class PortfolioAccount(Base):
    """User-confirmed account boundary used by Dify portfolio analysis."""
    __tablename__ = "portfolio_accounts"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    total_assets: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown_tolerance: Mapped[float] = mapped_column(Float, default=0.20)
    external_assets_included: Mapped[bool] = mapped_column(Boolean, default=False)
    as_of: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class PortfolioPosition(Base):
    """One user-confirmed stock/ETF position."""
    __tablename__ = "portfolio_positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str] = mapped_column(String(8), index=True)
    name: Mapped[str] = mapped_column(String(64))
    asset_type: Mapped[str] = mapped_column(String(16), default="stock")
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    available_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    cost_price: Mapped[float] = mapped_column(Float, default=0.0)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_amount: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    risk_bucket: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(32), default="user_confirmed")
    as_of: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "code", name="uq_portfolio_user_code"),
    )


class IndustryEvent(Base):
    """Influential industry events (past + future).

    Three sources:
      A. Macro calendar (auto-generated): FOMC, PBOC LPR, NBS CPI/PPI, etc.
      B. Industry events (curated): rocket launches, product reveals, conferences.
      C. Auto-detected events from news (regex + keyword matching)
      D. Past notable events (historical context for signal analysis).
    """
    __tablename__ = "industry_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    industry: Mapped[str] = mapped_column(String(64), index=True)  # 'macro' / 'aerospace' / 'semiconductor' etc.
    industry_label: Mapped[str] = mapped_column(String(64))  # '航天军工' / '半导体' etc.
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    # launch / earnings / policy / data_release / conference / regulatory / macro / other
    event_date: Mapped[date] = mapped_column(Date, index=True)
    impact_level: Mapped[int] = mapped_column(Integer, default=3)  # 1-5
    related_stocks: Mapped[str] = mapped_column(Text, default="")  # comma-sep codes
    source: Mapped[str] = mapped_column(String(32))  # 'macro_auto' / 'curated' / 'news' / 'historical'
    source_url: Mapped[str] = mapped_column(String(512), default="")
    is_future: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_event_date_industry", "event_date", "industry"),
    )


class EventReminder(Base):
    """Records of event reminder pushes (Feishu / console)."""
    __tablename__ = "event_reminders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("industry_events.id"), index=True)
    reminder_date: Mapped[date] = mapped_column(Date, index=True)
    urgency: Mapped[str] = mapped_column(String(16))  # 'today' / 'tomorrow' / 'this_week'
    days_until: Mapped[int] = mapped_column(Integer, default=0)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    channel: Mapped[str] = mapped_column(String(32), default="")
    delivered_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("event_id", "reminder_date", name="uq_event_reminder"),
    )


class FeishuChat(Base):
    """Registry of Feishu chats that the app bot can push to.

    Each entry corresponds to one ``chat_id`` (group chat / direct
    message / channel) plus a human-readable label. Send operations
    target either a specific chat_id or "all enabled chats" for
    broadcasts.
    """
    __tablename__ = "feishu_chats"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    chat_type: Mapped[str] = mapped_column(String(16), default="group")
    # 'group' / 'dm' / 'channel'
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================================================================
# Supply-chain analysis output (WF-02..WF-06)
# ============================================================================

class StorageEvent(Base):
    """A structured supply-chain event extracted from news by the LLM step.

    One row per (news_id, schema_version). The payload field is the raw JSON
    returned by MiniMax after schema validation; the columns below are the
    flattened fields required by Dify downstream nodes and by WF-03..WF-06.
    """
    __tablename__ = "storage_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    news_id: Mapped[int] = mapped_column(ForeignKey("news_raw.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    # Deterministic hash of (news_url + schema_version) — used for idempotency
    schema_version: Mapped[str] = mapped_column(String(16), default="v1")
    title: Mapped[str] = mapped_column(String(512))
    entities: Mapped[str] = mapped_column(Text, default="")
    products: Mapped[str] = mapped_column(Text, default="")
    industry_chain: Mapped[str] = mapped_column(String(64), index=True, default="")
    region: Mapped[str] = mapped_column(String(64), default="")
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    # supply_tight / demand_pickup / capacity_expansion / price_move / policy / other
    supply_direction: Mapped[str] = mapped_column(String(16), default="")
    # tight / loose / neutral
    demand_direction: Mapped[str] = mapped_column(String(16), default="")
    # up / down / neutral
    magnitude: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # 0..1, written verbatim from the LLM
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    counter_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("news_id", "schema_version", name="uq_event_news_version"),
        Index("ix_storage_event_industry_type", "industry_chain", "event_type"),
    )


class MismatchResult(Base):
    """A supply-demand mismatch identified from one or more StorageEvent rows.

    Produced by WF-03 by aggregating events on a (industry_chain, event_type)
    bucket, applying the configured scoring weights (document §5), and
    projecting the result onto the knowledge graph to discover beneficiary
    and at-risk segments.
    """
    __tablename__ = "mismatch_results"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    result_key: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    industry_chain: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(16), index=True)
    # tight / loose / mixed
    total_score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    multi_source_score: Mapped[float] = mapped_column(Float, default=0.0)
    supply_demand_score: Mapped[float] = mapped_column(Float, default=0.0)
    price_inventory_score: Mapped[float] = mapped_column(Float, default=0.0)
    graph_score: Mapped[float] = mapped_column(Float, default=0.0)
    freshness_score: Mapped[float] = mapped_column(Float, default=0.0)
    tradability_score: Mapped[float] = mapped_column(Float, default=0.0)
    n_events: Mapped[int] = mapped_column(Integer, default=0)
    n_sources: Mapped[int] = mapped_column(Integer, default=0)
    path_json: Mapped[str] = mapped_column(Text, default="[]")
    # chain of {from, to, kind} steps
    beneficiaries_json: Mapped[str] = mapped_column(Text, default="[]")
    at_risk_json: Mapped[str] = mapped_column(Text, default="[]")
    summary: Mapped[str] = mapped_column(Text, default="")
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class StockScore(Base):
    """Score breakdown for a stock/ETF candidate produced by WF-04.

    One row per (trade_date, code) per run; final_score ∈ [0, 100] and the
    six sub-scores mirror the requirements doc §5 split.
    """
    __tablename__ = "stock_scores"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    code: Mapped[str] = mapped_column(String(8), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    asset_type: Mapped[str] = mapped_column(String(16), default="stock")
    direction: Mapped[str] = mapped_column(String(8), index=True)
    # long / short / neutral
    final_score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    multi_source_score: Mapped[float] = mapped_column(Float, default=0.0)
    supply_demand_score: Mapped[float] = mapped_column(Float, default=0.0)
    price_inventory_score: Mapped[float] = mapped_column(Float, default=0.0)
    graph_score: Mapped[float] = mapped_column(Float, default=0.0)
    freshness_score: Mapped[float] = mapped_column(Float, default=0.0)
    tradability_score: Mapped[float] = mapped_column(Float, default=0.0)
    hard_filter_passed: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    hard_filter_reasons: Mapped[str] = mapped_column(Text, default="")
    catalyst_window: Mapped[str] = mapped_column(String(32), default="")
    observe_range: Mapped[str] = mapped_column(String(64), default="")
    entry_range: Mapped[str] = mapped_column(String(64), default="")
    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    invalidation: Mapped[str] = mapped_column(Text, default="")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    counter_evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    priced_in_note: Mapped[str] = mapped_column(Text, default="")
    risk_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("trade_date", "code", name="uq_score_date_code"),
        Index("ix_score_date_final", "trade_date", "final_score"),
    )


class PortfolioDiagnosis(Base):
    """Action recommendation for one held position, produced by WF-05."""
    __tablename__ = "portfolio_diagnoses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    code: Mapped[str] = mapped_column(String(8), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    asset_type: Mapped[str] = mapped_column(String(16), default="stock")
    action: Mapped[str] = mapped_column(String(16), index=True)
    # add / hold / trim / exit
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    industry_logic_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    valuation_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    drawdown_state: Mapped[str] = mapped_column(String(16), default="normal")
    # normal / watch / warn / critical
    bucket_exposure_pct: Mapped[float] = mapped_column(Float, default=0.0)
    observe_range: Mapped[str] = mapped_column(String(64), default="")
    entry_range: Mapped[str] = mapped_column(String(64), default="")
    stop_loss: Mapped[float] = mapped_column(Float, default=0.0)
    invalidation: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    risk_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("user_id", "trade_date", "code", name="uq_diag_user_date_code"),
    )


class MorningReport(Base):
    """Structured morning brief generated by WF-05 and pushed to Feishu."""
    __tablename__ = "morning_reports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    trade_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    portfolio_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    diagnoses_json: Mapped[str] = mapped_column(Text, default="[]")
    candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    risk_buckets_json: Mapped[str] = mapped_column(Text, default="[]")
    cash_suggestion: Mapped[str] = mapped_column(Text, default="")
    markdown: Mapped[str] = mapped_column(Text, default="")
    feishu_card_json: Mapped[str] = mapped_column(Text, default="")
    feishu_pushed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    feishu_pushed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    feishu_run_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class EveningReview(Base):
    """End-of-day review produced by WF-06."""
    __tablename__ = "evening_reviews"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    verified_count: Mapped[int] = mapped_column(Integer, default=0)
    contradicted_count: Mapped[int] = mapped_column(Integer, default=0)
    pnl_attribution_json: Mapped[str] = mapped_column(Text, default="[]")
    view_changes_json: Mapped[str] = mapped_column(Text, default="[]")
    bias_attribution_json: Mapped[str] = mapped_column(Text, default="[]")
    summary: Mapped[str] = mapped_column(Text, default="")
    markdown: Mapped[str] = mapped_column(Text, default="")
    feishu_card_json: Mapped[str] = mapped_column(Text, default="")
    feishu_pushed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    feishu_pushed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    feishu_run_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class FeishuPush(Base):
    """Audit row for every Feishu push attempted by the supply-chain flow.

    ``run_id`` is the PipelineRun or report key; the ``(run_id, payload_kind)``
    unique index guarantees no double-push even when multiple nodes share a
    backend trigger.
    """
    __tablename__ = "feishu_pushes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    payload_kind: Mapped[str] = mapped_column(String(32), index=True)
    # morning / evening / alert / general
    chat_id: Mapped[str] = mapped_column(String(128), default="")
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    response_code: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("run_id", "payload_kind", "chat_id", name="uq_push_run_kind_chat"),
    )


class TavilyQuotaLog(Base):
    """Per-day Tavily API call counter.

    One row per UTC calendar date; ``calls_used`` is incremented atomically
    inside a transaction so manual CLI invocations and cron-triggered runs
    share a single 20-call daily cap.
    """
    __tablename__ = "tavily_quota_log"

    call_date: Mapped[str] = mapped_column(String(10), primary_key=True)  # 'YYYY-MM-DD'
    calls_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
