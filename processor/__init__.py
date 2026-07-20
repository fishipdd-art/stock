"""Processor package init."""
from .time_decay import (  # noqa: F401
    age_days, weight_for_age, weight_for_datetime,
    filter_recent, score_with_decay,
)
from .matcher import (  # noqa: F401
    SignalMatch, match_news_to_signal, match_news_to_term,
    build_matches, group_matches_by_signal, group_matches_by_category,
)
from .supply_demand import (  # noqa: F401
    MismatchSignal, detect_from_futures, detect_from_knowledge_signals,
    detect_from_news, aggregate_mismatches,
)
from .event_boost import (  # noqa: F401
    EventBoost, compute_event_boost, get_upcoming_events_for_stocks,
    get_stock_codes_for_signal,
)
from .report import (  # noqa: F401
    generate_markdown_report, generate_feishu_payload, save_report,
)
from .signal_hits import (  # noqa: F401
    persist_signal_hits,
)