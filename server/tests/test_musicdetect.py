import math
import struct

import pytest

from nowplaying.config import MusicDetectorConfig
from nowplaying.models import AudioWindow
from nowplaying.musicdetect import BUNDLED_MODEL, FRAME_BYTES, MusicDetector

from helpers import FakeClock, silence, tone


class FakeCapture:
    def __init__(self):
        self.healthy = True
        self.window = AudioWindow(pcm=tone(1.0), sample_rate=16000, rms_dbfs=-20.0)

    def snapshot(self, seconds, minimum=1.0):
        return self.window


def make(scores=None, **cfg):
    """A detector whose classifier returns scripted (music, speech) scores."""
    clock = FakeClock()
    capture = FakeCapture()
    calls = []
    script = list(scores or [])

    def classifier(pcm):
        calls.append(len(pcm))
        return script.pop(0) if script else (0.0, 0.0)

    detector = MusicDetector(MusicDetectorConfig(**cfg), capture, silence_dbfs=-50.0,
                             classifier=classifier, clock=clock)
    return detector, clock, capture, calls


def feed(detector, clock, values, speech=0.0):
    for music in values:
        clock.advance(1.0)
        detector.observe(music, speech)


# ---------------------------------------------------------------- the decision

def test_starts_closed_and_opens_once_the_smoothed_score_crosses_the_threshold():
    detector, clock, _, _ = make(threshold=0.35, release=0.15, smooth_seconds=5)
    assert detector.verdict().is_music is False
    feed(detector, clock, [0.0, 0.0, 0.0, 0.0])         # talk
    assert detector.verdict().is_music is False
    feed(detector, clock, [0.9])                        # mean of the last 5 s = 0.18
    assert detector.verdict().is_music is False
    feed(detector, clock, [0.9])                        # mean = 0.36: music
    verdict = detector.verdict()
    assert verdict.is_music is True
    assert verdict.since == clock.now
    assert verdict.music == pytest.approx(0.36)


def test_one_odd_second_does_not_flip_the_decision():
    detector, clock, _, _ = make()
    feed(detector, clock, [0.9] * 8)
    assert detector.verdict().is_music
    opened = detector.verdict().since
    feed(detector, clock, [0.0])                        # a quiet bar, a spoken word
    assert detector.verdict().is_music
    feed(detector, clock, [0.9] * 3)
    assert detector.verdict().since == opened           # never left


def test_closes_only_below_the_lower_threshold():
    detector, clock, _, _ = make(threshold=0.35, release=0.15, smooth_seconds=5)
    feed(detector, clock, [0.9] * 6)
    feed(detector, clock, [0.2] * 5)                    # weak, but above the release level
    assert detector.verdict().is_music
    feed(detector, clock, [0.0] * 2)                    # mean of the last 5 s = 0.12
    verdict = detector.verdict()
    assert verdict.is_music is False and verdict.since == clock.now


def test_song_then_dialog_then_song():
    detector, clock, _, _ = make()
    states = []
    for music, speech in [(0.95, 0.0)] * 10 + [(0.01, 0.97)] * 12 + [(0.9, 0.0)] * 6:
        clock.advance(1.0)
        detector.observe(music, speech)
        states.append(detector.verdict().is_music)
    assert states[:1] == [True]                         # one loud second is enough on its own
    assert all(states[1:10])
    assert states[14:22] == [False] * 8                 # closed within 5 s of the talk starting
    assert states[-4:] == [True] * 4                    # and open again 2 s into the next song
    assert detector.verdict().speech < 0.5


def test_reports_when_the_music_began_if_it_heard_it_start():
    detector, clock, _, _ = make(threshold=0.35, smooth_seconds=5, hop_seconds=1.0)
    feed(detector, clock, [0.0, 0.0, 0.0])              # talk
    feed(detector, clock, [0.9])                        # the first second of a song
    first_music_frame = clock.now
    feed(detector, clock, [0.9])                        # now the gate opens
    verdict = detector.verdict()
    assert verdict.is_music
    assert verdict.began == first_music_frame - 1.0     # that frame covered the second before it
    feed(detector, clock, [0.9] * 20)
    assert detector.verdict().began == first_music_frame - 1.0      # fixed for the whole song
    feed(detector, clock, [0.0] * 6)
    assert detector.verdict().began is None             # gate closed: nothing is playing


def test_music_already_playing_at_start_has_no_known_beginning():
    detector, clock, _, _ = make()
    feed(detector, clock, [0.9])                        # the very first frame is already music
    verdict = detector.verdict()
    assert verdict.is_music and verdict.began is None


def test_old_frames_age_out_of_the_average():
    detector, clock, _, _ = make(smooth_seconds=5)
    feed(detector, clock, [0.9] * 5)
    clock.advance(60)                                   # a long pause in the data
    detector.observe(0.0, 0.0)
    assert detector.verdict().music == 0.0              # only the new frame counts


# --------------------------------------------------------------------- step()

def test_step_classifies_the_latest_second():
    detector, clock, capture, calls = make(scores=[(0.8, 0.1)])
    clock.advance(1)
    detector.step()
    assert calls == [len(capture.window.pcm)]
    assert detector.verdict().music == pytest.approx(0.8)
    assert detector.verdict().fresh


def test_silence_skips_the_model_and_counts_as_no_music():
    detector, clock, capture, calls = make(scores=[(0.9, 0.0)] * 3)
    for _ in range(3):
        clock.advance(1)
        detector.step()
    assert detector.verdict().is_music
    capture.window = AudioWindow(pcm=silence(1.0), sample_rate=16000, rms_dbfs=-80.0)
    for _ in range(6):
        clock.advance(1)
        detector.step()
    assert len(calls) == 3                              # the model was not run on silence
    assert detector.verdict().is_music is False


def test_no_audio_means_no_verdict_rather_than_a_stale_one():
    detector, clock, capture, calls = make(scores=[(0.9, 0.0)] * 5)
    for _ in range(5):
        clock.advance(1)
        detector.step()
    assert detector.verdict().fresh
    capture.healthy = False
    for _ in range(10):
        clock.advance(1)
        detector.step()
    assert len(calls) == 5
    assert detector.verdict().fresh is False            # the service then stops gating on it
    capture.healthy, capture.window = True, None        # healthy but nothing buffered yet
    detector.step()
    assert len(calls) == 5


def test_missing_model_is_reported_not_raised(tmp_path):
    detector = MusicDetector(MusicDetectorConfig(), FakeCapture(),
                             model_path=tmp_path / "nope.tflite")
    assert detector.load() is False
    assert detector.available is False
    assert "not found" in detector.error
    detector.start()                                    # must not start a thread or raise
    detector.step()
    assert detector.verdict().fresh is False


# ------------------------------------------------------------- the real model

def have_model():
    if not BUNDLED_MODEL.is_file():
        return False
    try:
        from nowplaying.musicdetect import _load_interpreter

        _load_interpreter()
        return True
    except ImportError:
        return False


needs_model = pytest.mark.skipif(not have_model(), reason="YAMNet model or runtime missing")


def harmonic_tone(seconds, rate=16000):
    """A steady chord: plainly not speech, and not much like a recording either."""
    count = int(seconds * rate)
    samples = (int(6000 * (math.sin(2 * math.pi * 220 * i / rate)
                           + math.sin(2 * math.pi * 277 * i / rate)
                           + math.sin(2 * math.pi * 330 * i / rate)) / 3) for i in range(count))
    return struct.pack(f"<{count}h", *samples)


@needs_model
def test_real_model_loads_and_scores_are_probabilities():
    from nowplaying.musicdetect import YamnetClassifier

    classifier = YamnetClassifier(BUNDLED_MODEL)
    assert (classifier.music_index, classifier.speech_index) == (132, 0)
    for pcm in (silence(1.0), tone(1.0), harmonic_tone(1.0), tone(0.3)):   # the last is short
        music, speech = classifier(pcm)
        assert 0.0 <= music <= 1.0 and 0.0 <= speech <= 1.0
    quiet_music, quiet_speech = classifier(silence(1.0))
    assert quiet_music < 0.2 and quiet_speech < 0.2
    assert classifier(tone(1.0))[1] < 0.2               # a pure tone is not speech


@needs_model
def test_frames_are_levelled_before_classification():
    """A quiet microphone and a loud one hand the model the same signal level."""
    import numpy as np

    from nowplaying.musicdetect import FRAME_SAMPLES, MAX_GAIN, TARGET_RMS, YamnetClassifier

    classifier = YamnetClassifier(BUNDLED_MODEL)

    def rms(samples):
        return float(np.sqrt(np.mean(samples * samples)))

    loud = classifier.prepare(tone(1.0, amplitude=0.5))
    quiet = classifier.prepare(tone(1.0, amplitude=0.02))
    assert loud.shape == quiet.shape == (FRAME_SAMPLES,)
    assert rms(loud) == pytest.approx(TARGET_RMS, rel=0.02)
    assert rms(quiet) == pytest.approx(TARGET_RMS, rel=0.02)

    faint = tone(1.0, amplitude=0.0005)                  # so faint the gain cap applies
    assert rms(classifier.prepare(faint)) == pytest.approx(0.0005 / math.sqrt(2) * MAX_GAIN,
                                                           rel=0.1)
    assert rms(classifier.prepare(silence(1.0))) == 0.0  # silence is not amplified into noise
    assert np.abs(classifier.prepare(tone(1.0, amplitude=1.0))).max() <= 1.0
    short = classifier.prepare(tone(0.25))
    assert short.shape == (FRAME_SAMPLES,) and rms(short[: FRAME_SAMPLES // 2]) == 0.0

    unlevelled = YamnetClassifier(BUNDLED_MODEL, normalize=False)
    assert rms(unlevelled.prepare(tone(1.0, amplitude=0.02))) == pytest.approx(
        0.02 / math.sqrt(2), rel=0.02)


@needs_model
def test_detector_runs_the_real_model_end_to_end():
    clock = FakeClock()
    capture = FakeCapture()
    capture.window = AudioWindow(pcm=tone(1.0), sample_rate=16000, rms_dbfs=-20.0)
    detector = MusicDetector(MusicDetectorConfig(), capture, clock=clock)
    assert detector.load() and detector.available
    assert len(capture.window.pcm) >= FRAME_BYTES
    for _ in range(3):
        clock.advance(1)
        detector.step()
    verdict = detector.verdict()
    assert verdict.fresh and 0.0 <= verdict.music <= 1.0
