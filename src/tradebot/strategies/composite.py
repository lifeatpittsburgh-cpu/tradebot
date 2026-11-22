from __future__ import annotations

from typing import List, Optional

import pandas as pd

from .base import Strategy
from .macd_trend import MACDTrend
from .rsi_reversion import RSIMeanReversion
from .sma_crossover import SMACrossover


class Composite(Strategy):
    """Vote of sub-strategies: BUY when the number currently 'long' rises to `min_votes`;
    SELL when it falls below `exit_votes`.

    Each sub-strategy is 'long' from its own BUY until its own SELL. With the defaults
    (2 to enter, 1 to stay), a trade opens when two strategies agree and stays open until
    every sub-strategy has turned flat (or the stop/target is hit).
    """

    name = "composite"
    defaults = {"min_votes": 2, "exit_votes": 1}

    def __init__(self, strategies: Optional[List[Strategy]] = None, **params) -> None:
        super().__init__(**params)
        self.strategies = strategies or [SMACrossover(), RSIMeanReversion(), MACDTrend()]
        p = self.params
        if not 1 <= p["exit_votes"] <= p["min_votes"] <= len(self.strategies):
            raise ValueError("composite: need 1 <= exit_votes <= min_votes <= number of strategies")

    @property
    def warmup(self) -> int:
        return max(s.warmup for s in self.strategies)

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        states = pd.DataFrame({s.name: s.state(df) for s in self.strategies}, index=df.index)
        votes = states.sum(axis=1)
        prev = votes.shift(1, fill_value=0)

        buy = (votes >= p["min_votes"]) & (prev < p["min_votes"])
        sell = (votes < p["exit_votes"]) & (prev >= p["exit_votes"])

        out = self._frame(df.index)
        for ts in out.index[buy]:
            longs = [n for n in states.columns if states.at[ts, n]]
            out.at[ts, "signal"] = 1
            out.at[ts, "reason"] = f"{int(votes[ts])}/{len(self.strategies)} bullish: " + ", ".join(longs)
        for ts in out.index[sell]:
            out.at[ts, "signal"] = -1
            out.at[ts, "reason"] = f"only {int(votes[ts])}/{len(self.strategies)} still bullish"
        return out
