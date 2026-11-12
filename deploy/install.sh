#!/usr/bin/env bash
# Install or update the Now Playing daemon on a Debian-family host
# (written for the Home Assistant Pi: Debian 12, aarch64, Python 3.11).
#
#   sudo deploy/install.sh
#
# It installs ffmpeg, creates a "nowplaying" system user, copies server/ to
# /opt/now-playing, builds a virtual environment there, and installs (but does
# not start) the systemd unit. It never overwrites an existing config.
# Safe to run again after pulling an update.
#
# NOT YET RUN ON THE PI: read it through before the first use.
set -euo pipefail

PREFIX=/opt/now-playing
CONF=/etc/now-playing
STATE=/var/lib/now-playing
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "run with sudo: sudo $0" >&2
    exit 1
fi
if [[ ! -f "$SRC/server/audio_recognition_daemon.py" ]]; then
    echo "cannot find server/ next to this script (looked in $SRC)" >&2
    exit 1
fi

echo "== system packages"
# (the lock timeout waits out a first-boot or unattended upgrade instead of failing on it)
apt-get -o DPkg::Lock::Timeout=300 update -qq
apt-get -o DPkg::Lock::Timeout=300 install -y --no-install-recommends python3 python3-venv ffmpeg alsa-utils

echo "== user"
if ! id -u nowplaying >/dev/null 2>&1; then
    useradd --system --home-dir "$PREFIX" --shell /usr/sbin/nologin --groups audio nowplaying
fi

echo "== program files -> $PREFIX"
install -d -m 755 "$PREFIX" "$CONF"
rm -rf "$PREFIX/server.new"
cp -a "$SRC/server" "$PREFIX/server.new"
rm -rf "$PREFIX/server.new/tests" "$PREFIX/server.new/data" "$PREFIX/server.new/.pytest_cache"
find "$PREFIX/server.new" -name __pycache__ -type d -prune -exec rm -rf {} +
rm -rf "$PREFIX/server.old"
[[ -d "$PREFIX/server" ]] && mv "$PREFIX/server" "$PREFIX/server.old"
mv "$PREFIX/server.new" "$PREFIX/server"
rm -rf "$PREFIX/server.old"
chown -R root:root "$PREFIX/server"

echo "== Python environment"
if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
    python3 -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/server/requirements.txt"

echo "== configuration -> $CONF"
if [[ ! -f "$CONF/config.toml" ]]; then
    sed 's|^data_dir = "data" .*|data_dir = "'"$STATE"'"|' \
        "$SRC/server/config.example.toml" > "$CONF/config.toml"
    chown root:nowplaying "$CONF/config.toml"
    chmod 640 "$CONF/config.toml"
    echo "   created $CONF/config.toml (edit it: audio input, MQTT, http.public_base_url)"
else
    echo "   kept the existing $CONF/config.toml"
fi
if [[ ! -f "$CONF/secrets.env" ]]; then
    cat > "$CONF/secrets.env" <<'EOF'
# Secrets for the Now Playing daemon, read by systemd. One NAME=value per line.
#NOWPLAYING_MQTT_USERNAME=
#NOWPLAYING_MQTT_PASSWORD=
#NOWPLAYING_AUDD_API_TOKEN=
#NOWPLAYING_ACOUSTID_API_KEY=
EOF
    chmod 600 "$CONF/secrets.env"
    echo "   created $CONF/secrets.env (put the MQTT password there)"
fi

echo "== systemd unit"
install -m 644 "$SRC/deploy/now-playing.service" /etc/systemd/system/now-playing.service
systemctl daemon-reload

cat <<EOF

Installed. Nothing has been started. Next:

  1. sudoedit $CONF/config.toml         # audio input, [mqtt], http.public_base_url
  2. sudoedit $CONF/secrets.env         # NOWPLAYING_MQTT_PASSWORD=...
  3. Check it (reads the same secrets the service will):
       sudo systemd-run --pty --wait --collect -p User=nowplaying -p SupplementaryGroups=audio \\
            -p EnvironmentFile=$CONF/secrets.env -p StateDirectory=now-playing \\
            $PREFIX/venv/bin/python $PREFIX/server/audio_recognition_daemon.py -c $CONF/config.toml doctor
  4. sudo systemctl enable --now now-playing
  5. journalctl -u now-playing -f
EOF
