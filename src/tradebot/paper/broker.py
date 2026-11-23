"""Local paper-trading account stored as a JSON ledger.

Uses the same fill rules as the backtester: an order queued from bar t's signal fills at
the open of the first later bar, and open positions are checked for stop (first) and
target on each new bar. Separate cash accounts for US (USD) and India (INR).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from ..config import CURRENCY, QTY_STEP, is_tradable, market_of
from ..risk import RiskConfig, affordable_qty, plan_long


class PaperBroker:
    def __init__(self, path, starting_capital: Dict[str, float], costs: Dict[str, Dict[str, float]],
                 risk: Optional[RiskConfig] = None, sectors: Optional[Dict[str, str]] = None) -> None:
        self.path = Path(path)
        self.starting_capital = starting_capital
        self.costs = costs
        self.risk = risk or RiskConfig()
        self.sectors = sectors or {}
        self.ledger: Dict[str, Any] = self._load()
        self.events: List[str] = []  # human-readable log of what happened this run

    # ---- persistence -------------------------------------------------------------
    def _fresh(self) -> Dict[str, Any]:
        return {
            "created": datetime.now().isoformat(timespec="seconds"),
            "accounts": {m: {"cash": float(c), "starting_capital": float(c), "currency": CURRENCY[m]}
                         for m, c in self.starting_capital.items()},
            "positions": {},
            "pending": {},
            "history": [],
        }

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return self._fresh()
        ledger = json.loads(self.path.read_text())
        for m, c in self.starting_capital.items():  # markets added after the ledger was created
            ledger["accounts"].setdefault(m, {"cash": float(c), "starting_capital": float(c), "currency": CURRENCY[m]})
        return ledger

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.ledger, indent=2, default=str))

    def reset(self) -> None:
        self.ledger = self._fresh()

    # ---- queries -----------------------------------------------------------------
    @property
    def positions(self) -> Dict[str, Dict[str, Any]]:
        return self.ledger["positions"]

    @property
    def pending(self) -> Dict[str, Dict[str, Any]]:
        return self.ledger["pending"]

    def cash(self, market: str) -> float:
        return float(self.ledger["accounts"][market]["cash"])

    def equity(self, market: str, prices: Optional[Dict[str, float]] = None) -> float:
        """Cash + open positions, valued at `prices` if given, else at entry price."""
        prices = prices or {}
        value = self.cash(market)
        for sym, p in self.positions.items():
            if p["market"] == market:
                value += p["quantity"] * prices.get(sym, p["entry_price"])
        return value

    # ---- order flow --------------------------------------------------------------
    def queue(self, symbol: str, side: str, signal_date: str, atr: float = 0.0,
              strategy: str = "", reason: str = "") -> bool:
        """Queue an order to fill at the next bar's open. Returns False if it makes no sense."""
        if symbol in self.pending or not is_tradable(symbol):
            return False
        if side == "buy" and symbol in self.positions:
            return False
        if side == "sell" and symbol not in self.positions:
            return False
        self.pending[symbol] = {"side": side, "signal_date": signal_date, "atr": atr,
                                "strategy": strategy, "reason": reason}
        self.events.append(f"QUEUED {side.upper()} {symbol} (signal {signal_date}) {reason}")
        return True

    def sync(self, symbol: str, df: pd.DataFrame) -> None:
        """Replay bars since the last update: fill pending orders and check stops/targets."""
        market = market_of(symbol)
        cost = self.costs.get(market, {})
        commission, slippage = cost.get("commission", 0.0), cost.get("slippage", 0.0)

        for ts, bar in df.iterrows():
            day = ts.strftime("%Y-%m-%d")
            order = self.pending.get(symbol)
            if order and day > order["signal_date"]:
                if order["side"] == "buy":
                    self._fill_buy(symbol, market, day, bar["open"], order, commission, slippage)
                else:
                    self._close(symbol, day, bar["open"], "signal", commission, slippage)
                del self.pending[symbol]

            pos = self.positions.get(symbol)
            if pos and day > pos["last_checked"]:
                if bar["low"] <= pos["stop_loss"]:
                    self._close(symbol, day, min(bar["open"], pos["stop_loss"]), "stop", commission, slippage)
                elif bar["high"] >= pos["target"]:
                    self._close(symbol, day, max(bar["open"], pos["target"]), "target", commission, slippage)
                else:
                    pos["last_checked"] = day
                    pos["last_price"] = float(bar["close"])

    def _fill_buy(self, symbol, market, day, open_px, order, commission, slippage) -> None:
        held = sum(1 for p in self.positions.values() if p["market"] == market)
        if held >= self.risk.max_positions:
            self.events.append(f"SKIPPED BUY {symbol}: already holding {held} positions in {market}")
            return
        sector = self.sectors.get(symbol)
        if self.risk.max_per_sector and sector:
            in_sector = sum(1 for s in self.positions if self.sectors.get(s) == sector)
            if in_sector >= self.risk.max_per_sector:
                self.events.append(f"SKIPPED BUY {symbol}: already holding {in_sector} {sector} positions")
                return
        fill = float(open_px) * (1 + slippage)
        step = QTY_STEP.get(market, 1.0)
        plan = plan_long(fill, float(order["atr"]), self.equity(market), self.risk, step)
        if plan is None:
            self.events.append(f"SKIPPED BUY {symbol}: could not size position")
            return
        cash = self.cash(market)
        qty = affordable_qty(plan.quantity, fill, cash, commission, step)
        if qty < step:
            self.events.append(f"SKIPPED BUY {symbol}: not enough cash")
            return
        total = fill * qty * (1 + commission)
        self.ledger["accounts"][market]["cash"] = cash - total
        self.positions[symbol] = {
            "market": market, "quantity": qty, "entry_price": fill, "entry_date": day,
            "cost": total, "stop_loss": plan.stop_loss, "target": plan.target,
            "strategy": order.get("strategy", ""), "last_checked": day, "last_price": fill,
        }
        self.events.append(f"BOUGHT {qty:g} {symbol} @ {fill:,.2f} on {day} "
                           f"(stop {plan.stop_loss:,.2f}, target {plan.target:,.2f})")

    def _close(self, symbol, day, raw_px, reason, commission, slippage) -> None:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return
        px = float(raw_px) * (1 - slippage)
        proceeds = px * pos["quantity"] * (1 - commission)
        self.ledger["accounts"][pos["market"]]["cash"] += proceeds
        pnl = proceeds - pos["cost"]
        self.ledger["history"].append({
            **{k: pos[k] for k in ("market", "quantity", "entry_price", "entry_date", "strategy")},
            "symbol": symbol, "exit_price": px, "exit_date": day, "exit_reason": reason,
            "pnl": pnl, "return_pct": pnl / pos["cost"] if pos["cost"] else 0.0,
        })
        self.events.append(f"SOLD {pos['quantity']:g} {symbol} @ {px:,.2f} on {day} ({reason}), P&L {pnl:+,.2f}")
