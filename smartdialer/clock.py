"""Injectable clock so the same code runs in production (wall clock) and in the simulator (virtual time)."""
import time
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


class Clock:
    def now(self) -> float:
        return time.time()


class SimClock(Clock):
    def __init__(self, start=None):
        # default: 2026-10-29 10:00 IST (inside calling hours)
        self.t = start if start is not None else datetime(2026, 10, 29, 10, 0, tzinfo=IST).timestamp()

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def ist_hour(ts: float) -> int:
    return datetime.fromtimestamp(ts, IST).hour
