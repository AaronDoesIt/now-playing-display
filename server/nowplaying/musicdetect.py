"""Local music detector: is the room hearing a song, or just talk?

The daemon listens all the time, but a recognition request is only worth making
when music is playing. This module decides that on the machine, with no network
call: once a second it runs YAMNet (Google's small AudioSet classifier, a 4 MB
TensorFlow Lite model) on the latest second of audio and reads its "Music" and
"Speech" scores. The Music score is smoothed over a few seconds and compared
with two thresholds (one to open the gate, a lower one to close it), so a
single odd second does not flip the decision.

Dialog, news, a phone call or kitchen noise therefore cost nothing: no lookup is
sent until the smoothed Music score says a song is playing.
"""

from __future__ import annotations

import logging
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import MusicDetectorConfig

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_SAMPLES = 15600               # YAMNet's input: 0.975 s at 16 kHz
FRAME_BYTES = FRAME_SAMPLES * 2
BUNDLED_MODEL = Path(__file__).resolve().parent.parent / "models" / "yamnet.tflite"
TARGET_RMS = 0.1                    # frames are levelled to about -20 dBFS...
MAX_GAIN = 40.0                     # ...with at most +32 dB of gain

#: classify(pcm) -> (music score, speech score), each 0..1
Classifier = Callable[[bytes], tuple[float, float]]


def _load_interpreter() -> Any:
    """Find a TensorFlow Lite interpreter: LiteRT, the older tflite-runtime, or TensorFlow."""
    try:
        from ai_edge_litert.interpreter import Interpreter

        return Interpreter
    except ImportError:
        pass
    try:
        from tflite_runtime.interpreter import Interpreter  # type: ignore[no-redef]

        return Interpreter
    except ImportError:
        pass
    try:
        from tensorflow.lite import Interpreter  # type: ignore[no-redef]

        return Interpreter
    except ImportError:
        raise ImportError("no TensorFlow Lite runtime found; pip install ai-edge-litert") from None


def _read_labels(model_path: Path) -> list[str]:
    """The class names packed inside the model file (it doubles as a zip archive)."""
    try:
        with zipfile.ZipFile(model_path) as archive:
            for name in archive.namelist():
                if name.endswith(".txt"):
                    return archive.read(name).decode("utf-8").splitlines()
    except (zipfile.BadZipFile, OSError):
        pass
    return []


class YamnetClassifier:
    """YAMNet on one 0.975 s frame of 16 kHz mono audio."""

    def __init__(self, model_path: Path, threads: int = 1, normalize: bool = True) -> None:
        import numpy as np

        self._np = np
        self._normalize = normalize
        interpreter = _load_interpreter()(model_path=str(model_path), num_threads=threads)
        interpreter.allocate_tensors()
        inputs = interpreter.get_input_details()[0]
        outputs = interpreter.get_output_details()[0]
        self._input_shape = tuple(int(n) for n in inputs["shape"])
        if int(np.prod(self._input_shape)) != FRAME_SAMPLES:
            raise ValueError(f"{model_path.name}: expected an input of {FRAME_SAMPLES} samples, "
                             f"found shape {self._input_shape}")
        self._input_index = inputs["index"]
        self._output_index = outputs["index"]
        self._interpreter = interpreter
        labels = _read_labels(model_path)
        self.music_index = labels.index("Music") if "Music" in labels else 132
        self.speech_index = labels.index("Speech") if "Speech" in labels else 0
        self._lock = threading.Lock()

    def prepare(self, pcm: bytes) -> Any:
        """The model's input: the last 0.975 s as float samples, levelled."""
        np = self._np
        samples = np.frombuffer(pcm[-FRAME_BYTES:], dtype="<i2").astype(np.float32) / 32768.0
        if samples.size < FRAME_SAMPLES:
            samples = np.concatenate([np.zeros(FRAME_SAMPLES - samples.size, np.float32), samples])
        if self._normalize:
            # A far-field microphone delivers quiet audio; the model was trained on
            # normally loud clips, so bring each frame up to a common level first.
            rms = float(np.sqrt(np.mean(samples * samples)))
            if rms > 1e-5:
                samples = np.clip(samples * min(MAX_GAIN, TARGET_RMS / rms), -1.0, 1.0)
        return samples.astype(np.float32)

    def __call__(self, pcm: bytes) -> tuple[float, float]:
        samples = self.prepare(pcm)
        with self._lock:
            self._interpreter.set_tensor(self._input_index, samples.reshape(self._input_shape))
            self._interpreter.invoke()
            scores = self._interpreter.get_tensor(self._output_index).reshape(-1)
            return float(scores[self.music_index]), float(scores[self.speech_index])


@dataclass(frozen=True)
class Verdict:
    is_music: bool
    music: float        # smoothed Music score, 0..1
    speech: float       # smoothed Speech score, 0..1
    since: float        # clock time at which is_music last changed
    fresh: bool         # False when no audio has been classified recently
    #: when the music that is playing started, if the detector heard it start;
    #: None when it was already playing (for example when the daemon started)
    began: float | None = None


class MusicDetector:
    """Classifies the capture once per hop and keeps a debounced "music is playing" flag."""

    def __init__(
        self,
        cfg: MusicDetectorConfig,
        capture: Any,
        silence_dbfs: float = -50.0,
        classifier: Classifier | None = None,
        clock: Callable[[], float] = time.monotonic,
        model_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.model_path = model_path or BUNDLED_MODEL
        self._capture = capture
        self._silence_dbfs = silence_dbfs
        self._clock = clock
        self._classifier = classifier
        self._frames: deque[tuple[float, float, float]] = deque()
        self._lock = threading.Lock()
        self._is_music = False
        self._since = clock()
        self._began: float | None = None
        self._music = 0.0
        self._speech = 0.0
        self._last_frame_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""

    # ---------------------------------------------------------------- lifecycle

    def load(self) -> bool:
        """Load the model. On failure the reason is kept in ``error`` and False returned."""
        if self._classifier is not None:
            return True
        path = self.model_path
        try:
            if not path.is_file():
                raise FileNotFoundError(f"model file not found: {path}")
            self._classifier = YamnetClassifier(path, threads=self.cfg.threads)
            log.info("music detector ready (%s)", path.name)
            return True
        except Exception as exc:
            self.error = f"{exc.__class__.__name__}: {exc}"
            log.error("music detector unavailable (%s); every sound will be looked up", self.error)
            return False

    @property
    def available(self) -> bool:
        return self._classifier is not None

    def start(self) -> None:
        if not self.load() or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="music-detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.cfg.hop_seconds):
            try:
                self.step()
            except Exception:
                log.exception("music detector step failed")

    # ------------------------------------------------------------------- logic

    def step(self) -> None:
        """Classify the most recent second of audio and update the verdict."""
        if self._classifier is None or not self._capture.healthy:
            return
        window = self._capture.snapshot(1.0, minimum=1.0)
        if window is None:
            return
        if window.rms_dbfs < self._silence_dbfs:
            music, speech = 0.0, 0.0            # silence is not music; skip the model
        else:
            music, speech = self._classifier(window.pcm)
        self.observe(music, speech)

    def observe(self, music: float, speech: float) -> None:
        """Feed one frame's scores (called by step; public for tests)."""
        now = self._clock()
        cfg = self.cfg
        with self._lock:
            self._last_frame_at = now
            self._frames.append((now, music, speech))
            horizon = now - cfg.smooth_seconds
            while self._frames and self._frames[0][0] <= horizon:
                self._frames.popleft()
            count = len(self._frames)
            self._music = sum(f[1] for f in self._frames) / count
            self._speech = sum(f[2] for f in self._frames) / count
            if not self._is_music and self._music >= cfg.threshold:
                self._is_music, self._since = True, now
                self._began = self._music_start()
                log.info("music detected (score %.2f)", self._music)
            elif self._is_music and self._music <= cfg.release:
                self._is_music, self._since = False, now
                self._began = None
                log.info("music stopped (score %.2f, speech %.2f)", self._music, self._speech)

    def _music_start(self) -> float | None:
        """When the music began, judged from the frames still in the smoothing window.

        If something that was not music was heard just before the first musical
        frame, the music started at that frame. If every frame so far is music,
        it was already playing when listening began and its start is unknown.
        """
        threshold = self.cfg.threshold
        heard_other = False
        for when, music, _ in self._frames:
            if music >= threshold:
                return when - self.cfg.hop_seconds if heard_other else None
            heard_other = True
        return None

    def verdict(self) -> Verdict:
        now = self._clock()
        with self._lock:
            fresh = (self._last_frame_at is not None
                     and now - self._last_frame_at <= max(5.0, 3 * self.cfg.hop_seconds))
            return Verdict(self._is_music, self._music, self._speech, self._since, fresh,
                           self._began)
