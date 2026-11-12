"""Chromaprint (fpcalc) + AcoustID lookup.

Read this before relying on it for live audio. AcoustID identifies whole files:

* its index only holds the part of each track between about 0:10 and 0:25,
* a lookup must be aligned with the start of the track to within about 10 s,
* and the ``duration`` sent must be within 7 s of the real track length.

So a clip recorded mid-song, or any clip sent with its own length as the
duration, returns nothing, and Chromaprint is not built for microphone noise
either (https://acoustid.org/faq). ``identify_file`` is the supported use: a
complete, clean file. ``recognize`` exists so AcoustID can sit in the live chain
as the project brief asks; expect it to answer "no match" for room audio.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from ..config import AcoustIDConfig
from ..models import AudioWindow, Match
from ..net import HttpClient, HttpError
from .base import RateLimited, Recognizer, RecognizerError

log = logging.getLogger(__name__)

API_URL = "https://api.acoustid.org/v2/lookup"
MIN_SECONDS = 3.0           # fpcalc needs about 2.7 s to produce its first item

# AcoustID error codes (acoustid/api/errors.py)
_BAD_INPUT = {3, 8}                 # invalid fingerprint / duration
_BAD_KEY = {4, 6, 17}               # invalid API key / user key / unknown application
_RATE_LIMITED = 14
_RELEASE_GROUP_LIMIT = 3


class AcoustIDRecognizer(Recognizer):
    name = "acoustid"

    def __init__(self, cfg: AcoustIDConfig, http: HttpClient,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self.cfg = cfg
        self.min_interval = cfg.min_interval
        self.daily_limit = cfg.daily_limit
        self._http = http
        self._run = run

    def check(self) -> tuple[bool, str]:
        if not self.cfg.api_key:
            return False, "no API key (register an application at https://acoustid.org/new-application)"
        if not shutil.which(self.cfg.fpcalc_path) and not Path(self.cfg.fpcalc_path).is_file():
            return False, (f"fpcalc not found ({self.cfg.fpcalc_path!r}); "
                           "Debian: sudo apt install libchromaprint-tools")
        return True, "ready (whole files only; cannot match short or mid-song clips)"

    # ----------------------------------------------------------- fingerprints

    def _fpcalc(self, path: str, length: int | None) -> tuple[str, float] | None:
        cmd = [self.cfg.fpcalc_path, "-json"]
        if length:
            cmd += ["-length", str(length)]
        cmd.append(path)
        try:
            done = self._run(cmd, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise RecognizerError(f"fpcalc not found ({self.cfg.fpcalc_path!r})") from None
        except subprocess.TimeoutExpired:
            raise RecognizerError("fpcalc timed out") from None
        if done.returncode != 0:
            message = (done.stderr or b"").decode("utf-8", "replace").strip()
            if "Empty fingerprint" in message or "Not enough audio" in message:
                return None                     # too short or silent to fingerprint
            raise RecognizerError(f"fpcalc failed: {message[-200:] or done.returncode}")
        try:
            data = json.loads(done.stdout)
            fingerprint = str(data["fingerprint"])
            duration = float(data.get("duration") or 0.0)
        except (ValueError, KeyError, TypeError):
            raise RecognizerError("fpcalc produced unreadable output") from None
        return (fingerprint, duration) if fingerprint else None

    def fingerprint_window(self, window: AudioWindow) -> str | None:
        if window.duration < MIN_SECONDS:
            return None
        handle, name = tempfile.mkstemp(prefix="nowplaying-", suffix=".wav")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(window.to_wav_bytes())
            result = self._fpcalc(name, math.ceil(window.duration))
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass
        return result[0] if result else None

    # ----------------------------------------------------------------- lookup

    def lookup(self, fingerprint: str, duration: float) -> Match | None:
        form = {
            "client": self.cfg.api_key,
            "format": "json",
            "duration": str(max(1, int(round(duration)))),     # the API wants an integer
            "fingerprint": fingerprint,
            "meta": "recordings releasegroups compress",       # values are space-separated
        }
        try:
            status, body = self._http.request_json(
                "POST", API_URL, data=form, timeout=self.cfg.timeout,
                allow_statuses=(400, 404, 413, 429, 500, 503))
        except HttpError as exc:
            if exc.status == 429:
                raise RateLimited(str(exc), exc.retry_after) from None
            raise RecognizerError(str(exc)) from None
        if not isinstance(body, dict):
            raise RecognizerError("AcoustID returned an unexpected reply")
        if body.get("status") != "ok":
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            code = error.get("code")
            message = str(error.get("message") or f"HTTP {status}")
            if code == _RATE_LIMITED or status == 429:
                raise RateLimited(message)
            if code in _BAD_INPUT:
                log.debug("AcoustID rejected the fingerprint: %s", message)
                return None
            if code in _BAD_KEY:
                raise RecognizerError(f"AcoustID rejected the API key ({message}); "
                                      "check recognizers.acoustid.api_key")
            raise RecognizerError(f"AcoustID error {code}: {message}")
        return pick_match(body.get("results") or [], self.cfg.min_score)

    # ------------------------------------------------------------- entry points

    def recognize(self, window: AudioWindow) -> Match | None:
        fingerprint = self.fingerprint_window(window)
        if fingerprint is None:
            return None
        return self.lookup(fingerprint, window.duration)

    def identify_file(self, path: str | Path) -> Match | None:
        """Identify a complete audio file: the use AcoustID is designed for."""
        result = self._fpcalc(str(path), None)
        if result is None:
            return None
        fingerprint, duration = result
        if duration <= 0:
            raise RecognizerError("fpcalc could not determine the file's duration")
        return self.lookup(fingerprint, duration)


def _release_group_rank(group: dict[str, Any]) -> int:
    """Lower is better: a plain studio album beats a single, which beats a compilation."""
    kind = group.get("type") or ""
    secondary = group.get("secondarytypes") or []
    if kind == "Album" and not secondary:
        return 0
    if kind in ("Single", "EP") and not secondary:
        return 1
    if kind == "Album":
        return 2
    return 3


def _artist_name(artists: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for index, artist in enumerate(artists):
        parts.append(str(artist.get("name") or ""))
        if index < len(artists) - 1:
            parts.append(str(artist.get("joinphrase") or ", "))
    return "".join(parts).strip()


def pick_match(results: list[dict[str, Any]], min_score: float) -> Match | None:
    """Choose the best recording from an AcoustID ``results`` list (sorted by score)."""
    for result in sorted(results, key=lambda r: float(r.get("score") or 0.0), reverse=True):
        score = float(result.get("score") or 0.0)
        if score < min_score:
            break
        usable = [r for r in result.get("recordings") or []
                  if r.get("title") and r.get("artists")]       # some entries are just an id
        if not usable:
            continue

        def rank(recording: dict[str, Any]) -> int:
            groups = recording.get("releasegroups") or []
            return min((_release_group_rank(g) for g in groups), default=4)

        recording = min(usable, key=rank)
        groups = sorted(recording.get("releasegroups") or [], key=_release_group_rank)
        duration = recording.get("duration")
        return Match(
            title=str(recording["title"]),
            artist=_artist_name(recording["artists"]),
            album=str(groups[0].get("title") or "") if groups else "",
            source="acoustid",
            confidence=score,
            recording_mbid=str(recording.get("id") or ""),
            release_group_mbids=[str(g["id"]) for g in groups[:_RELEASE_GROUP_LIMIT]
                                 if g.get("id")],
            duration=float(duration) if duration else None,
        )
    return None
