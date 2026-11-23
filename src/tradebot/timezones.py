"""Which time zone to show intraday times in (dashboard charts and chart alerts).

The choice is saved in state/preferences.json, so the dashboard and the scheduled job agree:
  "exchange"  each market's own clock: crypto in UTC, US stocks in New York, India in Kolkata
  "local"     this computer's time zone
  any IANA name, e.g. "America/Chicago" or "Asia/Kolkata"
"""
from __future__ import annotations

import json
import os
from typing import Optional

import pandas as pd

from .config import resolve_path
from .data.base import MARKET_TZ

EXCHANGE, LOCAL = "exchange", "local"
CHOICES = [EXCHANGE, LOCAL, "UTC", "America/New_York", "America/Chicago", "America/Denver",
           "America/Los_Angeles", "Europe/London", "Europe/Berlin", "Asia/Dubai", "Asia/Kolkata",
           "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney"]
NAMES = {"UTC": "UTC", "America/New_York": "New York", "America/Chicago": "Chicago", "America/Denver": "Denver",
         "America/Los_Angeles": "Los Angeles", "Asia/Kolkata": "India", "Europe/London": "London"}
PREFS_PATH = "state/preferences.json"


def _path():
    return resolve_path(PREFS_PATH)


def load_pref() -> str:
    try:
        value = json.loads(_path().read_text()).get("timezone", EXCHANGE)
    except (FileNotFoundError, ValueError, AttributeError):
        return EXCHANGE
    return value if value in (EXCHANGE, LOCAL) or _valid(value) else EXCHANGE


def save_pref(value: str) -> None:
    if value not in (EXCHANGE, LOCAL) and not _valid(value):
        raise ValueError(f"unknown time zone {value!r}")
    path = _path()
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        data = {}
    data["timezone"] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _valid(name: str) -> bool:
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(str(name))
        return True
    except Exception:
        return False


def local_zone() -> str:
    """This computer's IANA time zone (macOS and Linux keep it as the /etc/localtime link)."""
    if os.environ.get("TZ") and _valid(os.environ["TZ"]):
        return os.environ["TZ"]
    target = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in target:
        name = target.split("zoneinfo/", 1)[1]
        if _valid(name):
            return name
    return "UTC"


def resolve(pref: str, market: str) -> str:
    """IANA zone to show `market`'s bars in."""
    if pref == LOCAL:
        return local_zone()
    if pref == EXCHANGE or not _valid(pref):
        return MARKET_TZ.get(market, "UTC")
    return pref


def label(zone: str) -> str:
    """Short name for captions: "UTC", "New York", "Tokyo"."""
    return NAMES.get(zone, zone.split("/")[-1].replace("_", " "))


def choice_label(pref: str) -> str:
    if pref == EXCHANGE:
        return "Market's own"
    if pref == LOCAL:
        return f"Local ({label(local_zone())})"
    return label(pref)


def convert_index(index: pd.DatetimeIndex, market: str, zone: str) -> pd.DatetimeIndex:
    """Naive exchange-time timestamps -> naive timestamps in `zone`."""
    src = MARKET_TZ.get(market, "UTC")
    if zone == src or len(index) == 0:
        return index
    return index.tz_localize(src).tz_convert(zone).tz_localize(None)


def convert(obj, market: str, zone: str):
    """Copy of a frame/series with its index moved to `zone`."""
    out = obj.copy()
    out.index = convert_index(obj.index, market, zone)
    return out


def convert_ts(ts: Optional[pd.Timestamp], market: str, zone: str) -> Optional[pd.Timestamp]:
    if ts is None:
        return None
    return convert_index(pd.DatetimeIndex([ts]), market, zone)[0]
