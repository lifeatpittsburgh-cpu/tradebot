"""Daily job, alert channels and the launchd schedule (no network, no launchctl)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

import tradebot.alerts as alerts
import tradebot.daily as daily
from conftest import Scripted, make_ohlcv
from tradebot.alerts import Alert
from tradebot.alerts.macos import _quote
from tradebot.config import load_settings
from tradebot.schedule import build_plist, is_due, latest_slot, parse_time, timing_notes

N = 60


class FakeProvider:
    def __init__(self, frames):
        self.frames = frames

    def get_history(self, symbol, start, end=None):
        return self.frames[symbol]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = load_settings()
    settings["active_markets"] = ["us"]
    settings["watchlists"]["us"] = ["AAA", "BBB"]
    settings["paper"]["ledger_path"] = str(tmp_path / "ledger.json")
    settings["broker"]["enabled"] = False  # never reach Alpaca from a test, whatever settings.yaml says
    settings["ai_study"] = {"enabled": False}  # never call the Anthropic API from a test
    settings["alerts"].update(console=True, macos=False, telegram=False,
                              state_path=str(tmp_path / "alerted.json"),
                              last_run_path=str(tmp_path / "last_run.json"))
    # BUY on bar N-1 for every symbol
    import tradebot.services as services

    monkeypatch.setattr(services, "strategy_from", lambda s, *a, **k: Scripted(buys=(N - 1,)))
    full = make_ohlcv(100 + np.arange(N + 1, dtype=float))
    return settings, full


def test_daily_alerts_each_signal_once_then_reports_fill(setup):
    settings, full = setup
    first = FakeProvider({"AAA": full.iloc[:N], "BBB": full.iloc[:N]})

    r1 = daily.run_daily(settings, first)
    assert r1.ok and r1.new_actions == ["BUY AAA", "BUY BBB"]
    assert any(e.startswith("QUEUED BUY AAA") for e in r1.events)

    # Same candles again (weekend / holiday / second run the same evening): nothing new to send.
    r2 = daily.run_daily(settings, first)
    assert r2.new_signals == [] and r2.repeated_signals == 2 and r2.events == []
    assert r2.alert().lines == []

    # Next candle: the queued orders fill at its open and that's what gets reported.
    r3 = daily.run_daily(settings, FakeProvider({"AAA": full, "BBB": full}))
    assert r3.new_signals == []
    assert sum(e.startswith("BOUGHT") for e in r3.events) == 2
    assert "2 paper-account update" in r3.alert().summary


def test_daily_failure_when_no_data(setup):
    settings, _ = setup

    class Broken:
        def get_history(self, symbol, start, end=None):
            raise ConnectionError("no internet")

    report = daily.run_daily(settings, Broken())
    assert not report.ok
    assert report.alert().title.endswith("FAILED")
    daily.save_last_run(settings, report)
    assert daily.load_last_run(settings)["ok"] is False


def test_broken_channel_does_not_stop_others(monkeypatch, capsys):
    got = []

    class Good:
        def send(self, alert):
            got.append(alert.title)

    class Bad:
        def send(self, alert):
            raise RuntimeError("down")

    monkeypatch.setattr(alerts, "channels_from", lambda s: [Bad(), Good()])
    failures = alerts.send(Alert("t", ["line"]), {})
    assert got == ["t"] and failures[0][0] == "Bad"


def test_empty_alert_is_not_sent(monkeypatch):
    monkeypatch.setattr(alerts, "channels_from", lambda s: pytest.fail("should not build channels"))
    assert alerts.send(Alert("t", []), {}) == []


def test_mac_notification_quoting():
    assert _quote('say "hi" \\ bye') == '"say \\"hi\\" \\\\ bye"'


def test_schedule_time_and_plist():
    assert parse_time("20:15") == (20, 15) and parse_time("7:05") == (7, 5)
    for bad in ("24:00", "20:60", "8pm", ""):
        with pytest.raises(ValueError):
            parse_time(bad)
    plist = build_plist("20:15", "America/New_York")
    assert plist["StartInterval"] == 300 and "StartCalendarInterval" not in plist
    assert plist["ProgramArguments"][-6:] == ["daily", "--if-due", "--at", "20:15", "--tz", "America/New_York"]


NY = ZoneInfo("America/New_York")


def test_due_once_per_evening_with_catch_up():
    state = {}
    before = datetime(2026, 11, 23, 20, 0, tzinfo=NY)       # Monday 20:00: Sunday's slot not run yet
    due, slot = is_due(before, "20:15", "America/New_York", state)
    assert due and slot == datetime(2026, 11, 22, 20, 15, tzinfo=NY)   # catch-up for the missed evening
    state["last_success_slot"] = slot.isoformat()
    assert not is_due(before, "20:15", "America/New_York", state)[0]

    after = datetime(2026, 11, 23, 20, 16, tzinfo=NY)        # Monday 20:16: Monday's slot is due
    due, slot = is_due(after, "20:15", "America/New_York", state)
    assert due and slot.day == 23
    state["last_success_slot"] = slot.isoformat()
    assert not is_due(after.replace(hour=23), "20:15", "America/New_York", state)[0]


def test_slot_uses_schedule_timezone_not_the_macs():
    # 01:20 UTC on 23 Nov = 20:20 New York on 22 Nov, whatever timezone the Mac is in.
    now = datetime(2026, 11, 23, 1, 20, tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Kolkata"))
    assert latest_slot(now, "20:15", "America/New_York") == datetime(2026, 11, 22, 20, 15, tzinfo=NY)


@pytest.mark.parametrize("month", [1, 7])  # winter (EST) and summer (EDT)
def test_default_time_suits_both_markets(month):
    now = datetime(2026, month, 15, 12, 0, tzinfo=NY)
    done = {"last_success_slot": now.isoformat()}  # today's run already happened
    nxt, us, crypto = timing_notes("20:15", "America/New_York", now=now, state=done)
    assert nxt.startswith("Next run:")
    assert "after the close" in us
    assert crypto.startswith("Crypto: 0")  # 00:15 or 01:15 UTC: just after the crypto candle closed
