"""Chart alerts: Telegram (and the other alert channels) for BUY/SELL signals on intraday bars.

You pick symbol + bar interval + strategy, e.g. SOL-USD on 2-hour bars with macd_trend, in the
dashboard (Chart tab or the Alerts tab) or with `tradebot watch add SOL-USD --interval 2h`.
The scheduled job (`tradebot schedule install`) checks every 5 minutes; when a bar closes with a
BUY or SELL marker, the same marker you see on the dashboard chart, it sends one alert.

* Only signals on bars that closed after you added the alert are sent: no history dump.
* Each signal is sent once. The forming bar is never used, so a marker can't vanish later.
* Signals only. Nothing here touches the paper account or Alpaca.

Files (both under state/, so they move with the rest of the state when you change machines):
  state/watchlist.json    what to watch (written by the dashboard / CLI)
  state/watch_state.json  last bar checked per entry (written by the job)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from . import indicators as ind
from .alerts import Alert
from .config import market_of, resolve_path
from .data.base import MARKET_TZ
from .risk import RiskConfig

# Bar intervals you can get alerts for, in minutes. Shorter bars would close between checks.
WATCH_INTERVALS: Dict[str, int] = {"15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240}
# Yahoo interval downloaded for each, and how many days (Yahoo keeps 60 days of 15m/30m bars).
SOURCE: Dict[str, Any] = {"15m": ("15m", 59), "30m": ("30m", 59), "1h": ("1h", 365),
                          "2h": ("1h", 365), "4h": ("1h", 365)}
SESSION_END = {"us": time(16, 0), "india": time(15, 30)}   # regular-hours close; crypto never closes
SESSION_START = {"us": time(9, 30), "india": time(9, 15)}
MAX_ALERTS_PER_ENTRY = 3   # after a long sleep, send at most the 3 newest signals per entry

BarLoader = Callable[[str, str], pd.DataFrame]   # (symbol, interval) -> OHLCV bars


# ---- symbols ----------------------------------------------------------------------------------
def normalize_symbol(text: str) -> str:
    """Accept exchange-style pairs: SOL/USDT, SOLUSDT, SOL-USDT and SOL/USD all become SOL-USD.

    Yahoo Finance (the data source) only has USD pairs for most coins; USDT trades within
    about 0.1% of USD, so the bars and signals are effectively the same.
    """
    s = re.sub(r"\s+", "", str(text)).upper()
    m = re.fullmatch(r"([A-Z0-9]+)(?:/(USDT|USDC|USD)|-(USDT|USDC)|(USDT|USDC))", s)
    return f"{m.group(1)}-USD" if m else s


# ---- bars -------------------------------------------------------------------------------------
def resample_bars(df: pd.DataFrame, interval: str, market: str) -> pd.DataFrame:
    """Build 2h/4h bars from 1h bars. Crypto bars are aligned to 00:00 UTC (like exchanges);
    stock bars start at the session open (9:30-11:30, 11:30-13:30, ... in New York)."""
    hours = WATCH_INTERVALS[interval] // 60
    if df.empty or hours <= 1:
        return df
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if market == "crypto":
        return df.resample(f"{hours}h", origin="epoch", label="left", closed="left").agg(agg).dropna(subset=["close"])
    day = df.index.normalize()
    group = pd.Series(df.groupby(day).cumcount().to_numpy() // hours, index=df.index)
    keys = [day, group.to_numpy()]
    out = df.groupby(keys).agg(agg)
    out.index = df.index.to_series().groupby(keys).first().to_numpy()
    out.index.name = df.index.name
    return out


def bar_end(start: pd.Timestamp, interval: str, market: str) -> pd.Timestamp:
    """When a bar that opened at `start` (exchange time) closes; stock bars stop at the session close."""
    end = start + pd.Timedelta(minutes=WATCH_INTERVALS[interval])
    close = SESSION_END.get(market)
    if close is not None:
        end = min(end, pd.Timestamp.combine(start.date(), close))
    return end


def to_utc(ts: pd.Timestamp, market: str) -> datetime:
    """Exchange-time naive timestamp -> aware UTC datetime."""
    from zoneinfo import ZoneInfo

    return ts.to_pydatetime().replace(tzinfo=ZoneInfo(MARKET_TZ.get(market, "UTC"))).astimezone(timezone.utc)


def closed_bars(df: pd.DataFrame, interval: str, market: str, now: datetime) -> pd.DataFrame:
    """Drop the bar that is still forming (its close is after `now`)."""
    if df.empty:
        return df
    ends = [to_utc(bar_end(ts, interval, market), market) for ts in df.index]
    return df[[e <= now for e in ends]]


def yahoo_bars(symbol: str, interval: str) -> pd.DataFrame:
    """Bars for a watch entry from Yahoo, including the forming bar (dropped later)."""
    from .data import YFinanceProvider

    src, days = SOURCE[interval]
    df = YFinanceProvider().get_intraday(symbol, src, days)
    return resample_bars(df, interval, market_of(symbol))


# ---- the watchlist ----------------------------------------------------------------------------
def paths(settings: Dict[str, Any]) -> Dict[str, Path]:
    a = settings.get("alerts", {})
    return {"list": resolve_path(a.get("watchlist_path", "state/watchlist.json")),
            "state": resolve_path(a.get("watch_state_path", "state/watch_state.json"))}


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def entry_key(e: Dict[str, Any]) -> str:
    return f"{e['symbol']}|{e['interval']}|{e['strategy']}"


def load_watchlist(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(_load(paths(settings)["list"], {}).get("items", []))


def load_watch_state(settings: Dict[str, Any]) -> Dict[str, Any]:
    return dict(_load(paths(settings)["state"], {}))


def add_watch(settings: Dict[str, Any], symbol: str, interval: str, strategy: str,
              now: Optional[datetime] = None) -> Dict[str, Any]:
    """Add (or keep) an entry. Alerts start with the first bar that closes after now."""
    from .strategies import REGISTRY

    if interval not in WATCH_INTERVALS:
        raise ValueError(f"interval must be one of {', '.join(WATCH_INTERVALS)}")
    if strategy not in REGISTRY:
        raise ValueError(f"unknown strategy {strategy!r}; use one of {', '.join(REGISTRY)}")
    entry = {"symbol": normalize_symbol(symbol), "interval": interval, "strategy": strategy,
             "added": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")}
    items = load_watchlist(settings)
    if not any(entry_key(e) == entry_key(entry) for e in items):
        items.append(entry)
        _save(paths(settings)["list"], {"items": items})
    return entry


def remove_watch(settings: Dict[str, Any], symbol: str, interval: str, strategy: Optional[str] = None) -> int:
    symbol = normalize_symbol(symbol)
    items = load_watchlist(settings)
    keep = [e for e in items if not (e["symbol"] == symbol and e["interval"] == interval
                                     and (strategy is None or e["strategy"] == strategy))]
    _save(paths(settings)["list"], {"items": keep})
    return len(items) - len(keep)


def is_watched(settings: Dict[str, Any], symbol: str, interval: str, strategy: str) -> bool:
    key = entry_key({"symbol": normalize_symbol(symbol), "interval": interval, "strategy": strategy})
    return any(entry_key(e) == key for e in load_watchlist(settings))


# ---- checking ---------------------------------------------------------------------------------
@dataclass
class ChartSignal:
    symbol: str
    interval: str
    strategy: str
    action: str          # BUY / SELL
    bar_start: str       # bar open time in the chosen time zone (`tradebot.timezones`), as on the chart
    closed_utc: datetime
    price: float
    reason: str
    stop: Optional[float] = None
    target: Optional[float] = None
    tz: str = "UTC"      # label of the time zone bar_start is in

    def lines(self, now: datetime) -> List[str]:
        tz = self.tz
        out = [f"{'🟢' if self.action == 'BUY' else '🔴'} {self.action} {self.symbol} · {self.interval} bars · {self.strategy}",
               f"  Close {fmt_price(self.price)} on the {self.bar_start} bar ({tz} time)"]
        if self.action == "BUY" and self.stop is not None:
            out.append(f"  Stop {fmt_price(self.stop)} · target {fmt_price(self.target)}")
        out.append(f"  Why: {self.reason}")
        late = now - self.closed_utc
        if late > timedelta(minutes=WATCH_INTERVALS[self.interval]):
            out.append(f"  Late: that bar closed {_ago(late)} ago (the computer was asleep or offline).")
        return out


def fmt_price(p: float) -> str:
    return f"{p:,.2f}" if p >= 100 else (f"{p:.4f}" if p >= 1 else f"{p:.6g}")


def _ago(delta: timedelta) -> str:
    mins = int(delta.total_seconds() // 60)
    return f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"


@dataclass
class WatchResult:
    signals: List[ChartSignal] = field(default_factory=list)
    checked: int = 0
    skipped: int = 0                               # not due yet (no new bar could have closed)
    errors: Dict[str, str] = field(default_factory=dict)

    def alert(self, now: datetime) -> Alert:
        lines: List[str] = []
        for s in self.signals:
            lines += s.lines(now)
        if self.signals:
            lines.append("Chart alert, for reference: strategies are tested on daily candles, not intraday bars. "
                         "No order was placed.")
        names = ", ".join(f"{s.action} {s.symbol} {s.interval}" for s in self.signals)
        title = f"TradeBot chart alert: {names}" if len(self.signals) <= 2 else f"TradeBot: {len(self.signals)} chart alerts"
        return Alert(title, lines, names)


def _market_closed(market: str, now: datetime) -> bool:
    """True on weekends / outside regular hours for stocks (no new bar can close then)."""
    if market == "crypto" or market not in SESSION_END:
        return False
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(MARKET_TZ[market]))
    if local.weekday() >= 5:
        return True
    # The last bar closes at the session end; allow 30 minutes for Yahoo to publish it.
    late = (datetime.combine(local.date(), SESSION_END[market]) + timedelta(minutes=30)).time()
    return not (SESSION_START[market] <= local.time().replace(tzinfo=None) <= late)


def check(settings: Dict[str, Any], load_bars: Optional[BarLoader] = None,
          now: Optional[datetime] = None, force: bool = False) -> WatchResult:
    """Look for BUY/SELL signals on bars that closed since the last check. Updates watch_state.json."""
    from . import timezones
    from .services import strategy_from

    tz_pref = timezones.load_pref()
    now = now or datetime.now(timezone.utc)
    load_bars = load_bars or yahoo_bars
    risk = RiskConfig.from_settings(settings)
    state = load_watch_state(settings)
    result = WatchResult()
    items = load_watchlist(settings)
    for e in items:
        key, market = entry_key(e), market_of(e["symbol"])
        st = state.get(key, {})
        since = datetime.fromisoformat(st.get("last_close_utc") or e["added"])
        step = timedelta(minutes=WATCH_INTERVALS[e["interval"]])
        # Once a bar close is known, the next can't come sooner than one interval later.
        not_yet = bool(st.get("last_close_utc")) and now < since + step
        if not force and (not_yet or _market_closed(market, now)):
            result.skipped += 1
            continue
        try:
            strategy = strategy_from(settings, e["strategy"])
            bars = closed_bars(load_bars(e["symbol"], e["interval"]), e["interval"], market, now)
            if len(bars) < strategy.warmup + 2:
                raise ValueError(f"only {len(bars)} closed {e['interval']} bars; {e['strategy']} needs {strategy.warmup + 2}")
            sigs = strategy.generate(bars)
            atr = ind.atr(bars, risk.atr_period)
            closes = [to_utc(bar_end(ts, e["interval"], market), market) for ts in bars.index]
            zone = timezones.resolve(tz_pref, market)
            shown_at = timezones.convert_index(bars.index, market, zone)
            found = []
            for i, ts in enumerate(bars.index):
                if closes[i] <= since or sigs["signal"].iloc[i] == 0:
                    continue
                buy = sigs["signal"].iloc[i] > 0
                price, a = float(bars["close"].iloc[i]), float(atr.iloc[i])
                stop = price - risk.atr_stop_mult * a if buy and a == a else None
                found.append(ChartSignal(
                    e["symbol"], e["interval"], e["strategy"], "BUY" if buy else "SELL",
                    f"{shown_at[i]:%a %d %b %H:%M}", closes[i], price, str(sigs["reason"].iloc[i]),
                    stop, price + risk.reward_risk * (price - stop) if stop is not None else None,
                    timezones.label(zone)))
            result.signals += found[-MAX_ALERTS_PER_ENTRY:]
            st = {"last_close_utc": closes[-1].isoformat(), "last_bar": f"{bars.index[-1]:%Y-%m-%d %H:%M}"}
            if found:
                st["last_signal"] = f"{found[-1].action} on the {found[-1].bar_start} bar"
            else:
                st["last_signal"] = state.get(key, {}).get("last_signal", "")
            result.checked += 1
        except Exception as exc:
            st = dict(st, error=f"{type(exc).__name__}: {exc}")
            result.errors[key] = st["error"]
        st["checked_utc"] = now.isoformat(timespec="seconds")
        if key not in result.errors:
            st.pop("error", None)
        state[key] = st
    # forget entries that were removed from the watchlist
    live = {entry_key(e) for e in items}
    _save(paths(settings)["state"], {k: v for k, v in state.items() if k in live})
    return result


def run_and_alert(settings: Dict[str, Any], send_alerts: bool = True, force: bool = False,
                  load_bars: Optional[BarLoader] = None, now: Optional[datetime] = None) -> WatchResult:
    """Check every entry and send one alert with whatever is new (nothing when quiet)."""
    from .alerts import send

    now = now or datetime.now(timezone.utc)
    result = check(settings, load_bars, now, force)
    if result.signals:
        alert = result.alert(now)
        if send_alerts:
            send(alert, settings)
        else:
            print(alert.text())
    return result


# ---- CLI --------------------------------------------------------------------------------------
def cmd_watch(args, settings) -> int:
    from .services import strategy_name_for

    if args.action == "add":
        if not args.symbol:
            print("Usage: tradebot watch add SYMBOL --interval 2h [--strategy macd_trend]")
            return 2
        sym = normalize_symbol(args.symbol)
        strat = args.strategy or strategy_name_for(settings, market_of(sym))
        add_watch(settings, sym, args.interval, strat)
        note = f" ({args.symbol} -> Yahoo's {sym})" if sym != args.symbol.upper() else ""
        print(f"Watching {sym} on {args.interval} bars with {strat}{note}. "
              "Alerts start with the next bar that closes.")
    elif args.action == "remove":
        if not args.symbol:
            print("Usage: tradebot watch remove SYMBOL --interval 2h")
            return 2
        n = remove_watch(settings, args.symbol, args.interval, args.strategy)
        print(f"Removed {n} entr{'y' if n == 1 else 'ies'}.")
    elif args.action == "check":
        r = run_and_alert(settings, send_alerts=not args.no_alerts, force=True)
        print(f"Checked {r.checked}, {len(r.signals)} new signal(s)" + ("" if r.signals else "; nothing sent."))
        for k, err in r.errors.items():
            print(f"!! {k}: {err}")
        return 1 if r.errors and not r.checked else 0
    else:
        items, state = load_watchlist(settings), load_watch_state(settings)
        if not items:
            print("No chart alerts. Add one: tradebot watch add SOL-USD --interval 2h")
        for e in items:
            st = state.get(entry_key(e), {})
            info = st.get("error") or (f"last bar {st['last_bar']}" if st.get("last_bar") else "not checked yet")
            if st.get("last_signal"):
                info += f", last alert {st['last_signal']}"
            print(f"{e['symbol']:<12} {e['interval']:<4} {e['strategy']:<14} {info}")
    return 0


def add_parser(sub) -> None:
    sp = sub.add_parser("watch", help="Telegram alerts for BUY/SELL signals on intraday chart bars (e.g. SOL 2h)")
    sp.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "check"])
    sp.add_argument("symbol", nargs="?", help="e.g. SOL-USD, SOL/USDT or AAPL")
    sp.add_argument("--interval", default="1h", choices=list(WATCH_INTERVALS))
    sp.add_argument("--strategy", help="default: the strategy configured for the symbol's market")
    sp.add_argument("--no-alerts", action="store_true", help="check: print instead of sending")
