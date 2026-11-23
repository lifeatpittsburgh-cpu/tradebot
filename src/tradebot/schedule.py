"""Run `tradebot daily` automatically once a day with macOS launchd (a per-user LaunchAgent).

    tradebot schedule install [--time 20:15]   create/replace the job
    tradebot schedule status                   is it installed, next run, last result
    tradebot schedule run-now                  run the job immediately (to test it)
    tradebot schedule uninstall                remove it

How it works: launchd starts `tradebot daily --if-due` every 5 minutes. That command does
nothing unless a daily slot (schedule.time in schedule.timezone, default 20:15 New York time)
has passed without a successful run, so the real work happens once per evening.

Why not launchd's own calendar timer (StartCalendarInterval)? It computes times in the timezone
macOS had when you logged in, so after a timezone change it fires at the wrong hour until you
log out. It also can't express "20:15 New York time" on a Mac set to another timezone. The
5-minute check avoids both, catches up after sleep or shutdown, and follows daylight saving. The same check also sends chart alerts (`tradebot watch`).
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import PROJECT_ROOT

LABEL = "com.tradebot.daily"
LOG_PATH = PROJECT_ROOT / "state" / "logs" / "daily.log"
STATE_PATH = PROJECT_ROOT / "state" / "schedule_state.json"
CHECK_EVERY_SECONDS = 5 * 60
DEFAULT_TIME, DEFAULT_TZ = "20:15", "America/New_York"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def parse_time(value: str) -> Tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"time must be HH:MM (24-hour), got {value!r}")
    return int(m.group(1)), int(m.group(2))


def schedule_config(settings: Dict[str, Any]) -> Tuple[str, str]:
    """Default run time and timezone from settings.yaml (`tradebot schedule install` stores them in the job)."""
    cfg = settings.get("schedule", {}) or {}
    return str(cfg.get("time", DEFAULT_TIME)), str(cfg.get("timezone", DEFAULT_TZ))


# ---- when is a run due? ----------------------------------------------------------------------
def latest_slot(now: datetime, time_str: str, tz: str) -> datetime:
    """The most recent scheduled moment at or before `now` (today's if passed, else yesterday's)."""
    from zoneinfo import ZoneInfo

    hour, minute = parse_time(time_str)
    local = now.astimezone(ZoneInfo(tz))
    slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > local:
        slot = (local - timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot


def next_slot(now: datetime, time_str: str, tz: str) -> datetime:
    return latest_slot(now, time_str, tz) + timedelta(days=1)


def is_due(now: datetime, time_str: str, tz: str, state: Dict[str, Any]) -> Tuple[bool, datetime]:
    """Due when the latest slot has no successful run yet. Returns (due, slot)."""
    slot = latest_slot(now, time_str, tz)
    done = state.get("last_success_slot")
    return (done is None or datetime.fromisoformat(done) < slot), slot


def load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def timing_notes(time_str: str, tz: str, now: Optional[datetime] = None,
                 state: Optional[Dict[str, Any]] = None) -> List[str]:
    """What the run time means for each market's daily candle, and for this Mac's clock."""
    from zoneinfo import ZoneInfo

    now = now or datetime.now(ZoneInfo("UTC"))
    run = next_slot(now, time_str, tz)
    ny, utc, mac = run.astimezone(ZoneInfo("America/New_York")), run.astimezone(ZoneInfo("UTC")), run.astimezone()
    if is_due(now, time_str, tz, load_state() if state is None else state)[0]:
        notes = ["Today's run hasn't happened yet, so it runs now; after that, daily."]
    else:
        notes = [f"Next run: {mac:%a %d %b %H:%M} on this Mac's clock ({run:%H:%M} {tz})."]
    if (ny.hour, ny.minute) >= (16, 15):
        notes.append(f"US: {ny:%H:%M} New York time, after the close, so it uses that day's candle.")
    else:
        notes.append(f"US: {ny:%H:%M} New York time, before the 16:15 cutoff, so it uses the previous "
                     "day's candle. Pick a later time for same-day US signals.")
    notes.append(f"Crypto: {utc:%H:%M} UTC, {utc.hour + utc.minute / 60:.1f} h after the daily candle "
                 "closed at 00:00 UTC.")
    return notes


# ---- launchd -----------------------------------------------------------------------------------
def build_plist(time_str: str, tz: str) -> Dict[str, Any]:
    parse_time(time_str)
    exe = Path(sys.executable).parent / "tradebot"  # console script installed in the same venv
    base = [str(exe)] if exe.exists() else [sys.executable, "-m", "tradebot.cli"]
    return {
        "Label": LABEL,
        "ProgramArguments": base + ["daily", "--if-due", "--at", time_str, "--tz", tz],
        "WorkingDirectory": str(PROJECT_ROOT),
        "StartInterval": CHECK_EVERY_SECONDS,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "EnvironmentVariables": {"PYTHONWARNINGS": "ignore", "PYTHONUNBUFFERED": "1",
                                 "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "ProcessType": "Background",
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(time_str: str, tz: str) -> Path:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", f"{_domain()}/{LABEL}")  # replace an existing job; fails harmlessly if none
    with open(path, "wb") as fh:
        plistlib.dump(build_plist(time_str, tz), fh)
    res = _launchctl("bootstrap", _domain(), str(path))
    if res.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {res.stderr.strip() or res.stdout.strip()}")
    return path


def uninstall() -> bool:
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    path = plist_path()
    if path.exists():
        path.unlink()
        return True
    return False


def run_now() -> None:
    """Run the full daily job in the background now, even if today's run already happened."""
    state = load_state()
    state["force_next"] = True
    save_state(state)
    res = _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")
    if res.returncode != 0:
        raise RuntimeError(f"launchctl kickstart failed (is it installed?): {res.stderr.strip()}")


def status() -> Dict[str, Any]:
    path = plist_path()
    info: Dict[str, Any] = {"installed": path.exists(), "plist": str(path), "log": str(LOG_PATH)}
    if path.exists():
        with open(path, "rb") as fh:
            job = plistlib.load(fh)
        args = job.get("ProgramArguments", [])
        info["old_style"] = "StartCalendarInterval" in job  # pre-fix job: reinstall it
        info["check_every"] = job.get("StartInterval")
        for flag, key in (("--at", "time"), ("--tz", "timezone")):
            if flag in args and args.index(flag) + 1 < len(args):
                info[key] = args[args.index(flag) + 1]
    res = _launchctl("print", f"{_domain()}/{LABEL}")
    info["loaded"] = res.returncode == 0
    if res.returncode == 0:
        for key in ("state", "runs", "last exit code"):
            m = re.search(rf"^\s*{key} = (.+)$", res.stdout, re.M)
            if m:
                info[key] = m.group(1).strip()
    return info
