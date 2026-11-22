import numpy as np
import pandas as pd
import pytest

from tradebot.backtest import max_drawdown, run_backtest
from tradebot.risk import RiskConfig, plan_long
from conftest import Scripted, make_ohlcv


# ---- risk ---------------------------------------------------------------------------
def test_plan_long_risk_sizing():
    cfg = RiskConfig(risk_per_trade=0.01, atr_stop_mult=2, reward_risk=2, max_position_pct=1.0)
    plan = plan_long(entry=100, atr_value=2.5, equity=10_000, cfg=cfg)
    assert plan.stop_loss == 95 and plan.target == 110
    assert plan.quantity == 20  # 100 risk / 5 per share


def test_plan_long_caps_position_size():
    cfg = RiskConfig(risk_per_trade=0.05, max_position_pct=0.2)
    plan = plan_long(entry=100, atr_value=0.5, equity=10_000, cfg=cfg)
    assert plan.quantity == 20  # 20% of 10k / 100


def test_plan_long_rejects_bad_inputs():
    cfg = RiskConfig()
    assert plan_long(100, float("nan"), 10_000, cfg) is None
    assert plan_long(100, 1, 10, cfg) is None  # can't afford one share


# ---- engine -------------------------------------------------------------------------
def _flat_then(prices_after, n_flat=30, base=100.0):
    return make_ohlcv(np.concatenate([np.full(n_flat, base), prices_after]), spread=0.002)


def test_fills_next_open_and_exits_on_signal():
    df = _flat_then(np.linspace(100, 110, 20))
    strat = Scripted(buys=(30,), sells=(40,))
    res = run_backtest(df, strat, capital=10_000, risk=RiskConfig(reward_risk=100))
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_date == df.index[31] and t.entry_price == df["open"].iloc[31]
    assert t.exit_date == df.index[41] and t.exit_reason == "signal"
    assert t.pnl > 0


def test_stop_loss_limits_loss():
    df = _flat_then(np.linspace(100, 60, 20))
    res = run_backtest(df, Scripted(buys=(30,)), capital=10_000, risk=RiskConfig(risk_per_trade=0.01))
    t = res.trades[0]
    assert t.exit_reason == "stop"
    # Loss should be close to 1% of equity (plus a little gap / spread slack).
    assert -0.02 * 10_000 < t.pnl < 0


def test_target_hit():
    df = _flat_then(np.linspace(100, 200, 30))
    res = run_backtest(df, Scripted(buys=(30,)), capital=10_000)
    assert res.trades[0].exit_reason == "target"


def test_costs_reduce_pnl():
    df = _flat_then(np.linspace(100, 110, 20))
    strat = Scripted(buys=(30,), sells=(40,))
    risk = RiskConfig(reward_risk=100)
    free = run_backtest(df, strat, 10_000, risk)
    costly = run_backtest(df, strat, 10_000, risk, commission=0.001, slippage=0.001)
    assert costly.trades[0].pnl < free.trades[0].pnl


def test_equity_reconciles_with_trades(random_walk):
    from tradebot.strategies import get_strategy
    res = run_backtest(random_walk, get_strategy("sma_crossover"), 10_000, commission=0.001, slippage=0.0005)
    assert res.equity.iloc[-1] == pytest.approx(10_000 + sum(t.pnl for t in res.trades))
    assert set(res.metrics) >= {"cagr", "max_drawdown", "sharpe", "win_rate", "buy_hold_return"}


def test_max_drawdown():
    eq = pd.Series([100, 120, 90, 130, 65])
    assert max_drawdown(eq) == pytest.approx(-0.5)
