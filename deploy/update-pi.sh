#!/usr/bin/env bash
# Update the Now Playing daemon on the Pi Zero 2 W after ~/now_playing_display was refreshed
# from the laptop.
#
#   ssh -t admin@10.0.0.61 "sudo bash now_playing_display/deploy/update-pi.sh"
#
# What it does, in order:
#   1. deploy/install.sh: program files, Python packages (only if they changed), service file
#   2. makes /srv/now-playing/slides for the idle pictures. It belongs to you, not to root, so
#      pictures can be added or removed later without sudo
#   3. installs a staged config (~/np-stage/config.toml) if there is one; the old one is kept
#      beside it with the date in its name
#   4. copies staged pictures (~/np-stage/slides/*) into the slides folder
#   5. restarts the service and shows how it came up
#
# The broker and the secrets are not touched (deploy/setup-pi-zero.sh set those up).
# Everything it prints also goes to ~/np-update.log; it leaves ~/np-update.done or .failed.
set -uo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo: sudo bash $0" >&2
    exit 1
fi

RUN_USER="${SUDO_USER:-admin}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$RUN_HOME/np-stage"
LOG="$RUN_HOME/np-update.log"
CONF=/etc/now-playing
SLIDES=/srv/now-playing/slides

rm -f "$RUN_HOME/np-update.done" "$RUN_HOME/np-update.failed"
: > "$LOG"; chown "$RUN_USER": "$LOG"
exec > >(tee -a "$LOG") 2>&1

finish() {
    local code=$?
    if [[ $code -eq 0 ]]; then
        touch "$RUN_HOME/np-update.done"; chown "$RUN_USER": "$RUN_HOME/np-update.done"
        echo; echo "=== ALL DONE $(date '+%H:%M:%S') ==="
    else
        touch "$RUN_HOME/np-update.failed"; chown "$RUN_USER": "$RUN_HOME/np-update.failed"
        echo; echo "=== UPDATE FAILED (exit $code) at step: ${STEP:-?}  $(date '+%H:%M:%S') ==="
    fi
    sleep 1   # let tee flush
}
trap finish EXIT
step() { STEP="$1"; echo; echo "=== $1  ($(date '+%H:%M:%S'))"; }
die() { echo "ERROR: $*" >&2; exit 1; }

step "1/5 program files (deploy/install.sh)"
export DEBIAN_FRONTEND=noninteractive
bash "$SRC/deploy/install.sh" || die "install.sh failed"

step "2/5 folder for the idle pictures"
install -d -m 755 /srv/now-playing
install -d -m 755 -o "$RUN_USER" -g "$RUN_USER" "$SLIDES"
echo "$SLIDES belongs to $RUN_USER"

step "3/5 configuration"
if [[ -s "$STAGE/config.toml" ]]; then
    if [[ -f "$CONF/config.toml" ]] && cmp -s "$STAGE/config.toml" "$CONF/config.toml"; then
        echo "config unchanged"
    else
        [[ -f "$CONF/config.toml" ]] && \
            cp -a "$CONF/config.toml" "$CONF/config.toml.before-$(date +%Y%m%d-%H%M%S)"
        install -m 640 -o root -g nowplaying "$STAGE/config.toml" "$CONF/config.toml"
        echo "installed the staged config"
    fi
else
    echo "no staged config; keeping $CONF/config.toml"
fi

step "4/5 idle pictures"
if compgen -G "$STAGE/slides/*" > /dev/null; then
    cp -f "$STAGE"/slides/* "$SLIDES"/
    chown "$RUN_USER": "$SLIDES"/*
    chmod 644 "$SLIDES"/*
fi
echo "pictures in $SLIDES: $(find "$SLIDES" -maxdepth 1 -type f | wc -l)"
ls -1 "$SLIDES" | sed 's/^/   /'

step "5/5 restart the service"
systemctl daemon-reload
systemctl restart now-playing || die "now-playing did not start"
sleep 12
systemctl --no-pager --lines=0 status now-playing || true
echo "--- last log lines"
journalctl -u now-playing --no-pager -n 20 -o cat || true
systemctl is-active --quiet now-playing || die "now-playing is not running"
exit 0
