"""
Feishu bot — receive IM messages via WebSocket (长连接).

Uses Feishu's native WebSocket subscription (default mode in developer
console) so that no public HTTP callback URL is needed — the server only
needs outbound internet access.

Flow:
  1. SDK establishes a WebSocket connection to Feishu's event bus
  2. When a user messages the bot, Feishu pushes the event over this socket
  3. We route it to the same _handle_query logic as the HTTP endpoint
"""
from __future__ import annotations

import threading
from typing import Any

from loguru import logger

from config.settings import settings
from web.feishu_bot import _handle_query, _register_chat

# ---------------------------------------------------------------------------
# Feishu Python SDK event handler
# ---------------------------------------------------------------------------

# We import inside the start function so the module can be safely imported
# even if lark_oapi is not installed (graceful fallback).

_ws_client: Any = None
_ws_thread: threading.Thread | None = None


def _on_message(data: Any) -> None:
    """Callback when Feishu pushes an im.message.receive_v1 event."""
    try:
        event = data.event
        msg = event.message
        if not msg:
            return

        chat_id = msg.chat_id or ""
        msg_type = msg.message_type or ""

        # Only handle text messages
        if msg_type != "text":
            return

        content_raw = msg.content or "{}"
        import json
        try:
            content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
            user_text = (content.get("text") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            user_text = str(content_raw).strip()

        if not user_text:
            return

        sender_id = ""
        if event.sender:
            sender_id = (event.sender.sender_id or {}).get("open_id", "") if hasattr(event.sender, "sender_id") else ""

        logger.info(f"[FeishuWS] from={sender_id} chat={chat_id} text={user_text[:80]}")
        _register_chat(chat_id)
        _handle_query(chat_id, user_text)

    except Exception as e:
        logger.error(f"[FeishuWS] Error handling message: {e}")


def start_ws_client() -> bool:
    """Start the Feishu WebSocket client in a daemon thread.

    Returns True if the client was started, False if credentials are missing
    or the SDK is not installed.
    """
    global _ws_client, _ws_thread

    if not settings.feishu_app_id or not settings.feishu_app_secret:
        logger.info("[FeishuWS] No credentials, skipping WebSocket client")
        return False

    if not settings.feishu_enabled:
        logger.info("[FeishuWS] Disabled (FEISHU_ENABLED=false), skipping")
        return False

    try:
        import lark_oapi as lark
    except ImportError:
        logger.warning("[FeishuWS] lark-oapi not installed. Install with: pip install lark-oapi")
        return False

    try:
        # Build event handler
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .build()
        )

        # Create WS client
        _ws_client = lark.ws.Client(
            settings.feishu_app_id,
            settings.feishu_app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.ERROR,
        )

        # Start in a daemon thread so it doesn't block shutdown
        def _run():
            try:
                logger.info("[FeishuWS] Connecting to Feishu WebSocket...")
                _ws_client.start()
            except Exception as e:
                logger.error(f"[FeishuWS] Connection failed: {e}")

        _ws_thread = threading.Thread(target=_run, daemon=True, name="feishu-ws")
        _ws_thread.start()
        logger.info("[FeishuWS] Client thread started")
        return True

    except Exception as e:
        logger.error(f"[FeishuWS] Failed to start: {e}")
        return False


def stop_ws_client() -> None:
    """Stop the Feishu WebSocket client."""
    global _ws_client
    if _ws_client is not None:
        try:
            _ws_client.stop()
        except Exception:
            pass
        _ws_client = None
        logger.info("[FeishuWS] Client stopped")
