"""Entry filters: extra conditions a BUY signal must pass. Each returns boolean series by date.

* Market regime: only buy while the market index (SPY for stocks, BTC for crypto) is above its
  long moving average. Optionally sell everything when it drops below (`regime_exit`).
* Relative strength: only buy symbols whose trailing return ranks in the top part of the watchlist.
* Volume: only buy when the signal day's volume is at least a multiple of its recent average.

They plug into the backtester (`entry_allowed`, `force_exit`) and are compared with walk-forward
tests by `tradebot research`. None of them improved out-of-sample results (see docs/PROJECT_PLAN.md),
so live signals don't use them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import pandas as pd

from . import indicators as ind


def regime_on(index_close: pd.Series, sma: int = 200) -> pd.Series:
    """True while the index closes above its `sma`-day average (False during warm-up)."""
    avg = ind.sma(index_close, sma)
    return (index_close > avg) & avg.notna()


def relative_strength_ok(data: Dict[str, pd.DataFrame], lookback: int = 126,
                         top_frac: float = 0.5) -> Dict[str, pd.Series]:
    """Per symbol: True on days its `lookback`-bar return ranks in the top `top_frac` of the
    symbols that have that much history. Symbols without enough history yet are not allowed."""
    closes = pd.DataFrame({s: df["close"] for s, df in data.items()}).sort_index().ffill()
    ret = closes / closes.shift(lookback) - 1
    pct = ret.rank(axis=1, pct=True, ascending=True)  # 1.0 = strongest
    ok = (pct > 1 - top_frac) & ret.notna()
    return {s: ok[s].reindex(df.index).fillna(False) for s, df in data.items()}


def volume_ok(df: pd.DataFrame, window: int = 20, min_ratio: float = 1.0) -> pd.Series:
    """True when volume is at least `min_ratio` × its `window`-day average."""
    avg = df["volume"].rolling(window, min_periods=window).mean()
    return (df["volume"] >= min_ratio * avg) & avg.notna()


@dataclass
class FilterConfig:
    regime: bool = False
    regime_sma: int = 200
    regime_exit: bool = False      # also sell everything when the regime turns off
    rs: bool = False
    rs_lookback: int = 126         # ~6 months of trading days
    rs_top_frac: float = 0.5
    volume: bool = False
    volume_window: int = 20
    volume_ratio: float = 1.0

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "FilterConfig":
        f = settings.get("filters", {}) or {}
        reg, rs, vol = f.get("regime", {}) or {}, f.get("relative_strength", {}) or {}, f.get("volume", {}) or {}
        return cls(regime=bool(reg.get("enabled", False)), regime_sma=int(reg.get("sma", 200)),
                   regime_exit=bool(reg.get("exit", False)),
                   rs=bool(rs.get("enabled", False)), rs_lookback=int(rs.get("lookback", 126)),
                   rs_top_frac=float(rs.get("top_frac", 0.5)),
                   volume=bool(vol.get("enabled", False)), volume_window=int(vol.get("window", 20)),
                   volume_ratio=float(vol.get("min_ratio", 1.0)))

    @property
    def any(self) -> bool:
        return self.regime or self.rs or self.volume

    def label(self) -> str:
        parts = []
        if self.regime:
            parts.append(f"regime{self.regime_sma}" + ("+exit" if self.regime_exit else ""))
        if self.rs:
            parts.append(f"rs{self.rs_lookback}/top{self.rs_top_frac:g}")
        if self.volume:
            parts.append(f"vol{self.volume_ratio:g}x")
        return "+".join(parts) or "none"

    def masks(self, data: Dict[str, pd.DataFrame], index_close: Optional[pd.Series] = None):
        """(entry_allowed, force_exit) for the backtester. `index_close` is needed for the regime filter."""
        allowed: Dict[str, pd.Series] = {s: pd.Series(True, index=df.index) for s, df in data.items()}
        force_exit = None
        if self.regime:
            if index_close is None or index_close.empty:
                raise ValueError("the regime filter needs the market index prices (SPY / BTC-USD)")
            on = regime_on(index_close, self.regime_sma)
            for s, df in data.items():
                allowed[s] &= on.reindex(df.index, method="ffill").fillna(False).astype(bool)
            if self.regime_exit:
                force_exit = ~on
        if self.rs:
            for s, ok in relative_strength_ok(data, self.rs_lookback, self.rs_top_frac).items():
                allowed[s] &= ok.astype(bool)
        if self.volume:
            for s, df in data.items():
                allowed[s] &= volume_ok(df, self.volume_window, self.volume_ratio)
        return (allowed if (self.regime or self.rs or self.volume) else None), force_exit
