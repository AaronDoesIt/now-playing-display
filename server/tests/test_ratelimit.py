import datetime as dt

import pytest

from nowplaying.ratelimit import Budget

from helpers import FakeClock


class Calendar:
    def __init__(self):
        self.day = dt.date(2026, 11, 10)

    def __call__(self):
        return self.day


def make(**kwargs):
    clock, cal = FakeClock(), Calendar()
    return Budget(clock=clock, today=cal, **kwargs), clock, cal


def test_min_interval():
    budget, clock, _ = make(min_interval=3.0)
    assert budget.ready()
    budget.note_call()
    assert not budget.ready()
    assert budget.wait_time() == pytest.approx(3.0)
    clock.advance(3.0)
    assert budget.ready()


def test_daily_limit_and_rollover():
    budget, clock, cal = make(daily_limit=2)
    budget.note_call()
    budget.note_call()
    assert budget.exhausted
    assert budget.wait_time() == float("inf")
    cal.day += dt.timedelta(days=1)
    assert not budget.exhausted
    assert budget.ready()
    assert budget.calls == 2 and budget.calls_today == 0


def test_failures_back_off_exponentially_and_success_clears():
    budget, clock, _ = make()
    assert budget.note_failure() == 30.0
    assert not budget.ready()
    clock.advance(30.0)
    assert budget.ready()
    assert budget.note_failure() == 60.0
    assert budget.note_failure() == 120.0
    budget.note_success()
    assert budget.ready()
    assert budget.note_failure() == 30.0
    for _ in range(20):
        last = budget.note_failure()
    assert last == Budget.MAX_COOLDOWN


def test_retry_after_is_honoured_but_capped():
    budget, clock, _ = make()
    assert budget.note_failure(retry_after=300) == 300.0
    clock.advance(299)
    assert not budget.ready()
    clock.advance(2)
    assert budget.ready()
    assert budget.note_failure(retry_after=10**6) == Budget.MAX_COOLDOWN


def test_per_minute_cap_is_a_rolling_window():
    budget, clock, _ = make(max_per_minute=3)
    for second in (0, 10, 20):
        assert budget.ready()
        budget.note_call()
        clock.advance(10)
    # t = 30: three calls in the last minute, the oldest at t = 0
    assert not budget.ready()
    assert budget.wait_time() == pytest.approx(Budget.MINUTE - 30)
    clock.advance(Budget.MINUTE - 30)
    assert budget.ready()                       # the call at t = 0 has aged out
    budget.note_call()
    assert budget.wait_time() == pytest.approx(10.0)      # now waiting on the call at t = 10


def test_steady_twenty_second_pace_never_trips_a_cap_of_three():
    budget, clock, _ = make(min_interval=10.0, max_per_minute=3)
    for _ in range(30):
        assert budget.ready()
        budget.note_call()
        clock.advance(21)


def test_per_minute_cap_combines_with_min_interval_and_cooldown():
    budget, clock, _ = make(min_interval=10.0, max_per_minute=2)
    budget.note_call()
    assert budget.wait_time() == pytest.approx(10.0)      # spacing
    clock.advance(10)
    budget.note_call()
    assert budget.wait_time() == pytest.approx(Budget.MINUTE - 10)   # the cap dominates
    budget.note_failure(retry_after=300)
    assert budget.wait_time() == pytest.approx(300.0)     # the cool-down dominates


def test_counters():
    budget, _, _ = make()
    budget.note_call()
    budget.note_success(hit=True)
    budget.note_call()
    budget.note_success()
    budget.note_call()
    budget.note_failure()
    assert (budget.calls, budget.hits, budget.errors) == (3, 1, 1)
