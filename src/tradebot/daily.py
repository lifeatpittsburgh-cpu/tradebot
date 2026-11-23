"""The end-of-day job: update the paper account, then alert on what's new.

Run by `tradebot daily` (and by the scheduled job from `tradebot schedule install`).
It is safe to run any number of times a day, on weekends and on market holidays:

* the paper account only processes candles it hasn't seen, and
* alerts only go out for BUY/SELL signals on a candle that wasn't alerted before.

So no market calendar is needed: on a US holiday Yahoo has no new candle, and nothing is sent
for US stocks, while crypto (which trades every day) still gets its daily update.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .alerts import Alert
from .config import CURRENCY, active_markets, resolve_path
from .data.base import DataProvider
from .ai_study import Review, review_for, reviewer
from .services import accounts_frame, broker_from, run_paper_update, strategies_label, symbols_for
from .signals import Signal


@dataclass
class DailyReport:
    started: str
    finished: str = ""
    strategy: str = ""
    symbols: int = 0
    new_signals: List[str] = field(default_factory=list)      # full one-line summaries
    new_actions: List[str] = field(default_factory=list)      # "BUY AAPL", for notification banners
    repeated_signals: int = 0                                   # BUY/SELLs already alerted earlier
    events: List[str] = field(default_factory=list)             # paper fills, stops, targets, queued orders
    accounts: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    alert_failures: List[str] = field(default_factory=list)
    broker: List[str] = field(default_factory=list)              # Alpaca orders sent / proposed / problems
    ai: List[str] = field(default_factory=list)                  # AI market study: mood, held-back BUYs, problems

    @property
    def ok(self) -> bool:
        """False when no symbol could be updated at all (e.g. no internet)."""
        return self.symbols > 0 and len(self.errors) < self.symbols

    def alert(self) -> Alert:
        if not self.ok:
            return Alert("TradeBot daily run FAILED",
                         [f"{s}: {e}" for s, e in list(self.errors.items())[:10]],
                         summary=f"Could not update any of {self.symbols} symbols. Check the log.")
        lines: List[str] = []
        if self.new_signals:
            lines += ["New signals (act at the next open):"] + [f"  {s}" for s in self.new_signals]
        if self.events:
            lines += ["Paper account:"] + [f"  {e}" for e in self.events]
        if self.broker:
            lines += ["Alpaca:"] + [f"  {b}" for b in self.broker]
        # Market mood rides along with other news; held-back BUYs and study failures are news themselves.
        if self.ai and (lines or any(a.startswith(("Held back", "AI study unavailable")) for a in self.ai)):
            lines += ["AI market study:"] + [f"  {a}" for a in self.ai]
        if lines:
            lines += ["Accounts:"] + [f"  {a}" for a in self.accounts]
        if self.errors:
            lines.append(f"Skipped {len(self.errors)} symbol(s): " + ", ".join(self.errors))
        if self.broker and any("LIVE orders awaiting" in b for b in self.broker):
            return Alert("TradeBot: live orders need your confirmation", lines,
                         "Run `tradebot broker confirm` to review and send them.")
        summary = ", ".join(self.new_actions) if self.new_actions else (f"{len(self.events)} paper-account update(s)" if self.events else "")
        return Alert("TradeBot daily update", lines, summary)


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def state_paths(settings: Dict[str, Any]) -> Dict[str, Path]:
    a = settings.get("alerts", {})
    return {"alerted": resolve_path(a.get("state_path", "state/alert_state.json")),
            "last_run": resolve_path(a.get("last_run_path", "state/last_run.json"))}


def new_actionable(signals: List[Signal], alerted: Dict[str, str]) -> List[Signal]:
    """BUY/SELL signals on a candle newer than the last one alerted for that symbol."""
    return [s for s in signals if s.action in ("BUY", "SELL") and s.date > alerted.get(s.symbol, "")]


def run_daily(settings: Dict[str, Any], provider: Optional[DataProvider] = None,
              review: Optional[Review] = None) -> DailyReport:
    paths = state_paths(settings)
    report = DailyReport(started=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    broker = broker_from(settings)
    symbols = symbols_for(settings, "all")
    report.strategy, report.symbols = strategies_label(settings), len(symbols)

    # AI market study (off unless ai_study.enabled): may hold back BUYs, never adds trades.
    review = review if review is not None else review_for(settings, symbols)
    check = reviewer(settings, review)
    signals, report.errors = run_paper_update(settings, broker, symbols, None, provider, review=check)
    report.events = list(broker.events)

    alerted = _load_json(paths["alerted"])
    fresh = new_actionable(signals, alerted)
    report.new_signals = [s.summary() for s in fresh]
    report.new_actions = [f"{s.action} {s.symbol}" for s in fresh]
    report.repeated_signals = sum(1 for s in signals if s.action in ("BUY", "SELL")) - len(fresh)
    for s in fresh:
        alerted[s.symbol] = s.date
    _save_json(paths["alerted"], alerted)

    markets = active_markets(settings)
    for _, a in accounts_frame(broker, markets).iterrows():
        report.accounts.append(f"{a['market']}: equity {a['equity']:,.2f} {CURRENCY.get(a['market'], '')} "
                               f"({a['return']:+.2%}), {a['open_positions']} open")
    report.broker = broker_step(settings, check)
    report.ai = review.lines() if (review.study or review.error) else []
    report.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return report


def broker_step(settings: Dict[str, Any], review=None) -> List[str]:
    """Send (paper) or propose (live) Alpaca orders when `broker.enabled` is true."""
    from .broker.executor import BrokerConfig

    try:
        if not BrokerConfig.from_settings(settings).enabled:
            return []
        from .broker.cli import run_broker_step

        return run_broker_step(settings, review=review).lines()
    except Exception as exc:  # the local paper update and alerts must still go out
        return [f"!! broker step failed: {type(exc).__name__}: {exc}"]


def save_last_run(settings: Dict[str, Any], report: DailyReport) -> None:
    data = asdict(report)
    data["ok"] = report.ok
    _save_json(state_paths(settings)["last_run"], data)


def load_last_run(settings: Dict[str, Any]) -> Dict[str, Any]:
    return _load_json(state_paths(settings)["last_run"])
