"""Shared data types: a block of captured audio and a recognised track."""

from __future__ import annotations

import io
import re
import unicodedata
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AudioWindow:
    """A block of signed 16-bit little-endian mono PCM."""

    pcm: bytes
    sample_rate: int
    rms_dbfs: float = 0.0

    @property
    def duration(self) -> float:
        return len(self.pcm) / (2 * self.sample_rate)

    def to_wav_bytes(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(self.pcm)
        return buf.getvalue()

    def write_wav(self, path: str | Path) -> None:
        Path(path).write_bytes(self.to_wav_bytes())


@dataclass
class Match:
    """One identified track, as reported by a recognizer (or pushed in from HA)."""

    title: str
    artist: str
    album: str = ""
    source: str = ""
    confidence: float | None = None
    recording_mbid: str = ""
    release_group_mbids: list[str] = field(default_factory=list)
    release_mbids: list[str] = field(default_factory=list)
    isrc: str = ""
    art_url: str = ""
    duration: float | None = None
    offset: float | None = None
    #: the recognizer is not sure: show it only after a second lookup agrees
    tentative: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def identity(self) -> str:
        """Stable key for "the same song", independent of which service named it."""
        return f"{normalise(self.artist)}|{normalise(self.title)}"

    def fill_blanks_from(self, other: "Match") -> bool:
        """Copy fields this match lacks from a later match of the same track."""
        changed = False
        for name in ("album", "recording_mbid", "isrc", "art_url"):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
                changed = True
        for name in ("release_group_mbids", "release_mbids"):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, list(getattr(other, name)))
                changed = True
        if self.duration is None and other.duration is not None:
            self.duration = other.duration
            changed = True
        return changed


_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_DASH_SUFFIX = re.compile(
    r"\s+-\s+(?:\d{4}\s+)?(?:remaster(?:ed)?|mono|stereo|single|radio|album|live|"
    r"deluxe|bonus|version|edit|mix|original|from)\b.*$",
    re.IGNORECASE,
)
_FEATURING = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.IGNORECASE)
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def normalise(text: str) -> str:
    """Reduce a title or artist to a comparison key.

    "Hey Jude - Remastered 2015", "Hey Jude (Remastered)" and "hey jude" all
    give "hey jude", so the same song reported by two services (or as two
    releases by one service) is not treated as a track change.
    """
    folded = _fold(text)
    stripped = _BRACKETS.sub("", folded)
    stripped = _DASH_SUFFIX.sub("", stripped)
    stripped = _FEATURING.sub("", stripped)
    key = _collapse(stripped)
    # A title that is nothing but a bracket, e.g. "(Nothing)", must not vanish.
    return key or _collapse(folded)


def _collapse(text: str) -> str:
    text = text.replace("&", " and ")
    text = _PUNCTUATION.sub(" ", text)
    return " ".join(text.split())


_ARTIST_NOISE = {"and", "the", "feat", "ft", "featuring", "with", "x", "vs"}


def _artist_tokens(artist: str) -> set[str]:
    return {t for t in _collapse(_fold(artist)).split() if t not in _ARTIST_NOISE}


def same_track(a: Match | None, b: Match | None) -> bool:
    """True when two matches describe the same song."""
    if a is None or b is None:
        return False
    if a.recording_mbid and a.recording_mbid == b.recording_mbid:
        return True
    if a.isrc and a.isrc == b.isrc:
        return True
    if normalise(a.title) != normalise(b.title):
        return False
    ta, tb = _artist_tokens(a.artist), _artist_tokens(b.artist)
    if not ta or not tb:
        return ta == tb
    if ta <= tb or tb <= ta:
        return True
    return len(ta & tb) / len(ta | tb) >= 0.5
