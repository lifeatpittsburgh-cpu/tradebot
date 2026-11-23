# TradeBot — Project Plan

A swing-trading **signal bot** for US stocks (NYSE/NASDAQ) and crypto, with Indian (NSE/BSE)
stocks still supported.
It scans a watchlist on daily candles after market close, emits BUY / SELL / HOLD
signals with suggested stop-loss, target and position size, tracks them in a
**paper-trading** ledger, and lets you backtest strategies before trusting them.

> TradeBot is a decision-support tool, not financial advice. Signals come from
> mechanical rules applied to historical prices; past performance does not predict
> future results. You place any real trades yourself.

## Scope decisions (v1)

| Area | Decision |
|---|---|
| Markets | **Focus: US stocks + crypto** (`AAPL`, `BTC-USD`). India (`RELIANCE.NS`) kept but off by default |
| Timeframe | Daily candles, end-of-day signals (swing: days to weeks) |
| Execution | Signals + local paper trading; optional Alpaca paper account; live orders only with per-order confirmation |
| Interface | CLI first, Streamlit dashboard on top, Telegram alerts |
| Language | Python (3.9-compatible; 3.11+ recommended) |

## Architecture

```
                 ┌──────────────┐
 config/*.yaml ─▶│   Settings   │
                 └──────┬───────┘
                        ▼
 ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
 │ Data provider│─▶│  Indicators  │─▶│  Strategies  │─▶│ Risk manager │
 │ (yfinance,   │  │ SMA EMA RSI  │  │ crossover,   │  │ ATR stop,    │
 │  cache, CSV) │  │ MACD ATR BB  │  │ RSI, MACD,   │  │ target, size │
 └──────────────┘  └──────────────┘  │ composite    │  └──────┬───────┘
                                     └──────────────┘         ▼
                    ┌──────────────┬──────────────┬──────────────┐
                    │  Backtester  │ Paper broker │   Alerts     │
                    │  (metrics)   │ (JSON ledger)│ console/TG   │
                    └──────────────┴──────────────┴──────────────┘
                                  ▲
                         CLI  ·  Streamlit dashboard
```

## Project structure

```
tradingbot/
├── config/settings.yaml        # watchlists, strategy params, risk, alerts
├── docs/PROJECT_PLAN.md
├── src/tradebot/
│   ├── config.py               # load settings
│   ├── data/                   # DataProvider interface, yfinance, on-disk cache
│   ├── indicators.py           # pure pandas indicators
│   ├── strategies/             # Strategy base + concrete strategies + registry
│   ├── risk.py                 # stop-loss / target / position sizing
│   ├── signals.py              # Signal model
│   ├── backtest/engine.py      # daily-bar backtester + performance metrics
│   ├── paper/broker.py         # paper trading ledger
│   ├── alerts/                 # console + Telegram notifiers
│   ├── scanner.py              # run strategy over a watchlist → signals
│   └── cli.py                  # `tradebot scan | backtest | paper ...`
├── dashboard/app.py            # Streamlit UI (Phase 3)
└── tests/                      # unit tests on synthetic data (no network)
```

## Roadmap

### Phase 1 — Core engine (done)
- [x] Project skeleton, config, packaging
- [x] Data provider interface + yfinance adapter + parquet/CSV cache
- [x] Indicators: SMA, EMA, RSI, MACD, ATR, Bollinger Bands
- [x] Strategies: SMA crossover, RSI mean-reversion, MACD trend, composite vote
- [x] Composite v2: votes on how many sub-strategies are currently long (enter at 2, exit at 0)
- [x] Risk: ATR-based stop-loss, reward:risk target, fixed-fractional sizing
- [x] Backtester: next-bar-open fills, stop/target exits, commission + slippage,
      metrics (CAGR, max drawdown, Sharpe, win rate, profit factor), buy-and-hold benchmark
- [x] Portfolio backtester: whole watchlist on one cash account, max open positions,
      per-symbol breakdown, equal-weight buy-and-hold and index benchmarks (SPY, ^NSEI)
- [x] CLI: `scan`, `backtest` (one symbol, a list, or a watchlist), `backtest --compare`
- [x] Unit tests

### Phase 2 — Paper trading and alerts (done)
- [x] Paper broker with JSON ledger (open/close positions, P&L)
- [x] `paper run` CLI: apply today's signals to the paper account (max positions, indexes not traded)
- [x] `tradebot daily`: paper update + alerts; each BUY/SELL alerted once per candle
- [x] Alert channels: console/log, macOS notification, Telegram (+ `alerts telegram-chat-id`, `alerts test`)
- [x] Telegram tested with a real bot (2026-11-22)
- [x] Scheduled end-of-day run with launchd (`tradebot schedule install`, default 20:15 ET)
- [x] Holidays without a calendar library: no new candle on a holiday means no new alert
- [x] Dashboard shows the last daily run and warns when it failed or is over a day old

### Phase 3 — Dashboard and crypto (done)
- [x] Crypto market: fractional quantities, 24/7 calendar, exchange fees, BTC benchmark
- [x] Unfinished-candle filter (US before 4:15pm ET, crypto before 00:00 UTC) + 60-min data cache
- [x] Streamlit: watchlist signals table, price chart with indicators and signal markers
- [x] Backtest explorer with parameter sliders, strategy comparison, equity and drawdown curves
- [x] Paper portfolio view with one-click daily update
- [x] Intraday charts (1m / 5m / 15m / 1h) for viewing, with pan and time-only zoom that re-fits prices
- [x] Intraday backtest (5m / 1h): loses to daily after costs, so signals stay on daily candles

### Phase 4 — Strategy quality (done)
- [x] Walk-forward testing: tune on 3 years, test on the next 6 months, repeat; `tradebot research`
- [x] Entry filters tested out of sample: market regime (index above 200-day average, optional
      sell-all), relative strength (top half of the watchlist by 6-month return), volume
- [x] Position-size test (risk per trade, cap per position)
- [x] Live strategy switched to `macd_trend` (2026-11-22), then US changed to `sma_crossover` after the
      S&P 500 test (2026-11-23); position size unchanged (1% risk, 20% cap)
- [x] Survivorship-free universe: S&P 500 members as of each quarter since 2013, rebuilt from
      Wikipedia's page history (`tradebot universe update`); `tradebot research --universe sp500`
- [x] When BUYs compete for slots: optional priority (e.g. strongest 6-month return first)
- [x] Sector exposure limits: `risk.max_per_sector` + `sectors:` in settings (backtests and paper; off by default)
- [x] Fundamentals screen: `tradebot screen` ranks S&P 500 stocks on quality, growth, value and
      safety within their sector (current Yahoo figures; not backtestable)
- [x] Strategy per market (`strategy.by_market`): live US uses sma_crossover (best on the S&P 500
      test), crypto keeps macd_trend (best on the crypto watchlist; no fair crypto test exists)

### Phase 5 — Broker integration (done: paper live, real money off)
- [x] Alpaca client (REST): account, positions, orders, assets; paper and live hosts with separate keys
- [x] Order planner: US bracket orders (stop + target at the broker); crypto market buy, protective
      stop-limit, take-profit on the daily high; SELL signals cancel open orders and sell
- [x] Safety: kill switch (+ close-all), daily loss limit, per-order cap, max new buys per run,
      act-once state + client order IDs, watchlist only
- [x] Live mode, off by default: proposals only; each order sent after a typed `yes` in
      `tradebot broker confirm` at a terminal; proposals expire after 20 hours
- [x] Daily job integration (`broker.enabled`) with orders in the alert
- [x] Alpaca paper account connected and enabled (2026-11-23); daily job sends paper orders from then on
- [ ] Zerodha Kite / Upstox (India) read-only: dropped while India is out of scope

### Phase 6 — Intraday chart alerts and AI market study
- [x] Telegram chart alerts for chosen symbols and bar sizes (15m–4h), once per closed bar (#8)
- [x] Chart period fixes, time-zone choice, Live refresh switch (#9–#11)
- [x] AI market study: Claude rates news and market mood daily; can hold back BUYs, never adds trades
- [x] Raspberry Pi 5: Linux scheduler (systemd user timer), dashboard service, setup guide (docs/RASPBERRY_PI.md)

## Results log

**2026-11-22, portfolio backtest, 5 years to 2026-11-20, default watchlists and settings**

| Market | Best strategy | Return | Max DD | Sharpe | Avg invested | Buy & hold (eq. wt) | Index |
|---|---|---|---|---|---|---|---|
| US (9) | composite | +44.2% | -8.9% | 1.06 | 42% | +197.7% | SPY +77.5% |
| India (8) | sma_crossover | +8.1% | -8.1% | 0.45 | 19% | +43.4% | Nifty +43.9% |

| Crypto (9) | composite | +9.3% | -11.6% | 0.27 | 8% | +39.2% | BTC +111.9% |

- Every strategy trails buy-and-hold in every market. Drawdowns are much smaller, but
  money sits in cash 60-80% of the time (over 90% for crypto: its large daily ranges
  make the 1%-risk sizing put only ~5-10% of the account in each coin). MACD loses money in India (-15.7%).
- Profit-target sweep (2R / 3R / 5R / none): wider targets raise invested time and drawdown
  more than return, and Sharpe is best at 2R. Exits aren't the bottleneck; entry quality is.
- Caveat: 5 years, ~8 large caps per market, one mostly rising period. Treat as a smoke test,
  not evidence of an edge. Phase 4 (walk-forward, wider universe) decides what's real.

**2026-11-22, intraday portfolio backtest (same strategies, settings and costs), to 2026-11-20/22**

Yahoo history limits: 5m bars cover the last 60 days, 1h bars the last 2 years. Daily = the same
strategy on daily candles over the same window, for comparison.

| Market | Bars | Best strategy | Return | Before costs | Trades | Composite | Daily composite | Buy & hold (eq. wt) |
|---|---|---|---|---|---|---|---|---|
| US | 5m (60 d) | rsi_reversion | -4.0% | -3.3% | 72 | -9.9% | -1.2% | -10.1% |
| US | 1h (2 y) | sma_crossover | +14.1% | +24.4% | 284 | +2.5% | +28.0% | +57.3% |
| Crypto | 5m (60 d) | rsi_reversion | -12.9% | -2.9% | 142 | -66.7% | -3.3% | -36.2% |
| Crypto | 1h (2 y) | rsi_reversion | -15.9% | -3.7% | 173 | -61.0% | +1.0% | -59.9% |

- No strategy makes money before costs on 5m bars, and none on crypto at all except MACD 1h
  (+25.7% before costs, -79.3% after). Win rates fall to 11-27% on 5m: a 2×ATR stop on 5m bars is
  tiny, so ordinary noise stops trades out.
- Costs decide it: crypto round trips cost ~0.4%; MACD made 2,954 trades in 60 days on 5m crypto.
- On daily candles, composite and sma_crossover (the two re-run that way) beat their intraday
  versions in every window.
  Conclusion: keep signals and paper trading on daily candles; use intraday charts for viewing only.

**2026-11-22, walk-forward tests (Phase 4), daily candles, default watchlists and costs**

Train 3 years, test the next 6 months, repeated; only the test periods count. Out-of-sample
period: US 2018-09-14 to 2026-11-20, crypto 2018-06-11 to 2026-11-22.

*Strategies, no filters, 1% risk per trade (tuned = best settings from each training window):*

| Market | Strategy | Tuned | Default settings | Worst drop | Sharpe | Avg. invested |
|---|---|---|---|---|---|---|
| US | macd_trend | +106% | +90% | -12% | 1.05 | 37% |
| US | composite | +67% | +82% | -13% | 0.78 | 34% |
| US | sma_crossover | +12% | +46% | -11% | 0.38 | 10% |
| US | rsi_reversion | +9% | -3% | -16% | 0.20 | 15% |
| US | SPY buy & hold | +169% (12.8%/yr) | | -34% | 0.72 | 100% |
| Crypto | macd_trend | +138% | +87% | -16% | 0.89 | 10% |
| Crypto | composite | +55% | +51% | -13% | 0.70 | 6% |
| Crypto | sma_crossover | +18% | +50% | -16% | 0.30 | 6% |
| Crypto | rsi_reversion | -10% | -13% | -21% | -0.21 | 4% |
| Crypto | BTC buy & hold | +770% (29.2%/yr) | | -77% | 0.73 | 100% |

- **Filters don't help.** Regime, relative-strength and volume filters lowered out-of-sample
  returns in almost every case (e.g. US composite, default settings: none +82%, regime +60%,
  RS +52%, volume +53%). They cut drawdowns a little, but less than simply trading smaller.
  The one exception, the regime filter with sell-all on crypto composite/sma (+5 to +8 points),
  isn't consistent across strategies, so it stays off.
- **Tuning doesn't reliably help.** Walk-forward tuning beat the default settings for MACD in
  both markets, but lost badly for SMA crossover in both and for composite in the US. Keep the
  defaults; don't auto-tune.
- **MACD trend is the strongest strategy out of sample** in both markets, with a better Sharpe
  than buy & hold (1.05 vs 0.72 US; 0.89 vs 0.73 crypto) at a third or less of the drawdown.
- **The gap to buy & hold is mostly cash.** At 1% risk per trade the account is only 37% invested
  (US) or 10% (crypto). Larger positions scale return and drawdown together (Sharpe stays
  about the same), so position size is a risk choice, not an edge:

| Strategy (default settings) | Risk / cap | Return | Per year | Worst drop | Sharpe |
|---|---|---|---|---|---|
| US composite | 1% / 20% (current) | +82% | 7.6% | -10% | 0.87 |
| US composite | 2% / 35% | +139% | 11.2% | -19% | 0.87 |
| US composite | 3% / 50% | +169% | 12.8% | -21% | 0.87 |
| US macd_trend | 3% / 50% | +175% | 13.1% | -30% | 0.88 |
| Crypto macd_trend | 1% / 20% (current) | +87% | 7.7% | -19% | 0.70 |
| Crypto macd_trend | 2% / 35% | +221% | 14.8% | -34% | 0.79 |
| Crypto macd_trend | 3% / 35% | +313% | 18.3% | -38% | 0.80 |

- Caveats: one 8-year period with a strong bull market; 9 symbols per market picked today
  (survivorship bias: the equal-weight buy & hold of these symbols made +443% US, +1,532% crypto);
  position sizes were compared on the same period they're reported on.

**2026-11-23, survivorship-free test: point-in-time S&P 500, 2014-01-02 to 2026-11-20**

A stock may be bought only while it was in the index (quarterly snapshots). 621 of the 829 stocks
ever listed have Yahoo prices (71% of members in 2013, 99% today; the missing ones are mostly
acquired or failed companies). Default settings, 1% risk, 8 positions, US costs, $100k. With ~500
stocks there are usually more BUYs than slots, so each run is repeated with 10 random slot orders:

| Strategy | Per year (range over 10 runs) | Worst drop | Sharpe |
|---|---|---|---|
| SPY buy & hold | +12.4% | -34% | 0.76 |
| Equal-weight index members | +12.7% | -40% | 0.71 |
| sma_crossover | **+8.3%** (+4.7 to +10.7) | -23% | **0.64** ± 0.13 |
| sma_crossover, max 2 per sector | +9.3% (+6.9 to +11.4) | -20% (worst -22%) | 0.72 ± 0.08 |
| composite | +5.9% (+2.4 to +8.9) | -33% | 0.46 ± 0.14 |
| composite, max 2 per sector | +5.3% (+2.5 to +8.3) | -30% | 0.43 ± 0.11 |
| macd_trend | +3.4% (+1.1 to +5.3) | -34% | 0.30 ± 0.08 |
| macd_trend, max 2 per sector | +2.9% (-0.1 to +6.4) | -35% | 0.27 ± 0.12 |
| rsi_reversion (3 runs) | -0.7% | -32% | 0.01 |

- **No strategy beats SPY** on a fair universe. SMA crossover comes closest on risk (Sharpe
  0.64-0.72 vs 0.76) with a much smaller worst drop (-20 to -23% vs -34%).
- **MACD trend's lead was survivorship bias.** It was best on the 9 hand-picked stocks but is the
  weakest trend strategy here; its ranges don't overlap SMA crossover's averages.
- **Slot luck is large** (±2-3%/yr between random slot orders). "Strongest 6-month return first"
  wasn't better than random (MACD +3.7%, SMA +7.0%, composite +4.7%/yr).
- **Sector cap:** helps SMA crossover (higher Sharpe, worst drop -22% vs -32% across runs),
  neutral to slightly worse for MACD and composite: an option, not a default.
- **Regime filter and 20 smaller positions** didn't help on this universe either.
- Crypto can't be tested this way: there's no free history of which coins were in a top-N list.

## Known limits
- yfinance is unofficial and rate-limited; good for prototyping, not production. Swap
  in a paid feed (Polygon, Alpha Vantage, Kite historical) through the `DataProvider` interface.
- The watchlists are today's large caps, so backtests carry survivorship bias.
- Daily-bar backtests can't tell whether stop or target hit first within a bar; the engine
  assumes the **stop** first (conservative).
