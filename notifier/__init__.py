"""Notifier package init."""
from .base import BaseNotifier, ConsoleNotifier, MultiNotifier  # noqa: F401
from .feishu import FeishuNotifier  # noqa: F401


def get_default_notifier() -> MultiNotifier:
    """Build the default notifier chain (feishu + console fallback)."""
    return MultiNotifier([
        FeishuNotifier(),
        ConsoleNotifier(),
    ])