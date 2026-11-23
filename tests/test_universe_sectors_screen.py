"""Point-in-time universe, slot priority, sector caps (backtest and paper) and the fundamentals score."""
import numpy as np
import pandas as pd

from conftest import Scripted, make_ohlcv
from tradebot.backtest import run_portfolio_backtest
from tradebot.fundamentals import score
from tradebot.paper import PaperBroker
from tradebot.risk import RiskConfig
from tradebot.universe import Universe, _constituents, to_yahoo

RISK = RiskConfig(risk_per_trade=0.05, atr_stop_mult=5, reward_risk=50, max_position_pct=0.2)


def test_ticker_conversion_and_table_parsing():
    assert to_yahoo("brk.b") == "BRK-B"
    rows = "".join(f"<tr><td>T{i}.A</td><td>Co {i}</td><td>Energy</td></tr>" for i in range(320))
    html = f"<table><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr>{rows}</table>"
    df = _constituents(html)
    assert len(df) == 320 and df.iloc[0].tolist() == ["T0-A", "Co 0", "Energy"]


def test_universe_membership_is_point_in_time():
    snaps = pd.DataFrame({
        "snapshot": ["2020-01-01", "2020-01-01", "2020-04-01", "2020-04-01"],
        "symbol": ["OLD", "KEEP", "NEW", "KEEP"],
        "sector": ["Energy", "Financials", "Information Technology", "Financials"],
    })
    u = Universe(snaps)
    assert u.members("2020-02-15") == {"OLD", "KEEP"} and u.members("2020-05-01") == {"NEW", "KEEP"}
    idx = pd.bdate_range("2019-12-20", "2020-06-30")
    old, new = u.member_mask("OLD", idx), u.member_mask("NEW", idx)
    assert not old[:"2019-12-31"].any() and old["2020-01-02":"2020-03-31"].all() and not old["2020-04-01":].any()
    assert new["2020-04-01":].all() and not new[:"2020-03-31"].any()
    assert u.sectors()["NEW"] == "Information Technology"


def two_symbol_data():
    return {s: make_ohlcv(100 + np.arange(200, dtype=float)) for s in ("AAA", "BBB")}


def test_priority_decides_who_gets_the_last_slot():
    data = two_symbol_data()
    strat = Scripted(buys=(30,), sells=())
    pr = {"AAA": pd.Series(1.0, index=data["AAA"].index), "BBB": pd.Series(2.0, index=data["BBB"].index)}
    res = run_portfolio_backtest(data, strat, risk=RISK, max_positions=1, priority=pr)
    assert [t.symbol for t in res.trades] == ["BBB"]
    res = run_portfolio_backtest(data, strat, risk=RISK, max_positions=1)  # default: watchlist order
    assert [t.symbol for t in res.trades] == ["AAA"]


def test_sector_cap_in_backtest():
    data = two_symbol_data()
    strat = Scripted(buys=(30,), sells=())
    same = {"AAA": "Energy", "BBB": "Energy"}
    capped = run_portfolio_backtest(data, strat, risk=RISK, sector_of=same, max_per_sector=1)
    assert len(capped.trades) == 1 and capped.metrics["sector_skipped"] == 1
    free = run_portfolio_backtest(data, strat, risk=RISK, sector_of={"AAA": "Energy", "BBB": "Utilities"},
                                  max_per_sector=1)
    assert len(free.trades) == 2


def test_sector_cap_in_paper_broker(tmp_path):
    risk = RiskConfig(risk_per_trade=0.05, atr_stop_mult=2, max_per_sector=1)
    b = PaperBroker(tmp_path / "l.json", {"us": 10_000}, {"us": {}}, risk, sectors={"AAA": "Energy", "BBB": "Energy"})
    df = make_ohlcv(100 + np.arange(60, dtype=float))
    for sym in ("AAA", "BBB"):
        b.queue(sym, "buy", str(df.index[40].date()), atr=2.0)
        b.sync(sym, df.iloc[:42])
    assert list(b.positions) == ["AAA"]
    assert any("SKIPPED BUY BBB: already holding 1 Energy" in e for e in b.events)


def test_fundamental_score_is_sector_relative_and_drops_losses():
    raw = pd.DataFrame({
        "shortName": ["Good Tech", "Meh Tech", "Good Bank", "Loss Co"],
        "sector": ["Information Technology", "Information Technology", "Financials", "Information Technology"],
        "marketCap": [1e11] * 4, "trailingPE": [20, 40, 10, -5], "forwardPE": [18, 35, 9, None],
        "trailingEps": [5, 2, 3, -1], "returnOnEquity": [0.4, 0.1, 0.15, -0.2],
        "profitMargins": [0.3, 0.05, 0.25, -0.1], "revenueGrowth": [0.2, 0.01, 0.05, 0.5],
        "earningsGrowth": [0.25, 0.0, 0.05, None], "debtToEquity": [20, 150, 300, 10], "dividendYield": [0, 0, 3, 0],
    }, index=["GT", "MT", "GB", "LC"])
    d = score(raw)
    assert "LC" not in d.index                       # unprofitable
    assert d.loc["GT", "score"] > d.loc["MT", "score"]
    assert np.isnan(d.loc["GB", "safety"])           # no debt penalty for a bank
    assert d.loc["GB", "quality"] == 1.0             # best in its own sector (alone there)


def test_strategy_per_market():
    from tradebot.services import strategies_label, strategy_from, strategy_name_for

    settings = {"strategy": {"name": "sma_crossover", "params": {"fast": 10},
                             "by_market": {"crypto": "macd_trend"}}, "active_markets": ["us", "crypto"]}
    assert strategy_name_for(settings, "us") == "sma_crossover"
    assert strategy_name_for(settings, "crypto") == "macd_trend"
    assert strategy_name_for(settings, "india") == "sma_crossover"
    assert strategy_from(settings, market="us").params["fast"] == 10      # default's params apply
    assert strategy_from(settings, market="crypto").name == "macd_trend"
    assert strategy_from(settings, "rsi_reversion", market="crypto").name == "rsi_reversion"  # explicit wins
    assert strategies_label(settings) == "us: sma_crossover, crypto: macd_trend"
