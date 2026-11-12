"""Per-service request budget: spacing, a per-minute cap, a daily cap, back-off after errors."""

from __future__ import annotations

import datetime as _dt
import time
from collections import deque
from typing import Callable


class Budget:
    """Decides whether a service may be called right now.

    * ``min_interval``: seconds between calls.
    * ``max_per_minute``: calls in any rolling minute (0 = unlimited).
    * ``daily_limit``: calls per local calendar day (0 = unlimited).
    * after a failure the service is rested for 30 s, doubling up to an hour
      (or for the server's Retry-After), and a success clears the back-off.
    """

    BASE_COOLDOWN = 30.0
    MAX_COOLDOWN = 3600.0
    MINUTE = 62.0           # a minute plus a little slack for clock and network jitter

    def __init__(
        self,
        min_interval: float = 0.0,
        daily_limit: int = 0,
        max_per_minute: int = 0,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], _dt.date] = _dt.date.today,
    ) -> None:
        self.min_interval = max(0.0, min_interval)
        self.daily_limit = max(0, daily_limit)
        self.max_per_minute = max(0, max_per_minute)
        self._clock = clock
        self._today = today
        self._next_allowed = 0.0
        self._cooldown_until = 0.0
        self._failures = 0
        self._recent: deque[float] = deque()
        self._day = today()
        self.calls_today = 0
        self.calls = 0
        self.hits = 0
        self.errors = 0

    def _roll_day(self) -> None:
        day = self._today()
        if day != self._day:
            self._day = day
            self.calls_today = 0

    @property
    def exhausted(self) -> bool:
        """True when today's allowance is used up."""
        self._roll_day()
        return bool(self.daily_limit) and self.calls_today >= self.daily_limit

    def _minute_wait(self, now: float) -> float:
        if not self.max_per_minute:
            return 0.0
        while self._recent and now - self._recent[0] >= self.MINUTE:
            self._recent.popleft()
        if len(self._recent) < self.max_per_minute:
            return 0.0
        return self._recent[0] + self.MINUTE - now

    def wait_time(self) -> float:
        """Seconds until a call is allowed (0 = now, inf = not today)."""
        if self.exhausted:
            return float("inf")
        now = self._clock()
        spacing = max(self._next_allowed, self._cooldown_until) - now
        return max(0.0, spacing, self._minute_wait(now))

    def ready(self) -> bool:
        return self.wait_time() <= 0.0

    def note_call(self) -> None:
        self._roll_day()
        now = self._clock()
        self.calls += 1
        self.calls_today += 1
        self._next_allowed = now + self.min_interval
        if self.max_per_minute:
            self._recent.append(now)

    def note_success(self, hit: bool = False) -> None:
        self._failures = 0
        self._cooldown_until = 0.0
        if hit:
            self.hits += 1

    def note_failure(self, retry_after: float | None = None) -> float:
        """Record an error; returns how long the service will be rested."""
        self.errors += 1
        self._failures += 1
        cooldown = min(self.BASE_COOLDOWN * 2 ** (self._failures - 1), self.MAX_COOLDOWN)
        if retry_after is not None:
            cooldown = max(cooldown, min(float(retry_after), self.MAX_COOLDOWN))
        self._cooldown_until = self._clock() + cooldown
        return cooldown
