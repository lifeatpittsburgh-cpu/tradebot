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
| Execution | Signals + local paper trading. **No live orders in v1** |
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

### Phase 4 — Strategy quality
- [ ] Walk-forward / out-of-sample validation, parameter sweeps
- [ ] Market-regime filter (index above 200-DMA, VIX / India VIX)
- [ ] Volume and relative-strength filters, sector exposure limits
- [ ] Fundamentals screen for the long-term watchlist

### Phase 5 — Broker integration (optional, needs explicit opt-in)
- [ ] Alpaca (US) paper API, Zerodha Kite / Upstox (India) read-only
- [ ] Live order placement only behind a kill switch, daily loss limit and manual confirmation

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

## Known limits
- yfinance is unofficial and rate-limited; good for prototyping, not production. Swap
  in a paid feed (Polygon, Alpha Vantage, Kite historical) through the `DataProvider` interface.
- The watchlists are today's large caps, so backtests carry survivorship bias.
- Daily-bar backtests can't tell whether stop or target hit first within a bar; the engine
  assumes the **stop** first (conservative).
