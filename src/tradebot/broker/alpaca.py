"""Minimal Alpaca Trading API v2 client (REST, via `requests`).

Paper and live accounts use different hosts **and** different keys, read from .env:

    ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET_KEY   -> https://paper-api.alpaca.markets
    ALPACA_LIVE_KEY_ID  / ALPACA_LIVE_SECRET_KEY    -> https://api.alpaca.markets

The client never chooses the mode itself: the caller passes "paper" or "live", and live
requires the separate live keys, so paper settings can't reach a real-money account.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..config import load_dotenv

HOSTS = {"paper": "https://paper-api.alpaca.markets", "live": "https://api.alpaca.markets"}
ENV_KEYS = {"paper": ("ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY"),
            "live": ("ALPACA_LIVE_KEY_ID", "ALPACA_LIVE_SECRET_KEY")}


class AlpacaError(RuntimeError):
    pass


def to_alpaca(symbol: str) -> str:
    """TradeBot/Yahoo symbol -> Alpaca order symbol: BTC-USD -> BTC/USD, BRK-B -> BRK.B."""
    s = symbol.upper()
    if s.endswith(("-USD", "-USDT", "-USDC")):
        base, quote = s.rsplit("-", 1)
        return f"{base}/{quote}"
    return s.replace("-", ".")


def from_alpaca(symbol: str, asset_class: str = "") -> str:
    """Alpaca position/order symbol -> TradeBot symbol: BTCUSD or BTC/USD -> BTC-USD, BRK.B -> BRK-B."""
    s = symbol.upper()
    if "/" in s:
        base, quote = s.split("/", 1)
        return f"{base}-{quote}"
    if asset_class == "crypto":
        for quote in ("USDT", "USDC", "USD"):
            if s.endswith(quote):
                return f"{s[:-len(quote)]}-{quote}"
    return s.replace(".", "-")


def round_price(price: float, crypto: bool) -> float:
    """Alpaca rejects sub-penny stock prices; crypto allows finer ticks for cheap coins."""
    if not crypto or price >= 1:
        return round(price, 2)
    return float(f"{price:.6g}")


class AlpacaClient:
    def __init__(self, mode: str, key_id: str, secret: str, session=None, host: Optional[str] = None) -> None:
        if mode not in HOSTS:
            raise ValueError(f"mode must be 'paper' or 'live', got {mode!r}")
        self.mode = mode
        self.base = (host or HOSTS[mode]).rstrip("/")
        if session is None:
            import requests

            session = requests.Session()
        self.session = session
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}

    @classmethod
    def from_env(cls, mode: str, session=None) -> "AlpacaClient":
        load_dotenv()
        kid, sec = ENV_KEYS[mode]
        key_id, secret = os.environ.get(kid, "").strip(), os.environ.get(sec, "").strip()
        if not key_id or not secret:
            raise AlpacaError(f"{kid} / {sec} are not set in .env (see README: Phase 5 setup)")
        return cls(mode, key_id, secret, session)

    # ---- HTTP -------------------------------------------------------------------------
    def _call(self, method: str, path: str, **kw) -> Any:
        resp = getattr(self.session, method)(f"{self.base}{path}", headers=self.headers, timeout=20, **kw)
        if resp.status_code >= 400:  # never echo headers: they hold the keys
            try:
                msg = resp.json().get("message", resp.text)
            except ValueError:
                msg = resp.text
            raise AlpacaError(f"Alpaca {method.upper()} {path}: {resp.status_code} {str(msg)[:200]}")
        return resp.json() if resp.content else None

    # ---- reads ------------------------------------------------------------------------
    def account(self) -> Dict[str, Any]:
        return self._call("get", "/v2/account")

    def positions(self) -> Dict[str, Dict[str, Any]]:
        """{tradebot_symbol: position} with float qty / prices."""
        out = {}
        for p in self._call("get", "/v2/positions") or []:
            sym = from_alpaca(p["symbol"], p.get("asset_class", ""))
            out[sym] = {"qty": float(p["qty"]), "avg_entry": float(p["avg_entry_price"]),
                        "market_value": float(p.get("market_value") or 0),
                        "unrealized_pl": float(p.get("unrealized_pl") or 0),
                        "asset_class": p.get("asset_class", ""), "alpaca_symbol": p["symbol"]}
        return out

    def open_orders(self) -> List[Dict[str, Any]]:
        orders = self._call("get", "/v2/orders", params={"status": "open", "nested": "true", "limit": 500}) or []
        for o in orders:
            o["tb_symbol"] = from_alpaca(o["symbol"], o.get("asset_class", ""))
        return orders

    def asset(self, symbol: str) -> Dict[str, Any]:
        return self._call("get", f"/v2/assets/{to_alpaca(symbol).replace('/', '')}")

    # ---- writes -----------------------------------------------------------------------
    def submit(self, order: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("post", "/v2/orders", json=order)

    def cancel(self, order_id: str) -> None:
        self._call("delete", f"/v2/orders/{order_id}")

    def cancel_all(self) -> None:
        self._call("delete", "/v2/orders")

    def close_position(self, symbol: str) -> Dict[str, Any]:
        return self._call("delete", f"/v2/positions/{to_alpaca(symbol).replace('/', '')}")
