"""End-to-end check against real services. Not part of the unit tests; run it by hand.

It runs the real daemon twice, with an internet radio stream standing in for the
microphone: real ffmpeg capture, the real music detector, real Shazam lookups,
real cover-art downloads, a real MQTT broker (amqtt, started inside this script)
and the real HTTP server.

  1. A talk station: the daemon must listen the whole time and make NO
     recognition request, because the detector hears speech, not music.
  2. A music station: tracks must be identified (compared with the station's own
     playlist), covers must be 480x480 baseline JPEGs, and everything a display
     or Home Assistant relies on (retained state, discovery, commands,
     availability) is checked.

    python tests/live_check.py --minutes 5

Needs the dev requirements (amqtt) and internet access. It makes at most about
three Shazam requests a minute, plus a few to the station playlist and art hosts.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nowplaying.app import build_service  # noqa: E402
from nowplaying.config import load_config, validate  # noqa: E402
from nowplaying.models import Match, same_track  # noqa: E402

UA = {"User-Agent": "NowPlayingDisplay-livecheck/0.1"}
ENTITIES = 11           # discovery configs the daemon announces


def run_broker(port: int, ready: threading.Event, stop: threading.Event) -> None:
    async def main() -> None:
        from amqtt.broker import Broker

        broker = Broker({
            "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
            "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin":
                        {"allow_anonymous": True}},
        })
        await broker.start()
        ready.set()
        while not stop.is_set():
            await asyncio.sleep(0.2)
        await broker.shutdown()

    asyncio.run(main())


class Recorder:
    """An MQTT client that keeps everything it sees, like a display or HA would."""

    def __init__(self, port: int, client_id: str) -> None:
        self.messages: list[tuple[float, str, bytes, bool]] = []
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.on_connect = lambda c, u, f, rc, p=None: c.subscribe("#", qos=1)
        self.client.on_message = lambda c, u, m: self.messages.append(
            (time.time(), m.topic, bytes(m.payload), bool(m.retain)))
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()

    def publish(self, topic: str, payload: str) -> None:
        self.client.publish(topic, payload, qos=1)

    def on(self, topic: str) -> list[tuple[float, bytes, bool]]:
        return [(t, p, r) for t, tp, p, r in list(self.messages) if tp == topic]

    def json(self, topic: str) -> list[dict]:
        return [json.loads(p) for _, p, _ in self.on(topic)]

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()


def station_playlist(url: str) -> list[Match]:
    try:
        data = requests.get(url, headers=UA, timeout=10).json()
        return [Match(title=str(s.get("title") or ""), artist=str(s.get("artist") or ""),
                      album=str(s.get("album") or "")) for s in data.get("songs", [])]
    except Exception as exc:
        print(f"  (station playlist unavailable: {exc})")
        return []


def frame_markers(data: bytes) -> list[int]:
    markers, index = [], 2
    while index < len(data) and data[index] == 0xFF:
        marker = data[index + 1]
        if marker == 0xDA:
            break
        markers.append(marker)
        index += 2 + int.from_bytes(data[index + 2:index + 4], "big")
    return markers


def check_cover(url: str) -> str:
    reply = requests.get(url, timeout=10)
    assert reply.status_code == 200, f"{url}: HTTP {reply.status_code}"
    assert reply.headers["Content-Type"] == "image/jpeg", reply.headers["Content-Type"]
    assert int(reply.headers["Content-Length"]) == len(reply.content)
    image = Image.open(io.BytesIO(reply.content))
    assert image.format == "JPEG" and image.size == (480, 480) and image.mode == "RGB", \
        (image.format, image.size, image.mode)
    markers = frame_markers(reply.content)
    assert 0xC0 in markers and 0xC2 not in markers, "cover is not a baseline JPEG"
    return f"480x480 baseline JPEG, {len(reply.content)} bytes"


def make_config(args: argparse.Namespace, instance: str, stream: str, workdir: Path):
    cfg = load_config(None, env={})
    cfg.general.name, cfg.general.instance = f"Live {instance}", instance
    cfg.general.data_dir = str(workdir)
    cfg.capture.format, cfg.capture.input = "", stream
    cfg.mqtt.host, cfg.mqtt.port = "127.0.0.1", args.mqtt_port
    cfg.mqtt.level_interval = 5.0
    cfg.http.host, cfg.http.port = "127.0.0.1", args.http_port
    cfg.http.public_base_url = f"http://127.0.0.1:{args.http_port}"
    validate(cfg)
    return cfg


def talk_phase(args: argparse.Namespace, workdir: Path, failures: list[str]) -> None:
    """A talk station: always listening, never asking the recognition service."""
    print(f"\n--- phase 1: talk ({args.talk_seconds:g} s of {args.talk_stream}) ---")
    cfg = make_config(args, "live_talk", args.talk_stream, workdir)
    root = f"nowplaying/{cfg.general.instance}"
    recorder = Recorder(args.mqtt_port, "live-check-talk")
    service = build_service(cfg)
    worker = threading.Thread(target=service.run, name="daemon-talk")
    worker.start()
    time.sleep(args.talk_seconds)
    snapshot = requests.get(f"http://127.0.0.1:{args.http_port}/state.json", timeout=5).json()
    service.stop()
    worker.join(timeout=20)
    time.sleep(1.0)
    reports = recorder.json(f"{root}/audio")
    states = recorder.json(f"{root}/state")
    recorder.stop()

    calls = sum(stats["calls"] for stats in snapshot["recognizers"].values())
    heard = [r for r in reports if r.get("level_dbfs") is not None]
    print(f"audio reports           : {len(heard)}")
    if heard:
        print(f"  input level           : {min(r['level_dbfs'] for r in heard):.0f} to "
              f"{max(r['level_dbfs'] for r in heard):.0f} dBFS")
        print(f"  Music score           : max {max(r['music'] for r in heard):.2f}")
        print(f"  Speech score          : mean {sum(r['speech'] for r in heard) / len(heard):.2f}")
    print(f"recognition requests    : {calls}")
    print(f"tracks announced        : {sum(1 for s in states if s['state'] == 'playing')}")
    if not heard:
        failures.append("talk phase: no audio reports arrived (stream down?)")
    elif max(r["level_dbfs"] for r in heard) < cfg.detection.silence_dbfs:
        failures.append("talk phase: the stream was silent, so the test proves nothing")
    if calls:
        failures.append(f"talk phase: {calls} recognition request(s) were made for speech")
    if any(r["is_music"] for r in heard):
        failures.append("talk phase: the detector reported music on a talk station")
    if any(s["state"] == "playing" for s in states):
        failures.append("talk phase: a track was announced")


def music_phase(args: argparse.Namespace, workdir: Path, failures: list[str]) -> None:
    print(f"\n--- phase 2: music ({args.minutes:g} min of {args.stream}) ---")
    cfg = make_config(args, "live_music", args.stream, workdir)
    root = f"nowplaying/{cfg.general.instance}"
    recorder = Recorder(args.mqtt_port, "live-check-music")
    service = build_service(cfg)
    worker = threading.Thread(target=service.run, name="daemon-music")
    started = time.time()
    worker.start()

    seen: list[dict] = []
    confirmed: dict[str, bool] = {}
    first_match_after: float | None = None
    identify_sent = False
    deadline = started + args.minutes * 60
    while time.time() < deadline:
        time.sleep(2.0)
        elapsed = time.time() - started
        states = recorder.json(f"{root}/state")
        for state in states[len(seen):]:
            seen.append(state)
            if state["state"] != "playing":
                print(f"[{elapsed:5.0f}s] state: {state['state']}")
                continue
            if first_match_after is None:
                first_match_after = elapsed
            print(f"[{elapsed:5.0f}s] {state['artist']} - {state['title']}"
                  f"{' [' + state['album'] + ']' if state['album'] else ''}"
                  f"  (cover: {state['art_source']})")
        if not identify_sent and elapsed > 70:
            identify_sent = True
            recorder.publish(f"{root}/identify", "PRESS")       # the HA "Identify now" button
        # the station's playlist lags the stream a little, so keep comparing as we go
        playlist = station_playlist(args.playlist)[:6]
        for state in seen:
            if state["state"] == "playing":
                key = f"{state['artist']} - {state['title']}"
                found = Match(title=state["title"], artist=state["artist"])
                if any(same_track(found, entry) for entry in playlist):
                    confirmed[key] = True
                confirmed.setdefault(key, False)

    snapshot = requests.get(f"http://127.0.0.1:{args.http_port}/state.json", timeout=5).json()
    reports = recorder.json(f"{root}/audio")
    if not any(r.get("is_music") for r in reports):
        failures.append("music phase: the detector never reported music")

    # ---- the listening switch, over real MQTT ---------------------------------
    recorder.publish(f"{root}/listening/set", "OFF")
    time.sleep(3)
    if recorder.json(f"{root}/state")[-1]["state"] != "off":
        failures.append("listening OFF did not give state off")
    if recorder.on(f"{root}/listening")[-1][1] != b"OFF":
        failures.append("listening state topic did not report OFF")
    recorder.publish(f"{root}/listening/set", "ON")
    time.sleep(3)
    if recorder.on(f"{root}/listening")[-1][1] != b"ON":
        failures.append("listening state topic did not report ON")

    # ---- what a display gets when it connects late: the retained messages ------
    late = Recorder(args.mqtt_port, "live-check-late-joiner")
    time.sleep(2)
    retained_state = [m for m in late.on(f"{root}/state") if m[2]]
    retained_avail = [m for m in late.on(f"{root}/availability") if m[2]]
    prefix = f"homeassistant/"
    retained_config = {m[1] for m in late.messages
                       if m[1].startswith(prefix) and "nowplaying_live_music" in m[1] and m[3]}
    if not retained_state:
        failures.append("state is not retained")
    if not retained_avail or retained_avail[-1][1] != b"online":
        failures.append("availability is not retained as online")
    if len(retained_config) != ENTITIES:
        failures.append(f"expected {ENTITIES} retained discovery configs, "
                        f"saw {len(retained_config)}")
    late.stop()

    # ---- HTTP: every cover that was announced must be fetchable and correct ----
    covers = {}
    for state in seen:
        try:
            covers[state["art_url"]] = check_cover(state["art_url"])
        except AssertionError as exc:
            failures.append(f"cover check failed: {exc}")
    health = requests.get(f"http://127.0.0.1:{args.http_port}/healthz", timeout=5)
    if health.status_code != 200:
        failures.append(f"/healthz returned {health.status_code}: {health.text}")

    service.stop()
    worker.join(timeout=20)
    time.sleep(1.5)
    if worker.is_alive():
        failures.append("the daemon did not stop")
    if recorder.on(f"{root}/availability")[-1][1] != b"offline":
        failures.append("availability after shutdown is not offline")
    if recorder.json(f"{root}/state")[-1]["state"] != "off":
        failures.append("state after shutdown is not off")
    recorder.stop()

    if not any(s["state"] == "playing" for s in seen):
        failures.append("music phase: nothing was identified")
    stats = snapshot.get("recognizers", {})
    minutes = (time.time() - started) / 60
    print(f"first identification    : "
          f"{f'{first_match_after:.0f} s after start' if first_match_after else 'none'}")
    print(f"tracks identified       : {len(confirmed)}")
    for key, ok in confirmed.items():
        print(f"  {'confirmed by the station' if ok else 'not in station playlist '}  {key}")
    for name, values in stats.items():
        print(f"{name} requests         : {values['calls']} in {minutes:.1f} min "
              f"({values['hits']} hits, {values['errors']} errors)")
        if values["errors"]:
            failures.append(f"{name} reported {values['errors']} error(s) (rate limited?)")
    print(f"state messages          : {len(seen)}")
    print(f"covers checked          : {len(covers)}")
    for url, what in covers.items():
        print(f"  {url.rsplit('/', 1)[-1]}: {what}")
    print(f"discovery configs       : {len(retained_config)} retained")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=5.0, help="length of the music phase")
    parser.add_argument("--stream", default="http://ice1.somafm.com/seventies-128-mp3")
    parser.add_argument("--playlist", default="https://somafm.com/songs/seventies.json")
    parser.add_argument("--talk-stream", default="http://npr-ice.streamguys1.com/live.mp3")
    parser.add_argument("--talk-seconds", type=float, default=75.0,
                        help="length of the talk phase (0 skips it)")
    parser.add_argument("--mqtt-port", type=int, default=18830)
    parser.add_argument("--http-port", type=int, default=18777)
    parser.add_argument("--keep", help="directory to keep the cover cache in (default: temp)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("amqtt", "transitions", "urllib3", "PIL", "asyncio", "shazamio_core"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    ready, broker_stop = threading.Event(), threading.Event()
    threading.Thread(target=run_broker, args=(args.mqtt_port, ready, broker_stop),
                     daemon=True).start()
    assert ready.wait(15), "the test broker did not start"
    workdir = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="nowplaying-live-"))

    failures: list[str] = []
    if args.talk_seconds > 0:
        talk_phase(args, workdir, failures)
    if args.minutes > 0:
        music_phase(args, workdir, failures)
    broker_stop.set()

    print(f"\ncover cache             : {workdir}")
    print(f"failures                : {len(failures)}")
    for failure in failures:
        print(f"  FAIL {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
