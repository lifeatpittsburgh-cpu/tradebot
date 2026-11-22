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
cd ~/Documents/Projects/Personal/TradeBot/tradingbot
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,dashboard]"
```

**2. Start the dashboard:**

```bash
cd ~/Documents/Projects/Personal/TradeBot/tradingbot
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
| Paper portfolio | Simulated account. Click **Run paper update** once a day after the close to fill orders, apply stops/targets and queue new signals |

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
```

**When to run:** US daily candles are final after 4:00pm ET; crypto candles close at
00:00 UTC. Until then TradeBot ignores today's unfinished candle, so running it mid-day
gives you yesterday's signals. Orders from a signal fill at the next candle's open.

**Crypto notes:** tickers are Yahoo's `BTC-USD` style, quantities are fractional
(0.0001 steps), and costs assume a 0.1% exchange fee plus 0.1% slippage per side.

## Strategies (long-only)

| Name | Buy | Sell |
|---|---|---|
| `sma_crossover` | SMA20 crosses above SMA50, price above SMA200 | SMA20 crosses below SMA50 |
| `rsi_reversion` | RSI(14) recovers above 30 while price above SMA200 | RSI rises above 65 |
| `macd_trend` | MACD crosses above signal, price above EMA100 | MACD crosses below signal |
| `composite` (default) | 2 of the 3 above are 'long' at once | all 3 have turned flat |

Every position also has an ATR-based **stop-loss** (2×ATR below entry) and a
**target** (2× the risk above entry). Position size risks 1% of the account per trade,
capped at 20% of the account, with at most 8 open positions per account.
Indexes such as `^NSEI` are scanned for reference but never traded. All of this is set in `config/settings.yaml`.

## Telegram alerts

1. Create a bot with @BotFather and get your chat id.
2. `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Set `alerts.telegram: true` in `config/settings.yaml`.

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
