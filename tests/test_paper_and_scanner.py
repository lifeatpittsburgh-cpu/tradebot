import numpy as np
import pytest

from tradebot.config import market_of
from tradebot.paper import PaperBroker
from tradebot.risk import RiskConfig
from tradebot.scanner import signal_for_latest_bar
from tradebot.strategies import get_strategy
from conftest import make_ohlcv

COSTS = {"us": {"commission": 0.0, "slippage": 0.0}, "india": {"commission": 0.001, "slippage": 0.0}}


@pytest.fixture
def broker(tmp_path):
    return PaperBroker(tmp_path / "ledger.json", {"us": 10_000, "india": 500_000}, COSTS, RiskConfig())


def test_market_detection():
    assert market_of("RELIANCE.NS") == "india"
    assert market_of("TCS.BO") == "india"
    assert market_of("^NSEI") == "india"
    assert market_of("AAPL") == "us"


def test_paper_buy_fills_next_open_then_target(broker):
    df = make_ohlcv(np.concatenate([np.full(30, 100.0), np.linspace(100, 150, 30)]), spread=0.002)
    day = lambda i: df.index[i].strftime("%Y-%m-%d")

    broker.sync("AAPL", df.iloc[:30])
    assert broker.queue("AAPL", "buy", day(29), atr=2.0, strategy="t")
    assert not broker.queue("AAPL", "buy", day(29), atr=2.0)  # no duplicates

    broker.sync("AAPL", df.iloc[:31])
    pos = broker.positions["AAPL"]
    assert pos["entry_date"] == day(30) and pos["entry_price"] == pytest.approx(df["open"].iloc[30])
    assert broker.cash("us") < 10_000

    broker.sync("AAPL", df)  # rally hits the target
    assert "AAPL" not in broker.positions
    trade = broker.ledger["history"][0]
    assert trade["exit_reason"] == "target" and trade["pnl"] > 0
    assert broker.cash("us") == pytest.approx(10_000 + trade["pnl"])


def test_paper_ledger_persists(broker, tmp_path):
    broker.queue("TCS.NS", "buy", "2024-01-01", atr=50)
    broker.save()
    again = PaperBroker(tmp_path / "ledger.json", {"us": 1, "india": 1}, COSTS)
    assert "TCS.NS" in again.pending
    assert again.cash("india") == 500_000


def test_sell_requires_position(broker):
    assert not broker.queue("AAPL", "sell", "2024-01-01")


def test_scanner_latest_bar_signal(down_then_up):
    strat = get_strategy("sma_crossover", use_trend_filter=False)
    sig = signal_for_latest_bar("AAPL", down_then_up, strat, RiskConfig(), equity=10_000)
    assert sig.action in ("BUY", "SELL", "HOLD")
    assert sig.extra["last_signal"] == "BUY"
    assert sig.market == "us"
