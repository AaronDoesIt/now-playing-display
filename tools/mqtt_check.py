"""Connect to the broker on the Pi the way the panel does (same login, from the LAN) and show what is there.
Usage: python mqtt_check.py <secrets.yaml> <broker> [seconds]      (the password is read, never printed)"""
import json
import re
import sys
import time

import paho.mqtt.client as mqtt

text = open(sys.argv[1], encoding="utf-8").read()
user = re.search(r'^mqtt_username:\s*"?([^"\n]*)"?', text, re.M).group(1)
pw = re.search(r'^mqtt_password:\s*"?([^"\n]*)"?', text, re.M).group(1)
host = sys.argv[2]
seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 8

seen = {}


def on_connect(c, u, flags, rc, props=None):
    print("connect result:", rc)
    c.subscribe([("nowplaying/#", 0), ("now-playing-display/#", 0), ("homeassistant/#", 0)])


def on_message(c, u, m):
    seen[m.topic] = (m.payload, m.retain)


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="laptop-check")
c.username_pw_set(user, pw)
c.on_connect = on_connect
c.on_message = on_message
c.connect(host, 1883, 30)
c.loop_start()
time.sleep(seconds)
c.loop_stop()
c.disconnect()

disc = [t for t in seen if t.startswith("homeassistant/")]
print(f"topics seen: {len(seen)}  (Home Assistant discovery configs: {len(disc)})")
for t in sorted(seen):
    if t.startswith("homeassistant/"):
        continue
    payload, retained = seen[t]
    s = payload.decode("utf-8", "replace")
    if t.endswith("/state") and s.startswith("{"):
        try:
            d = json.loads(s)
            s = json.dumps({k: d[k] for k in ("state", "title", "artist", "album", "art_url", "source", "updated") if k in d})
        except ValueError:
            pass
    print(f"  {'R' if retained else ' '} {t} = {s[:260]}")

# an anonymous client must be refused
a = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="laptop-anon")
res = {}
a.on_connect = lambda cl, u, f, rc, p=None: res.setdefault("rc", str(rc))
try:
    a.connect(host, 1883, 10)
    a.loop_start()
    time.sleep(2)
    a.loop_stop()
    a.disconnect()
except Exception as e:  # noqa: BLE001
    res["rc"] = repr(e)
print("anonymous client:", res.get("rc", "no answer"))
