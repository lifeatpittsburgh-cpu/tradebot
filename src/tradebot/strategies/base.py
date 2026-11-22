"""Strategy interface.

A strategy looks at daily OHLCV bars and marks each bar with BUY (1), SELL (-1) or
HOLD (0), decided using only data up to that bar's close. Trades act on the next bar.
Strategies are long-only: SELL means "exit the long position".
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

import pandas as pd


class Strategy(ABC):
    name: str = "base"
    defaults: Dict[str, Any] = {}

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameters {sorted(unknown)}")
        self.params: Dict[str, Any] = {**self.defaults, **params}

    @property
    def warmup(self) -> int:
        """Bars needed before signals are meaningful."""
        return 50

    @abstractmethod
    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a frame on df's index with columns `signal` (int) and `reason` (str)."""

    def state(self, df: pd.DataFrame) -> pd.Series:
        """1 while this strategy would hold a position (from a BUY until the next SELL), else 0."""
        sig = self.generate(df)["signal"].astype(float)
        return sig.replace(0.0, float("nan")).ffill().fillna(0.0).clip(lower=0.0).astype(int)

    @staticmethod
    def _frame(index: pd.Index) -> pd.DataFrame:
        return pd.DataFrame({"signal": 0, "reason": ""}, index=index)

    def __repr__(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.name}({args})"
