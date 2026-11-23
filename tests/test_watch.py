"""Chart alerts (`tradebot watch`): symbols, 2h/4h bars, closed-bar detection and alert-once logic."""
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import tradebot.services as services
from tradebot import timezones, watch
from tradebot.config import load_settings
from tradebot.strategies.base import Strategy


def utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def bars(start, periods, freq="1h"):
    idx = pd.date_range(start, periods=periods, freq=freq)
    close = 100 + np.arange(periods, dtype=float)
    return pd.DataFrame({"open": close - 0.5, "high": close + 1, "low": close - 1, "close": close,
                         "volume": 10.0}, index=idx)


class AtTimes(Strategy):
    """Fires BUY/SELL on the bars that open at the given timestamps."""
    name = "at_times"
    defaults = {"buys": (), "sells": ()}

    @property
    def warmup(self):
        return 0

    def generate(self, df):
        out = self._frame(df.index)
        for t in self.params["buys"]:
            if pd.Timestamp(t) in out.index:
                out.loc[pd.Timestamp(t)] = [1, "cross up"]
        for t in self.params["sells"]:
            if pd.Timestamp(t) in out.index:
                out.loc[pd.Timestamp(t)] = [-1, "cross down"]
        return out


@pytest.mark.parametrize("text,expected", [
    ("SOL/USDT", "SOL-USD"), ("solusdt", "SOL-USD"), ("SOL-USDT", "SOL-USD"), ("eth/usd", "ETH-USD"),
    ("BTC-USD", "BTC-USD"), (" aapl ", "AAPL"), ("UNI7083-USD", "UNI7083-USD"), ("RELIANCE.NS", "RELIANCE.NS"),
])
def test_normalize_symbol(text, expected):
    assert watch.normalize_symbol(text) == expected


def test_crypto_2h_bars_align_to_utc_midnight():
    out = watch.resample_bars(bars("2026-11-20 01:00", 8), "2h", "crypto")
    assert list(out.index.strftime("%H:%M")) == ["00:00", "02:00", "04:00", "06:00", "08:00"]
    first = out.iloc[1]  # 02:00 and 03:00 bars
    assert first["open"] == 100.5 and first["close"] == 102 and first["high"] == 103 and first["volume"] == 20


def test_stock_2h_bars_start_at_the_open_and_end_at_the_close():
    hourly = pd.concat([bars("2026-11-19 09:30", 7), bars("2026-11-20 09:30", 7)])  # 9:30 ... 15:30
    out = watch.resample_bars(hourly, "2h", "us")
    assert list(out.index.strftime("%d %H:%M")) == ["19 09:30", "19 11:30", "19 13:30", "19 15:30",
                                                    "20 09:30", "20 11:30", "20 13:30", "20 15:30"]
    assert watch.bar_end(pd.Timestamp("2026-11-19 15:30"), "2h", "us") == pd.Timestamp("2026-11-19 16:00")


def test_forming_bar_is_dropped():
    df = bars("2026-11-20 00:00", 6, "2h")  # last bar 10:00-12:00 UTC
    assert len(watch.closed_bars(df, "2h", "crypto", utc("2026-11-20 11:59"))) == 5
    assert len(watch.closed_bars(df, "2h", "crypto", utc("2026-11-20 12:00"))) == 6
    # US: the 15:30 hourly bar closes at 16:00 New York time (21:00 UTC in November)
    us = bars("2026-11-20 14:30", 2)
    assert len(watch.closed_bars(us, "1h", "us", utc("2026-11-20 20:59"))) == 1
    assert len(watch.closed_bars(us, "1h", "us", utc("2026-11-20 21:00"))) == 2


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = load_settings()
    settings["alerts"].update(console=True, macos=False, telegram=False,
                              watchlist_path=str(tmp_path / "wl.json"), watch_state_path=str(tmp_path / "ws.json"))
    monkeypatch.setattr(timezones, "PREFS_PATH", str(tmp_path / "prefs.json"))  # not your saved choice
    strat = {"s": AtTimes()}
    monkeypatch.setattr(services, "strategy_from", lambda s, *a, **k: strat["s"])
    return settings, strat


def test_alerts_only_new_closed_signals_once(setup):
    settings, strat = setup
    data = bars("2026-11-19 00:00", 30, "2h")  # through 2026-11-21 10:00 (forming until 12:00)
    load = lambda sym, interval: data  # noqa: E731
    watch.add_watch(settings, "SOL/USDT", "2h", "macd_trend", now=utc("2026-11-21 05:00"))
    strat["s"] = AtTimes(buys=("2026-11-20 00:00", "2026-11-21 04:00"), sells=("2026-11-21 10:00",))

    # The BUY before the alert was added is history; the 04:00-06:00 bar closed after it: alert.
    r1 = watch.check(settings, load, utc("2026-11-21 06:01"))
    assert [(s.symbol, s.action, s.bar_start) for s in r1.signals] == [("SOL-USD", "BUY", "Sat 21 Nov 04:00")]
    sig = r1.signals[0]
    assert sig.stop < sig.price < sig.target
    assert "BUY SOL-USD · 2h" in r1.alert(utc("2026-11-21 06:01")).text()

    # Same bar again: nothing. The SELL bar (10:00) is still forming at 11:00: nothing.
    assert watch.check(settings, load, utc("2026-11-21 08:05")).signals == []
    assert watch.check(settings, load, utc("2026-11-21 11:00")).signals == []
    r4 = watch.check(settings, load, utc("2026-11-21 12:03"))
    assert [s.action for s in r4.signals] == ["SELL"]
    assert watch.load_watch_state(settings)["SOL-USD|2h|macd_trend"]["last_signal"] == "SELL on the Sat 21 Nov 10:00 bar"


def test_not_due_skips_download_and_late_alerts_are_marked(setup):
    settings, strat = setup
    data = bars("2026-11-19 00:00", 30, "2h")
    calls = []

    def load(sym, interval):
        calls.append(sym)
        return data

    watch.add_watch(settings, "SOL-USD", "2h", "macd_trend", now=utc("2026-11-21 03:00"))
    strat["s"] = AtTimes(buys=("2026-11-21 02:00",))
    watch.check(settings, load, utc("2026-11-21 04:30"))
    assert len(calls) == 1
    watch.check(settings, load, utc("2026-11-21 05:00"))  # next 2h bar can't have closed yet
    assert len(calls) == 1
    # computer asleep from 04:00 to 12:30: the alert says it's late
    watch.remove_watch(settings, "SOL-USD", "2h")
    watch.add_watch(settings, "ETH-USD", "2h", "macd_trend", now=utc("2026-11-21 03:00"))
    text = watch.check(settings, load, utc("2026-11-21 12:30")).alert(utc("2026-11-21 12:30")).text()
    assert "Late: that bar closed 8 h 30 min ago" in text


def test_stocks_are_not_checked_when_the_market_is_closed(setup):
    settings, _ = setup
    watch.add_watch(settings, "AAPL", "1h", "sma_crossover", now=utc("2026-11-20 14:00"))
    boom = lambda *a: (_ for _ in ()).throw(AssertionError("should not download"))  # noqa: E731
    assert watch.check(settings, boom, utc("2026-11-22 18:00")).skipped == 1   # Sunday
    assert watch.check(settings, boom, utc("2026-11-23 03:00")).skipped == 1   # Monday 22:00 NY


def test_errors_are_recorded_and_removed_entries_forgotten(setup):
    settings, _ = setup
    watch.add_watch(settings, "NOPE-USD", "1h", "macd_trend", now=utc("2026-11-20 00:00"))
    r = watch.check(settings, lambda *a: bars("2026-11-20", 0), utc("2026-11-21 00:00"))
    assert "NOPE-USD|1h|macd_trend" in r.errors and r.checked == 0
    assert "error" in watch.load_watch_state(settings)["NOPE-USD|1h|macd_trend"]
    watch.remove_watch(settings, "NOPE-USD", "1h")
    watch.check(settings, lambda *a: bars("2026-11-20", 0), utc("2026-11-21 01:00"))
    assert watch.load_watch_state(settings) == {}


def test_add_watch_validates_and_dedupes(setup):
    settings, _ = setup
    with pytest.raises(ValueError):
        watch.add_watch(settings, "SOL-USD", "1m", "macd_trend")
    with pytest.raises(ValueError):
        watch.add_watch(settings, "SOL-USD", "2h", "nope")
    watch.add_watch(settings, "SOL/USDT", "2h", "macd_trend")
    watch.add_watch(settings, "SOL-USD", "2h", "macd_trend")
    assert len(watch.load_watchlist(settings)) == 1
    assert watch.is_watched(settings, "solusdt", "2h", "macd_trend")


def test_alert_times_follow_the_chosen_time_zone(setup):
    settings, strat = setup
    data = bars("2026-11-19 00:00", 30, "2h")
    watch.add_watch(settings, "SOL-USD", "2h", "macd_trend", now=utc("2026-11-21 05:00"))
    strat["s"] = AtTimes(buys=("2026-11-21 04:00",))
    timezones.save_pref("America/New_York")
    sig = watch.check(settings, lambda *a: data, utc("2026-11-21 06:01")).signals[0]
    assert (sig.bar_start, sig.tz) == ("Fri 20 Nov 23:00", "New York")   # 04:00 UTC = 23:00 EST


def test_time_zone_preference_and_conversion(tmp_path, monkeypatch):
    monkeypatch.setattr(timezones, "PREFS_PATH", str(tmp_path / "prefs.json"))
    assert timezones.load_pref() == timezones.EXCHANGE
    with pytest.raises(ValueError):
        timezones.save_pref("Mars/Olympus")
    timezones.save_pref("Asia/Kolkata")
    assert timezones.load_pref() == "Asia/Kolkata"
    assert timezones.resolve(timezones.EXCHANGE, "crypto") == "UTC"
    assert timezones.resolve(timezones.EXCHANGE, "us") == "America/New_York"
    idx = pd.DatetimeIndex(["2026-11-20 09:30", "2026-07-20 09:30"])   # New York, winter and summer
    out = timezones.convert_index(idx, "us", "UTC")
    assert list(out.strftime("%H:%M")) == ["14:30", "13:30"]
    assert timezones.convert_index(idx, "us", "America/New_York").equals(idx)
