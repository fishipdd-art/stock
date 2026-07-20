"""
Industry events module.

Combines:
  A. Auto-generated macro calendar (FOMC, PBOC, NBS, USDA, OPEC, EIA) — scrapers
  B. Curated industry events (rockets, products, conferences)
  C. Auto-detected events from news (regex + keyword matching)
  D. Historical context (past 12 months)
"""
from __future__ import annotations

from .collector import (  # noqa: F401
    generate_macro_calendar,
    load_curated_events,
    collect_all_events,
    upsert_events,
    refresh_events,
    get_upcoming,
    get_events,
)
from .detector import (  # noqa: F401
    detect_from_news,
    save_detected_events,
    detect_and_save,
    DetectedEvent,
)
from .reminder import (  # noqa: F401
    get_reminders, render_reminder_payload, save_reminders,
    push_reminders_to_feishu, run_reminder_job, get_recent_reminders,
    UpcomingReminder,
)
from .macro_scraper import (  # noqa: F401
    scrape_all, scrape_pboc, scrape_nbs, scrape_fomc,
    scrape_eia, scrape_opec, scrape_usda_wasde, scrape_us_employment,
)
from .backtest import (  # noqa: F401
    run_backtest, format_backtest_report, backtest_event,
    EventBacktest, AggregateCorrelation,
)
from .predictor import (  # noqa: F401
    predict_impact, predict_upcoming, ImpactPrediction, TYPE_HEURISTIC,
)
from .advanced_predictor import (  # noqa: F401
    predict_advanced, predict_upcoming_advanced, AdvancedPrediction,
)
from .clustering import (  # noqa: F401
    find_clusters, summarize_clusters, EventCluster,
)
from .ml_model import (  # noqa: F401
    predict_dlm, predict_upcoming_dlm, train_model, load_model,
    DLMPrediction, HAS_TORCH,
)
from .playwright_scraper import (  # noqa: F401
    scrape_all_playwright, scrape_pboc_playwright, scrape_nbs_playwright,
    HAS_PLAYWRIGHT,
)