import numpy as np
import pandas as pd

from tradebot import indicators as ind
from conftest import make_ohlcv


def test_sma_matches_manual():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = ind.sma(s, 3)
    assert out.isna().sum() == 2
    assert out.iloc[-1] == 4.0


def test_rsi_bounds_and_extremes(random_walk):
    r = ind.rsi(random_walk["close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    up = ind.rsi(pd.Series(np.arange(1, 50, dtype=float)))
    assert up.iloc[-1] == 100.0
    down = ind.rsi(pd.Series(np.arange(50, 1, -1, dtype=float)))
    assert down.iloc[-1] == 0.0


def test_macd_columns(random_walk):
    m = ind.macd(random_walk["close"])
    assert list(m.columns) == ["macd", "signal", "hist"]
    np.testing.assert_allclose(m["hist"].dropna(), (m["macd"] - m["signal"]).dropna())


def test_atr_positive_and_includes_gaps():
    df = make_ohlcv([100, 100, 100, 120, 120], spread=0.0)
    df.loc[df.index[3], ["open", "low"]] = 118.0  # gap up: bar range 118-120, prev close 100
    tr = ind.true_range(df)
    assert tr.iloc[3] == 20.0  # gap counted via previous close, not just high-low (2)
    a = ind.atr(df, 2).dropna()
    assert (a >= 0).all() and a.iloc[-1] > 0


def test_bollinger_order(random_walk):
    bb = ind.bollinger(random_walk["close"]).dropna()
    assert (bb["upper"] >= bb["mid"]).all() and (bb["mid"] >= bb["lower"]).all()


def test_crossings():
    a = pd.Series([1, 2, 3, 2, 1], dtype=float)
    assert ind.crossed_above(a, 2.5).tolist() == [False, False, True, False, False]
    assert ind.crossed_below(a, 2.5).tolist() == [False, False, False, True, False]
