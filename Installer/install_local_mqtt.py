import os
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
    
    choice = input("Möchtest du Mosquitto jetzt installieren? (j/n): ").strip().lower()
    if choice != 'j':
        print("Abbruch.")
        return

    print("\n→ Installiere Mosquitto-Pakete (via apt)...")
    logger.info("Starte Mosquitto Installation.")
    
    run_command("sudo apt-get update")
    res = run_command("sudo DEBIAN_FRONTEND=noninteractive apt-get install -y mosquitto mosquitto-clients")
    
    if not res['success']:
        print(f"✗ Fehler bei der Installation: {res['stderr']}")
        log_error("mqtt_broker", f"Apt-Fehler: {res['stderr']}")
        return

    print("✓ Pakete erfolgreich installiert.")

    print("→ Konfiguriere Mosquitto (Netzwerkzugriff erlauben)...")
    # Ab Mosquitto 2.0 muss der externe Zugriff explizit erlaubt werden
    conf_content = "listener 1883\nallow_anonymous true\n"
    
    try:
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            tmp.write(conf_content)
            tmp_name = tmp.name
        
        run_command(f"sudo mv {tmp_name} /etc/mosquitto/conf.d/e3dc_local.conf")
        run_command("sudo chown root:root /etc/mosquitto/conf.d/e3dc_local.conf")
        run_command("sudo chmod 644 /etc/mosquitto/conf.d/e3dc_local.conf")
        
        print("✓ Konfiguration erstellt (/etc/mosquitto/conf.d/e3dc_local.conf).")
    except Exception as e:
        print(f"✗ Fehler bei der Konfiguration: {e}")
        log_error("mqtt_broker", f"Config-Fehler: {e}")
        return

    print("→ Starte Mosquitto neu...")
    run_command("sudo systemctl enable mosquitto")
    run_command("sudo systemctl restart mosquitto")
    
    print("\n✓ Lokaler MQTT-Broker (Mosquitto) ist nun aktiv und läuft auf Port 1883!")
    log_task_completed("Lokaler MQTT-Broker installiert")

register_command("47", "Lokalen MQTT-Broker (Mosquitto)", install_local_mqtt_menu, sort_order=47)
