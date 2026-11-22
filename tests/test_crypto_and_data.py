from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from tradebot.backtest import run_portfolio_backtest
from tradebot.config import market_of
from tradebot.data import drop_incomplete_bar
from tradebot.risk import RiskConfig, affordable_qty, plan_long, round_qty
from conftest import Scripted, make_ohlcv


def test_crypto_market_detection():
    assert market_of("BTC-USD") == "crypto"
    assert market_of("eth-usdt") == "crypto"
    assert market_of("BRK-B") == "us"  # hyphenated stock tickers stay US


def test_fractional_sizing():
    assert round_qty(0.123456, 0.0001) == 0.1234
    assert round_qty(7.9, 1.0) == 7.0
    plan = plan_long(entry=60_000, atr_value=3_000, equity=10_000, cfg=RiskConfig(), step=0.0001)
    assert plan is not None and 0 < plan.quantity < 1  # whole-coin sizing would have returned None
    assert plan_long(60_000, 3_000, 10_000, RiskConfig(), step=1.0) is None
    assert affordable_qty(1.0, 60_000, 30_000, 0.0, 0.0001) == 0.5


def test_crypto_backtest_buys_fractions():
    df = make_ohlcv(np.concatenate([np.full(30, 60_000.0), np.linspace(60_000, 70_000, 30)]), spread=0.002)
    res = run_portfolio_backtest({"BTC-USD": df}, Scripted(buys=(30,)), 10_000, RiskConfig(reward_risk=100))
    assert len(res.trades) == 1 and res.trades[0].quantity < 1
    assert res.trades[0].pnl > 0


@pytest.mark.parametrize("market,now,expect_dropped", [
    ("us", datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), True),     # 1pm ET, market open
    ("us", datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc), False),    # 5pm ET, candle final
    ("crypto", datetime(2026, 11, 20, 23, 0, tzinfo=timezone.utc), True),  # UTC day still running
    ("crypto", datetime(2026, 11, 21, 0, 5, tzinfo=timezone.utc), False),  # day over
])
def test_drop_incomplete_bar(market, now, expect_dropped):
    df = make_ohlcv(np.full(5, 100.0), start="2026-11-16")  # last bar 2026-11-20
    out = drop_incomplete_bar(df, market, now)
    assert len(out) == (4 if expect_dropped else 5)


def test_empty_download_keeps_datetime_index():
    from tradebot.data import normalize_ohlcv

    out = normalize_ohlcv(pd.DataFrame())
    assert out.empty and isinstance(out.index, pd.DatetimeIndex)
    assert out[out.index >= pd.Timestamp("2024-01-01")].empty  # date filters must not crash


def test_cache_handles_unknown_ticker(tmp_path):
    from datetime import date

    from tradebot.data import CachedProvider, DataProvider, normalize_ohlcv

    class NoData(DataProvider):
        def get_history(self, symbol, start, end=None):
            return normalize_ohlcv(pd.DataFrame())

    out = CachedProvider(NoData(), tmp_path).get_history("UNI-USD", date(2024, 1, 1))
    assert out.empty
    assert not (tmp_path / "UNI-USD.csv").exists()


def test_intraday_timestamps_converted_to_exchange_time():
    from tradebot.data import normalize_ohlcv

    idx = pd.DatetimeIndex(["2024-03-04 14:30", "2024-03-04 14:31"], tz="UTC")
    raw = pd.DataFrame({"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [5, 5]}, index=idx)
    out = normalize_ohlcv(raw, tz="America/New_York")
    assert out.index[0] == pd.Timestamp("2024-03-04 09:30") and out.index.tz is None
