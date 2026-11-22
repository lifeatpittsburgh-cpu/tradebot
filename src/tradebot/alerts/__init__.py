"""Alert channels. `notify` sends actionable (BUY/SELL) signals to every enabled channel."""
from __future__ import annotations

from typing import Any, Dict, List

from ..signals import Signal
from .console import ConsoleNotifier
from .telegram import TelegramNotifier


def notify(signals: List[Signal], settings: Dict[str, Any], events: List[str] = None) -> None:
    actionable = [s for s in signals if s.action in ("BUY", "SELL")]
    cfg = settings.get("alerts", {})
    channels = []
    if cfg.get("console", True):
        channels.append(ConsoleNotifier())
    if cfg.get("telegram"):
        channels.append(TelegramNotifier.from_env())
    for ch in channels:
        ch.send(actionable, events or [])


__all__ = ["notify", "ConsoleNotifier", "TelegramNotifier"]
