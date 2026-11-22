"""Stop-loss, target and position sizing."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    reward_risk: float = 2.0
    max_position_pct: float = 0.20
    max_positions: int = 8

    @classmethod
    def from_settings(cls, settings: Dict[str, Any]) -> "RiskConfig":
        r = settings.get("risk", {})
        return cls(**{k: r[k] for k in cls.__dataclass_fields__ if k in r})


@dataclass
class TradePlan:
    entry: float
    stop_loss: float
    target: float
    quantity: float

    @property
    def risk_per_share(self) -> float:
        return self.entry - self.stop_loss


def round_qty(qty: float, step: float = 1.0) -> float:
    """Round down to a multiple of `step` (1 share for stocks, 0.0001 coin for crypto)."""
    if qty <= 0:
        return 0.0
    n = math.floor(qty / step + 1e-9)
    return float(round(n * step, 8))


def affordable_qty(qty: float, price: float, cash: float, commission: float, step: float = 1.0) -> float:
    """Shrink `qty` so price * qty plus commission fits in `cash`."""
    if price <= 0 or cash <= 0:
        return 0.0
    return min(qty, round_qty(cash / (price * (1 + commission)), step))


def plan_long(entry: float, atr_value: float, equity: float, cfg: RiskConfig,
              step: float = 1.0) -> Optional[TradePlan]:
    """Size a long trade so a stop-out loses about `risk_per_trade` of equity.

    Returns None when the trade can't be sized (no ATR yet, or less than one `step`).
    """
    if not (entry > 0 and atr_value > 0 and equity > 0) or math.isnan(atr_value):
        return None
    stop = entry - cfg.atr_stop_mult * atr_value
    if stop <= 0:
        stop = entry * 0.5
    risk_per_share = entry - stop
    target = entry + cfg.reward_risk * risk_per_share

    qty_by_risk = (equity * cfg.risk_per_trade) / risk_per_share
    qty_by_cap = (equity * cfg.max_position_pct) / entry
    qty = round_qty(min(qty_by_risk, qty_by_cap), step)
    if qty < step:
        return None
    return TradePlan(entry=entry, stop_loss=round(stop, 6), target=round(target, 6), quantity=qty)
