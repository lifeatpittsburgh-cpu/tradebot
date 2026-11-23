"""Phase 5 broker integration against a simulated Alpaca API (no network)."""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from conftest import make_ohlcv
from tradebot.broker.alpaca import AlpacaClient, AlpacaError, from_alpaca, round_price, to_alpaca
from tradebot.broker.executor import BrokerConfig, Executor
from tradebot.config import load_settings
from tradebot.signals import Signal


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.content = b"x" if data is not None else b""
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeAlpaca:
    """Just enough of /v2 to exercise the executor; records every write."""

    def __init__(self, equity=100_000.0, last_equity=100_000.0):
        self.account = {"equity": str(equity), "last_equity": str(last_equity), "cash": str(equity),
                        "buying_power": str(equity), "trading_blocked": False, "account_blocked": False}
        self.positions, self.orders, self.posts, self.deletes = [], [], [], []
        self.untradable, self.fail_submit = set(), False

    def get(self, url, headers=None, params=None, timeout=None):
        path = url.split(".markets", 1)[1]
        if path == "/v2/account":
            return Resp(200, self.account)
        if path == "/v2/positions":
            return Resp(200, self.positions)
        if path == "/v2/orders":
            return Resp(200, self.orders)
        if path.startswith("/v2/assets/"):
            sym = path.rsplit("/", 1)[1]
            return Resp(200, {"symbol": sym, "tradable": sym not in self.untradable})
        return Resp(404, {"message": "not found"})

    def post(self, url, headers=None, json=None, timeout=None):
        if self.fail_submit:
            return Resp(403, {"message": "insufficient buying power"})
        self.posts.append(json)
        return Resp(200, {"id": f"o{len(self.posts)}", **json})

    def delete(self, url, headers=None, timeout=None):
        self.deletes.append(url.split(".markets", 1)[1])
        return Resp(204, None)


def sig(symbol, action, price=100.0, date="2026-11-20", atr=2.0):
    return Signal(symbol=symbol, market="crypto" if symbol.endswith("-USD") else "us", date=date, action=action,
                  price=price, strategy="t", reason="test", extra={"atr": atr})


@pytest.fixture
def setup(tmp_path):
    def make(mode="paper", **kw):
        settings = load_settings()
        settings["broker"].update(enabled=True, mode=mode, **kw)
        settings["risk"].update(max_per_sector=None)
        fake = FakeAlpaca()
        client = AlpacaClient(mode, "KEY", "SECRET", session=fake)
        return Executor(settings, client, state_dir=tmp_path), fake
    return make


def test_symbol_mapping_and_price_rounding():
    assert to_alpaca("BTC-USD") == "BTC/USD" and to_alpaca("BRK-B") == "BRK.B" and to_alpaca("AAPL") == "AAPL"
    assert from_alpaca("BTCUSD", "crypto") == "BTC-USD" and from_alpaca("BTC/USD") == "BTC-USD"
    assert from_alpaca("BRK.B", "us_equity") == "BRK-B"
    assert round_price(187.4567, False) == 187.46 and round_price(0.123456789, True) == 0.123457


def test_paper_buy_sends_bracket_for_stocks_and_market_for_crypto(setup):
    ex, fake = setup()
    res = ex.run([sig("AAPL", "BUY", 200.0, atr=4.0), sig("BTC-USD", "BUY", 60_000.0, atr=2_000.0)])
    assert len(res.submitted) == 2 and not res.errors
    stock, coin = fake.posts
    assert stock["order_class"] == "bracket" and stock["symbol"] == "AAPL"
    assert stock["stop_loss"]["stop_price"] == "192.0" and stock["take_profit"]["limit_price"] == "216.0"
    assert stock["qty"] == "10"                       # $2,000 per-order cap / $200
    assert coin["symbol"] == "BTC/USD" and coin["type"] == "market" and "order_class" not in coin
    assert float(coin["qty"]) == pytest.approx(2000 / 60_000, abs=1e-4)
    assert stock["client_order_id"] == "tb-p-AAPL-2026-11-20-buy"

    again = ex.run([sig("AAPL", "BUY", 200.0, atr=4.0)])  # same candle again: already acted on
    assert again.submitted == [] and len(fake.posts) == 2


def test_sell_cancels_open_orders_then_sells_everything(setup):
    ex, fake = setup()
    fake.positions = [{"symbol": "AAPL", "qty": "10", "avg_entry_price": "200", "asset_class": "us_equity"}]
    fake.orders = [{"id": "parent", "symbol": "AAPL", "side": "buy", "type": "market", "status": "held",
                    "legs": [{"id": "tp", "side": "sell"}, {"id": "sl", "side": "sell"}]}]
    res = ex.run([sig("AAPL", "SELL", 210.0)])
    assert fake.deletes == ["/v2/orders/parent", "/v2/orders/tp", "/v2/orders/sl"]
    assert fake.posts[-1] == {"symbol": "AAPL", "qty": "10", "side": "sell", "type": "market",
                              "time_in_force": "day", "client_order_id": "tb-p-AAPL-2026-11-20-sell"}
    assert len(res.submitted) == 1


def test_daily_loss_limit_blocks_buys_but_not_sells(setup):
    ex, fake = setup()
    fake.account.update(equity="96000", last_equity="100000")
    fake.positions = [{"symbol": "MSFT", "qty": "5", "avg_entry_price": "400", "asset_class": "us_equity"}]
    res = ex.run([sig("AAPL", "BUY"), sig("MSFT", "SELL", 390.0)])
    assert [p["side"] for p in fake.posts] == ["sell"]
    assert any("Daily loss limit" in n for n in res.notes)


def test_kill_switch(setup):
    ex, fake = setup()
    ex.halt("testing")
    res = ex.run([sig("AAPL", "BUY")])
    assert fake.posts == [] and "HALTED" in res.notes[0]
    assert ex.resume() and ex.run([sig("AAPL", "BUY")]).submitted


def test_order_limits_and_untradable_assets(setup):
    ex, fake = setup(max_orders_per_run=2)
    fake.untradable = {"XOM"}
    res = ex.run([sig(s, "BUY") for s in ("XOM", "AAPL", "MSFT", "NVDA")])
    assert [p["symbol"] for p in fake.posts] == ["AAPL", "MSFT"]
    assert any("isn't tradable" in n for n in res.notes) and any("max 2 new buys" in n for n in res.notes)


def test_failed_order_is_retried_next_run(setup):
    ex, fake = setup()
    fake.fail_submit = True
    assert ex.run([sig("AAPL", "BUY")]).errors
    fake.fail_submit = False
    assert ex.run([sig("AAPL", "BUY")]).submitted


def test_crypto_gets_protective_stop_then_takes_profit(setup):
    ex, fake = setup()
    ex.run([sig("BTC-USD", "BUY", 60_000.0, atr=2_000.0)])       # stop 56,000, target 68,000
    fake.positions = [{"symbol": "BTCUSD", "qty": "0.0333", "avg_entry_price": "60000", "asset_class": "crypto"}]
    bars = make_ohlcv(np.full(30, 61_000.0), spread=0.001)
    res = ex.run([], {"BTC-USD": bars})
    stop = fake.posts[-1]
    assert stop["type"] == "stop_limit" and stop["stop_price"] == "56000.0" and stop["limit_price"] == "55440.0"
    fake.orders = [{"id": "s1", "symbol": "BTCUSD", "asset_class": "crypto", "side": "sell", "type": "stop_limit",
                    "status": "new"}]
    assert ex.run([], {"BTC-USD": bars}).submitted == []          # stop already in place
    high = make_ohlcv(np.r_[np.full(29, 61_000.0), 69_000.0], spread=0.001)
    res = ex.run([], {"BTC-USD": high})
    assert fake.deletes[-1] == "/v2/orders/s1" and fake.posts[-1]["type"] == "market"
    assert "take_profit" in res.submitted[0]


def test_live_mode_only_proposes_and_needs_typed_yes(setup):
    ex, fake = setup(mode="live")
    res = ex.run([sig("AAPL", "BUY"), sig("MSFT", "BUY", 400.0)])
    assert fake.posts == [] and len(res.proposed) == 2

    assert ex.confirm(interactive=False).errors and fake.posts == []   # e.g. the scheduled job
    answers = iter(["yes", "no"])
    res = ex.confirm(ask=lambda prompt: next(answers), interactive=True)
    assert [p["symbol"] for p in fake.posts] == ["AAPL"] and fake.posts[0]["client_order_id"].startswith("tb-l-")
    assert ex.confirm(interactive=True).notes == ["No live orders waiting."]


def test_stale_live_proposals_are_discarded(setup):
    ex, fake = setup(mode="live")
    ex.run([sig("AAPL", "BUY")])
    path = ex._path("live_proposals.json")
    data = json.loads(path.read_text())
    data["created"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    path.write_text(json.dumps(data))
    res = ex.confirm(ask=lambda p: "yes", interactive=True)
    assert fake.posts == [] and "discarded" in res.notes[0]


def test_live_needs_its_own_keys(monkeypatch):
    for k in ("ALPACA_LIVE_KEY_ID", "ALPACA_LIVE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "pk")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "ps")
    monkeypatch.setattr("tradebot.broker.alpaca.load_dotenv", lambda: None)
    with pytest.raises(AlpacaError):
        AlpacaClient.from_env("live", session=FakeAlpaca())
    assert AlpacaClient.from_env("paper", session=FakeAlpaca()).base == "https://paper-api.alpaca.markets"


def test_config_and_daily_step_disabled_by_default():
    from tradebot.daily import broker_step

    settings = load_settings()
    assert BrokerConfig.from_settings(settings).enabled is False and broker_step(settings) == []
    settings["broker"]["mode"] = "real"
    with pytest.raises(ValueError):
        BrokerConfig.from_settings(settings)
