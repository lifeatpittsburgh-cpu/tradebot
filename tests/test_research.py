"""Phase 4 research tools: backtest options, entry filters and walk-forward (synthetic data)."""
import numpy as np
import pandas as pd
import pytest

from conftest import Scripted, make_ohlcv
from tradebot.backtest import run_portfolio_backtest
from tradebot.filters import FilterConfig, regime_on, relative_strength_ok, volume_ok
from tradebot.research.walkforward import _fold_dates, param_grid, walk_forward
from tradebot.risk import RiskConfig

RISK = RiskConfig(risk_per_trade=0.05, atr_stop_mult=5, reward_risk=50)  # rising prices: exits only on signals


def rising(n=300, start="2020-01-01"):
    return make_ohlcv(100 + np.arange(n, dtype=float), start=start)


def test_start_ignores_signals_before_it():
    df = rising()
    strat = Scripted(buys=(10, 200), sells=(50, 250))
    res = run_portfolio_backtest({"AAA": df}, strat, risk=RISK, start=df.index[100])
    assert [t.entry_date for t in res.trades] == [df.index[201]]       # only the BUY after `start`
    assert res.equity.index[0] == df.index[0] and res.equity[df.index[150]] == 10_000  # flat until then


def test_precomputed_signals_override_strategy():
    df = rising()
    sig = pd.Series(0, index=df.index)
    sig.iloc[20], sig.iloc[40] = 1, -1
    res = run_portfolio_backtest({"AAA": df}, Scripted(), risk=RISK, signals={"AAA": sig})
    assert len(res.trades) == 1 and res.trades[0].entry_date == df.index[21]


def test_entry_filter_blocks_buy_and_counts_it():
    df = rising()
    allowed = pd.Series(True, index=df.index)
    allowed.iloc[20] = False
    res = run_portfolio_backtest({"AAA": df}, Scripted(buys=(20, 60), sells=(50, 100)), risk=RISK,
                                 entry_allowed={"AAA": allowed})
    assert [t.entry_date for t in res.trades] == [df.index[61]]
    assert res.metrics["blocked_entries"] == 1


def test_force_exit_sells_and_blocks():
    df = rising()
    off = pd.Series(False, index=df.index)
    off.iloc[30:60] = True
    res = run_portfolio_backtest({"AAA": df}, Scripted(buys=(20, 40, 70), sells=(200,)), risk=RISK, force_exit=off)
    t = res.trades
    assert t[0].entry_date == df.index[21] and t[0].exit_date == df.index[31] and t[0].exit_reason == "signal"
    assert t[1].entry_date == df.index[71]   # the BUY on bar 40 fell inside the "off" period


def test_regime_and_volume_filters():
    close = pd.Series(np.r_[np.linspace(100, 200, 250), np.linspace(200, 120, 100)],
                      index=pd.bdate_range("2020-01-01", periods=350))
    on = regime_on(close, 200)
    assert not on.iloc[:199].any()               # warm-up
    assert on.iloc[240] and not on.iloc[-1]      # above in the rally, below after the fall

    df = rising(60)
    df.loc[df.index[-1], "volume"] = 3_000_000
    v = volume_ok(df, 20, 1.5)
    assert v.iloc[-1] and not v.iloc[-2]


def test_relative_strength_picks_the_leaders():
    idx = pd.bdate_range("2020-01-01", periods=200)
    data = {s: make_ohlcv(100 * (1 + g) ** np.arange(200), start="2020-01-01")
            for s, g in (("FAST", 0.004), ("MID", 0.002), ("SLOW", 0.0), ("DOWN", -0.002))}
    ok = relative_strength_ok(data, lookback=100, top_frac=0.5)
    assert ok["FAST"].iloc[-1] and ok["MID"].iloc[-1]
    assert not ok["SLOW"].iloc[-1] and not ok["DOWN"].iloc[-1]
    assert not ok["FAST"].iloc[50]               # not enough history yet
    assert ok["FAST"].index.equals(idx)


def test_filter_config_from_settings_and_label():
    cfg = FilterConfig.from_settings({"filters": {"regime": {"enabled": True, "sma": 150, "exit": True},
                                                  "relative_strength": {"enabled": True, "top_frac": 0.3}}})
    assert cfg.regime and cfg.regime_sma == 150 and cfg.regime_exit and cfg.rs and not cfg.volume
    assert cfg.label() == "regime150+exit+rs126/top0.3"
    assert FilterConfig().label() == "none" and not FilterConfig().any


def test_param_grid_skips_invalid_combos():
    combos = param_grid("sma_crossover")
    assert all(c["fast"] < c["slow"] for c in combos) and len(combos) == 8
    assert all(c["exit_votes"] <= c["min_votes"] for c in param_grid("composite"))


def test_fold_dates_cover_the_test_period_without_gaps():
    folds = _fold_dates(pd.Timestamp("2015-01-01"), pd.Timestamp("2020-12-31"), 3, 6)
    assert folds[0][1] == pd.Timestamp("2018-01-01")
    for (_, _, end), (_, nxt, _) in zip(folds, folds[1:]):
        assert nxt == end + pd.Timedelta(days=1)
    assert folds[-1][2] == pd.Timestamp("2020-12-31")


def test_walk_forward_runs_end_to_end():
    rng = np.random.default_rng(7)
    data = {f"S{i}": make_ohlcv(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, 1500))), start="2015-01-01")
            for i in range(3)}
    wf = walk_forward(data, "sma_crossover", 10_000, RiskConfig(), 0.0, 0.0,
                      grid={"fast": [10, 20], "slow": [50]}, train_years=2, test_months=6)
    assert len(wf.folds) >= 5
    assert all(f.params in ({"fast": 10, "slow": 50}, {"fast": 20, "slow": 50}) for f in wf.folds)
    first_test = wf.folds[0].test_start
    assert all(t.entry_date >= first_test for t in wf.oos.trades)   # nothing traded in training data
    assert wf.oos.equity[wf.oos.equity.index < first_test].eq(10_000).all()
