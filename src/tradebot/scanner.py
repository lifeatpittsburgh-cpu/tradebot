"""Run a strategy across a watchlist and turn the latest bar into a Signal."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from . import indicators as ind
from .config import QTY_STEP, market_of
from .data.base import DataProvider
from .risk import RiskConfig, plan_long
from .signals import ACTION_NAMES, Signal
from .strategies.base import Strategy


def signal_for_latest_bar(
    symbol: str,
    df: pd.DataFrame,
    strategy: Strategy,
    risk: RiskConfig,
    equity: Optional[float] = None,
) -> Signal:
    """Build a Signal from the strategy's output on the most recent bar."""
    sigs = strategy.generate(df)
    last = sigs.iloc[-1]
    ts = df.index[-1]
    close = float(df["close"].iloc[-1])
    atr_value = float(ind.atr(df, risk.atr_period).iloc[-1])
    action = ACTION_NAMES[int(last["signal"])]

    signal = Signal(
        symbol=symbol,
        market=market_of(symbol),
        date=ts.strftime("%Y-%m-%d"),
        action=action,
        price=round(close, 4),
        strategy=strategy.name,
        reason=str(last["reason"]),
        extra={"atr": round(atr_value, 4)},
    )

    if action == "BUY":
        step = QTY_STEP.get(market_of(symbol), 1.0)
        plan = plan_long(close, atr_value, equity or 0.0, risk, step) if equity else None
        if plan is not None:
            signal.stop_loss, signal.target, signal.quantity = plan.stop_loss, plan.target, plan.quantity
        else:  # no account size given: still report the levels
            stop = close - risk.atr_stop_mult * atr_value
            signal.stop_loss = round(stop, 4)
            signal.target = round(close + risk.reward_risk * (close - stop), 4)

    nonzero = sigs[sigs["signal"] != 0]
    if not nonzero.empty:
        signal.extra["last_signal"] = ACTION_NAMES[int(nonzero["signal"].iloc[-1])]
        signal.extra["last_signal_date"] = nonzero.index[-1].strftime("%Y-%m-%d")
    return signal


def scan(
    symbols: Iterable[str],
    provider: DataProvider,
    strategy: Strategy,
    risk: RiskConfig,
    lookback_days: int = 500,
    equity_by_market: Optional[Dict[str, float]] = None,
) -> Tuple[List[Signal], Dict[str, str]]:
    """Return (signals, errors). One Signal per symbol, HOLD included."""
    start = date.today() - timedelta(days=lookback_days)
    signals: List[Signal] = []
    errors: Dict[str, str] = {}
    for symbol in symbols:
        try:
            df = provider.get_history(symbol, start)
            if len(df) < strategy.warmup + 2:
                errors[symbol] = f"only {len(df)} bars, need {strategy.warmup + 2}"
                continue
            equity = (equity_by_market or {}).get(market_of(symbol))
            signals.append(signal_for_latest_bar(symbol, df, strategy, risk, equity))
        except Exception as exc:  # one bad ticker shouldn't stop the scan
            errors[symbol] = f"{type(exc).__name__}: {exc}"
    return signals, errors
