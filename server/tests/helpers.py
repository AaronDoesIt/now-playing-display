"""Test helpers: synthetic audio and a controllable clock."""

from __future__ import annotations

import math
import sys
from array import array


def tone(seconds: float, rate: int = 16000, freq: float = 440.0, amplitude: float = 0.3) -> bytes:
    count = int(round(seconds * rate))
    samples = array(
        "h",
        (int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate)) for i in range(count)),
    )
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def silence(seconds: float, rate: int = 16000) -> bytes:
    return b"\x00\x00" * int(round(seconds * rate))


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
