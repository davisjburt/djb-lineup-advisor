#!/usr/bin/env bash
# Install the DJB dashboard as a service on the Pi (Tailscale-only, read-only).
# Run as your normal user:  bash ~/fantasy-lineup/install_dashboard.sh
set -euo pipefail
DIR="$HOME/fantasy-lineup"
U="$(id -un)"

pkill -f "[d]ashboard.py --serve" || true   # stop any test copy

sudo tee /etc/systemd/system/fantasy-dashboard.service >/dev/null <<UNIT
[Unit]
Description=DJB dashboard (read-only Yahoo data, Tailscale only)
Wants=network-online.target
After=network-online.target tailscaled.service

[Service]
User=$U
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -W ignore $DIR/dashboard.py --serve
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now fantasy-dashboard.service
sleep 3
systemctl --no-pager --lines=3 status fantasy-dashboard.service | head -5
echo
echo "Dashboard: http://$(tailscale ip -4 | head -1):8090  (any device signed in to your Tailscale)"
