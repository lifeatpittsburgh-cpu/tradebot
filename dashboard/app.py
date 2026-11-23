"""TradeBot dashboard.

Run with:  tradebot dashboard      (or: streamlit run dashboard/app.py)
"""
from __future__ import annotations

import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))      # works even without `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent))
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

import streamlit.components.v1 as components  # noqa: E402

from charts import chart_html, drawdown_chart, fmt_price, line_chart, price_chart  # noqa: E402
from tradebot import indicators as ind  # noqa: E402
from tradebot.backtest import run_portfolio_backtest  # noqa: E402
from tradebot.config import (CURRENCY, active_markets, is_tradable, load_settings,  # noqa: E402
                             market_of, resolve_path)
from tradebot.data import INTRADAY_MAX_DAYS, CachedProvider, YFinanceProvider  # noqa: E402
from tradebot.risk import RiskConfig  # noqa: E402
from tradebot.scanner import signal_for_latest_bar  # noqa: E402
from tradebot.services import (DISCLAIMER, accounts_frame, broker_from, history_frame,  # noqa: E402
                               pending_frame, positions_frame, run_paper_update, signals_frame,
                               strategy_from, strategy_name_for, symbols_for)
from tradebot.daily import load_last_run  # noqa: E402
from tradebot.strategies import REGISTRY  # noqa: E402
from tradebot import watch as chart_alerts  # noqa: E402

LOCAL_TZ = datetime.now().astimezone().tzinfo

st.set_page_config(page_title="TradeBot", page_icon="📈", layout="wide")

MARKET_NAMES = {"us": "US stocks", "crypto": "Crypto", "india": "India"}
ACTION_COLORS = {"BUY": "background-color: rgba(22,163,74,0.18); color: #15803d; font-weight: 600",
                 "SELL": "background-color: rgba(220,38,38,0.15); color: #b91c1c; font-weight: 600",
                 "HOLD": "color: #64748b"}

settings = load_settings()
if "nonce" not in st.session_state:
    st.session_state.nonce = 0  # bumped by "Refresh data" to bypass every cache


# ---- data access (cached) ----------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def load_history(symbol: str, start_iso: str, nonce: int) -> pd.DataFrame:
    d = settings["data"]
    ttl = 0 if nonce else d.get("cache_ttl_minutes", 60)
    provider = CachedProvider(YFinanceProvider(), resolve_path(d["cache_dir"]), ttl)
    return provider.get_history(symbol, date.fromisoformat(start_iso))


@st.cache_data(ttl=60, show_spinner=False)
def load_intraday(symbol: str, interval: str, days: int, nonce: int) -> pd.DataFrame:
    """Minute/hour bars for the chart. Cached for a minute only; the last bar may still be forming.
    2h and 4h bars are built from hourly bars, the same way the chart alerts build them."""
    src = CHART_SOURCE.get(interval, interval)
    df = YFinanceProvider().get_intraday(symbol, src, days)
    return chart_alerts.resample_bars(df, interval, market_of(symbol)) if src != interval else df


def no_data_message(symbol: str) -> str:
    msg = f"Yahoo Finance returned no data for **{symbol}**. Check the ticker."
    if market_of(symbol) == "crypto":
        msg += (" Some coins use a numbered Yahoo ticker because the plain name is taken, e.g. Uniswap is "
                "`UNI7083-USD`. Search the coin on finance.yahoo.com to find its exact ticker.")
    return msg


def history(symbol: str, days: int) -> pd.DataFrame:
    return load_history(symbol, (date.today() - timedelta(days=days)).isoformat(), st.session_state.nonce)


@st.cache_data(ttl=900, show_spinner=False)
def scan_market(market: str, strategy_name: str, nonce: int):
    strategy = strategy_from(settings, strategy_name)
    risk = RiskConfig.from_settings(settings)
    equity = settings["paper"]["starting_capital"].get(market)
    sigs, errors = [], {}
    for sym in symbols_for(settings, market):
        try:
            df = load_history(sym, (date.today() - timedelta(days=settings["data"]["lookback_days"])).isoformat(), nonce)
            if len(df) < strategy.warmup + 2:
                errors[sym] = f"only {len(df)} bars of history"
                continue
            sigs.append(signal_for_latest_bar(sym, df, strategy, risk, equity))
        except Exception as exc:
            errors[sym] = f"{type(exc).__name__}: {exc}"
    return signals_frame(sigs), errors


def show_price_chart(fig) -> None:
    """Price chart with time-only zoom (wheel, drag or +/-) and price panels that re-fit to the view."""
    theme = getattr(getattr(st.context, "theme", None), "type", None)
    components.html(chart_html(fig, dark=theme == "dark"), height=int(fig.layout.height or 860) + 20)
    st.caption("Drag the chart to move through time. Scroll or use + / − in the toolbar to zoom. "
               "Double-click to reset the view.")


def price_fmt(market: str) -> str:
    return "%.4f" if market == "crypto" else "%.2f"


# ---- sidebar -------------------------------------------------------------------------------
with st.sidebar:
    st.title("📈 TradeBot")
    markets = active_markets(settings)
    market = st.radio("Market", markets, format_func=lambda m: MARKET_NAMES.get(m, m))
    names = list(REGISTRY)
    strategy_name = st.selectbox("Strategy", names, index=names.index(strategy_name_for(settings, market)),
                                 key=f"strategy_{market}",
                                 help="Rules used for signals, charts and backtests. Defaults to the strategy "
                                      "configured for this market (strategy.by_market in settings.yaml).")
    st.caption(REGISTRY[strategy_name].__doc__.strip().split("\n\n")[0].replace("\n", " "))
    if st.button("🔄 Refresh market data", width="stretch",
                 help="Re-download prices now instead of using the cached copy."):
        st.session_state.nonce += 1
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.caption(f"Daily candles from Yahoo Finance. Today's unfinished candle is ignored. {DISCLAIMER}")

tab_signals, tab_chart, tab_alerts, tab_backtest, tab_paper = st.tabs(
    ["🚦 Signals", "📊 Chart", "🔔 Chart alerts", "🧪 Backtest", "💼 Paper portfolio"])


# ---- Signals ---------------------------------------------------------------------------------
with tab_signals:
    st.subheader(f"Signals: {MARKET_NAMES[market]}")
    with st.spinner("Scanning watchlist..."):
        table, errors = scan_market(market, strategy_name, st.session_state.nonce)
    if table.empty:
        st.warning("No signals: the watchlist is empty or no data could be loaded.")
    else:
        counts = table["action"].value_counts()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("BUY", int(counts.get("BUY", 0)), border=True)
        c2.metric("SELL", int(counts.get("SELL", 0)), border=True)
        c3.metric("HOLD", int(counts.get("HOLD", 0)), border=True)
        c4.metric("Latest candle", table["date"].max(), border=True)
        st.caption("**BUY**: enter at the next open, with the stop and target shown. **SELL**: exit if you hold it. "
                   "**HOLD**: nothing new today. *Last signal* is the most recent BUY/SELL for that symbol. "
                   f"Quantities are sized from the paper starting capital "
                   f"({CURRENCY[market]} {settings['paper']['starting_capital'].get(market, 0):,.0f}).")
        fmt = price_fmt(market)
        st.dataframe(
            table.drop(columns=["market"]).style.map(lambda v: ACTION_COLORS.get(v, ""), subset=["action"]),
            hide_index=True, width="stretch",
            column_config={
                "close": st.column_config.NumberColumn(format=fmt),
                "stop": st.column_config.NumberColumn(format=fmt),
                "target": st.column_config.NumberColumn(format=fmt),
                "qty": st.column_config.NumberColumn(format="%g"),
                "last_signal": "last signal", "last_signal_date": "on",
                "reason": st.column_config.TextColumn(width="large"),
            })
    for sym, err in errors.items():
        st.error(f"{sym}: {err}")


# ---- Chart -----------------------------------------------------------------------------------
# Periods offered per bar interval, as (label, days). Yahoo keeps 7 days of 1m bars,
# 60 days of 5m/15m bars and 2 years of hourly bars.
CHART_PERIODS = {
    "1m": [("1D", 1), ("2D", 2), ("5D", 5), ("7D", 7)],
    "5m": [("1D", 1), ("5D", 5), ("1M", 30), ("2M", 60)],
    "15m": [("5D", 5), ("1M", 30), ("2M", 60)],
    "30m": [("5D", 5), ("1M", 30), ("2M", 60)],
    "1h": [("1M", 30), ("3M", 92), ("6M", 183), ("1Y", 365), ("2Y", 730)],
    "2h": [("1D", 1), ("5D", 5), ("1M", 30), ("3M", 92), ("1Y", 365), ("2Y", 730)],
    "4h": [("5D", 5), ("1M", 30), ("3M", 92), ("1Y", 365), ("2Y", 730)],
    "1D": [("3M", 92), ("6M", 183), ("1Y", 365), ("2Y", 730), ("5Y", 1826)],
}
INTRADAY_VIEW_BARS = 120  # intraday charts open on the latest 120 candles; drag left for the rest of the period
DEFAULT_PERIOD = {"1m": "1D", "5m": "5D", "15m": "1M", "30m": "1M", "1h": "3M", "2h": "3M", "4h": "1Y", "1D": "1Y"}
CHART_SOURCE = {"2h": "1h", "4h": "1h"}  # Yahoo has no 2h/4h bars: built from 1h bars


def alert_toggle(symbol: str, interval: str, strategy: str) -> None:
    """On/off switch for Telegram alerts on exactly what this chart shows (symbol, bars, strategy)."""
    if interval not in chart_alerts.WATCH_INTERVALS:
        st.caption("🔔 Telegram alerts are available for 15m bars and longer.")
        return
    key = f"alert_{symbol}_{interval}_{strategy}"
    on = chart_alerts.is_watched(settings, symbol, interval, strategy)
    if st.session_state.get(key) != on:  # follow the saved list (e.g. after removing it in the Alerts tab)
        st.session_state[key] = on

    def flip() -> None:
        if st.session_state[key]:
            chart_alerts.add_watch(settings, symbol, interval, strategy)
            st.toast(f"Alerts on for {symbol} {interval} ({strategy})", icon="🔔")
        else:
            chart_alerts.remove_watch(settings, symbol, interval, strategy)
            st.toast(f"Alerts off for {symbol} {interval}", icon="🔕")

    st.toggle(f"🔔 Telegram alerts: {symbol} · {interval} · {strategy}", key=key, on_change=flip,
              help="Sends a message when a bar closes with a BUY or SELL marker for this symbol, bar size "
                   "and strategy. It starts with the next bar that closes. Manage all alerts in the "
                   "Chart alerts tab.")


def _intraday_chart(symbol: str, interval: str, days: int, label: str) -> None:
    sym_market = market_of(symbol)
    # Load extra bars so the 200-bar average is valid from the first bar shown, within Yahoo's limit.
    with st.spinner(f"Loading {interval} bars for {symbol}..."):
        df = load_intraday(symbol, interval, INTRADAY_MAX_DAYS[CHART_SOURCE.get(interval, interval)],
                           st.session_state.nonce)
    if df.empty:
        st.error(no_data_message(symbol))
        return
    display_from = df.index[-1] - pd.Timedelta(days=days)
    if days == 1:  # "1D" means the latest session, however long ago it started
        display_from = pd.Timestamp(df.index[-1].date())
        if sym_market == "crypto":
            display_from = df.index[-1] - pd.Timedelta(hours=24)
    shown = df[df.index >= display_from]
    last, first = shown["close"].iloc[-1], shown["open"].iloc[0]
    tz = {"us": "New York", "india": "India", "crypto": "UTC"}.get(sym_market, "")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Last", f"{fmt_price(last)}", f"{last / first - 1:+.2%} over {label}", border=True,
              help=f"Latest {interval} bar at {df.index[-1]:%Y-%m-%d %H:%M} ({tz} time). It may still be forming.")
    k2.metric("High", f"{fmt_price(shown['high'].max())}", border=True, help=f"Highest price over {label}")
    k3.metric("Low", f"{fmt_price(shown['low'].min())}", border=True, help=f"Lowest price over {label}")
    k4.metric(f"RSI ({interval})", f"{ind.rsi(df['close']).iloc[-1]:.0f}", border=True,
              help="RSI (14) on these bars: <30 oversold, >70 overbought")
    t1, t2 = st.columns([3, 4])
    show_sigs = t1.toggle("Show strategy signals on these bars", value=False,
                          help="The strategies are built and tested for daily candles. On minute bars their "
                               "signals are for reference only; paper trading and the Signals tab use daily candles.")
    with t2:
        alert_toggle(symbol, interval, strategy_name)
    sigs = strategy_from(settings, strategy_name).generate(df) if show_sigs else pd.DataFrame(
        {"signal": 0, "reason": ""}, index=df.index)
    st.caption(f"{interval} candles, {tz} time. Indicators are computed on {interval} bars "
               f"(SMA 20 = last 20 bars). Yahoo data can lag a few minutes.")
    fig = price_chart(df, sigs, sym_market, display_from=display_from, intraday=True,
                      initial_bars=INTRADAY_VIEW_BARS)
    show_price_chart(fig)


with tab_chart:
    watch = symbols_for(settings, market)
    c1, c2, c3, c4 = st.columns([2, 2, 2, 3])
    pick = c1.selectbox("Symbol", watch, key="chart_symbol") if watch else None
    raw = c2.text_input("…or any ticker", placeholder="e.g. TSLA, DOGE-USD or SOL/USDT")
    custom = chart_alerts.normalize_symbol(raw)
    interval = c3.segmented_control("Interval", list(CHART_PERIODS), default="1D", key="chart_interval") or "1D"
    options = CHART_PERIODS[interval]
    span = c4.segmented_control("Period", [lbl for lbl, _ in options], default=DEFAULT_PERIOD[interval],
                                key=f"chart_span_{interval}") or DEFAULT_PERIOD[interval]
    days = dict(options)[span]
    symbol = custom or pick
    if raw.strip() and custom != raw.strip().upper():
        st.caption(f"Showing Yahoo Finance's **{custom}** for {raw.strip()}. Yahoo has USD pairs only; "
                   "USDT trades within about 0.1% of USD, so candles and signals are effectively the same.")
    if symbol and interval != "1D":
        _intraday_chart(symbol, interval, days, span)
    elif symbol:
        strategy = strategy_from(settings, strategy_name)
        with st.spinner(f"Loading {symbol}..."):
            df = history(symbol, days + 420)  # extra history so SMA200 is valid from the first bar shown
        if df.empty:
            st.error(no_data_message(symbol))
        elif len(df) < strategy.warmup + 2:
            st.error(f"Only {len(df)} daily bars of history for {symbol}; the strategy needs {strategy.warmup + 2}.")
        else:
            sym_market = market_of(symbol)
            risk = RiskConfig.from_settings(settings)
            sig = signal_for_latest_bar(symbol, df, strategy, risk,
                                        settings["paper"]["starting_capital"].get(sym_market))
            prev_close = float(df["close"].iloc[-2])
            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("Close", f"{fmt_price(sig.price)}", f"{sig.price / prev_close - 1:+.2%}", border=True,
                      help=f"{symbol} close on {sig.date} (latest completed candle)")
            k2.metric("Signal", sig.action, border=True, help="Signal on the latest completed candle")
            k3.metric("RSI", f"{ind.rsi(df['close']).iloc[-1]:.0f}", border=True, help="RSI (14): <30 oversold, >70 overbought")
            k4.metric("ATR", f"{sig.extra['atr'] / sig.price:.1%}", border=True,
                      help=f"Average daily range (14): {fmt_price(sig.extra['atr'])}, as % of price")
            last = sig.extra.get("last_signal")
            k5.metric("Last", last or "none", sig.extra.get("last_signal_date", "")[5:] or None,
                      delta_color="off", border=True, help="Most recent BUY/SELL and its date")

            levels = {}
            if sig.action == "BUY" and sig.stop_loss:
                levels = {"Stop": sig.stop_loss, "Target": sig.target}
                st.success(f"**BUY** {symbol}: {sig.reason}. Entry near the next open, stop {fmt_price(sig.stop_loss)}, "
                           f"target {fmt_price(sig.target)}" + (f", size {sig.quantity:g}" if sig.quantity else "") + ".")
            elif sig.action == "SELL":
                st.error(f"**SELL** {symbol}: {sig.reason}. Exit at the next open if you hold it.")
            pos = broker_from(settings).positions.get(symbol)
            if pos:
                levels = {"Entry": pos["entry_price"], "Stop": pos["stop_loss"], "Target": pos["target"]}
                st.info(f"Paper position: {pos['quantity']:g} @ {fmt_price(pos['entry_price'])} since {pos['entry_date']}.")

            fig = price_chart(df, strategy.generate(df), sym_market,
                              display_from=df.index[-1] - pd.Timedelta(days=days), levels=levels)
            show_price_chart(fig)



# ---- Chart alerts ----------------------------------------------------------------------------
def _ago_text(iso: str) -> str:
    if not iso:
        return ""
    mins = int((pd.Timestamp.now(tz="UTC") - pd.Timestamp(iso)).total_seconds() // 60)
    return f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago"


with tab_alerts:
    st.subheader("Chart alerts")
    st.caption("Get a Telegram message when a bar closes with a BUY or SELL marker, the same markers the Chart tab "
               "shows with *Show strategy signals*. Pick the symbol, bar size and strategy. Alerts start with the "
               "next bar that closes, and each signal is sent once. Signals only: nothing is bought or sold.")
    from tradebot import schedule as sched  # noqa: E402

    a_cfg = settings.get("alerts", {})
    sinfo = sched.status() if sys.platform == "darwin" else {"installed": False}
    if not a_cfg.get("telegram"):
        st.warning("Telegram is off (`alerts.telegram` in config/settings.yaml), so alerts only reach this "
                   "computer's notifications and the log.", icon="📵")
    if not sinfo.get("installed"):
        st.warning("The scheduled job isn't installed, so nothing checks for new bars. In a terminal run "
                   "`tradebot schedule install`.", icon="⏰")
    elif sinfo.get("check_every") != sched.CHECK_EVERY_SECONDS:
        st.warning(f"The scheduled job checks every {int(sinfo.get('check_every') or 900) // 60} minutes. Run "
                   f"`tradebot schedule install` once to check every {sched.CHECK_EVERY_SECONDS // 60} minutes.",
                   icon="⏰")
    else:
        st.info(f"Checked every {sched.CHECK_EVERY_SECONDS // 60} minutes while this computer is awake, so a "
                "message arrives within about 5 minutes of the bar closing.", icon="⏱️")

    with st.form("add_alert", clear_on_submit=True, border=True):
        f1, f2, f3, f4 = st.columns([3, 2, 3, 2], vertical_alignment="bottom")
        new_sym = f1.text_input("Symbol", placeholder="SOL/USDT, BTC-USD, AAPL")
        new_int = f2.selectbox("Bars", list(chart_alerts.WATCH_INTERVALS), index=3)
        new_strat = f3.selectbox("Strategy", ["(market default)"] + names)
        if f4.form_submit_button("Add alert", type="primary", width="stretch") and new_sym.strip():
            sym = chart_alerts.normalize_symbol(new_sym)
            strat = strategy_name_for(settings, market_of(sym)) if new_strat == "(market default)" else new_strat
            if load_intraday(sym, "1h", 5, st.session_state.nonce).empty:
                st.error(no_data_message(sym))
            else:
                chart_alerts.add_watch(settings, sym, new_int, strat)
                st.toast(f"Alerts on for {sym} {new_int} ({strat})", icon="🔔")

    items = chart_alerts.load_watchlist(settings)
    wstate = chart_alerts.load_watch_state(settings)
    if not items:
        st.caption("No alerts yet. Add one above, or switch on *Telegram alerts* above a chart in the Chart tab.")
    for e in items:
        stt = wstate.get(chart_alerts.entry_key(e), {})
        r1, r2, r3, r4 = st.columns([3, 4, 4, 1], vertical_alignment="center")
        r1.markdown(f"**{e['symbol']}** · {e['interval']} · `{e['strategy']}`")
        if stt.get("error"):
            r2.markdown(f":red[Error: {stt['error']}]")
        elif stt.get("last_bar"):
            r2.caption(f"Checked {_ago_text(stt.get('checked_utc', ''))}; latest closed bar {stt['last_bar']}")
        else:
            r2.caption(f"Added {_ago_text(e['added'])}; waiting for the first bar to close")
        r3.caption(f"Last alert: {stt['last_signal']}" if stt.get("last_signal") else "No alert sent yet")
        if r4.button("✕", key=f"rm_{chart_alerts.entry_key(e)}", help="Stop these alerts"):
            chart_alerts.remove_watch(settings, e["symbol"], e["interval"], e["strategy"])
            st.rerun()
    st.caption("Strategies are tested on daily candles. On intraday bars they trade more often and costs matter "
               "more (the 5m and 1h backtest lost to daily after costs), so treat these as prompts to look at "
               "the chart, not as tested trades.")


# ---- Backtest --------------------------------------------------------------------------------
def _scaled(series: pd.Series, start_ts, capital: float) -> pd.Series:
    s = series[series.index >= start_ts].dropna()
    return s / s.iloc[0] * capital if len(s) else s


def _equal_weight(data, start_ts, capital: float) -> pd.Series:
    closes = pd.DataFrame({s: df["close"] for s, df in data.items()}).ffill()
    closes = closes[closes.index >= start_ts].bfill()
    return (closes / closes.iloc[0]).mean(axis=1) * capital


with tab_backtest:
    st.subheader(f"Backtest: {MARKET_NAMES[market]}")
    st.caption("Replays the strategy on past daily candles as one portfolio with shared cash. Signals fill at the "
               "next open, with commission and slippage, and the stop is assumed to hit before the target on the "
               "same day. Past results don't predict future returns.")
    tradable = [s for s in symbols_for(settings, market) if is_tradable(s)]
    risk_cfg = RiskConfig.from_settings(settings)
    with st.form("backtest_form"):
        chosen = st.multiselect("Symbols", tradable, default=tradable)
        extra = st.text_input("Extra tickers (comma separated, same market)", "")
        f1, f2, f3, f4 = st.columns(4)
        years = f1.slider("Years of history", 1, 10, int(settings["backtest"]["years"]))
        capital = f2.number_input(f"Starting capital ({CURRENCY[market]})", min_value=100.0, step=1000.0,
                                  value=float(settings["paper"]["starting_capital"].get(market, 10000)))
        max_pos = f3.number_input("Max open positions", 1, 50, int(risk_cfg.max_positions))
        compare = f4.toggle("Compare all strategies", value=False)
        g1, g2, g3, g4 = st.columns(4)
        risk_pct = g1.slider("Risk per trade (%)", 0.25, 5.0, risk_cfg.risk_per_trade * 100, 0.25)
        atr_mult = g2.slider("Stop distance (× ATR)", 0.5, 5.0, risk_cfg.atr_stop_mult, 0.25)
        rr = g3.slider("Target (× risk)", 0.5, 10.0, risk_cfg.reward_risk, 0.5)
        cap_pct = g4.slider("Max per position (%)", 5, 100, int(risk_cfg.max_position_pct * 100), 5)
        submitted = st.form_submit_button("Run backtest", type="primary")

    if submitted:
        symbols = chosen + [s.strip().upper() for s in extra.split(",") if s.strip()]
        wrong = [s for s in symbols if market_of(s) != market]
        if wrong:
            st.error(f"These aren't {MARKET_NAMES[market]} tickers: {', '.join(wrong)}")
        elif not symbols:
            st.error("Pick at least one symbol.")
        else:
            risk = RiskConfig(risk_per_trade=risk_pct / 100, atr_period=risk_cfg.atr_period, atr_stop_mult=atr_mult,
                              reward_risk=rr, max_position_pct=cap_pct / 100, max_positions=int(max_pos))
            days = int(365.25 * years)
            with st.spinner("Loading prices and running..."):
                data = {}
                for s in symbols:
                    df = history(s, days)
                    if not df.empty:
                        data[s] = df
                bm_sym = settings["backtest"]["benchmark"].get(market)
                bm = history(bm_sym, days)["close"] if bm_sym else None
                costs = settings["costs"][market]
                strats = [strategy_from(settings, n) for n in REGISTRY] if compare else [strategy_from(settings, strategy_name)]
                results = []
                for strat in strats:
                    try:
                        results.append(run_portfolio_backtest(
                            data, strat, capital, risk, costs["commission"], costs["slippage"],
                            max_positions=1 if len(data) == 1 else int(max_pos), benchmark=bm,
                            sector_of=settings.get("sectors") or {}, max_per_sector=risk_cfg.max_per_sector))
                    except ValueError as exc:
                        st.warning(f"{strat.name}: {exc}")
            st.session_state.bt = dict(results=results, data=data, bm=bm, bm_sym=bm_sym, capital=capital,
                                       market=market, compare=compare,
                                       warmup=max(s.warmup for s in strats))

    bt = st.session_state.get("bt")
    if bt and bt["market"] == market and bt["results"]:
        results, data, capital = bt["results"], bt["data"], bt["capital"]
        start_ts = min(df.index[min(bt["warmup"], len(df) - 1)] for df in data.values())
        passive = {"Buy & hold (equal weight)": _equal_weight(data, start_ts, capital)}
        if bt["bm"] is not None:
            passive[f"Benchmark ({bt['bm_sym']})"] = _scaled(bt["bm"], start_ts, capital)

        if bt["compare"]:
            rows = [{"strategy": r.strategy.split("(")[0], "return": r.metrics["total_return"],
                     "CAGR": r.metrics["cagr"], "max drawdown": r.metrics["max_drawdown"],
                     "sharpe": r.metrics["sharpe"], "trades": int(r.metrics["trades"]),
                     "win rate": r.metrics["win_rate"], "profit factor": r.metrics["profit_factor"],
                     "avg invested": r.metrics["exposure"]} for r in results]
            cmp = pd.DataFrame(rows).sort_values("sharpe", ascending=False)
            pct = st.column_config.NumberColumn(format="percent")
            st.dataframe(cmp, hide_index=True, width="stretch",
                         column_config={"return": pct, "CAGR": pct, "max drawdown": pct, "win rate": pct,
                                        "avg invested": pct,
                                        "sharpe": st.column_config.NumberColumn(format="%.2f"),
                                        "profit factor": st.column_config.NumberColumn(format="%.2f")})
            curves = {r.strategy.split("(")[0]: r.equity[r.equity.index >= start_ts] for r in results}
            st.plotly_chart(line_chart({**curves, **passive}, "Equity curves", CURRENCY[market]),
                            config={"displaylogo": False})
        else:
            res = results[0]
            m = res.metrics
            bh = m["buy_hold_return"]
            r1 = st.columns(4)
            r1[0].metric("Total return", f"{m['total_return']:+.1%}", f"{m['total_return'] - bh:+.1%} vs buy & hold",
                         border=True)
            r1[1].metric("CAGR", f"{m['cagr']:+.1%}", border=True)
            r1[2].metric("Max drawdown", f"{m['max_drawdown']:.1%}", border=True)
            r1[3].metric("Sharpe", f"{m['sharpe']:.2f}", border=True)
            r2 = st.columns(4)
            r2[0].metric("Trades", int(m["trades"]), f"{res.skipped_entries} BUYs not filled" if res.skipped_entries else None,
                         delta_color="off", border=True)
            r2[1].metric("Win rate", f"{m['win_rate']:.0%}", border=True)
            r2[2].metric("Profit factor", f"{m['profit_factor']:.2f}", border=True)
            r2[3].metric("Avg capital invested", f"{m['exposure']:.0%}", border=True)
            st.caption(f"Buy & hold (equal weight): {bh:+.1%}" +
                       (f" · {bt['bm_sym']}: {m['benchmark_return']:+.1%}" if "benchmark_return" in m else ""))

            eq = res.equity[res.equity.index >= start_ts]
            st.plotly_chart(line_chart({res.strategy.split("(")[0]: eq, **passive}, "Equity", CURRENCY[market]),
                            config={"displaylogo": False})
            st.plotly_chart(drawdown_chart(eq), config={"displaylogo": False})

            if len(data) > 1:
                st.markdown("**Per symbol**")
                pct = st.column_config.NumberColumn(format="percent")
                st.dataframe(res.per_symbol, hide_index=True, width="stretch",
                             column_config={"win_rate": pct, "avg_return": pct, "buy_hold": pct,
                                            "pnl": st.column_config.NumberColumn(format="%.2f")})
            trades = res.trades_frame()
            if not trades.empty:
                st.markdown("**Trades**")
                trades = trades[["symbol", "entry_date", "entry_price", "exit_date", "exit_price", "quantity",
                                 "exit_reason", "pnl", "return_pct", "bars_held"]].sort_values("entry_date",
                                                                                           ascending=False)
                st.dataframe(trades, hide_index=True, width="stretch", column_config={
                    "entry_date": st.column_config.DateColumn(), "exit_date": st.column_config.DateColumn(),
                    "quantity": st.column_config.NumberColumn(format="%g"),
                    "return_pct": st.column_config.NumberColumn("return", format="percent"),
                    "pnl": st.column_config.NumberColumn(format="%.2f")})
                st.download_button("Download trades CSV", trades.to_csv(index=False), "trades.csv", "text/csv")


# ---- Paper portfolio ---------------------------------------------------------------------------
with tab_paper:
    broker = broker_from(settings)
    st.subheader("Paper portfolio")
    st.caption("A simulated account: no real orders. Each update fills yesterday's queued orders at the open, "
               "checks stops and targets, then queues orders from the latest signals. Run it once a day "
               "after the close (US 4pm ET; crypto candles close 00:00 UTC).")

    last = load_last_run(settings)
    if not last:
        st.warning("No automatic daily run yet. Set one up in a terminal with `tradebot schedule install`, "
                   "or press the button below once a day.", icon="⏰")
    else:
        when = pd.Timestamp(last.get("finished") or last["started"]).tz_convert(None)
        age_h = (pd.Timestamp.utcnow().tz_localize(None) - when).total_seconds() / 3600
        msg = (f"Last daily run: {when.tz_localize('UTC').tz_convert(LOCAL_TZ):%a %d %b, %H:%M} "
               f"({age_h:.0f} h ago), {len(last.get('new_signals', []))} new signal(s), "
               f"{len(last.get('events', []))} paper update(s)")
        if not last.get("ok"):
            st.error(msg + ". It FAILED: check state/logs/daily.log.", icon="⚠️")
        elif age_h > 30:
            st.warning(msg + ". That's over a day old: is the schedule installed (`tradebot schedule status`)?",
                       icon="⏰")
        else:
            st.success(msg + ".", icon="✅")

    acc = accounts_frame(broker, active_markets(settings))
    cols = st.columns(max(len(acc), 1))
    for col, (_, a) in zip(cols, acc.iterrows()):
        col.metric(f"{MARKET_NAMES.get(a['market'], a['market'])} equity ({a['currency']})", f"{a['equity']:,.2f}",
                   f"{a['return']:+.2%} · {a['open_positions']} open · cash {a['cash']:,.0f}", border=True)

    b1, b2, _ = st.columns([2, 2, 3])
    if b1.button(f"▶ Run paper update: {MARKET_NAMES[market]}", type="primary", width="stretch"):
        with st.spinner("Updating paper account..."):
            signals, errors = run_paper_update(settings, broker, symbols_for(settings, market),
                                               strategy_from(settings, strategy_name))
        st.session_state.paper_events = broker.events or ["Nothing to do: no fills, exits or new signals."]
        st.session_state.paper_errors = errors
        st.rerun()
    with b2.popover("Reset paper account", width="stretch"):
        st.write("Deletes all paper positions and history and restores the starting capital.")
        if st.button("Yes, reset everything", type="secondary"):
            broker.reset()
            broker.save()
            st.session_state.paper_events = ["Paper account reset."]
            st.rerun()

    for e in st.session_state.pop("paper_events", []):
        st.info(e)
    for sym, err in st.session_state.pop("paper_errors", {}).items():
        st.warning(f"{sym}: {err}")

    pos = positions_frame(broker)
    st.markdown("**Open positions**")
    if pos.empty:
        st.caption("None yet.")
    else:
        st.dataframe(pos, hide_index=True, width="stretch", column_config={
            "qty": st.column_config.NumberColumn(format="%g"),
            "unrealized": st.column_config.NumberColumn(format="%.2f"),
            "unrealized_pct": st.column_config.NumberColumn("unrealized %", format="percent")})
    pend = pending_frame(broker)
    if not pend.empty:
        st.markdown("**Pending orders** (fill at the next open)")
        st.dataframe(pend, hide_index=True, width="stretch")
    hist = history_frame(broker)
    st.markdown("**Closed trades**")
    if hist.empty:
        st.caption("None yet.")
    else:
        st.dataframe(hist.sort_values("exit_date", ascending=False), hide_index=True, width="stretch",
                     column_config={"return_pct": st.column_config.NumberColumn("return", format="percent"),
                                    "pnl": st.column_config.NumberColumn(format="%.2f"),
                                    "quantity": st.column_config.NumberColumn(format="%g")})
