from typing import Any, Dict, Type

from .base import Strategy
from .composite import Composite
from .macd_trend import MACDTrend
from .rsi_reversion import RSIMeanReversion
from .sma_crossover import SMACrossover

REGISTRY: Dict[str, Type[Strategy]] = {
    cls.name: cls for cls in (SMACrossover, RSIMeanReversion, MACDTrend, Composite)
}


def get_strategy(name: str, **params: Any) -> Strategy:
    try:
        cls = REGISTRY[name]
    except KeyError:
        raise ValueError(f"Unknown strategy '{name}'. Available: {', '.join(REGISTRY)}") from None
    return cls(**params)


__all__ = ["Strategy", "REGISTRY", "get_strategy", "SMACrossover", "RSIMeanReversion", "MACDTrend", "Composite"]
