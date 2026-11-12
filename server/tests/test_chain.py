import datetime as dt

from nowplaying.models import AudioWindow, Match
from nowplaying.recognizers import RecognizerChain
from nowplaying.recognizers.base import RateLimited, Recognizer, RecognizerError

from helpers import FakeClock, tone

WINDOW = AudioWindow(pcm=tone(1.0), sample_rate=16000)
HIT = Match(title="Hey Jude", artist="The Beatles")


class Scripted(Recognizer):
    """Returns (or raises) the next item of a script; repeats the last one."""

    def __init__(self, name, script, min_interval=0.0, daily_limit=0):
        self.name = name
        self.script = list(script)
        self.min_interval = min_interval
        self.daily_limit = daily_limit
        self.calls = 0

    def recognize(self, window):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


def chain(*recognizers):
    clock = FakeClock()
    return RecognizerChain(list(recognizers), clock=clock,
                           today=lambda: dt.date(2026, 11, 10)), clock


def test_first_match_wins_and_later_recognizers_are_not_called():
    a, b = Scripted("a", [Match(title="T", artist="A")]), Scripted("b", [HIT])
    c, _ = chain(a, b)
    result = c.recognize(WINDOW)
    assert result.match.title == "T" and result.answered
    assert result.match.source == "a"                 # the chain stamps the source
    assert b.calls == 0


def test_falls_through_on_no_match():
    a, b = Scripted("a", [None]), Scripted("b", [HIT])
    c, _ = chain(a, b)
    result = c.recognize(WINDOW)
    assert result.match.title == "Hey Jude" and result.match.source == "b"
    assert (a.calls, b.calls) == (1, 1)


def test_clean_miss_from_everyone_is_answered():
    c, _ = chain(Scripted("a", [None]), Scripted("b", [None]))
    result = c.recognize(WINDOW)
    assert result.match is None and result.answered is True


def test_rate_limited_recognizer_is_rested_and_the_next_one_used():
    a = Scripted("a", [RateLimited("slow down", retry_after=120), HIT])
    b = Scripted("b", [Match(title="From B", artist="B")])
    c, clock = chain(a, b)
    assert c.recognize(WINDOW).match.title == "From B"
    assert c.recognize(WINDOW).match.title == "From B"
    assert a.calls == 1                               # still resting
    clock.advance(121)
    assert c.recognize(WINDOW).match.title == "Hey Jude"
    assert a.calls == 2


def test_all_failing_is_unanswered_and_reports_a_wait():
    a = Scripted("a", [RecognizerError("down")])
    b = Scripted("b", [RuntimeError("bug in the recognizer")])      # crashes are contained too
    c, clock = chain(a, b)
    result = c.recognize(WINDOW)
    assert result.match is None and result.answered is False
    assert c.wait_time() == 30.0                      # both resting
    assert c.recognize(WINDOW).answered is False      # nobody is asked while resting
    assert (a.calls, b.calls) == (1, 1)
    clock.advance(30)
    assert c.wait_time() == 0.0


def test_min_interval_and_daily_limit():
    a = Scripted("a", [None], min_interval=5.0, daily_limit=2)
    c, clock = chain(a)
    assert c.wait_time() == 0.0
    c.recognize(WINDOW)
    assert c.wait_time() == 5.0
    clock.advance(5)
    c.recognize(WINDOW)
    clock.advance(5)
    assert c.wait_time() == float("inf")              # the day's allowance is spent
    assert c.recognize(WINDOW).answered is False
    assert a.calls == 2


def test_stats():
    a, b = Scripted("a", [None, HIT]), Scripted("b", [RecognizerError("x")])
    c, _ = chain(a, b)
    c.recognize(WINDOW)
    c.recognize(WINDOW)
    stats = c.stats()
    assert stats["a"] == {"calls": 2, "hits": 1, "errors": 0, "calls_today": 2}
    assert stats["b"]["errors"] == 1
