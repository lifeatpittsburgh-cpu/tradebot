from __future__ import annotations

import pandas as pd

from .. import indicators as ind
from .base import Strategy


class SMACrossover(Strategy):
    """Trend following: buy when the fast SMA crosses above the slow SMA, sell on the reverse.

    Optional trend filter: only buy while price is above the long-term SMA.
    """

    name = "sma_crossover"
    defaults = {"fast": 20, "slow": 50, "trend": 200, "use_trend_filter": True}

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p["slow"], p["trend"] if p["use_trend_filter"] else 0) + 1

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = df["close"]
        fast, slow = ind.sma(close, p["fast"]), ind.sma(close, p["slow"])
        buy = ind.crossed_above(fast, slow)
        if p["use_trend_filter"]:
            buy &= close > ind.sma(close, p["trend"])
        sell = ind.crossed_below(fast, slow)

        out = self._frame(df.index)
        out.loc[buy, ["signal", "reason"]] = [1, f"SMA{p['fast']} crossed above SMA{p['slow']}"]
        out.loc[sell, ["signal", "reason"]] = [-1, f"SMA{p['fast']} crossed below SMA{p['slow']}"]
        return out
