"""Measure the music detector on real music and talk streams. Run by hand, not by pytest.

For each stream it captures audio with the daemon's own capture code, runs the
YAMNet classifier once a second, and reports how often the detector's verdict
(the smoothed Music score against the on/off thresholds) is right. Every stream
is then scored again as a microphone across the room would hear it: with
reverberation and with room noise at several signal-to-noise ratios.

    python tests/live_music_detector.py --seconds 60
    python tests/live_music_detector.py --cache /tmp/streams   # reuse captured audio
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nowplaying.capture import AudioCapture  # noqa: E402
from nowplaying.config import CaptureConfig, MusicDetectorConfig  # noqa: E402
from nowplaying.musicdetect import BUNDLED_MODEL, FRAME_SAMPLES, YamnetClassifier  # noqa: E402

RATE = 16000
SOMA = "http://ice1.somafm.com/{}-128-mp3"
STREAMS = [
    # (key, label, expected, url)
    ("seventies", "70s rock, vocals", "music", SOMA.format("seventies")),
    ("indiepop", "indie pop, vocals", "music", SOMA.format("indiepop")),
    ("groovesalad", "downtempo electronic", "music", SOMA.format("groovesalad")),
    ("sonicuniverse", "jazz", "music", SOMA.format("sonicuniverse")),
    ("metal", "metal", "music", SOMA.format("metal")),
    ("bootliquor", "americana / country", "music", SOMA.format("bootliquor")),
    ("7soul", "soul 45s", "music", SOMA.format("7soul")),
    ("u80s", "80s new wave", "music", SOMA.format("u80s")),
    ("dronezone", "ambient drones", "music", SOMA.format("dronezone")),
    ("wqxr", "classical (WQXR)", "music", "http://stream.wqxr.org/wqxr"),
    # Commercial stations mix songs with ad breaks, DJ talk and music beds, so a
    # whole-stream label would be wrong. They are shown second by second instead
    # and left out of the accuracy table.
    ("hiphop", "hip-hop / R&B (181.fm)", "mixed", "http://listen.181fm.com/181-beat_128k.mp3"),
    ("oldschool", "old-school rap (181.fm)", "mixed",
     "http://listen.181fm.com/181-oldschool_128k.mp3"),
    ("comedy", "stand-up comedy (181.fm)", "mixed",
     "http://listen.181fm.com/181-comedy_128k.mp3"),
    ("npr", "NPR news/talk", "speech", "http://npr-ice.streamguys1.com/live.mp3"),
    ("kqed", "KQED talk", "speech", "http://streams.kqed.org/kqedradio"),
    ("bbcws", "BBC World Service", "speech",
     "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service"),
    ("wamu", "WAMU talk", "speech", "http://wamu-1.streamguys.com/wamu-1"),
    ("kcrw", "KCRW news", "speech", "http://kcrw.streamguys1.com/kcrw_192k_mp3_news"),
]
CONDITIONS = [("clean stream", None, None), ("room, SNR 30 dB", 0.5, 30.0),
              ("room, SNR 20 dB", 0.5, 20.0), ("room, SNR 15 dB", 0.5, 15.0),
              ("room, SNR 10 dB", 0.5, 10.0), ("room, SNR 5 dB", 0.5, 5.0)]
THRESHOLDS = [(0.5, 0.25), (0.35, 0.15), (0.25, 0.1)]


def collect(url: str, seconds: float, out: dict, key: str) -> None:
    capture = AudioCapture(CaptureConfig(format="", input=url,
                                         buffer_seconds=int(seconds) + 5))
    capture.start()
    deadline = time.monotonic() + seconds + 25
    while time.monotonic() < deadline and capture.seconds_buffered() < seconds:
        time.sleep(0.5)
    window = capture.snapshot(seconds, minimum=10.0)
    capture.stop()
    out[key] = window.pcm if window else b""


def room(samples: np.ndarray, rt60: float, snr_db: float, seed: int = 1) -> np.ndarray:
    """What a microphone across a room hears: reverberation plus low-pitched room noise."""
    rng = np.random.default_rng(seed)
    # reverb: a direct path followed by an exponentially decaying diffuse tail
    length = int(rt60 * RATE)
    tail = rng.standard_normal(length) * np.exp(-6.9 * np.arange(length) / length)
    impulse = np.concatenate([[1.0], 0.35 * tail / np.sqrt(np.sum(tail ** 2))])
    size = samples.size + impulse.size - 1
    wet = np.fft.irfft(np.fft.rfft(samples, size) * np.fft.rfft(impulse, size), size)
    wet = wet[:samples.size]
    # noise: pink-ish (more rumble than hiss), set relative to the signal level
    spectrum = np.fft.rfft(rng.standard_normal(samples.size))
    spectrum /= np.sqrt(np.maximum(1.0, np.arange(spectrum.size)))
    noise = np.fft.irfft(spectrum, samples.size)
    noise *= np.sqrt(np.mean(wet ** 2)) / np.sqrt(np.mean(noise ** 2)) * 10 ** (-snr_db / 20)
    return (wet + noise).astype(np.float32)


def to_pcm(samples: np.ndarray, level_dbfs: float = -35.0) -> bytes:
    """Scale to a typical quiet microphone level and convert to 16-bit PCM."""
    rms = float(np.sqrt(np.mean(samples ** 2))) or 1.0
    scaled = samples * (10 ** (level_dbfs / 20) / rms)
    return (np.clip(scaled, -1, 1) * 32767).astype("<i2").tobytes()


def frame_scores(classifier: YamnetClassifier, pcm: bytes) -> tuple[np.ndarray, np.ndarray]:
    frame_bytes, step = FRAME_SAMPLES * 2, RATE * 2       # one frame per second
    scores = [classifier(pcm[start:start + frame_bytes])
              for start in range(0, len(pcm) - frame_bytes + 1, step)]
    return np.array([s[0] for s in scores]), np.array([s[1] for s in scores])


def gate(music: np.ndarray, on: float, off: float, width: int) -> tuple[np.ndarray, np.ndarray]:
    """Smoothed score and the open/closed decision per second."""
    smooth = np.array([music[max(0, i - width + 1):i + 1].mean() for i in range(music.size)])
    opened, state = [], False
    for value in smooth:
        if not state and value >= on:
            state = True
        elif state and value <= off:
            state = False
        opened.append(state)
    return smooth, np.array(opened)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--cache", help="directory to keep the captured audio in for re-runs")
    args = parser.parse_args()
    cfg = MusicDetectorConfig()
    width = max(1, round(cfg.smooth_seconds / cfg.hop_seconds))
    classifier = YamnetClassifier(BUNDLED_MODEL)

    audio: dict[str, bytes] = {}
    cache = Path(args.cache) if args.cache else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        for key, *_ in STREAMS:
            if (cache / f"{key}.pcm").is_file():
                audio[key] = (cache / f"{key}.pcm").read_bytes()
    missing = [(key, url) for key, _, _, url in STREAMS if key not in audio]
    threads = [threading.Thread(target=collect, args=(url, args.seconds, audio, key))
               for key, url in missing]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if cache:
        for key, _ in missing:
            if audio.get(key):
                (cache / f"{key}.pcm").write_bytes(audio[key])

    usable = [(key, label, expected) for key, label, expected, _ in STREAMS
              if len(audio.get(key, b"")) >= RATE * 2 * 20]
    for key, label, expected, _ in STREAMS:
        if (key, label, expected) not in usable:
            print(f"(stream unavailable: {label})")

    # ---- per-stream scores on the clean audio ---------------------------------
    print(f"\n{'stream':24s} {'is':7s} {'secs':>4s}  {'Music':>5s} {'Speech':>6s}   "
          f"smoothed Music: {'min':>5s} {'max':>5s}")
    scores: dict[tuple[str, str], np.ndarray] = {}
    timelines: list[str] = []
    for key, label, expected in usable:
        samples = np.frombuffer(audio[key], dtype="<i2").astype(np.float32) / 32768.0
        for name, rt60, snr in CONDITIONS:
            if expected == "mixed" and rt60 is not None:
                continue
            heard = samples if rt60 is None else room(samples, rt60, snr)
            music, speech = frame_scores(classifier, to_pcm(heard))
            scores[(key, name)] = music
            if rt60 is None:
                smooth, _ = gate(music, 1.0, 0.0, width)
                print(f"{label:24s} {expected:7s} {music.size:4d}  {music.mean():5.2f} "
                      f"{speech.mean():6.2f}   {'':15s} {smooth[width:].min():5.2f} "
                      f"{smooth[width:].max():5.2f}")
            if rt60 is None and expected == "mixed":
                _, opened = gate(music, cfg.threshold, cfg.release, width)
                tenths = lambda values: "".join(str(min(9, int(v * 10))) for v in values)  # noqa: E731
                timelines.append(f"{label}\n  music  {tenths(music)}\n  speech {tenths(speech)}\n"
                                 f"  gate   {''.join('M' if o else '.' for o in opened)}")
    if timelines:
        print("\nmixed stations, one character per second (scores in tenths, M = gate open):")
        print("\n".join(timelines))
    usable = [item for item in usable if item[2] != "mixed"]

    # ---- gate accuracy by condition and threshold pair -------------------------
    print(f"\nseconds judged correctly (music: gate open; talk: gate closed), "
          f"{cfg.smooth_seconds:g} s smoothing")
    header = "".join(f"   on {on:.2f}/off {off:.2f}".ljust(24) for on, off in THRESHOLDS)
    print(f"{'condition':18s}{header}")
    print(f"{'':18s}" + "".join(f"   {'music':>7s} {'talk':>7s}".ljust(24) for _ in THRESHOLDS))
    for name, _, _ in CONDITIONS:
        cells = []
        for on, off in THRESHOLDS:
            good = {"music": [0, 0], "speech": [0, 0]}
            for key, _, expected in usable:
                _, opened = gate(scores[(key, name)], on, off, width)
                judged = opened[width:]                 # skip the smoothing warm-up
                right = judged if expected == "music" else ~judged
                good[expected][0] += int(right.sum())
                good[expected][1] += int(right.size)
            music_pct = good["music"][0] / max(1, good["music"][1]) * 100
            talk_pct = good["speech"][0] / max(1, good["speech"][1]) * 100
            cells.append(f"   {music_pct:6.1f}% {talk_pct:6.1f}%".ljust(24))
        print(f"{name:18s}" + "".join(cells))
    total = sum(scores[(key, 'clean stream')].size for key, _, _ in usable)
    print(f"\n{len(usable)} streams, {total} seconds of audio per condition")
    return 0


if __name__ == "__main__":
    sys.exit(main())
