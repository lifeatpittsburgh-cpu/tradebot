"""Walk-forward testing: tune on the past, judge on data the tuning never saw.

    |---- train (3y) ----|- test (6m) -|
          |---- train (3y) ----|- test (6m) -|
                |---- train (3y) ----|- test (6m) -| ...

For each fold, every parameter combination is backtested on the training window and the best
(by Sharpe ratio) is kept. Its signals on the following test window are joined across folds and
replayed as one continuous out-of-sample backtest. That track record is the honest estimate of
how the tuned strategy would have done. The gap between training and test results shows how much
of the tuning was fitting noise.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from ..backtest import run_portfolio_backtest
from ..backtest.engine import BacktestResult
from ..filters import FilterConfig
from ..risk import RiskConfig
from ..strategies import get_strategy

# Small, sensible grids: enough to tune, few enough not to overfit 3 years of data.
DEFAULT_GRIDS: Dict[str, Dict[str, List[Any]]] = {
    "sma_crossover": {"fast": [10, 20, 50], "slow": [50, 100, 200]},
    "rsi_reversion": {"oversold": [25, 30, 35], "exit_level": [55, 65, 75]},
    "macd_trend": {"fast": [8, 12], "slow": [21, 26], "trend": [50, 100, 200]},
    "composite": {"min_votes": [1, 2, 3], "exit_votes": [1, 2]},
}


def _valid(name: str, params: Dict[str, Any]) -> bool:
    if name == "sma_crossover":
        return params["fast"] < params["slow"]
    if name == "macd_trend":
        return params["fast"] < params["slow"]
    if name == "composite":
        return params["exit_votes"] <= params["min_votes"]
    return True


def param_grid(name: str, grid: Optional[Dict[str, List[Any]]] = None) -> List[Dict[str, Any]]:
    grid = grid if grid is not None else DEFAULT_GRIDS.get(name, {})
    keys = list(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))] or [{}]
    return [c for c in combos if _valid(name, c)]


@dataclass
class Fold:
    train_start: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    params: Dict[str, Any]
    train_sharpe: float
    train_return: float
    test_return: float = 0.0


@dataclass
class WalkForwardResult:
    strategy: str
    filters: str
    folds: List[Fold]
    oos: BacktestResult                 # continuous out-of-sample backtest, tuned params per fold
    fixed: BacktestResult               # same period, default params, no tuning
    buy_hold: float                     # equal-weight buy & hold over the out-of-sample period
    benchmark: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def _score(res: BacktestResult) -> float:
    m = res.metrics
    return m["sharpe"] if m["trades"] >= 3 else -99.0  # a near-empty run isn't evidence


def _fold_dates(first: pd.Timestamp, last: pd.Timestamp, train_years: float,
                test_months: int) -> List[Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    folds = []
    train = pd.DateOffset(months=int(round(train_years * 12)))
    test_start = first + train
    while test_start < last:
        test_end = min(test_start + pd.DateOffset(months=test_months) - pd.Timedelta(days=1), last)
        train_start = test_start - train
        folds.append((train_start, test_start, test_end))
        test_start = test_end + pd.Timedelta(days=1)
    return folds


def walk_forward(
    data: Dict[str, pd.DataFrame],
    strategy_name: str,
    capital: float,
    risk: RiskConfig,
    commission: float,
    slippage: float,
    index_close: Optional[pd.Series] = None,
    filters: Optional[FilterConfig] = None,
    grid: Optional[Dict[str, List[Any]]] = None,
    train_years: float = 3.0,
    test_months: int = 6,
    first_date: Optional[pd.Timestamp] = None,
    max_positions: Optional[int] = None,
) -> WalkForwardResult:
    filters = filters or FilterConfig()
    combos = param_grid(strategy_name, grid)
    strategies = {i: get_strategy(strategy_name, **p) for i, p in enumerate(combos)}
    warmup = max(s.warmup for s in strategies.values())

    all_dates = sorted(set().union(*(df.index for df in data.values())))
    # Training can start once the slowest indicator has warmed up on the earliest symbol.
    first = pd.Timestamp(first_date) if first_date is not None else pd.Timestamp(all_dates[min(warmup, len(all_dates) - 1)])
    last = pd.Timestamp(all_dates[-1])
    entry_allowed, force_exit = filters.masks(data, index_close)
    common = dict(capital=capital, risk=risk, commission=commission, slippage=slippage,
                  max_positions=max_positions, entry_allowed=entry_allowed, force_exit=force_exit)

    # Signals for every combination over the full history, computed once.
    full_signals = {i: {s: strat.generate(df)["signal"] for s, df in data.items()} for i, strat in strategies.items()}

    folds: List[Fold] = []
    stitched: Dict[str, List[pd.Series]] = {s: [] for s in data}
    for train_start, test_start, test_end in _fold_dates(first, last, train_years, test_months):
        train_data = {s: df[df.index < test_start] for s, df in data.items()}
        train_data = {s: df for s, df in train_data.items() if len(df) > warmup + 1 and df.index[-1] >= train_start}
        best_i, best, best_res = 0, -1e9, None
        for i, strat in strategies.items():
            if not train_data:
                break
            res = run_portfolio_backtest(train_data, strat, start=train_start, **common)
            sc = _score(res)
            if sc > best:
                best_i, best, best_res = i, sc, res
        folds.append(Fold(train_start, test_start, test_end, combos[best_i],
                          best_res.metrics["sharpe"] if best_res else 0.0,
                          best_res.metrics["total_return"] if best_res else 0.0))
        for s in data:
            sig = full_signals[best_i][s]
            stitched[s].append(sig[(sig.index >= test_start) & (sig.index <= test_end)])

    if not folds:
        raise ValueError("not enough history for one training window plus a test window")
    oos_start = folds[0].test_start
    oos_signals = {s: (pd.concat(parts) if parts else pd.Series(dtype=int)) for s, parts in stitched.items()}
    any_strat = strategies[0]
    oos = run_portfolio_backtest(data, any_strat, start=oos_start, signals=oos_signals, **common)
    default = get_strategy(strategy_name)
    fixed = run_portfolio_backtest(data, default, start=oos_start, **common)

    for f in folds:  # each fold's slice of the continuous out-of-sample equity curve
        eq = oos.equity[(oos.equity.index >= f.test_start) & (oos.equity.index <= f.test_end)]
        prev = oos.equity[oos.equity.index < f.test_start]
        base = prev.iloc[-1] if len(prev) else capital
        f.test_return = float(eq.iloc[-1] / base - 1) if len(eq) else 0.0

    bench = None
    if index_close is not None and not index_close.empty:
        bm = index_close[index_close.index >= oos_start]
        bench = float(bm.iloc[-1] / bm.iloc[0] - 1) if len(bm) > 1 else None
    return WalkForwardResult(strategy_name, filters.label(), folds, oos, fixed,
                             oos.metrics["buy_hold_return"], bench)
