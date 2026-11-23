"""Alert channels. Every channel receives the same `Alert`: a title plus lines of text.

Channels (config `alerts:` in settings.yaml):
  console   print to the terminal / log file
  macos     macOS notification banner (no setup needed)
  telegram  Telegram message (needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..signals import Signal
from .console import ConsoleNotifier
from .macos import MacNotifier
from .telegram import TelegramNotifier


@dataclass
class Alert:
    title: str
    lines: List[str] = field(default_factory=list)
    summary: str = ""  # one short line for notification banners

    def text(self) -> str:
        return "\n".join([self.title] + self.lines)


def signals_alert(signals: List[Signal], events: Optional[List[str]] = None, title: str = "TradeBot signals") -> Alert:
    actionable = [s for s in signals if s.action in ("BUY", "SELL")]
    lines = [s.summary() for s in actionable] + list(events or [])
    summary = ", ".join(f"{s.action} {s.symbol}" for s in actionable) or (f"{len(events)} paper updates" if events else "")
    return Alert(title, lines, summary)


def channels_from(settings: Dict[str, Any]) -> List[Any]:
    cfg = settings.get("alerts", {})
    channels: List[Any] = []
    if cfg.get("console", True):
        channels.append(ConsoleNotifier())
    if cfg.get("macos", True) and sys.platform == "darwin":
        channels.append(MacNotifier())
    if cfg.get("telegram"):
        channels.append(TelegramNotifier.from_env())
    return channels


def send(alert: Alert, settings: Dict[str, Any], quiet_if_empty: bool = True) -> List[Tuple[str, str]]:
    """Send to every enabled channel. Returns (channel, error) for channels that failed;
    one broken channel never stops the others."""
    if quiet_if_empty and not alert.lines:
        print("No new BUY/SELL signals or paper-account changes.")
        return []
    failures: List[Tuple[str, str]] = []
    try:
        channels = channels_from(settings)
    except Exception as exc:  # e.g. Telegram enabled but not configured
        failures.append(("setup", str(exc)))
        channels = [ConsoleNotifier()]
    for ch in channels:
        try:
            ch.send(alert)
        except Exception as exc:
            failures.append((type(ch).__name__, f"{type(exc).__name__}: {exc}"))
    for name, err in failures:
        print(f"!! alert channel {name} failed: {err}", file=sys.stderr)
    return failures


def notify(signals: List[Signal], settings: Dict[str, Any], events: Optional[List[str]] = None) -> None:
    """Send actionable (BUY/SELL) signals and paper-account events to every enabled channel."""
    send(signals_alert(signals, events), settings)


__all__ = ["Alert", "signals_alert", "channels_from", "send", "notify",
           "ConsoleNotifier", "MacNotifier", "TelegramNotifier"]
