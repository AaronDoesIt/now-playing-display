"""The quiet period after a match: far fewer lookups, without a stale display.

With the track's length and the clip's position known, the next lookup is timed
for just after the track should end. Without the length there is a plain
freeze, then the old rhythm.
"""

import threading
import time

import pytest

from nowplaying.config import DetectionConfig
from nowplaying.models import Match
from nowplaying.tracker import Tracker

from test_service import HEY_JUDE, LET_IT_BE, MISS, Rig, hit

A = dict(title="Hey Jude", artist="The Beatles", source="shazam")
B = dict(title="Let It Be", artist="The Beatles", source="shazam")


def tracker(**overrides):
    # The quiet period is an option that is off by default (music from a TV is
    # mostly snippets); these tests switch it on.
    return Tracker(DetectionConfig(**{"freeze_seconds": 120.0, "use_track_length": True,
                                      **overrides}))


def test_by_default_a_showing_track_is_rechecked_every_locked_interval():
    t = Tracker(DetectionConfig())
    t.on_match(Match(**A), now=0, position=10)
    t.set_duration(200)
    assert t.expected_end() is None
    assert t.next_delay() == t.cfg.locked_interval == 30
    t.on_match(Match(**A), now=30, position=40)
    assert t.next_delay() == 30


# -------------------------------------------------------------------- the tracker

def test_length_unknown_freezes_then_returns_to_the_usual_rechecks():
    t = tracker()
    assert t.on_match(Match(**A), now=6) is True
    assert t.next_delay() == 120                    # freeze_seconds from when it appeared
    assert t.on_match(Match(**A), now=126) is False
    assert t.next_delay() == t.cfg.locked_interval  # the freeze is over


def test_a_lookup_inside_the_freeze_does_not_restart_it():
    t = tracker()
    t.on_match(Match(**A), now=0)
    t.on_match(Match(**A), now=50)                  # e.g. after a pause in the sound
    assert t.next_delay() == 70
    t.on_match(Match(**A), now=100)
    assert t.next_delay() == t.cfg.locked_interval  # never sooner than a normal re-check


def test_known_length_waits_until_just_after_the_end():
    t = tracker()
    t.on_match(Match(**A), now=100, position=60)
    assert t.expected_end() is None                 # the length is not known yet
    assert t.set_duration(200) is True
    assert t.expected_end() == 240
    # 140 s remain; the lookup comes min_window_seconds + 1 into the next track
    assert t.next_delay(100) == 140 + t.cfg.min_window_seconds + 1


def test_a_very_long_track_is_still_checked_now_and_then():
    t = tracker()
    t.on_match(Match(**A), now=0, position=10)
    t.set_duration(1500)
    assert t.next_delay(0) == t.cfg.max_freeze_seconds


def test_a_match_near_the_end_looks_again_soon_but_not_at_once():
    t = tracker()
    t.on_match(Match(**A), now=0, position=196)
    t.set_duration(200)
    assert t.next_delay(0) == t.cfg.search_interval


def test_the_same_track_past_its_listed_length_means_the_length_is_wrong():
    t = tracker()
    t.on_match(Match(**A), now=0, position=230)     # a longer version than the catalogue's
    t.set_duration(200)
    assert t.next_delay(0) == t.cfg.locked_interval


def test_a_recheck_of_the_same_track_moves_the_expected_end():
    t = tracker()
    t.on_match(Match(**A), now=0, position=100)
    t.set_duration(200)
    assert t.expected_end() == 100
    assert t.on_match(Match(**A), now=107, position=150) is False   # it was not over after all
    assert t.expected_end() == 157
    assert t.next_delay(107) == 50 + t.cfg.min_window_seconds + 1


def test_the_first_miss_after_a_freeze_does_not_drop_the_track():
    """Nobody asked during the freeze, so the hold timer starts when it ends."""
    t = tracker()
    t.on_match(Match(**A), now=0)
    assert t.next_delay() == 120
    assert t.on_miss(now=120) is False
    assert t.next_delay() == t.cfg.search_interval
    assert t.on_miss(now=135) is False
    assert t.on_miss(now=179) is False
    assert t.on_miss(now=180) is True               # hold_seconds after the freeze ended


def test_after_the_expected_end_two_failed_lookups_clear_the_track():
    t = tracker()
    t.on_match(Match(**A), now=0, position=190)
    t.set_duration(200)                             # over at 10
    assert t.next_delay(0) == 17
    assert t.on_miss(now=17) is False               # one failed lookup is not enough
    assert t.on_miss(now=32) is True                # the second one is


def test_unavailable_recognizers_after_a_freeze_get_the_same_grace():
    t = tracker()
    t.on_match(Match(**A), now=0)
    t.next_delay()
    assert t.on_unavailable(now=125) is False
    assert t.on_unavailable(now=181) is True


def test_a_gap_in_the_sound_ends_the_trust():
    t = tracker()
    t.on_match(Match(**A), now=0)
    assert t.next_delay() == 120
    t.on_onset()                                    # sound resumed after a pause
    assert t.on_miss(now=26) is False
    assert t.on_miss(now=59) is False
    assert t.on_miss(now=60) is True                # counted from the last match again


def test_a_new_track_starts_a_new_quiet_period():
    t = tracker()
    t.on_match(Match(**A), now=0, position=100)
    t.set_duration(200)
    assert t.on_match(Match(**B), now=50) is True
    assert t.expected_end() is None                 # nothing is known about the new one yet
    assert t.next_delay() == 120


@pytest.mark.parametrize("seconds", [None, 0, 5, 4 * 3600])
def test_unusable_lengths_are_ignored(seconds):
    t = tracker()
    assert t.set_duration(200) is False             # nothing is playing
    t.on_match(Match(**A), now=0, position=10)
    assert t.set_duration(seconds) is False
    assert t.expected_end() is None


def test_both_switches_off_give_the_old_rhythm():
    t = tracker(freeze_seconds=0, use_track_length=False)
    t.on_match(Match(**A), now=0, position=10)
    t.set_duration(200)
    assert t.expected_end() is None
    assert t.next_delay() == t.cfg.locked_interval
    assert t.on_miss(now=60) is True                # and the old hold timer


# -------------------------------------------------------------------- the service

class FakeLengths:
    def __init__(self, seconds, gate=None):
        self.seconds = seconds
        self.gate = gate
        self.asked = []

    def lookup(self, track):
        self.asked.append(track.title)
        if self.gate is not None:
            self.gate.wait(5)
        return self.seconds


def settle(rig, want_states):
    """Let a background result arrive and be handled."""
    deadline = time.time() + 5
    while len(rig.pub.states) < want_states and time.time() < deadline:
        time.sleep(0.02)
        rig.step()


def test_freeze_stops_the_half_minute_rechecks(tmp_path):
    rig = Rig(tmp_path, detection__freeze_seconds=120.0)
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step()
    assert rig.chain.calls == 1
    for _ in range(4):                              # the old rhythm would have looked 3 times
        rig.step(advance=29.9)
    assert rig.chain.calls == 1
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step(advance=0.5)                           # two minutes are up
    assert rig.chain.calls == 2
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step(advance=rig.det.locked_interval)       # after the freeze: the usual re-check
    assert rig.chain.calls == 3
    assert len(rig.pub.states) == 1                 # the same track throughout


def test_with_the_length_known_the_next_lookup_waits_for_the_end(tmp_path, caplog):
    lengths = FakeLengths(200.0)
    rig = Rig(tmp_path, lengths=lengths, detection__freeze_seconds=120.0,
              detection__use_track_length=True)
    rig.chain.script.append(hit(dict(HEY_JUDE, offset=60.0)))
    with caplog.at_level("INFO", logger="nowplaying.service"):
        rig.step()
        settle(rig, 2)                              # the length arrives and is published
    assert lengths.asked == ["Hey Jude"]
    assert rig.last["title"] == "Hey Jude" and rig.last["duration"] == 200.0
    plans = [r.getMessage() for r in caplog.records if r.getMessage().startswith("next lookup")]
    assert plans[0].startswith("next lookup in 120 s: holding this track")
    assert plans[-1] == "next lookup in 139 s: this track should end in 132 s"
    # the clip was 8 s and began 60 s in, so the music is 68 s into a 200 s track
    rig.step(advance=138.0)
    assert rig.chain.calls == 1                     # quiet until the track should be over
    rig.chain.script.append(hit(LET_IT_BE))
    rig.step(advance=1.1)
    assert rig.chain.calls == 2
    assert rig.last["title"] == "Let It Be"
    assert rig.capture.snapshots[-1] == pytest.approx(7.1, abs=0.2)   # only the new track's audio


def test_a_track_that_is_nearly_over_is_checked_sooner_than_the_freeze(tmp_path):
    rig = Rig(tmp_path, lengths=FakeLengths(200.0), detection__freeze_seconds=120.0,
              detection__use_track_length=True)
    rig.chain.script.append(hit(dict(HEY_JUDE, offset=170.0)))      # 178 s into 200 s
    rig.step()
    settle(rig, 2)
    rig.chain.script.append(hit(LET_IT_BE))
    rig.step(advance=29.5)                          # 22 s left + 7 s into the next one
    assert rig.chain.calls == 2 and rig.last["title"] == "Let It Be"


def test_a_length_that_arrives_after_the_track_changed_is_dropped(tmp_path):
    gate = threading.Event()
    lengths = FakeLengths(200.0, gate)
    rig = Rig(tmp_path, lengths=lengths, detection__freeze_seconds=0.0,
              detection__use_track_length=True)
    rig.chain.script.append(hit(dict(HEY_JUDE, offset=10.0)))
    rig.step()
    rig.play(LET_IT_BE)                             # the track changed while the lookup hung
    count = len(rig.pub.states)
    gate.set()
    time.sleep(0.3)
    rig.step()
    assert rig.last["title"] == "Let It Be"
    assert rig.svc.tracker.current.duration in (None, 200.0)
    assert len(rig.pub.states) <= count + 1         # at most Let It Be's own length arriving
    assert all(s["title"] != "Hey Jude" for s in rig.pub.states[count:])


def test_no_length_lookup_when_switched_off(tmp_path):
    lengths = FakeLengths(200.0)
    rig = Rig(tmp_path, lengths=lengths, detection__use_track_length=False)
    rig.play(dict(HEY_JUDE, offset=60.0))
    time.sleep(0.1)
    rig.step()
    assert lengths.asked == []
    assert rig.last["duration"] is None


def test_a_pause_in_the_sound_still_triggers_a_lookup_during_the_freeze(tmp_path):
    rig = Rig(tmp_path, detection__freeze_seconds=120.0)
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step()
    rig.step(advance=40.0)
    assert rig.chain.calls == 1
    rig.capture.last_onset_at = rig.clock.now       # sound came back after a gap: a new track?
    rig.chain.script.append(hit(LET_IT_BE))
    rig.step(advance=rig.det.min_window_seconds)
    assert rig.chain.calls == 2 and rig.last["title"] == "Let It Be"


def test_identify_now_cuts_the_freeze_short(tmp_path):
    rig = Rig(tmp_path, detection__freeze_seconds=120.0)
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step()
    rig.step(advance=20.0)
    rig.svc.handle_command("identify")
    rig.chain.script.append(hit(LET_IT_BE))
    rig.step()
    assert rig.chain.calls == 2 and rig.last["title"] == "Let It Be"


def test_unrecognised_music_after_the_expected_end_clears_the_old_track(tmp_path):
    rig = Rig(tmp_path, lengths=FakeLengths(200.0), detection__freeze_seconds=120.0,
              detection__use_track_length=True)
    rig.chain.script.append(hit(dict(HEY_JUDE, offset=170.0)))
    rig.step()
    settle(rig, 2)
    rig.chain.default = MISS
    rig.step(advance=29.5)                          # first lookup after the end: no match
    assert rig.chain.calls == 2 and rig.last["title"] == "Hey Jude"
    rig.step(advance=rig.det.search_interval)       # the second one settles it
    assert rig.chain.calls == 3 and rig.last["state"] == "idle"
