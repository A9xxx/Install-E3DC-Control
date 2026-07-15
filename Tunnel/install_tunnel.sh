#!/bin/bash
# Cloudflare Tunnel Setup für RPi 5 (Hauptgerät)
set -e

echo "--- Update & Installation von cloudflared ---"
sudo apt-get update && sudo apt-get install -y curl wget

# Download für ARM64 (Standard für Pi 5)
wget -O cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared.deb
rm cloudflared.deb

echo "--- Installation abgeschlossen ---"
echo "Nächste Schritte:"
echo "1. 'cloudflared tunnel login' ausführen."
echo "2. 'cloudflared tunnel create mein-tunnel' ausführen."
echo "3. Die Tunnel-ID in die /etc/cloudflared/config.yml eintragen."