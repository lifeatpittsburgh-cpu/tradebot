"""Performance metrics for a backtest."""
from __future__ import annotations

import math
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def compute_metrics(
    equity: pd.Series,
    trades: List,
    invested: Sequence[float],
    buy_hold_return: float,
    periods_per_year: int = TRADING_DAYS,
) -> Dict[str, float]:
    """`invested` is the fraction of equity in positions each day; `buy_hold_return` is
    the passive comparison over the same period."""
    start_capital = float(equity.iloc[0]) if len(equity) else 0.0
    final = float(equity.iloc[-1]) if len(equity) else 0.0
    total_return = final / start_capital - 1.0 if start_capital else 0.0

    days = (equity.index[-1] - equity.index[0]).days if len(equity) > 1 else 0
    years = days / 365.25
    cagr = (final / start_capital) ** (1 / years) - 1.0 if years > 0 and final > 0 else 0.0

    rets = equity.pct_change().dropna()
    std = rets.std()
    sharpe = float(rets.mean() / std * math.sqrt(periods_per_year)) if std and std > 0 else 0.0

    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    gross_loss = -losses.sum()
    profit_factor = float(wins.sum() / gross_loss) if gross_loss > 0 else (math.inf if len(wins) else 0.0)

    inv = np.asarray(invested, dtype=float)
    return {
        "start_capital": start_capital,
        "final_equity": final,
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_drawdown(equity),
        "sharpe": sharpe,
        "trades": float(len(trades)),
        "win_rate": float(len(wins) / len(pnls)) if len(pnls) else 0.0,
        "profit_factor": profit_factor,
        "avg_trade_return": float(np.mean([t.return_pct for t in trades])) if trades else 0.0,
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])) if trades else 0.0,
        "exposure": float(inv.mean()) if inv.size else 0.0,
        "buy_hold_return": float(buy_hold_return),
    }
