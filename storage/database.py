"""
Database setup (SQLite + SQLAlchemy 2.x) with PostgreSQL support.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from config.settings import settings
from storage.models import Base


def _make_engine(db_url: str | None = None, db_path: Path | None = None) -> Engine:
    """Create SQLAlchemy engine. Supports SQLite (default) and PostgreSQL.

    Priority:
      1. Explicit db_url argument
      2. settings.database_url (from env DATABASE_URL)
      3. SQLite at settings.db_path
    """
    db_url = db_url or settings.database_url

    if db_url and db_url.startswith("postgres"):
        # PostgreSQL
        engine = create_engine(
            db_url,
            echo=settings.db_echo,
            future=True,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )
        return engine

    # SQLite (default)
    db_path = db_path or settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        future=True,
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()

    return engine


class Database:
    """Thin wrapper around SQLAlchemy engine + session."""

    def __init__(self, db_url: str | None = None, db_path: Path | None = None):
        self.engine = _make_engine(db_url, db_path)
        self.SessionLocal = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False
        )

    def init_schema(self) -> None:
        """Create all tables (works for both SQLite and PostgreSQL)."""
        Base.metadata.create_all(self.engine)
        self._migrate_compat_columns()

    def _migrate_compat_columns(self) -> None:
        """Apply small additive migrations for installations without Alembic.

        These migrations are deliberately limited to nullable/defaulted
        columns and indexes, so startup remains idempotent for SQLite and
        PostgreSQL deployments.
        """
        schema = inspect(self.engine)
        tables = set(schema.get_table_names())
        additions = {
            "pipeline_runs": {
                "business_date": "DATE",
            },
            "signal_hits": {
                "match_key": "VARCHAR(64)",
            },
            "sector_heat": {
                "attention_score": "FLOAT NOT NULL DEFAULT 0",
                "market_score": "FLOAT NOT NULL DEFAULT 0",
                "evidence_score": "FLOAT NOT NULL DEFAULT 0",
                "calculation_version": "VARCHAR(16) NOT NULL DEFAULT 'v2'",
            },
        }
        with self.engine.begin() as connection:
            for table, columns in additions.items():
                if table not in tables:
                    continue
                existing = {c["name"] for c in inspect(self.engine).get_columns(table)}
                for column, ddl in columns.items():
                    if column not in existing:
                        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            if "pipeline_runs" in tables:
                connection.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_pipeline_runs_business_date "
                    "ON pipeline_runs (business_date)"
                ))
            if "signal_hits" in tables:
                connection.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_signal_hits_match_key "
                    "ON signal_hits (match_key)"
                ))

    def session(self) -> Session:
        return self.SessionLocal()

    @contextmanager
    def tx(self) -> Iterator[Session]:
        """Context manager that commits on success, rolls back on error."""
        s = self.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def init_db(db_path: Path | None = None, db_url: str | None = None) -> Database:
    global _db
    if db_url is not None:
        _db = Database(db_url=db_url)
    elif db_path is not None:
        _db = Database(db_path=db_path)
    else:
        _db = get_db()
    _db.init_schema()
    return _db


def is_postgres() -> bool:
    """Check if currently using PostgreSQL."""
    db_url = settings.database_url
    return bool(db_url and db_url.startswith("postgres"))
