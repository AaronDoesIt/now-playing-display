"""The "what is playing" state machine. Pure logic: no I/O, time is passed in.

It turns a stream of lookup results (match / miss / silence) into a stable
answer, so the display does not flicker when one lookup fails, and it decides
how soon the next lookup is worth making.

Once a track is showing, the tracker goes quiet rather than re-checking at a
fixed pace. If it knows how far into the track the music is (the recognizer
says) and how long the track runs (looked up once), the next lookup is timed
for just after the track should end. If the length is unknown it waits
``freeze_seconds`` after the track first appeared and then returns to a
re-check every ``locked_interval``.
"""

from __future__ import annotations

from .config import DetectionConfig
from .models import Match, same_track

FAST_ATTEMPTS = 2       # lookups at the base interval before backing off
WINDOW_STEP = 3.0       # extra seconds of audio used after each miss
AFTER_END_EXTRA = 1.0   # the lookup after a track's end waits min_window_seconds plus this
OVERRUN_GRACE = 20.0    # still the same track this long after its listed end: the length is wrong
MIN_TRACK_SECONDS = 20.0
MAX_TRACK_SECONDS = 3 * 3600.0


class Tracker:
    def __init__(self, cfg: DetectionConfig) -> None:
        self.cfg = cfg
        self.current: Match | None = None
        self._candidate: Match | None = None
        self._candidate_hits = 0
        self._last_seen = 0.0
        self._misses = 0
        self._event_time = 0.0                  # when the last match / miss / silence came in
        self._shown_at = 0.0                    # when the current track was first shown
        self._started_at: float | None = None   # when the current track began, from its position
        self._trusted_until = 0.0               # end of a planned quiet period (0 = none)

    # ---------------------------------------------------------------- events

    def on_match(self, match: Match, now: float, position: float | None = None) -> bool:
        """A recognizer named a track. Returns True if what should be shown changed.

        *position* is how far into the track the music is at *now*, in seconds,
        when the recognizer reported where its clip came from.
        """
        self._event_time = now
        self._misses = 0
        if same_track(self.current, match):
            assert self.current is not None
            self._last_seen = now
            self._candidate = None
            self._candidate_hits = 0
            self._note_position(position, now)
            return self.current.fill_blanks_from(match)

        if same_track(self._candidate, match):
            self._candidate_hits += 1
        else:
            self._candidate = match
            self._candidate_hits = 1
        # An answer the recognizer flagged as shaky must be repeated by another
        # lookup before it is believed; a clean one is shown straight away
        # (unless the configuration asks for confirmation of everything).
        needed = max(self.cfg.confirmations, 2 if match.tentative else 1)
        if self._candidate_hits < needed:
            return False
        self.current = self._candidate
        self._candidate = None
        self._candidate_hits = 0
        self._last_seen = now
        self._shown_at = now
        self._started_at = None
        self._trusted_until = 0.0
        self._note_position(position, now)
        return True

    def on_miss(self, now: float) -> bool:
        """Audio was present but nothing recognised it."""
        self._event_time = now
        self._misses += 1
        if self.current is not None and self._stale(now):
            self._clear()
            return True
        return False

    def on_unavailable(self, now: float) -> bool:
        """No recognizer could be asked (all resting or over budget).

        That is not evidence about the music, so it is not counted as a miss, but
        a track that has gone unconfirmed for hold_seconds is still dropped: a
        stale title is worse than none.
        """
        self._event_time = now
        if self.current is not None and self._stale(now):
            self._clear()
            return True
        return False

    def on_silence(self, now: float, trailing_silence: float) -> bool:
        """The input is silent. Clears the track once the silence has lasted idle_after."""
        self._event_time = now
        self._misses = 0
        self._candidate = None
        self._candidate_hits = 0
        if self.current is not None and trailing_silence >= self.cfg.idle_after:
            self._clear()
            return True
        return False

    def on_onset(self) -> None:
        """Sound resumed after a gap: probably a new track, so search eagerly again.

        It also ends a quiet period: the track is no longer taken on trust.
        """
        self._misses = 0
        self._trusted_until = 0.0

    def reset(self) -> bool:
        """Forget everything (listening switched off, capture lost)."""
        had = self.current is not None
        self._clear()
        self._misses = 0
        return had

    def set_duration(self, seconds: float | None) -> bool:
        """The current track's length became known. Returns True if it was accepted."""
        if self.current is None or seconds is None:
            return False
        if not MIN_TRACK_SECONDS <= seconds <= MAX_TRACK_SECONDS:
            return False
        self.current.duration = float(seconds)
        return True

    def _clear(self) -> None:
        self.current = None
        self._candidate = None
        self._candidate_hits = 0
        self._started_at = None
        self._trusted_until = 0.0

    def _note_position(self, position: float | None, now: float) -> None:
        if position is not None and position >= 0:
            self._started_at = now - position

    def _stale(self, now: float) -> bool:
        """Has the current track gone unconfirmed for too long?

        During a planned quiet period nobody was asking, so the clock starts at
        its end. Once the track should be over by its own length, a short wait
        is enough: two failed lookups rather than a full hold_seconds.
        """
        hold = self.cfg.hold_seconds
        end = self.expected_end()
        if end is not None and now > end:
            hold = min(hold, self.cfg.search_interval)
        return now - max(self._last_seen, self._trusted_until) >= hold

    # -------------------------------------------------------------- scheduling

    @property
    def state(self) -> str:
        return "playing" if self.current is not None else "idle"

    @property
    def misses(self) -> int:
        return self._misses

    def expected_end(self) -> float | None:
        """When the current track should end (same clock as the events), or None if unknown."""
        current = self.current
        if (not self.cfg.use_track_length or current is None or self._started_at is None
                or not current.duration):
            return None
        return self._started_at + current.duration

    def next_delay(self, now: float | None = None) -> float:
        """Seconds to wait before the next lookup, counted from *now* (default: the last event).

        When this starts a quiet period it also remembers until when the current
        track is taken on trust.
        """
        cfg = self.cfg
        if now is None:
            now = self._event_time
        if self._candidate is not None:
            return cfg.search_interval          # an answer is waiting to be confirmed
        if self.current is not None:
            if self._misses:
                return cfg.search_interval      # a miss usually means the track just changed
            delay, quiet = self._quiet_period(now)
            if quiet:
                self._trusted_until = now + delay
            return delay
        factor = 2 ** max(0, self._misses - FAST_ATTEMPTS)
        return min(cfg.search_interval * factor, cfg.search_max_interval)

    def _quiet_period(self, now: float) -> tuple[float, bool]:
        """(seconds until the next routine lookup, whether that is a planned quiet period)."""
        cfg = self.cfg
        end = self.expected_end()
        if end is not None:
            remaining = end - now
            if remaining > -OVERRUN_GRACE:
                # Look again once the next track has played long enough for a clip.
                wait = remaining + cfg.min_window_seconds + AFTER_END_EXTRA
                return min(max(wait, cfg.search_interval), cfg.max_freeze_seconds), True
            return cfg.locked_interval, False   # long past its listed end: do not trust the length
        frozen_for = now - self._shown_at
        if cfg.freeze_seconds > frozen_for:
            return max(cfg.freeze_seconds - frozen_for, cfg.locked_interval), True
        return cfg.locked_interval, False

    def window_seconds(self) -> float:
        """How much audio the next lookup should use (grows after misses)."""
        cfg = self.cfg
        return min(cfg.window_seconds + WINDOW_STEP * min(self._misses, 8),
                   cfg.max_window_seconds)
