"""Broker integration (Phase 5): send TradeBot's daily orders to an Alpaca account.

Paper mode submits orders automatically. Live mode only proposes them; each one must be
approved by typing `yes` in `tradebot broker confirm`. See executor.py for the safety rules.
"""
from .alpaca import AlpacaClient, AlpacaError, from_alpaca, to_alpaca

__all__ = ["AlpacaClient", "AlpacaError", "from_alpaca", "to_alpaca"]
