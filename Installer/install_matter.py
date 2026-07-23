import os
import shlex
import tempfile
import time

from .utils import (
    MATTER_SYSTEM_PACKAGES,
    command_exists,
    install_apt_package_transaction,
    run_command,
)
from .installer_config import get_install_path, get_install_user
from .logging_manager import get_or_create_logger, log_task_completed, log_error

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

    lock_path = os.path.join(matter_dir, "package-lock.json")
    if not os.path.isfile(lock_path):
        print("✗ Matter-Lockdatei fehlt; Installation wird abgebrochen.")
        return False

    # Matter-Abhängigkeiten sind optional und werden nur hier bewusst gemeinsam
    # installiert. Ein Solver- oder Installationsfehler stoppt vor Storage,
    # npm ci und jeder expliziten Serviceänderung.
    if not install_apt_package_transaction(
        MATTER_SYSTEM_PACKAGES,
        log_label="Matter-Systempakete",
    ):
        print("✗ Matter-Systempakete konnten nicht vollständig installiert werden.")
        return False

    missing_commands = [
        command for command in ("node", "npm", "avahi-publish-service")
        if not command_exists(command)
    ]
    if missing_commands:
        print(f"✗ Matter-Laufzeitprogramme fehlen: {', '.join(missing_commands)}")
        return False

    print("→ Installiere hashgebundene NPM-Pakete (npm ci)...")
    npm_result = run_command("npm ci --omit=dev --ignore-scripts", cwd=matter_dir)
    if not npm_result.get("success"):
        print("✗ Matter-Abhängigkeiten konnten nicht aus der Lockdatei installiert werden.")
        return False
    print("✓ NPM-Pakete installiert.")

    storage_result = run_command(
        "sudo install -d -m 700 "
        f"-o {shlex.quote(install_user)} -g www-data /var/www/html/data/matter-storage"
    )
    if not storage_result.get("success"):
        print("✗ Privater Matter-Storage konnte nicht vorbereitet werden.")
        return False

    enable_avahi = run_command("sudo systemctl enable avahi-daemon")
    start_avahi = run_command("sudo systemctl start avahi-daemon")
    active_avahi = run_command("systemctl is-active avahi-daemon")
    if not (
        enable_avahi.get("success")
        and start_avahi.get("success")
        and active_avahi.get("success")
        and active_avahi.get("stdout", "").strip() == "active"
    ):
        print("✗ avahi-daemon konnte nicht aktiviert und gestartet werden.")
        return False
    print("✓ avahi-daemon läuft.")

    print("→ Erstelle Systemd Service...")
    # Fix 2: avahi-daemon als Abhängigkeit eintragen, damit es immer vor der Bridge läuft
    # Fix 3: Sicherstellen dass Matter-Pakete korrekt gelinkt werden (Netzwerk + avahi)
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
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
            tmp.write(service_content)
            tmp_path = tmp.name

        service_steps = (
            (f"sudo mv {shlex.quote(tmp_path)} {shlex.quote(service_path)}", "Service-Datei installieren"),
            (f"sudo chmod 644 {shlex.quote(service_path)}", "Service-Rechte setzen"),
            ("sudo systemctl daemon-reload", "systemd neu laden"),
            ("sudo systemctl enable e3dc-matter-bridge", "Matter-Service aktivieren"),
            ("sudo systemctl restart e3dc-matter-bridge", "Matter-Service starten"),
        )
        for command, label in service_steps:
            result = run_command(command)
            if not result.get("success"):
                print(f"✗ {label} fehlgeschlagen.")
                return False

        # Check if active
        time.sleep(2)
        res = run_command("systemctl is-active e3dc-matter-bridge")
        if res['success'] and res['stdout'].strip() == "active":
            print("\n✓ Matter Bridge erfolgreich gestartet!")
            log_task_completed("Matter Bridge installiert und gestartet")
        else:
            print(f"\n⚠ Service eingerichtet, konnte aber nicht starten. Log: journalctl -u e3dc-matter-bridge")
            log_error("install_matter", "Matter Bridge failed to start properly")
            return False

    except Exception as e:
        print(f"✗ Fehler beim Erstellen des Services: {e}")
        log_error("install_matter", f"Fehler Service: {e}", e)
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return True
