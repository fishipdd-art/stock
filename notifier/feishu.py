"""
Feishu (Lark) notifier.

Two delivery paths:
  1. **Webhook** (``feishu_webhook_url``) — simplest. One URL = one chat.
  2. **App bot** (``feishu_app_id`` + ``feishu_app_secret``) — supports
     multi-chat routing via a persistent registry of ``chat_id``s.

When app-bot mode is active, ``register_chat()`` adds an entry to the
``feishu_chats`` table. ``send(payload, chat_id=None)`` delivers to:
  - the given ``chat_id`` if specified
  - all chats with ``enabled=True`` if not specified

The registry persists in SQLite, so chat_ids survive restarts and can be
inspected via ``/api/feishu/chats``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

import httpx
from loguru import logger
from sqlalchemy import select

from config.settings import settings
from storage import get_db
from storage.models import Base, FeishuChat
from .base import BaseNotifier


# Feishu open API endpoints
_FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_FEISHU_SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"


def ensure_feishu_schema() -> None:
    """Create the feishu_chats table if it doesn't exist (idempotent).

    We register the model only when the notifier module is imported, so
    the table isn't created on plain DB imports. Idempotent: Base.metadata
    is shared, so this is a no-op if other code has already done it.
    """
    db = get_db()
    Base.metadata.create_all(db.engine, tables=[FeishuChat.__table__])


@dataclass
class ChatInfo:
    """Plain-data view of a registered Feishu chat."""
    id: int
    chat_id: str
    name: str
    chat_type: str
    enabled: bool
    created_at: Optional[datetime]
    last_sent_at: Optional[datetime]

    @classmethod
    def from_row(cls, row: FeishuChat) -> "ChatInfo":
        return cls(
            id=row.id,
            chat_id=row.chat_id,
            name=row.name,
            chat_type=row.chat_type,
            enabled=row.enabled,
            created_at=row.created_at,
            last_sent_at=row.last_sent_at,
        )


class FeishuNotifier(BaseNotifier):
    """Send to Feishu via webhook or app bot."""

    name = "feishu"

    def __init__(self):
        self.webhook = settings.feishu_webhook_url
        self.app_id = settings.feishu_app_id
        self.app_secret = settings.feishu_app_secret
        self._token: str | None = None
        self._token_expire: float = 0
        self._enabled = settings.feishu_enabled
        # Make sure the registry table exists when the notifier is built.
        try:
            ensure_feishu_schema()
        except Exception as e:
            logger.debug(f"Feishu schema ensure skipped: {e}")

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @property
    def has_webhook(self) -> bool:
        return bool(self.webhook)

    @property
    def has_app_creds(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def is_configured(self) -> bool:
        """True if any delivery path is available."""
        return self.has_webhook or self.has_app_creds

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _get_token(self) -> str | None:
        """Fetch tenant_access_token from Feishu open API."""
        if not self.has_app_creds:
            return None
        if self._token and time.time() < self._token_expire - 60:
            return self._token
        try:
            with httpx.Client(timeout=10) as c:
                resp = c.post(_FEISHU_TOKEN_URL, json={
                    "app_id": self.app_id,
                    "app_secret": self.app_secret,
                })
                data = resp.json()
                if data.get("code") == 0:
                    self._token = data["tenant_access_token"]
                    self._token_expire = time.time() + int(data.get("expire", 7200))
                    return self._token
                logger.error(f"Feishu token error: {data}")
        except Exception as e:
            logger.error(f"Feishu token fetch failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Chat registry CRUD
    # ------------------------------------------------------------------

    def register_chat(
        self,
        chat_id: str,
        name: str = "",
        chat_type: str = "group",
        enabled: bool = True,
    ) -> ChatInfo:
        """Register a chat for future sends. Idempotent on chat_id.

        If the chat_id already exists, updates name / type / enabled.
        Returns the resulting ChatInfo.
        """
        db = get_db()
        with db.tx() as s:
            row = s.query(FeishuChat).filter(FeishuChat.chat_id == chat_id).first()
            if row is None:
                row = FeishuChat(
                    chat_id=chat_id,
                    name=name or chat_id,
                    chat_type=chat_type,
                    enabled=enabled,
                )
                s.add(row)
            else:
                if name:
                    row.name = name
                row.chat_type = chat_type
                row.enabled = enabled
            s.flush()
            s.refresh(row)
            return ChatInfo.from_row(row)

    def list_chats(self, enabled_only: bool = False) -> list[ChatInfo]:
        db = get_db()
        with db.session() as s:
            q = s.query(FeishuChat)
            if enabled_only:
                q = q.filter(FeishuChat.enabled == True)
            return [ChatInfo.from_row(r) for r in q.order_by(FeishuChat.id).all()]

    def remove_chat(self, chat_id: str) -> bool:
        db = get_db()
        with db.tx() as s:
            n = s.query(FeishuChat).filter(FeishuChat.chat_id == chat_id).delete()
        return n > 0

    def set_chat_enabled(self, chat_id: str, enabled: bool) -> bool:
        db = get_db()
        with db.tx() as s:
            row = s.query(FeishuChat).filter(FeishuChat.chat_id == chat_id).first()
            if row is None:
                return False
            row.enabled = enabled
        return True

    # ------------------------------------------------------------------
    # Public send entry
    # ------------------------------------------------------------------

    def send(self, payload: dict[str, Any], chat_id: str | None = None) -> bool:
        """Send payload to Feishu.

        Args:
            payload: card / message payload.
            chat_id: target chat. If None, broadcasts to all enabled chats
                     (app-bot) or to the single configured webhook.

        Returns:
            True if at least one delivery succeeded.
        """
        if not self._enabled:
            logger.warning(
                "[Feishu] Disabled (FEISHU_ENABLED=false). Skipping."
            )
            return False
        if not self.is_configured():
            logger.warning(
                "[Feishu] No credentials. Set feishu_webhook_url OR "
                "(feishu_app_id + feishu_app_secret)."
            )
            return False

        # Webhook path: single destination.
        if self.has_webhook and chat_id is None:
            return self._send_webhook(payload)

        # App-bot path: route to specific chat(s).
        if self.has_app_creds:
            if chat_id is not None:
                return self._send_to_chat(payload, chat_id)
            # Broadcast to all enabled chats
            chats = self.list_chats(enabled_only=True)
            if not chats:
                logger.warning(
                    "[Feishu] No chats registered. Use register_chat() first."
                )
                return False
            ok = False
            for c in chats:
                if self._send_to_chat(payload, c.chat_id):
                    ok = True
            return ok

        # Webhook path with explicit chat_id is invalid (webhook != chat_id).
        if chat_id is not None and self.has_webhook and not self.has_app_creds:
            logger.warning(
                f"[Feishu] chat_id={chat_id!r} given but only webhook configured; "
                "ignoring chat_id and using webhook."
            )
            return self._send_webhook(payload)

        return False

    def send_with_retry(
        self,
        payload: dict[str, Any],
        chat_id: str | None = None,
        attempts: int = 3,
    ) -> bool:
        """Retry transient delivery failures with bounded backoff."""
        for attempt in range(max(1, int(attempts))):
            if self.send(payload, chat_id=chat_id):
                return True
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 4))
        return False

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------

    def _send_webhook(self, payload: dict[str, Any]) -> bool:
        try:
            with httpx.Client(timeout=15) as c:
                resp = c.post(self.webhook, json=payload)
                ok = resp.status_code == 200
                if not ok:
                    logger.error(
                        f"Feishu webhook failed: {resp.status_code} {resp.text[:200]}"
                    )
                else:
                    logger.info(
                        f"Feishu webhook sent OK ({resp.json().get('StatusMessage', '')})"
                    )
                return ok
        except Exception as e:
            logger.error(f"Feishu webhook error: {e}")
            return False

    # ------------------------------------------------------------------
    # App bot
    # ------------------------------------------------------------------

    def _send_to_chat(self, payload: dict[str, Any], chat_id: str) -> bool:
        """Send to a specific chat via app bot (Feishu im/v1/messages)."""
        token = self._get_token()
        if not token:
            return False

        # Feishu's send-message API expects:
        #   { receive_id_type: "chat_id", content: {text: "..."}, msg_type: "text" }
        # or for interactive cards:
        #   { receive_id_type: "chat_id", content: <json-string>, msg_type: "interactive" }
        msg_type = "interactive" if payload.get("msg_type") == "interactive" else "text"
        if msg_type == "interactive":
            content = json.dumps(payload.get("card", payload), ensure_ascii=False)
        else:
            # text fallback: collapse card to a brief summary
            text = payload.get("text", {}).get("text", "") or json.dumps(
                payload, ensure_ascii=False
            )[:2000]
            content = json.dumps({"text": text}, ensure_ascii=False)

        body = {
            "receive_id": chat_id,
            "receive_id_type": "chat_id",
            "msg_type": msg_type,
            "content": content,
        }

        try:
            with httpx.Client(timeout=15) as c:
                resp = c.post(
                    _FEISHU_SEND_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=body,
                )
                data = resp.json()
                if resp.status_code == 200 and data.get("code") == 0:
                    logger.info(f"Feishu app bot sent to chat {chat_id}")
                    self._touch_chat(chat_id)
                    return True
                logger.error(
                    f"Feishu send to {chat_id} failed: "
                    f"status={resp.status_code} body={str(data)[:300]}"
                )
                return False
        except Exception as e:
            logger.error(f"Feishu send to {chat_id} error: {e}")
            return False

    def _touch_chat(self, chat_id: str) -> None:
        """Update last_sent_at on successful delivery."""
        try:
            db = get_db()
            with db.tx() as s:
                row = s.query(FeishuChat).filter(FeishuChat.chat_id == chat_id).first()
                if row:
                    row.last_sent_at = datetime.utcnow()
        except Exception as e:
            logger.debug(f"_touch_chat({chat_id}) failed: {e}")


# json is only needed by the class above; import locally to keep top clean
import json  # noqa: E402
