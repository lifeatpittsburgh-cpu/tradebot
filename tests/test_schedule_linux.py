"""systemd user units for Linux / Raspberry Pi (no systemctl or loginctl is run)."""
import subprocess

import pytest

import tradebot.schedule as schedule


@pytest.fixture
def linux(tmp_path, monkeypatch):
    calls = []

    def fake_systemctl(*args):
        calls.append(args)
        out = ""
        if args[0] == "show" and (args[1].endswith(".timer") or "dashboard" in args[1]):
            out = "ActiveState=active\nSubState=running\n"
        elif args[0] == "show":
            out = "ExecMainStatus=0\nResult=success\n"
        return subprocess.CompletedProcess(args, 0, out, "")

    linger = {"on": False}

    def fake_run(cmd, **kw):
        if cmd[:2] == ["loginctl", "enable-linger"]:
            linger["on"] = True
        out = f"Linger={'yes' if linger['on'] else 'no'}\n" if cmd[:2] == ["loginctl", "show-user"] else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(schedule.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(schedule, "_systemctl", fake_systemctl)
    monkeypatch.setattr(schedule.subprocess, "run", fake_run)
    monkeypatch.setattr(schedule, "LOG_PATH", tmp_path / "logs" / "daily.log")
    monkeypatch.setattr(schedule, "STATE_PATH", tmp_path / "schedule_state.json")
    return calls, tmp_path / "cfg" / "systemd" / "user"


def test_backend_by_platform(monkeypatch):
    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    assert schedule.backend() == "launchd"
    monkeypatch.setattr(schedule.sys, "platform", "linux")
    assert schedule.backend() == "systemd"
    monkeypatch.setattr(schedule.sys, "platform", "win32")
    with pytest.raises(RuntimeError):
        schedule.backend()


def test_units_run_the_same_five_minute_check():
    units = schedule.build_units("20:15", "America/New_York")
    service, timer = units["tradebot-daily.service"], units["tradebot-daily.timer"]
    assert "daily --if-due --at 20:15 --tz America/New_York" in service
    assert "Type=oneshot" in service and f"WorkingDirectory={schedule.PROJECT_ROOT}" in service
    assert "OnUnitActiveSec=300s" in timer and "OnBootSec=1min" in timer and "WantedBy=timers.target" in timer
    with pytest.raises(ValueError):
        schedule.build_units("8pm", "America/New_York")


def test_arguments_with_spaces_are_quoted():
    assert schedule._quote_arg("/home/pi/my bots/tradebot") == '"/home/pi/my bots/tradebot"'
    assert schedule._quote_arg("/home/pi/tradebot") == "/home/pi/tradebot"


def test_install_status_run_now_uninstall(linux):
    calls, unit_dir = linux
    path = schedule.install("20:15", "America/New_York")
    assert path == unit_dir / "tradebot-daily.timer" and path.exists()
    assert ("enable", "--now", "tradebot-daily.timer") in calls
    assert schedule.enable_linger() is True

    info = schedule.status()
    assert info["installed"] and info["loaded"] and info["linger"]
    assert (info["time"], info["timezone"], info["check_every"]) == ("20:15", "America/New_York", 300)
    assert info["last exit code"] == "0"

    schedule.run_now()
    assert schedule.load_state()["force_next"] is True
    assert ("start", "--no-block", "tradebot-daily.service") in calls

    assert schedule.uninstall() is True
    assert not (unit_dir / "tradebot-daily.service").exists()
    assert schedule.status()["installed"] is False


def test_dashboard_service(linux, monkeypatch):
    calls, unit_dir = linux
    path = schedule.install_dashboard(8600)
    text = path.read_text()
    assert "dashboard --port 8600" in text and "Restart=on-failure" in text and "WantedBy=default.target" in text
    assert ("restart", "tradebot-dashboard.service") in calls
    assert schedule.dashboard_status()["ActiveState"] == "active"
    assert schedule.uninstall_dashboard() is True and not path.exists()

    monkeypatch.setattr(schedule.sys, "platform", "darwin")
    with pytest.raises(RuntimeError):
        schedule.install_dashboard()
