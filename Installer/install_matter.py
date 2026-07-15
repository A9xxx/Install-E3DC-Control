import os
import tempfile
import time

from .core import register_command
from .utils import run_command, apt_install
from .installer_config import get_install_path, get_install_use
from .logging_manager import get_or_create_logger, log_task_completed, log_erro

INSTALL_PATH = get_install_path()
INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
logger = get_or_create_logger("install_matter")

def install_matter_bridge(headless=False):
    """Richtet die Matter Bridge als Systemd Service ein."""
    print("\n=== Smart Home Matter Bridge installieren ===\n")
    logger.info("Starte Matter Bridge Installation.")

    install_user = get_install_user()
    service_path = "/etc/systemd/system/e3dc-matter-bridge.service"
    matter_dir = os.path.join(INSTALLER_DIR, "matter")

    if not os.path.exists(matter_dir):
        print(f"✗ Matter-Verzeichnis nicht gefunden: {matter_dir}")
        return False

    # avahi-utils wird für die lokale Matter-Erkennung benötigt.
    print("→ Prüfe avahi-utils (mDNS Proxy für Matter Discovery)...")
    if run_command("which avahi-publish-service")['success'] is False:
        print("→ Avahi nicht vollständig installiert, installiere...")
        apt_install("avahi-daemon")
        apt_install("avahi-utils")
        apt_install("dbus")
    run_command("sudo systemctl enable avahi-daemon")
    run_command("sudo systemctl start avahi-daemon")
    print("✓ avahi-daemon läuft.")

    print("→ Prüfe Node.js Abhängigkeiten...")
    if run_command("which npm")['success'] is False:
        print("→ npm ist nicht installiert, installiere nodejs und npm...")
        apt_install("nodejs")
        apt_install("npm")

        if run_command("which npm")['success'] is False:
            print("✗ Installation von npm fehlgeschlagen!")
            return False

    print("→ Installiere fehlende NPM-Pakete (npm install)...")
    run_command("npm install", cwd=matter_dir)
    print("✓ NPM-Pakete installiert.")

    print("→ Erstelle Systemd Service...")
    # Avahi muss vor der Bridge bereitstehen.
    service_content = f"""[Unit]
Description=E3DC Matter Bridge
After=network-online.target avahi-daemon.service
Wants=avahi-daemon.service
Requires=network-online.target

[Service]
Type=simple
User={install_user}
Group=www-data
WorkingDirectory={matter_dir}
ExecStart=/usr/bin/npm run start
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
    try:
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
            tmp.write(service_content)
            tmp_path = tmp.name

        run_command(f"sudo mv {tmp_path} {service_path}")
        run_command(f"sudo chmod 644 {service_path}")
        run_command("sudo systemctl daemon-reload")
        run_command("sudo systemctl enable e3dc-matter-bridge")
        run_command("sudo systemctl restart e3dc-matter-bridge")

        # Check if active
        time.sleep(2)
        res = run_command("systemctl is-active e3dc-matter-bridge")
        if res['success'] and res['stdout'].strip() == "active":
            print("\n✓ Matter Bridge erfolgreich gestartet!")
            log_task_completed("Matter Bridge installiert und gestartet")
        else:
            print(f"\n⚠ Service eingerichtet, konnte aber nicht starten. Log: journalctl -u e3dc-matter-bridge")
            log_error("install_matter", "Matter Bridge failed to start properly")

    except Exception as e:
        print(f"✗ Fehler beim Erstellen des Services: {e}")
        log_error("install_matter", f"Fehler Service: {e}", e)
        return False

    return True

# Untergruppe für Smart Home / Matte
register_command("45", "Smart Home Matter Bridge (Beta)", install_matter_bridge, sort_order=45)
