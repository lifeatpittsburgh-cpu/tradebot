from __future__ import annotations

import pandas as pd

from .. import indicators as ind
from .base import Strategy


class MACDTrend(Strategy):
    """Momentum: buy when MACD crosses above its signal line while price is above the trend EMA.
    Sell when MACD crosses below the signal line.
    """

    name = "macd_trend"
    defaults = {"fast": 12, "slow": 26, "signal": 9, "trend": 100}

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p["slow"] + p["signal"], p["trend"]) + 1

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = df["close"]
        m = ind.macd(close, p["fast"], p["slow"], p["signal"])
        buy = ind.crossed_above(m["macd"], m["signal"]) & (close > ind.ema(close, p["trend"]))
        sell = ind.crossed_below(m["macd"], m["signal"])

        out = self._frame(df.index)
        out.loc[buy, ["signal", "reason"]] = [1, "MACD crossed above signal, price above trend EMA"]
        out.loc[sell, ["signal", "reason"]] = [-1, "MACD crossed below signal"]
        return out
