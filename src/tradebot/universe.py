"""Point-in-time S&P 500 membership and sectors, for survivorship-free research.

Backtesting today's index members over the past only tests stocks that we already know
survived and grew (survivorship bias). Instead this rebuilds who was in the index at each
point: it reads past versions of Wikipedia's "List of S&P 500 companies" page, one per
quarter, through Wikipedia's public revision API. A stock counts as a member from the first
snapshot that lists it until the first snapshot that doesn't, so dates are accurate to about
one quarter. Each snapshot also gives the stock's GICS sector.

Limits: Yahoo has no prices for many removed tickers (acquisitions, bankruptcies), so some
survivorship bias remains; `tradebot universe update` reports how many are missing. A ticker
later reused by a different company can pick up the wrong prices.

    tradebot universe update     # fetch snapshots (once; ~1 minute) and download prices
    tradebot universe status
"""
from __future__ import annotations

import io
import time
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd

UA = {"User-Agent": "tradebot-research/0.1 (personal research script)"}
API = "https://en.wikipedia.org/w/api.php"
PAGE = "List of S&P 500 companies"


def to_yahoo(ticker: str) -> str:
    """Wikipedia writes share classes with a dot (BRK.B); Yahoo uses a dash (BRK-B)."""
    return str(ticker).strip().upper().replace(".", "-")


def _constituents(html: str) -> pd.DataFrame:
    """The constituents table of one page version: symbol, security, sector."""
    for t in pd.read_html(io.StringIO(html)):
        cols = {str(c).lower(): c for c in t.columns}
        sym = next((cols[c] for c in cols if "symbol" in c or c == "ticker"), None)
        sec = next((cols[c] for c in cols if "gics sector" in c), None)
        if sym is not None and sec is not None and len(t) > 300:
            name = cols.get("security", sym)
            return pd.DataFrame({"symbol": t[sym].map(to_yahoo), "security": t[name].astype(str),
                                 "sector": t[sec].astype(str)})
    raise ValueError("no constituents table found")


def fetch_snapshots(start: str = "2013-01-01", months: int = 3, pause: float = 0.5) -> pd.DataFrame:
    """One page version per `months` from `start` to today, as rows (snapshot, symbol, security, sector)."""
    import requests

    rows: List[pd.DataFrame] = []
    seen_rev = set()
    for when in pd.date_range(start, date.today(), freq=f"{months}MS").append(pd.DatetimeIndex([pd.Timestamp(date.today())])):
        j = requests.get(API, params=dict(action="query", prop="revisions", titles=PAGE, rvlimit=1,
                                          rvstart=f"{when:%Y-%m-%d}T23:59:59Z", rvdir="older",
                                          rvprop="ids|timestamp", format="json"), headers=UA, timeout=30).json()
        rev = list(j["query"]["pages"].values())[0]["revisions"][0]
        if rev["revid"] in seen_rev:
            continue
        seen_rev.add(rev["revid"])
        html = requests.get("https://en.wikipedia.org/w/index.php", params={"oldid": rev["revid"]},
                            headers=UA, timeout=30).text
        snap = _constituents(html)
        snap.insert(0, "snapshot", pd.Timestamp(when).normalize())
        rows.append(snap)
        time.sleep(pause)  # be polite to Wikipedia
    return pd.concat(rows, ignore_index=True)


class Universe:
    """Membership by quarterly snapshot. `member_mask` gives a per-day boolean series per symbol."""

    def __init__(self, snapshots: pd.DataFrame) -> None:
        snapshots = snapshots.copy()
        snapshots["snapshot"] = pd.to_datetime(snapshots["snapshot"])
        self.snapshots = snapshots
        self.dates = sorted(snapshots["snapshot"].unique())
        self.by_date = {d: set(g["symbol"]) for d, g in snapshots.groupby("snapshot")}

    @classmethod
    def load(cls, path) -> "Universe":
        return cls(pd.read_csv(path))

    @property
    def symbols(self) -> List[str]:
        return sorted(self.snapshots["symbol"].unique())

    def members(self, day) -> set:
        day = pd.Timestamp(day)
        past = [d for d in self.dates if d <= day]
        return self.by_date[past[-1]] if past else set()

    def sectors(self) -> Dict[str, str]:
        """Latest known GICS sector per symbol."""
        latest = self.snapshots.sort_values("snapshot").groupby("symbol").tail(1)
        return dict(zip(latest["symbol"], latest["sector"]))

    def member_mask(self, symbol: str, index: pd.DatetimeIndex) -> pd.Series:
        flags = pd.Series([symbol in self.by_date[d] for d in self.dates], index=pd.DatetimeIndex(self.dates))
        return flags.astype(float).reindex(index, method="ffill").fillna(0.0).astype(bool)


def download_prices(symbols: Iterable[str], start: date, cache_dir, batch: int = 60) -> Dict[str, str]:
    """Bulk-download daily prices into the regular CSV cache. Returns {symbol: problem} for failures."""
    import yfinance as yf

    from .data.base import drop_incomplete_bar, normalize_ohlcv
    from .data.cache import CachedProvider

    cache = CachedProvider.__new__(CachedProvider)  # only for its file-naming rule
    cache.cache_dir = Path(cache_dir)
    cache.cache_dir.mkdir(parents=True, exist_ok=True)
    symbols = list(symbols)
    problems: Dict[str, str] = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        raw = yf.download(chunk, start=start.isoformat(), interval="1d", auto_adjust=True,
                          progress=False, threads=True, group_by="ticker")
        for s in chunk:
            try:
                part = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = normalize_ohlcv(part.dropna(how="all"))
            except Exception as exc:
                problems[s] = f"{type(exc).__name__}"
                continue
            df = drop_incomplete_bar(df, "us")
            if df.empty:
                problems[s] = "no data"
                continue
            df.to_csv(cache._path(s))
    return problems


def load_cached(symbols: Iterable[str], cache_dir, start: Optional[date] = None) -> Dict[str, pd.DataFrame]:
    from .data.base import normalize_ohlcv
    from .data.cache import CachedProvider

    cache = CachedProvider.__new__(CachedProvider)
    cache.cache_dir = Path(cache_dir)
    out = {}
    for s in symbols:
        p = cache._path(s)
        if p.exists():
            df = normalize_ohlcv(pd.read_csv(p, index_col=0, parse_dates=True))
            if start is not None:
                df = df[df.index >= pd.Timestamp(start)]
            if len(df) > 250:
                out[s] = df
    return out


def current_sp500(cache_dir, max_age_days: int = 30) -> pd.DataFrame:
    """Today's members (symbol, security, sector), from the latest snapshot or a fresh page fetch."""
    cache_dir = Path(cache_dir)
    path = cache_dir / "current.csv"
    if path.exists() and time.time() - path.stat().st_mtime < max_age_days * 86400:
        return pd.read_csv(path)
    import requests

    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=UA, timeout=30).text
    df = _constituents(html)
    cache_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


def cmd_universe(args, settings) -> int:
    from .config import resolve_path

    folder = resolve_path(settings["data"]["cache_dir"]) / "sp500"
    snap_path = folder / "snapshots.csv"
    if args.action == "update":
        print("Fetching quarterly S&P 500 snapshots from Wikipedia's page history (a few minutes)...")
        snaps = fetch_snapshots(args.since, 3)
        folder.mkdir(parents=True, exist_ok=True)
        snaps.to_csv(snap_path, index=False)
        u = Universe(snaps)
        print(f"{len(u.dates)} snapshots, {len(u.symbols)} different stocks. Downloading prices...")
        start = (pd.Timestamp(args.since) - pd.DateOffset(years=1)).date()
        problems = download_prices(u.symbols, start, folder)
        pd.Series(problems, name="problem").to_csv(folder / "missing.csv")
        print(f"Prices for {len(u.symbols) - len(problems)} of {len(u.symbols)} stocks "
              f"({len(problems)} not on Yahoo, mostly removed or acquired companies).")
    if not snap_path.exists():
        print("No universe yet. Run `tradebot universe update`.")
        return 1
    u = Universe.load(snap_path)
    data = load_cached(u.symbols, folder)
    print(f"S&P 500 snapshots: {len(u.dates)} from {pd.Timestamp(u.dates[0]):%Y-%m} to {pd.Timestamp(u.dates[-1]):%Y-%m}; "
          f"{len(u.symbols)} stocks ever, {len(data)} with prices")
    for d in u.dates[::8] + [u.dates[-1]]:
        have = len([s for s in u.by_date[d] if s in data]) / len(u.by_date[d])
        print(f"  {pd.Timestamp(d):%Y-%m}: {have:.0%} of that quarter's members have prices")
    return 0


def add_parser(sub) -> None:
    sp = sub.add_parser("universe", help="S&P 500 membership history for survivorship-free research")
    sp.add_argument("action", choices=["update", "status"])
    sp.add_argument("--since", default="2013-01-01", help="first snapshot date (default 2013-01-01)")
