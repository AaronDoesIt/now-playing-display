"""Ask the panel for two pictures a split second apart and report what it does with them.

The panel loads one picture at a time. This repeats, on purpose, what went wrong on 2026-11-11:
a cover was asked for while an idle picture was still loading, the request was thrown away, and
the idle picture stayed on screen under the song's text. The panel's own log (it publishes it
over MQTT) shows what it fetched and when the text went up.

It sends two messages on the daemon's state topic, exactly as the daemon would: first "idle"
with one of the idle pictures, then, a moment later, "playing" with a cover. After a few seconds
it puts the daemon's real state back. The daemon itself is not touched.

    python tools/picture_race_test.py display/esphome/secrets.yaml 10.0.0.61 COVER.jpg [gap seconds] [rounds]
    python tools/picture_race_test.py display/esphome/secrets.yaml 10.0.0.61 --missing

COVER.jpg is the file name of any cover in the Pi's cache (/var/lib/now-playing/art/c....jpg).
--missing asks for a picture that does not exist instead: the text must still go up (after a
few seconds), and the panel must stop asking after three tries.
"""
import json
import re
import sys
import time

import paho.mqtt.client as mqtt
import requests

secrets, host, cover = sys.argv[1], sys.argv[2], sys.argv[3]
missing = cover == "--missing"
gap = float(sys.argv[4]) if len(sys.argv) > 4 else 0.15
rounds = int(sys.argv[5]) if len(sys.argv) > 5 else 1
TOPIC = "nowplaying/now_playing/state"
PANEL_LOG = "now-playing-display/debug"
base = f"http://{host}:8777/art/"
SHOW = ("online_image", "[screen", "[E]", "[W]", "http_request")

text = open(secrets, encoding="utf-8").read()
user = re.search(r'^mqtt_username:\s*"?([^"\n]*)"?', text, re.M).group(1)
pw = re.search(r'^mqtt_password:\s*"?([^"\n]*)"?', text, re.M).group(1)

if missing:
    cover = "no-such-picture.jpg"
elif requests.get(base + cover, timeout=10).status_code != 200:
    sys.exit(f"the Pi does not have {cover}")

real = []            # what the daemon itself said (our own messages carry "test")
panel = []           # (clock, line) from the panel's log


def on_message(client, userdata, msg):
    body = msg.payload.decode("utf-8", "replace")
    if msg.topic == PANEL_LOG:
        panel.append((time.time(), re.sub(r"\x1b\[[0-9;]*m", "", body)))
    elif body.startswith("{"):
        data = json.loads(body)
        if not data.get("test"):
            real.append((time.time(), data))


def wait_for_quiet():
    """Start a few seconds after the daemon last spoke, so its next idle picture (one every
    10 s) does not land in the middle of the test."""
    seen = len(real)
    deadline = time.time() + 12
    while len(real) == seen and time.time() < deadline:
        time.sleep(0.1)
    time.sleep(3.0)


def report(mark, seconds):
    time.sleep(seconds)
    lines = [(when, line) for when, line in panel if when >= mark]
    for when, line in lines:
        if any(k in line for k in SHOW):
            print(f"    +{when - mark:5.2f} s  {line.replace(base, '')[:120]}")
    spoke = [d for when, d in real if when >= mark]
    if spoke:
        print(f"    (the daemon itself spoke {len(spoke)} time(s) during this round)")
    return [line for _, line in lines]


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="laptop-race-test")
c.username_pw_set(user, pw)
c.on_message = on_message
c.connect(host, 1883, 30)
c.loop_start()
c.subscribe([(TOPIC, 0), (PANEL_LOG, 0)])
time.sleep(2.0)
if not real:
    sys.exit("no state from the daemon (is it running?)")

playing = {"state": "playing", "title": "Picture loader test", "artist": "the cover should be showing",
           "album": "", "art_url": base + cover, "art_source": "shazam", "test": True}
ok = 0
if missing:
    rounds = 1
    wait_for_quiet()
    mark = time.time()
    c.publish(TOPIC, json.dumps(playing)).wait_for_publish(5)
    print(f"asked for a picture the Pi does not have ({cover}), with a song's text")
    lines = report(mark, 12.0)
    tries = sum("Updating image from" in line and cover in line for line in lines)
    text_up = any("showing playing: Picture loader test" in line for line in lines)
    ok = int(tries == 3 and text_up)
    print(f"    tries: {tries} (should be 3)   text went up anyway: {'yes' if text_up else 'NO'}")
else:
    for n in range(rounds):
        wait_for_quiet()
        current = real[-1][1].get("art_url", "")
        # an idle picture the panel does not have on screen now, so it really has to fetch it
        slides = [u for u in dict.fromkeys(d.get("art_url", "") for _, d in real)
                  if "/art/s" in u and u != current]
        first = slides[-1] if slides else base + "idle450.jpg"
        mark = time.time()
        one = {"state": "idle", "title": "", "artist": "", "album": "", "art_url": first,
               "art_source": "slide", "test": True}
        c.publish(TOPIC, json.dumps(one)).wait_for_publish(5)
        time.sleep(gap)
        c.publish(TOPIC, json.dumps(playing)).wait_for_publish(5)
        print(f"round {n + 1}: asked for {first.rsplit('/', 1)[-1]}, then {gap * 1000:.0f} ms later for the cover {cover}")
        lines = report(mark, 6.0)
        joined = "\n".join(lines)
        on_screen = f"picture on screen: {base}{cover}" in joined
        fetched = f"Updating image from {base}{cover}" in joined
        new_firmware = "picture on screen:" in joined
        good = on_screen if new_firmware else (fetched and "already being updated" not in joined)
        # the song's text must not go up before its cover is on screen
        if new_firmware and good:
            where_text = joined.find("showing playing: Picture loader test")
            where_cover = joined.find(f"picture on screen: {base}{cover}")
            if where_text != -1 and where_text < where_cover:
                good = False
                print("    the song's text went up before its cover")
        ok += good
        print(f"    -> {'RIGHT: the cover reached the screen' if good else 'WRONG: the cover was lost'}")
        c.publish(TOPIC, json.dumps(real[-1][1])).wait_for_publish(5)      # put the truth back
        time.sleep(1.0)

if missing:
    c.publish(TOPIC, json.dumps(real[-1][1])).wait_for_publish(5)
    time.sleep(1.0)
c.loop_stop()
c.disconnect()
print(f"{ok} of {rounds} passed")
