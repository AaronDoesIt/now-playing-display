"""Put the idle pictures on the panel for a moment, to look at them while the radio test stream
(which is music nonstop) would otherwise never let the idle slideshow appear.

It pauses the Pi's lookups, tells the panel to show each picture in turn exactly as the daemon
does when no music is detected, then switches the lookups back on. The pictures themselves are
the ones the daemon already rendered: their names in its cache are worked out the same way it
does (a hash of the file and the picture settings).

    python tools/show_slides_once.py display/esphome/secrets.yaml 10.0.0.61 [seconds per picture] [rounds]
"""
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import requests

secrets, host = sys.argv[1], sys.argv[2]
seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
rounds = int(sys.argv[4]) if len(sys.argv) > 4 else 1
SIZE, FIT, QUALITY = 450, "cover", 85                 # must match the Pi's [artwork] settings
TOPIC = "nowplaying/now_playing"

text = open(secrets, encoding="utf-8").read()
user = re.search(r'^mqtt_username:\s*"?([^"\n]*)"?', text, re.M).group(1)
pw = re.search(r'^mqtt_password:\s*"?([^"\n]*)"?', text, re.M).group(1)

urls = []
for path in sorted((Path(__file__).resolve().parent.parent / "idle_images").glob("*.jpg"),
                   key=lambda p: p.name.lower()):
    digest = hashlib.sha1(path.read_bytes() + f"{SIZE}|{FIT}|{QUALITY}".encode()).hexdigest()
    url = f"http://{host}:8777/art/s{digest[:20]}.jpg"
    r = requests.get(url, timeout=10)
    print(f"{path.name}: HTTP {r.status_code}, {len(r.content)} bytes")
    if r.status_code == 200:
        urls.append(url)
if not urls:
    sys.exit("the Pi has not rendered these pictures (is the update installed?)")

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="laptop-slides")
c.username_pw_set(user, pw)
c.connect(host, 1883, 30)
c.loop_start()
try:
    c.publish(f"{TOPIC}/listening/set", "OFF").wait_for_publish(5)
    time.sleep(2)
    for _ in range(rounds):
        for url in urls:
            state = {"state": "idle", "title": "", "artist": "", "album": "", "art_url": url,
                     "art_source": "slide"}
            c.publish(f"{TOPIC}/state", json.dumps(state), retain=False).wait_for_publish(5)
            print("showing", url.rsplit("/", 1)[-1])
            time.sleep(seconds)
finally:
    c.publish(f"{TOPIC}/listening/set", "ON").wait_for_publish(5)
    time.sleep(1)
    c.loop_stop()
    c.disconnect()
    print("lookups switched back on")
