"""Telegram alerts.

Setup (once):
  1. In Telegram, message @BotFather, send /newbot and follow the prompts. It gives you a token.
  2. Put it in .env as TELEGRAM_BOT_TOKEN=... (copy .env.example to .env first).
  3. Open your new bot in Telegram and send it any message (e.g. "hi").
  4. Run `tradebot alerts telegram-chat-id`; copy the printed TELEGRAM_CHAT_ID line into .env.
  5. Set `alerts.telegram: true` in config/settings.yaml and run `tradebot alerts test`.
"""
from __future__ import annotations

import os
from typing import Dict, List

import requests

from ..config import load_dotenv

API = "https://api.telegram.org/bot{token}/{method}"


def _token_from_env() -> str:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env (see `tradebot alerts --help`)")
    return token


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token, self.chat_id = token, chat_id

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        token = _token_from_env()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not chat_id:
            raise RuntimeError("TELEGRAM_CHAT_ID is not set in .env; run `tradebot alerts telegram-chat-id`")
        return cls(token, chat_id)

    def send(self, alert) -> None:
        resp = requests.post(API.format(token=self.token, method="sendMessage"),
                             json={"chat_id": self.chat_id, "text": alert.text()[:4000]}, timeout=15)
        if not resp.ok:  # don't echo the URL: it contains the token
            raise RuntimeError(f"Telegram API error {resp.status_code}: {resp.text[:200]}")


def find_chat_ids() -> List[Dict[str, str]]:
    """Chats that have messaged the bot recently (Telegram keeps updates for about 24 hours)."""
    resp = requests.get(API.format(token=_token_from_env(), method="getUpdates"), timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Telegram API error {resp.status_code}: {resp.text[:200]}")
    chats: Dict[str, Dict[str, str]] = {}
    for upd in resp.json().get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            chats[str(chat["id"])] = {"id": str(chat["id"]), "name": name or chat.get("username", ""),
                                      "type": chat.get("type", "")}
    return list(chats.values())
