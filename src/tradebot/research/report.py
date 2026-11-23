"""`tradebot research`: walk-forward experiments over strategies and entry filters."""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

from ..config import is_tradable, load_settings
from ..filters import FilterConfig
from ..risk import RiskConfig
from ..strategies import REGISTRY
from .walkforward import WalkForwardResult, walk_forward

# Filter combinations compared by `--matrix`.
PRESETS: Dict[str, FilterConfig] = {
    "none": FilterConfig(),
    "regime": FilterConfig(regime=True),
    "regime+exit": FilterConfig(regime=True, regime_exit=True),
    "rs": FilterConfig(rs=True),
    "regime+rs": FilterConfig(regime=True, rs=True),
    "volume": FilterConfig(volume=True),
}


def load_universe(settings: Dict[str, Any], market: str, years: float, symbols: Optional[List[str]] = None):
    from ..services import provider_from, symbols_for

    provider = provider_from(settings)
    start = date.today() - pd.Timedelta(days=int(years * 365.25)).to_pytimedelta()
    symbols = symbols or [s for s in symbols_for(settings, market) if is_tradable(s)]
    data = {}
    for s in symbols:
        df = provider.get_history(s, start)
        if len(df) > 250:
            data[s] = df
    index_symbol = settings["backtest"]["benchmark"][market]
    index_df = data.get(index_symbol)
    if index_df is None:
        index_df = provider.get_history(index_symbol, start)
    return data, index_df["close"]


def summarize(wf: WalkForwardResult) -> Dict[str, Any]:
    o, f = wf.oos.metrics, wf.fixed.metrics
    folds = wf.folds
    return {
        "strategy": wf.strategy, "filters": wf.filters,
        "oos_return": o["total_return"], "oos_cagr": o["cagr"], "oos_maxdd": o["max_drawdown"],
        "oos_sharpe": o["sharpe"], "oos_trades": int(o["trades"]), "oos_exposure": o["exposure"],
        "fixed_return": f["total_return"], "fixed_cagr": f["cagr"], "fixed_maxdd": f["max_drawdown"],
        "fixed_sharpe": f["sharpe"],
        "train_sharpe": sum(x.train_sharpe for x in folds) / len(folds),
        "buy_hold": wf.buy_hold, "benchmark": wf.benchmark,
        "folds": len(folds), "oos_start": str(folds[0].test_start.date()),
        "blocked": int(o.get("blocked_entries", 0)),
    }


def run_research(settings: Dict[str, Any], market: str, strategies: List[str], presets: List[str],
                 years: float = 12, train_years: float = 3, test_months: int = 6,
                 csv: Optional[str] = None, verbose_folds: bool = False) -> pd.DataFrame:
    data, index_close = load_universe(settings, market, years)
    risk = RiskConfig.from_settings(settings)
    costs = settings["costs"][market]
    capital = float(settings["paper"]["starting_capital"][market])
    first = min(df.index[0] for df in data.values())
    print(f"{market}: {len(data)} symbols from {first:%Y-%m-%d}; train {train_years:g}y, test {test_months}m, "
          f"costs {costs['commission']:.2%} + {costs['slippage']:.2%} per side")
    rows = []
    for name in strategies:
        for preset in presets:
            t0 = time.time()
            wf = walk_forward(data, name, capital, risk, costs["commission"], costs["slippage"],
                              index_close=index_close, filters=replace(PRESETS[preset]),
                              train_years=train_years, test_months=test_months)
            row = summarize(wf)
            rows.append(row)
            print(f"  {name:14s} {preset:12s} out-of-sample {row['oos_return']:+8.1%} "
                  f"(CAGR {row['oos_cagr']:+6.1%}, DD {row['oos_maxdd']:6.1%}, Sharpe {row['oos_sharpe']:5.2f}, "
                  f"{row['oos_trades']} trades)  default params {row['fixed_return']:+8.1%} "
                  f"(Sharpe {row['fixed_sharpe']:5.2f})  [{time.time() - t0:.0f}s]")
            if verbose_folds:
                for fo in wf.folds:
                    print(f"      {fo.test_start:%Y-%m}..{fo.test_end:%Y-%m}  {fo.params}  "
                          f"train Sharpe {fo.train_sharpe:5.2f}  test {fo.test_return:+6.1%}")
    out = pd.DataFrame(rows)
    if rows:
        r = rows[0]
        print(f"  same period: equal-weight buy & hold {r['buy_hold']:+.1%}"
              + (f", index {r['benchmark']:+.1%}" if r["benchmark"] is not None else "")
              + f" (out-of-sample from {r['oos_start']})")
    if csv:
        out.to_csv(csv, index=False)
        print(f"Saved {csv}")
    return out


def cmd_research(args, settings) -> int:
    strategies = list(REGISTRY) if args.strategy == "all" else [args.strategy]
    presets = list(PRESETS) if args.matrix else [p.strip() for p in args.filters.split(",")]
    unknown = [p for p in presets if p not in PRESETS]
    if unknown:
        raise SystemExit(f"unknown filter preset(s): {unknown}; choose from {list(PRESETS)}")
    for market in args.market:
        run_research(settings, market, strategies, presets, args.years, args.train_years, args.test_months,
                     args.csv.replace(".csv", f"_{market}.csv") if args.csv else None, args.folds)
    return 0


def add_parser(sub) -> None:
    sp = sub.add_parser("research", help="walk-forward test strategies and entry filters (Phase 4)")
    sp.add_argument("--market", "-m", nargs="+", default=["us"], choices=["us", "crypto", "india"])
    sp.add_argument("--strategy", "-s", default="composite", choices=list(REGISTRY) + ["all"])
    sp.add_argument("--filters", default="none",
                    help=f"comma-separated presets: {', '.join(PRESETS)} (default none)")
    sp.add_argument("--matrix", action="store_true", help="compare every filter preset")
    sp.add_argument("--years", type=float, default=12, help="history to load (default 12)")
    sp.add_argument("--train-years", type=float, default=3)
    sp.add_argument("--test-months", type=int, default=6)
    sp.add_argument("--folds", action="store_true", help="print each fold's chosen parameters")
    sp.add_argument("--csv", help="save results (one file per market)")


__all__ = ["PRESETS", "run_research", "cmd_research", "add_parser", "load_universe", "summarize", "load_settings"]
