"""The daemon's main loop: capture -> recognise -> track -> cover art -> publish."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import logging
import queue
import threading
import time
from typing import Any, Callable

from .artwork import Art, ArtworkService
from .capture import AudioCapture
from .config import Config
from .models import Match, same_track
from .musicdetect import MusicDetector, Verdict
from .recognizers import RecognizerChain
from .tracker import Tracker

log = logging.getLogger(__name__)

DETECTOR_GRACE = 30.0       # how long a silent music detector is waited for before it is bypassed
IMMINENT = 3.0              # a lookup this close is waited for before a stale track is dropped
CLIP_SLACK = 0.5            # a clip may begin this much before a moment and still count as after it

# What "there is sound, but the music detector says it is not music" means on a given step:
SHUT = "shut"               # believe it: no lookup (talk costs nothing)
ASK = "ask"                 # a track is showing: one lookup of that sound decides
OVERRULED = "overruled"     # that lookup found the track again: carry on as with music

_ON = {"on", "true", "1", "yes"}
_OFF = {"off", "false", "0", "no"}
_ACTIVE_STATES = {"playing", "on", "buffering"}


class NowPlayingService:
    """Owns the state and decides, step by step, what to do next.

    ``_step`` does one unit of work and returns how long to wait before the
    next, so the whole loop can be driven by a fake clock in tests.
    """

    def __init__(
        self,
        cfg: Config,
        capture: AudioCapture | None,
        chain: RecognizerChain,
        artwork: ArtworkService,
        clock: Callable[[], float] = time.monotonic,
        detector: MusicDetector | None = None,
        lengths: Any = None,
    ) -> None:
        self.cfg = cfg
        self.capture = capture
        self.chain = chain
        self.artwork = artwork
        self.detector = detector
        self.lengths = lengths      # looks up how long a track runs (metadata.TrackLengths)
        self._clock = clock
        self.tracker = Tracker(cfg.detection)
        self.art_server: Any = None
        self.publisher: Any = None
        self.external: Match | None = None
        self._state_file = cfg.data_dir / "state.json"
        self.listening = self._load_listening()
        self._commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=2,
                                                           thread_name_prefix="art")
        # Its own worker: a slow length lookup must never hold up a cover.
        self._length_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                                  thread_name_prefix="length")
        self._generation = 0
        self._payload: dict[str, Any] = {}
        self._art: Art | None = None
        self._next_lookup = 0.0
        self._routine_wait = False  # True while _next_lookup is the tracker's own schedule
        self._slide_index = 0
        self._slide_shown_at = 0.0
        self._boundary_seen: float | None = None
        self._music_since_seen: float | None = None
        self._music_started_at: float | None = None
        self._heard_from: float | None = None   # where the clip began that last found the track showing
        self._doubt_seen: float | None = None   # the detector's "not music since" now being dealt with
        self._doubt_asked = False               # the one lookup for it has been made
        self._force = False
        self._last_audio_at = 0.0
        self._last_is_music: bool | None = None
        self._detector_warming = False
        self._detector_quiet_since: float | None = None
        self._detector_warned = False
        self._last_restart_at = 0.0
        self._started_at = clock()

    def attach(self, art_server: Any, publisher: Any) -> None:
        self.art_server = art_server
        self.publisher = publisher

    # ------------------------------------------------------------- lifecycle

    def run(self) -> None:
        self.art_server.start()
        self.publisher.start()
        if self.capture is not None:
            self.capture.start()
            if self.detector is not None:
                self.detector.start()
        self.publisher.publish_listening(self.listening)
        self._publish()
        try:
            while not self._stop.is_set():
                delay = self._step()
                self._wake.wait(delay)
                self._wake.clear()
        finally:
            self.shutdown()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def shutdown(self) -> None:
        log.info("shutting down")
        if self.detector is not None:
            self.detector.stop()
        if self.capture is not None:
            self.capture.stop()
        self.tracker.reset()
        self.external = None
        try:
            self._generation += 1
            self._emit("off", None, self.artwork.idle())
        except Exception:
            log.debug("final state publish failed", exc_info=True)
        self.publisher.stop()
        self.art_server.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._length_pool.shutdown(wait=False, cancel_futures=True)
        self.chain.close()

    # --------------------------------------------------------------- commands

    def handle_command(self, name: str, payload: Any = b"") -> None:
        """Queue a command. Safe to call from any thread (MQTT, signal handlers)."""
        self._commands.put((name, payload))
        self._wake.set()

    def _drain_commands(self) -> None:
        while True:
            try:
                name, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if name == "listening":
                    self._set_listening(payload)
                elif name == "identify":
                    self._identify_now()
                elif name == "external":
                    self._set_external(payload)
                elif name == "art":
                    self._art_arrived(*payload)
                elif name == "length":
                    self._length_arrived(*payload)
                else:
                    log.warning("unknown command %r", name)
            except Exception:
                log.exception("command %r failed", name)

    def _set_listening(self, payload: bytes) -> None:
        text = bytes(payload).decode("utf-8", "replace").strip().lower()
        if text in _ON:
            wanted = True
        elif text in _OFF:
            wanted = False
        else:
            log.warning("listening/set: expected ON or OFF, got %r", text[:40])
            return
        if wanted == self.listening:
            self.publisher.publish_listening(self.listening)
            return
        self.listening = wanted
        self._save_listening()
        self.publisher.publish_listening(wanted)
        log.info("listening switched %s", "on" if wanted else "off")
        if wanted:
            self.tracker.on_onset()
            self._next_lookup = self._clock()
            self._routine_wait = False
        else:
            self.tracker.reset()
        self._publish()

    def _identify_now(self) -> None:
        log.info("identify requested")
        self._force = True
        self.tracker.on_onset()
        self._next_lookup = self._clock()
        self._routine_wait = False

    def _set_external(self, payload: bytes) -> None:
        """A track pushed in by Home Assistant (e.g. from a Spotify media_player).

        While one is active it is shown instead of the acoustic result and no
        lookups are made. An empty payload, or a state other than playing,
        clears it and listening resumes.
        """
        text = bytes(payload).decode("utf-8", "replace").strip()
        track: Match | None = None
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                log.warning("external/set: payload is not JSON")
                return
            if not isinstance(data, dict):
                log.warning("external/set: expected a JSON object")
                return
            state = str(data.get("state") or "playing").strip().lower()
            title = str(data.get("title") or "").strip()
            if state in _ACTIVE_STATES and title:
                track = Match(
                    title=title,
                    artist=str(data.get("artist") or "").strip(),
                    album=str(data.get("album") or "").strip(),
                    source=str(data.get("source") or "external").strip()[:40] or "external",
                    art_url=self._absolute_url(str(data.get("art_url") or "").strip()),
                )
        if track is None and self.external is None:
            return
        if (track is not None and self.external is not None and same_track(track, self.external)
                and track.art_url == self.external.art_url
                and track.album == self.external.album):
            return
        self.external = track
        if track is None:
            log.info("external source stopped; listening again")
            self.tracker.on_onset()
            self._next_lookup = self._clock()
            self._routine_wait = False
        else:
            self.tracker.reset()
        self._publish()

    def _absolute_url(self, url: str) -> str:
        if url.lower().startswith(("http://", "https://")):
            return url
        if url.startswith("/"):       # e.g. a media_player entity_picture
            base = self.cfg.external.ha_base_url.rstrip("/")
            if base:
                return base + url
            log.warning("external/set: relative art_url needs external.ha_base_url to be set")
        return ""

    # ------------------------------------------------------------------- loop

    def _step(self) -> float:
        """Do one unit of work; return the seconds to wait before the next."""
        self._drain_commands()
        now = self._clock()
        self._tick_slides(now)
        verdict = self._verdict()
        self._publish_audio(now, verdict)
        cap = self.capture
        if cap is None or not self.listening or self.external is not None:
            return 1.0
        det = self.cfg.detection

        if not cap.healthy:
            age = cap.seconds_since_data()
            # stall_seconds = 0 switches the restart off: with a UDP input, no packets
            # just means the sender is off, and ffmpeg is still listening.
            stall = self.cfg.capture.stall_seconds
            if stall > 0 and age is not None and age > stall \
                    and now - self._last_restart_at > stall:
                self._last_restart_at = now
                cap.restart()
            if self.tracker.current is not None and (age is None or age > det.idle_after):
                log.warning("audio input lost; clearing the current track")
                self.tracker.reset()
                self._publish()
            return 1.0

        trailing = cap.trailing_silence()
        if trailing >= det.gap_seconds:
            if self.tracker.on_silence(now, trailing):
                self._publish()
            return 0.5

        if verdict is None and self._detector_warming and not self._force:
            return 0.5                          # the detector has not judged this audio yet

        gate = None
        if verdict is not None:
            if not verdict.is_music:
                # There is sound, but the detector says it is not music: dialog, news,
                # a phone call. Normally that means no lookup at all.
                gate = self._not_music(now, verdict)
                if gate == SHUT and not self._force:    # "Identify now" overrides the gate once
                    return 0.5
            elif verdict.since != self._music_since_seen:
                # The gate has just opened. If the detector heard the music start,
                # that moment is a boundary; if music was already playing when
                # listening began, there is none and a full clip can be used.
                self._music_since_seen = verdict.since
                self._music_started_at = verdict.began

        # A boundary is where new sound began: after a silent gap, or where the
        # detector first heard music. Lookups never reach back across it.
        starts = [t for t in (cap.last_onset_at, self._music_started_at) if t is not None]
        boundary = max(starts) if starts else None
        if boundary is not None and boundary != self._boundary_seen:
            # Most likely a new track. Look as soon as there is enough of it:
            # not before (a clip that is mostly the old sound is a wasted
            # request), and without waiting out the usual interval.
            self._boundary_seen = boundary
            self.tracker.on_onset()
            self._next_lookup = boundary + det.min_window_seconds
            self._routine_wait = False

        if not self._force and now < self._next_lookup:
            return min(1.0, self._next_lookup - now)

        wait = self.chain.wait_time()
        if wait > 0:
            # A lookup that is only moments away will settle what is playing.
            # Dropping the track just before it would blank the display for a
            # blink between two songs, so only a longer wait lets it expire.
            if wait > IMMINENT and self.tracker.on_unavailable(now):
                self._publish()
            return min(5.0, wait)

        want = self.tracker.window_seconds()
        if boundary is not None:
            want = min(want, max(det.min_window_seconds, now - boundary))
        ended = self.tracker.expected_end()
        if ended is not None and now > ended:
            # The track that was showing should be over: like a boundary, the clip
            # should not reach back into it.
            want = min(want, max(det.min_window_seconds, now - ended))
        if gate == ASK:
            # Only the sound the detector has doubts about, not the music before it.
            want = min(want, max(det.min_window_seconds, now - verdict.since))
        if not self._force and cap.level_dbfs(want) < det.silence_dbfs:
            if gate == ASK:
                self._doubt_asked = True        # next to nothing to hear: nothing to look up
            return 0.5
        taken_at = now
        window = cap.snapshot(want, minimum=det.min_window_seconds)
        if window is None:
            return 0.5
        clip_from = taken_at - window.duration

        self._force = False
        result = self.chain.recognize(window)
        now = self._clock()
        if gate == ASK:
            if clip_from >= verdict.since - CLIP_SLACK:
                self._doubt_asked = True
            else:                               # (a forced lookup that reached back into the music)
                self._doubt_seen = None         # ask again once a clean clip is possible
        if result.match is not None:
            # The skew is how far the clip is from lining up with the reference recording
            # in time or pitch (Shazam only). It is logged so recognizers.shazam.confirm_skew
            # can be set from what a real microphone in a real room produces.
            skew = result.match.extra.get("skew")
            log.info("lookup, %.0f s clip at %.0f dB: %s - %s%s%s", window.duration,
                     window.rms_dbfs, result.match.artist, result.match.title,
                     " (doubtful: shown only if the next lookup agrees)"
                     if result.match.tentative else "",
                     f" [skew {skew:.4f}]" if isinstance(skew, (int, float)) else "")
            # The recognizer reports where in the track its clip began; the music
            # has moved on by the clip's length and the time the lookup took.
            position = None
            if result.match.offset is not None:
                position = result.match.offset + window.duration + (now - taken_at)
            before = self.tracker.current
            changed = self.tracker.on_match(result.match, now, position)
            shown = self.tracker.current
            if shown is not None and shown is not before:
                self._request_length(shown)
            if same_track(shown, result.match):
                # This clip is the latest evidence for the track that is showing.
                self._heard_from = clip_from
                if gate == ASK and self._doubt_asked and shown is before:
                    log.info("still playing: the music detector heard no music (music %.2f, "
                             "speech %.2f) but the lookup found the track", verdict.music,
                             verdict.speech)
        elif result.answered:
            log.info("lookup, %.0f s clip at %.0f dB: no match", window.duration, window.rms_dbfs)
            changed = self.tracker.on_miss(now)
        else:
            log.info("lookup, %.0f s clip at %.0f dB: no recognizer could answer", window.duration,
                     window.rms_dbfs)
            changed = self.tracker.on_unavailable(now)
        if changed:
            self._publish()
        now = self._clock()
        delay = self.tracker.next_delay(now)
        self._next_lookup = now + delay
        self._routine_wait = True
        self._log_plan(delay, now)
        return min(1.0, delay)

    # ------------------------------------------- sound the detector calls "not music"

    def _not_music(self, now: float, verdict: Verdict) -> str:
        """The detector says the sound is not music. Returns SHUT, ASK or OVERRULED.

        With nothing showing that is the end of it: no lookup, so talk costs nothing.

        With a track showing, the detector's word alone does not take it down. It is
        often wrong about a song that is already playing: a rap verse scores as
        speech, and a quiet passage through a room microphone scores as nothing much
        (on 2026-11-11 it "heard the music stop" during a song about once a minute).
        So one lookup is made of the sound from after the detector changed its mind:

        * it finds the track: the detector is overruled for as long as it keeps
          saying "not music", and the track is re-checked on the usual schedule and
          dropped by the usual rules (lookups that keep failing, or real silence);
        * it finds nothing: the detector is believed, and the track is cleared once
          the music has been gone for idle_after, the same moment as before.

        The lookup waits until there is a full clip of the doubted sound, so the tail
        of a song that really did end cannot vouch for it.
        """
        det = self.cfg.detection
        gone_for = now - verdict.since
        if self.tracker.current is None:
            self.tracker.on_silence(now, gone_for)
            return SHUT
        if self._heard_from is not None and self._heard_from >= verdict.since - CLIP_SLACK:
            return OVERRULED
        if verdict.since != self._doubt_seen:
            self._doubt_seen = verdict.since
            self._doubt_asked = False
            self._next_lookup = verdict.since + det.window_seconds
            self._routine_wait = False
        if not self._doubt_asked and gone_for < det.hold_seconds:
            return ASK              # (past hold_seconds no lookup could be made: stop waiting)
        if self.tracker.on_silence(now, gone_for):
            self._publish()
        return SHUT

    # ------------------------------------------------------- the quiet period

    def _log_plan(self, delay: float, now: float) -> None:
        """Say so when the next lookup is further off than the usual re-check."""
        if self.tracker.current is None or delay <= self.cfg.detection.locked_interval:
            return
        end = self.tracker.expected_end()
        if end is not None:
            log.info("next lookup in %.0f s: this track should end in %.0f s", delay, end - now)
        else:
            log.info("next lookup in %.0f s: holding this track (its length is not known)", delay)

    def _request_length(self, track: Match) -> None:
        """Look up how long *track* runs, in the background."""
        if self.lengths is None or not self.cfg.detection.use_track_length or track.duration:
            return
        identity = track.identity()
        future = self._length_pool.submit(self.lengths.lookup, track)
        future.add_done_callback(lambda done, i=identity: self._length_done(i, done))

    def _length_done(self, identity: str, future: concurrent.futures.Future) -> None:
        if future.cancelled() or future.exception() is not None:
            return
        self.handle_command("length", (identity, future.result()))

    def _length_arrived(self, identity: str, seconds: float | None) -> None:
        track = self.tracker.current
        if track is None or track.identity() != identity or not self.tracker.set_duration(seconds):
            return
        now = self._clock()
        if self._routine_wait:
            # Plan again now that the end of the track can be worked out. That can be
            # later than the plain freeze, or sooner when the track is nearly over.
            delay = self.tracker.next_delay(now)
            self._next_lookup = now + delay
            self._log_plan(delay, now)
        if self.external is None and self._art is not None:
            self._emit(self._state_name(track), track, self._art, quiet=True)

    # ----------------------------------------------------- pictures while idle

    def _idle_art(self) -> Art:
        """What to show while nothing is playing: the current slide, or the plain tile.

        The same pictures are shown whether the daemon is listening and hears no
        music, or has been switched off.
        """
        slides = self.artwork.slides()
        if not slides:
            return self.artwork.idle()
        return slides[self._slide_index % len(slides)]

    def _tick_slides(self, now: float) -> None:
        """While nothing is playing (idle, or listening switched off), move on to the
        next picture every idle.slide_seconds."""
        state = self._payload.get("state")
        if state not in ("idle", "off") or self._stop.is_set():
            return
        if now - self._slide_shown_at < self.cfg.idle.slide_seconds:
            return
        self._slide_shown_at = now
        slides = self.artwork.slides()
        if not slides:
            return
        if len(slides) > 1:
            self._slide_index = (self._slide_index + 1) % len(slides)
        art = slides[self._slide_index % len(slides)]
        if self._art is None or art.key != self._art.key:
            self._emit(state, None, art, quiet=True)

    def _verdict(self) -> Verdict | None:
        """The music detector's current opinion, or None when there is none.

        Three cases give None, and ``_detector_warming`` tells them apart:

        * the detector is switched off or failed to load: there is no gate, and
          every sound is looked up as it would be without a detector;
        * it is running but has not judged the current audio yet (the first
          second after start, or after the input comes back): wait for it, so
          that not even one lookup slips through for speech;
        * it has gone quiet for DETECTOR_GRACE seconds although audio is
          arriving: treat it as broken and stop gating. A broken detector must
          not silence the display.
        """
        self._detector_warming = False
        detector = self.detector
        if detector is None or not detector.available:
            return None
        if self.capture is None or not self.capture.healthy:
            self._detector_quiet_since = None       # no audio: nothing to judge
            return None
        verdict = detector.verdict()
        if verdict.fresh:
            self._detector_quiet_since = None
            self._detector_warned = False
            return verdict
        now = self._clock()
        if self._detector_quiet_since is None:
            self._detector_quiet_since = now
        if now - self._detector_quiet_since < DETECTOR_GRACE:
            self._detector_warming = True
        elif not self._detector_warned:
            self._detector_warned = True
            log.warning("the music detector has produced nothing for %.0f s; "
                        "looking up every sound until it recovers", DETECTOR_GRACE)
        return None

    def _audio_report(self, verdict: Verdict | None) -> dict[str, Any]:
        cap = self.capture
        alive = bool(cap and cap.healthy)
        return {
            "level_dbfs": round(cap.level_dbfs(5.0), 1) if alive else None,
            "music": round(verdict.music, 2) if verdict else None,
            "speech": round(verdict.speech, 2) if verdict else None,
            "is_music": verdict.is_music if verdict else None,
        }

    def _publish_audio(self, now: float, verdict: Verdict | None) -> None:
        """Report the input level and the detector's verdict: on a timer, and at
        once whenever the verdict flips, so Home Assistant sees it promptly."""
        if self.capture is None:
            return
        is_music = verdict.is_music if verdict else None
        flipped = is_music != self._last_is_music
        interval = self.cfg.mqtt.level_interval
        due = interval > 0 and now - self._last_audio_at >= interval
        if not (flipped or due):
            return
        self._last_audio_at = now
        self._last_is_music = is_music
        if self.capture.healthy:
            self.publisher.publish_audio(self._audio_report(verdict))

    # ------------------------------------------------------------- publishing

    def _current_track(self) -> Match | None:
        return self.external if self.external is not None else self.tracker.current

    def _state_name(self, track: Match | None) -> str:
        if track is not None:
            return "playing"
        return "idle" if self.listening else "off"

    def _publish(self) -> None:
        """Publish the current track, fetching its cover (briefly) first."""
        self._generation += 1
        generation = self._generation
        track = self._current_track()
        if track is None:
            art = self._idle_art()
            self._slide_shown_at = self._clock()
        else:
            art = self.artwork.cached(track)
            if art is None:
                future = self._pool.submit(self.artwork.resolve, track)
                try:
                    art = future.result(timeout=self.cfg.artwork.wait_seconds)
                except concurrent.futures.TimeoutError:
                    # Show the title now with a generated cover; swap the real one
                    # in when it arrives rather than holding the text back.
                    art = self.artwork.placeholder(track)
                    future.add_done_callback(
                        lambda done, g=generation: self._art_done(g, done))
        self._emit(self._state_name(track), track, art)

    def _art_done(self, generation: int, future: concurrent.futures.Future) -> None:
        if future.cancelled() or future.exception() is not None:
            return
        self.handle_command("art", (generation, future.result()))

    def _art_arrived(self, generation: int, art: Art) -> None:
        track = self._current_track()
        if generation != self._generation or track is None:
            return                      # the track changed while the cover was loading
        if self._art is not None and art.key == self._art.key:
            return
        self._emit(self._state_name(track), track, art)

    def _emit(self, state: str, track: Match | None, art: Art, quiet: bool = False) -> None:
        payload: dict[str, Any] = {
            "state": state,
            "title": track.title if track else "",
            "artist": track.artist if track else "",
            "album": track.album if track else "",
            "art_url": self.art_server.url_for(art.key),
            "source": track.source if track else "",
            "art_source": art.source,
            "confidence": (round(track.confidence, 3)
                           if track and track.confidence is not None else None),
            "duration": round(track.duration, 1) if track and track.duration else None,
            "recording_mbid": track.recording_mbid if track else "",
            "release_group_mbid": (track.release_group_mbids[0]
                                   if track and track.release_group_mbids else ""),
            "isrc": track.isrc if track else "",
            "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        self._payload = payload
        self._art = art
        self.publisher.publish_state(payload)
        if quiet:       # a new slide, or the same track with one more detail: not news
            log.debug("republished: %s, art %s", state, art.key)
        elif track is not None:
            log.info("now playing: %s - %s%s (via %s, cover: %s)", track.artist, track.title,
                     f" [{track.album}]" if track.album else "", track.source or "?", art.source)
        else:
            log.info("state: %s", state)

    # ------------------------------------------------------------ for the HTTP server

    def state_snapshot(self) -> dict[str, Any]:
        snapshot = dict(self._payload)
        cap = self.capture
        snapshot.update(self._audio_report(self._verdict()))
        snapshot.update({
            "listening": self.listening,
            "capture_ok": bool(cap and cap.healthy),
            "level_dbfs": round(cap.level_dbfs(1.0), 1) if cap and cap.healthy else None,
            "mqtt_connected": bool(self.publisher and self.publisher.connected),
            "recognizers": self.chain.stats(),
        })
        return snapshot

    def health(self) -> dict[str, Any]:
        cap = self.capture
        capture_ok = bool(cap and cap.healthy)
        return {
            "ok": capture_ok or cap is None or not self.listening,
            "capture_ok": capture_ok,
            "mqtt_connected": bool(self.publisher and self.publisher.connected),
            "state": self._payload.get("state", ""),
            "uptime_s": int(self._clock() - self._started_at),
        }

    def current_art_key(self) -> str | None:
        return self._art.key if self._art else None

    # ---------------------------------------------------------- saved switch state

    def _load_listening(self) -> bool:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return bool(data.get("listening", True))
        except (OSError, ValueError, AttributeError):
            return True

    def _save_listening(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({"listening": self.listening}),
                                        encoding="utf-8")
        except OSError as exc:
            log.warning("could not save the listening switch state: %s", exc)
