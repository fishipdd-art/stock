"""Check production DB schema vs current SQLAlchemy models."""
from __future__ import annotations
import sqlite3, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from storage.database import Base

prod = 'data/db/supply_chain.db'

def check():
    conn = sqlite3.connect(prod)
    db_tables = {}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        t = row[0]
        if t.startswith('sqlite_') or t.startswith('alembic_'):
            continue
        db_tables[t] = {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()}
    conn.close()
    model_tables = {tbl.name: [c.name for c in tbl.columns] for tbl in Base.metadata.sorted_tables}

    miss_t = [t for t in model_tables if t not in db_tables]
    miss_c = 0
    for t in model_tables:
        if t in db_tables:
            diff = set(model_tables[t]) - db_tables[t]
            if diff:
                print(f'  表 {t} 缺列: {sorted(diff)}')
                miss_c += len(diff)
    print(f'缺表 {len(miss_t)}: {miss_t}')
    print(f'缺列 {miss_c}')
    return len(miss_t), miss_c

if __name__ == '__main__':
    check()
