"""Shared building blocks for the CLI and the dashboard, so both behave identically."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .config import active_markets, market_of, resolve_path
from .data import default_provider
from .data.base import DataProvider
from .paper import PaperBroker
from .risk import RiskConfig
from .scanner import signal_for_latest_bar
from .signals import Signal
from .strategies import get_strategy
from .strategies.base import Strategy

DISCLAIMER = "Signals are rule-based and for research only; they are not financial advice."


def provider_from(settings: Dict[str, Any]) -> DataProvider:
    d = settings["data"]
    return default_provider(resolve_path(d["cache_dir"]), d.get("cache_ttl_minutes", 60))


def broker_from(settings: Dict[str, Any], risk: Optional[RiskConfig] = None) -> PaperBroker:
    p = settings["paper"]
    return PaperBroker(resolve_path(p["ledger_path"]), p["starting_capital"], settings["costs"],
                       risk or RiskConfig.from_settings(settings), settings.get("sectors") or {})


def strategy_name_for(settings: Dict[str, Any], market: Optional[str] = None) -> str:
    """The configured strategy for `market` (strategy.by_market), else the default strategy.name."""
    cfg = settings["strategy"]
    return (cfg.get("by_market") or {}).get(market) or cfg["name"]


def strategy_from(settings: Dict[str, Any], name: Optional[str] = None,
                  params: Optional[Dict[str, Any]] = None, market: Optional[str] = None) -> Strategy:
    """`name` with its defaults, or the strategy configured for `market` (or the default one).
    `strategy.params` apply to the default strategy only; `params` override either."""
    cfg = settings["strategy"]
    name = name or strategy_name_for(settings, market)
    merged = dict(cfg.get("params") or {}) if name == cfg["name"] else {}
    merged.update(params or {})
    return get_strategy(name, **merged)


def strategies_label(settings: Dict[str, Any], markets: Optional[List[str]] = None) -> str:
    """e.g. "us: sma_crossover, crypto: macd_trend" (or one name when every market uses the same)."""
    markets = markets or active_markets(settings)
    names = {m: strategy_name_for(settings, m) for m in markets}
    if len(set(names.values())) == 1:
        return next(iter(names.values()))
    return ", ".join(f"{m}: {n}" for m, n in names.items())


def symbols_for(settings: Dict[str, Any], market: str = "all") -> List[str]:
    markets = active_markets(settings) if market == "all" else [market]
    return [s for m in markets for s in settings["watchlists"].get(m, [])]


def run_paper_update(
    settings: Dict[str, Any],
    broker: PaperBroker,
    symbols: List[str],
    strategy: Optional[Strategy] = None,
    provider: Optional[DataProvider] = None,
) -> Tuple[List[Signal], Dict[str, str]]:
    """One end-of-day paper-trading pass: fill yesterday's orders, check stops/targets,
    queue orders from today's signals, save the ledger. Without `strategy`, each symbol uses
    the strategy configured for its market."""
    by_market: Dict[str, Strategy] = {}

    def strat_for(symbol: str) -> Strategy:
        if strategy is not None:
            return strategy
        m = market_of(symbol)
        if m not in by_market:
            by_market[m] = strategy_from(settings, market=m)
        return by_market[m]

    provider = provider or provider_from(settings)
    start = date.today() - timedelta(days=settings["data"]["lookback_days"])
    signals: List[Signal] = []
    errors: Dict[str, str] = {}
    for symbol in symbols:
        try:
            strat = strat_for(symbol)
            df = provider.get_history(symbol, start)
            if len(df) < strat.warmup + 2:
                errors[symbol] = f"only {len(df)} bars"
                continue
            broker.sync(symbol, df)
            sig = signal_for_latest_bar(symbol, df, strat, broker.risk, broker.equity(market_of(symbol)))
            signals.append(sig)
            if sig.action == "BUY":
                broker.queue(symbol, "buy", sig.date, sig.extra["atr"], strat.name, sig.reason)
            elif sig.action == "SELL":
                broker.queue(symbol, "sell", sig.date, strategy=strat.name, reason=sig.reason)
        except Exception as exc:  # one bad ticker shouldn't stop the run
            errors[symbol] = f"{type(exc).__name__}: {exc}"
    broker.save()
    return signals, errors


# ---- tables for display ----------------------------------------------------------------
def signals_frame(signals: List[Signal]) -> pd.DataFrame:
    rows = [{
        "symbol": s.symbol, "market": s.market, "date": s.date, "action": s.action, "close": s.price,
        "stop": s.stop_loss, "target": s.target, "qty": s.quantity,
        "last_signal": s.extra.get("last_signal", ""), "last_signal_date": s.extra.get("last_signal_date", ""),
        "reason": s.reason,
    } for s in signals]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    order = {"BUY": 0, "SELL": 1, "HOLD": 2}
    return df.sort_values(["action", "symbol"], key=lambda c: c.map(order) if c.name == "action" else c) \
             .reset_index(drop=True)


def accounts_frame(broker: PaperBroker, markets: Optional[List[str]] = None) -> pd.DataFrame:
    prices = {s: p.get("last_price", p["entry_price"]) for s, p in broker.positions.items()}
    rows = []
    for market, acct in broker.ledger["accounts"].items():
        if markets and market not in markets:
            continue
        eq = broker.equity(market, prices)
        start = acct["starting_capital"]
        rows.append({"market": market, "currency": acct["currency"], "cash": acct["cash"], "equity": eq,
                     "starting_capital": start, "return": eq / start - 1 if start else 0.0,
                     "open_positions": sum(1 for p in broker.positions.values() if p["market"] == market)})
    return pd.DataFrame(rows)


def positions_frame(broker: PaperBroker) -> pd.DataFrame:
    rows = []
    for s, p in broker.positions.items():
        last = p.get("last_price", p["entry_price"])
        rows.append({"symbol": s, "market": p["market"], "qty": p["quantity"], "entry": p["entry_price"],
                     "last": last, "stop": p["stop_loss"], "target": p["target"], "since": p["entry_date"],
                     "unrealized": (last - p["entry_price"]) * p["quantity"],
                     "unrealized_pct": last / p["entry_price"] - 1})
    return pd.DataFrame(rows)


def pending_frame(broker: PaperBroker) -> pd.DataFrame:
    return pd.DataFrame([{"symbol": s, "side": o["side"].upper(), "signal_date": o["signal_date"],
                          "reason": o.get("reason", "")} for s, o in broker.pending.items()])


def history_frame(broker: PaperBroker) -> pd.DataFrame:
    return pd.DataFrame(broker.ledger["history"])
