"""Switch the Pi's listening on or off over MQTT (the same switch Home Assistant will get).

    python tools/listening_switch.py on|off [secrets.yaml] [broker]

Off: no lookups are made and the panel shows the idle pictures with a "Not listening" tag.
"""
import re
import sys
from pathlib import Path

import paho.mqtt.client as mqtt

want = sys.argv[1].strip().lower()
if want not in ("on", "off"):
    sys.exit("say on or off")
root = Path(__file__).resolve().parent.parent
secrets = sys.argv[2] if len(sys.argv) > 2 else str(root / "display" / "esphome" / "secrets.yaml")
host = sys.argv[3] if len(sys.argv) > 3 else "10.0.0.61"

text = open(secrets, encoding="utf-8").read()
user = re.search(r'^mqtt_username:\s*"?([^"\n]*)"?', text, re.M).group(1)
pw = re.search(r'^mqtt_password:\s*"?([^"\n]*)"?', text, re.M).group(1)
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="laptop-switch")
c.username_pw_set(user, pw)
c.connect(host, 1883, 30)
c.loop_start()
c.publish("nowplaying/now_playing/listening/set", want.upper()).wait_for_publish(5)
c.loop_stop()
c.disconnect()
print("listening ->", want.upper())
