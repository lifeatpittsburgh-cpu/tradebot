from __future__ import annotations


class ConsoleNotifier:
    def send(self, alert) -> None:
        print(alert.text())
