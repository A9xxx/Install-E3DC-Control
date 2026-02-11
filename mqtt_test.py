#!/usr/bin/env python3
"""
E3DC-Control MQTT Verbindungstester

Testet die Verbindung zu einem MQTT Broker und empfängt Test-Messages.
"""

import sys
import time
import socket
import re


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


def test_socket_connection(ip, port):
    """Testet die Socket-Verbindung (TCP)."""
    print(f"\n→ Teste TCP-Verbindung zu {ip}:{port}…")
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((ip, int(port)))
        sock.close()
        
        if result == 0:
            print(f"✓ TCP-Verbindung erfolgreich!")
            return True
        else:
            print(f"✗ Keine TCP-Verbindung möglich!")
            print(f"  Überprüfe:")
            print(f"  - IP-Adresse: {ip}")
            print(f"  - Port: {port}")
            print(f"  - Netzwerk-Verbindung")
            print(f"  - Firewall-Einstellungen")
            return False
    except socket.gaierror:
        print(f"✗ Fehler: Hostname '{ip}' konnte nicht aufgelöst werden!")
        return False
    except socket.error as e:
        print(f"✗ Socket-Fehler: {e}")
        return False
    except Exception as e:
        print(f"✗ Fehler: {e}")
        return False


def test_mqtt_connection(ip, port, topic=None, timeout=5):
    """Testet die MQTT-Verbindung und empfängt optional Messages."""
    print(f"\n→ Teste MQTT-Verbindung zu {ip}:{port}…")
    
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("✗ Fehler: paho-mqtt ist nicht installiert!")
        print("  Installiere mit: sudo apt-get install -y python3-paho-mqtt")
        return False
    
    # Callback-Funktionen
    connected = False
    messages = []
    
    def on_connect(client, userdata, flags, rc):
        nonlocal connected
        if rc == 0:
            connected = True
            print(f"✓ Mit MQTT Broker verbunden!")
            if topic:
                print(f"→ Abonniere Topic: {topic}")
                client.subscribe(topic)
        else:
            print(f"✗ Verbindungsfehler: Returncode {rc}")
            error_messages = {
                1: "Ungültige Protokollversion",
                2: "Ungültige Client ID",
                3: "Broker nicht verfügbar",
                4: "Ungültige Benutzerdaten",
                5: "Nicht autorisiert"
            }
            print(f"  {error_messages.get(rc, 'Unbekannter Fehler')}")
    
    def on_message(client, userdata, msg):
        messages.append({
            'topic': msg.topic,
            'payload': msg.payload.decode('utf-8'),
            'qos': msg.qos,
            'retain': msg.retain
        })
        print(f"✓ Message empfangen:")
        print(f"  Topic: {msg.topic}")
        print(f"  Payload: {msg.payload.decode('utf-8')}")
        print(f"  QoS: {msg.qos}, Retain: {msg.retain}")
    
    def on_disconnect(client, userdata, rc):
        if rc != 0:
            print(f"⚠ Unerwartete Trennung: {rc}")
    
    # Erstelle Client
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.DEFAULT)
        client.on_connect = on_connect
        client.on_message = on_message
        client.on_disconnect = on_disconnect
        
        # Verbinde
        print(f"→ Verbinde zu {ip}:{port}…")
        client.connect(ip, int(port), keepalive=10)
        
        # Starte Loop mit Timeout
        client.loop_start()
        
        # Warte auf Verbindung
        wait_time = 0
        while not connected and wait_time < timeout:
            time.sleep(0.5)
            wait_time += 0.5
        
        if not connected:
            print(f"✗ Verbindung zum MQTT Broker fehlgeschlagen!")
            client.loop_stop()
            return False
        
        # Warte auf Messages (wenn Topic angegeben)
        if topic:
            print(f"→ Warte {timeout-1} Sekunden auf Messages…")
            time.sleep(timeout - 1)
            
            if messages:
                print(f"\n✓ {len(messages)} Message(s) empfangen!")
            else:
                print(f"⚠ Keine Messages empfangen.")
                print(f"  Prüfe ob das Topic '{topic}' korrekt ist.")
        
        client.loop_stop()
        client.disconnect()
        return True
    
    except connectivity.error.BrokerConnectError:
        print(f"✗ Fehler: Kann sich nicht mit dem Broker verbinden!")
        return False
    except Exception as e:
        print(f"✗ Fehler: {e}")
        return False


def get_test_config():
    """Interaktive Abfrage der Test-Parameter."""
    print("\n" + "="*50)
    print("  MQTT Verbindungstester")
    print("="*50 + "\n")
    
    # IP-Adresse
    while True:
        ip = input("IP-Adresse des MQTT Brokers (z.B. 192.168.178.100): ").strip()
        if validate_ip(ip):
            break
        print("✗ Ungültige IP-Adresse! Bitte versuche es erneut.")
    
    # Port
    port = input("MQTT Port (Standard: 1883) [Enter für Standard]: ").strip()
    if not port:
        port = "1883"
    
    try:
        int(port)
    except ValueError:
        print("✗ Port muss eine Zahl sein! Nutze Standard: 1883")
        port = "1883"
    
    # Topic (optional)
    topic = input("\nMQTT Topic zum Abhören (Optional, [Enter] zum Überspringen): ").strip()
    if not topic:
        topic = None
    
    return ip, port, topic


def main():
    """Hauptfunktion."""
    print("\n" + "="*50)
    print("  E3DC MQTT Verbindungstester")
    print("="*50)
    
    # Kommandozeilen-Argumente verarbeiten
    if len(sys.argv) > 1:
        ip = sys.argv[1]
        port = sys.argv[2] if len(sys.argv) > 2 else "1883"
        topic = sys.argv[3] if len(sys.argv) > 3 else None
    else:
        ip, port, topic = get_test_config()
    
    print(f"\n→ Teste MQTT-Verbindung:")
    print(f"  IP: {ip}")
    print(f"  Port: {port}")
    if topic:
        print(f"  Topic: {topic}")
    
    # Socket-Test
    if not test_socket_connection(ip, port):
        print("\n✗ TCP-Verbindung fehlgeschlagen!")
        print("→ MQTT-Test aus Sicherheitsgründen übersprungen.\n")
        sys.exit(1)
    
    # MQTT-Test
    if not test_mqtt_connection(ip, port, topic):
        print("\n✗ MQTT-Verbindung fehlgeschlagen!\n")
        sys.exit(1)
    
    print("\n" + "="*50)
    print("✓ Alle Tests erfolgreich!")
    print("="*50 + "\n")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n✗ Test unterbrochen.\n")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Fehler: {e}\n")
        import traceback
        traceback.print_exc()
        sys.exit(1)
