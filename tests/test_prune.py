"""Tests for the JobRun retention / prune feature."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from storage.models import JobRun
from scheduler.jobs import prune_job_runs


def _insert_run(db, job_id: str, days_ago: float, status: str = "ok") -> int:
    """Insert one JobRun row with started_at = now - days_ago."""
    started = datetime.utcnow() - timedelta(days=days_ago)
    with db.tx() as s:
        r = JobRun(
            job_id=job_id,
            job_name=job_id,
            started_at=started,
            status=status,
            duration_sec=0.0,
        )
        s.add(r)
        s.flush()
        return r.id


class TestPruneJobRuns:
    def test_no_old_rows(self, in_memory_db):
        _insert_run(in_memory_db, "collect_futures", days_ago=1)
        n = prune_job_runs(older_than_days=30)
        assert n == 0
        with in_memory_db.session() as s:
            assert s.query(JobRun).count() == 1

    def test_deletes_old_rows(self, in_memory_db):
        _insert_run(in_memory_db, "collect_futures", days_ago=1)
        _insert_run(in_memory_db, "collect_futures", days_ago=31)
        _insert_run(in_memory_db, "collect_futures", days_ago=60)
        n = prune_job_runs(older_than_days=30)
        assert n == 2
        with in_memory_db.session() as s:
            assert s.query(JobRun).count() == 1

    def test_filter_by_job_id(self, in_memory_db):
        _insert_run(in_memory_db, "collect_futures", days_ago=31)
        _insert_run(in_memory_db, "compute_hotness", days_ago=31)
        n = prune_job_runs(older_than_days=30, job_id="collect_futures")
        assert n == 1
        with in_memory_db.session() as s:
            # Only the compute_hotness row remains
            remaining = s.query(JobRun).all()
            assert len(remaining) == 1
            assert remaining[0].job_id == "compute_hotness"

    def test_threshold_exact_boundary(self, in_memory_db):
        # A row 30.5 days old should be deleted at threshold=30
        _insert_run(in_memory_db, "compute_hotness", days_ago=30.5)
        n = prune_job_runs(older_than_days=30)
        assert n == 1

    def test_young_rows_kept(self, in_memory_db):
        # Even young rows are kept regardless of their job_id
        _insert_run(in_memory_db, "compute_hotness", days_ago=10)
        _insert_run(in_memory_db, "generate_report", days_ago=29)
        n = prune_job_runs(older_than_days=30)
        assert n == 0
        with in_memory_db.session() as s:
            assert s.query(JobRun).count() == 2

    def test_returns_zero_on_empty_table(self, in_memory_db):
        n = prune_job_runs(older_than_days=30)
        assert n == 0
