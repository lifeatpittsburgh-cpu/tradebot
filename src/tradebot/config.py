"""Settings loading and market helpers."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"

DEFAULTS: Dict[str, Any] = {
    "active_markets": ["us", "crypto"],
    "watchlists": {"us": [], "crypto": [], "india": []},
    "strategy": {"name": "sma_crossover", "params": {}, "by_market": {}},
    "data": {"lookback_days": 500, "cache_dir": "data_cache", "cache_ttl_minutes": 60},
    "risk": {
        "risk_per_trade": 0.01,
        "atr_period": 14,
        "atr_stop_mult": 2.0,
        "reward_risk": 2.0,
        "max_position_pct": 0.20,
        "max_positions": 8,
        "max_per_sector": None,
    },
    "sectors": {},
    "backtest": {"years": 5, "benchmark": {"us": "SPY", "crypto": "BTC-USD", "india": "^NSEI"}},
    "costs": {
        "us": {"commission": 0.0, "slippage": 0.0005},
        "crypto": {"commission": 0.001, "slippage": 0.001},
        "india": {"commission": 0.001, "slippage": 0.0005},
    },
    "paper": {
        "ledger_path": "state/paper_ledger.json",
        "starting_capital": {"us": 10000, "crypto": 10000, "india": 500000},
    },
    "alerts": {"console": True, "macos": True, "telegram": False,
               "state_path": "state/alert_state.json", "last_run_path": "state/last_run.json"},
    "schedule": {"time": "20:15", "timezone": "America/New_York"},
}

MARKETS = ("us", "crypto", "india")
CURRENCY = {"us": "USD", "crypto": "USD", "india": "INR"}
# Smallest tradable quantity. Stocks trade whole shares; crypto is fractional.
QTY_STEP = {"us": 1.0, "crypto": 0.0001, "india": 1.0}
# Bars per year, for annualising Sharpe. Crypto trades every day.
PERIODS_PER_YEAR = {"us": 252, "crypto": 365, "india": 248}
CRYPTO_QUOTES = ("-USD", "-USDT", "-USDC")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(path: Optional[str] = None) -> Dict[str, Any]:
    """Load YAML settings merged over defaults. Relative paths resolve to the project root."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    user: Dict[str, Any] = {}
    if cfg_path.exists():
        with open(cfg_path) as fh:
            user = yaml.safe_load(fh) or {}
    return _deep_merge(DEFAULTS, user)


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


def market_of(symbol: str) -> str:
    """Infer the market from a Yahoo-style ticker.

    BTC-USD / ETH-USDT are crypto; .NS / .BO / ^NSEI / ^BSESN are India; everything else US.
    """
    s = symbol.upper()
    if s.endswith(CRYPTO_QUOTES):
        return "crypto"
    if s.endswith(".NS") or s.endswith(".BO") or s in ("^NSEI", "^BSESN", "^NSEBANK"):
        return "india"
    return "us"


def active_markets(settings: Dict[str, Any]) -> List[str]:
    return [m for m in settings.get("active_markets", MARKETS) if m in MARKETS]


def is_tradable(symbol: str) -> bool:
    """Indexes like ^NSEI can be scanned but not bought."""
    return not symbol.startswith("^")


def load_dotenv(path: Optional[Path] = None) -> None:
    """Minimal .env loader (KEY=VALUE lines) so we don't need python-dotenv."""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
