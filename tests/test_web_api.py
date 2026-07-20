"""Integration tests for the FastAPI app (run_job endpoint)."""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """A TestClient that points at a temp DB."""
    # Force no internal token (open mode) for these tests
    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
    from web import auth, server
    importlib.reload(auth)
    importlib.reload(server)
    # Override DB
    from storage import database
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(database.settings, "db_path", pathlib.Path(tmp) / "test.db")
        monkeypatch.setattr(database.settings, "database_url", "")
        monkeypatch.setattr(database, "_db", None)
        db = database.init_db()
        yield TestClient(server.app)
        monkeypatch.setattr(database, "_db", None)


class TestRunJobAuth:
    def test_run_unknown_job_returns_400(self, client):
        # Open mode: should reach the handler and return 400
        resp = client.post("/api/run/does_not_exist")
        assert resp.status_code == 400


def test_pipeline_wait_for_completion_returns_final_result(client, monkeypatch):
    from pipeline import service

    monkeypatch.setattr(
        service,
        "create_pipeline_run",
        lambda pipeline, params, key, trigger_source: (
            {"run_id": "test-run", "pipeline": pipeline, "status": "queued"}, True,
        ),
    )
    monkeypatch.setattr(
        service,
        "execute_pipeline_run",
        lambda run_id: {
            "run_id": run_id,
            "pipeline": "compute_hotness",
            "status": "succeeded",
            "quality_status": "pass",
            "item_count": 21,
        },
    )
    resp = client.post(
        "/api/v1/pipeline/compute_hotness",
        json={"idempotency_key": "wait-test", "wait_for_completion": True},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"
    assert resp.json()["quality_status"] == "pass"

    def test_protected_endpoint_requires_token(self, client, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "secret-token")
        from web import auth, server
        importlib.reload(auth)
        importlib.reload(server)
        client2 = TestClient(server.app)

        # No token -> 401
        resp = client2.post("/api/run/does_not_exist")
        assert resp.status_code == 401

        # Wrong token -> 403
        resp = client2.post(
            "/api/run/does_not_exist",
            headers={"X-Internal-Token": "wrong"},
        )
        assert resp.status_code == 403

        # Right token -> reaches handler (400 here)
        resp = client2.post(
            "/api/run/does_not_exist",
            headers={"X-Internal-Token": "secret-token"},
        )
        assert resp.status_code == 400
