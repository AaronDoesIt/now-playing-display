import pytest

from nowplaying.config import load_config
from nowplaying.metadata import (CAA_ROOT, ITUNES_SEARCH, MB_ROOT, MusicBrainz, art_variants,
                                 build_finders, cover_art_archive_finder, itunes_finder,
                                 recognizer_finder, release_groups_from)
from nowplaying.models import Match
from nowplaying.net import HttpError

from helpers import FakeClock


class FakeHttp:
    """Answers get_json from a list of (url-substring, param-substring, reply) routes."""

    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls = []

    def get_json(self, url, params=None, **kwargs):
        params = dict(params or {})
        self.calls.append((url, params))
        for needle, param_needle, reply in self.routes:
            if needle in url and param_needle in str(params):
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise HttpError(f"GET {url} returned HTTP 404", status=404)


def release(group_id, kind, title="Album", secondary=(), status="Official", date="2000"):
    return {"id": f"rel-{group_id}", "title": title, "status": status, "date": date,
            "release-group": {"id": group_id, "title": title, "primary-type": kind,
                              "secondary-types": list(secondary)}}


# ------------------------------------------------------------------ art variants

def test_apple_artwork_is_requested_larger_with_the_original_as_fallback():
    url = "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/ab/cd/400x400cc.jpg"
    assert art_variants(url) == [
        "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/ab/cd/600x600bb.jpg", url]
    small = "https://is1-ssl.mzstatic.com/image/thumb/Music/x/100x100bb.jpg"
    assert art_variants(small)[0].endswith("/600x600bb.jpg")
    already = "https://is1-ssl.mzstatic.com/image/thumb/Music/x/600x600bb.jpg"
    assert art_variants(already) == [already]


def test_other_hosts_are_left_alone():
    assert art_variants("https://i.scdn.co/image/abc") == ["https://i.scdn.co/image/abc"]
    assert art_variants("http://ha:8123/api/media_player_proxy/x?token=t&cache=400x400cc.jpg") \
        == ["http://ha:8123/api/media_player_proxy/x?token=t&cache=400x400cc.jpg"]
    assert art_variants("") == []


def test_recognizer_finder_labels_with_the_source():
    track = Match(title="T", artist="A", source="shazam", art_url="https://i.scdn.co/image/abc")
    assert list(recognizer_finder(track)) == [("https://i.scdn.co/image/abc", "shazam")]
    assert list(recognizer_finder(Match(title="T", artist="A"))) == []


# ------------------------------------------------------------------- MusicBrainz

def test_release_groups_prefer_official_studio_albums_then_earliest():
    recordings = [{"releases": [
        release("comp", "Album", "Greatest Hits", secondary=["Compilation"], date="1981"),
        release("single", "Single", "The Single", date="1975-10"),
        release("bootleg", "Album", "Bootleg", status="Bootleg", date="1970"),
        release("reissue", "Album", "A Night at the Opera", date="2011"),
        release("album", "Album", "A Night at the Opera", date="1975-11-21"),
        release("album", "Album", "A Night at the Opera", date="1975-11-21"),      # duplicate
    ]}]
    ordered = [gid for gid, _ in release_groups_from(recordings)]
    assert ordered == ["album", "reissue", "single", "comp", "bootleg"]
    assert release_groups_from([{"id": "no-releases"}]) == []


def test_a_known_album_wins_over_everything_else():
    """From a live MusicBrainz search for "Gold Dust Woman": a small page of results
    held only broadcast bootlegs and compilations, and the wrong cover was chosen."""
    recordings = [
        {"releases": [release("bootleg", "Album", "Next Stop: Main Road", status="Bootleg",
                              date="1991")]},
        {"releases": [release("broadcast", "Album", "Transmission Impossible",
                              secondary=["Compilation"], date="")]},
        {"releases": [release("single", "Single", "Don't Stop", date="1977-04-01")]},
        {"releases": [release("rumours", "Album", "Rumours", date="1985"),
                      release("rumours", "Album", "Rumours", date="2013-01-29")]},
        {"releases": [release("dance", "Album", "The Dance", secondary=["Live"], date="1997")]},
    ]
    assert release_groups_from(recordings)[0] == ("rumours", "Rumours")       # studio album
    by_album = release_groups_from(recordings, album="The Dance")
    assert by_album[0] == ("dance", "The Dance")                              # the named one
    assert by_album[1] == ("rumours", "Rumours")
    assert release_groups_from(recordings, album="the dance (Live)")[0][0] == "dance"
    assert release_groups_from(recordings, album="Unknown Album")[0][0] == "rumours"


def musicbrainz(routes):
    http, clock, naps = FakeHttp(routes), FakeClock(), []

    def sleep(seconds):
        naps.append(seconds)
        clock.advance(seconds)

    return MusicBrainz(http, clock=clock, sleep=sleep), http, naps


def test_lookup_by_isrc_uses_the_search_endpoint():
    mb, http, _ = musicbrainz([("/recording", "isrc:GBUM71029604",
                                {"recordings": [{"releases": [release("rg1", "Album", "Opera")]}]})])
    track = Match(title="Bohemian Rhapsody", artist="Queen", isrc="GBUM71029604")
    assert mb.release_groups(track) == [("rg1", "Opera")]
    url, params = http.calls[0]
    assert url == f"{MB_ROOT}/recording"
    assert params["query"] == "isrc:GBUM71029604" and params["fmt"] == "json"


def test_falls_back_from_isrc_to_recording_id_to_search():
    routes = [
        ("/recording/mbid-1", "releases+release-groups", {"releases": []}),
        ("/recording", "recording:", {"recordings": [
            {"score": 100, "title": "Bohemian Rhapsody",
             "releases": [release("rg-search", "Album", "Opera")]},
            {"score": 100, "title": "Bohemian Rhapsody (Live)",              # same song, live
             "releases": [release("rg-live", "Album", "Live", secondary=["Live"])]},
            {"score": 100, "title": "Somebody to Love",                      # another song
             "releases": [release("rg-other", "Album", "Other")]},
            {"score": 60, "title": "Bohemian Rhapsody",                      # low score
             "releases": [release("rg-weak", "Album", "Weak")]},
        ]}),
        ("/recording", "isrc:", {"recordings": []}),
    ]
    mb, http, _ = musicbrainz(routes)
    track = Match(title="Bohemian Rhapsody", artist="Queen", isrc="X", recording_mbid="mbid-1")
    assert mb.release_groups(track) == [("rg-search", "Opera"), ("rg-live", "Live")]
    assert [call[0].rsplit("/", 1)[-1] for call in http.calls] == ["recording", "mbid-1",
                                                                  "recording"]
    assert http.calls[2][1]["query"] == 'recording:"Bohemian Rhapsody" AND artist:"Queen"'


def test_search_query_escapes_lucene_syntax():
    mb, http, _ = musicbrainz([("/recording", "", {"recordings": []})])
    mb.release_groups(Match(title='What\'s "Up"? (Remix)', artist="AC/DC"))
    assert http.calls[0][1]["limit"] == "25"
    assert http.calls[0][1]["query"] == r'recording:"What' "'" r's \"Up\"\? \(Remix\)" AND ' \
                                        r'artist:"AC\/DC"'


def test_results_are_cached_and_requests_spaced_one_second_apart():
    mb, http, naps = musicbrainz([("/recording", "", {"recordings": []})])
    first = Match(title="One", artist="A")
    mb.release_groups(first)
    mb.release_groups(first)                               # cached: no second request
    mb.release_groups(Match(title="Two", artist="A"))      # immediately after: must wait
    assert len(http.calls) == 2
    assert naps == [pytest.approx(1.1)]


# ------------------------------------------------------------ Cover Art Archive

def test_caa_uses_known_release_groups_without_asking_musicbrainz():
    mb, http, _ = musicbrainz([])
    track = Match(title="T", artist="A", release_group_mbids=["g1", "g2", "g3", "g4"],
                  release_mbids=["r1"])
    urls = [url for url, label in cover_art_archive_finder(mb)(track)]
    assert urls == [f"{CAA_ROOT}/release-group/g1/front-500",
                    f"{CAA_ROOT}/release-group/g2/front-500",
                    f"{CAA_ROOT}/release-group/g3/front-500",
                    f"{CAA_ROOT}/release/r1/front-500"]
    assert http.calls == []


def test_caa_asks_musicbrainz_when_the_track_has_no_ids():
    mb, _, _ = musicbrainz([("/recording", "isrc:I1",
                             {"recordings": [{"releases": [release("found", "Album")]}]})])
    found = list(cover_art_archive_finder(mb)(Match(title="T", artist="A", isrc="I1")))
    assert found == [(f"{CAA_ROOT}/release-group/found/front-500", "coverartarchive")]
    assert list(cover_art_archive_finder(None)(Match(title="T", artist="A"))) == []


# ----------------------------------------------------------------------- iTunes

def itunes_item(track, artist, album, tag):
    return {"trackName": track, "artistName": artist, "collectionName": album,
            "artworkUrl100": f"https://is1-ssl.mzstatic.com/image/thumb/Music/{tag}/100x100bb.jpg"}


def test_itunes_keeps_only_the_same_song_and_prefers_the_same_album():
    http = FakeHttp([("itunes.apple.com", "", {"resultCount": 4, "results": [
        itunes_item("Hey Jude (Karaoke Version)", "Party Tyme", "Karaoke", "wrong-artist"),
        itunes_item("Hey Jude", "The Beatles", "1 (2015 Version)", "one"),
        itunes_item("Let It Be", "The Beatles", "Let It Be", "wrong-song"),
        itunes_item("Hey Jude", "The Beatles", "Hey Jude", "album"),
    ]})])
    track = Match(title="Hey Jude", artist="The Beatles", album="Hey Jude")
    urls = [url for url, label in itunes_finder(http)(track)]
    assert urls[0] == "https://is1-ssl.mzstatic.com/image/thumb/Music/album/600x600bb.jpg"
    assert all("wrong" not in url for url in urls)
    url, params = http.calls[0]
    assert url == ITUNES_SEARCH
    assert params["term"] == "The Beatles Hey Jude" and params["entity"] == "song"


def test_itunes_no_results_or_no_title():
    http = FakeHttp([("itunes.apple.com", "", {"resultCount": 0, "results": []})])
    assert list(itunes_finder(http)(Match(title="Hey Jude", artist="The Beatles"))) == []
    assert list(itunes_finder(http)(Match(title="", artist="The Beatles"))) == []
    assert len(http.calls) == 1


def test_build_finders_honours_the_musicbrainz_switch():
    cfg = load_config(None, env={})
    finders = build_finders(cfg, FakeHttp())
    assert set(finders) == {"recognizer", "coverartarchive", "itunes"}
    cfg.metadata.musicbrainz = False
    http = FakeHttp()
    finders = build_finders(cfg, http)
    assert list(finders["coverartarchive"](Match(title="T", artist="A", isrc="I"))) == []
    assert http.calls == []


# ------------------------------------------------------------------ track length

def lengths_for(routes):
    from nowplaying.metadata import TrackLengths

    http = FakeHttp(routes)
    return TrackLengths(http), http


def test_track_length_comes_from_the_apple_song_id():
    from nowplaying.metadata import ITUNES_LOOKUP

    lengths, http = lengths_for([(ITUNES_LOOKUP, "1492263241", {"results": [
        {"wrapperType": "track", "trackName": "Help Me", "trackTimeMillis": 201893}]})])
    track = Match(title="Help Me", artist="Joni Mitchell", extra={"apple_song_id": "1492263241"})
    assert lengths.lookup(track) == pytest.approx(201.893)
    assert lengths.lookup(track) == pytest.approx(201.893)
    assert len(http.calls) == 1                     # remembered


def test_track_length_falls_back_to_a_search_and_prefers_the_same_album():
    lengths, http = lengths_for([(ITUNES_SEARCH, "Joni Mitchell Help Me", {"results": [
        {"trackName": "Help Me", "artistName": "Somebody Else", "trackTimeMillis": 100000},
        {"trackName": "Help Me (Live)", "artistName": "Joni Mitchell",
         "collectionName": "Miles of Aisles", "trackTimeMillis": 255000},
        {"trackName": "Help Me", "artistName": "Joni Mitchell",
         "collectionName": "Court and Spark", "trackTimeMillis": 201893},
    ]})])
    track = Match(title="Help Me", artist="Joni Mitchell", album="Court and Spark")
    assert lengths.lookup(track) == pytest.approx(201.893)
    assert http.calls[0][0] == ITUNES_SEARCH        # no song id, so no lookup by id


def test_track_length_unknown_is_none_and_is_asked_again_later():
    lengths, http = lengths_for([])                 # every request fails
    track = Match(title="Obscure", artist="Nobody", extra={"apple_song_id": "42"})
    assert lengths.lookup(track) is None
    assert lengths.lookup(track) is None
    assert len(http.calls) == 4                     # by id and by search, both times


@pytest.mark.parametrize("millis", [None, "201893", 3000, 4 * 3600 * 1000, True])
def test_silly_track_lengths_are_ignored(millis):
    from nowplaying.metadata import ITUNES_LOOKUP

    lengths, _ = lengths_for([(ITUNES_LOOKUP, "7", {"results": [
        {"wrapperType": "track", "trackTimeMillis": millis}]})])
    assert lengths.lookup(Match(title="T", artist="A", extra={"apple_song_id": "7"})) is None
