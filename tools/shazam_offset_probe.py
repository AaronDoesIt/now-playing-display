"""Live check: what does Shazam's `offset` mean, and where can a track's length come from?

Pauses the Pi's own lookups (Listening switch OFF over MQTT) so the two do not add up against
Shazam's rate limit, makes three lookups on the radio stream about 25 s apart, then switches
Listening back ON. Run from now_playing_display/server with the project's .venv python.

    python offset_probe.py <secrets.yaml> <broker>
"""
import asyncio
import json
import re
import sys
import time

import paho.mqtt.client as mqtt
import requests
import shazamio

from nowplaying.capture import decode_file
from nowplaying.config import CaptureConfig, ShazamConfig
from nowplaying.recognizers.shazam import ShazamRecognizer, parse_shazam

URL = "http://ice1.somafm.com/seventies-128-mp3"
CLIP = 8.0

text = open(sys.argv[1], encoding="utf-8").read()
user = re.search(r'^mqtt_username:\s*"?([^"\n]*)"?', text, re.M).group(1)
pw = re.search(r'^mqtt_password:\s*"?([^"\n]*)"?', text, re.M).group(1)
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="laptop-probe")
client.username_pw_set(user, pw)
client.connect(sys.argv[2], 1883, 30)
client.loop_start()


def listening(on: bool) -> None:
    client.publish("nowplaying/now_playing/listening/set", "ON" if on else "OFF").wait_for_publish(5)
    print("Pi listening ->", "ON" if on else "OFF")


def apple_ids(track: dict) -> dict:
    out = {"albumadamid": track.get("albumadamid"), "trackadamid": track.get("trackadamid")}
    hub = track.get("hub") or {}
    ids = [a.get("id") for a in (hub.get("actions") or []) if isinstance(a, dict) and a.get("id")]
    out["hub.actions ids"] = ids
    for opt in hub.get("options") or []:
        for a in opt.get("actions") or []:
            m = re.search(r"[?&]i=(\d+)", str(a.get("uri") or ""))
            if m:
                out.setdefault("song id in hub uri", m.group(1))
    return out


rec = ShazamRecognizer(ShazamConfig())
cfg = CaptureConfig()
rows = []
try:
    listening(False)
    time.sleep(3)
    for i in range(3):
        t0 = time.time()
        win = decode_file(URL, cfg, length=CLIP)
        t1 = time.time()
        answer = asyncio.run(rec._ask(shazamio, win.to_wav_bytes()))
        t2 = time.time()
        m = parse_shazam(answer)
        if m is None:
            print(i, "no match")
        else:
            track = answer["track"]
            first = (answer.get("matches") or [{}])[0]
            rows.append((t1, m.identity(), m.offset))
            print(f"{i}: capture {t1 - t0:.1f}s, lookup {t2 - t1:.1f}s -> {m.artist} - {m.title}"
                  f" | offset {m.offset} | match keys {sorted(first)} | isrc {m.isrc}")
            if i == 0 or (len(rows) > 1 and rows[-1][1] != rows[-2][1]):
                ids = apple_ids(track)
                print("   track keys:", sorted(track))
                print("   apple ids:", ids)
                song_id = ids.get("trackadamid") or ids.get("song id in hub uri") or (ids["hub.actions ids"][0] if ids["hub.actions ids"] else None)
                if song_id:
                    r = requests.get("https://itunes.apple.com/lookup", params={"id": song_id, "country": "US"}, timeout=10).json()
                    for item in r.get("results", [])[:2]:
                        print("   itunes lookup:", item.get("wrapperType"), item.get("kind"), "|", item.get("artistName"), "-",
                              item.get("trackName"), "|", item.get("collectionName"), "| ms", item.get("trackTimeMillis"))
                if m.isrc:
                    r = requests.get("https://musicbrainz.org/ws/2/recording", params={"query": f"isrc:{m.isrc}", "limit": "5", "fmt": "json"},
                                     headers={"User-Agent": "now-playing-display/0.1 (home project)"}, timeout=10).json()
                    print("   musicbrainz by isrc: lengths ms", [x.get("length") for x in r.get("recordings", [])][:5])
        if i < 2:
            time.sleep(max(0.0, 25.0 - (time.time() - t0)))
finally:
    listening(True)
    time.sleep(1)
    client.loop_stop()
    client.disconnect()

print("--- same-song pairs: seconds between captures vs change in offset")
for a, b in zip(rows, rows[1:]):
    if a[1] == b[1] and a[2] is not None and b[2] is not None:
        print(f"   wall {b[0] - a[0]:.1f}s   offset {b[2] - a[2]:.1f}s")
