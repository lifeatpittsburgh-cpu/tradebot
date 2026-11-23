"""Fundamentals screen: rank stocks on quality, growth, value and balance-sheet strength.

    tradebot screen                      # S&P 500, top 25 overall
    tradebot screen --sector "Health Care" --top 10
    tradebot screen --universe watchlist

Data comes from Yahoo Finance's current company figures (cached for 7 days). Each stock is
scored against the others **in its own sector**, so a bank isn't compared with a software company:

  quality  return on equity, profit margin
  growth   revenue growth, earnings growth (latest year over year)
  value    forward earnings yield (1 / forward P/E), falling back to trailing
  safety   low debt-to-equity (not used for Financials and Real Estate, where debt is the business)

Hard screens: profitable (positive trailing EPS) and positive return on equity.

This is a starting point for a long-term watchlist, not a trading signal. Yahoo only has
today's figures, so the screen can't be backtested without look-ahead bias.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

FIELDS = ["shortName", "sector", "marketCap", "trailingPE", "forwardPE", "trailingEps", "returnOnEquity",
          "profitMargins", "revenueGrowth", "earningsGrowth", "debtToEquity", "dividendYield"]
CACHE_DAYS = 7
NO_SAFETY = {"Financials", "Financial Services", "Real Estate"}


def _cache_path(cache_dir: Path, symbol: str) -> Path:
    return cache_dir / f"{symbol.replace('^', 'IDX_').replace('/', '_')}.json"


def fetch_one(symbol: str, cache_dir: Path, refresh: bool = False) -> Dict[str, Any]:
    path = _cache_path(cache_dir, symbol)
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < CACHE_DAYS * 86400:
        return json.loads(path.read_text())
    import yfinance as yf

    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:  # network error, unknown ticker, rate limit
        return {"symbol": symbol, "error": f"{type(exc).__name__}"}
    row = {"symbol": symbol, **{k: info.get(k) for k in FIELDS}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row))
    return row


def fetch_many(symbols: Iterable[str], cache_dir, refresh: bool = False, workers: int = 6) -> pd.DataFrame:
    cache_dir = Path(cache_dir)
    symbols = list(symbols)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda s: fetch_one(s, cache_dir, refresh), symbols))
    return pd.DataFrame(rows).set_index("symbol")


def _pct(s: pd.Series, higher_is_better: bool = True) -> pd.Series:
    return s.rank(pct=True, ascending=higher_is_better)


def score(df: pd.DataFrame, sectors: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Add sector-relative component scores (0-1) and a total; drop unprofitable companies."""
    d = df.copy()
    for c in FIELDS[2:]:
        d[c] = pd.to_numeric(d.get(c), errors="coerce")
    if sectors:
        d["sector"] = [sectors.get(s) or d.at[s, "sector"] for s in d.index]
    d["sector"] = d["sector"].fillna("Unknown")
    d = d[(d["trailingEps"] > 0) & (d["returnOnEquity"] > 0)].copy()
    pe = d["forwardPE"].where(d["forwardPE"] > 0, d["trailingPE"])
    d["earnings_yield"] = 1 / pe.where(pe > 0)

    def by_sector(col: str, higher: bool = True) -> pd.Series:
        return d.groupby("sector")[col].transform(lambda x: _pct(x, higher))

    d["quality"] = pd.concat([by_sector("returnOnEquity"), by_sector("profitMargins")], axis=1).mean(axis=1)
    d["growth"] = pd.concat([by_sector("revenueGrowth"), by_sector("earningsGrowth")], axis=1).mean(axis=1)
    d["value"] = by_sector("earnings_yield")
    d["safety"] = by_sector("debtToEquity", higher=False).where(~d["sector"].isin(NO_SAFETY))
    d["score"] = d[["quality", "growth", "value", "safety"]].mean(axis=1, skipna=True)
    return d.sort_values("score", ascending=False)


def report(d: pd.DataFrame, top: int = 25) -> str:
    t = d.head(top)
    cols = {
        "name": t["shortName"].astype(str).str.slice(0, 24),
        "sector": t["sector"].astype(str).str.slice(0, 22),
        "score": t["score"].round(2),
        "qual": t["quality"].round(2), "grow": t["growth"].round(2),
        "value": t["value"].round(2), "safe": t["safety"].round(2),
        "ROE": (t["returnOnEquity"] * 100).round(0).astype("Int64").astype(str) + "%",
        "margin": (t["profitMargins"] * 100).round(0).astype("Int64").astype(str) + "%",
        "rev_g": (t["revenueGrowth"] * 100).round(0).astype("Int64").astype(str) + "%",
        "fwd_PE": t["forwardPE"].round(1),
        "mcap_$B": (t["marketCap"] / 1e9).round(0).astype("Int64"),
    }
    return pd.DataFrame(cols, index=t.index).to_string()


def cmd_screen(args, settings) -> int:
    from .config import is_tradable, resolve_path
    from .services import symbols_for

    cache = resolve_path(settings["data"]["cache_dir"]) / "fundamentals"
    sectors: Dict[str, str] = dict(settings.get("sectors") or {})
    if args.universe == "watchlist":
        symbols = [s for s in symbols_for(settings, "us") if is_tradable(s) and s != "SPY"]
    else:
        from .universe import current_sp500

        members = current_sp500(resolve_path(settings["data"]["cache_dir"]) / "sp500")
        symbols = list(members["symbol"])
        sectors.update(dict(zip(members["symbol"], members["sector"])))
    print(f"Fetching fundamentals for {len(symbols)} stocks (cached {CACHE_DAYS} days)...")
    raw = fetch_many(symbols, cache, refresh=args.refresh)
    errors = raw["error"].notna().sum() if "error" in raw else 0
    d = score(raw.drop(columns=["error"], errors="ignore"), sectors)
    if args.sector:
        d = d[d["sector"].str.lower() == args.sector.lower()]
        if d.empty:
            print(f"No stocks in sector {args.sector!r}. Sectors: {', '.join(sorted(set(sectors.values())))}")
            return 1
    print(f"{len(d)} profitable stocks scored" + (f" ({errors} could not be fetched)" if errors else "")
          + ". Scores are 0-1, relative to the stock's own sector.\n")
    print(report(d, args.top))
    if args.csv:
        d.to_csv(args.csv)
        print(f"\nSaved {args.csv}")
    print("\nCurrent figures only: a starting point for research, not a buy list. Not financial advice.")
    return 0


def add_parser(sub) -> None:
    sp = sub.add_parser("screen", help="rank stocks on fundamentals (quality, growth, value, safety)")
    sp.add_argument("--universe", choices=["sp500", "watchlist"], default="sp500")
    sp.add_argument("--sector", help='only this GICS sector, e.g. "Health Care"')
    sp.add_argument("--top", type=int, default=25)
    sp.add_argument("--refresh", action="store_true", help="ignore the 7-day cache")
    sp.add_argument("--csv", help="save every scored stock to a CSV file")
