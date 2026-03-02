import os
import json
from .core import register_command
from .installer_config import save_config, load_config
from .system import setup_venv

def change_venv_name():
    """Ändert den Namen des Python Virtual Environments."""
    print("\n=== Python venv Namen ändern ===\n")
    
    config = load_config()
    current_name = config.get("venv_name", ".venv_e3dc")
    
    print(f"Aktueller Name: {current_name}")
    new_name = input(f"Neuen Namen eingeben (Enter für '.venv_e3dc'): ").strip()
    
    if not new_name:
        new_name = ".venv_e3dc"
    
    if new_name == current_name:
        print("→ Name ist unverändert.\n")
        return

    # 1. In installer_config.json speichern
    config["venv_name"] = new_name
    save_config(config)
    print(f"✓ Konfiguration gespeichert: {new_name}")

    # 2. In e3dc_paths.json aktualisieren (für PHP)
    try:
        paths_file = "/var/www/html/e3dc_paths.json"
        if os.path.exists(paths_file):
            with open(paths_file, 'r') as f:
                d = json.load(f)
            d['venv_name'] = new_name
            with open(paths_file, 'w') as f:
                json.dump(d, f, indent=2)
            print("✓ e3dc_paths.json aktualisiert")
    except Exception as e:
        print(f"⚠ Fehler beim Aktualisieren von e3dc_paths.json: {e}")

    # 3. Setup ausführen?
    if input("\nSoll das neue venv jetzt erstellt werden? (j/n) [j]: ").strip().lower() != 'n':
        setup_venv(show_header=True)
        print(f"\nHinweis: Der alte Ordner '{current_name}' wurde nicht gelöscht.")
        print(f"Du kannst ihn manuell löschen, wenn alles läuft.")

register_command("24", "Python venv Namen ändern", change_venv_name, sort_order=240)
