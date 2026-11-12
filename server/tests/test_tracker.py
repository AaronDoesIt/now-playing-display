import pytest

from nowplaying.config import DetectionConfig
from nowplaying.models import Match
from nowplaying.tracker import Tracker

A = Match(title="Hey Jude", artist="The Beatles", source="shazam")
A_VARIANT = Match(title="Hey Jude - Remastered 2015", artist="The Beatles", source="acoustid",
                  album="1")
B = Match(title="Let It Be", artist="The Beatles", source="shazam")


def fresh(match):
    return Match(**{**match.__dict__})


def make(**overrides):
    # These tests pin down the basic rhythm. The quiet period after a match is
    # switched off here; it has its own tests in test_quiet_period.py.
    return Tracker(DetectionConfig(**{"freeze_seconds": 0.0, "use_track_length": False,
                                      **overrides}))


def test_first_match_starts_playing():
    tracker = make()
    assert tracker.state == "idle"
    assert tracker.on_match(fresh(A), now=0) is True
    assert tracker.state == "playing"
    assert tracker.current.title == "Hey Jude"


def test_same_track_again_changes_nothing():
    tracker = make()
    tracker.on_match(fresh(A), now=0)
    assert tracker.on_match(fresh(A), now=20) is False
    assert tracker.current.title == "Hey Jude"


def test_release_variant_is_not_a_track_change_but_fills_blanks():
    tracker = make()
    tracker.on_match(fresh(A), now=0)
    assert tracker.on_match(fresh(A_VARIANT), now=20) is True      # album became known
    assert tracker.current.title == "Hey Jude"                     # the title stays stable
    assert tracker.current.album == "1"
    assert tracker.on_match(fresh(A_VARIANT), now=40) is False


def test_new_track_replaces_the_old_one():
    tracker = make()
    tracker.on_match(fresh(A), now=0)
    assert tracker.on_match(fresh(B), now=20) is True
    assert tracker.current.title == "Let It Be"


def test_confirmations_require_repeated_agreement():
    tracker = make(confirmations=2)
    assert tracker.on_match(fresh(A), now=0) is False
    assert tracker.state == "idle"
    assert tracker.next_delay() == tracker.cfg.search_interval     # confirm quickly
    assert tracker.on_match(fresh(B), now=6) is False              # a different answer restarts
    assert tracker.on_match(fresh(B), now=12) is True
    assert tracker.current.title == "Let It Be"


def shaky(match):
    copy = fresh(match)
    copy.tentative = True
    return copy


def test_a_shaky_answer_is_only_shown_once_a_second_lookup_agrees():
    tracker = make()
    assert tracker.on_match(shaky(A), now=0) is False
    assert tracker.state == "idle"
    assert tracker.next_delay() == tracker.cfg.search_interval      # confirm soon
    assert tracker.on_match(shaky(A), now=15) is True               # the same again: believed
    assert tracker.current.title == "Hey Jude"


def test_a_clean_answer_is_shown_at_once_and_can_confirm_a_shaky_one():
    tracker = make()
    assert tracker.on_match(fresh(A), now=0) is True
    tracker = make()
    tracker.on_match(shaky(A), now=0)
    assert tracker.on_match(fresh(A), now=15) is True


def test_a_one_off_wrong_answer_never_replaces_the_track_that_is_showing():
    """What happened in the live test: one stray answer in the middle of a song."""
    tracker = make()
    tracker.on_match(fresh(A), now=0)
    assert tracker.on_match(shaky(B), now=30) is False              # a doubtful different answer
    assert tracker.current.title == "Hey Jude"
    assert tracker.next_delay() == tracker.cfg.search_interval      # re-check sooner than usual
    assert tracker.on_match(fresh(A), now=45) is False              # the right one again
    assert tracker.current.title == "Hey Jude"
    assert tracker.next_delay() == tracker.cfg.locked_interval      # back to the normal pace
    assert tracker.on_match(shaky(B), now=75) is False              # doubt does not accumulate
    assert tracker.current.title == "Hey Jude"


def test_a_shaky_answer_followed_by_a_miss_starts_over():
    tracker = make()
    tracker.on_match(shaky(A), now=0)
    tracker.on_silence(now=5, trailing_silence=2)
    assert tracker.on_match(shaky(A), now=20) is False              # counted from one again
    assert tracker.on_match(shaky(A), now=35) is True


def test_misses_are_tolerated_for_hold_seconds():
    tracker = make(hold_seconds=60)
    tracker.on_match(fresh(A), now=0)
    assert tracker.on_miss(now=20) is False
    assert tracker.on_miss(now=59) is False
    assert tracker.state == "playing"
    assert tracker.on_miss(now=60) is True
    assert tracker.state == "idle"


def test_a_match_refreshes_the_hold_timer():
    tracker = make(hold_seconds=60)
    tracker.on_match(fresh(A), now=0)
    tracker.on_miss(now=50)
    tracker.on_match(fresh(A), now=55)
    assert tracker.on_miss(now=100) is False                       # 45 s since last seen
    assert tracker.on_miss(now=116) is True


def test_silence_clears_after_idle_after_only():
    tracker = make(idle_after=20)
    tracker.on_match(fresh(A), now=0)
    assert tracker.on_silence(now=5, trailing_silence=3) is False   # the gap between songs
    assert tracker.state == "playing"
    assert tracker.on_silence(now=30, trailing_silence=20) is True
    assert tracker.state == "idle"
    assert tracker.on_silence(now=31, trailing_silence=21) is False  # already idle


def test_search_backs_off_when_nothing_is_recognised():
    tracker = make(search_interval=6, search_max_interval=90)
    delays = []
    for i in range(10):
        tracker.on_miss(now=i)
        delays.append(tracker.next_delay())
    assert delays[:2] == [6, 6]                 # two quick tries...
    assert delays[2:6] == [12, 24, 48, 90]      # ...then it backs off to the ceiling
    assert delays[-1] == 90


def test_backoff_resets_on_silence_onset_and_match():
    tracker = make()
    for i in range(8):
        tracker.on_miss(now=i)
    assert tracker.next_delay() > tracker.cfg.search_interval
    tracker.on_silence(now=9, trailing_silence=1)
    assert tracker.next_delay() == tracker.cfg.search_interval
    for i in range(8):
        tracker.on_miss(now=10 + i)
    tracker.on_onset()
    assert tracker.next_delay() == tracker.cfg.search_interval


def test_locked_interval_and_quick_retry_after_a_miss():
    tracker = make(search_interval=6, locked_interval=20)
    tracker.on_match(fresh(A), now=0)
    assert tracker.next_delay() == 20
    tracker.on_miss(now=20)
    assert tracker.next_delay() == 6                # probably a track change: look again soon
    tracker.on_match(fresh(A), now=26)
    assert tracker.next_delay() == 20


def test_window_grows_after_misses_up_to_the_maximum():
    tracker = make(window_seconds=8, max_window_seconds=14)
    assert tracker.window_seconds() == 8
    tracker.on_miss(now=0)
    assert tracker.window_seconds() == 11
    tracker.on_miss(now=1)
    assert tracker.window_seconds() == 14
    tracker.on_miss(now=2)
    assert tracker.window_seconds() == 14
    tracker.on_match(fresh(A), now=3)
    assert tracker.window_seconds() == 8


def test_unavailable_recognizers_do_not_count_as_misses_but_stale_tracks_still_expire():
    tracker = make(hold_seconds=60)
    tracker.on_match(fresh(A), now=0)
    assert tracker.on_unavailable(now=30) is False
    assert tracker.misses == 0
    assert tracker.on_unavailable(now=61) is True
    assert tracker.state == "idle"
    assert tracker.on_unavailable(now=62) is False


def test_reset():
    tracker = make()
    assert tracker.reset() is False
    tracker.on_match(fresh(A), now=0)
    assert tracker.reset() is True
    assert tracker.state == "idle" and tracker.current is None
