#!/bin/bash

# Cloudflare Tunnel Setup Script für Raspberry Pi
# Nutzung: sudo bash setup_tunnel.sh

set -e

echo "--- Starte Cloudflare Tunnel Installation ---"

# 1. System-Update und Abhängigkeiten
apt-get update && apt-get install -y curl wget

# 2. Download und Installation von cloudflared (ARM-Architektur für RPi)
ARCH=$(dpkg --print-architecture)
if [[ "$ARCH" == "armhf" ]]; then
    wget -O cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-armhf.deb
elif [[ "$ARCH" == "arm64" ]]; then
    wget -O cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
fi

dpkg -i cloudflared.deb
rm cloudflared.deb

echo "--- cloudflared erfolgreich installiert ---"
echo "Bitte führe jetzt 'cloudflared tunnel login' aus, um dich zu authentifizieren."
echo "Danach kannst du einen Tunnel mit 'cloudflared tunnel create <NAME>' erstellen."