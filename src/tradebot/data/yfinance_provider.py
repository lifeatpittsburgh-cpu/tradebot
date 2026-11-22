"""Yahoo Finance provider (free, unofficial). US tickers, crypto (BTC-USD) and NSE/BSE (.NS/.BO)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import pandas as pd

from ..config import market_of
from .base import DataProvider, drop_incomplete_bar, normalize_ohlcv


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
