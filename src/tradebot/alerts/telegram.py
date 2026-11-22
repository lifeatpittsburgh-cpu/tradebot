"""Telegram alerts. Create a bot with @BotFather, then set TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID in .env and `alerts.telegram: true` in config/settings.yaml.
"""
from __future__ import annotations

import os
from typing import List

import requests

from ..config import load_dotenv
from ..signals import Signal


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token, self.chat_id = token, chat_id

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        load_dotenv()
        token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise RuntimeError("Telegram alerts enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env")
        return cls(token, chat_id)

    def send(self, signals: List[Signal], events: List[str]) -> None:
        if not signals and not events:
            return
        lines = ["TradeBot signals"] + [s.summary() for s in signals] + events
        resp = requests.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": "\n".join(lines)[:4000]},
            timeout=15,
        )
        resp.raise_for_status()
