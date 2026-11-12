import os
import subprocess
import wave

import pytest

from nowplaying.config import AcoustIDConfig
from nowplaying.models import AudioWindow
from nowplaying.net import HttpError
from nowplaying.recognizers.acoustid import API_URL, AcoustIDRecognizer, pick_match
from nowplaying.recognizers.base import RateLimited, RecognizerError

from helpers import tone

# Shape taken from a live api.acoustid.org reply for meta=recordings+releasegroups.
OK_REPLY = {
    "status": "ok",
    "results": [{
        "id": "9ff43b6a-4f16-427c-93c2-92307ca505e0",
        "score": 0.97,
        "recordings": [
            {"id": "sparse-entry-with-no-metadata"},
            {
                "id": "cd2e7c47-16f5-46c6-a37c-a1eb7bf599ff",
                "title": "Lower Your Eyelids to Die With the Sun",
                "duration": 637.333,
                "artists": [{"id": "a1", "name": "M83"}],
                "releasegroups": [
                    {"id": "rg-comp", "title": "Donkey Punch", "type": "Album",
                     "secondarytypes": ["Compilation", "Soundtrack"]},
                    {"id": "rg-album", "title": "Before the Dawn Heals Us", "type": "Album"},
                    {"id": "rg-single", "title": "Teen Angst", "type": "Single"},
                ],
            },
        ],
    }],
}


class FakeHttp:
    def __init__(self, reply=(200, OK_REPLY)):
        self.reply = reply
        self.calls = []

    def request_json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeFpcalc:
    def __init__(self, stdout=b'{"duration": 8.00, "fingerprint": "AQADtEmSJE"}', stderr=b"",
                 returncode=0, error=None):
        self.stdout, self.stderr, self.returncode, self.error = stdout, stderr, returncode, error
        self.commands = []
        self.wav_frames = None

    def __call__(self, cmd, **kwargs):
        self.commands.append(cmd)
        if self.error:
            raise self.error
        path = cmd[-1]
        if path.endswith(".wav") and os.path.exists(path):
            with wave.open(path) as wav:
                self.wav_frames = wav.getnframes()
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def make(http=None, fpcalc=None, **cfg):
    http = http or FakeHttp()
    fpcalc = fpcalc or FakeFpcalc()
    cfg.setdefault("api_key", "KEY")
    return AcoustIDRecognizer(AcoustIDConfig(**cfg), http, run=fpcalc), http, fpcalc


WINDOW = AudioWindow(pcm=tone(8.0), sample_rate=16000)


def test_recognize_fingerprints_a_temp_wav_and_cleans_up():
    rec, http, fpcalc = make()
    match = rec.recognize(WINDOW)
    cmd = fpcalc.commands[0]
    assert cmd[:4] == ["fpcalc", "-json", "-length", "8"]
    assert fpcalc.wav_frames == 8 * 16000            # fpcalc saw the whole window
    assert not os.path.exists(cmd[-1])               # and the temp file is gone
    assert match.title == "Lower Your Eyelids to Die With the Sun"


def test_lookup_request_matches_the_api():
    rec, http, _ = make()
    rec.lookup("AQADtEmSJE", 241.6)
    method, url, kwargs = http.calls[0]
    assert (method, url) == ("POST", API_URL)
    assert kwargs["data"] == {
        "client": "KEY", "format": "json", "duration": "242",
        "fingerprint": "AQADtEmSJE", "meta": "recordings releasegroups compress",
    }


def test_pick_match_prefers_the_studio_album_and_skips_sparse_recordings():
    match = pick_match(OK_REPLY["results"], min_score=0.6)
    assert match.artist == "M83"
    assert match.album == "Before the Dawn Heals Us"
    assert match.release_group_mbids == ["rg-album", "rg-single", "rg-comp"]
    assert match.recording_mbid == "cd2e7c47-16f5-46c6-a37c-a1eb7bf599ff"
    assert match.confidence == 0.97 and match.duration == pytest.approx(637.333)
    assert match.source == "acoustid"


def test_pick_match_thresholds_and_empty_results():
    assert pick_match(OK_REPLY["results"], min_score=0.99) is None
    assert pick_match([], 0.5) is None
    assert pick_match([{"id": "x", "score": 0.9}], 0.5) is None                 # no recordings
    assert pick_match([{"id": "x", "score": 0.9, "recordings": [{"id": "y"}]}], 0.5) is None


def test_pick_match_takes_the_highest_score_and_joins_artists():
    results = [
        {"score": 0.7, "recordings": [{"id": "low", "title": "Low", "artists": [{"name": "A"}]}]},
        {"score": 0.9, "recordings": [{"id": "high", "title": "Under Pressure", "artists": [
            {"name": "Queen", "joinphrase": " & "}, {"name": "David Bowie"}]}]},
    ]
    match = pick_match(results, 0.5)
    assert match.recording_mbid == "high"
    assert match.artist == "Queen & David Bowie"
    assert match.album == "" and match.release_group_mbids == []


def test_short_or_unfingerprintable_audio_is_no_match_without_a_lookup():
    rec, http, fpcalc = make()
    assert rec.recognize(AudioWindow(pcm=tone(1.0), sample_rate=16000)) is None
    assert fpcalc.commands == [] and http.calls == []
    rec, http, _ = make(fpcalc=FakeFpcalc(stdout=b"", stderr=b"ERROR: Empty fingerprint\n",
                                          returncode=2))
    assert rec.recognize(WINDOW) is None
    assert http.calls == []


def test_fpcalc_problems_are_recognizer_errors():
    rec, _, _ = make(fpcalc=FakeFpcalc(error=FileNotFoundError()))
    with pytest.raises(RecognizerError, match="fpcalc not found"):
        rec.recognize(WINDOW)
    rec, _, _ = make(fpcalc=FakeFpcalc(stdout=b"", stderr=b"ERROR: boom", returncode=1))
    with pytest.raises(RecognizerError, match="boom"):
        rec.recognize(WINDOW)
    rec, _, _ = make(fpcalc=FakeFpcalc(stdout=b"not json"))
    with pytest.raises(RecognizerError, match="unreadable"):
        rec.recognize(WINDOW)


@pytest.mark.parametrize("status,code,expected", [
    (400, 4, RecognizerError),          # invalid API key
    (400, 17, RecognizerError),         # unknown application
    (429, 14, RateLimited),
    (503, 13, RecognizerError),
])
def test_api_errors(status, code, expected):
    reply = (status, {"status": "error", "error": {"code": code, "message": "nope"}})
    rec, _, _ = make(http=FakeHttp(reply))
    with pytest.raises(expected) as info:
        rec.recognize(WINDOW)
    if expected is RecognizerError:
        assert not isinstance(info.value, RateLimited)


def test_invalid_key_error_names_the_setting():
    reply = (400, {"error": {"code": 4, "message": "invalid API key"}, "status": "error"})
    rec, _, _ = make(http=FakeHttp(reply))
    with pytest.raises(RecognizerError, match="api_key"):
        rec.recognize(WINDOW)


def test_rejected_fingerprint_is_no_match():
    reply = (400, {"status": "error", "error": {"code": 3, "message": "invalid fingerprint"}})
    rec, _, _ = make(http=FakeHttp(reply))
    assert rec.recognize(WINDOW) is None


def test_network_errors_and_http_429():
    rec, _, _ = make(http=FakeHttp(HttpError("connection refused")))
    with pytest.raises(RecognizerError):
        rec.recognize(WINDOW)
    rec, _, _ = make(http=FakeHttp(HttpError("slow down", status=429, retry_after=90)))
    with pytest.raises(RateLimited) as info:
        rec.recognize(WINDOW)
    assert info.value.retry_after == 90


def test_identify_file_sends_the_whole_file_duration():
    fpcalc = FakeFpcalc(stdout=b'{"duration": 641.23, "fingerprint": "AQABz0qUkZK4oOfhL"}')
    rec, http, fpcalc = make(fpcalc=fpcalc)
    assert rec.identify_file("song.flac") is not None
    assert fpcalc.commands[0] == ["fpcalc", "-json", "song.flac"]     # default 120 s, from 0:00
    assert http.calls[0][2]["data"]["duration"] == "641"


def test_identify_file_needs_a_duration():
    rec, _, _ = make(fpcalc=FakeFpcalc(stdout=b'{"duration": 0.00, "fingerprint": "AQAB"}'))
    with pytest.raises(RecognizerError, match="duration"):
        rec.identify_file("raw.pcm")


def test_check_reports_missing_key():
    rec, _, _ = make(api_key="")
    ok, why = rec.check()
    assert ok is False and "API key" in why
