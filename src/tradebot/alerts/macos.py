"""macOS notification banners via `osascript` (built in; nothing to install or configure)."""
from __future__ import annotations

import subprocess


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MacNotifier:
    def send(self, alert) -> None:
        body = alert.summary or (alert.lines[0] if alert.lines else "")
        script = f"display notification {_quote(body[:240])} with title {_quote(alert.title[:80])}"
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=15)
