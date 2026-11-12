import time
import wave

import pytest

from nowplaying.capture import (AudioCapture, _redact, block_dbfs, decode_file, mean_dbfs,
                                resolve_ffmpeg)
from nowplaying.config import CaptureConfig

from helpers import FakeClock, silence, tone


def make(clock=None, **overrides):
    cfg = CaptureConfig(**overrides)
    return AudioCapture(cfg, silence_dbfs=-50.0, gap_seconds=1.0, clock=clock or FakeClock())


def have_ffmpeg():
    try:
        resolve_ffmpeg("ffmpeg")
        return True
    except FileNotFoundError:
        return False


needs_ffmpeg = pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not available")


# ------------------------------------------------------------------ level meter

def test_block_dbfs_known_levels():
    assert block_dbfs(tone(0.1, amplitude=0.5)) == pytest.approx(-9.03, abs=0.1)
    assert block_dbfs(tone(0.1, amplitude=0.05)) == pytest.approx(-29.03, abs=0.2)
    assert block_dbfs(silence(0.1)) == -120.0
    assert block_dbfs(b"") == -120.0


def test_block_dbfs_ignores_dc_offset():
    dc = (1000).to_bytes(2, "little", signed=True) * 1600
    assert block_dbfs(dc) == -120.0


def test_mean_dbfs_averages_power_not_decibels():
    assert mean_dbfs([-20.0, -20.0]) == pytest.approx(-20.0)
    assert mean_dbfs([-10.0, -120.0]) == pytest.approx(-13.01, abs=0.01)
    assert mean_dbfs([]) == -120.0


# ------------------------------------------------------------------ ring buffer

def test_ring_keeps_only_the_latest_audio():
    cap = make(buffer_seconds=5)
    cap.feed(tone(4.0, freq=300))
    cap.feed(tone(4.0, freq=900))
    assert cap.seconds_buffered() == pytest.approx(5.0)
    window = cap.snapshot(4.0)
    assert window.duration == pytest.approx(4.0)
    assert window.pcm == tone(4.0, freq=900)


def test_snapshot_needs_a_minimum_and_reports_level():
    cap = make()
    assert cap.snapshot(8.0) is None
    cap.feed(tone(0.5))
    assert cap.snapshot(8.0, minimum=1.0) is None
    cap.feed(tone(2.5))
    window = cap.snapshot(8.0, minimum=1.0)          # less than asked for, but usable
    assert window.duration == pytest.approx(3.0)
    assert window.rms_dbfs == pytest.approx(-13.5, abs=0.3)
    assert window.sample_rate == 16000


def test_odd_sized_chunks_give_identical_audio():
    whole, pieces = make(), make()
    data = tone(3.0, freq=523.25)
    whole.feed(data)
    for start in range(0, len(data), 777):            # odd sizes split samples in half
        pieces.feed(data[start:start + 777])
    assert pieces.snapshot(3.0).pcm == whole.snapshot(3.0).pcm


# ------------------------------------------------------------- silence tracking

def test_trailing_silence_and_onset():
    clock = FakeClock()
    cap = make(clock)
    cap.feed(tone(2.0))
    assert cap.trailing_silence() == 0.0
    assert cap.last_onset_at is None                 # no silence came before it
    cap.feed(silence(3.0))
    assert cap.trailing_silence() == pytest.approx(3.0)
    clock.advance(10)
    cap.feed(tone(1.0))
    assert cap.trailing_silence() == 0.0
    assert cap.last_onset_at == clock.now


def test_short_gap_is_not_an_onset():
    cap = make()
    cap.feed(tone(2.0))
    cap.feed(silence(0.5))                           # shorter than gap_seconds
    cap.feed(tone(1.0))
    assert cap.last_onset_at is None


def test_click_does_not_interrupt_silence():
    cap = make()
    cap.feed(tone(2.0))
    cap.feed(silence(2.0))
    cap.feed(tone(0.1, amplitude=0.8))               # a pop
    cap.feed(silence(2.0))
    assert cap.trailing_silence() == pytest.approx(4.1)
    assert cap.last_onset_at is None


def test_quiet_noise_counts_as_silence():
    cap = make()
    cap.feed(tone(3.0, amplitude=0.001))             # about -63 dBFS, below the -50 gate
    assert cap.trailing_silence() == pytest.approx(3.0)
    assert cap.level_dbfs(1.0) < -55


def test_health_follows_data_arrival():
    clock = FakeClock()
    cap = make(clock)
    assert cap.healthy is False
    cap.feed(tone(0.2))
    assert cap.healthy is True
    clock.advance(5)
    assert cap.healthy is False


# ---------------------------------------------------------------------- ffmpeg

def test_redact_hides_stream_credentials():
    cmd = ["ffmpeg", "-i", "rtsp://admin:hunter2@10.0.0.9:554/cam/realmonitor?channel=1"]
    shown = " ".join(_redact(cmd))
    assert "hunter2" not in shown and "admin" not in shown
    assert "10.0.0.9:554" in shown


@needs_ffmpeg
def test_build_command_layout():
    cap = make(format="alsa", input="plughw:CARD=Device", input_args=["-thread_queue_size", "64"])
    cmd = cap.build_command()
    assert cmd[cmd.index("-f") + 1] == "alsa"
    assert cmd[cmd.index("-i") + 1] == "plughw:CARD=Device"
    assert cmd.index("-thread_queue_size") < cmd.index("-i")
    assert cmd[-1] == "pipe:1" and "s16le" in cmd and "16000" in cmd
    no_format = make(format="", input="rtsp://x/y").build_command()
    assert no_format[no_format.index("-i") - 1] != ""          # no empty "-f" pair
    assert no_format.count("-f") == 1                          # only the output format


def write_wav(path, pcm, rate=16000):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)


@needs_ffmpeg
def test_decode_file_with_offset_and_length(tmp_path):
    path = tmp_path / "clip.wav"
    write_wav(path, silence(2.0) + tone(4.0))
    whole = decode_file(path, CaptureConfig())
    assert whole.duration == pytest.approx(6.0, abs=0.05)
    part = decode_file(path, CaptureConfig(), offset=3.0, length=2.0)
    assert part.duration == pytest.approx(2.0, abs=0.05)
    assert part.rms_dbfs == pytest.approx(-13.5, abs=0.5)      # all tone, no silence


@needs_ffmpeg
def test_decode_file_reports_bad_input(tmp_path):
    bad = tmp_path / "not-audio.wav"
    bad.write_bytes(b"this is not audio")
    with pytest.raises(RuntimeError, match="could not decode"):
        decode_file(bad, CaptureConfig())


@needs_ffmpeg
@pytest.mark.parametrize("sample_format", ["s16le", "s32le"])
def test_real_ffmpeg_receives_raw_pcm_over_udp(sample_format):
    """The transport an ESP32 microphone uses: 16 kHz mono PCM in UDP datagrams.

    An I2S MEMS microphone such as the INMP441 delivers 32-bit samples, so that
    format must arrive at the same level as plain 16-bit audio.
    """
    import socket
    import struct
    import threading

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    cfg = CaptureConfig(format=sample_format,
                        input=f"udp://127.0.0.1:{port}?overrun_nonfatal=1",
                        input_args=["-ar", "16000", "-ac", "1"], stall_seconds=0)
    cap = AudioCapture(cfg, silence_dbfs=-50.0, gap_seconds=1.0)
    cap.start()
    stop = threading.Event()

    def send():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        pcm = tone(0.032, amplitude=0.3)                 # 512 samples, 32 ms
        if sample_format == "s32le":                     # the same audio, left-aligned in 32 bits
            values = struct.unpack("<512h", pcm)
            pcm = struct.pack("<512i", *(value << 16 for value in values))
        start, sent = time.monotonic(), 0
        while not stop.is_set():
            sock.sendto(pcm, ("127.0.0.1", port))
            sent += 1
            time.sleep(max(0.0, start + sent * 0.032 - time.monotonic()))
        sock.close()

    time.sleep(1.0)                                      # let ffmpeg open its listening socket
    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and cap.seconds_buffered() < 2.0:
            time.sleep(0.1)
        assert cap.seconds_buffered() >= 2.0
        assert cap.healthy
        assert cap.level_dbfs(1.0) == pytest.approx(-13.5, abs=1.0)
        stop.set()
        sender.join(timeout=2)
        time.sleep(3.5)
        assert not cap.healthy                           # the sender is off...
        assert cap.restarts == 0                         # ...and ffmpeg is still listening
    finally:
        stop.set()
        cap.stop()


@needs_ffmpeg
def test_real_ffmpeg_stream_fills_the_buffer(tmp_path):
    """End to end: a real ffmpeg child process, read in real time, restarted at EOF."""
    path = tmp_path / "loop.wav"
    write_wav(path, tone(3.0, amplitude=0.4), rate=44100)       # resampled to 16 kHz
    cfg = CaptureConfig(format="", input=str(path), input_args=["-re"], buffer_seconds=20)
    cap = AudioCapture(cfg, silence_dbfs=-50.0, gap_seconds=1.0)
    cap.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and cap.seconds_buffered() < 2.0:
            time.sleep(0.1)
        assert cap.seconds_buffered() >= 2.0
        assert cap.healthy
        window = cap.snapshot(2.0)
        assert window.rms_dbfs == pytest.approx(-11.0, abs=1.0)
        assert cap.trailing_silence() == 0.0
    finally:
        cap.stop()
    assert cap.healthy or cap.seconds_since_data() is not None
