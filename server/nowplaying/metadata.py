"""Where cover art comes from: the recognizer, MusicBrainz + Cover Art Archive, iTunes.

Each "finder" turns a track into candidate image URLs, best first. The
ArtworkService fetches them in the configured order until one yields a usable
picture.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable, Iterator

from .artwork import Finder
from .config import Config
from .models import Match, normalise, same_track
from .net import HttpClient

log = logging.getLogger(__name__)

MB_ROOT = "https://musicbrainz.org/ws/2"
CAA_ROOT = "https://coverartarchive.org"
ITUNES_SEARCH = "https://itunes.apple.com/search"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"

MB_MIN_INTERVAL = 1.1           # MusicBrainz allows one request a second per IP
MB_MIN_SCORE = 90               # search results below this are not trusted for art
_APPLE_SIZE = re.compile(r"/\d+x\d+(?:bb|cc)(?:-\d+)?\.(?:jpe?g|png|webp)$", re.IGNORECASE)
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def art_variants(url: str) -> list[str]:
    """Candidate URLs for a recognizer-supplied cover: a larger rendition first.

    Apple artwork (what Shazam and AudD hand out) has the size in the file
    name, so a 400 px link can be asked for at 600 px, enough for a 480 px panel.
    """
    if not url:
        return []
    if "mzstatic.com" in url and _APPLE_SIZE.search(url):
        larger = _APPLE_SIZE.sub("/600x600bb.jpg", url)
        return [larger, url] if larger != url else [url]
    return [url]


def recognizer_finder(track: Match) -> Iterator[tuple[str, str]]:
    for url in art_variants(track.art_url):
        yield url, track.source or "recognizer"


# ------------------------------------------------------------------ MusicBrainz

def _group_rank(release: dict[str, Any], album: str = "") -> tuple[int, int, str]:
    """Sort key for a release: the named album first, then official studio albums,
    earliest first. Live sets, compilations and bootlegs come last."""
    group = release.get("release-group") or {}
    kind = group.get("primary-type") or ""
    secondary = group.get("secondary-types") or []
    official = (release.get("status") or "Official") == "Official"
    if kind == "Album" and not secondary:
        rank = 0
    elif kind in ("Single", "EP") and not secondary:
        rank = 1
    elif kind == "Album":
        rank = 2
    else:
        rank = 3
    if not official:
        rank += 4
    title = str(group.get("title") or release.get("title") or "")
    named = 0 if album and normalise(title) == album else 1
    return named, rank, str(release.get("date") or "9999")


def release_groups_from(recordings: list[dict[str, Any]],
                        album: str = "") -> list[tuple[str, str]]:
    """(release-group id, title) pairs from MusicBrainz recordings, best first.

    A popular song appears on dozens of releases (live albums, compilations,
    bootlegs of radio broadcasts). When the album is known it wins outright;
    otherwise the earliest official studio album does.
    """
    wanted = normalise(album) if album else ""
    releases = [release for recording in recordings
                for release in (recording.get("releases") or [])
                if isinstance(release, dict) and (release.get("release-group") or {}).get("id")]
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for release in sorted(releases, key=lambda r: _group_rank(r, wanted)):
        group = release["release-group"]
        if group["id"] not in seen:
            seen.add(group["id"])
            ordered.append((str(group["id"]), str(group.get("title") or release.get("title") or "")))
    return ordered


class MusicBrainz:
    """Finds the release groups (albums) a track appears on."""

    def __init__(self, http: HttpClient, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._http = http
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_request: float | None = None
        self._cache: dict[str, list[tuple[str, str]]] = {}

    def _get(self, path: str, params: dict[str, str]) -> Any:
        with self._lock:                      # serialises callers and keeps to 1 request/s
            if self._last_request is not None:
                wait = self._last_request + MB_MIN_INTERVAL - self._clock()
                if wait > 0:
                    self._sleep(wait)
            self._last_request = self._clock()
            return self._http.get_json(f"{MB_ROOT}/{path}", params={**params, "fmt": "json"})

    def release_groups(self, track: Match) -> list[tuple[str, str]]:
        """(release-group id, title) pairs for *track*, best first; [] if unknown."""
        key = track.identity()
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        groups: list[tuple[str, str]] = []
        if track.isrc:
            # The /isrc/ lookup omits releases in JSON; the search embeds them.
            data = self._get("recording", {"query": f"isrc:{track.isrc}", "limit": "10"})
            groups = release_groups_from(data.get("recordings") or [], track.album)
        if not groups and track.recording_mbid:
            data = self._get(f"recording/{track.recording_mbid}",
                             {"inc": "releases+release-groups+artist-credits"})
            groups = release_groups_from([data], track.album)
        if not groups and track.title and track.artist:
            query = (f'recording:"{_lucene(track.title)}" AND '
                     f'artist:"{_lucene(track.artist)}"')
            # Many recordings share a title (studio, live, broadcast); a small page
            # can miss the studio one, so take a full page and rank across it.
            data = self._get("recording", {"query": query, "limit": "25"})
            trusted = [r for r in data.get("recordings") or []
                       if int(r.get("score") or 0) >= MB_MIN_SCORE
                       and normalise(str(r.get("title") or "")) == normalise(track.title)]
            groups = release_groups_from(trusted, track.album)
        with self._lock:
            self._cache[key] = groups
            if len(self._cache) > 512:
                self._cache.pop(next(iter(self._cache)))
        return groups


def _lucene(text: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", text)


def cover_art_archive_finder(musicbrainz: MusicBrainz | None) -> Finder:
    """500 px fronts from the Cover Art Archive, by release group then release."""

    def find(track: Match) -> Iterator[tuple[str, str]]:
        group_ids = list(track.release_group_mbids)
        if not group_ids and musicbrainz is not None:
            group_ids = [gid for gid, _ in musicbrainz.release_groups(track)]
        for group_id in group_ids[:3]:
            yield f"{CAA_ROOT}/release-group/{group_id}/front-500", "coverartarchive"
        for release_id in track.release_mbids[:3]:
            yield f"{CAA_ROOT}/release/{release_id}/front-500", "coverartarchive"

    return find


# ----------------------------------------------------------------------- iTunes

def itunes_finder(http: HttpClient) -> Finder:
    """Apple's free search API: no key, about 20 requests a minute."""

    def find(track: Match) -> Iterator[tuple[str, str]]:
        if not track.title:
            return
        data = http.get_json(ITUNES_SEARCH, params={
            "term": f"{track.artist} {track.title}".strip(),
            "media": "music", "entity": "song", "limit": "8", "country": "US"})
        results = [item for item in data.get("results") or []
                   if isinstance(item, dict) and item.get("artworkUrl100")
                   and same_track(track, Match(title=str(item.get("trackName") or ""),
                                               artist=str(item.get("artistName") or "")))]
        wanted_album = normalise(track.album) if track.album else ""
        # prefer the same album when we know it; otherwise keep Apple's ranking
        results.sort(key=lambda item: 0 if wanted_album and normalise(
            str(item.get("collectionName") or "")) == wanted_album else 1)
        for item in results[:2]:
            for url in art_variants(str(item["artworkUrl100"])):
                yield url, "itunes"

    return find


# ----------------------------------------------------------------- track length

class TrackLengths:
    """How long a track runs, from Apple's catalogue.

    The daemon uses it to stay quiet until a track should be over instead of
    re-checking every half minute. Shazam names the Apple Music song id, which
    gives an exact answer; without one a search by artist and title is tried.
    Failures are not remembered, a found length is.
    """

    def __init__(self, http: HttpClient) -> None:
        self._http = http
        self._lock = threading.Lock()
        self._cache: dict[str, float] = {}

    def lookup(self, track: Match) -> float | None:
        """The length of *track* in seconds, or None when it cannot be found. Never raises."""
        key = track.identity()
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        seconds: float | None = None
        for source in (self._by_song_id, self._by_search):
            try:
                seconds = source(track)
            except Exception as exc:        # a network or parsing problem: try the next way
                log.info("track length: %s failed: %s", source.__name__.lstrip("_"), exc)
                continue
            if seconds:
                break
        if seconds:
            with self._lock:
                self._cache[key] = seconds
                if len(self._cache) > 512:
                    self._cache.pop(next(iter(self._cache)))
        return seconds

    def _by_song_id(self, track: Match) -> float | None:
        song_id = str(track.extra.get("apple_song_id") or "").strip()
        if not song_id.isdigit():
            return None
        data = self._http.get_json(ITUNES_LOOKUP, params={"id": song_id, "country": "US"})
        for item in data.get("results") or []:
            if isinstance(item, dict) and item.get("wrapperType") == "track":
                return _seconds(item.get("trackTimeMillis"))
        return None

    def _by_search(self, track: Match) -> float | None:
        if not track.title:
            return None
        data = self._http.get_json(ITUNES_SEARCH, params={
            "term": f"{track.artist} {track.title}".strip(),
            "media": "music", "entity": "song", "limit": "8", "country": "US"})
        results = [item for item in data.get("results") or []
                   if isinstance(item, dict) and _seconds(item.get("trackTimeMillis"))
                   and same_track(track, Match(title=str(item.get("trackName") or ""),
                                               artist=str(item.get("artistName") or "")))]
        wanted_album = normalise(track.album) if track.album else ""
        results.sort(key=lambda item: 0 if wanted_album and normalise(
            str(item.get("collectionName") or "")) == wanted_album else 1)
        return _seconds(results[0].get("trackTimeMillis")) if results else None


def _seconds(millis: Any) -> float | None:
    """Milliseconds to seconds, keeping only lengths a real track can have."""
    if not isinstance(millis, (int, float)) or isinstance(millis, bool):
        return None
    seconds = millis / 1000.0
    return seconds if 20.0 <= seconds <= 3 * 3600.0 else None


def build_finders(cfg: Config, http: HttpClient) -> dict[str, Finder]:
    musicbrainz = MusicBrainz(http) if cfg.metadata.musicbrainz else None
    return {
        "recognizer": recognizer_finder,
        "coverartarchive": cover_art_archive_finder(musicbrainz),
        "itunes": itunes_finder(http),
    }
