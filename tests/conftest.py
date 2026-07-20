"""
Shared pytest fixtures.

The stock system has a complex default-on-import config (paths, .env, DB
location). For unit tests we want isolation: a tmp SQLite database per
test, project_root left untouched, and module imports cheap.

Strategy: lazy-import project modules inside fixtures/tests, not at
import time. This avoids the chain
  conftest.py -> config.settings -> settings.ensure_dirs()
which would touch the user's data/ dir on test runs.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


# Make project root importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def tmp_data_dir(monkeypatch, tmp_path):
    """Redirect settings to a temporary data/ directory.

    After: settings.data_dir / settings.db_path / settings.logs_dir etc.
    all point inside tmp_path. Imported modules that cached the
    singleton Database at import time are NOT affected (they keep the
    real DB); new code paths in the test body that call get_db() will
    use the temp one.
    """
    monkeypatch.setenv("STOCK_TEST_TMP", str(tmp_path))
    return tmp_path


@pytest.fixture
def in_memory_db(tmp_data_dir, monkeypatch):
    """SQLite :memory: backed Database with schema created.

    Returns a storage.database.Database instance. The session factory
    is reset on teardown.
    """
    from storage import database
    monkeypatch.setattr(database.settings, "db_path", tmp_data_dir / "test.db")
    monkeypatch.setattr(database.settings, "database_url", "")
    # Force re-init: clear the cached singleton
    monkeypatch.setattr(database, "_db", None)
    db = database.init_db()
    yield db
    # Cleanup
    monkeypatch.setattr(database, "_db", None)


@pytest.fixture
def sample_signal():
    """A reusable KnowledgeSignal object (detached from a session)."""
    from storage.models import KnowledgeSignal
    return KnowledgeSignal(
        id=1,
        signal_key="test::signal_001",
        title="Test signal about copper supply",
        description="desc",
        strength=4.0,
        direction="supply_tight",
        grade="A",
        phase="active",
    )


@pytest.fixture
def sample_event():
    """A reusable IndustryEvent object (detached)."""
    from datetime import date
    from storage.models import IndustryEvent
    return IndustryEvent(
        id=1,
        industry="semiconductor",
        industry_label="半导体",
        title="Test launch",
        event_type="launch",
        event_date=date.today(),
        impact_level=4,
        source="curated",
        is_future=True,
    )
