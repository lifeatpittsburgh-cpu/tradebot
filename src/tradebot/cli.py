"""Command-line interface.

    tradebot strategies                         list strategies and their parameters
    tradebot scan [--market us|crypto|india|all] today's signals for the watchlist
    tradebot dashboard                          open the web dashboard
    tradebot backtest AAPL [--strategy NAME]    backtest one symbol
    tradebot backtest -m india [--compare]      backtest a watchlist as one portfolio
    tradebot paper run | status | reset         paper-trading account
"""
from __future__ import annotations

import argparse
import sys
import warnings

# macOS system Python links LibreSSL; urllib3 warns on every run even though HTTPS works.
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")
from datetime import date, timedelta
from typing import Any, Dict, List

import pandas as pd
import yaml

from . import __version__
from .alerts import notify
from .backtest import run_portfolio_backtest
from .config import CURRENCY, MARKETS, is_tradable, load_settings, market_of
from .paper import PaperBroker
from .risk import RiskConfig
from .scanner import scan
from .services import (DISCLAIMER, accounts_frame, broker_from, pending_frame, positions_frame,
                       provider_from, run_paper_update, signals_frame, strategy_from, symbols_for)
from .strategies import REGISTRY, get_strategy

def _strategy_from(args, settings):
    params = {}
    for kv in getattr(args, "param", None) or []:
        k, v = kv.split("=", 1)
        params[k] = yaml.safe_load(v)  # "20" -> 20, "1.5" -> 1.5, "false" -> False
    return strategy_from(settings, args.strategy, params)


def _symbols(args, settings) -> List[str]:
    if getattr(args, "symbols", None):
        return args.symbols
    return symbols_for(settings, args.market)


_provider = provider_from


# ---- commands ------------------------------------------------------------------------
def cmd_strategies(args, settings) -> int:
    for name, cls in REGISTRY.items():
        doc = (cls.__doc__ or "").strip().splitlines()[0]
        print(f"{name:<15} {doc}")
        print(f"{'':<15} defaults: {cls.defaults}")
    return 0


def cmd_scan(args, settings) -> int:
    strategy = _strategy_from(args, settings)
    risk = RiskConfig.from_settings(settings)
    capital = settings["paper"]["starting_capital"]
    symbols = _symbols(args, settings)
    print(f"Scanning {len(symbols)} symbols with {strategy!r} ...")
    signals, errors = scan(symbols, _provider(settings), strategy, risk,
                           settings["data"]["lookback_days"], equity_by_market=capital)

    table = signals_frame(signals)
    if not table.empty:
        table["last_signal"] = table["last_signal"] + " " + table["last_signal_date"]
        table = table.drop(columns=["market", "last_signal_date"]).rename(columns={"qty": "qty*"})
        with pd.option_context("display.width", 200, "display.max_colwidth", 60):
            print(table.fillna("").to_string(index=False))
        mkts = sorted({s.market for s in signals})
        print("* qty sized from paper starting capital: " +
              ", ".join(f"{m} {CURRENCY[m]} {capital[m]:,.0f}" for m in mkts if m in capital))
    for sym, err in errors.items():
        print(f"!! {sym}: {err}", file=sys.stderr)
    if not args.no_alerts:
        notify(signals, settings)
    print(DISCLAIMER)
    return 0


def _pct(x: float) -> str:
    return f"{x * 100:,.2f}%"


def _load_many(provider, symbols, start, end):
    data, errors = {}, {}
    for sym in symbols:
        try:
            df = provider.get_history(sym, start, end)
            if df.empty:
                errors[sym] = "no data"
            else:
                data[sym] = df
        except Exception as exc:
            errors[sym] = f"{type(exc).__name__}: {exc}"
    return data, errors


def _metric_rows(m: Dict[str, float]):
    rows = [
        ("Start capital", f"{m['start_capital']:,.2f}"),
        ("Final equity", f"{m['final_equity']:,.2f}"),
        ("Total return", _pct(m["total_return"])),
        ("Buy & hold (equal wt)", _pct(m["buy_hold_return"])),
    ]
    if "benchmark_return" in m:
        rows.append(("Benchmark index", _pct(m["benchmark_return"])))
    rows += [
        ("CAGR", _pct(m["cagr"])),
        ("Max drawdown", _pct(m["max_drawdown"])),
        ("Sharpe", f"{m['sharpe']:.2f}"),
        ("Trades", f"{int(m['trades'])}"),
        ("Win rate", _pct(m["win_rate"])),
        ("Profit factor", f"{m['profit_factor']:.2f}"),
        ("Avg trade return", _pct(m["avg_trade_return"])),
        ("Avg bars held", f"{m['avg_bars_held']:.1f}"),
        ("Avg capital invested", _pct(m["exposure"])),
    ]
    return rows


def cmd_backtest(args, settings) -> int:
    risk = RiskConfig.from_settings(settings)
    if args.max_positions:
        risk.max_positions = args.max_positions
    symbols = args.symbols or settings["watchlists"].get(args.market or "us", [])
    symbols = [s for s in symbols if is_tradable(s)]
    markets = {market_of(s) for s in symbols}
    if not symbols:
        print("No tradable symbols to backtest.", file=sys.stderr)
        return 1
    if len(markets) > 1:
        print("Backtest one market at a time (accounts are in different currencies).", file=sys.stderr)
        return 1
    market = markets.pop()
    costs = settings["costs"][market]
    years = args.years or settings["backtest"]["years"]
    start = date.fromisoformat(args.start) if args.start else date.today() - timedelta(days=int(365.25 * years))
    end = date.fromisoformat(args.end) if args.end else None
    capital = args.capital or settings["paper"]["starting_capital"][market]

    provider = _provider(settings)
    data, errors = _load_many(provider, symbols, start, end)
    for sym, err in errors.items():
        print(f"!! {sym}: {err}", file=sys.stderr)
    if not data:
        return 1
    benchmark = None
    bm_symbol = settings["backtest"]["benchmark"].get(market)
    if bm_symbol and not args.no_benchmark:
        bm_data, _ = _load_many(provider, [bm_symbol], start, end)
        benchmark = bm_data.get(bm_symbol, pd.DataFrame()).get("close")

    strategies = [get_strategy(n) for n in REGISTRY] if args.compare else [_strategy_from(args, settings)]
    first = min(df.index[0] for df in data.values()).date()
    last = max(df.index[-1] for df in data.values()).date()
    print(f"\n{len(data)} symbol(s), {first} -> {last}, {CURRENCY[market]} {capital:,.0f}, "
          f"max {risk.max_positions} positions" + (f", benchmark {bm_symbol}" if benchmark is not None else ""))

    results = []
    for strat in strategies:
        try:
            results.append(run_portfolio_backtest(
                data, strat, capital, risk, costs["commission"], costs["slippage"],
                max_positions=1 if len(data) == 1 else None, benchmark=benchmark))
        except ValueError as exc:
            print(f"!! {strat.name}: {exc}", file=sys.stderr)

    if args.compare:
        rows = [{
            "strategy": r.strategy, "return": _pct(r.metrics["total_return"]), "CAGR": _pct(r.metrics["cagr"]),
            "maxDD": _pct(r.metrics["max_drawdown"]), "sharpe": round(r.metrics["sharpe"], 2),
            "trades": int(r.metrics["trades"]), "win": _pct(r.metrics["win_rate"]),
            "PF": round(r.metrics["profit_factor"], 2), "invested": _pct(r.metrics["exposure"]),
        } for r in sorted(results, key=lambda r: r.metrics["sharpe"], reverse=True)]
        print(pd.DataFrame(rows).to_string(index=False))
        if results:
            m = results[0].metrics
            print(f"Buy & hold (equal weight): {_pct(m['buy_hold_return'])}"
                  + (f"   {bm_symbol}: {_pct(m['benchmark_return'])}" if "benchmark_return" in m else ""))
        print(DISCLAIMER)
        return 0

    res = results[0]
    print(f"{res.symbol}  {res.strategy}")
    print("-" * 60)
    for label, val in _metric_rows(res.metrics):
        print(f"{label:<24}{val:>20}")
    if res.skipped_entries:
        print(f"{'BUYs not filled':<24}{res.skipped_entries:>20}  (no free slot, cash or size)")
    if len(data) > 1:
        ps = res.per_symbol.copy()
        for c in ("win_rate", "avg_return", "buy_hold"):
            ps[c] = ps[c].map(_pct)
        ps["pnl"] = ps["pnl"].map(lambda v: f"{v:+,.0f}")
        print("\nPer symbol\n" + ps.to_string(index=False))
    if args.trades and res.trades:
        tf = res.trades_frame()[["symbol", "entry_date", "entry_price", "exit_date", "exit_price",
                                 "quantity", "exit_reason", "pnl", "return_pct"]]
        tf["entry_date"] = pd.to_datetime(tf["entry_date"]).dt.date
        tf["exit_date"] = pd.to_datetime(tf["exit_date"]).dt.date
        print("\n" + tf.round(2).to_string(index=False))
    if args.csv:
        res.trades_frame().to_csv(args.csv, index=False)
        print(f"\nTrades written to {args.csv}")
    print(DISCLAIMER)
    return 0


def cmd_paper(args, settings) -> int:
    broker = broker_from(settings)
    if args.action == "reset":
        broker.reset()
        broker.save()
        print(f"Paper ledger reset: {broker.path}")
        return 0
    if args.action == "run":
        signals, errors = run_paper_update(settings, broker, _symbols(args, settings), _strategy_from(args, settings))
        for sym, err in errors.items():
            print(f"!! {sym}: {err}", file=sys.stderr)
        notify(signals, settings, broker.events)
    _print_status(broker)
    return 0


def _print_status(broker: PaperBroker) -> None:
    print("\nPaper accounts")
    for _, a in accounts_frame(broker).iterrows():
        print(f"  {a['market']:<7} cash {a['cash']:>14,.2f}  equity {a['equity']:>14,.2f} {a['currency']}"
              f"  ({_pct(a['return'])}, {a['open_positions']} open)")
    pos = positions_frame(broker)
    if not pos.empty:
        print("\nOpen positions\n" + pos.drop(columns=["unrealized_pct"]).round(4).to_string(index=False))
    pend = pending_frame(broker)
    if not pend.empty:
        print("\nPending orders (fill at next open)\n" + pend.to_string(index=False))
    hist = broker.ledger["history"]
    if hist:
        h = pd.DataFrame(hist)
        print(f"\nClosed trades: {len(h)}  win rate {_pct((h['pnl'] > 0).mean())}  realized P&L: "
              + ", ".join(f"{m} {v:+,.2f}" for m, v in h.groupby("market")["pnl"].sum().items()))


def cmd_daily(args, settings) -> int:
    from datetime import datetime, timezone

    from .alerts import send
    from .daily import run_daily, save_last_run

    sched_state, slot = None, None
    if args.if_due:  # called every 15 minutes by the scheduled job: only run once per daily slot
        from . import schedule

        default_time, default_tz = schedule.schedule_config(settings)
        sched_state = schedule.load_state()
        due, slot = schedule.is_due(datetime.now(timezone.utc), args.at or default_time,
                                    args.tz or default_tz, sched_state)
        forced = sched_state.pop("force_next", False)
        if not (due or forced):
            return 0

    print(f"=== TradeBot daily run {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}"
          + (f" (scheduled for {slot:%Y-%m-%d %H:%M %Z})" if slot else "") + " ===")
    report = run_daily(settings)
    print(f"Strategy {report.strategy}, {report.symbols} symbols, {len(report.new_signals)} new signal(s)"
          + (f", {report.repeated_signals} already alerted" if report.repeated_signals else ""))
    for sym, err in report.errors.items():
        print(f"!! {sym}: {err}", file=sys.stderr)
    alert = report.alert()
    # A failing scheduled run retries every 15 minutes; send its failure alert only once per slot.
    repeat_failure = (sched_state is not None and not report.ok
                      and sched_state.get("failure_alerted_slot") == (slot.isoformat() if slot else None))
    if args.no_alerts or repeat_failure:
        print(alert.text() if alert.lines else "Nothing new.")
    else:
        report.alert_failures = [f"{ch}: {err}" for ch, err in send(alert, settings)]
    save_last_run(settings, report)
    if sched_state is not None:
        from . import schedule

        if report.ok:
            sched_state["last_success_slot"] = slot.isoformat()
        else:
            sched_state["failure_alerted_slot"] = slot.isoformat()
        schedule.save_state(sched_state)
    return 0 if report.ok else 1


def cmd_alerts(args, settings) -> int:
    from .alerts import Alert, channels_from, send

    if args.action == "telegram-chat-id":
        from .alerts.telegram import find_chat_ids

        chats = find_chat_ids()
        if not chats:
            print("No messages found. Open your bot in Telegram, send it any message, then run this again.")
            return 1
        for c in chats:
            print(f"{c['type']:<8} {c['name']:<30} TELEGRAM_CHAT_ID={c['id']}")
        print("\nCopy the TELEGRAM_CHAT_ID line into .env, then set alerts.telegram: true in config/settings.yaml.")
        return 0
    try:
        names = [type(c).__name__.replace("Notifier", "") for c in channels_from(settings)]
        print("Enabled channels: " + ", ".join(names))
    except Exception as exc:
        print(f"!! {exc}", file=sys.stderr)
    failures = send(Alert("TradeBot test alert", ["If you can read this, TradeBot alerts are working."],
                          summary="Alerts are working."), settings)
    return 1 if failures else 0


def cmd_schedule(args, settings) -> int:
    from datetime import datetime, timezone

    from . import schedule

    default_time, default_tz = schedule.schedule_config(settings)
    if args.action == "install":
        time_str, tz = args.time or default_time, args.timezone or default_tz
        schedule.parse_time(time_str)
        path = schedule.install(time_str, tz)
        print(f"Installed: `tradebot daily` runs once a day after {time_str} {tz}.")
        for note in schedule.timing_notes(time_str, tz):
            print("  " + note)
        print(f"Job file: {path}\nLog:      {schedule.LOG_PATH}")
        print("It checks every 15 minutes, so a run missed while the Mac slept or was off happens soon after "
              "it wakes. Remove it with `tradebot schedule uninstall`.")
    elif args.action == "uninstall":
        print("Removed the scheduled job." if schedule.uninstall() else "No scheduled job was installed.")
    elif args.action == "run-now":
        schedule.run_now()
        print(f"Started. Follow it with:  tail -f {schedule.LOG_PATH}")
    else:
        from .daily import load_last_run

        info = schedule.status()
        if not info["installed"]:
            print("Not installed. Run `tradebot schedule install`.")
        elif info.get("old_style"):
            print("Installed with the old timer, which can fire at the wrong hour. "
                  "Run `tradebot schedule install` again to update it.")
        else:
            time_str, tz = info.get("time", default_time), info.get("timezone", default_tz)
            now = datetime.now(timezone.utc)
            due, _ = schedule.is_due(now, time_str, tz, schedule.load_state())
            nxt = schedule.next_slot(now, time_str, tz).astimezone()
            print(f"Installed: once a day after {time_str} {tz} ({'loaded' if info['loaded'] else 'NOT loaded'})")
            print("  " + ("Due now: it will run at the next 15-minute check." if due
                          else f"Next run: {nxt:%a %d %b %H:%M} on this Mac's clock."))
            if "last exit code" in info:
                print(f"  last check exit code: {info['last exit code']}")
        last = load_last_run(settings)
        if last:
            when = datetime.fromisoformat(last.get("finished") or last["started"]).astimezone()
            print(f"Last daily run: {when:%a %d %b %H:%M}, {'ok' if last.get('ok') else 'FAILED'}, "
                  f"{len(last.get('new_signals', []))} new signal(s), {len(last.get('errors', {}))} error(s)")
        print(f"Log: {schedule.LOG_PATH}")
    return 0


def _cmd_research(args, settings) -> int:
    from .research.report import cmd_research

    return cmd_research(args, settings)


def cmd_dashboard(args, settings) -> int:
    import subprocess
    from pathlib import Path

    app = Path(__file__).resolve().parents[2] / "dashboard" / "app.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(args.port),
           "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    print(f"Dashboard: http://localhost:{args.port}   (Ctrl+C to stop)")
    import os

    env = dict(os.environ, PYTHONWARNINGS="ignore::Warning:urllib3")
    try:
        return subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        return 0


# ---- entry point ---------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradebot", description="Swing-trading signal bot (US stocks, crypto, India).")
    p.add_argument("--config", help="path to settings.yaml")
    p.add_argument("--version", action="version", version=f"tradebot {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def strategy_opts(sp):
        sp.add_argument("--strategy", "-s", choices=list(REGISTRY), help="override configured strategy")
        sp.add_argument("--param", "-p", action="append", metavar="KEY=VALUE", help="strategy parameter")

    def universe_opts(sp):
        sp.add_argument("--market", "-m", choices=list(MARKETS) + ["all"], default="all",
                        help="'all' = active_markets in settings (default: us + crypto)")
        sp.add_argument("--symbols", nargs="+", help="explicit symbols instead of the watchlist")

    sub.add_parser("strategies", help="list strategies")

    sp = sub.add_parser("scan", help="latest signals for the watchlist")
    universe_opts(sp)
    strategy_opts(sp)
    sp.add_argument("--no-alerts", action="store_true")

    sp = sub.add_parser("backtest", help="backtest symbols or a whole watchlist as one portfolio")
    sp.add_argument("symbols", nargs="*", help="symbols (default: the --market watchlist)")
    sp.add_argument("--market", "-m", choices=list(MARKETS), help="watchlist to use when no symbols given (default us)")
    strategy_opts(sp)
    sp.add_argument("--compare", action="store_true", help="run every strategy and rank them")
    sp.add_argument("--max-positions", type=int, help="override risk.max_positions")
    sp.add_argument("--no-benchmark", action="store_true")
    sp.add_argument("--years", type=float)
    sp.add_argument("--start", help="YYYY-MM-DD (overrides --years)")
    sp.add_argument("--end", help="YYYY-MM-DD")
    sp.add_argument("--capital", type=float)
    sp.add_argument("--trades", action="store_true", help="print the trade list")
    sp.add_argument("--csv", help="write trades to a CSV file")

    sp = sub.add_parser("dashboard", help="open the web dashboard (needs: pip install -e '.[dashboard]')")
    sp.add_argument("--port", type=int, default=8501)

    sp = sub.add_parser("paper", help="paper trading account")
    sp.add_argument("action", choices=["run", "status", "reset"])
    universe_opts(sp)
    strategy_opts(sp)

    sp = sub.add_parser("daily", help="end-of-day job: update the paper account and alert on new signals")
    sp.add_argument("--no-alerts", action="store_true", help="print the report instead of sending alerts")
    sp.add_argument("--if-due", action="store_true", help=argparse.SUPPRESS)  # used by the scheduled job
    sp.add_argument("--at", help=argparse.SUPPRESS)
    sp.add_argument("--tz", help=argparse.SUPPRESS)

    sp = sub.add_parser("alerts", help="test alert channels or set up Telegram")
    sp.add_argument("action", choices=["test", "telegram-chat-id"])

    from .research.report import add_parser as add_research_parser

    add_research_parser(sub)

    sp = sub.add_parser("schedule", help="run `tradebot daily` automatically every day (macOS)")
    sp.add_argument("action", choices=["install", "status", "run-now", "uninstall"])
    sp.add_argument("--time", help="HH:MM, 24-hour (default: schedule.time in settings, 20:15)")
    sp.add_argument("--timezone", help="timezone of --time (default: schedule.timezone, America/New_York)")
    return p


def main(argv: List[str] = None) -> int:
    args = build_parser().parse_args(argv)
    settings: Dict[str, Any] = load_settings(args.config)
    handler = {"strategies": cmd_strategies, "scan": cmd_scan, "backtest": cmd_backtest,
               "paper": cmd_paper, "dashboard": cmd_dashboard, "daily": cmd_daily,
               "alerts": cmd_alerts, "schedule": cmd_schedule, "research": _cmd_research}[args.command]
    return handler(args, settings)


if __name__ == "__main__":
    sys.exit(main())
