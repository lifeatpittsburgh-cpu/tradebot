from __future__ import annotations

from typing import List

from ..signals import Signal


class ConsoleNotifier:
    def send(self, signals: List[Signal], events: List[str]) -> None:
        if not signals and not events:
            print("No new BUY/SELL signals.")
            return
        for s in signals:
            print(">> " + s.summary())
        for e in events:
            print("   " + e)
