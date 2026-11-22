"""Daily-bar backtester for long-only strategies, single symbol or whole portfolio.

Rules (kept identical to the paper broker so results are comparable):
- A signal on bar t's close is filled at that symbol's next bar open, adjusted by slippage.
- Entries are sized with `risk.plan_long` from the ATR at the signal bar and the
  portfolio's equity (marked at the latest closes), limited by available cash.
- At most `max_positions` open at once; when more BUYs arrive than slots, they fill in
  watchlist order.
- While in a position, each bar checks the stop first, then the target (conservative,
  since daily bars can't show which came first). Gaps fill at the open.
- Commission is charged per side as a fraction of trade value.
- One market per run (US, crypto, India): quantities and calendars differ.
- Crypto quantities are fractional (0.0001 steps); stocks trade whole shares.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..config import PERIODS_PER_YEAR, QTY_STEP, market_of
from ..risk import RiskConfig, affordable_qty, plan_long
from ..strategies.base import Strategy
from .metrics import compute_metrics


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    quantity: float
    stop_loss: float
    target: float
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    cost: float = 0.0          # cash paid including commission
    proceeds: float = 0.0      # cash received including commission
    bars_held: int = 0

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost

    @property
    def return_pct(self) -> float:
        return self.pnl / self.cost if self.cost else 0.0


@dataclass
class BacktestResult:
    symbol: str                       # symbol, or "PORTFOLIO(n)"
    strategy: str
    equity: pd.Series
    trades: List[Trade]
    signals: pd.DataFrame             # single-symbol runs: that symbol's signals
    metrics: Dict[str, float] = field(default_factory=dict)
    per_symbol: pd.DataFrame = field(default_factory=pd.DataFrame)
    skipped_entries: int = 0          # BUYs that couldn't be filled (no slot / cash / size)

    def trades_frame(self) -> pd.DataFrame:
        rows = []
        for t in self.trades:
            d = asdict(t)
            d.update(pnl=t.pnl, return_pct=t.return_pct)
            rows.append(d)
        return pd.DataFrame(rows)


@dataclass
class _Book:
    """Per-symbol arrays prepared once before the day loop."""
    index: pd.DatetimeIndex
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    signal: np.ndarray
    atr: np.ndarray


def run_portfolio_backtest(
    data: Dict[str, pd.DataFrame],
    strategy: Strategy,
    capital: float = 10_000.0,
    risk: Optional[RiskConfig] = None,
    commission: float = 0.0,
    slippage: float = 0.0,
    max_positions: Optional[int] = None,
    benchmark: Optional[pd.Series] = None,
) -> BacktestResult:
    """Backtest one strategy over several symbols sharing one cash account."""
    risk = risk or RiskConfig()
    max_positions = max_positions or risk.max_positions
    market = market_of(next(iter(data))) if data else "us"
    step = QTY_STEP.get(market, 1.0)
    data = {s: df for s, df in data.items() if len(df) > strategy.warmup + 1}
    if not data:
        raise ValueError("no symbol has enough history for this strategy")

    books: Dict[str, _Book] = {}
    all_signals: Dict[str, pd.DataFrame] = {}
    for sym, df in data.items():
        sigs = strategy.generate(df)
        all_signals[sym] = sigs
        books[sym] = _Book(df.index, *(df[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close")),
                           sigs["signal"].to_numpy(), ind.atr(df, risk.atr_period).to_numpy())
    order = list(books)  # watchlist order decides who gets a slot first
    pos_in_book = {s: {ts: i for i, ts in enumerate(b.index)} for s, b in books.items()}
    dates = sorted(set().union(*(b.index for b in books.values())))

    cash = float(capital)
    positions: Dict[str, Trade] = {}
    pending: Dict[str, dict] = {}       # symbol -> {"side": "buy"/"sell", "atr": float}
    last_close: Dict[str, float] = {}
    trades: List[Trade] = []
    equity: List[float] = []
    invested: List[float] = []
    skipped = 0

    def mark_to_market() -> float:
        return cash + sum(t.quantity * last_close[s] for s, t in positions.items())

    def close_pos(sym: str, i: int, raw_price: float, reason: str) -> None:
        nonlocal cash
        t = positions.pop(sym)
        px = raw_price * (1 - slippage)
        t.exit_date, t.exit_price, t.exit_reason = books[sym].index[i], px, reason
        t.proceeds = px * t.quantity * (1 - commission)
        t.bars_held = i - pos_in_book[sym][t.entry_date]
        cash += t.proceeds
        trades.append(t)

    last_day = dates[-1]
    for day in dates:
        today = {s: pos_in_book[s][day] for s in order if day in pos_in_book[s]}

        # 1) Exits queued yesterday free cash and slots before new entries.
        for sym, i in today.items():
            if pending.get(sym, {}).get("side") == "sell":
                del pending[sym]
                if sym in positions:
                    close_pos(sym, i, books[sym].open[i], "signal")

        # 2) Entries, sized off equity at the latest known closes.
        for sym, i in today.items():
            if pending.get(sym, {}).get("side") != "buy":
                continue
            order_ = pending.pop(sym)
            if sym in positions:
                continue
            if len(positions) >= max_positions:
                skipped += 1
                continue
            fill = books[sym].open[i] * (1 + slippage)
            plan = plan_long(fill, order_["atr"], mark_to_market(), risk, step)
            qty = affordable_qty(plan.quantity, fill, cash, commission, step) if plan else 0.0
            if qty < step:
                skipped += 1
                continue
            cost = fill * qty * (1 + commission)
            cash -= cost
            positions[sym] = Trade(sym, day, fill, qty, plan.stop_loss, plan.target, cost=cost)
            last_close.setdefault(sym, fill)

        # 3) Protective exits inside the bar.
        for sym, i in today.items():
            t = positions.get(sym)
            if t is None:
                continue
            b = books[sym]
            if b.low[i] <= t.stop_loss:
                close_pos(sym, i, min(b.open[i], t.stop_loss), "stop")
            elif b.high[i] >= t.target:
                close_pos(sym, i, max(b.open[i], t.target), "target")

        # 4) Signals at the close.
        for sym, i in today.items():
            b = books[sym]
            last_close[sym] = b.close[i]
            s = b.signal[i]
            if s == 1 and sym not in positions and sym not in pending:
                pending[sym] = {"side": "buy", "atr": float(b.atr[i])}
            elif s == -1 and sym in positions:
                pending[sym] = {"side": "sell"}

        if day == last_day:
            for sym in list(positions):
                b = books[sym]
                close_pos(sym, len(b.index) - 1, b.close[-1], "end")

        eq = mark_to_market()
        equity.append(eq)
        invested.append((eq - cash) / eq if eq > 0 else 0.0)

    equity_s = pd.Series(equity, index=pd.DatetimeIndex(dates), name="equity")
    invested_s = pd.Series(invested, index=equity_s.index)

    # Measure from the first day any symbol could trade (after indicator warm-up).
    start_ts = min(b.index[min(strategy.warmup, len(b.index) - 1)] for b in books.values())
    eq_eval = equity_s[equity_s.index >= start_ts]
    bh = {s: float(b.close[-1] / b.close[min(strategy.warmup, len(b.close) - 1)] - 1) for s, b in books.items()}
    metrics = compute_metrics(eq_eval, trades, invested_s[invested_s.index >= start_ts],
                              float(np.mean(list(bh.values()))), PERIODS_PER_YEAR.get(market, 252))
    if benchmark is not None and not benchmark.empty:
        bm = benchmark[benchmark.index >= start_ts]
        metrics["benchmark_return"] = float(bm.iloc[-1] / bm.iloc[0] - 1) if len(bm) > 1 else 0.0

    per_symbol = _per_symbol(trades, bh)
    label = order[0] if len(order) == 1 else f"PORTFOLIO({len(order)})"
    sigs = all_signals[order[0]] if len(order) == 1 else pd.DataFrame()
    return BacktestResult(label, repr(strategy), equity_s, trades, sigs, metrics, per_symbol, skipped)


def run_backtest(
    df: pd.DataFrame,
    strategy: Strategy,
    capital: float = 10_000.0,
    risk: Optional[RiskConfig] = None,
    commission: float = 0.0,
    slippage: float = 0.0,
    symbol: str = "",
) -> BacktestResult:
    """Single-symbol backtest (a one-stock portfolio)."""
    return run_portfolio_backtest({symbol or "SYMBOL": df}, strategy, capital, risk,
                                  commission, slippage, max_positions=1)


def _per_symbol(trades: List[Trade], buy_hold: Dict[str, float]) -> pd.DataFrame:
    rows = []
    for sym, bh in buy_hold.items():
        ts = [t for t in trades if t.symbol == sym]
        pnl = sum(t.pnl for t in ts)
        rows.append({
            "symbol": sym, "trades": len(ts),
            "win_rate": float(np.mean([t.pnl > 0 for t in ts])) if ts else 0.0,
            "pnl": pnl, "avg_return": float(np.mean([t.return_pct for t in ts])) if ts else 0.0,
            "buy_hold": bh,
        })
    return pd.DataFrame(rows).sort_values("pnl", ascending=False).reset_index(drop=True)
