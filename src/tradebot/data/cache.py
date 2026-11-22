"""On-disk CSV cache around any provider. A symbol is re-downloaded at most once per `ttl_minutes`."""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from .base import DataProvider, empty_ohlcv, normalize_ohlcv


class CachedProvider(DataProvider):
    def __init__(self, inner: DataProvider, cache_dir, ttl_minutes: float = 60) -> None:
        self.inner = inner
        self.ttl_seconds = ttl_minutes * 60
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        safe = symbol.replace("^", "IDX_").replace("/", "_")
        return self.cache_dir / f"{safe}.csv"

    def get_history(self, symbol: str, start: date, end: Optional[date] = None) -> pd.DataFrame:
        path = self._path(symbol)
        cached: Optional[pd.DataFrame] = None
        if path.exists():
            cached = normalize_ohlcv(pd.read_csv(path, index_col=0, parse_dates=True))
            fresh = time.time() - path.stat().st_mtime < self.ttl_seconds
            covers_start = not cached.empty and cached.index[0].date() <= start
            if fresh and covers_start:
                return _slice(cached, start, end)

        df = self.inner.get_history(symbol, start, end)
        if cached is not None and not cached.empty:
            df = normalize_ohlcv(pd.concat([cached, df]))
        if not df.empty:
            df.to_csv(path)
        return _slice(df, start, end)


def _slice(df: pd.DataFrame, start: date, end: Optional[date]) -> pd.DataFrame:
    if df.empty:
        return empty_ohlcv()
    out = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        out = out[out.index <= pd.Timestamp(end)]
    return out
