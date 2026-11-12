#!/bin/bash
# A black-box recorder for the Pi Zero 2 W. Every two seconds it appends one line of vital signs to
# ~/np-watch.log and forces it onto the SD card, so that if the Pi freezes, loses power or drops off
# Wi-Fi there is a trail up to the last moment. Started at boot from admin's crontab:
#     @reboot /home/admin/np-watch.sh
# Columns: clock, seconds since boot, power flags (0x0 = never under-voltage), temperature,
# free memory, swap in use, load, Wi-Fi signal (DOWN = not connected), address, the daemon's memory.
LOG="$HOME/np-watch.log"
# keep the previous boots' tail: one file per boot, the last five kept
[ -f "$LOG" ] && mv "$LOG" "$HOME/np-watch.$(date -r "$LOG" +%m%d-%H%M%S).log"
ls -1t "$HOME"/np-watch.*.log 2>/dev/null | tail -n +6 | xargs -r rm -f
echo "boot: $(date '+%Y-%m-%d %H:%M:%S') kernel $(uname -r)" > "$LOG"
# kernel warnings and errors (Wi-Fi driver, SD card, power) go into the same file as they happen
if sudo -n true 2>/dev/null; then
    sudo -n dmesg --follow --level=emerg,alert,crit,err,warn 2>/dev/null \
        | sed -u 's/^/KERNEL /' >> "$LOG" &
fi
while true; do
    wifi="$(/usr/sbin/iw dev wlan0 link 2>/dev/null | awk '/signal/ {print $2 "dBm"} /Not connected/ {print "DOWN"}' | head -1)"
    printf '%s up=%s thr=%s temp=%s free=%sMB swap=%sMB load=%s wifi=%s ip=%s daemon=%sMB\n' \
        "$(date +%H:%M:%S)" "$(cut -d. -f1 /proc/uptime)" \
        "$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)" \
        "$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)" \
        "$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)" \
        "$(awk '/SwapTotal/ {t=$2} /SwapFree/ {f=$2} END {printf "%d", (t-f)/1024}' /proc/meminfo)" \
        "$(cut -d' ' -f1 /proc/loadavg)" "${wifi:-none}" \
        "$(hostname -I 2>/dev/null | cut -d' ' -f1)" \
        "$(ps -o rss= -C python 2>/dev/null | sort -n | tail -1 | awk '{printf "%d", $1/1024}')" >> "$LOG"
    sync                # everything, so the system journal reaches the card too
    sleep 2
done
