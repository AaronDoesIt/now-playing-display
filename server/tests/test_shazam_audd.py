import asyncio
import sys
import types

import pytest

from nowplaying.config import AudDConfig, ShazamConfig
from nowplaying.models import AudioWindow
from nowplaying.net import HttpError
from nowplaying.recognizers.audd import API_URL, AudDRecognizer, parse_audd
from nowplaying.recognizers.base import RateLimited, RecognizerError
from nowplaying.recognizers.shazam import ShazamRecognizer, parse_shazam

from helpers import tone

WINDOW = AudioWindow(pcm=tone(2.0), sample_rate=16000)

# Trimmed from the response recorded in the shazamio repository's test cassettes.
SHAZAM_HIT = {
    "matches": [{"id": "549679333", "offset": 44.72, "timeskew": 0.0002, "frequencyskew": 0.0}],
    "timestamp": 1700000000000, "timezone": "Europe/London", "tagid": "ABC",
    "track": {
        "layout": "5", "type": "MUSIC", "key": "549679333",
        "title": "Bohemian Rhapsody", "subtitle": "Queen",
        "images": {
            "background": "https://is1-ssl.mzstatic.com/image/thumb/a/800x800cc.jpg",
            "coverart": "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/ab/400x400cc.jpg",
            "coverarthq": "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/ab/400x400cc.jpg",
        },
        "sections": [
            {"type": "SONG", "metadata": [{"title": "Album", "text": "A Night at the Opera"},
                                          {"title": "Label", "text": "EMI"},
                                          {"title": "Released", "text": "1975"}]},
            {"type": "RELATED"},
        ],
        "isrc": "GBUM71029604", "albumadamid": "1440650428",
    },
}
SHAZAM_MISS = {"tagid": "ABC", "retryms": 7000, "matches": []}


def test_parse_shazam_hit():
    match = parse_shazam(SHAZAM_HIT)
    assert (match.title, match.artist, match.album) == ("Bohemian Rhapsody", "Queen",
                                                        "A Night at the Opera")
    assert match.isrc == "GBUM71029604"
    assert match.art_url.endswith("/400x400cc.jpg")
    assert match.offset == pytest.approx(44.72)
    assert match.source == "shazam"


@pytest.mark.parametrize("answer", [SHAZAM_MISS, {"matches": []}, {}, None, [],
                                    {"track": {"subtitle": "No title"}}])
def test_parse_shazam_miss(answer):
    assert parse_shazam(answer) is None


def test_parse_shazam_minimal_track():
    match = parse_shazam({"matches": [], "track": {"title": "Only A Title"}})
    assert (match.title, match.artist, match.album, match.art_url) == ("Only A Title", "", "", "")
    assert match.offset is None


# --- the recognizer, against a stand-in for the shazamio package ---------------

class FakeRateLimited(Exception):
    pass


def fake_shazamio(behaviour):
    """A module with the parts of shazamio the recognizer uses."""
    module = types.ModuleType("shazamio")
    log = {"closed": 0, "kwargs": None, "data": None, "init": None, "retry": None}

    class HTTPClient:
        def __init__(self, retry_options=None):
            log["retry"] = retry_options

        async def close(self):
            log["closed"] += 1

    class Shazam:
        def __init__(self, language="en-US", endpoint_country="GB", http_client=None):
            log["init"] = (language, endpoint_country, http_client)

        async def recognize(self, data, **kwargs):
            log["data"], log["kwargs"] = data, kwargs
            return await behaviour()

    class BadResponseStatus(Exception):
        pass

    module.HTTPClient, module.Shazam = HTTPClient, Shazam
    module.BadResponseStatus = BadResponseStatus
    module.RateLimited = FakeRateLimited
    module.SignatureError = type("SignatureError", (Exception,), {})
    module.FailedDecodeJson = type("FailedDecodeJson", (Exception,), {})
    return module, log


@pytest.fixture
def shazam(monkeypatch):
    def install(behaviour, **cfg):
        module, log = fake_shazamio(behaviour)
        monkeypatch.setitem(sys.modules, "shazamio", module)
        return ShazamRecognizer(ShazamConfig(**cfg)), log
    return install


async def _hit():
    return SHAZAM_HIT


def test_shazam_recognize_sends_a_wav_and_closes_the_client(shazam):
    rec, log = shazam(_hit, language="en-GB", endpoint_country="GB")
    match = rec.recognize(WINDOW)
    assert match.title == "Bohemian Rhapsody"
    assert log["data"][:4] == b"RIFF" and log["data"][8:12] == b"WAVE"
    assert log["init"][:2] == ("en-GB", "GB")
    assert log["kwargs"] == {}
    assert log["closed"] == 1
    assert 429 not in log["retry"].statuses          # a throttle must surface, not be retried
    assert log["retry"].attempts == 2
    assert match.tentative is False                  # skew 0.0002: a clean answer


def skewed(time_skew, frequency_skew):
    answer = dict(SHAZAM_HIT)
    answer["matches"] = [dict(SHAZAM_HIT["matches"][0], timeskew=time_skew,
                              frequencyskew=frequency_skew)]

    async def reply():
        return answer
    return reply


@pytest.mark.parametrize("time_skew,frequency_skew,tentative", [
    (0.0002, 0.0, False),
    (-0.0009, 0.0004, False),
    (0.0031, 0.0, True),             # time stretched: doubtful
    (0.0, -0.012, True),             # pitch shifted: doubtful
])
def test_shazam_answers_that_do_not_line_up_are_marked_tentative(shazam, time_skew,
                                                                 frequency_skew, tentative):
    rec, _ = shazam(skewed(time_skew, frequency_skew))
    match = rec.recognize(WINDOW)
    assert match.tentative is tentative
    assert match.extra["skew"] == pytest.approx(max(abs(time_skew), abs(frequency_skew)))


def test_shazam_confirmation_can_be_switched_off_and_missing_skew_is_doubtful(shazam):
    rec, _ = shazam(skewed(0.05, 0.05), confirm_skew=0.0)
    assert rec.recognize(WINDOW).tentative is False

    async def no_skew_fields():
        return {"matches": [{"id": "1", "offset": 3.0}], "track": {"title": "T", "subtitle": "A"}}
    rec, _ = shazam(no_skew_fields)
    match = rec.recognize(WINDOW)
    assert match.extra["skew"] is None and match.tentative is True


def test_shazam_miss(shazam):
    async def miss():
        return SHAZAM_MISS
    rec, log = shazam(miss)
    assert rec.recognize(WINDOW) is None
    assert log["closed"] == 1


def test_shazam_rate_limit_and_errors_are_mapped(shazam):
    async def throttled():
        raise FakeRateLimited("429")
    rec, log = shazam(throttled)
    with pytest.raises(RateLimited):
        rec.recognize(WINDOW)
    assert log["closed"] == 1                        # closed even when the call fails

    async def offline():
        raise OSError("network unreachable")
    rec, _ = shazam(offline)
    with pytest.raises(RecognizerError, match="network unreachable"):
        rec.recognize(WINDOW)


def test_shazam_timeout(shazam):
    async def hang():
        await asyncio.sleep(5)
    rec, log = shazam(hang, timeout=0.05)
    with pytest.raises(RecognizerError, match="no reply"):
        rec.recognize(WINDOW)
    assert log["closed"] == 1


def test_shazam_can_be_called_repeatedly(shazam):
    rec, log = shazam(_hit)
    for _ in range(3):
        assert rec.recognize(WINDOW) is not None
    assert log["closed"] == 3


# ------------------------------------------------------------------------- AudD

AUDD_RESULT = {
    "artist": "Imagine Dragons", "title": "Warriors", "album": "Warriors",
    "release_date": "2014-09-18", "label": "Universal Music", "timecode": "00:40",
    "song_link": "https://lis.tn/Warriors",
    "apple_music": {
        "artwork": {"width": 1500, "height": 1500,
                    "url": "https://is2-ssl.mzstatic.com/image/thumb/Music/v4/x/{w}x{h}bb.jpg"},
        "isrc": "USUM71414163", "albumName": "Smoke + Mirrors (Deluxe)",
    },
    "musicbrainz": [{
        "id": "rec-1", "score": 100, "title": "Warriors",
        "releases": [{"id": "rel-1", "release-group": {"id": "rg-1", "primary-type": "Single"}},
                     {"id": "rel-2", "release-group": {"id": "rg-2", "primary-type": "Album"}},
                     {"id": "rel-3", "release-group": {"id": "rg-1", "primary-type": "Single"}}],
    }],
}


class FakeHttp:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def request_json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.reply, Exception):
            raise self.reply
        return 200, self.reply


def audd(reply, **cfg):
    http = FakeHttp(reply)
    cfg.setdefault("api_token", "TOKEN")
    return AudDRecognizer(AudDConfig(**cfg), http), http


def test_audd_request_and_parse():
    rec, http = audd({"status": "success", "result": AUDD_RESULT})
    match = rec.recognize(WINDOW)
    method, url, kwargs = http.calls[0]
    assert (method, url) == ("POST", API_URL)
    assert kwargs["data"] == {"api_token": "TOKEN", "return": "apple_music,musicbrainz"}
    name, content, mime = kwargs["files"]["file"]
    assert name.endswith(".wav") and content[:4] == b"RIFF" and mime == "audio/wav"
    assert (match.artist, match.title, match.album) == ("Imagine Dragons", "Warriors", "Warriors")
    assert match.art_url == "https://is2-ssl.mzstatic.com/image/thumb/Music/v4/x/600x600bb.jpg"
    assert match.isrc == "USUM71414163"
    assert match.recording_mbid == "rec-1" and match.release_group_mbids == ["rg-1", "rg-2"]
    assert match.offset == 40.0 and match.source == "audd"


def test_audd_art_fallbacks():
    spotify_only = {"title": "T", "artist": "A", "spotify": {
        "album": {"images": [{"url": "https://i.scdn.co/image/big", "width": 640}]},
        "external_ids": {"isrc": "ISRC1"}}}
    match = parse_audd(spotify_only)
    assert match.art_url == "https://i.scdn.co/image/big" and match.isrc == "ISRC1"
    link_only = parse_audd({"title": "T", "artist": "A", "song_link": "https://lis.tn/x"})
    assert link_only.art_url == "https://lis.tn/x?thumb"
    assert parse_audd({"title": "T", "artist": "A"}).art_url == ""


def test_audd_no_match_and_unusable_audio():
    assert audd({"status": "success", "result": None})[0].recognize(WINDOW) is None
    too_short = {"status": "error", "error": {"error_code": 300, "error_message": "too short"}}
    assert audd(too_short)[0].recognize(WINDOW) is None
    assert parse_audd({"artist": "no title"}) is None


@pytest.mark.parametrize("code,expected", [(900, RecognizerError), (903, RecognizerError),
                                           (901, RateLimited), (902, RateLimited),
                                           (19, RecognizerError)])
def test_audd_errors(code, expected):
    reply = {"status": "error", "error": {"error_code": code, "error_message": "msg"}}
    with pytest.raises(expected) as info:
        audd(reply)[0].recognize(WINDOW)
    if expected is RateLimited:
        assert info.value.retry_after == 3600.0       # out of quota: do not hammer
    else:
        assert not isinstance(info.value, RateLimited)


def test_audd_bad_token_names_the_setting():
    reply = {"status": "error", "error": {"error_code": 900, "error_message": "wrong token"}}
    with pytest.raises(RecognizerError, match="api_token"):
        audd(reply)[0].recognize(WINDOW)


def test_audd_network_errors():
    with pytest.raises(RecognizerError):
        audd(HttpError("timeout"))[0].recognize(WINDOW)
    with pytest.raises(RateLimited):
        audd(HttpError("slow", status=429, retry_after=30))[0].recognize(WINDOW)


def test_parse_shazam_finds_the_apple_song_id():
    """Shazam names the Apple Music song in the track's hub; it gives the track's length."""
    answer = dict(SHAZAM_HIT)
    answer["track"] = dict(SHAZAM_HIT["track"], hub={"type": "APPLEMUSIC", "actions": [
        {"name": "apple", "type": "applemusicplay", "id": "1440650711"},
        {"name": "apple", "type": "uri", "uri": "https://audio-ssl.itunes.apple.com/x.m4a"}]})
    assert parse_shazam(answer).extra["apple_song_id"] == "1440650711"
    assert parse_shazam(SHAZAM_HIT).extra["apple_song_id"] == ""         # no hub at all
    answer["track"]["hub"] = {"actions": [{"type": "uri", "uri": "x"}, "junk", {"id": "abc"}]}
    assert parse_shazam(answer).extra["apple_song_id"] == ""
