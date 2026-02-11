"""
E3DC-Control openWB MQTT Integration

Installiert MQTT und konfiguriert die openWB Wallbox Integration.
"""

import os
import re
import subprocess
from .core import register_command
from .utils import run_command


CONFIG_FILE = os.path.expanduser("~/E3DC-Control/e3dc.config.txt")
MQTT_PACKAGES = ["mosquitto-clients", "python3-paho-mqtt"]


def install_mqtt():
    """Installiert MQTT-Pakete."""
    print("→ Installiere MQTT-Pakete…")
    print("  Dies kann einige Minuten dauern…\n")
    
    for package in MQTT_PACKAGES:
        try:
            result = run_command(f"apt-get install -y {package}", timeout=300)
            if result['success']:
                print(f"  ✓ {package} installiert")
            else:
                print(f"  ✗ Fehler bei {package}: {result['stderr']}")
                return False
        except Exception as e:
            print(f"  ✗ Fehler: {e}")
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
    
    # IP-Adresse des MQTT Brokers
    while True:
        ip = input("IP-Adresse der openWB Wallbox (z.B. 192.168.178.100): ").strip()
        if validate_ip(ip):
            break
        print("✗ Ungültige IP-Adresse! Bitte versuche es erneut.")
    
    print()
    
    # MQTT Topic
    while True:
        topic = input("MQTT Topic (Standard: openWB/internal_chargepoint/0/get/power): ").strip()
        if not topic:
            topic = "openWB/internal_chargepoint/0/get/power"
        if topic:
            break
        print("✗ Topic darf nicht leer sein!")
    
    print()
    
    # Port (optional)
    port = input("MQTT Port (Standard: 1883) [Enter für Standard]: ").strip()
    if not port:
        port = "1883"
    
    try:
        int(port)
    except ValueError:
        print("✗ Port muss eine Zahl sein! Nutze Standard: 1883")
        port = "1883"
    
    return {
        'ip': ip,
        'topic': topic,
        'port': port
    }


def update_config(config_data):
    """Aktualisiert die e3dc.config.txt mit MQTT-Einstellungen."""
    print("\n→ Aktualisiere Konfiguration…")
    
    if not os.path.exists(CONFIG_FILE):
        print(f"✗ Fehler: Konfigurationsdatei {CONFIG_FILE} nicht gefunden!")
        print("  Stellen Sie sicher, dass E3DC-Control bereits installiert ist.")
        return False
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            content = f.read()
        
        # Definiere die neuen Konfigurationseinträge
        wb_ip_line = f"WB_ip={config_data['ip']}\n"
        wb_topic_line = f"WB_topic={config_data['topic']}\n"
        wb_port_line = f"WB_port={config_data['port']}\n"
        
        # Prüfe und aktualisiere/füge hinzu
        if 'WB_ip=' in content:
            content = re.sub(r'WB_ip=.*\n', wb_ip_line, content)
        else:
            content += wb_ip_line
        
        if 'WB_topic=' in content:
            content = re.sub(r'WB_topic=.*\n', wb_topic_line, content)
        else:
            content += wb_topic_line
        
        if 'WB_port=' in content:
            content = re.sub(r'WB_port=.*\n', wb_port_line, content)
        else:
            content += wb_port_line
        
        # Schreibe zurück
        with open(CONFIG_FILE, 'w') as f:
            f.write(content)
        
        print("✓ Konfiguration aktualisiert:")
        print(f"  WB_ip = {config_data['ip']}")
        print(f"  WB_topic = {config_data['topic']}")
        print(f"  WB_port = {config_data['port']}")
        
        return True
    
    except Exception as e:
        print(f"✗ Fehler beim Aktualisieren der Konfiguration: {e}")
        return False


def test_mqtt_connection(ip, port):
    """Testet die Verbindung zum MQTT Broker."""
    print("\n→ Teste MQTT-Verbindung…")
    
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((ip, int(port)))
        sock.close()
        
        if result == 0:
            print(f"✓ Verbindung zum MQTT Broker ({ip}:{port}) erfolgreich!")
            return True
        else:
            print(f"✗ Keine Verbindung zum MQTT Broker ({ip}:{port})!")
            print("  Bitte prüfe IP-Adresse und Port.")
            return False
    except Exception as e:
        print(f"⚠ Fehler beim Testen: {e}")
        return False


def setup_openwb_mqtt():
    """Hauptfunktion für openWB MQTT Setup."""
    print("\n" + "="*50)
    print("  openWB MQTT Integration")
    print("="*50 + "\n")
    
    print("Dieses Modul installiert MQTT und konfiguriert")
    print("die Integration mit einer openWB Wallbox.\n")
    
    # MQTT installieren
    if not install_mqtt():
        print("\n✗ MQTT-Installation fehlgeschlagen!")
        return
    
    print("\n✓ MQTT installiert\n")
    
    # Konfiguration abfragen
    config = get_mqtt_config()
    
    # Verbindung testen
    test_success = test_mqtt_connection(config['ip'], config['port'])
    
    if not test_success:
        choice = input("\nFortfahren trotzdem? (j/n): ").strip().lower()
        if choice != "j":
            print("→ Setup abgebrochen.")
            return
    
    # Konfiguration speichern
    if update_config(config):
        print("\n" + "="*50)
        print("✓ openWB MQTT Integration erfolgreich eingerichtet!")
        print("="*50 + "\n")
        print("Nächste Schritte:")
        print("1. Starten Sie E3DC-Control neu")
        print("2. Die Wallbox-Daten werden jetzt über MQTT gelesen")
        print("3. Überprüfen Sie die Logs für mögliche Fehler\n")
    else:
        print("\n✗ Fehler beim Speichern der Konfiguration!")


# Registriere als Menü-Befehl
register_command("16", "openWB MQTT Integration einrichten", setup_openwb_mqtt, sort_order=160)
