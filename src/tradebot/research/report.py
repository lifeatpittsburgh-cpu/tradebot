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


def run_sp500(settings: Dict[str, Any], strategies: List[str], since: str = "2014-01-02", seeds: int = 5,
              max_per_sector: Optional[int] = None, capital: float = 100_000) -> pd.DataFrame:
    """Each strategy on point-in-time S&P 500 members (`tradebot universe update` first).

    A stock may be bought only while it was in the index. With ~500 stocks there are usually more
    BUY signals than free slots, so who gets a slot is close to luck; each strategy is run with
    `seeds` random slot orders and the spread is reported, so differences smaller than it are noise.
    """
    import math

    import numpy as np

    from ..backtest import run_portfolio_backtest
    from ..backtest.metrics import max_drawdown
    from ..config import resolve_path
    from ..strategies import get_strategy
    from ..universe import Universe, load_cached

    folder = resolve_path(settings["data"]["cache_dir"]) / "sp500"
    if not (folder / "snapshots.csv").exists():
        raise SystemExit("No S&P 500 history yet: run `tradebot universe update` first.")
    u = Universe.load(folder / "snapshots.csv")
    data = load_cached(u.symbols, folder)
    mask = {k: u.member_mask(k, df.index) for k, df in data.items()}
    risk = RiskConfig.from_settings(settings)
    costs = settings["costs"]["us"]
    start = pd.Timestamp(since)
    spy = load_cached(["SPY"], resolve_path(settings["data"]["cache_dir"])).get("SPY")
    print(f"S&P 500 members from {since}: {len(data)} stocks with prices, {risk.max_positions} positions, "
          f"{seeds} random slot orders" + (f", max {max_per_sector} per sector" if max_per_sector else ""))
    if spy is not None:
        sp = spy["close"][spy.index >= start]
        r = sp.pct_change().dropna()
        yrs = (sp.index[-1] - sp.index[0]).days / 365.25
        print(f"  {'SPY buy & hold':14s} {(sp.iloc[-1] / sp.iloc[0]) ** (1 / yrs) - 1:+6.1%}/yr  worst drop "
              f"{max_drawdown(sp):4.0%}  Sharpe {r.mean() / r.std() * math.sqrt(252):.2f}")
    rows = []
    for name in strategies:
        cagr, dd, sh = [], [], []
        for seed in range(seeds):
            rng = np.random.default_rng(seed)
            prio = {k: pd.Series(rng.random(len(df)), index=df.index) for k, df in data.items()}
            m = run_portfolio_backtest(data, get_strategy(name), capital, risk, costs["commission"], costs["slippage"],
                                       start=start, entry_allowed=mask, priority=prio, sector_of=u.sectors(),
                                       max_per_sector=max_per_sector).metrics
            cagr.append(m["cagr"]); dd.append(m["max_drawdown"]); sh.append(m["sharpe"])
        row = dict(strategy=name, cagr=float(np.mean(cagr)), cagr_min=min(cagr), cagr_max=max(cagr),
                   maxdd=float(np.mean(dd)), worst_dd=min(dd), sharpe=float(np.mean(sh)), sharpe_sd=float(np.std(sh)))
        rows.append(row)
        print(f"  {name:14s} {row['cagr']:+6.1%}/yr (range {row['cagr_min']:+.1%} to {row['cagr_max']:+.1%})  "
              f"worst drop {row['maxdd']:4.0%}  Sharpe {row['sharpe']:.2f} ± {row['sharpe_sd']:.2f}")
    return pd.DataFrame(rows)


def cmd_research(args, settings) -> int:
    strategies = list(REGISTRY) if args.strategy == "all" else [args.strategy]
    if args.universe == "sp500":
        out = run_sp500(settings, strategies, args.since, args.seeds, args.max_per_sector)
        if args.csv:
            out.to_csv(args.csv, index=False)
        return 0
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
    sp.add_argument("--universe", choices=["watchlist", "sp500"], default="watchlist",
                    help="sp500: point-in-time S&P 500 members (run `tradebot universe update` first)")
    sp.add_argument("--since", default="2014-01-02", help="sp500 test start date")
    sp.add_argument("--seeds", type=int, default=5, help="sp500: random slot orders per strategy")
    sp.add_argument("--max-per-sector", type=int, help="sp500: sector cap")


__all__ = ["PRESETS", "run_research", "cmd_research", "add_parser", "load_universe", "summarize", "load_settings"]
