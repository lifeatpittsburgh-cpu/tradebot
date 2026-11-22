"""Data provider interface. Every provider returns the same OHLCV frame shape."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, time, timezone
from typing import Optional

import pandas as pd

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class DataProvider(ABC):
    """Daily OHLCV history for one symbol.

    Returns a DataFrame indexed by a tz-naive DatetimeIndex (ascending) with
    lowercase columns open, high, low, close, volume. Prices are split/dividend adjusted.
    """

    @abstractmethod
    def get_history(self, symbol: str, start: date, end: Optional[date] = None) -> pd.DataFrame:
        ...


def empty_ohlcv() -> pd.DataFrame:
    """An empty OHLCV frame that still has a DatetimeIndex, so date filters work on it."""
    return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([], name="date"), dtype=float)


def normalize_ohlcv(df: pd.DataFrame, tz: Optional[str] = None) -> pd.DataFrame:
    """Coerce a raw frame into the standard OHLCV shape.

    Timestamps carrying a timezone are converted to `tz` (when given) and then made naive,
    so intraday bars read in the exchange's local wall-clock time.
    """
    if df is None or df.empty:
        return empty_ohlcv()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
    if "close" not in df.columns and "adj_close" in df.columns:
        df["close"] = df["adj_close"]
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV data missing columns: {missing}")
    df = df[OHLCV_COLUMNS]
    df.index = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        if tz:
            df.index = df.index.tz_convert(tz)
        df.index = df.index.tz_localize(None)
    df.index.name = "date"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.dropna(subset=["open", "high", "low", "close"])


# Exchange timezone per market, used to show intraday bars in local time.
MARKET_TZ = {"us": "America/New_York", "india": "Asia/Kolkata", "crypto": "UTC"}

# When each market's daily candle is final (local time) — a little after the official close.
_SESSION_CLOSE = {
    "us": ("America/New_York", time(16, 15)),
    "india": ("Asia/Kolkata", time(15, 45)),
    "crypto": ("UTC", None),  # 24/7: a UTC day's candle is final only once the day is over
}


def drop_incomplete_bar(df: pd.DataFrame, market: str, now: Optional[datetime] = None) -> pd.DataFrame:
    """Remove today's candle while it's still forming, so signals never act on a partial day."""
    if df.empty or market not in _SESSION_CLOSE:
        return df
    from zoneinfo import ZoneInfo

    tz_name, close_time = _SESSION_CLOSE[market]
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(tz_name))
    last_day = df.index[-1].date()
    if last_day > local.date():
        return df.iloc[:-1]
    if last_day == local.date() and (close_time is None or local.time() < close_time):
        return df.iloc[:-1]
    return df
