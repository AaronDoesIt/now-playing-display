#!/usr/bin/env python3
"""Now Playing: identify the music in the room and publish it for a 480x480 display.

  audio_recognition_daemon.py -c config.toml run        the daemon (default command)
  audio_recognition_daemon.py -c config.toml doctor     check the installation
  audio_recognition_daemon.py -c config.toml levels     live input level and music/speech scores
  audio_recognition_daemon.py identify song.mp3 --offset 60
                                                         run the recognizers on a file
  audio_recognition_daemon.py -c config.toml simulate --artist "..." --title "..."
                                                         publish a track without any audio

See README.md for the architecture and config.example.toml for every option.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from nowplaying import __version__
from nowplaying.config import Config, ConfigError, load_config

log = logging.getLogger("nowplaying")


def setup_logging(level: str) -> None:
    # journald adds its own timestamps; a terminal does not
    fmt = "%(levelname)s %(name)s: %(message)s" if os.environ.get("JOURNAL_STREAM") \
        else "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt)
    for noisy in ("urllib3", "PIL", "asyncio", "aiohttp_retry", "shazamio_core", "shazamio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load(args: argparse.Namespace, required: bool) -> Config:
    path = args.config or os.environ.get("NOWPLAYING_CONFIG")
    if not path and Path("config.toml").is_file():
        path = "config.toml"
    if not path and required:
        raise ConfigError("no config file: copy config.example.toml to config.toml "
                          "or pass -c /path/to/config.toml")
    cfg = load_config(path)
    setup_logging("DEBUG" if args.verbose else cfg.general.log_level)
    return cfg


def install_signal_handlers(stop) -> None:
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, lambda *_: stop())
            except (ValueError, OSError):       # not the main thread, or unsupported
                pass


# ---------------------------------------------------------------------- commands

def cmd_run(args: argparse.Namespace) -> int:
    from nowplaying.app import build_service

    cfg = load(args, required=True)
    log.info("now-playing %s starting (instance %s, recognizers: %s)", __version__,
             cfg.general.instance, ", ".join(cfg.recognizers.order))
    service = build_service(cfg)
    install_signal_handlers(service.stop)
    service.run()
    return 0


def cmd_identify(args: argparse.Namespace) -> int:
    from nowplaying.app import build_artwork, build_recognizer
    from nowplaying.capture import decode_file
    from nowplaying.recognizers.base import RecognizerError

    cfg = load(args, required=False)
    if not Path(args.file).is_file():
        print(f"no such file: {args.file}", file=sys.stderr)
        return 2
    names = [args.recognizer] if args.recognizer else list(cfg.recognizers.order)
    clip_mode = args.offset is not None or args.length is not None
    offset = args.offset or 0.0
    length = args.length if args.length is not None else 12.0
    window = None
    match = None
    for name in names:
        recognizer = build_recognizer(cfg, name)
        ok, why = recognizer.check()
        if not ok:
            print(f"{name}: unavailable: {why}")
            continue
        started = time.monotonic()
        try:
            if name == "acoustid" and not clip_mode:
                # AcoustID only works on a whole file fingerprinted from 0:00.
                found = recognizer.identify_file(args.file)      # type: ignore[attr-defined]
                mode = "whole file"
            else:
                if window is None:
                    window = decode_file(args.file, cfg.capture, offset, length)
                found = recognizer.recognize(window)
                mode = f"{window.duration:.1f} s clip from {offset:.0f} s"
        except RecognizerError as exc:
            print(f"{name}: error: {exc}")
            continue
        took = time.monotonic() - started
        if found is None:
            print(f"{name}: no match ({mode}, {took:.1f} s)")
        else:
            print(f"{name}: {found.artist} - {found.title} ({mode}, {took:.1f} s)")
            match = match or found
    if match is None:
        return 1
    record = dataclasses.asdict(match)
    record.pop("extra", None)
    if args.art:
        artwork = build_artwork(cfg)
        art = artwork.resolve(match)
        record["art_file"] = str(artwork.cache.path(art.key))
        record["art_source"] = art.source
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from nowplaying.app import build_service

    cfg = load(args, required=False)
    service = build_service(cfg, with_capture=False)
    install_signal_handlers(service.stop)
    track = {"state": "playing", "title": args.title, "artist": args.artist,
             "album": args.album, "art_url": args.art_url, "source": "simulated"}
    service.handle_command("external", json.dumps(track).encode("utf-8"))
    if args.seconds:
        timer = threading.Timer(args.seconds, service.stop)
        timer.daemon = True
        timer.start()
    log.info("simulating %s - %s; press Ctrl+C to stop", args.artist, args.title)
    service.run()
    return 0


def cmd_levels(args: argparse.Namespace) -> int:
    """Once a second: the input level, and what the music detector makes of it."""
    from nowplaying.app import build_detector
    from nowplaying.capture import AudioCapture

    cfg = load(args, required=False)
    threshold = cfg.detection.silence_dbfs
    capture = AudioCapture(cfg.capture, silence_dbfs=threshold,
                           gap_seconds=cfg.detection.gap_seconds)
    detector = build_detector(cfg, capture)
    if detector is not None and not detector.load():
        detector = None
    capture.start()
    print(f"input: {cfg.capture.format or 'auto'} {cfg.capture.input}   "
          f"silence threshold: {threshold:.0f} dBFS")
    if detector is not None:
        print(f"music detector: gate opens at a score of {cfg.music_detector.threshold:.2f}, "
              f"closes at {cfg.music_detector.release:.2f}")
    try:
        for _ in range(args.seconds):
            time.sleep(1.0)
            if not capture.healthy:
                print("  (no audio arriving yet)")
                continue
            level = capture.level_dbfs(1.0)
            bar = "#" * max(0, min(40, int((level + 70) * 2 / 3)))
            line = f"{level:7.1f} dBFS  {bar:40s}"
            if level < threshold:
                line += "  silent"
            elif detector is not None:
                detector.step()
                verdict = detector.verdict()
                line += (f"  music {verdict.music:.2f}  speech {verdict.speech:.2f}  "
                         f"{'MUSIC: would look up' if verdict.is_music else 'not music: no lookup'}")
            print(line)
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from nowplaying.app import build_detector, build_recognizer
    from nowplaying.capture import AudioCapture, resolve_ffmpeg

    failures = 0

    def report(ok: bool, label: str, detail: str) -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"[{'ok' if ok else 'FAIL'}] {label}: {detail}")

    try:
        cfg = load(args, required=False)
    except ConfigError as exc:
        report(False, "config", str(exc))
        return 1
    report(True, "config", str(cfg.path) if cfg.path else "built-in defaults (no file given)")

    try:
        ffmpeg = resolve_ffmpeg(cfg.capture.ffmpeg_path)
        version = subprocess.run([ffmpeg, "-version"], capture_output=True, timeout=15)
        report(True, "ffmpeg", version.stdout.decode("utf-8", "replace").splitlines()[0])
    except Exception as exc:
        report(False, "ffmpeg", str(exc))

    for name in cfg.recognizers.order:
        ok, why = build_recognizer(cfg, name).check()
        report(ok, f"recognizer {name}", why)

    detector = build_detector(cfg, None)
    if detector is None:
        report(True, "music detector", "switched off: every sound will be looked up")
    else:
        loaded = detector.load()
        report(loaded, "music detector",
               f"ready ({detector.model_path.name})" if loaded else detector.error)

    try:
        existed = cfg.data_dir.is_dir()
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        if not existed:
            cfg.data_dir.rmdir()            # a check should not leave a folder behind
        report(True, "data_dir", str(cfg.data_dir))
    except OSError as exc:
        report(False, "data_dir", f"{cfg.data_dir}: {exc}")

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((cfg.http.host, cfg.http.port))
        report(True, "http port", f"{cfg.http.host}:{cfg.http.port} is free")
    except OSError as exc:
        report(False, "http port", f"{cfg.http.host}:{cfg.http.port} cannot be used "
                                   f"(is the daemon already running?): {exc}")

    if cfg.mqtt.enabled:
        ok, detail = _check_mqtt(cfg)
        report(ok, "mqtt", detail)
    else:
        report(True, "mqtt", "disabled in the config")

    if not args.no_capture:
        capture = AudioCapture(cfg.capture, silence_dbfs=cfg.detection.silence_dbfs,
                               gap_seconds=cfg.detection.gap_seconds)
        capture.start()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and capture.seconds_buffered() < 3.0:
            time.sleep(0.2)
        got = capture.seconds_buffered()
        level = capture.level_dbfs(3.0) if got else None
        capture.stop()
        if got >= 3.0:
            note = "below the silence threshold" if level < cfg.detection.silence_dbfs \
                else "above the silence threshold"
            report(True, "audio capture", f"{level:.1f} dBFS, {note} "
                                          f"({cfg.detection.silence_dbfs:.0f})")
        else:
            report(False, "audio capture", f"no audio from {cfg.capture.format or 'auto'} "
                                           f"{cfg.capture.input!r} (see the ffmpeg lines above)")
    print("all checks passed" if not failures else f"{failures} check(s) failed")
    return 1 if failures else 0


def _check_mqtt(cfg: Config) -> tuple[bool, str]:
    import paho.mqtt.client as mqtt

    mq = cfg.mqtt
    outcome: dict[str, object] = {}
    done = threading.Event()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"nowplaying-doctor-{os.getpid()}")
    if mq.username:
        client.username_pw_set(mq.username, mq.password or None)
    if mq.tls:
        client.tls_set()

    def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
        outcome["reason"] = reason_code
        done.set()

    client.on_connect = on_connect
    try:
        client.connect(mq.host, mq.port, keepalive=10)
    except OSError as exc:
        return False, f"cannot reach {mq.host}:{mq.port}: {exc}"
    client.loop_start()
    answered = done.wait(8)
    client.disconnect()
    client.loop_stop()
    if not answered:
        return False, f"{mq.host}:{mq.port} did not answer the MQTT handshake"
    reason = outcome["reason"]
    if getattr(reason, "is_failure", False):
        return False, f"{mq.host}:{mq.port} refused the login: {reason}"
    return True, f"connected to {mq.host}:{mq.port}"


# -------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio_recognition_daemon.py",
        description="Identify the music playing in a room and publish it over MQTT/HTTP.")
    parser.add_argument("-c", "--config", help="path to config.toml "
                        "(default: $NOWPLAYING_CONFIG, then ./config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"now-playing {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="run the daemon (the default)")

    identify = sub.add_parser("identify", help="run the recognizers on an audio file")
    identify.add_argument("file")
    identify.add_argument("--offset", type=float, help="start this many seconds into the file")
    identify.add_argument("--length", type=float, help="clip length in seconds (default 12)")
    identify.add_argument("--recognizer", choices=("shazam", "acoustid", "audd"),
                          help="use only this recognizer instead of recognizers.order")
    identify.add_argument("--art", action="store_true", help="also fetch and convert the cover")

    simulate = sub.add_parser("simulate", help="publish a track without capturing audio")
    simulate.add_argument("--title", required=True)
    simulate.add_argument("--artist", default="")
    simulate.add_argument("--album", default="")
    simulate.add_argument("--art-url", default="", help="use this image instead of looking one up")
    simulate.add_argument("--seconds", type=float, default=0.0,
                          help="stop after this long (default: run until Ctrl+C)")

    levels = sub.add_parser("levels", help="print the input level once a second")
    levels.add_argument("--seconds", type=int, default=30)

    doctor = sub.add_parser("doctor", help="check ffmpeg, recognizers, MQTT and the audio input")
    doctor.add_argument("--no-capture", action="store_true", help="skip the audio input test")
    return parser


COMMANDS = {"run": cmd_run, "identify": cmd_identify, "simulate": cmd_simulate,
            "levels": cmd_levels, "doctor": cmd_doctor}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = COMMANDS[args.command or "run"]
    try:
        return command(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
