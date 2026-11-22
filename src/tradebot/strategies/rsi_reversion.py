from __future__ import annotations

import pandas as pd

from .. import indicators as ind
from .base import Strategy


class RSIMeanReversion(Strategy):
    """Buy pullbacks in an uptrend: RSI climbs back above `oversold` while price is above
    the long-term SMA. Exit when RSI rises above `exit_level`.
    """

    name = "rsi_reversion"
    defaults = {"period": 14, "oversold": 30, "exit_level": 65, "trend": 200}

    @property
    def warmup(self) -> int:
        return max(self.params["period"], self.params["trend"]) + 1

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = df["close"]
        r = ind.rsi(close, p["period"])
        uptrend = close > ind.sma(close, p["trend"])
        buy = ind.crossed_above(r, p["oversold"]) & uptrend
        sell = ind.crossed_above(r, p["exit_level"])

        out = self._frame(df.index)
        out.loc[buy, ["signal", "reason"]] = [1, f"RSI recovered above {p['oversold']} in uptrend"]
        out.loc[sell, ["signal", "reason"]] = [-1, f"RSI rose above {p['exit_level']}"]
        return out
