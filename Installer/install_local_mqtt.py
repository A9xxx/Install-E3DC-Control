import os
import shlex
import tempfile
from .core import register_command
from .utils import run_command
from .logging_manager import get_or_create_logger, log_task_completed, log_error
from .installer_config import get_install_path

logger = get_or_create_logger("mqtt_broker")

def install_local_mqtt_menu():
    print("\n=== Lokalen MQTT-Broker (Mosquitto) installieren ===")
    print("Dies installiert einen leichtgewichtigen MQTT-Broker direkt auf diesem Raspberry Pi.")
    print("Perfekt als zentrale Schnittstelle zwischen EVCC und E3DC-Control.\n")
    
    print("Hinweis: Der Broker ist danach im lokalen Netz ohne Anmeldung erreichbar.")
    print("Jeder im Netz kann Werte und Fahrzeugdaten einspielen; diese Daten können in die Ladeplanung eingehen.")
    choice = input("Mosquitto jetzt mit anonymem LAN-Zugang installieren? (j/N): ").strip().lower()
    if choice != 'j':
        print("Abbruch.")
        return None

    print("\n→ Installiere Mosquitto-Pakete (via apt)...")
    logger.info("Starte Mosquitto Installation.")
    
    update_result = run_command("sudo apt-get update")
    if not isinstance(update_result, dict) or not update_result.get("success"):
        print("✗ Paketquellen konnten nicht aktualisiert werden.")
        return False
    res = run_command("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y mosquitto mosquitto-clients")
    
    if not isinstance(res, dict) or not res.get('success'):
        print(f"✗ Fehler bei der Installation: {(res or {}).get('stderr', '')}")
        log_error("mqtt_broker", f"Apt-Fehler: {(res or {}).get('stderr', '')}")
        return False

    print("✓ Pakete erfolgreich installiert.")

    print("→ Konfiguriere Mosquitto (Netzwerkzugriff erlauben)...")
    # Ab Mosquitto 2.0 muss der externe Zugriff explizit erlaubt werden
    conf_content = "listener 1883\nallow_anonymous true\n"
    
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            tmp.write(conf_content)
            tmp_name = tmp.name
        
        for command in (
            f"sudo install -o root -g root -m 644 {shlex.quote(tmp_name)} /etc/mosquitto/conf.d/e3dc_local.conf",
        ):
            result = run_command(command)
            if not isinstance(result, dict) or not result.get("success"):
                raise RuntimeError("Broker-Konfiguration konnte nicht installiert werden")
        
        print("✓ Konfiguration erstellt (/etc/mosquitto/conf.d/e3dc_local.conf).")
    except Exception as e:
        print(f"✗ Fehler bei der Konfiguration: {e}")
        log_error("mqtt_broker", f"Config-Fehler: {e}")
        return False

    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)

    print("→ Starte Mosquitto neu...")
    for command in ("sudo systemctl enable mosquitto", "sudo systemctl restart mosquitto", "systemctl is-active mosquitto"):
        result = run_command(command)
        if not isinstance(result, dict) or not result.get("success"):
            print("✗ Mosquitto konnte nicht vollständig aktiviert werden. Nächster Schritt: Hauptmenü 2) Systemstatus anzeigen öffnen.")
            return False
    
    print("\n✓ Lokaler MQTT-Broker (Mosquitto) ist nun aktiv und läuft auf Port 1883!")
    log_task_completed("Lokaler MQTT-Broker installiert")
    return True

register_command("47", "Lokalen MQTT-Broker (Mosquitto)", install_local_mqtt_menu, sort_order=47)
