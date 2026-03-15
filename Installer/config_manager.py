import os
import json
import logging
import tempfile
import sys

# Standard-Ausgabe auf UTF-8 erzwingen
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from .utils import run_command
from .installer_config import CONFIG_FILE, get_install_path, get_install_user, get_home_dir, load_config
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_PATH = get_install_path()
INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))

# ANSI Colors
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

# HINWEIS: Der Modul-globale Logger wurde entfernt, um Probleme mit der Import-Reihenfolge zu vermeiden.
# Er wird stattdessen bei Bedarf innerhalb der jeweiligen Funktion geholt.

def check_and_set_config_defaults():
    """Prüft, ob wichtige UI-bezogene Variablen in e3dc.config.txt vorhanden sind und fügt sie bei Bedarf hinzu."""
    perm_logger = logging.getLogger("permissions")
    print("\n=== Konfigurations-Standardwerte-Prüfung ===\n")
    perm_logger.info("--- Starte Prüfung der Konfigurations-Standardwerte ---")

    config_file = os.path.join(INSTALL_PATH, "e3dc.config.txt")

    if not os.path.exists(config_file):
        print(f"{RED}✗{RESET} Konfigurationsdatei {config_file} nicht gefunden. Prüfung übersprungen.")
        perm_logger.warning(f"e3dc.config.txt nicht gefunden, Prüfung der Standardwerte übersprungen.")
        return True # Kein Fehler, da die Datei vielleicht erst später erstellt wird.

    defaults_to_check = {
        "show_forecast": "0",
        "wbcostpowers": "7.2, 11.0",
        "darkmode": "1",
        "pvatmosphere": "0.815",
        "check_updates": "0",
        "auto_update_enable": "0",
        "auto_update_time": "23:00",
        # Luxtronik Defaults
        "luxtronik": "0",
        "luxtronik_ip": "192.168.178.88",
        "auto_mode": "1",
        "GRID_START_LIMIT": "-3500",
        "MIN_SOC": "80",
        "AT_LIMIT": "10.0",
        "WWS": "50.0",
        "WWW": "48.0",
        "HZ": "32.0",
        "price_boost_enable": "0",
        "price_limit": "20.0",
        "price_hard_limit": "-99.0",
        "price_min_duration": "60",
        "price_max_daily": "180",
        "stop_delay_minutes": "10",
        "manual_boost_max_duration": "180",
        "rl_source": "internal",
        "wq_min_temp": "1.0",
        "manual_boost_min_soc": "25",
        "pv_pause_enable": "0",
        "pv_pause_soc": "80",
        "pv_pause_watt": "3000.0",
        "pv_pause_timeout_minutes": "120",
        "pv_pause_min_at": "0.0",
        "morning_boost_enable": "0",
        "morning_boost_prio": "wallbox",
        "morning_boost_wb_power": "7.0",
        "morning_boost_min_hours": "3",
        "morning_boost_min_pv_pct": "50.0",
        "morning_boost_target_soc": "20",
        "morning_boost_deadline": "8",
        "super_intelligence_enable": "0",
        "super_intelligence_deadline": "8",
        "telegram_token": "",
        "telegram_chat_id": "",
        "telegram_stats_enable": "0"
    }

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        existing_keys = {line.split('=', 1)[0].strip().lower() for line in content.splitlines() if '=' in line and not line.strip().startswith('#')}

        missing_keys_to_add = []
        for key, value in defaults_to_check.items():
            if key.lower() not in existing_keys:
                missing_keys_to_add.append(f"{key} = {value}")
                print(f"{RED}✗{RESET} Variable '{key}' fehlt in e3dc.config.txt.")
                perm_logger.warning(f"Variable '{key}' fehlt in e3dc.config.txt.")

        if not missing_keys_to_add:
            print(f"{GREEN}✓{RESET} Alle notwendigen UI-Konfigurationsvariablen sind vorhanden.\n")
            perm_logger.info("Alle UI-Konfigurationsvariablen vorhanden.")
            return True
        else:
            print("\n→ Füge fehlende Variablen am Ende der Datei hinzu...")
            if not content.endswith('\n'):
                content += '\n'
            content += "\n# --- Automatisch hinzugefügte UI-Parameter ---\n"
            content += "\n".join(missing_keys_to_add) + "\n"
            with open(config_file, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"\n{GREEN}✓{RESET} Konfigurationsdatei aktualisiert.\n")
            return True
    except Exception as e:
        print(f"{RED}✗{RESET} Fehler beim Lesen oder Schreiben von {config_file}: {e}")
        perm_logger.error(f"Fehler beim Prüfen/Setzen der Config-Defaults: {e}")
        return False

def check_config_duplicates():
    """Prüft e3dc.config.txt auf doppelte Einträge (case-insensitive) und entfernt Duplikate (behält das erste)."""
    perm_logger = logging.getLogger("permissions")
    print("\n=== Konfigurations-Duplikat-Prüfung ===\n")
    perm_logger.info("--- Starte Prüfung auf Konfigurations-Duplikate ---")

    config_file = os.path.join(INSTALL_PATH, "e3dc.config.txt")

    if not os.path.exists(config_file):
        print(f"{GREEN}✓{RESET} Konfigurationsdatei {config_file} nicht gefunden. Übersprungen.")
        return True

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        seen_keys = set()
        new_lines = []
        duplicates_found = False
        removed_count = 0

        for line in lines:
            stripped = line.strip()
            # Kommentare und Leerzeilen behalten
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            
            if "=" in stripped:
                parts = stripped.split("=", 1)
                key = parts[0].strip().lower()
                
                if key in seen_keys:
                    print(f"  {RED}✗{RESET} Duplikat gefunden und entfernt: {parts[0].strip()} (Zeile: {stripped})")
                    perm_logger.warning(f"Duplikat entfernt: {parts[0].strip()}")
                    duplicates_found = True
                    removed_count += 1
                    continue # Zeile überspringen (löschen)
                else:
                    seen_keys.add(key)
                    new_lines.append(line)
            else:
                new_lines.append(line)

        if duplicates_found:
            print(f"\n→ Entferne {removed_count} Duplikate (behalte jeweils das erste)...")
            with open(config_file, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            print(f"{GREEN}✓{RESET} Konfigurationsdatei bereinigt.\n")
            perm_logger.info(f"Konfigurationsdatei bereinigt, {removed_count} Duplikate entfernt.")
            return True
        else:
            print(f"{GREEN}✓{RESET} Keine Duplikate gefunden.\n")
            perm_logger.info("Keine Duplikate in Konfiguration gefunden.")
            return True

    except Exception as e:
        print(f"{RED}✗{RESET} Fehler bei der Duplikat-Prüfung: {e}")
        perm_logger.error(f"Fehler bei Duplikat-Prüfung: {e}")
        return False

def _migrate_luxtronik_config():
    """Prüft auf alte config.lux.json und migriert die Werte."""
    perm_logger = logging.getLogger("permissions")
    lux_config_path = os.path.join(INSTALLER_DIR, "luxtronik", "config.lux.json")
    if not os.path.exists(lux_config_path):
        return # Nichts zu tun

    print("\n=== Veraltete Luxtronik-Konfiguration gefunden ===\n")
    print("→ Migriere Einstellungen nach e3dc.config.txt...")
    perm_logger.info("Veraltete config.lux.json gefunden, starte Migration.")

    e3dc_config_path = os.path.join(INSTALL_PATH, "e3dc.config.txt")
    
    try:
        with open(lux_config_path, 'r', encoding='utf-8') as f:
            lux_conf = json.load(f)

        if not os.path.exists(e3dc_config_path):
            print(f"✗ Fehler: {e3dc_config_path} nicht gefunden. Migration abgebrochen.")
            perm_logger.error(f"e3dc.config.txt nicht gefunden, Migration abgebrochen.")
            return

        with open(e3dc_config_path, 'r', encoding='utf-8') as f:
            e3dc_lines = f.readlines()

        new_lines = []
        keys_to_update = {k.lower(): str(v) for k, v in lux_conf.items()}
        
        for line in e3dc_lines:
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith('#'):
                new_lines.append(line)
                continue
            
            if '=' in stripped_line:
                key, _ = stripped_line.split('=', 1)
                key_lower = key.strip().lower()
                if key_lower in keys_to_update:
                    new_lines.append(f"{key.strip()} = {keys_to_update[key_lower]}\n")
                    del keys_to_update[key_lower]
                    continue
            new_lines.append(line)

        if keys_to_update:
            new_lines.append("\n# --- Automatisch migrierte Luxtronik-Parameter ---\n")
            for key, value in keys_to_update.items():
                new_lines.append(f"{key} = {value}\n")

        with open(e3dc_config_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        
        os.rename(lux_config_path, lux_config_path + ".migrated")
        print(f"✓ Konfiguration erfolgreich migriert. Alte Datei umbenannt zu: {os.path.basename(lux_config_path)}.migrated\n")
        perm_logger.info("Luxtronik-Konfiguration erfolgreich migriert.")

    except Exception as e:
        print(f"✗ Fehler bei der Konfigurations-Migration: {e}")
        perm_logger.error(f"Fehler bei der Konfigurations-Migration: {e}")

def _migrate_strompreis_file():
    """Benennt e3dc.strompreis.txt in e3dc.strompreise.txt um."""
    perm_logger = logging.getLogger("permissions")
    old_file = os.path.join(INSTALL_PATH, "e3dc.strompreis.txt")
    new_file = os.path.join(INSTALL_PATH, "e3dc.strompreise.txt")
    
    if os.path.exists(old_file) and not os.path.exists(new_file):
        print(f"\n→ Migriere Strompreis-Datei: {os.path.basename(old_file)} -> {os.path.basename(new_file)}\n")
        try:
            os.rename(old_file, new_file)
            print(f"{GREEN}✓{RESET} Datei umbenannt.")
            perm_logger.info(f"Strompreis-Datei umbenannt: {old_file} -> {new_file}")
        except Exception as e:
            print(f"{RED}✗{RESET} Fehler beim Umbenennen: {e}")
            perm_logger.error(f"Fehler beim Umbenennen der Strompreis-Datei: {e}")

def run_config_wizard():
    """Führt alle Konfigurations-Checks und Migrationen aus."""
    log_task_completed("Konfigurations-Management gestartet", details="Prüfe & migriere Konfigs")
    
    _migrate_luxtronik_config()
    _migrate_strompreis_file()
    
    # Auf Duplikate prüfen und bereinigen (bevor Defaults geprüft werden)
    check_config_duplicates()

    # Standardwerte in der Konfiguration prüfen und setzen
    config_defaults_success = check_and_set_config_defaults()
    if not config_defaults_success:
        log_warning("config_manager", "Prüfung der Konfigurations-Standardwerte hatte Fehler")
    
    log_task_completed("Konfigurations-Management beendet")
