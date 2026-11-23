"""`tradebot broker ...` commands."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from ..config import is_tradable, market_of
from ..signals import Signal
from .alpaca import AlpacaClient, AlpacaError
from .executor import BrokerConfig, Executor, RunResult


def latest_signals(settings: Dict[str, Any], markets: List[str],
                   review: Optional[Callable[[Signal], Signal]] = None) -> Tuple[List[Signal], Dict[str, pd.DataFrame]]:
    """Signals on the latest completed candle, each market with its configured strategy."""
    from ..risk import RiskConfig
    from ..scanner import signal_for_latest_bar
    from ..services import provider_from, strategy_from, symbols_for

    provider = provider_from(settings)
    risk = RiskConfig.from_settings(settings)
    start = date.today() - timedelta(days=settings["data"]["lookback_days"])
    signals, bars = [], {}
    for m in markets:
        strat = strategy_from(settings, market=m)
        for sym in symbols_for(settings, m):
            if not is_tradable(sym):
                continue
            try:
                df = provider.get_history(sym, start)
                if len(df) < strat.warmup + 2:
                    continue
                bars[sym] = df
                sig = signal_for_latest_bar(sym, df, strat, risk)
                signals.append(review(sig) if review is not None else sig)
            except Exception as exc:
                print(f"!! {sym}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return signals, bars


def make_executor(settings: Dict[str, Any], session=None) -> Executor:
    cfg = BrokerConfig.from_settings(settings)
    return Executor(settings, AlpacaClient.from_env(cfg.mode, session), cfg=cfg)


def run_broker_step(settings: Dict[str, Any], signals: Optional[List[Signal]] = None,
                    bars: Optional[Dict[str, pd.DataFrame]] = None, dry_run: bool = False,
                    review: Optional[Callable[[Signal], Signal]] = None) -> RunResult:
    ex = make_executor(settings)
    if signals is None:
        signals, bars = latest_signals(settings, ex.cfg.markets, review)
    return ex.run(signals, bars, dry_run=dry_run)


def _banner(cfg: BrokerConfig) -> str:
    return ("LIVE ACCOUNT (real money)" if cfg.mode == "live" else "paper account (simulated money)")


def cmd_broker(args, settings) -> int:
    cfg = BrokerConfig.from_settings(settings)
    try:
        ex = make_executor(settings)
    except (AlpacaError, ValueError) as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 1
    print(f"Alpaca {_banner(cfg)}" + ("" if cfg.enabled else "  [broker.enabled is false: the daily job won't use it]"))

    if args.action == "halt":
        ex.halt(args.reason or "manual halt")
        print("HALTED: TradeBot will send no orders until `tradebot broker resume`.")
        if args.close_all:
            if not sys.stdin.isatty():
                print("!! --close-all needs an interactive terminal to confirm.", file=sys.stderr)
                return 1
            positions = ex.client.positions()
            print(f"This cancels every open order and sells all {len(positions)} position(s) at market: "
                  + ", ".join(f"{p['qty']:g} {s}" for s, p in positions.items()))
            if input("Type yes to close everything: ").strip().lower() != "yes":
                print("Not closed. Positions and their stop orders are unchanged.")
                return 0
            ex.client.cancel_all()
            for s in positions:
                try:
                    ex.client.close_position(s)
                    print(f"  closing {s}")
                except AlpacaError as exc:
                    print(f"!! {s}: {exc}", file=sys.stderr)
        return 0
    if args.action == "resume":
        print("Resumed: orders will be sent again." if ex.resume() else "Wasn't halted.")
        return 0
    if args.action == "confirm":
        res = ex.confirm()
        for line in res.lines():
            print(line)
        return 1 if res.errors else 0
    if args.action == "run":
        res = run_broker_step(settings, dry_run=args.dry_run)
        for line in res.lines() or ["Nothing to do: no new signals, exits or protective orders."]:
            print(line)
        return 1 if res.errors else 0

    # status
    acct = ex.client.account()
    eq, last = float(acct["equity"]), float(acct.get("last_equity") or acct["equity"])
    print(f"Equity ${eq:,.2f} ({eq / last - 1:+.2%} today), cash ${float(acct['cash']):,.2f}, "
          f"buying power ${float(acct['buying_power']):,.2f}")
    print(f"Safety: daily loss limit -{cfg.daily_loss_limit:.0%}, max ${cfg.max_order_value:,.0f}/order, "
          f"max {cfg.max_orders_per_run} new buys/run, markets {', '.join(cfg.markets)}"
          + (f"\nHALTED: {ex.halted()}" if ex.halted() else ""))
    pos = ex.client.positions()
    if pos:
        print("\nPositions")
        for s, p in pos.items():
            print(f"  {s:<10} {p['qty']:>12g} @ {p['avg_entry']:>12,.2f}  value ${p['market_value']:>11,.2f}  "
                  f"P&L ${p['unrealized_pl']:>+10,.2f}")
    orders = ex.client.open_orders()
    if orders:
        print("\nOpen orders")
        for o in orders:
            for leg in [o] + list(o.get("legs") or []):
                px = leg.get("limit_price") or leg.get("stop_price") or "market"
                print(f"  {o['tb_symbol']:<10} {leg['side']:<4} {leg['type']:<10} qty {leg.get('qty')}  @ {px}  "
                      f"({leg['status']})")
    proposals = ex._load("live_proposals.json").get("orders", [])
    if proposals:
        print(f"\n{len(proposals)} live order(s) waiting for `tradebot broker confirm`")
    return 0


def add_parser(sub) -> None:
    sp = sub.add_parser("broker", help="Alpaca account: status, run, halt/resume, confirm live orders")
    sp.add_argument("action", choices=["status", "run", "halt", "resume", "confirm"])
    sp.add_argument("--dry-run", action="store_true", help="run: show the orders without sending them")
    sp.add_argument("--close-all", action="store_true", help="halt: also cancel orders and sell every position")
    sp.add_argument("--reason", help="halt: note stored with the kill switch")
