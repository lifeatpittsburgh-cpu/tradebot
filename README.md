# TradeBot

A swing-trading **signal bot** for **US stocks and crypto** (Indian stocks also supported).
It reads daily candles, reports **BUY / SELL / HOLD** signals with a suggested stop-loss,
target and position size, lets you backtest every strategy, and follows its signals in a
**paper-trading** account. A web dashboard shows all of it with charts.

> Signals are mechanical rules applied to past prices. They are not financial advice,
> and past performance doesn't predict future results. TradeBot never places real orders.

See [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) for the architecture and roadmap.

## Quick start: see the dashboard

**1. One-time setup** (from a terminal):

```bash
cd ~/Projects/tradingbot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,dashboard]"
```

**2. Start the dashboard:**

```bash
cd ~/Projects/tradingbot
source .venv/bin/activate
tradebot dashboard
```

**3. Open <http://localhost:8501>** in your browser. Stop it with `Ctrl+C` in the terminal.

The first load of each market takes 10-20 seconds while prices download; after that
they're cached for an hour (use **Refresh market data** in the sidebar to force a reload).

**What's on each tab**

| Tab | What it does |
|---|---|
| Signals | Today's BUY / SELL / HOLD for every symbol in the watchlist, with stop, target and size |
| Chart | Candles, SMA 20/50/200, Bollinger bands, BUY/SELL markers, volume, RSI, MACD; type any ticker |
| Backtest | Replay a strategy on 1-10 years of history as a portfolio; tune risk with sliders; "Compare all strategies" ranks them; download trades |
| Paper portfolio | Simulated account, updated by the daily job (or click **Run paper update**): fills orders, applies stops/targets, queues new signals. Shows when the last daily run happened |

Use the sidebar to switch between **US stocks** and **Crypto** and to pick the strategy.

## Command line

Every `tradebot` command needs the virtual environment active (`source .venv/bin/activate`),
or call it as `.venv/bin/tradebot`.

```bash
tradebot strategies                          # list strategies and default parameters
tradebot scan                                # signals for US stocks + crypto
tradebot scan -m crypto                      # crypto only (-m us, -m india also work)
tradebot scan --symbols AAPL TSLA -s rsi_reversion
tradebot backtest AAPL --years 5 --trades    # backtest one symbol
tradebot backtest -m crypto                  # whole crypto watchlist as one portfolio
tradebot backtest -m us --compare            # rank every strategy on the US watchlist
tradebot backtest NVDA -s sma_crossover -p fast=10 -p slow=30
tradebot paper run                           # update the paper accounts with today's signals
tradebot paper status
tradebot paper reset
tradebot dashboard                           # web dashboard on http://localhost:8501
tradebot daily                               # end-of-day job: paper update + alerts on new signals
tradebot alerts test                         # send a test alert to every enabled channel
tradebot schedule install|status|run-now|uninstall   # run `tradebot daily` automatically
tradebot broker status|run|halt|resume|confirm       # Alpaca account (see "Broker: Alpaca")
tradebot research -m us -s all --matrix      # walk-forward test every strategy × entry filter
tradebot research -m crypto -s macd_trend --folds   # one strategy, show each fold's settings
```

**Walk-forward research** (`tradebot research`) tunes each strategy on 3 years of history,
tests it on the next 6 months, steps forward and repeats, then reports only those unseen
test periods next to the default settings and buy & hold. Filter presets: `none`, `regime`
(index above its 200-day average), `regime+exit` (also sell everything when it drops below),
`rs` (top half of the watchlist by 6-month return), `regime+rs`, `volume`. Results so far are
in `docs/PROJECT_PLAN.md`.

**Survivorship-free test on the S&P 500.** Testing today's winners over the past flatters every
strategy. `tradebot universe update` rebuilds which stocks were in the S&P 500 each quarter since
2013 (from Wikipedia's page history, a few minutes once) and downloads their prices into
`data_cache/sp500/` (needs `pip install -e ".[research]"`). Then:

```bash
tradebot research --universe sp500 -s all --seeds 5          # every strategy vs SPY
tradebot research --universe sp500 -s sma_crossover --max-per-sector 2
```

**Fundamentals screen** for a long-term watchlist: ranks S&P 500 stocks against others in their
sector on quality (ROE, margin), growth (revenue, earnings), value (forward earnings yield) and
safety (low debt). Current Yahoo figures, cached 7 days; it favours companies at peak earnings
(e.g. cyclicals), so treat it as a list to research, not a buy list.

```bash
tradebot screen                                  # top 25 overall
tradebot screen --sector "Health Care" --top 10
```

**Sector cap:** set `risk.max_per_sector` (e.g. 2) in `config/settings.yaml` to limit open positions
per sector in paper trading and backtests; sectors come from the `sectors:` list there.

**When to run:** US daily candles are final after 4:00pm ET; crypto candles close at
00:00 UTC. Until then TradeBot ignores today's unfinished candle, so running it mid-day
gives you yesterday's signals. Orders from a signal fill at the next candle's open.

**Crypto notes:** tickers are Yahoo's `BTC-USD` style, quantities are fractional
(0.0001 steps), and costs assume a 0.1% exchange fee plus 0.1% slippage per side.

## Broker: Alpaca (Phase 5)

TradeBot can send its daily orders to an [Alpaca](https://alpaca.markets) account. **Paper
trading is free** and uses simulated money; that's the intended setup.

**Setup (paper):**

1. Create an Alpaca account, open the **Paper Trading** dashboard and generate API keys.
2. Add them to `.env` (never commit it):
   `ALPACA_PAPER_KEY_ID=...` and `ALPACA_PAPER_SECRET_KEY=...`
3. Check the connection: `tradebot broker status`
4. Preview without sending anything: `tradebot broker run --dry-run`
5. Set `broker.enabled: true` in `config/settings.yaml`. From then on the daily job sends orders
   to the paper account each evening and lists them in its alert.

**What it sends:** for a new BUY on a US stock, one bracket order (market buy with an attached
stop-loss and take-profit, managed by Alpaca). For crypto, a market buy, then a protective
stop-limit sell on the next run, and a market sell once a daily high reaches the target. A SELL
signal cancels the symbol's open orders and sells the position. Sizes follow `risk:`, based on the
Alpaca account's equity.

**Safety rules (paper and live):**

| Rule | Setting / command |
|---|---|
| Kill switch: send nothing | `tradebot broker halt` (add `--close-all` to also sell everything), `tradebot broker resume` |
| No new buys after a bad day | `broker.daily_loss_limit` (default 3% since the previous close) |
| Order size cap | `broker.max_order_value` (default $2,000) |
| New buys per run | `broker.max_orders_per_run` (default 5) |
| Each signal acted on once | state/broker/*_processed.json + Alpaca client order IDs |

**Live mode (real money) is off.** It needs all of: `broker.mode: live`, separate
`ALPACA_LIVE_KEY_ID` / `ALPACA_LIVE_SECRET_KEY` in `.env`, and your approval of every order:
the daily job only *proposes* live orders (the alert says so), and `tradebot broker confirm`, run
at a terminal, shows each one and sends it only if you type `yes`. Proposals expire after 20 hours.
The scheduled job can never confirm. Run paper for a good while before considering it.

## Strategies (long-only)

| Name | Buy | Sell |
|---|---|---|
| `sma_crossover` (default for US) | SMA20 crosses above SMA50, price above SMA200 | SMA20 crosses below SMA50 |
| `rsi_reversion` | RSI(14) recovers above 30 while price above SMA200 | RSI rises above 65 |
| `macd_trend` (default for crypto) | MACD crosses above signal, price above EMA100 | MACD crosses below signal |
| `composite` | 2 of the 3 above are 'long' at once | all 3 have turned flat |

Each market can use its own strategy: `strategy.by_market` in `config/settings.yaml` (US uses
SMA crossover, crypto MACD trend; see `docs/PROJECT_PLAN.md` for why). `--strategy` on the
command line, or the dashboard's strategy picker, overrides it.

Every position also has an ATR-based **stop-loss** (2×ATR below entry) and a
**target** (2× the risk above entry). Position size risks 1% of the account per trade,
capped at 20% of the account, with at most 8 open positions per account.
Indexes such as `^NSEI` are scanned for reference but never traded. All of this is set in `config/settings.yaml`.

## Daily auto-run and alerts

`tradebot daily` is the end-of-day job. It updates the paper account (fills queued orders,
applies stops and targets, queues orders from new signals) and sends an alert listing **new**
BUY/SELL signals and paper-account changes. Each signal is alerted once: running it again the
same evening, on a weekend or on a market holiday sends nothing for candles already covered.

**Run it automatically every day (macOS):**

```bash
tradebot schedule install          # once a day after 20:15 New York time (schedule: in settings.yaml)
tradebot schedule status           # installed? next run, last run and its result
tradebot schedule run-now          # run the full job now to test it
tradebot schedule uninstall
```

20:15 New York time is after both the US close and the crypto daily close (00:00 UTC), all year,
whatever timezone the Mac is set to. The job checks every 15 minutes and does the real work once
per evening, so a run missed while the Mac was asleep or off happens soon after it wakes, and a
failed run (e.g. no internet) is retried, with one failure alert. The output goes to
`state/logs/daily.log`, and the Paper portfolio tab shows the last run.

> The project must not live in `~/Documents`, `~/Desktop` or `~/Downloads`: macOS blocks
> background jobs from reading those folders, so the scheduled run would fail.

**Alert channels** (`alerts:` in `config/settings.yaml`):

| Channel | Setup |
|---|---|
| `console` | none: printed to the terminal and the log |
| `macos` | none: a notification banner on this Mac |
| `telegram` | see below |

**Telegram setup:**

1. In Telegram, message **@BotFather**, send `/newbot` and follow the prompts. It replies with a token.
2. `cp .env.example .env` and set `TELEGRAM_BOT_TOKEN=` to that token. `.env` is git-ignored; never commit it.
3. Open your new bot in Telegram and send it any message.
4. Run `tradebot alerts telegram-chat-id` and copy the `TELEGRAM_CHAT_ID=...` line into `.env`.
5. Set `alerts.telegram: true` in `config/settings.yaml`, then run `tradebot alerts test`.

## Tests

```bash
pytest
```

The tests use synthetic price data and need no network access.

## Layout

```
config/settings.yaml   active markets, watchlists, strategy, risk, costs, paper account, alerts
dashboard/             Streamlit app (app.py) and Plotly charts (charts.py)
src/tradebot/
  data/                data providers (yfinance), disk cache, unfinished-candle filter
  indicators.py        SMA, EMA, RSI, MACD, ATR, Bollinger
  strategies/          strategy classes + registry
  risk.py              stop / target / position sizing
  backtest/            backtest engine and metrics
  paper/               paper-trading ledger
  scanner.py           watchlist -> signals
  alerts/              console + Telegram
  services.py          shared logic used by both the CLI and the dashboard
  cli.py               `tradebot` command
tests/
```
