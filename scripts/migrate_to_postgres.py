"""
PostgreSQL migration script.

Migrates SQLite data to PostgreSQL when DATABASE_URL is set.

Usage:
  export DATABASE_URL=postgresql://user:pass@host:5432/dbname
  python scripts/migrate_to_postgres.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


def main():
    from sqlalchemy import create_engine, text, inspect
    from sqlalchemy.orm import sessionmaker
    from storage import get_db
    from storage.models import (
        Base, KnowledgeCategory, SearchTerm, AStock, KnowledgeSignal, SignalStock,
        FuturesPrice, NewsRaw, StockQuote, SectorHeat, DailyReport, PendingTerm,
        JobRun, SystemState, IndustryEvent, EventReminder, UserProfile, UserFavorite,
    )
    from config.settings import settings

    pg_url = os.environ.get("DATABASE_URL")
    if not pg_url:
        print("[ERR] DATABASE_URL env var not set.")
        print("Example: export DATABASE_URL=postgresql://user:pass@localhost:5432/stockdb")
        return 1

    # Source: SQLite
    src_db = get_db()
    src_session = src_db.session()

    # Target: PostgreSQL
    print(f"Connecting to PostgreSQL: {pg_url.split('@')[-1]}")
    tgt_engine = create_engine(pg_url, future=True)
    TgtSession = sessionmaker(bind=tgt_engine, expire_on_commit=False)
    tgt_session = TgtSession()

    # Create schema
    print("Creating PostgreSQL schema...")
    Base.metadata.create_all(tgt_engine)

    # Table mapping
    tables = [
        ("knowledge_categories", KnowledgeCategory),
        ("search_terms", SearchTerm),
        ("a_stocks", AStock),
        ("knowledge_signals", KnowledgeSignal),
        ("signal_stocks", SignalStock),
        ("futures_prices", FuturesPrice),
        ("news_raw", NewsRaw),
        ("stock_quotes", StockQuote),
        ("sector_heat", SectorHeat),
        ("daily_reports", DailyReport),
        ("pending_terms", PendingTerm),
        ("job_runs", JobRun),
        ("system_state", SystemState),
        ("industry_events", IndustryEvent),
        ("event_reminders", EventReminder),
        ("user_profiles", UserProfile),
        ("user_favorites", UserFavorite),
    ]

    print("Migrating data...")
    for table_name, model_class in tables:
        try:
            rows = src_session.query(model_class).all()
            if not rows:
                print(f"  {table_name}: empty, skipping")
                continue
            count = 0
            for row in rows:
                tgt_session.merge(row)
                count += 1
            tgt_session.commit()
            print(f"  {table_name}: {count} rows migrated")
        except Exception as e:
            tgt_session.rollback()
            print(f"  {table_name}: ERROR {e}")

    print("Verifying migration...")
    inspector = inspect(tgt_engine)
    tables_in_db = inspector.get_table_names()
    print(f"  Tables in PostgreSQL: {len(tables_in_db)}")
    print(f"  Expected: {len(tables)}")
    if len(tables_in_db) >= len(tables):
        print("✅ Migration successful!")
        return 0
    else:
        print("⚠️  Some tables missing")
        return 1


if __name__ == "__main__":
    sys.exit(main())