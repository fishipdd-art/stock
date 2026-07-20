"""
One-time DB migration: bring data/db/supply_chain.db schema in line with
the current SQLAlchemy models.

Idempotent: safe to run multiple times.
- Creates tables that don't exist (via metadata.create_all)
- Adds columns that don't exist on existing tables (via ALTER TABLE ADD COLUMN)

Usage:  python scripts/migrate_db.py
"""
from __future__ import annotations
import sqlite3, os, sys
from sqlalchemy import create_engine
from sqlalchemy.types import (
    Integer, BigInteger, Float, String, Text, Boolean, Date, DateTime, JSON,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from storage.database import Base  # noqa: E402

DB_PATH = 'data/db/supply_chain.db'


def sqlite_type(col) -> str:
    """Best-effort SQL type for ALTER TABLE ADD COLUMN."""
    t = col.type
    if isinstance(t, Integer):
        return 'INTEGER'
    if isinstance(t, BigInteger):
        return 'BIGINT'
    if isinstance(t, Float):
        return 'FLOAT'
    if isinstance(t, Boolean):
        return 'BOOLEAN'
    if isinstance(t, DateTime):
        return 'DATETIME'
    if isinstance(t, Date):
        return 'DATE'
    if isinstance(t, Text):
        return 'TEXT'
    if isinstance(t, JSON):
        return 'TEXT'  # SQLite stores JSON as TEXT
    if isinstance(t, String):
        n = getattr(t, 'length', None) or 255
        return f'VARCHAR({n})'
    return 'TEXT'


def default_literal(col) -> str | None:
    """Return SQL literal for the column default, or None."""
    d = col.default
    if d is None:
        return None
    arg = d.arg
    if callable(arg):
        return None  # skip callable defaults (datetime.utcnow) — column will be NULL or handled
    if isinstance(arg, bool):
        return '1' if arg else '0'
    if isinstance(arg, (int, float)):
        return str(arg)
    if isinstance(arg, str):
        return repr(arg)
    if isinstance(arg, (list, dict)):
        return repr(arg)
    return repr(arg)


def add_missing_columns(conn):
    """ALTER TABLE ADD COLUMN for every (table, col) the model has but DB lacks."""
    existing = {}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        t = row[0]
        if t.startswith('sqlite_') or t.startswith('alembic_'):
            continue
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()}
        existing[t] = cols

    adds = 0
    for tbl in Base.metadata.sorted_tables:
        tname = tbl.name
        if tname not in existing:
            continue  # will be handled by create_all
        for col in tbl.columns:
            if col.name in existing[tname]:
                continue
            sqlt = sqlite_type(col)
            default = default_literal(col)
            nullable = col.nullable and default is None  # only NULL if no default
            parts = [f'ALTER TABLE {tname} ADD COLUMN {col.name} {sqlt}']
            if default is not None:
                parts.append(f'DEFAULT {default}')
            if not nullable:
                parts.append('NOT NULL')
            sql = ' '.join(parts)
            print(f'  + {tname}.{col.name}  {sqlt}  default={default}')
            conn.execute(sql)
            adds += 1
    return adds


def main():
    if not os.path.exists(DB_PATH):
        print(f'[skip] {DB_PATH} 不存在，无需迁移')
        return

    print(f'=== 迁移 {DB_PATH} (size={os.path.getsize(DB_PATH):,} bytes) ===')

    # 1) Create missing tables via SQLAlchemy
    engine = create_engine(f'sqlite:///{DB_PATH}')
    Base.metadata.create_all(engine)
    engine.dispose()
    print('[1/2] SQLAlchemy create_all 完毕')

    # 2) Add missing columns
    conn = sqlite3.connect(DB_PATH)
    try:
        n = add_missing_columns(conn)
        conn.commit()
        print(f'[2/2] ALTER TABLE 完毕，新增 {n} 列')
    finally:
        conn.close()

    print('\n=== 验证：再 diff 一遍 ===')
    # re-import after migrations
    from scripts._check_schema import check  # noqa
    check()


if __name__ == '__main__':
    main()
