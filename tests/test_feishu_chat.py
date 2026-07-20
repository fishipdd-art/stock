"""Tests for the Feishu chat registry (app-bot chat_id routing)."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def feishu(monkeypatch):
    """Fresh FeishuNotifier with clean DB."""
    monkeypatch.setenv("FEISHU_ENABLED", "true")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "")
    from storage import database
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(database.settings, "db_path", pathlib.Path(tmp) / "test.db")
        monkeypatch.setattr(database.settings, "database_url", "")
        monkeypatch.setattr(database, "_db", None)
        db = database.init_db()
        # Reload the notifier module to pick up new settings
        from notifier import feishu as fmod
        importlib.reload(fmod)
        # Also force settings attributes clean (the .env file still has credentials)
        from config.settings import settings as _st
        monkeypatch.setattr(_st, "feishu_app_id", "")
        monkeypatch.setattr(_st, "feishu_app_secret", "")
        monkeypatch.setattr(_st, "feishu_webhook_url", "")
        yield fmod
        monkeypatch.setattr(database, "_db", None)


class TestRegisterChat:
    def test_register_new(self, feishu):
        info = feishu.FeishuNotifier().register_chat(
            chat_id="oc_abc123", name="Trading Group", chat_type="group"
        )
        assert info.chat_id == "oc_abc123"
        assert info.name == "Trading Group"
        assert info.chat_type == "group"
        assert info.enabled is True

    def test_register_idempotent(self, feishu):
        n = feishu.FeishuNotifier()
        n.register_chat(chat_id="oc_x", name="First")
        n.register_chat(chat_id="oc_x", name="Second")  # updates name
        chats = n.list_chats()
        assert len(chats) == 1
        assert chats[0].name == "Second"

    def test_register_can_disable(self, feishu):
        n = feishu.FeishuNotifier()
        n.register_chat(chat_id="oc_d", enabled=False)
        # Default list includes disabled
        assert len(n.list_chats()) == 1
        # enabled_only filter excludes it
        assert n.list_chats(enabled_only=True) == []


class TestListChats:
    def test_empty(self, feishu):
        assert feishu.FeishuNotifier().list_chats() == []

    def test_returns_all(self, feishu):
        n = feishu.FeishuNotifier()
        n.register_chat(chat_id="oc_a", name="A")
        n.register_chat(chat_id="oc_b", name="B")
        n.register_chat(chat_id="oc_c", name="C", enabled=False)
        all_chats = n.list_chats()
        assert len(all_chats) == 3
        assert {c.chat_id for c in all_chats} == {"oc_a", "oc_b", "oc_c"}

    def test_enabled_only(self, feishu):
        n = feishu.FeishuNotifier()
        n.register_chat(chat_id="oc_a", enabled=True)
        n.register_chat(chat_id="oc_b", enabled=False)
        enabled = n.list_chats(enabled_only=True)
        assert len(enabled) == 1
        assert enabled[0].chat_id == "oc_a"


class TestRemoveChat:
    def test_remove_existing(self, feishu):
        n = feishu.FeishuNotifier()
        n.register_chat(chat_id="oc_rm")
        assert n.remove_chat("oc_rm") is True
        assert n.list_chats() == []

    def test_remove_nonexistent(self, feishu):
        assert feishu.FeishuNotifier().remove_chat("oc_nope") is False


class TestSetEnabled:
    def test_toggle(self, feishu):
        n = feishu.FeishuNotifier()
        n.register_chat(chat_id="oc_t", enabled=True)
        assert n.set_chat_enabled("oc_t", False) is True
        assert n.list_chats(enabled_only=True) == []
        # Toggle back
        assert n.set_chat_enabled("oc_t", True) is True
        assert len(n.list_chats(enabled_only=True)) == 1

    def test_missing(self, feishu):
        assert feishu.FeishuNotifier().set_chat_enabled("oc_missing", True) is False


class TestSendRouting:
    """Verify send() picks the right path based on configuration + chat_id."""

    def test_disabled_returns_false(self, feishu, monkeypatch):
        monkeypatch.setenv("FEISHU_ENABLED", "false")
        importlib.reload(feishu)
        n = feishu.FeishuNotifier()
        n.webhook = ""  # no creds
        n.app_id = ""
        n.app_secret = ""
        n._enabled = False
        assert n.send({"msg_type": "text"}) is False

    def test_no_creds_returns_false(self, feishu):
        n = feishu.FeishuNotifier()
        n.webhook = ""
        n.app_id = ""
        n.app_secret = ""
        n._enabled = True
        assert n.send({"msg_type": "text"}) is False

    def test_webhook_path_no_chat_id(self, feishu, monkeypatch):
        """When webhook configured and no chat_id, send to webhook."""
        sent = []

        def fake_webhook(self, payload):
            sent.append(("webhook", payload))
            return True

        monkeypatch.setattr(feishu.FeishuNotifier, "_send_webhook", fake_webhook)
        n = feishu.FeishuNotifier()
        n.webhook = "https://example.com/webhook"
        n._enabled = True
        assert n.send({"msg_type": "text", "x": 1}) is True
        assert sent == [("webhook", {"msg_type": "text", "x": 1})]

    def test_webhook_ignores_chat_id(self, feishu, monkeypatch):
        """Webhook is one-URL; explicit chat_id is warned-and-still-webhook."""
        sent = []

        def fake_webhook(self, payload):
            sent.append(("webhook", payload))
            return True

        monkeypatch.setattr(feishu.FeishuNotifier, "_send_webhook", fake_webhook)
        n = feishu.FeishuNotifier()
        n.webhook = "https://example.com/webhook"
        n._enabled = True
        assert n.send({"x": 1}, chat_id="oc_x") is True
        assert sent == [("webhook", {"x": 1})]

    def test_app_bot_specific_chat(self, feishu, monkeypatch):
        sent = []

        def fake_send_to_chat(self, payload, chat_id):
            sent.append((chat_id, payload))
            return True

        monkeypatch.setattr(
            feishu.FeishuNotifier, "_send_to_chat", fake_send_to_chat
        )
        n = feishu.FeishuNotifier()
        n.app_id = "cli_xxx"
        n.app_secret = "secret"
        n._enabled = True
        n.register_chat(chat_id="oc_target", name="Target")
        assert n.send({"y": 2}, chat_id="oc_target") is True
        assert sent == [("oc_target", {"y": 2})]

    def test_app_bot_broadcast(self, feishu, monkeypatch):
        sent = []

        def fake_send_to_chat(self, payload, chat_id):
            sent.append(chat_id)
            return True

        monkeypatch.setattr(
            feishu.FeishuNotifier, "_send_to_chat", fake_send_to_chat
        )
        n = feishu.FeishuNotifier()
        n.app_id = "cli_xxx"
        n.app_secret = "secret"
        n._enabled = True
        n.register_chat(chat_id="oc_a", enabled=True)
        n.register_chat(chat_id="oc_b", enabled=True)
        n.register_chat(chat_id="oc_c", enabled=False)
        assert n.send({"z": 3}) is True
        # Only enabled chats get the message
        assert set(sent) == {"oc_a", "oc_b"}

    def test_app_bot_no_chats_registered(self, feishu):
        n = feishu.FeishuNotifier()
        n.app_id = "cli_xxx"
        n.app_secret = "secret"
        n._enabled = True
        # No chats registered
        assert n.send({"x": 1}) is False
