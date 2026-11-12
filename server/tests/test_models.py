import io
import wave

import pytest

from nowplaying.models import AudioWindow, Match, normalise, same_track

from helpers import tone


@pytest.mark.parametrize("text,expected", [
    ("Hey Jude", "hey jude"),
    ("Hey Jude - Remastered 2015", "hey jude"),
    ("Hey Jude (Remastered)", "hey jude"),
    ("Hey Jude [2009 Stereo Mix]", "hey jude"),
    ("Under Pressure (feat. David Bowie)", "under pressure"),
    ("Song 2 - 2012 Remaster", "song 2"),
    ("Déjà Vu", "deja vu"),
    ("Rock & Roll", "rock and roll"),
    ("(Nothing)", "nothing"),
    ("  Mr.   Brightside ", "mr brightside"),
    ("Blue - Green", "blue green"),          # a dash that is part of the title stays
])
def test_normalise(text, expected):
    assert normalise(text) == expected


def test_same_track_ignores_release_variants():
    a = Match(title="Hey Jude", artist="The Beatles")
    b = Match(title="Hey Jude - Remastered 2015", artist="Beatles")
    assert same_track(a, b)


def test_same_track_artist_lists():
    a = Match(title="Under Pressure", artist="Queen & David Bowie")
    b = Match(title="Under Pressure", artist="David Bowie, Queen")
    c = Match(title="Under Pressure", artist="Queen")
    assert same_track(a, b)
    assert same_track(a, c)


def test_different_tracks():
    a = Match(title="Hey Jude", artist="The Beatles")
    assert not same_track(a, Match(title="Let It Be", artist="The Beatles"))
    assert not same_track(a, Match(title="Hey Jude", artist="Wilson Pickett"))
    assert not same_track(a, None)
    assert not same_track(None, None)


def test_same_track_by_ids():
    a = Match(title="A", artist="X", recording_mbid="m1")
    b = Match(title="Completely different", artist="Y", recording_mbid="m1")
    assert same_track(a, b)
    c = Match(title="A", artist="X", isrc="USRC17607839")
    d = Match(title="Other", artist="Z", isrc="USRC17607839")
    assert same_track(c, d)


def test_identity_is_stable_across_variants():
    a = Match(title="Hey Jude - Remastered 2015", artist="The Beatles")
    b = Match(title="hey jude", artist="the beatles")
    assert a.identity() == b.identity()


def test_fill_blanks_from():
    a = Match(title="T", artist="A")
    b = Match(title="T", artist="A", album="Album", isrc="X", release_group_mbids=["rg"],
              duration=200.0)
    assert a.fill_blanks_from(b) is True
    assert (a.album, a.isrc, a.release_group_mbids, a.duration) == ("Album", "X", ["rg"], 200.0)
    assert a.fill_blanks_from(b) is False          # nothing new the second time
    a.album = "Kept"
    a.fill_blanks_from(Match(title="T", artist="A", album="Other"))
    assert a.album == "Kept"                       # never overwrites


def test_audio_window_wav_roundtrip():
    pcm = tone(1.5)
    window = AudioWindow(pcm=pcm, sample_rate=16000)
    assert window.duration == pytest.approx(1.5)
    with wave.open(io.BytesIO(window.to_wav_bytes())) as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.readframes(wav.getnframes()) == pcm
