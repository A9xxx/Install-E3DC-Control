"""
VitalStats - Ermittelt detaillierte Batteriedaten vom E3DC
Verwendet nativ den rscp_client.py (Keine RSCPGui Abhängigkeit mehr!)
"""
from typing import Dict, Any
import json
import fcntl
import sys
import os
import argparse
import socket
import struct
import grp

# Füge das Verzeichnis dieses Skripts zum Pfad hinzu
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from rscp_client import fetch_battery_vitals


def _set_www_data_shared(path, mode):
    try:
        os.chown(path, -1, grp.getgrnam("www-data").gr_gid)
    except OSError:
        pass
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def open_shared_lock_file():
    """Open a lock file shared by PHP/www-data and Python services."""
    candidates = [
        "/var/www/html/tmp/vital_stats.lock",
        "/tmp/e3dc_vital_stats.lock",
    ]
    last_error = None
    for lock_file in candidates:
        lock_dir = os.path.dirname(lock_file)
        if not os.path.isdir(lock_dir):
            continue
        try:
            fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o664)
            _set_www_data_shared(lock_file, 0o664)
            return os.fdopen(fd, "w"), lock_file
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("Kein beschreibbarer Lock-Pfad fuer vital_stats.py gefunden")

def resolve_credentials():
    """Liest Verbindungs-Credentials aus e3dc_v4.json (Single Source of Truth)."""
    cfg = {}

    if os.path.exists("/var/www/html/data/e3dc_v4.json"):
        try:
            with open("/var/www/html/data/e3dc_v4.json", "r", encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    cfg[k.lower()] = str(v)
        except: pass

    E3DC_IP       = cfg.get("server_ip", "")
    E3DC_PORT     = int(cfg.get("server_port", "5033"))
    E3DC_USER     = cfg.get("e3dc_user", "")
    E3DC_PASSWORD = cfg.get("e3dc_password", "")
    AES_PASSWORD  = cfg.get("aes_password", E3DC_PASSWORD) if cfg.get("aes_password") else E3DC_PASSWORD

    return E3DC_IP, E3DC_PORT, E3DC_USER, E3DC_PASSWORD, AES_PASSWORD

def collect_system_data() -> Dict[str, Any]:
    host, port, user, pw, rscp_pw = resolve_credentials()
    if not (host and user and pw and rscp_pw):
        return {"error": "Fehlende lokale RSCP-Verbindungsdaten in der E3DC-Konfiguration"}
    
    try:
        return fetch_battery_vitals(host, port, user, pw, rscp_pw)
    except Exception as e:
        return {"error": f"RSCP Verbindungsfehler: {str(e)}"}

def handle_socket_request():
    """Erlaubt via UNIX-Socket /tmp/e3dc_vital_socket das antriggern des Skripts wie eba's Daemon."""
    socket_path = '/tmp/e3dc_vital_socket'
    if os.path.exists(socket_path):
        os.remove(socket_path)
    
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    _set_www_data_shared(socket_path, 0o660)
    server.listen(1)

    print(f"Lausche auf {socket_path}...")
    try:
        while True:
            conn, addr = server.accept()
            with conn:
                data = collect_system_data()
                response = json.dumps(data)
                
                # Als Länge + JSON zurückgeben (für einfache PHP Integration)
                header = struct.pack('!I', len(response))
                conn.sendall(header + response.encode('utf-8'))
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        if os.path.exists(socket_path):
            os.remove(socket_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help="Nur einmal Daten holen und als JSON ausgeben")
    parser.add_argument('--socket', action='store_true', help="Als Socket-Server laufen")
    args = parser.parse_args()

    # Einzige Instanz sicherstellen via FileLock (Keine parallelen E3DC Logins!)
    lock_fp, lock_file = open_shared_lock_file()
    try:
        fcntl.lockf(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        if args.socket:
             print("Dienst läuft bereits (Lock aktiv).")
             sys.exit(1)

    if args.socket:
        handle_socket_request()
    else:
        # Standard: Nur einmal ausführen und ausgeben
        data = collect_system_data()
        print(json.dumps(data, indent=4, ensure_ascii=False))
