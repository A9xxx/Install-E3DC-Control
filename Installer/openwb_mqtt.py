"""
E3DC-Control openWB MQTT Integration

Installiert MQTT und konfiguriert die openWB Wallbox Integration.
"""

import os
import re
import subprocess
from .core import register_command
from .utils import run_command
from .installer_config import get_install_path
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning
from .config_secret_permissions import apply_config_secret_permissions


V4_CONFIG_FILE = "/var/www/html/data/e3dc_v4.json"
MQTT_PACKAGES = ["mosquitto-clients", "python3-paho-mqtt"]
openwb_logger = get_or_create_logger("openwb")


def install_mqtt():
    """Installiert MQTT-Pakete."""
    print("→ Installiere MQTT-Pakete…")
    print("  Dies kann einige Minuten dauern…\n")
    openwb_logger.info("Starte Installation der MQTT-Pakete.")
    
    for package in MQTT_PACKAGES:
        try:
            result = run_command(f"apt-get install -y {package}", timeout=300)
            if result['success']:
                print(f"  ✓ {package} installiert")
                openwb_logger.info(f"Paket '{package}' erfolgreich installiert.")
            else:
                print(f"  ✗ Fehler bei {package}: {result['stderr']}")
                log_error("openwb_mqtt", f"Fehler bei der Installation von {package}: {result['stderr']}")
                return False
        except Exception as e:
            print(f"  ✗ Fehler: {e}")
            log_error("openwb_mqtt", f"Exception bei der Installation von {package}: {e}", e)
            return False
    
    return True


def validate_ip(ip):
    """Validiert eine IP-Adresse."""
    pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
    if not re.match(pattern, ip):
        return False
    
    parts = ip.split('.')
    for part in parts:
        try:
            if int(part) > 255:
                return False
        except ValueError:
            return False
    
    return True


def get_mqtt_config():
    """Interaktive Abfrage der MQTT-Einstellungen."""
    print("\n" + "="*50)
    print("  openWB MQTT Integration Setup")
    print("="*50 + "\n")
    openwb_logger.info("Starte interaktive MQTT-Konfiguration.")
    
    # IP-Adresse des MQTT Brokers
    while True:
        ip = input("IP-Adresse der openWB Wallbox (z.B. 192.168.178.100): ").strip()
        if validate_ip(ip):
            break
        print("✗ Ungültige IP-Adresse! Bitte versuche es erneut.")
        log_warning("openwb_mqtt", f"Ungültige IP-Adresse eingegeben: {ip}")
    
    print()
    
    # MQTT Topic
    while True:
        topic = input("MQTT Topic (Standard: openWB/internal_chargepoint/0/get/power): ").strip()
        if not topic:
            topic = "openWB/internal_chargepoint/0/get/power"
        if topic:
            break
        print("✗ Topic darf nicht leer sein!")
        log_warning("openwb_mqtt", "Leeres MQTT-Topic eingegeben.")
    
    print()
    
    # Port (optional)
    port = input("MQTT Port (Standard: 1883) [Enter für Standard]: ").strip()
    if not port:
        port = "1883"
    
    try:
        int(port)
    except ValueError:
        print("✗ Port muss eine Zahl sein! Nutze Standard: 1883")
        log_warning("openwb_mqtt", f"Ungültiger Port eingegeben: {port}. Fallback auf 1883.")
        port = "1883"
    
    config_data = {
        'ip': ip,
        'topic': topic,
        'port': port
    }
    openwb_logger.info(f"MQTT-Konfiguration erhalten: IP={ip}, Topic={topic}, Port={port}")
    return config_data


def update_config(config_data):
    """Speichert MQTT-Einstellungen in e3dc_v4.json (Single Source of Truth)."""
    print("\n-> Aktualisiere Konfiguration...")
    openwb_logger.info("Speichere MQTT-Einstellungen in e3dc_v4.json.")

    if not os.path.exists(V4_CONFIG_FILE):
        print(f"[!] Fehler: {V4_CONFIG_FILE} nicht gefunden!")
        print("    Bitte zuerst die Grundkonfiguration vornehmen (Menuepunkt 8).")
        log_error("openwb_mqtt", f"e3dc_v4.json nicht gefunden: {V4_CONFIG_FILE}")
        return False

    try:
        import json
        with open(V4_CONFIG_FILE, 'r', encoding='utf-8') as f:
            v4_data = json.load(f)

        v4_data['wb_ip']    = config_data['ip']
        v4_data['wb_topic'] = config_data['topic']
        v4_data['wb_port']  = config_data['port']

        tmp = V4_CONFIG_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(v4_data, f, indent=4, ensure_ascii=False)
        os.replace(tmp, V4_CONFIG_FILE)
        apply_config_secret_permissions(V4_CONFIG_FILE, data=v4_data)

        print("[OK] Konfiguration gespeichert (e3dc_v4.json):")
        print(f"     wb_ip    = {config_data['ip']}")
        print(f"     wb_topic = {config_data['topic']}")
        print(f"     wb_port  = {config_data['port']}")
        openwb_logger.info("MQTT-Einstellungen erfolgreich in e3dc_v4.json gespeichert.")
        return True

    except Exception as e:
        print(f"[!] Fehler beim Speichern: {e}")
        log_error("openwb_mqtt", f"Fehler beim Speichern der MQTT-Einstellungen: {e}", e)
        return False


def test_mqtt_connection(ip, port):
    """Testet die Verbindung zum MQTT Broker."""
    print("\n→ Teste MQTT-Verbindung…")
    openwb_logger.info(f"Teste MQTT-Verbindung zu {ip}:{port}.")
    
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((ip, int(port)))
        sock.close()
        
        if result == 0:
            print(f"✓ Verbindung zum MQTT Broker ({ip}:{port}) erfolgreich!")
            openwb_logger.info(f"MQTT-Verbindung zu {ip}:{port} erfolgreich.")
            return True
        else:
            print(f"✗ Keine Verbindung zum MQTT Broker ({ip}:{port})!")
            print("  Bitte prüfe IP-Adresse und Port.")
            log_warning("openwb_mqtt", f"Keine Verbindung zum MQTT Broker ({ip}:{port}).")
            return False
    except Exception as e:
        print(f"⚠ Fehler beim Testen: {e}")
        log_warning("openwb_mqtt", f"Fehler beim Testen der MQTT-Verbindung: {e}")
        return False


def setup_openwb_mqtt():
    """Hauptfunktion für openWB MQTT Setup."""
    print("\n" + "="*50)
    print("  openWB MQTT Integration")
    print("="*50 + "\n")
    
    print("Dieses Modul installiert MQTT und konfiguriert")
    print("die Integration mit einer openWB Wallbox.\n")
    openwb_logger.info("Starte openWB MQTT Integration Setup.")
    
    # MQTT installieren
    if not install_mqtt():
        print("\n✗ MQTT-Installation fehlgeschlagen!")
        log_error("openwb_mqtt", "MQTT-Installation fehlgeschlagen.")
        return
    
    print("\n✓ MQTT installiert\n")
    log_task_completed("MQTT-Pakete installieren")
    
    # Konfiguration abfragen
    config = get_mqtt_config()
    
    # Verbindung testen
    test_success = test_mqtt_connection(config['ip'], config['port'])
    
    if not test_success:
        choice = input("\nFortfahren trotzdem? (j/n): ").strip().lower()
        if choice != "j":
            print("→ Setup abgebrochen.")
            openwb_logger.warning("Setup nach fehlgeschlagenem Verbindungstest abgebrochen.")
            return
    
    # Konfiguration speichern
    if update_config(config):
        print("\n" + "="*50)
        print("✓ openWB MQTT Integration erfolgreich eingerichtet!")
        print("="*50 + "\n")
        print("Naechste Schritte:")
        print("1. Starte den wallbox_manager neu: sudo systemctl restart e3dc-wallbox-manager")
        print("2. Die Wallbox-Daten werden jetzt ueber MQTT gelesen")
        print("3. Ueberpruefen Sie die Logs fuer moegliche Fehler\n")
        log_task_completed("openWB MQTT Integration")
    else:
        print("\n[!] Fehler beim Speichern der Konfiguration!")
        log_error("openwb_mqtt", "Fehler beim Speichern der MQTT-Einstellungen in e3dc_v4.json.")


# Registriere als Menü-Befehl
register_command("46", "openWB MQTT Integration", setup_openwb_mqtt, sort_order=46)
