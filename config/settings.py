"""
Global settings for the supply chain stock analysis system.
"""
from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # === Paths ===
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data"
    db_path: Path = PROJECT_ROOT / "data" / "db" / "supply_chain.db"
    cache_dir: Path = PROJECT_ROOT / "data" / "cache"
    reports_dir: Path = PROJECT_ROOT / "data" / "reports"
    logs_dir: Path = PROJECT_ROOT / "data" / "logs"

    # === Database backend (sqlite or postgresql) ===
    # Set DATABASE_URL=postgresql://user:pass@host:5432/dbname to use Postgres
    database_url: str = ""  # if empty, use SQLite at db_path
    db_echo: bool = False

    # === Cache backend ===
    # Set REDIS_URL=redis://host:6379/0 to enable Redis
    redis_url: str = ""

    # === LLM config ===
    llm_api_base: str = ""  # OpenAI-compatible API
    llm_api_key: str = ""
    llm_model: str = "gpt-3.5-turbo"

    # === Knowledge graph source ===
    knowledge_graph_dir: Path = (
        PROJECT_ROOT / "data" / "knowledge_graph"
    )  # where the 4 JSON files live

    # === Schedule ===
    # All times in Asia/Shanghai; APScheduler converts from local to UTC internally
    daily_news_run_time: str = "07:30"  # before 8am target
    daily_report_run_time: str = "08:30"  # before 9am target
    weekly_backfill_day: str = "sat"  # Saturday backfill
    weekly_backfill_time: str = "10:00"
    # Dify is the only production scheduler. Set to "python" manually only
    # during an explicitly declared disaster-recovery window.
    scheduler_owner: str = "dify"

    # === Hotness ranking ===
    deep_process_top_n: int = 8  # Only the strongest categories get deep processing
    hotness_window_days: int = 1  # Lookback window for hotness calc

    # === Time decay ===
    # Exponential decay: weight = exp(-lambda * days_old)
    # Default lambda = 0.35 -> day0=1.0, day1=0.70, day2=0.50, day7=0.08
    time_decay_lambda: float = 0.35
    news_keep_days: int = 7  # discard news older than this

    # === Feishu (awaiting credentials) ===
    feishu_app_id: str = Field(default="", description="Feishu app id (awaiting)")
    feishu_app_secret: str = Field(default="", description="Feishu app secret (awaiting)")
    feishu_webhook_url: str = Field(default="", description="Feishu custom robot webhook")
    feishu_enabled: bool = False  # auto-enabled when credentials present
    # Optional inbound bot features. They are not required for Dify push and
    # are disabled by default so web/API startup never depends on a long-lived
    # Feishu socket or an external tunnel process.
    feishu_ws_enabled: bool = False
    public_tunnel_enabled: bool = False

    # === Tavily web-search (backstops CLS/EastMoney blind spots) ===
    # Empty key disables the Tavily collector silently.
    tavily_api_key: str = Field(
        default="",
        description="Tavily web-search API key. Empty disables TavilyCollector.",
    )

    # === Logging ===
    log_level: str = "INFO"
    log_rotation: str = "100 MB"

    # === HTTP ===
    http_timeout: float = 15.0
    http_max_retries: int = 3

    def ensure_dirs(self) -> None:
        """Create all required directories."""
        for d in [self.data_dir, self.db_path.parent, self.cache_dir,
                  self.reports_dir, self.logs_dir, self.knowledge_graph_dir]:
            d.mkdir(parents=True, exist_ok=True)


def configure_logging(verbose: bool = False, log_file: str | None = None) -> None:
    """Configure loguru once — call at process start, do NOT call logger.remove() elsewhere.

    Args:
        verbose: Enable DEBUG level instead of INFO.
        log_file: Optional file path for persistent logs.
    """
    import sys
    from loguru import logger as _logger
    _logger.remove()
    level = "DEBUG" if verbose else settings.log_level
    _logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
    )
    if log_file:
        _logger.add(
            log_file,
            level=level,
            rotation=settings.log_rotation,
            retention="30 days",
        )


# Singleton
settings = Settings()
settings.ensure_dirs()
