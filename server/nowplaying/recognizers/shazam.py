"""Shazam, through the open-source ``shazamio`` library.

This is the recognizer that actually works on a short clip of room audio: it
uses Shazam's landmark fingerprints, which are built for a few noisy seconds
from anywhere in a song.

Be aware of what it is: ``shazamio`` talks to Shazam's app endpoint, which is
not a published API. It is free and widely used, but Shazam can change or
throttle it without notice. Measured on 2026-11-10: one lookup every 20 s was
answered nine times in a row, one every 15 s was refused (HTTP 429, no
Retry-After) at the sixth, and four within a minute were refused once. Hence
the defaults: at most 3 lookups in any minute, at least 10 s apart, and a
refusal rests this recognizer for 30 s, doubling while it keeps refusing.
If it stops working altogether, list ``audd`` in recognizers.order instead.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import ShazamConfig
from ..models import AudioWindow, Match
from .base import RateLimited, Recognizer, RecognizerError

log = logging.getLogger(__name__)


class ShazamRecognizer(Recognizer):
    name = "shazam"

    def __init__(self, cfg: ShazamConfig) -> None:
        self.cfg = cfg
        self.min_interval = cfg.min_interval
        self.max_per_minute = cfg.max_per_minute
        self.daily_limit = cfg.daily_limit

    def check(self) -> tuple[bool, str]:
        try:
            import shazamio  # noqa: F401
            from importlib.metadata import version

            return True, f"ready (shazamio {version('shazamio')})"
        except ImportError:
            return False, "the shazamio package is not installed (pip install shazamio)"

    def recognize(self, window: AudioWindow) -> Match | None:
        try:
            import aiohttp
            import shazamio
        except ImportError:
            raise RecognizerError("the shazamio package is not installed") from None
        try:
            answer = asyncio.run(self._ask(shazamio, window.to_wav_bytes()))
        except shazamio.RateLimited:
            raise RateLimited("HTTP 429 from Shazam") from None
        except asyncio.TimeoutError:
            raise RecognizerError(f"no reply within {self.cfg.timeout:.0f} s") from None
        except shazamio.SignatureError as exc:
            raise RecognizerError(f"could not fingerprint the clip: {exc}") from None
        except (shazamio.BadResponseStatus, shazamio.FailedDecodeJson, aiohttp.ClientError,
                OSError) as exc:
            raise RecognizerError(f"{exc.__class__.__name__}: {exc}") from None
        match = parse_shazam(answer)
        if match is not None and self.cfg.confirm_skew > 0:
            skew = match.extra.get("skew")
            # A right answer lines up with the reference recording almost exactly.
            # shazamio's authors measured, on 234 songs, that a skew above 0.001
            # marks 23 of 28 wrong answers but only 15 of 611 right ones. Such an
            # answer is not thrown away (a turntable running slightly fast gives a
            # right answer with a large skew); it just has to be repeated.
            match.tentative = skew is None or skew > self.cfg.confirm_skew
        return match

    async def _ask(self, shazamio: Any, wav: bytes) -> dict[str, Any]:
        from aiohttp_retry import ExponentialRetry

        # The library's default retries a throttled request for up to ~12 minutes.
        # A daemon must not block that long: retry server errors once, surface 429.
        client = shazamio.HTTPClient(retry_options=ExponentialRetry(
            attempts=2, start_timeout=0.5, max_timeout=3.0, statuses={500, 502, 503, 504}))
        try:
            shazam = shazamio.Shazam(language=self.cfg.language,
                                     endpoint_country=self.cfg.endpoint_country,
                                     http_client=client)
            return await asyncio.wait_for(shazam.recognize(wav), timeout=self.cfg.timeout)
        finally:
            await client.close()


def parse_shazam(answer: Any) -> Match | None:
    """Turn Shazam's reply into a Match. A reply without a ``track`` is "no match"."""
    if not isinstance(answer, dict):
        return None
    track = answer.get("track")
    if not isinstance(track, dict):
        return None
    title = str(track.get("title") or "").strip()
    if not title:
        return None
    album = ""
    for section in track.get("sections") or []:
        if isinstance(section, dict) and section.get("type") == "SONG":
            for item in section.get("metadata") or []:
                if isinstance(item, dict) and item.get("title") == "Album":
                    album = str(item.get("text") or "").strip()
    images = track.get("images") if isinstance(track.get("images"), dict) else {}
    matches = answer.get("matches") or []
    first = matches[0] if matches and isinstance(matches[0], dict) else {}
    offset = first.get("offset")
    skews = [abs(first[key]) for key in ("timeskew", "frequencyskew")
             if isinstance(first.get(key), (int, float))]
    return Match(
        title=title,
        artist=str(track.get("subtitle") or "").strip(),
        album=album,
        source="shazam",
        isrc=str(track.get("isrc") or "").strip(),
        art_url=str(images.get("coverarthq") or images.get("coverart") or "").strip(),
        offset=float(offset) if isinstance(offset, (int, float)) else None,
        extra={"shazam_key": track.get("key"), "apple_album_id": track.get("albumadamid"),
               "apple_song_id": _apple_song_id(track),
               "skew": max(skews) if len(skews) == 2 else None},
    )


def _apple_song_id(track: dict[str, Any]) -> str:
    """The Apple Music id of the song, which Shazam puts in the track's "hub" actions.

    It is the exact key for looking the track's length up in Apple's catalogue.
    """
    hub = track.get("hub") if isinstance(track.get("hub"), dict) else {}
    actions = [a for a in hub.get("actions") or [] if isinstance(a, dict)]
    for wanted in ("applemusicplay", None):
        for action in actions:
            song_id = str(action.get("id") or "")
            if song_id.isdigit() and (wanted is None or action.get("type") == wanted):
                return song_id
    return ""
