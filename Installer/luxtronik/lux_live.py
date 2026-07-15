import websocket
import time
import json
import os
import re
import threading
import signal
import sys

LOGIN_PAYLOAD = "LOGIN;"

def handle_sigterm(signum, frame):
    print(f"[{time.strftime('%H:%M:%S')}] SIGTERM empfangen, melde ordentlich ab...")
    if 'ws' in globals():
        try:
            ws.close()
        except:
            pass
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)
RAMDISK_FILE = "/var/www/html/ramdisk/waermepumpe.json"

GLOBAL_WP_DATA = {}
ID_TO_NAME = {}  # NEU: Das "Gedächtnis" des Skripts für die Speicher-IDs

def _read_ip():
    # Liest luxtronik_ip ausschliesslich aus e3dc_v4.json (Single Source of Truth)
    v4_path = "/var/www/html/data/e3dc_v4.json"
    try:
        if os.path.exists(v4_path):
            with open(v4_path, 'r', encoding='utf-8-sig') as f:
                v4_data = json.load(f)
                for k, v in v4_data.items():
                    if k.strip().lower() == "luxtronik_ip":
                        return str(v).strip()
    except Exception: pass
    return "0.0.0.0"

def clean_value(val_str):
    """Macht aus '32.3°C' -> 32.3 und aus 'Aus' -> 0"""
    if not isinstance(val_str, str): return val_str
    if val_str == "Aus": return 0
    if val_str == "Ein": return 1
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*(?:°C|kW|kWh|W|Hz|bar|V|A|K|%|l/h|h|min)$", val_str.strip())
    if m:
        try: return float(m.group(1))
        except: pass
    return val_str

def extract_values_and_map_ids(items, result_dict, parent_name=""):
    """Liest die initiale Seite aus und baut das ID-zu-Name Gedächtnis auf."""
    global ID_TO_NAME
    for item in items:
        name = item.get("name", "").replace(":", "").strip()
        value = item.get("value")
        item_id = item.get("id")
        sub_items = item.get("items")
        
        # NEU: Kollisionen bei gleichen Namen verhindern!
        dict_key = name
        if name in ["Heizung", "Warmwasser", "Gesamt", "Schwimmbad"] and parent_name in ["Wärmemenge", "Leistungsaufnahme"]:
            dict_key = f"{parent_name}_{name}"
            
        if name and value is not None:
            result_dict[dict_key] = clean_value(value)
            if item_id:
                ID_TO_NAME[item_id] = dict_key  # Merkt sich z.B. "0xbc70d4" -> "Wärmemenge_Gesamt"
            
        if sub_items:
            # Den aktuellen Namen als "Elternteil" für die nächste Ebene mitgeben
            extract_values_and_map_ids(sub_items, result_dict, name if name else parent_name)

def update_values_from_refresh(items, result_dict):
    """Wertet die sekündlichen Updates aus, die nur noch aus IDs bestehen."""
    global ID_TO_NAME
    for item in items:
        item_id = item.get("id")
        value = item.get("value")
        sub_items = item.get("items")
        
        # Wenn wir die ID kennen, überschreiben wir den Wert in unseren Daten
        if item_id and value is not None and item_id in ID_TO_NAME:
            name = ID_TO_NAME[item_id]
            result_dict[name] = clean_value(value)
            
        if sub_items:
            update_values_from_refresh(sub_items, result_dict)

def find_id_by_name(items, target_name):
    """Sucht rekursiv im Navigationsbaum nach der tagesaktuellen ID eines Menüs."""
    if not isinstance(items, list): return None
    for item in items:
        if not isinstance(item, dict): continue
        if item.get("name") == target_name and "id" in item:
            return item["id"]
        if "items" in item:
            res = find_id_by_name(item["items"], target_name)
            if res: return res
    return None

def poll_data(ws, info_id):
    """Fragt die Wärmepumpe jetzt exakt so ab wie die originale Web UI: Jede Sekunde!"""
    ws.send(f"GET;{info_id}")
    while True:
        time.sleep(3) # Polling auf 3s entspannt, um Modbus nicht zu blockieren!
        try:
            ws.send("REFRESH")
        except:
            break

def save_to_ramdisk():
    """Speichert das Dictionary threadsicher in der Ramdisk."""
    global GLOBAL_WP_DATA
    if not GLOBAL_WP_DATA: return
    GLOBAL_WP_DATA['ts'] = time.time()
    tmp_file = RAMDISK_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(GLOBAL_WP_DATA, f, ensure_ascii=False)
    
    try:
        os.chmod(tmp_file, 0o664)
        import grp
        www_data_gid = grp.getgrnam("www-data").gr_gid
        os.chown(tmp_file, -1, www_data_gid)
    except: pass
    
    os.replace(tmp_file, RAMDISK_FILE)

def on_message(ws, message):
    global GLOBAL_WP_DATA
    try:
        data = json.loads(message)
        
        if isinstance(data, dict) and data.get("type") == "Navigation":
            info_id = find_id_by_name(data.get("items", []), "Informationen") or data.get("id")
            if info_id:
                print(f"[{time.strftime('%H:%M:%S')}] Login erfolgreich. Starte Live-Stream...")
                threading.Thread(target=poll_data, args=(ws, info_id), daemon=True).start()
            
        elif isinstance(data, dict) and data.get("type") == "Content":
            # 1. Initialer Ladevorgang (Erstellt das Wörterbuch)
            extract_values_and_map_ids(data.get("items", []), GLOBAL_WP_DATA)
            save_to_ramdisk()
            print(f"[{time.strftime('%H:%M:%S')}] Mapping erstellt: {len(GLOBAL_WP_DATA)} Sensoren erkannt.")
            
        elif isinstance(data, dict) and data.get("type") == "values":
            # 2. Sekündliche Updates von der Luxtronik verarbeiten!
            update_values_from_refresh(data.get("items", []), GLOBAL_WP_DATA)
            save_to_ramdisk()
                
    except json.JSONDecodeError:
        pass

def on_error(ws, error):
    print(f"Fehler: {error}")

def on_close(ws, close_status_code, close_msg):
    print("Verbindung zur Luxtronik getrennt.")

def on_open(ws):
    print("Verbindung steht! Sende Login...")
    ws.send(LOGIN_PAYLOAD)

if __name__ == "__main__":
    ip = _read_ip()
    if not ip or ip == "0.0.0.0":
        print("Luxtronik IP nicht konfiguriert (0.0.0.0). Beende Live-Stream.")
        exit(0)
        
    print(f"Starte WebSocket-Verbindung zu {ip}...")
    ws = websocket.WebSocketApp(f"ws://{ip}:8214/",
                                subprotocols=["Lux_WS"],
                                on_open=on_open,
                                on_message=on_message,
                                on_error=on_error,
                                on_close=on_close)
    
    ws.run_forever()