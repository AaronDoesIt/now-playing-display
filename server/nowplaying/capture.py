"""Audio capture: one long-running ffmpeg process feeding a ring buffer.

ffmpeg does the device/stream handling, so the same code captures from ALSA,
PulseAudio, RTSP, UDP or a file. Keeping the stream open (instead of recording
a fresh clip per lookup) means a lookup never waits for a device or an RTSP
handshake, silence is detected without spending an API call, and a retry can
use a longer window of audio that is already in memory.
"""

from __future__ import annotations

import logging
import math
import operator
import shutil
import subprocess
import sys
import threading
import time
from array import array
from collections import deque
from pathlib import Path
from typing import Callable

from .config import CaptureConfig
from .models import AudioWindow

log = logging.getLogger(__name__)

BLOCK_SECONDS = 0.1   # level is measured in blocks of this length
ONSET_BLOCKS = 3      # this many loud blocks in a row count as "sound" (ignores clicks)
MIN_DB = -120.0
BYTES_PER_SAMPLE = 2


def resolve_ffmpeg(configured: str) -> str:
    """Return a usable ffmpeg path, or raise FileNotFoundError with a hint."""
    found = shutil.which(configured)
    if found:
        return found
    if Path(configured).is_file():
        return configured
    try:  # development convenience: the imageio-ffmpeg wheel ships a static binary
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise FileNotFoundError(
        f"ffmpeg not found (looked for {configured!r}). Install it "
        "(Debian: sudo apt install ffmpeg) or set capture.ffmpeg_path."
    )


def block_dbfs(block: bytes) -> float:
    """RMS level of s16le mono PCM in dB relative to full scale, DC offset removed."""
    samples = array("h")
    samples.frombytes(block[: len(block) - len(block) % BYTES_PER_SAMPLE])
    if sys.byteorder == "big":
        samples.byteswap()
    count = len(samples)
    if not count:
        return MIN_DB
    mean = sum(samples) / count
    power = sum(map(operator.mul, samples, samples)) / count - mean * mean
    if power <= 0:
        return MIN_DB
    return max(MIN_DB, 10.0 * math.log10(power / (32768.0 * 32768.0)))


def mean_dbfs(levels: list[float]) -> float:
    """Average a list of per-block dB values in the power domain."""
    if not levels:
        return MIN_DB
    power = sum(10.0 ** (db / 10.0) for db in levels) / len(levels)
    return max(MIN_DB, 10.0 * math.log10(power)) if power > 0 else MIN_DB


class _Ring:
    """Keeps the most recent *capacity* bytes."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._chunks: deque[bytes] = deque()
        self._size = 0

    def __len__(self) -> int:
        return min(self._size, self._capacity)

    def append(self, data: bytes) -> None:
        self._chunks.append(data)
        self._size += len(data)
        while self._chunks and self._size - len(self._chunks[0]) >= self._capacity:
            self._size -= len(self._chunks.popleft())

    def tail(self, count: int) -> bytes:
        parts: list[bytes] = []
        have = 0
        for chunk in reversed(self._chunks):
            parts.append(chunk)
            have += len(chunk)
            if have >= count:
                break
        data = b"".join(reversed(parts))
        return data[-count:] if count < len(data) else data

    def clear(self) -> None:
        self._chunks.clear()
        self._size = 0


class AudioCapture:
    """Continuously captured audio with a level meter and silence tracking."""

    def __init__(
        self,
        cfg: CaptureConfig,
        silence_dbfs: float = -50.0,
        gap_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.silence_dbfs = silence_dbfs
        self.gap_seconds = gap_seconds
        self._clock = clock
        self._block_bytes = round(cfg.sample_rate * BLOCK_SECONDS) * BYTES_PER_SAMPLE
        blocks = round(cfg.buffer_seconds / BLOCK_SECONDS)
        self._ring = _Ring(blocks * self._block_bytes)
        self._levels: deque[float] = deque(maxlen=blocks)
        self._pending = b""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._silent_blocks = 0
        self._loud_run = 0
        self._last_data_at: float | None = None
        #: when sound last resumed after at least gap_seconds of silence
        self.last_onset_at: float | None = None
        self.restarts = 0

    # ------------------------------------------------------------------ process

    def build_command(self) -> list[str]:
        cfg = self.cfg
        cmd = [resolve_ffmpeg(cfg.ffmpeg_path), "-hide_banner", "-nostdin",
               "-loglevel", "warning"]
        cmd += cfg.input_args
        if cfg.format:
            cmd += ["-f", cfg.format]
        cmd += [
            "-i", cfg.input,
            "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", str(cfg.sample_rate),
            "-acodec", "pcm_s16le", "-f", "s16le",
            "-flush_packets", "1",
            "pipe:1",
        ]
        return cmd

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def restart(self) -> None:
        """Kill ffmpeg; the reader loop starts a new one (used when a stream stalls)."""
        log.warning("restarting audio capture")
        self._kill()

    def _kill(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                cmd = self.build_command()
                log.info("starting capture: %s", subprocess.list2cmdline(_redact(cmd)))
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, bufsize=0,
                )
            except (OSError, FileNotFoundError) as exc:
                log.error("cannot start ffmpeg: %s", exc)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self._proc = proc
            threading.Thread(target=self._drain_stderr, args=(proc,),
                             name="capture-stderr", daemon=True).start()
            started = self._clock()
            received = 0
            assert proc.stdout is not None
            while not self._stop.is_set():
                data = proc.stdout.read(self._block_bytes)
                if not data:
                    break
                received += len(data)
                self.feed(data)
            self._kill()
            code = proc.poll()
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass
            if self._stop.is_set():
                break
            if received and self._clock() - started > 20:
                backoff = 1.0
            self.restarts += 1
            log.warning("ffmpeg ended (exit %s, %d bytes); restarting in %.0f s",
                        code, received, backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        shown = 0
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            shown += 1
            if shown <= 20:
                log.warning("ffmpeg: %s", line)
            elif shown == 21:
                log.warning("ffmpeg: further messages suppressed until it restarts")

    # --------------------------------------------------------------------- data

    def feed(self, data: bytes) -> None:
        """Add raw PCM. Called by the reader thread (and directly by tests)."""
        now = self._clock()
        with self._lock:
            self._last_data_at = now
            data = self._pending + data
            usable = len(data) - len(data) % self._block_bytes
            self._pending = data[usable:]
            for start in range(0, usable, self._block_bytes):
                block = data[start:start + self._block_bytes]
                self._ring.append(block)
                self._note_level(block_dbfs(block), now)

    def _note_level(self, db: float, now: float) -> None:
        self._levels.append(db)
        if db >= self.silence_dbfs:
            self._loud_run += 1
            if self._loud_run >= ONSET_BLOCKS:
                if self._silent_blocks * BLOCK_SECONDS >= self.gap_seconds:
                    self.last_onset_at = now
                self._silent_blocks = 0
        else:
            if self._loud_run < ONSET_BLOCKS:
                # a click shorter than ONSET_BLOCKS does not interrupt the silence
                self._silent_blocks += self._loud_run
            self._loud_run = 0
            self._silent_blocks += 1

    def snapshot(self, seconds: float, minimum: float = 1.0) -> AudioWindow | None:
        """The most recent *seconds* of audio, or None if less than *minimum* is buffered."""
        with self._lock:
            want = round(seconds / BLOCK_SECONDS) * self._block_bytes
            have = len(self._ring)
            if have < round(minimum / BLOCK_SECONDS) * self._block_bytes:
                return None
            take = min(want, have)
            pcm = self._ring.tail(take)
            blocks = take // self._block_bytes
            levels = list(self._levels)[-blocks:] if blocks else []
        return AudioWindow(pcm=pcm, sample_rate=self.cfg.sample_rate,
                           rms_dbfs=mean_dbfs(levels))

    def level_dbfs(self, seconds: float = 1.0) -> float:
        with self._lock:
            count = max(1, round(seconds / BLOCK_SECONDS))
            return mean_dbfs(list(self._levels)[-count:])

    def trailing_silence(self) -> float:
        """Seconds of uninterrupted silence at the end of the buffer."""
        with self._lock:
            return self._silent_blocks * BLOCK_SECONDS

    def seconds_buffered(self) -> float:
        with self._lock:
            return len(self._ring) / (self.cfg.sample_rate * BYTES_PER_SAMPLE)

    def seconds_since_data(self) -> float | None:
        with self._lock:
            if self._last_data_at is None:
                return None
            return self._clock() - self._last_data_at

    @property
    def healthy(self) -> bool:
        """True while audio is arriving."""
        age = self.seconds_since_data()
        return age is not None and age < 3.0


def decode_file(path: str | Path, cfg: CaptureConfig, offset: float = 0.0,
                length: float | None = None) -> AudioWindow:
    """Decode (part of) an audio file to an AudioWindow with ffmpeg."""
    cmd = [resolve_ffmpeg(cfg.ffmpeg_path), "-hide_banner", "-nostdin", "-loglevel", "error"]
    if offset > 0:
        cmd += ["-ss", f"{offset:.3f}"]
    if length:
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(cfg.sample_rate),
            "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"]
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip()[-400:]
        raise RuntimeError(f"ffmpeg could not decode {path}: {detail or 'no audio produced'}")
    pcm = result.stdout[: len(result.stdout) - len(result.stdout) % BYTES_PER_SAMPLE]
    block = round(cfg.sample_rate * BLOCK_SECONDS) * BYTES_PER_SAMPLE
    levels = [block_dbfs(pcm[i:i + block]) for i in range(0, len(pcm) - block + 1, block)]
    return AudioWindow(pcm=pcm, sample_rate=cfg.sample_rate,
                       rms_dbfs=mean_dbfs(levels) if levels else block_dbfs(pcm))


def _redact(cmd: list[str]) -> list[str]:
    """Hide credentials embedded in stream URLs before logging a command line."""
    out = []
    for arg in cmd:
        if "://" in arg and "@" in arg:
            scheme, rest = arg.split("://", 1)
            arg = f"{scheme}://***@{rest.rsplit('@', 1)[1]}"
        out.append(arg)
    return out
