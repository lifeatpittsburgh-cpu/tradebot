"""Turn TradeBot signals into Alpaca orders, behind safety rules.

What happens on each run (normally from the daily job, after the close):

* New BUY signal, not held, no open order:
    - US stock: one bracket order, a market buy plus an attached take-profit (limit at the
      target) and stop-loss (stop at the stop price). The broker manages both exits.
    - Crypto: a market buy. Bracket orders aren't available for crypto, so the next run adds a
      protective stop-limit sell at the stop price, and sells at market once a daily high
      reaches the target.
* New SELL signal on a held symbol: cancel its open orders, then sell the whole position at market.

Safety rules (both modes):

* Kill switch: `tradebot broker halt` creates state/broker/HALT; while it exists nothing is sent.
* Daily loss limit: if account equity is down `daily_loss_limit` (default 3%) since the previous
  close, no new buys (sells and stops still go out).
* Per order: at most `max_order_value` dollars; per run: at most `max_orders_per_run` new buys.
* Only symbols in the watchlists of `broker.markets`; position count and size follow `risk:`.
* Each signal is acted on once (state/broker/<mode>_processed.json), and every order carries a
  client_order_id derived from symbol + signal date, which Alpaca refuses to accept twice.
* Live mode never submits from `run`: it writes proposals that expire after 20 hours, and only
  `tradebot broker confirm`, at an interactive terminal, sends them, one typed `yes` per order.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from ..config import QTY_STEP, market_of, resolve_path
from ..risk import RiskConfig, plan_long, round_qty
from ..signals import Signal
from .alpaca import AlpacaClient, AlpacaError, round_price, to_alpaca

PROPOSAL_MAX_AGE_HOURS = 20


@dataclass
class BrokerConfig:
    enabled: bool = False
    mode: str = "paper"
    markets: List[str] = field(default_factory=lambda: ["us", "crypto"])
    daily_loss_limit: float = 0.03
    max_order_value: float = 2000.0
    max_orders_per_run: int = 5
    crypto_stop_limit_gap: float = 0.01   # stop-limit's limit price sits this far below the stop

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "BrokerConfig":
        b = settings.get("broker", {}) or {}
        cfg = cls(**{k: b[k] for k in cls.__dataclass_fields__ if k in b})
        if cfg.mode not in ("paper", "live"):
            raise ValueError(f"broker.mode must be 'paper' or 'live', got {cfg.mode!r}")
        return cfg


@dataclass
class PlannedOrder:
    symbol: str
    side: str                 # buy | sell
    kind: str                 # entry | exit | protect | take_profit
    qty: float
    est_price: float
    signal_date: str
    reason: str
    payload: Dict[str, Any]
    cancel_ids: List[str] = field(default_factory=list)
    stop: Optional[float] = None
    target: Optional[float] = None

    def describe(self) -> str:
        text = f"{self.side.upper()} {self.qty:g} {self.symbol} ({self.kind}) ~{self.est_price:,.2f}" \
               f" = ~${self.qty * self.est_price:,.0f}"
        if self.stop:
            text += f", stop {self.stop:,.2f}"
        if self.target:
            text += f", target {self.target:,.2f}"
        return text + f"  [{self.reason}]"


@dataclass
class RunResult:
    mode: str
    planned: List[PlannedOrder] = field(default_factory=list)
    submitted: List[str] = field(default_factory=list)
    proposed: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    account: Dict[str, float] = field(default_factory=dict)

    def lines(self) -> List[str]:
        out = []
        if self.submitted:
            out += [f"Sent to Alpaca ({self.mode}): {s}" for s in self.submitted]
        if self.proposed:
            out.append(f"LIVE orders awaiting your confirmation ({len(self.proposed)}): run `tradebot broker confirm`")
            out += [f"  {p}" for p in self.proposed]
        out += self.notes + [f"!! {e}" for e in self.errors]
        return out


def _cid(mode: str, symbol: str, date: str, kind: str) -> str:
    return f"tb-{mode[0]}-{symbol}-{date}-{kind}"[:48]


class Executor:
    def __init__(self, settings: Dict[str, Any], client: AlpacaClient, state_dir=None,
                 cfg: Optional[BrokerConfig] = None) -> None:
        self.settings = settings
        self.cfg = cfg or BrokerConfig.from_settings(settings)
        if client.mode != self.cfg.mode:
            raise ValueError("client mode and broker.mode differ")
        self.client = client
        self.risk = RiskConfig.from_settings(settings)
        self.sectors = settings.get("sectors") or {}
        self.dir = Path(state_dir) if state_dir else resolve_path("state/broker")
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- state --------------------------------------------------------------------------
    def _path(self, name: str) -> Path:
        return self.dir / name

    def _load(self, name: str) -> Dict[str, Any]:
        try:
            return json.loads(self._path(name).read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self, name: str, data: Dict[str, Any]) -> None:
        tmp = self._path(name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(self._path(name))

    @property
    def halt_path(self) -> Path:
        return self._path("HALT")

    def halted(self) -> Optional[str]:
        if not self.halt_path.exists():
            return None
        return self.halt_path.read_text().strip() or "halted"

    def halt(self, reason: str) -> None:
        self.halt_path.write_text(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {reason}")

    def resume(self) -> bool:
        if self.halt_path.exists():
            self.halt_path.unlink()
            return True
        return False

    # ---- planning -----------------------------------------------------------------------
    def plan(self, signals: List[Signal], latest: Optional[Dict[str, pd.DataFrame]] = None) -> RunResult:
        res = RunResult(self.cfg.mode)
        if self.halted():
            res.notes.append(f"Broker HALTED ({self.halted()}): nothing sent. `tradebot broker resume` to re-enable.")
            return res
        acct = self.client.account()
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            res.errors.append("Alpaca reports the account is blocked from trading")
            return res
        equity, last_eq = float(acct["equity"]), float(acct.get("last_equity") or acct["equity"])
        res.account = {"equity": equity, "last_equity": last_eq, "cash": float(acct.get("cash", 0))}
        day_change = equity / last_eq - 1 if last_eq else 0.0
        buys_ok = day_change > -self.cfg.daily_loss_limit
        if not buys_ok:
            res.notes.append(f"Daily loss limit hit ({day_change:+.1%} today, limit -{self.cfg.daily_loss_limit:.0%}):"
                             " no new buys this run.")

        positions = self.client.positions()
        open_orders = self.client.open_orders()
        orders_by_sym: Dict[str, List[Dict[str, Any]]] = {}
        for o in open_orders:
            for leg in [o] + list(o.get("legs") or []):
                orders_by_sym.setdefault(o["tb_symbol"], []).append(leg)
        processed = self._load(f"{self.cfg.mode}_processed.json")
        plans = self._load(f"{self.cfg.mode}_plans.json")
        held_by_market: Dict[str, int] = {}
        for s in positions:
            held_by_market[market_of(s)] = held_by_market.get(market_of(s), 0) + 1
        new_buys = 0
        selling = set()

        for sig in signals:
            m = market_of(sig.symbol)
            if m not in self.cfg.markets or sig.action not in ("BUY", "SELL"):
                continue
            if processed.get(sig.symbol, "") >= sig.date:
                continue  # already acted on this candle's signal
            crypto = m == "crypto"
            if sig.action == "SELL":
                pos = positions.get(sig.symbol)
                if not pos:
                    processed[sig.symbol] = sig.date
                    continue
                selling.add(sig.symbol)
                res.planned.append(self._exit(sig.symbol, pos["qty"], sig.price, sig.date, "exit",
                                              f"SELL signal: {sig.reason}", orders_by_sym.get(sig.symbol, []), crypto))
                continue
            # BUY
            why = None
            if sig.symbol in positions or sig.symbol in orders_by_sym:
                why = "already held or ordered"
            elif not buys_ok:
                why = "daily loss limit"
            elif new_buys >= self.cfg.max_orders_per_run:
                why = f"max {self.cfg.max_orders_per_run} new buys per run"
            elif held_by_market.get(m, 0) >= self.risk.max_positions:
                why = f"already {held_by_market[m]} {m} positions"
            elif self._sector_full(sig.symbol, positions):
                why = f"sector cap ({self.sectors.get(sig.symbol)})"
            if why:
                res.notes.append(f"Skipped BUY {sig.symbol}: {why}")
                if why == "already held or ordered":
                    processed[sig.symbol] = sig.date
                continue
            try:
                asset = self.client.asset(sig.symbol)
            except AlpacaError as exc:
                res.notes.append(f"Skipped BUY {sig.symbol}: not available on Alpaca ({exc})")
                processed[sig.symbol] = sig.date
                continue
            if not asset.get("tradable", False):
                res.notes.append(f"Skipped BUY {sig.symbol}: Alpaca says it isn't tradable")
                processed[sig.symbol] = sig.date
                continue
            order = self._entry(sig, equity, crypto)
            if order is None:
                res.notes.append(f"Skipped BUY {sig.symbol}: position too small to size")
                processed[sig.symbol] = sig.date
                continue
            res.planned.append(order)
            new_buys += 1
            held_by_market[m] = held_by_market.get(m, 0) + 1

        # Crypto positions: protective stop if missing; take profit once a daily high reaches the target.
        for sym, pos in positions.items():
            if market_of(sym) != "crypto" or sym in selling or sym not in plans:
                continue
            p = plans[sym]
            bars = (latest or {}).get(sym)
            if p.get("target") and bars is not None and len(bars) and float(bars["high"].iloc[-1]) >= p["target"]:
                res.planned.append(self._exit(sym, pos["qty"], float(bars["close"].iloc[-1]),
                                              str(bars.index[-1].date()), "take_profit",
                                              f"daily high reached target {p['target']:,.2f}",
                                              orders_by_sym.get(sym, []), True))
                continue
            has_sell = any(o.get("side") == "sell" for o in orders_by_sym.get(sym, []))
            if not has_sell and p.get("stop"):
                stop = round_price(p["stop"], True)
                limit = round_price(p["stop"] * (1 - self.cfg.crypto_stop_limit_gap), True)
                res.planned.append(PlannedOrder(
                    sym, "sell", "protect", pos["qty"], stop, p.get("date", ""), "protective stop",
                    {"symbol": to_alpaca(sym), "qty": f"{pos['qty']:g}", "side": "sell", "type": "stop_limit",
                     "time_in_force": "gtc", "stop_price": f"{stop}", "limit_price": f"{limit}",
                     "client_order_id": _cid(self.cfg.mode, sym, p.get("date", ""), "stop")},
                    stop=stop))
        self._pending_state = (processed, plans)
        return res

    def _sector_full(self, symbol: str, positions: Dict[str, Any]) -> bool:
        cap, sector = self.risk.max_per_sector, self.sectors.get(symbol)
        return bool(cap and sector and sum(1 for s in positions if self.sectors.get(s) == sector) >= cap)

    def _entry(self, sig: Signal, equity: float, crypto: bool) -> Optional[PlannedOrder]:
        step = QTY_STEP.get(market_of(sig.symbol), 1.0)
        plan = plan_long(sig.price, float(sig.extra.get("atr", 0)), equity, self.risk, step)
        if plan is None:
            return None
        qty = round_qty(min(plan.quantity, self.cfg.max_order_value / sig.price), step)
        if qty < step:
            return None
        stop, target = round_price(plan.stop_loss, crypto), round_price(plan.target, crypto)
        payload: Dict[str, Any] = {"symbol": to_alpaca(sig.symbol), "qty": f"{qty:g}", "side": "buy",
                                   "type": "market", "time_in_force": "gtc",
                                   "client_order_id": _cid(self.cfg.mode, sig.symbol, sig.date, "buy")}
        if not crypto:
            payload.update(order_class="bracket", take_profit={"limit_price": f"{target}"},
                           stop_loss={"stop_price": f"{stop}"})
        return PlannedOrder(sig.symbol, "buy", "entry", qty, sig.price, sig.date,
                            f"BUY signal: {sig.reason}", payload, stop=stop, target=target)

    def _exit(self, symbol: str, qty: float, price: float, date: str, kind: str, reason: str,
              open_orders: List[Dict[str, Any]], crypto: bool) -> PlannedOrder:
        payload = {"symbol": to_alpaca(symbol), "qty": f"{qty:g}", "side": "sell", "type": "market",
                   "time_in_force": "gtc" if crypto else "day",
                   "client_order_id": _cid(self.cfg.mode, symbol, date, "sell")}
        return PlannedOrder(symbol, "sell", kind, qty, price, date, reason, payload,
                            cancel_ids=[o["id"] for o in open_orders if o.get("id")])

    # ---- execution ----------------------------------------------------------------------
    def run(self, signals: List[Signal], latest: Optional[Dict[str, pd.DataFrame]] = None,
            dry_run: bool = False) -> RunResult:
        res = self.plan(signals, latest)
        if dry_run or not res.planned:
            if dry_run:
                res.notes += [f"(dry run) would send: {o.describe()}" for o in res.planned]
            if not dry_run:
                self._commit_state(res.planned, sent=set())
            return res
        if self.cfg.mode == "live":
            self._save("live_proposals.json", {
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "orders": [asdict(o) for o in res.planned]})
            res.proposed = [o.describe() for o in res.planned]
            self._commit_state([], sent=set())  # nothing marked done until confirmed
            return res
        sent = set()
        for o in res.planned:
            if self._send(o, res):
                sent.add(id(o))
        self._commit_state(res.planned, sent)
        return res

    def _send(self, o: PlannedOrder, res: RunResult) -> bool:
        try:
            for oid in o.cancel_ids:
                try:
                    self.client.cancel(oid)
                except AlpacaError as exc:
                    res.notes.append(f"Could not cancel order {oid} for {o.symbol}: {exc}")
            self.client.submit(o.payload)
            res.submitted.append(o.describe())
            return True
        except AlpacaError as exc:
            res.errors.append(f"{o.symbol}: {exc}")
            return False

    def _commit_state(self, orders: List[PlannedOrder], sent: set) -> None:
        processed, plans = getattr(self, "_pending_state", (self._load(f"{self.cfg.mode}_processed.json"),
                                                             self._load(f"{self.cfg.mode}_plans.json")))
        for o in orders:
            if id(o) not in sent:
                continue  # failed orders are retried on the next run
            if o.kind in ("entry", "exit"):
                processed[o.symbol] = o.signal_date
            if o.kind == "entry":
                plans[o.symbol] = {"stop": o.stop, "target": o.target, "date": o.signal_date}
            if o.kind in ("exit", "take_profit"):
                plans.pop(o.symbol, None)
        self._save(f"{self.cfg.mode}_processed.json", processed)
        self._save(f"{self.cfg.mode}_plans.json", plans)

    # ---- live confirmation ----------------------------------------------------------------
    def confirm(self, ask: Callable[[str], str] = input, interactive: Optional[bool] = None) -> RunResult:
        """Live mode: show each proposed order and send it only after a typed `yes`."""
        res = RunResult(self.cfg.mode)
        if self.cfg.mode != "live":
            res.notes.append("Not in live mode: paper orders are sent automatically, nothing to confirm.")
            return res
        if not (sys.stdin.isatty() if interactive is None else interactive):
            res.errors.append("Live orders can only be confirmed at an interactive terminal.")
            return res
        if self.halted():
            res.notes.append(f"Broker HALTED ({self.halted()}): nothing sent.")
            return res
        data = self._load("live_proposals.json")
        if not data.get("orders"):
            res.notes.append("No live orders waiting.")
            return res
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["created"])).total_seconds() / 3600
        if age > PROPOSAL_MAX_AGE_HOURS:
            res.notes.append(f"Proposals are {age:.0f} hours old (limit {PROPOSAL_MAX_AGE_HOURS}); discarded. "
                             "They'll be recreated on the next daily run if the signals still stand.")
            self._path("live_proposals.json").unlink()
            return res
        orders = [PlannedOrder(**o) for o in data["orders"]]
        sent = set()
        for o in orders:
            answer = ask(f"LIVE (real money): {o.describe()}\nType yes to send, anything else to skip: ")
            if answer.strip().lower() == "yes" and self._send(o, res):
                sent.add(id(o))
            elif id(o) not in sent:
                res.notes.append(f"Skipped: {o.describe()}")
        self._pending_state = (self._load("live_processed.json"), self._load("live_plans.json"))
        self._commit_state(orders, sent)
        self._path("live_proposals.json").unlink()
        return res
