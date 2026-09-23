#!/usr/bin/env bash
# Primo Panel setup — installs the config-driven miner service + the web
# control/monitor panel on an ARM SBC (Orange Pi / Pi / Termux-less Linux).
# Run as root:  sudo ./setup.sh
#
# Optional environment overrides:
#   MINER_USER=orangepi      user the miner runs as (default: $SUDO_USER or orangepi)
#   PRIMO_BIN=/path/to/bin   install this primo-arm-miner binary to /usr/local/bin
#   PANEL_PASS=secret        basic-auth password for the panel (default: changeme)
#   POOL_URL / POOL_USER / POOL_PASS / ALGO / THREADS   seed config.json values
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo ./setup.sh)"; exit 1; }
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MINER_USER="${MINER_USER:-${SUDO_USER:-orangepi}}"
PANEL_PASS="${PANEL_PASS:-changeme}"
BIN=/usr/local/bin/primo-arm-miner

echo "==> Dependencies (libcurl, libjansson, python3)"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y libcurl4 libjansson4 python3 >/dev/null
fi

echo "==> Miner binary"
# Default to the binary bundled in this kit (bin/primo-arm-miner) if the caller
# didn't point PRIMO_BIN elsewhere. It's ARMv8 (armv8-a+crypto) so it runs on
# any ARMv8 board (H618 Zero2W/Zero3, RK3588, Pi 4/5, etc.).
: "${PRIMO_BIN:=$SRC/bin/primo-arm-miner}"
if [ -f "$PRIMO_BIN" ]; then
  install -m 755 "$PRIMO_BIN" "$BIN"
fi
if [ ! -x "$BIN" ]; then
  echo "ERROR: $BIN not found. Copy the primo-arm-miner binary there (or pass PRIMO_BIN=/path)."
  echo "       e.g. build it from source or scp it from an existing miner."
  exit 1
fi

echo "==> Config at /etc/primo/config.json"
mkdir -p /etc/primo
if [ ! -f /etc/primo/config.json ]; then
  cp "$SRC/config.example.json" /etc/primo/config.json
  # Seed values from env if provided (best-effort sed on the example scaffold).
  [ -n "${ALGO:-}" ]      && sed -i "s#\"algo\": \"[^\"]*\"#\"algo\": \"$ALGO\"#" /etc/primo/config.json
  [ -n "${POOL_URL:-}" ]  && sed -i "s#\"url\": \"[^\"]*\"#\"url\": \"$POOL_URL\"#" /etc/primo/config.json
  [ -n "${POOL_USER:-}" ] && sed -i "s#\"user\": \"[^\"]*\"#\"user\": \"$POOL_USER\"#" /etc/primo/config.json
  [ -n "${POOL_PASS:-}" ] && sed -i "s#\"pass\": \"[^\"]*\"#\"pass\": \"$POOL_PASS\"#" /etc/primo/config.json
  [ -n "${THREADS:-}" ]   && sed -i "s#\"threads\": [0-9]*#\"threads\": $THREADS#" /etc/primo/config.json
  echo "    wrote /etc/primo/config.json (api-bind already 0.0.0.0:4068)"
else
  echo "    exists — left as-is"
fi
chmod 644 /etc/primo/config.json

echo "==> Panel to /opt/primo-panel"
mkdir -p /opt/primo-panel
install -m 755 "$SRC/primo-panel.py" /opt/primo-panel/primo-panel.py

echo "==> systemd units"
sed "s/__MINER_USER__/$MINER_USER/" "$SRC/primo-miner.service" > /etc/systemd/system/primo-miner.service
sed "s/^Environment=PANEL_PASS=.*/Environment=PANEL_PASS=$PANEL_PASS/" "$SRC/primo-panel.service" > /etc/systemd/system/primo-panel.service
systemctl daemon-reload
systemctl enable --now primo-miner.service
systemctl enable --now primo-panel.service

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Done."
echo "  Miner:  systemctl status primo-miner"
echo "  Panel:  http://${IP:-<device-ip>}:8088   (login: admin / $PANEL_PASS)"
[ "$PANEL_PASS" = "changeme" ] && echo "  WARNING: set a real PANEL_PASS (re-run with PANEL_PASS=... or edit the unit)."
