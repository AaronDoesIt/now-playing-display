"""The recognizer interface every identification service implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AudioWindow, Match


class RecognizerError(Exception):
    """The service could not be asked (network, bad reply). It is rested and the chain moves on."""


class RateLimited(RecognizerError):
    """The service said to slow down."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Recognizer(ABC):
    #: short name used in the config, logs and the "source" field of a match
    name: str = ""
    #: seconds between calls, calls per rolling minute and calls per day
    #: (0 = unlimited); enforced by the chain
    min_interval: float = 0.0
    max_per_minute: int = 0
    daily_limit: int = 0

    @abstractmethod
    def recognize(self, window: AudioWindow) -> Match | None:
        """Identify the audio. Return None for "no match"; raise RecognizerError on failure."""

    def check(self) -> tuple[bool, str]:
        """Is this recognizer usable on this machine? Returns (ok, explanation)."""
        return True, "ready"

    def close(self) -> None:
        """Release any resources."""
