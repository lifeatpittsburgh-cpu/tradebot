"""Signal model shared by the scanner, paper broker and alerts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

BUY = 1
SELL = -1
HOLD = 0
ACTION_NAMES = {BUY: "BUY", SELL: "SELL", HOLD: "HOLD"}


@dataclass
class Signal:
    symbol: str
    market: str
    date: str               # bar date the signal was generated on (YYYY-MM-DD)
    action: str             # BUY / SELL / HOLD
    price: float            # close of the signal bar; entry expected near next open
    strategy: str
    reason: str = ""
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    quantity: Optional[float] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        parts = [f"{self.action:<4} {self.symbol:<14} @ {self.price:,.2f}"]
        if self.action == "BUY" and self.stop_loss is not None:
            parts.append(f"stop {self.stop_loss:,.2f}")
            parts.append(f"target {self.target:,.2f}")
            if self.quantity:
                parts.append(f"qty {self.quantity:g}")
        parts.append(f"[{self.strategy}] {self.reason}")
        ai = self.extra.get("ai") if self.action in ("BUY", "SELL") else None
        if ai:
            parts.append(f"AI {ai['view']} {ai['confidence']:.2f}: {ai['reason']}")
        return " | ".join(parts)
