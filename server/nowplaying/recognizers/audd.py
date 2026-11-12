"""AudD (https://audd.io): a documented, paid recognition API.

The sanctioned alternative to Shazam for short noisy clips: 300 free requests
on signup, then about $5 per 1,000. A daily cap (daily_limit) keeps a room full
of unrecognisable sound from running up a bill.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import AudDConfig
from ..models import AudioWindow, Match
from ..net import HttpClient, HttpError
from .base import RateLimited, Recognizer, RecognizerError

log = logging.getLogger(__name__)

API_URL = "https://api.audd.io/"
_BAD_TOKEN = {900, 903}             # invalid / disabled token
_QUOTA = {901, 902}                 # free limit reached / token quota exceeded
_UNUSABLE_AUDIO = {300, 500}        # could not fingerprint / invalid audio
_ART_SIZE = 600


class AudDRecognizer(Recognizer):
    name = "audd"

    def __init__(self, cfg: AudDConfig, http: HttpClient) -> None:
        self.cfg = cfg
        self.min_interval = cfg.min_interval
        self.daily_limit = cfg.daily_limit
        self._http = http

    def check(self) -> tuple[bool, str]:
        if not self.cfg.api_token:
            return False, "no API token (sign up at https://audd.io)"
        return True, "ready"

    def recognize(self, window: AudioWindow) -> Match | None:
        try:
            _, body = self._http.request_json(
                "POST", API_URL,
                data={"api_token": self.cfg.api_token, "return": "apple_music,musicbrainz"},
                files={"file": ("clip.wav", window.to_wav_bytes(), "audio/wav")},
                timeout=self.cfg.timeout)
        except HttpError as exc:
            if exc.status == 429:
                raise RateLimited(str(exc), exc.retry_after) from None
            raise RecognizerError(str(exc)) from None
        if not isinstance(body, dict):
            raise RecognizerError("AudD returned an unexpected reply")
        if body.get("status") == "error":
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            code = error.get("error_code")
            message = str(error.get("error_message") or "unknown error")
            if code in _QUOTA:
                raise RateLimited(f"AudD quota used up ({message})", retry_after=3600.0)
            if code in _BAD_TOKEN:
                raise RecognizerError(f"AudD rejected the API token ({message}); "
                                      "check recognizers.audd.api_token")
            if code in _UNUSABLE_AUDIO:
                log.debug("AudD could not use the clip: %s", message)
                return None
            raise RecognizerError(f"AudD error {code}: {message}")
        return parse_audd(body.get("result"))


def _timecode_seconds(value: Any) -> float | None:
    """AudD reports the position in the song as "MM:SS"."""
    try:
        parts = [int(p) for p in str(value).split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return float(seconds)


def parse_audd(result: Any) -> Match | None:
    """Turn AudD's ``result`` into a Match. A null result is "no match"."""
    if not isinstance(result, dict):
        return None
    title = str(result.get("title") or "").strip()
    if not title:
        return None
    apple = result.get("apple_music") if isinstance(result.get("apple_music"), dict) else {}
    spotify = result.get("spotify") if isinstance(result.get("spotify"), dict) else {}

    art_url = ""
    template = (apple.get("artwork") or {}).get("url") if isinstance(apple.get("artwork"), dict) \
        else None
    if template:
        art_url = str(template).replace("{w}", str(_ART_SIZE)).replace("{h}", str(_ART_SIZE))
    if not art_url:
        images = (spotify.get("album") or {}).get("images") if isinstance(spotify.get("album"),
                                                                           dict) else None
        if images and isinstance(images[0], dict):
            art_url = str(images[0].get("url") or "")
    if not art_url and result.get("song_link"):
        art_url = f"{result['song_link']}?thumb"

    isrc = str(apple.get("isrc") or "")
    if not isrc and isinstance(spotify.get("external_ids"), dict):
        isrc = str(spotify["external_ids"].get("isrc") or "")

    recording_mbid = ""
    release_groups: list[str] = []
    recordings = result.get("musicbrainz")
    if isinstance(recordings, list) and recordings and isinstance(recordings[0], dict):
        recording_mbid = str(recordings[0].get("id") or "")
        for release in recordings[0].get("releases") or []:
            group = release.get("release-group") if isinstance(release, dict) else None
            if isinstance(group, dict) and group.get("id") and group["id"] not in release_groups:
                release_groups.append(str(group["id"]))

    return Match(
        title=title,
        artist=str(result.get("artist") or "").strip(),
        album=str(result.get("album") or apple.get("albumName") or "").strip(),
        source="audd",
        recording_mbid=recording_mbid,
        release_group_mbids=release_groups[:3],
        isrc=isrc,
        art_url=art_url,
        offset=_timecode_seconds(result.get("timecode")),
    )
