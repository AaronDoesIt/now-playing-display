"""Capture the panel's log from its USB serial port (UART0, 115200).
Usage: python serial_log.py COM12 SECONDS [reset]   ("reset" pulses EN first so the log starts at boot)
Prints the log with colour codes removed; anything that looks like a secret is hidden."""
import re
import sys
import time

import serial

port, seconds = sys.argv[1], float(sys.argv[2])
do_reset = len(sys.argv) > 3 and sys.argv[3] == "reset"

p = serial.Serial()
p.port = port
p.baudrate = 115200
p.timeout = 0.2
p.dtr = False
p.rts = False
p.open()
p.reset_input_buffer()
if do_reset:
    p.rts = True       # EN low
    time.sleep(0.15)
    p.rts = False      # EN high: normal boot
t0 = time.time()
buf = b""
while time.time() - t0 < seconds:
    buf += p.read(8192)
p.close()

text = buf.decode("utf-8", "replace")
text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
print(f"== {len(buf)} bytes, {len(lines)} lines in {seconds:.0f} s from {port}")
for ln in lines:
    low = ln.lower()
    if "password" in low or "psk" in low or "encryption key" in low or "noise" in low and "key" in low:
        ln = re.sub(r"(['\"]).*?\1", "'(hidden)'", ln)
    print(ln[:230])
