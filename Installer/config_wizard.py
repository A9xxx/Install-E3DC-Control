import os

from .core import register_command
from .utils import replace_in_file
from .installer_config import get_install_path

INSTALL_PATH = get_install_path()
CONFIG_FILE = os.path.join(INSTALL_PATH, "e3dc.config.txt")


def load_config():
    """Lädt die Konfigurationsdatei."""
    if not os.path.exists(CONFIG_FILE):
        print(f"✗ Konfigurationsdatei nicht gefunden: {CONFIG_FILE}")
        return {}

    config = {}
    try:
        with open(CONFIG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    try:
                        key, value = line.split("=", 1)
                        config[key.strip()] = value.strip()
                    except ValueError:
                        continue
        return config
    except Exception as e:
        print(f"✗ Fehler beim Lesen der Datei: {e}")
        return {}


def save_param(key, value):
    """Speichert einen Parameter in der Konfigurationsdatei."""
    if not os.path.exists(CONFIG_FILE):
        print(f"✗ Konfigurationsdatei nicht vorhanden: {CONFIG_FILE}")
        return False
    
    success = replace_in_file(CONFIG_FILE, key, f"{key} = {value}")
    return success


def config_wizard():
    """Wizard zur Bearbeitung der Konfiguration."""
    print("\n=== Config-Wizard ===\n")

    config = load_config()
    if not config:
        print("✗ Keine Konfiguration geladen.\n")
        return

    print("Aktuelle Parameter:\n")
    for k, v in sorted(config.items()):
        print(f"  {k}: {v}")

    print("\nParameter ändern (oder leer lassen zum Überspringen):\n")

    updated = False
    for key in sorted(config.keys()):
        new_val = input(f"  {key} [{config[key]}]: ").strip()
        if new_val:
            if save_param(key, new_val):
                print(f"  ✓ {key} aktualisiert")
                updated = True
            else:
                print(f"  ✗ Fehler bei {key}")

    if updated:
        print("\n✓ Konfiguration aktualisiert.\n")
    else:
        print("\n→ Keine Änderungen gemacht.\n")


register_command("8", "E3DC-Control Konfiguration bearbeiten", config_wizard, sort_order=80)
