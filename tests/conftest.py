import numpy as np
import pandas as pd
import pytest

from tradebot.strategies.base import Strategy


def make_ohlcv(closes, start="2022-01-03", spread=0.01) -> pd.DataFrame:
    """Build a daily OHLCV frame from a close series. open = previous close."""
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(start, periods=len(closes))
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) * (1 + spread)
    lows = np.minimum(opens, closes) * (1 - spread)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1_000_000},
        index=idx,
    )


@pytest.fixture
def random_walk():
    rng = np.random.default_rng(42)
    rets = rng.normal(0.0005, 0.015, 800)
    return make_ohlcv(100 * np.exp(np.cumsum(rets)))


@pytest.fixture
def down_then_up():
    """Long decline then a strong rally: a fast/slow SMA cross must happen."""
    down = np.linspace(200, 100, 250)
    up = np.linspace(100, 260, 250)
    return make_ohlcv(np.concatenate([down, up]))


class Scripted(Strategy):
    """Test strategy that fires BUY/SELL on given bar numbers."""
    name = "scripted"
    defaults = {"buys": (), "sells": ()}

    @property
    def warmup(self):
        return 0

    def generate(self, df):
        out = self._frame(df.index)
        for i in self.params["buys"]:
            out.iloc[i] = [1, "b"]
        for i in self.params["sells"]:
            out.iloc[i] = [-1, "s"]
        return out
