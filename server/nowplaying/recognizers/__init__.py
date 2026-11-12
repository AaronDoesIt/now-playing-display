"""Recognizers and the chain that tries them in order."""

from __future__ import annotations

import datetime as _dt
import logging
import time
from dataclasses import dataclass
from typing import Callable

from ..models import AudioWindow, Match
from ..ratelimit import Budget
from .base import RateLimited, Recognizer, RecognizerError

log = logging.getLogger(__name__)

__all__ = ["ChainResult", "RateLimited", "Recognizer", "RecognizerChain", "RecognizerError"]


@dataclass
class ChainResult:
    match: Match | None
    #: True when at least one recognizer gave a definite answer (a match or a clean "no match").
    #: False means nobody could be asked, which says nothing about what is playing.
    answered: bool


class RecognizerChain:
    """Asks each recognizer in order until one names the track.

    A recognizer that errors or reports a rate limit is rested with exponential
    back-off and the next one is tried, so one service being down or throttled
    does not stop identification.
    """

    def __init__(
        self,
        recognizers: list[Recognizer],
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], _dt.date] = _dt.date.today,
    ) -> None:
        self.recognizers = recognizers
        self.budgets = {
            r.name: Budget(r.min_interval, r.daily_limit, r.max_per_minute,
                           clock=clock, today=today)
            for r in recognizers
        }

    def wait_time(self) -> float:
        """Seconds until any recognizer may be called (inf if none can be today)."""
        return min((b.wait_time() for b in self.budgets.values()), default=float("inf"))

    def recognize(self, window: AudioWindow) -> ChainResult:
        answered = False
        for recognizer in self.recognizers:
            budget = self.budgets[recognizer.name]
            if not budget.ready():
                continue
            budget.note_call()
            started = time.monotonic()
            try:
                match = recognizer.recognize(window)
            except RateLimited as exc:
                rest = budget.note_failure(exc.retry_after)
                log.warning("%s is rate limiting us (%s); resting it for %.0f s",
                            recognizer.name, exc, rest)
                continue
            except RecognizerError as exc:
                rest = budget.note_failure()
                log.warning("%s failed (%s); resting it for %.0f s", recognizer.name, exc, rest)
                continue
            except Exception:
                rest = budget.note_failure()
                log.exception("%s crashed; resting it for %.0f s", recognizer.name, rest)
                continue
            answered = True
            budget.note_success(hit=match is not None)
            log.debug("%s answered in %.1f s: %s", recognizer.name, time.monotonic() - started,
                      f"{match.artist} - {match.title}" if match else "no match")
            if match is not None:
                if not match.source:
                    match.source = recognizer.name
                return ChainResult(match, True)
        return ChainResult(None, answered)

    def stats(self) -> dict[str, dict[str, int]]:
        return {
            name: {"calls": b.calls, "hits": b.hits, "errors": b.errors,
                   "calls_today": b.calls_today}
            for name, b in self.budgets.items()
        }

    def close(self) -> None:
        for recognizer in self.recognizers:
            try:
                recognizer.close()
            except Exception:
                log.debug("closing %s failed", recognizer.name, exc_info=True)
