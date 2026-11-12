"""Watch the daemon's state messages and the panel's own log side by side, in the order they
happened. The panel publishes its log over MQTT, so this shows which picture it fetched and
when the text went up, without a USB cable.

    python tools/mqtt_watch.py [seconds] [--resume] [--all]

--resume switches the Pi's listening back on first; --all prints every panel log line.
Run it from the project folder (it reads display/esphome/secrets.yaml for the MQTT login).
"""
import json
import re
import sys
import time

import paho.mqtt.client as mqtt

t = open("display/esphome/secrets.yaml", encoding="utf-8").read()
user = re.search(r'^mqtt_username:\s*"?([^"\n]*)"?', t, re.M).group(1)
pw = re.search(r'^mqtt_password:\s*"?([^"\n]*)"?', t, re.M).group(1)
numbers = [a for a in sys.argv[1:] if not a.startswith("--")]
seconds = float(numbers[0]) if numbers else 40
everything = "--all" in sys.argv
KEEP = ("Updating image", "Image fully", "already being updated", "[screen", "[E]", "[W]",
        "touch x=", "compiled on", "failed", "Guru", "Rotation")
events, panel_lines = [], 0


def clock():
    return time.strftime("%H:%M:%S") + f".{int(time.time() * 10) % 10}"


def on_message(cl, u, m):
    global panel_lines
    s = m.payload.decode("utf-8", "replace")
    if m.topic.endswith("/debug"):
        panel_lines += 1
        line = re.sub(r"\x1b\[[0-9;]*m", "", s)
        if everything or any(k in line for k in KEEP):
            line = re.sub(r"http://[0-9.]+:\d+/art/", "", line)
            events.append(f"{clock()}      panel  {line[:120]}")
    elif m.topic.endswith("/state") and s.startswith("{"):
        d = json.loads(s)
        who = "TEST  " if d.get("test") else "daemon"
        events.append(f"{clock()}  {who}  {d.get('state')}  {(d.get('title') or '')[:34]!r}  "
                      f"{d.get('art_url', '').rsplit('/', 1)[-1]}")
    elif m.topic.endswith("/status"):
        events.append(f"{clock()}      panel  is {s}")


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="laptop-watch")
c.username_pw_set(user, pw)
c.on_message = on_message
c.connect("10.0.0.61", 1883, 30)
c.loop_start()
c.subscribe([("now-playing-display/debug", 0), ("nowplaying/now_playing/state", 0),
             ("now-playing-display/status", 0)])
if "--resume" in sys.argv:
    c.publish("nowplaying/now_playing/listening/set", "ON").wait_for_publish(5)
    print("Pi listening -> ON")
time.sleep(seconds)
c.loop_stop()
c.disconnect()
for e in events:
    print(e)
print(f"({panel_lines} panel log lines in {seconds:.0f} s)")
