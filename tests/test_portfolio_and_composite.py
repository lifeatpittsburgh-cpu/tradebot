import numpy as np
import pandas as pd
import pytest

from tradebot.backtest import run_portfolio_backtest
from tradebot.config import is_tradable
from tradebot.paper import PaperBroker
from tradebot.risk import RiskConfig
from tradebot.strategies import Composite
from conftest import Scripted, make_ohlcv


# ---- composite voting ---------------------------------------------------------------
class Named(Scripted):
    def __init__(self, name, **kw):
        super().__init__(**kw)
        self.name = name


def test_composite_buys_on_second_vote_and_holds_until_all_flat():
    df = make_ohlcv(np.full(20, 100.0))
    subs = [Named("a", buys=(2,), sells=(10,)), Named("b", buys=(5,), sells=(8,)), Named("c")]
    out = Composite(subs).generate(df)["signal"]
    assert out[out == 1].index.tolist() == [df.index[5]]    # second strategy agrees on bar 5
    assert out[out == -1].index.tolist() == [df.index[10]]  # last one flat on bar 10


def test_composite_exit_votes_tighter_exit():
    df = make_ohlcv(np.full(20, 100.0))
    subs = [Named("a", buys=(2,), sells=(10,)), Named("b", buys=(5,), sells=(8,)), Named("c")]
    out = Composite(subs, min_votes=2, exit_votes=2).generate(df)["signal"]
    assert out[out == -1].index.tolist() == [df.index[8]]


def test_composite_validates_votes():
    with pytest.raises(ValueError):
        Composite(min_votes=1, exit_votes=2)
    with pytest.raises(ValueError):
        Composite(min_votes=4)


# ---- portfolio engine ---------------------------------------------------------------
def _rally(n_flat=30, n_up=30, base=100.0):
    return make_ohlcv(np.concatenate([np.full(n_flat, base), np.linspace(base, base * 1.2, n_up)]), spread=0.002)


def test_max_positions_limits_entries():
    data = {s: _rally() for s in ("A", "B", "C")}
    res = run_portfolio_backtest(data, Scripted(buys=(30,)), 30_000, RiskConfig(reward_risk=100), max_positions=2)
    assert {t.symbol for t in res.trades} == {"A", "B"}  # watchlist order wins the slots
    assert res.skipped_entries == 1


def test_shared_cash_and_reconciliation():
    data = {"A": _rally(), "B": _rally(base=50.0)}
    res = run_portfolio_backtest(data, Scripted(buys=(30,), sells=(45,)), 10_000,
                                 RiskConfig(reward_risk=100), commission=0.001, slippage=0.001)
    assert len(res.trades) == 2
    entry_cost = sum(t.cost for t in res.trades)
    assert entry_cost <= 10_000
    assert res.equity.iloc[-1] == pytest.approx(10_000 + sum(t.pnl for t in res.trades))
    assert set(res.per_symbol["symbol"]) == {"A", "B"}


def test_handles_different_calendars():
    """US-style and India-style holidays: symbols with missing days still trade correctly."""
    a = _rally()
    b = _rally().drop(index=_rally().index[[31, 32]])  # B has no bars on days 31-32
    res = run_portfolio_backtest({"A": a, "B": b}, Scripted(buys=(30,)), 20_000, RiskConfig(reward_risk=100))
    entries = {t.symbol: t.entry_date for t in res.trades}
    assert entries["A"] == a.index[31]
    assert entries["B"] == b.index[31] == a.index[33]  # B's next bar skips its missing days
    assert len(res.equity) == len(a)


def test_benchmark_return_reported():
    df = _rally()
    bm = df["close"] * 2
    res = run_portfolio_backtest({"A": df}, Scripted(buys=(30,)), 10_000, benchmark=bm)
    assert res.metrics["benchmark_return"] == pytest.approx(df["close"].iloc[-1] / df["close"].iloc[0] - 1)


# ---- tradability and paper limits ---------------------------------------------------
def test_indexes_not_tradable(tmp_path):
    assert not is_tradable("^NSEI") and is_tradable("SPY")
    b = PaperBroker(tmp_path / "l.json", {"us": 1, "india": 1}, {})
    assert not b.queue("^NSEI", "buy", "2024-01-01", atr=10)


def test_paper_respects_max_positions(tmp_path):
    costs = {"us": {"commission": 0.0, "slippage": 0.0}}
    b = PaperBroker(tmp_path / "l.json", {"us": 100_000}, costs, RiskConfig(max_positions=1))
    df = make_ohlcv(np.full(5, 100.0))
    for sym in ("A", "B"):
        b.queue(sym, "buy", df.index[0].strftime("%Y-%m-%d"), atr=2.0)
        b.sync(sym, df)
    assert list(b.positions) == ["A"]
    assert any("SKIPPED BUY B" in e for e in b.events)
