from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._t += seconds

    def advance(self, seconds: float) -> None:
        self._t += seconds
