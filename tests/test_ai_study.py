"""AI market study: veto rules, caching, failure handling (no network, no Anthropic API)."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import tradebot.ai_study as ai
import tradebot.daily as daily
from conftest import Scripted, make_ohlcv
from tradebot.config import load_settings
from tradebot.signals import Signal

NOW = datetime(2026, 11, 23, 21, 0, tzinfo=timezone.utc)


def view(v, c=0.8, r="news"):
    return {"view": v, "confidence": c, "reason": r}


def buy(sym="AAA", market="us"):
    return Signal(sym, market, "2026-11-23", "BUY", 100.0, "scripted", reason="cross up",
                  stop_loss=95.0, target=110.0, quantity=3)


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    s["ai_study"] = {"enabled": True, "path": str(tmp_path / "ai.json")}
    return s


def review(symbols=None, markets=None):
    return ai.Review(study={"symbols": symbols or {}, "markets": markets or {}})


def test_bearish_news_holds_back_buy(settings):
    r = review({"AAA": view("bearish", 0.8, "guidance cut")})
    out = ai.reviewer(settings, r)(buy())
    assert out.action == "HOLD" and out.stop_loss is None and out.quantity is None
    assert "guidance cut" in out.reason and "cross up" in out.reason
    assert r.held_back == ["BUY AAA: AAA news bearish 0.80: guidance cut"]


def test_weak_or_non_bearish_views_let_buy_through(settings):
    check = ai.reviewer(settings, review({"AAA": view("bearish", 0.5), "BBB": view("bullish", 0.9)}))
    a, b = check(buy("AAA")), check(buy("BBB"))
    assert a.action == b.action == "BUY"
    assert b.extra["ai"]["view"] == "bullish"
    assert "AI bullish 0.90" in b.summary()


def test_sell_is_never_blocked(settings):
    sell = Signal("AAA", "us", "2026-11-23", "SELL", 100.0, "scripted")
    out = ai.reviewer(settings, review({"AAA": view("bearish", 0.99)}, {"us": view("bearish", 0.99)}))(sell)
    assert out.action == "SELL"


def test_market_mood_veto_and_switches(settings):
    r = review(markets={"us": view("bearish", 0.7, "VIX spike")})
    assert ai.reviewer(settings, r)(buy()).action == "HOLD"
    settings["ai_study"]["market_veto"] = False
    assert ai.reviewer(settings, review(markets={"us": view("bearish", 0.7)}))(buy()).action == "BUY"
    settings["ai_study"].update(market_veto=True, veto=False)
    assert ai.reviewer(settings, review({"AAA": view("bearish", 0.99)}))(buy()).action == "BUY"


def test_failed_study_changes_nothing(settings):
    r = ai.Review(error="no key")
    assert ai.reviewer(settings, r)(buy()) == buy()
    assert r.lines() == ["AI study unavailable (no key); signals used as-is"]


def test_normalize_clamps_and_drops_unknown():
    data = {"symbols": [{"symbol": "AAA", "view": "weird", "confidence": 7, "reason": "x"},
                        {"symbol": "ZZZ", "view": "bullish", "confidence": 0.5, "reason": "y"}],
            "markets": [{"market": "us", "view": "bearish", "confidence": -1, "reason": "z"}]}
    out = ai.normalize(data, ["AAA"], ["us"])
    assert out["symbols"] == {"AAA": {"view": "neutral", "confidence": 1.0, "reason": "x"}}
    assert out["markets"]["us"]["confidence"] == 0.0


def fake_inputs():
    calls = []

    def ask(inputs, cfg):
        calls.append(sorted(inputs["symbols"]))
        return ({"symbols": {s: view("neutral", 0.3) for s in inputs["symbols"]},
                 "markets": {m: view("neutral", 0.3) for m in inputs["markets"]}},
                {"input_tokens": 100, "output_tokens": 50})

    kw = dict(ask=ask, headlines=lambda q, n: [{"title": f"{q} news", "publisher": "p", "date": "2026-11-23"}],
              numbers=lambda ms: {m: "SPY 5d +1%" for m in ms})
    return calls, kw


def test_study_is_cached_until_stale_or_new_symbols(settings):
    calls, kw = fake_inputs()
    ai.run_study(settings, ["AAA", "BTC-USD"], now=NOW, **kw)
    ai.run_study(settings, ["AAA"], now=NOW + timedelta(hours=2), **kw)          # reused
    assert len(calls) == 1
    ai.run_study(settings, ["AAA", "CCC"], now=NOW + timedelta(hours=2), **kw)   # new symbol
    ai.run_study(settings, ["AAA", "CCC"], now=NOW + timedelta(hours=15), **kw)  # stale
    ai.run_study(settings, ["AAA", "CCC"], now=NOW + timedelta(hours=15), force=True, **kw)
    assert len(calls) == 4
    saved = ai.load(settings)
    assert saved["usage"]["input_tokens"] == 100 and set(saved["markets"]) == {"us"}


def test_crypto_news_searched_by_coin_name():
    assert ai.news_query("SOL-USD") == "Solana" and ai.news_query("AAPL") == "AAPL"


def test_no_headlines_is_an_error_not_a_call(settings):
    calls, kw = fake_inputs()
    kw["headlines"] = lambda q, n: []
    r = ai.review_for(settings, ["AAA"], now=NOW, **kw)
    assert r.error.startswith("no headlines") and not calls


def test_disabled_study_does_nothing(settings):
    settings["ai_study"]["enabled"] = False
    assert not ai.review_for(settings, ["AAA"]).active


def test_daily_run_holds_back_vetoed_buy(settings, tmp_path, monkeypatch):
    import tradebot.services as services

    settings["active_markets"] = ["us"]
    settings["watchlists"]["us"] = ["AAA", "BBB"]
    settings["paper"]["ledger_path"] = str(tmp_path / "ledger.json")
    settings["broker"]["enabled"] = False
    settings["alerts"].update(console=True, macos=False, telegram=False,
                              state_path=str(tmp_path / "alerted.json"), last_run_path=str(tmp_path / "last.json"))
    n = 60
    monkeypatch.setattr(services, "strategy_from", lambda s, *a, **k: Scripted(buys=(n - 1,)))
    frame = make_ohlcv(100 + np.arange(n, dtype=float))

    class Provider:
        def get_history(self, symbol, start, end=None):
            return frame

    r = review({"AAA": view("bearish", 0.9, "SEC probe"), "BBB": view("bullish", 0.7, "beat")},
               {"us": view("neutral", 0.4, "mixed")})
    report = daily.run_daily(settings, Provider(), review=r)
    assert report.new_actions == ["BUY BBB"]
    assert not any("AAA" in e for e in report.events)            # nothing queued in the paper account
    text = report.alert().text()
    assert "Held back BUY AAA" in text and "SEC probe" in text and "AI bullish 0.70: beat" in text
