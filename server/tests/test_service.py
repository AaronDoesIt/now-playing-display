"""The whole loop, driven step by step with a fake clock and fake audio/recognizers."""

import io
import json
import threading
import time

import pytest
from PIL import Image

from nowplaying.artwork import ArtCache, ArtworkService
from nowplaying.config import load_config
from nowplaying.metadata import recognizer_finder
from nowplaying.models import AudioWindow, Match
from nowplaying.musicdetect import Verdict
from nowplaying.recognizers import ChainResult
from nowplaying.service import NowPlayingService

from helpers import FakeClock


def png():
    out = io.BytesIO()
    Image.new("RGB", (300, 300), (30, 120, 200)).save(out, "PNG")
    return out.getvalue()


PNG = png()
HEY_JUDE = dict(title="Hey Jude", artist="The Beatles", album="1", source="shazam",
                art_url="http://art.example/heyjude.jpg")
LET_IT_BE = dict(title="Let It Be", artist="The Beatles", source="shazam",
                 art_url="http://art.example/letitbe.jpg")


def hit(fields):
    return ChainResult(Match(**fields), True)


MISS = ChainResult(None, True)
UNANSWERED = ChainResult(None, False)


class FakeCapture:
    def __init__(self):
        self.healthy = True
        self.silence = 0.0
        self.last_onset_at = None
        self.level = -20.0
        self.buffered = 30.0
        self.since_data = 0.0
        self.snapshots = []
        self.restarts = 0
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def restart(self):
        self.restarts += 1

    def trailing_silence(self):
        return self.silence

    def level_dbfs(self, seconds=1.0):
        return self.level

    def seconds_since_data(self):
        return self.since_data

    def seconds_buffered(self):
        return self.buffered

    def snapshot(self, seconds, minimum=1.0):
        if self.buffered < minimum:
            return None
        take = min(seconds, self.buffered)
        self.snapshots.append(take)
        return AudioWindow(pcm=b"\0\0" * int(16000 * take), sample_rate=16000,
                           rms_dbfs=self.level)


class FakeChain:
    def __init__(self):
        self.script = []
        self.default = MISS
        self.calls = 0
        self.wait = 0.0
        self.closed = False

    def wait_time(self):
        return self.wait

    def recognize(self, window):
        self.calls += 1
        return self.script.pop(0) if self.script else self.default

    def stats(self):
        return {"shazam": {"calls": self.calls}}

    def close(self):
        self.closed = True


class FakeDetector:
    """Stands in for the music detector: the test says whether it hears music."""

    def __init__(self, cfg, clock):
        self.cfg = cfg.music_detector
        self._clock = clock
        self.available = True
        self.fresh = True
        self.is_music = False
        self.since = clock.now
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def hears(self, music: bool, began_ago: float | None = 2.0):
        """began_ago: how long ago the music started; None = it was already playing."""
        self.is_music = music
        self.since = self._clock.now
        self.began = self._clock.now - began_ago if music and began_ago is not None else None

    def verdict(self):
        music, speech = (0.95, 0.01) if self.is_music else (0.01, 0.95)
        return Verdict(self.is_music, music, speech, self.since, self.fresh,
                       getattr(self, "began", None))


class FakePublisher:
    connected = True

    def __init__(self):
        self.states, self.listening, self.audio = [], [], []
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def publish_state(self, payload):
        self.states.append(payload)

    def publish_listening(self, on):
        self.listening.append(on)

    def publish_audio(self, report):
        self.audio.append(report)


class FakeArtServer:
    started = stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def url_for(self, key):
        return f"http://10.0.0.151:8777/art/{key}.jpg"


class Rig:
    def __init__(self, tmp_path, fetch=None, with_detector=False, lengths=None, slides_dir=None,
                 **options):
        cfg = load_config(None, env={})
        cfg.general.data_dir = str(tmp_path)
        cfg.external.ha_base_url = "http://ha.local:8123"
        # The tests in this file pin down the basic rhythm: a re-check every
        # locked_interval. The quiet period after a match is switched off unless a
        # test asks for it (test_quiet_period.py does).
        cfg.detection.freeze_seconds = 0.0
        cfg.detection.use_track_length = False
        for dotted, value in options.items():
            section, name = dotted.split("__")
            setattr(getattr(cfg, section), name, value)
        self.cfg = cfg
        self.det = cfg.detection
        self.clock = FakeClock()
        self.capture = FakeCapture()
        self.chain = FakeChain()
        self.detector = FakeDetector(cfg, self.clock) if with_detector else None
        self.fetched = []

        def default_fetch(url):
            self.fetched.append(url)
            return PNG

        self.artwork = ArtworkService(cfg.artwork, ArtCache(tmp_path / "art"),
                                      fetch or default_fetch, {"recognizer": recognizer_finder},
                                      slides_dir=slides_dir)
        self.svc = NowPlayingService(cfg, self.capture, self.chain, self.artwork,
                                     clock=self.clock, detector=self.detector, lengths=lengths)
        self.pub = FakePublisher()
        self.server = FakeArtServer()
        self.svc.attach(self.server, self.pub)

    def step(self, advance=0.0):
        self.clock.advance(advance)
        return self.svc._step()

    @property
    def last(self):
        return self.pub.states[-1]

    def play(self, fields):
        """Let the scheduled re-check come due and recognise *fields*."""
        self.chain.script.append(hit(fields))
        self.step(advance=self.det.locked_interval)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.fixture
def gated(tmp_path):
    return Rig(tmp_path, with_detector=True)


# ------------------------------------------------------------------ recognition

def test_first_lookup_publishes_the_track_with_its_cover(rig):
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step()
    assert rig.chain.calls == 1
    state = rig.last
    assert (state["state"], state["title"], state["artist"], state["album"]) == (
        "playing", "Hey Jude", "The Beatles", "1")
    assert state["source"] == "shazam" and state["art_source"] == "shazam"
    assert state["art_url"].startswith("http://10.0.0.151:8777/art/c")
    assert rig.fetched == ["http://art.example/heyjude.jpg"]
    assert len(rig.pub.states) == 1                    # one message: the cover was quick
    key = rig.svc.current_art_key()
    assert Image.open(io.BytesIO(rig.artwork.cache.read(key))).size == (480, 480)


def test_lookups_follow_the_tracker_schedule(rig):
    locked = rig.det.locked_interval
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step()
    assert rig.step(advance=locked * 0.25) <= 1.0 and rig.chain.calls == 1   # too early
    rig.step(advance=locked * 0.70)
    assert rig.chain.calls == 1                                     # still too early
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step(advance=locked * 0.05 + 0.01)                          # due: re-check
    assert rig.chain.calls == 2
    assert len(rig.pub.states) == 1                                 # same track: no republish


def test_track_change_publishes_the_new_track(rig):
    rig.play(HEY_JUDE)
    rig.play(LET_IT_BE)
    assert rig.last["title"] == "Let It Be"
    assert [s["title"] for s in rig.pub.states] == ["Hey Jude", "Let It Be"]
    assert rig.pub.states[0]["art_url"] != rig.pub.states[1]["art_url"]


def test_misses_hold_the_track_then_clear_it(rig):
    rig.play(HEY_JUDE)
    seen_at = rig.clock.now
    rig.step(advance=rig.det.locked_interval)                       # first miss
    assert rig.chain.calls == 2 and rig.last["state"] == "playing"
    while rig.clock.now - seen_at < rig.det.hold_seconds - rig.det.search_interval:
        rig.step(advance=rig.det.search_interval)                   # quick retries, still held
        assert rig.last["state"] == "playing"
    rig.step(advance=rig.det.search_interval)                       # held for hold_seconds
    assert rig.last["state"] == "idle" and rig.last["title"] == ""
    assert rig.last["art_source"] == "idle"
    assert rig.last["art_url"].endswith("/art/idle480.jpg")         # always a valid image URL


def test_unanswered_lookups_are_not_misses(rig):
    rig.chain.default = UNANSWERED
    for _ in range(6):
        rig.step(advance=rig.det.search_interval)
    assert rig.chain.calls >= 5
    assert rig.svc.tracker.misses == 0
    assert rig.svc.tracker.next_delay() == rig.det.search_interval  # no back-off


def test_no_idle_blink_between_songs_when_a_lookup_is_about_to_happen(rig):
    """Seen live: the old track expired half a second before the lookup that named the new one."""
    rig.play(HEY_JUDE)
    rig.chain.wait = 1.0                                            # paced: a lookup in 1 s
    rig.step(advance=rig.det.hold_seconds + 5)                      # the old track is stale now
    assert rig.last["title"] == "Hey Jude"                          # ...but not dropped yet
    rig.chain.wait = 0.0
    rig.chain.script.append(hit(LET_IT_BE))
    rig.step(advance=1.0)
    assert [s["state"] for s in rig.pub.states] == ["playing", "playing"]     # no idle in between
    assert rig.last["title"] == "Let It Be"


def test_stale_track_is_dropped_when_no_lookup_is_coming_soon(rig):
    rig.play(HEY_JUDE)
    rig.chain.wait = 40.0                                           # resting after a refusal
    rig.step(advance=rig.det.hold_seconds + 5)
    assert rig.last["state"] == "idle"


def test_stale_track_is_dropped_if_the_awaited_lookup_finds_nothing(rig):
    rig.play(HEY_JUDE)
    rig.chain.wait = 1.0
    rig.step(advance=rig.det.hold_seconds + 5)
    rig.chain.wait = 0.0
    rig.step(advance=1.0)                                           # the lookup: no match
    assert rig.last["state"] == "idle"


def test_every_lookup_is_logged(rig, caplog):
    doubtful = dict(HEY_JUDE, tentative=True)
    rig.chain.script += [MISS, hit(doubtful), hit(doubtful), UNANSWERED]
    with caplog.at_level("INFO", logger="nowplaying.service"):
        for _ in range(4):
            rig.step(advance=rig.det.locked_interval)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("lookup")]
    assert lines[0] == "lookup, 8 s clip at -20 dB: no match"
    assert lines[1].startswith("lookup, 11 s clip at -20 dB: The Beatles - Hey Jude (doubtful")
    assert lines[2].startswith("lookup, 8 s clip at -20 dB: The Beatles - Hey Jude (doubtful")
    assert lines[3].endswith("no recognizer could answer")
    assert [s["title"] for s in rig.pub.states] == ["Hey Jude"]     # shown once, after agreement


def test_no_lookup_while_every_recognizer_is_resting(rig):
    rig.chain.wait = 45.0
    assert rig.step() == 5.0
    assert rig.chain.calls == 0
    rig.chain.wait = float("inf")
    assert rig.step(advance=5) == 5.0                               # still polls commands


# ---------------------------------------------------------------------- silence

def test_silence_between_tracks_keeps_the_track_long_silence_clears_it(rig):
    rig.play(HEY_JUDE)
    rig.capture.silence = 3.0
    rig.step(advance=3)
    assert rig.last["state"] == "playing"
    calls = rig.chain.calls
    rig.capture.silence = 25.0
    rig.step(advance=22)
    assert rig.last["state"] == "idle"
    assert rig.chain.calls == calls                                 # silence costs no lookups


def test_quiet_room_makes_no_lookups(rig):
    rig.capture.level = -62.0                                       # below the -50 gate
    for _ in range(5):
        rig.step(advance=10)
    assert rig.chain.calls == 0


def test_onset_after_a_gap_triggers_an_early_lookup_on_new_audio_only(rig):
    enough = rig.det.min_window_seconds
    rig.play(HEY_JUDE)                                              # next re-check is far off
    rig.capture.last_onset_at = rig.clock.now + 2                   # gap, then sound again
    rig.step(advance=3)                                             # 1 s into the new sound
    assert rig.chain.calls == 1
    rig.step(advance=enough - 1.5)                                  # still half a second short
    assert rig.chain.calls == 1
    rig.chain.script.append(hit(LET_IT_BE))
    rig.step(advance=1.0)                                           # enough of the new track
    assert rig.chain.calls == 2
    assert rig.capture.snapshots[-1] == pytest.approx(enough + 0.5)  # not reaching across the gap
    assert rig.last["title"] == "Let It Be"


# ------------------------------------------------- the music detector as a gate

def test_dialog_never_reaches_the_recognizer(gated):
    """Loud, continuous sound that is not music: ten minutes, zero lookups."""
    for _ in range(600):
        gated.step(advance=1.0)
    assert gated.chain.calls == 0
    assert gated.pub.states == []                                   # nothing to announce
    assert all(report["is_music"] is False for report in gated.pub.audio)
    assert gated.pub.audio[0]["speech"] == 0.95 and gated.pub.audio[0]["level_dbfs"] == -20.0


def test_music_after_talk_is_looked_up_as_soon_as_there_is_enough_of_it(gated):
    enough = gated.det.min_window_seconds
    for _ in range(120):
        gated.step(advance=1.0)                                     # two minutes of talk
    gated.detector.hears(True, began_ago=2.0)                       # a song started 2 s ago
    gated.chain.script.append(hit(HEY_JUDE))
    gated.step()
    assert gated.chain.calls == 0                                   # too little music yet
    gated.step(advance=enough - 2.5)
    assert gated.chain.calls == 0
    gated.step(advance=1.0)                                         # enough + 0.5 s of music
    assert gated.chain.calls == 1
    assert gated.last["title"] == "Hey Jude"
    # the clip starts where the music did, not in the talk before it
    assert gated.capture.snapshots[-1] == pytest.approx(enough + 0.5)


def test_music_already_playing_when_listening_starts_uses_a_full_clip(gated):
    gated.detector.hears(True, began_ago=None)                      # no start was heard
    gated.chain.script.append(hit(HEY_JUDE))
    gated.step()
    assert gated.chain.calls == 1                                   # no reason to wait
    assert gated.capture.snapshots[-1] == pytest.approx(gated.det.window_seconds)


def test_song_gives_way_to_dialog(gated):
    gated.detector.hears(True, began_ago=None)
    gated.chain.script.append(hit(HEY_JUDE))
    gated.step()
    calls = gated.chain.calls
    gated.detector.hears(False)                                     # the song ends, people talk
    gated.step(advance=gated.det.idle_after / 2)
    assert gated.last["state"] == "playing"                         # not cleared instantly
    # one lookup, of the talk alone, to make sure the song really is over
    assert gated.chain.calls == calls + 1
    assert gated.capture.snapshots[-1] == pytest.approx(gated.det.window_seconds)
    gated.step(advance=gated.det.idle_after / 2 + 1)
    assert gated.last["state"] == "idle"                            # cleared when it always was
    for _ in range(300):
        gated.step(advance=1.0)
    assert gated.chain.calls == calls + 1                           # the talk after that costs nothing


def test_song_the_detector_takes_for_talk_stays_up(gated):
    """A rap verse scores as speech, a quiet passage as nothing much. The song is not
    dropped on the detector's word: a lookup of that sound finds it, and it stays for
    as long as lookups keep finding it. (2026-11-11: the idle pictures came up in the
    middle of a song.)"""
    gated.detector.hears(True, began_ago=None)
    gated.chain.script.append(hit(HEY_JUDE))
    gated.step()
    gated.detector.hears(False)                                     # "not music", says the detector
    gated.chain.default = hit(HEY_JUDE)                             # the recognizer still hears the song
    calls = gated.chain.calls
    for _ in range(int(gated.det.window_seconds) - 1):
        gated.step(advance=1.0)
    assert gated.chain.calls == calls                               # not before there is a full clip of it
    gated.step(advance=1.0)
    assert gated.chain.calls == calls + 1
    assert gated.capture.snapshots[-1] == pytest.approx(gated.det.window_seconds)
    for _ in range(300):                                            # five more minutes of the same
        gated.step(advance=1.0)
    assert [s["state"] for s in gated.pub.states] == ["playing"]    # never idle, nothing announced twice
    # and it is re-checked at the usual pace, as if the detector had said "music"
    assert gated.chain.calls == calls + 1 + int(300 // gated.det.locked_interval)


def test_song_the_detector_takes_for_talk_is_dropped_once_lookups_stop_finding_it(gated):
    gated.detector.hears(True, began_ago=None)
    gated.chain.script.append(hit(HEY_JUDE))
    gated.step()
    gated.detector.hears(False)
    gated.chain.script.append(hit(HEY_JUDE))                        # the check finds it: it stays
    for _ in range(40):
        gated.step(advance=1.0)
    assert gated.last["state"] == "playing"
    # The song ends. The detector has nothing new to say, so the lookups decide,
    # with the same patience as for any track that stops being recognised.
    for _ in range(int(gated.det.hold_seconds) + 40):
        gated.step(advance=1.0)
    assert gated.last["state"] == "idle"
    calls = gated.chain.calls
    for _ in range(300):
        gated.step(advance=1.0)
    assert gated.chain.calls == calls                               # and the talk after it costs nothing


def test_the_check_listens_only_to_what_came_after_the_music(gated):
    """A re-check that falls due just as the detector hears the music stop is not made on
    a clip that is mostly the song: its tail would vouch for a song that is over."""
    gated.detector.hears(True, began_ago=None)
    gated.chain.script.append(hit(HEY_JUDE))
    gated.step()
    calls = gated.chain.calls
    gated.step(advance=gated.det.locked_interval - 2)               # the re-check is due in 2 s
    gated.detector.hears(False)                                     # and the song ends here
    gated.step(advance=2.0)
    assert gated.chain.calls == calls                               # held back
    gated.step(advance=gated.det.window_seconds - 2.5)
    assert gated.chain.calls == calls
    gated.step(advance=0.5)                                         # a full clip since the music stopped
    assert gated.chain.calls == calls + 1
    assert gated.capture.snapshots[-1] == pytest.approx(gated.det.window_seconds)


def test_track_waits_for_the_check_when_lookups_are_held_up(gated):
    gated.detector.hears(True, began_ago=None)
    gated.chain.script.append(hit(HEY_JUDE))
    gated.step()
    gated.detector.hears(False)
    gated.chain.wait = 45.0                                         # the recognizer is resting
    calls = gated.chain.calls
    for _ in range(int(gated.det.idle_after) + 10):
        gated.step(advance=1.0)
    assert gated.chain.calls == calls
    assert gated.last["state"] == "playing"                         # not dropped unheard
    gated.chain.wait = 0.0
    gated.step(advance=1.0)                                         # the check: nothing found
    assert gated.chain.calls == calls + 1
    gated.step(advance=1.0)
    assert gated.last["state"] == "idle"                            # the music has been gone long enough


def test_track_is_dropped_in_the_end_if_the_check_can_never_be_made(gated):
    gated.detector.hears(True, began_ago=None)
    gated.chain.script.append(hit(HEY_JUDE))
    gated.step()
    gated.detector.hears(False)
    gated.chain.wait = 3600.0
    calls = gated.chain.calls
    for _ in range(int(gated.det.hold_seconds) - 2):
        gated.step(advance=1.0)
    assert gated.last["state"] == "playing"
    for _ in range(4):
        gated.step(advance=1.0)
    assert gated.last["state"] == "idle" and gated.chain.calls == calls


def test_answer_to_the_identify_button_is_not_wiped_by_the_detector(gated):
    """Tapping the screen while the detector hears only talk: the answer came from that
    very sound, so the detector's opinion does not clear it a moment later."""
    for _ in range(120):
        gated.step(advance=1.0)                                     # two minutes of "talk"
    gated.svc.handle_command("identify", b"PRESS")
    gated.chain.script.append(hit(HEY_JUDE))
    gated.step()
    assert gated.last["title"] == "Hey Jude"
    gated.chain.default = hit(HEY_JUDE)
    for _ in range(90):
        gated.step(advance=1.0)
    assert [s["state"] for s in gated.pub.states] == ["playing"]


def test_music_resuming_after_dialog_is_a_fresh_search(gated):
    gated.detector.hears(True)
    for _ in range(6):                                              # unrecognised music: back-off
        gated.step(advance=gated.svc.tracker.next_delay())
    assert gated.svc.tracker.next_delay() > gated.det.search_interval
    gated.detector.hears(False)
    gated.step(advance=30)
    gated.detector.hears(True, began_ago=2.0)                       # a new song starts
    gated.chain.script.append(hit(LET_IT_BE))
    calls = gated.chain.calls
    gated.step(advance=gated.det.min_window_seconds - 2.0)
    assert gated.chain.calls == calls + 1                           # not held back by the back-off
    assert gated.last["title"] == "Let It Be"


def test_identify_button_overrides_the_gate_once(gated):
    gated.step(advance=5)
    gated.svc.handle_command("identify", b"PRESS")
    gated.chain.script.append(hit(HEY_JUDE))
    gated.step()
    assert gated.chain.calls == 1 and gated.last["title"] == "Hey Jude"
    for _ in range(10):
        gated.step(advance=10)
    # one check of the answer (nothing found here), and the gate is shut again
    assert gated.chain.calls == 2 and gated.last["state"] == "idle"


def test_no_lookup_slips_through_before_the_detector_has_an_opinion(gated):
    """At start-up the audio buffer can fill before the first verdict exists."""
    gated.detector.fresh = False                                    # nothing judged yet
    for _ in range(5):
        gated.step(advance=0.5)
    assert gated.chain.calls == 0
    gated.detector.fresh = True                                     # first verdict: it is talk
    for _ in range(60):
        gated.step(advance=1.0)
    assert gated.chain.calls == 0


def test_detector_that_goes_quiet_is_bypassed_after_a_grace_period(gated, caplog):
    from nowplaying.service import DETECTOR_GRACE

    gated.detector.fresh = False
    gated.step()
    gated.step(advance=DETECTOR_GRACE - 1)
    assert gated.chain.calls == 0                                   # still waiting for it
    gated.chain.script.append(hit(HEY_JUDE))
    with caplog.at_level("WARNING"):
        gated.step(advance=2)                                       # given up: no gate
        gated.step(advance=1)
    assert gated.chain.calls == 1 and gated.last["title"] == "Hey Jude"
    assert gated.svc.state_snapshot()["is_music"] is None
    assert sum("music detector" in r.message for r in caplog.records) == 1   # said once

    gated.detector.fresh = True                                     # it comes back: gate again
    gated.detector.hears(False)
    calls = gated.chain.calls
    for _ in range(120):
        gated.step(advance=1.0)
    # one check of the track that was showing, then nothing more
    assert gated.chain.calls == calls + 1 and gated.last["state"] == "idle"


def test_detector_that_never_loaded_means_no_gate(gated):
    gated.detector.available = False
    gated.chain.script.append(hit(LET_IT_BE))
    gated.step()
    assert gated.chain.calls == 1 and gated.last["title"] == "Let It Be"


def test_lost_audio_is_not_mistaken_for_a_broken_detector(gated, caplog):
    from nowplaying.service import DETECTOR_GRACE

    gated.capture.healthy = False
    gated.detector.fresh = False
    with caplog.at_level("WARNING"):
        for _ in range(int(DETECTOR_GRACE) + 20):
            gated.step(advance=1.0)
    assert not any("music detector" in r.message for r in caplog.records)
    gated.capture.healthy = True                                    # audio returns
    gated.step(advance=1.0)
    assert gated.chain.calls == 0                                   # waits for a verdict again


def test_detector_verdict_is_reported_as_soon_as_it_flips(gated):
    gated.step()
    assert [r["is_music"] for r in gated.pub.audio] == [False]
    gated.step(advance=1)
    assert len(gated.pub.audio) == 1                                # no change: wait for the timer
    gated.detector.hears(True)
    gated.step(advance=1)
    assert [r["is_music"] for r in gated.pub.audio] == [False, True]
    assert gated.pub.audio[-1]["music"] == 0.95
    gated.step(advance=gated.cfg.mqtt.level_interval)
    assert len(gated.pub.audio) == 3                                # and on the timer as well


# ------------------------------------------------------------------- cover art

def test_slow_cover_shows_a_placeholder_first_then_the_real_one(tmp_path):
    release = threading.Event()

    def slow_fetch(url):
        release.wait(5)
        return PNG

    rig = Rig(tmp_path, fetch=slow_fetch, artwork__wait_seconds=0.05)
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step()
    assert rig.last["art_source"] == "placeholder" and rig.last["title"] == "Hey Jude"
    release.set()
    deadline = time.time() + 5
    while len(rig.pub.states) < 2 and time.time() < deadline:
        time.sleep(0.02)
        rig.step()
    assert rig.last["art_source"] == "shazam" and rig.last["title"] == "Hey Jude"
    assert rig.pub.states[0]["art_url"] != rig.pub.states[1]["art_url"]
    assert len(rig.pub.states) == 2


def test_late_cover_for_a_track_that_is_gone_is_dropped(tmp_path):
    release = threading.Event()

    def fetch(url):
        if "heyjude" in url:
            release.wait(5)
        return PNG

    rig = Rig(tmp_path, fetch=fetch, artwork__wait_seconds=0.05)
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step()
    rig.play(LET_IT_BE)                                             # track changed meanwhile
    count = len(rig.pub.states)
    release.set()
    time.sleep(0.3)
    rig.step()
    assert len(rig.pub.states) == count
    assert rig.last["title"] == "Let It Be" and rig.last["art_source"] == "shazam"


def test_art_failure_still_publishes_with_a_generated_cover(tmp_path):
    def broken(url):
        raise RuntimeError("HTTP 404")

    rig = Rig(tmp_path, fetch=broken)
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step()
    assert rig.last["title"] == "Hey Jude" and rig.last["art_source"] == "placeholder"
    assert rig.artwork.cache.read(rig.svc.current_art_key()) is not None


# -------------------------------------------------------------------- commands

def test_listening_switch_off_and_on(rig, tmp_path):
    rig.play(HEY_JUDE)
    rig.svc.handle_command("listening", b"OFF")
    rig.step()
    assert rig.last["state"] == "off" and rig.last["title"] == ""
    assert rig.pub.listening[-1] is False
    calls = rig.chain.calls
    for _ in range(3):
        rig.step(advance=60)
    assert rig.chain.calls == calls                                 # deaf while off
    assert json.loads((tmp_path / "state.json").read_text())["listening"] is False

    restarted = Rig(tmp_path)                                       # survives a restart
    assert restarted.svc.listening is False

    rig.svc.handle_command("listening", b"on")
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step()
    assert rig.pub.listening[-1] is True
    assert rig.last["state"] == "playing"                           # looked up at once


def test_bad_listening_payload_is_ignored(rig):
    rig.svc.handle_command("listening", b"maybe")
    rig.step()
    assert rig.svc.listening is True and rig.pub.listening == []


def test_identify_now_skips_the_wait(rig):
    rig.play(HEY_JUDE)
    calls = rig.chain.calls
    rig.step(advance=2)
    assert rig.chain.calls == calls
    rig.svc.handle_command("identify", b"PRESS")
    rig.chain.script.append(hit(LET_IT_BE))
    rig.step()
    assert rig.chain.calls == calls + 1 and rig.last["title"] == "Let It Be"


def external(**fields):
    return json.dumps(fields).encode("utf-8")


def test_external_track_from_home_assistant_takes_over(rig):
    rig.play(HEY_JUDE)
    calls = rig.chain.calls
    rig.svc.handle_command("external", external(
        state="playing", title="Black Noise", artist="Sleep Sounds", album="Ten Hours",
        source="spotify", art_url="/api/media_player_proxy/media_player.spotify?token=abc&cache=1"))
    rig.step()
    state = rig.last
    assert (state["state"], state["title"], state["source"]) == ("playing", "Black Noise",
                                                                 "spotify")
    assert state["art_source"] == "spotify"
    # the relative entity_picture was fetched through external.ha_base_url
    assert rig.fetched[-1] == ("http://ha.local:8123/api/media_player_proxy/"
                               "media_player.spotify?token=abc&cache=1")
    for _ in range(3):
        rig.step(advance=60)
    assert rig.chain.calls == calls                                 # no acoustic lookups meanwhile

    count = len(rig.pub.states)
    rig.svc.handle_command("external", external(                    # the same again: no republish
        state="playing", title="Black Noise", artist="Sleep Sounds", album="Ten Hours",
        source="spotify", art_url="/api/media_player_proxy/media_player.spotify?token=abc&cache=1"))
    rig.step()
    assert len(rig.pub.states) == count

    rig.svc.handle_command("external", external(state="paused", title="Black Noise"))
    rig.chain.script.append(hit(LET_IT_BE))
    rig.step()                                                      # cleared: listening resumes
    assert rig.chain.calls == calls + 1
    assert rig.last["title"] == "Let It Be"


def test_external_works_while_listening_is_off_and_clears_to_off(rig):
    rig.svc.handle_command("listening", b"OFF")
    rig.svc.handle_command("external", external(title="From HA", artist="Someone"))
    rig.step()
    assert rig.last["state"] == "playing" and rig.last["title"] == "From HA"
    rig.svc.handle_command("external", b"")
    rig.step()
    assert rig.last["state"] == "off"


@pytest.mark.parametrize("payload", [b"not json", b"[1, 2]", external(state="playing"),
                                     external(state="idle", title="x")])
def test_external_payloads_that_do_not_start_a_track(rig, payload):
    rig.svc.handle_command("external", payload)
    rig.step()
    assert rig.svc.external is None and rig.pub.states == []


def test_external_art_url_must_be_http(rig):
    rig.svc.handle_command("external", external(title="T", artist="A",
                                                art_url="file:///etc/passwd"))
    rig.step()
    assert rig.svc.external.art_url == ""
    assert rig.fetched == [] and rig.last["art_source"] == "placeholder"


# ------------------------------------------------------------------ robustness

def test_lost_audio_stops_lookups_clears_the_track_and_restarts_capture(rig):
    rig.play(HEY_JUDE)
    calls = rig.chain.calls
    rig.capture.healthy = False
    rig.capture.since_data = 5.0
    rig.step(advance=5)
    assert rig.last["state"] == "playing" and rig.capture.restarts == 0
    rig.capture.since_data = 25.0
    rig.step(advance=20)
    assert rig.last["state"] == "idle"
    assert rig.capture.restarts == 1
    rig.step(advance=1)
    assert rig.capture.restarts == 1                                # not restarted every second
    rig.capture.since_data = 45.0
    rig.step(advance=20)
    assert rig.capture.restarts == 2
    assert rig.chain.calls == calls


def test_udp_style_input_is_never_restarted_when_the_sender_goes_quiet(tmp_path):
    rig = Rig(tmp_path, capture__stall_seconds=0.0)
    rig.play(HEY_JUDE)
    rig.capture.healthy = False                                     # the panel was unplugged
    for seconds in (5.0, 60.0, 600.0):
        rig.capture.since_data = seconds
        rig.step(advance=seconds)
    assert rig.capture.restarts == 0
    assert rig.last["state"] == "idle"                              # the track is still cleared


def test_waits_for_enough_audio_after_start(rig):
    enough = rig.det.min_window_seconds
    rig.capture.buffered = enough - 1.0
    rig.step()
    assert rig.chain.calls == 0
    rig.capture.buffered = enough + 1.0
    rig.step(advance=2)
    assert rig.chain.calls == 1
    assert rig.capture.snapshots[-1] == pytest.approx(enough + 1.0)     # all there is so far


def test_audio_report_is_published_on_its_interval(rig):
    rig.capture.level = -33.3
    rig.step()
    rig.step(advance=5)
    rig.step(advance=11)
    assert rig.pub.audio == [{"level_dbfs": -33.3, "music": None, "speech": None,
                              "is_music": None}] * 2          # no detector in this rig


def test_run_and_shutdown(gated):
    gated.detector.hears(True, began_ago=None)
    gated.chain.script.append(hit(HEY_JUDE))
    worker = threading.Thread(target=gated.svc.run)
    worker.start()
    deadline = time.time() + 5
    while time.time() < deadline and not any(s["state"] == "playing" for s in gated.pub.states):
        time.sleep(0.02)
    gated.svc.stop()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert gated.capture.started and gated.capture.stopped
    assert gated.detector.started and gated.detector.stopped
    assert gated.pub.started and gated.pub.stopped and gated.server.stopped and gated.chain.closed
    assert gated.pub.states[0]["state"] == "idle"                   # announced before any lookup
    assert any(s["title"] == "Hey Jude" for s in gated.pub.states)
    assert gated.pub.states[-1]["state"] == "off"                   # no stale "playing" left behind


def test_snapshot_and_health_for_the_http_server(gated):
    gated.detector.hears(True, began_ago=None)
    gated.chain.script.append(hit(HEY_JUDE))
    gated.step()
    snap = gated.svc.state_snapshot()
    assert snap["title"] == "Hey Jude" and snap["listening"] is True
    assert snap["capture_ok"] is True and snap["level_dbfs"] == -20.0
    assert snap["is_music"] is True and snap["music"] == 0.95
    assert snap["mqtt_connected"] is True and "shazam" in snap["recognizers"]
    assert gated.svc.health()["ok"] is True
    gated.capture.healthy = False
    assert gated.svc.health()["ok"] is False
    gated.svc.handle_command("listening", b"OFF")
    gated.step()
    assert gated.svc.health()["ok"] is True                         # not listening: not a fault
