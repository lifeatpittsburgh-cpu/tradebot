"""Yahoo Finance provider (free, unofficial). US tickers, crypto (BTC-USD) and NSE/BSE (.NS/.BO)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import pandas as pd

from ..config import market_of
from .base import MARKET_TZ, DataProvider, drop_incomplete_bar, normalize_ohlcv

# Yahoo only keeps a limited window of intraday bars: the longest history available per interval.
INTRADAY_MAX_DAYS = {"1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60, "60m": 730, "1h": 730}


class YFinanceProvider(DataProvider):
    def get_history(self, symbol: str, start: date, end: Optional[date] = None) -> pd.DataFrame:
        import yfinance as yf  # imported lazily so tests don't need network libs loaded

        # yfinance's `end` is exclusive; add a day so the latest close is included.
        end_excl = (end or date.today()) + timedelta(days=1)
        raw = yf.download(
            symbol,
            start=start.isoformat(),
            end=end_excl.isoformat(),
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        return drop_incomplete_bar(normalize_ohlcv(raw), market_of(symbol))

    def get_intraday(self, symbol: str, interval: str = "5m", days: int = 5) -> pd.DataFrame:
        """Intraday bars (1m, 5m, 15m, 1h, ...) for charting, in the exchange's local time.

        The last bar may still be forming. Yahoo limits history: 7 days of 1m bars,
        60 days of 2m-30m bars and 730 days of hourly bars; `days` is clipped to that.
        """
        import yfinance as yf

        if interval not in INTRADAY_MAX_DAYS:
            raise ValueError(f"unsupported interval {interval!r}; use one of {sorted(INTRADAY_MAX_DAYS)}")
        days = max(1, min(int(days), INTRADAY_MAX_DAYS[interval]))
        raw = yf.download(
            symbol,
            period=f"{days}d",
            interval=interval,
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        return normalize_ohlcv(raw, tz=MARKET_TZ.get(market_of(symbol)))
