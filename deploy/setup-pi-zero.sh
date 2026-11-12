#!/usr/bin/env bash
# One-time root setup of the Now Playing "brain" on the Pi Zero 2 W that lives in the retro TV.
#
#   ssh -t admin@10.0.0.61 "sudo bash now_playing_display/deploy/setup-pi-zero.sh"
#
# What it does, in order:
#   1. deploy/install.sh: ffmpeg, a Python environment in /opt/now-playing, the systemd unit
#   2. a Mosquitto broker on this Pi with one login (the display panel and the daemon share it)
#   3. puts the staged config and secrets in place (staged in ~/np-stage by the laptop, mode 600)
#   4. runs the daemon's own "doctor" check
#   5. enables and starts the service
#
# Safe to run again. Everything it prints also goes to ~/np-setup.log, and it leaves
# ~/np-setup.done or ~/np-setup.failed behind so the laptop can tell how it ended.
set -uo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo: sudo bash $0" >&2
    exit 1
fi

RUN_USER="${SUDO_USER:-admin}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$RUN_HOME/np-stage"
LOG="$RUN_HOME/np-setup.log"
CONF=/etc/now-playing
PY=/opt/now-playing/venv/bin/python
APP=/opt/now-playing/server/audio_recognition_daemon.py

rm -f "$RUN_HOME/np-setup.done" "$RUN_HOME/np-setup.failed"
: > "$LOG"; chown "$RUN_USER": "$LOG"
exec > >(tee -a "$LOG") 2>&1

finish() {
    local code=$?
    if [[ $code -eq 0 ]]; then
        touch "$RUN_HOME/np-setup.done"; chown "$RUN_USER": "$RUN_HOME/np-setup.done"
        echo; echo "=== ALL DONE $(date '+%H:%M:%S') ==="
    else
        touch "$RUN_HOME/np-setup.failed"; chown "$RUN_USER": "$RUN_HOME/np-setup.failed"
        echo; echo "=== SETUP FAILED (exit $code) at step: ${STEP:-?}  $(date '+%H:%M:%S') ==="
    fi
    sleep 1   # let tee flush
}
trap finish EXIT
step() { STEP="$1"; echo; echo "=== $1  ($(date '+%H:%M:%S'))"; }
die() { echo "ERROR: $*" >&2; exit 1; }

for f in config.toml mqtt_user mqtt_password; do
    [[ -s "$STAGE/$f" ]] || die "staged file $STAGE/$f is missing"
done
MQTT_USER="$(tr -d '\r\n' < "$STAGE/mqtt_user")"
MQTT_PASS="$(tr -d '\r\n' < "$STAGE/mqtt_password")"
[[ -n "$MQTT_USER" && -n "$MQTT_PASS" ]] || die "staged MQTT login is empty"

step "1/5 program, Python environment, service file (deploy/install.sh)"
export DEBIAN_FRONTEND=noninteractive
bash "$SRC/deploy/install.sh" || die "install.sh failed"

step "2/5 Mosquitto broker on this Pi"
apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends mosquitto mosquitto-clients \
    || die "could not install mosquitto"
PWFILE=/etc/mosquitto/now-playing.passwd
rm -f "$PWFILE"
mosquitto_passwd -b -c "$PWFILE" "$MQTT_USER" "$MQTT_PASS" || die "mosquitto_passwd failed"
chown mosquitto:mosquitto "$PWFILE"; chmod 600 "$PWFILE"
cat > /etc/mosquitto/conf.d/now-playing.conf <<EOF
# Now Playing: one password-protected listener. The daemon connects from this Pi,
# the display panel from the LAN. Created by deploy/setup-pi-zero.sh.
listener 1883 0.0.0.0
allow_anonymous false
password_file $PWFILE
EOF
systemctl enable mosquitto >/dev/null 2>&1
systemctl restart mosquitto || die "mosquitto did not start (journalctl -u mosquitto)"
sleep 2
systemctl is-active --quiet mosquitto || die "mosquitto is not running"
# prove the login works and that anonymous clients are refused
if mosquitto_pub -h 127.0.0.1 -u "$MQTT_USER" -P "$MQTT_PASS" -t nowplaying/setup-check -m ok; then
    echo "broker accepts the login"
else
    die "broker refused the login"
fi
if mosquitto_pub -h 127.0.0.1 -t nowplaying/setup-check -m anon 2>/dev/null; then
    die "broker accepted an anonymous client"
else
    echo "broker refuses anonymous clients"
fi

step "3/5 configuration and secrets"
install -d -m 755 "$CONF"
if [[ -f "$CONF/config.toml" ]] && ! cmp -s "$STAGE/config.toml" "$CONF/config.toml"; then
    cp -a "$CONF/config.toml" "$CONF/config.toml.before-$(date +%Y%m%d-%H%M%S)"
fi
install -m 640 -o root -g nowplaying "$STAGE/config.toml" "$CONF/config.toml"
umask 077
cat > "$CONF/secrets.env" <<EOF
# Secrets for the Now Playing daemon, read by systemd. One NAME=value per line.
NOWPLAYING_MQTT_USERNAME=$MQTT_USER
NOWPLAYING_MQTT_PASSWORD=$MQTT_PASS
EOF
chmod 600 "$CONF/secrets.env"; chown root:root "$CONF/secrets.env"
umask 022
install -d -m 750 -o nowplaying -g nowplaying /var/lib/now-playing
echo "wrote $CONF/config.toml and $CONF/secrets.env"

step "4/5 doctor (the daemon checks its own installation)"
runuser -u nowplaying -- env NOWPLAYING_MQTT_USERNAME="$MQTT_USER" NOWPLAYING_MQTT_PASSWORD="$MQTT_PASS" \
    "$PY" "$APP" -c "$CONF/config.toml" doctor
echo "doctor exit code: $?  (a warning here does not stop the setup)"

step "5/5 start the service"
systemctl daemon-reload
systemctl enable now-playing >/dev/null 2>&1
systemctl restart now-playing || die "now-playing did not start"
sleep 12
systemctl --no-pager --lines=0 status now-playing || true
echo "--- last log lines"
journalctl -u now-playing --no-pager -n 25 -o cat || true
systemctl is-active --quiet now-playing || die "now-playing is not running"

# the staged copy of the password is no longer needed
shred -u "$STAGE/mqtt_password" 2>/dev/null || rm -f "$STAGE/mqtt_password"
exit 0
