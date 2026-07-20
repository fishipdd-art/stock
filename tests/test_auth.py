"""Tests for web/auth.py."""
from __future__ import annotations

import importlib

import pytest


class TestIsProtected:
    def test_no_token(self, monkeypatch):
        monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
        from web import auth
        importlib.reload(auth)
        assert auth.is_protected() is False

    def test_with_token(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "secret-123")
        from web import auth
        importlib.reload(auth)
        assert auth.is_protected() is True


class TestVerifyToken:
    def test_open_mode_passes(self, monkeypatch):
        monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
        from web import auth
        importlib.reload(auth)
        # No header needed in open mode
        auth.verify_internal_token(x_internal_token=None)

    def test_missing_header_rejected(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "secret-123")
        from web import auth
        importlib.reload(auth)
        with pytest.raises(Exception) as exc:
            auth.verify_internal_token(x_internal_token=None)
        assert exc.value.status_code == 401

    def test_wrong_token_rejected(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "secret-123")
        from web import auth
        importlib.reload(auth)
        with pytest.raises(Exception) as exc:
            auth.verify_internal_token(x_internal_token="wrong")
        assert exc.value.status_code == 403

    def test_correct_token_accepted(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "secret-123")
        from web import auth
        importlib.reload(auth)
        # Should not raise
        auth.verify_internal_token(x_internal_token="secret-123")
