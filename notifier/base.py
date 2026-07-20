"""
Notifier base + console fallback.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from loguru import logger


class BaseNotifier(ABC):
    """Abstract notifier."""

    name: str = "base"

    @abstractmethod
    def send(self, payload: dict[str, Any]) -> bool:
        """Send a payload. Return True on success."""


class ConsoleNotifier(BaseNotifier):
    """Print payload to stdout. Always-available fallback."""

    name = "console"

    def send(self, payload: dict[str, Any]) -> bool:
        try:
            if payload.get("msg_type") == "interactive":
                card = payload.get("card", {})
                title = card.get("header", {}).get("title", {}).get("content", "")
                print(f"\n{'='*60}")
                print(f"[Feishu Card] {title}")
                print(f"{'='*60}")
                for el in card.get("elements", []):
                    text = el.get("text", {}).get("content", "")
                    if text:
                        for line in text.split("\n"):
                            print(f"  {line}")
                print(f"{'='*60}\n")
            elif "markdown" in payload:
                print(f"\n{'='*60}")
                print("[Markdown Report]")
                print(f"{'='*60}")
                print(payload["markdown"])
                print(f"{'='*60}\n")
            else:
                print(f"[Notifier] payload: {json.dumps(payload, ensure_ascii=False)[:500]}")
            return True
        except Exception as e:
            logger.error(f"ConsoleNotifier failed: {e}")
            return False


class MultiNotifier(BaseNotifier):
    """Send to multiple notifiers."""

    name = "multi"

    def __init__(self, notifiers: list[BaseNotifier]):
        self.notifiers = notifiers

    def send(self, payload: dict[str, Any]) -> bool:
        results = []
        for n in self.notifiers:
            try:
                ok = n.send(payload)
                results.append((n.name, ok))
            except Exception as e:
                logger.error(f"Notifier {n.name} crashed: {e}")
                results.append((n.name, False))
        any_ok = any(r[1] for r in results)
        logger.info(f"MultiNotifier: {results} -> {'OK' if any_ok else 'FAIL'}")
        return any_ok