import os
import re

from .core import register_command
from .utils import ensure_dir

INSTALL_PATH = "/home/pi/E3DC-Control"
PRICE_FILE = os.path.join(INSTALL_PATH, "e3dc.strompreis.txt")

# Standardwerte
DEFAULT_ENTRIES = [
    "2 17.77",
    "6 24.76",
    "12 17.77",
    "13 17.76",
    "14 17.75",
    "15 17.74",
    "16 24.76",
    "18 29.77",
    "21 24.76"
]


def validate_entry(line):
    """Validiert einen Eintrag (Stunde Preis)."""
    parts = line.strip().split()
    
    if len(parts) != 2:
        return False, "Format ungültig (erwartet: 'Stunde Preis')"
    
    try:
        hour = int(parts[0])
        price = float(parts[1])
        
        if not (0 <= hour <= 23):
            return False, f"Stunde {hour} ungültig (0-23)"
        
        if price < 0:
            return False, f"Preis {price} kann nicht negativ sein"
        
        return True, (hour, price)
    except ValueError:
        return False, "Stunde und Preis müssen Zahlen sein"


def parse_entries(entries):
    """Parsed und validiert eine Liste von Einträgen."""
    parsed = {}
    errors = []
    
    for line in entries:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        
        valid, result = validate_entry(line)
        
        if not valid:
            errors.append(f"'{line}' → {result}")
        else:
            hour, price = result
            if hour in parsed:
                errors.append(f"Stunde {hour} doppelt vorhanden – überschreibe")
            parsed[hour] = price
    
    return parsed, errors


def create_default_price_file():
    """Erstellt die Datei mit Standardwerten."""
    try:
        if not ensure_dir(INSTALL_PATH):
            print("✗ Verzeichnis konnte nicht erstellt werden\n")
            return False
        
        print("→ Erstelle Strompreis-Datei mit Standardwerten…")
        
        with open(PRICE_FILE, "w") as f:
            f.write("# Strompreise (Stunde Preis)\n")
            f.write("# Format: 0-23 (Stunde) und Preis in €/kWh\n\n")
            for line in DEFAULT_ENTRIES:
                f.write(line + "\n")
        
        os.chmod(PRICE_FILE, 0o664)
        print(f"✓ Datei erstellt: {PRICE_FILE}\n")
        return True
    except Exception as e:
        print(f"✗ Fehler beim Erstellen der Datei: {e}\n")
        return False


def load_entries():
    """Liest die Datei ein oder bietet Standardwerte an."""
    if not os.path.exists(PRICE_FILE):
        print("⚠ Strompreis-Datei existiert nicht.")
        choice = input("Jetzt mit Standardwerten erstellen? (j/n): ").strip().lower()
        if choice == "j":
            if create_default_price_file():
                return DEFAULT_ENTRIES.copy()
        else:
            print("→ Abgebrochen.\n")
        return None

    try:
        entries = []
        with open(PRICE_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    entries.append(line)
        return entries if entries else None
    except Exception as e:
        print(f"✗ Fehler beim Lesen der Datei: {e}\n")
        return None


def save_entries(parsed_prices):
    """Speichert die Strompreise (sortiert nach Stunde)."""
    try:
        with open(PRICE_FILE, "w") as f:
            f.write("# Strompreise (Stunde Preis)\n")
            f.write("# Format: 0-23 (Stunde) und Preis in €/kWh\n\n")
            
            for hour in sorted(parsed_prices.keys()):
                price = parsed_prices[hour]
                f.write(f"{hour} {price}\n")
        
        os.chmod(PRICE_FILE, 0o664)
        return True
    except Exception as e:
        print(f"✗ Fehler beim Speichern: {e}")
        return False


def strompreis_wizard():
    """Hauptlogik für Strompreis-Konfiguration."""
    print("\n=== Strompreis-Wizard ===\n")

    entries = load_entries()
    if entries is None:
        return

    print("Aktuelle Einträge:\n")
    for line in entries:
        print(f"  {line}")

    print("\n--- Neue Werte eingeben ---")
    print("Format: Stunde (0-23) und Preis")
    print("Beispiel: '14 0.25'")
    print("Mehrere Zeilen eingeben, mit leerer Zeile beenden.\n")

    new_entries = []
    while True:
        line = input("> ").strip()
        if not line:
            break
        if line.startswith("#"):
            continue
        new_entries.append(line)

    # Wenn keine Eingaben → Standardwerte anbieten
    if not new_entries:
        print("\nKeine Eingaben gemacht.")
        choice = input("Standardwerte übernehmen? (j/n): ").strip().lower()
        if choice == "j":
            new_entries = DEFAULT_ENTRIES.copy()
        else:
            print("→ Abgebrochen.\n")
            return

    # Validiere alle Einträge
    parsed, errors = parse_entries(new_entries)

    if errors:
        print("\n⚠ Fehler bei der Validierung:\n")
        for error in errors:
            print(f"  - {error}")

    if not parsed:
        print("\n✗ Keine gültigen Einträge – Abbruch.\n")
        return

    # Bestätigung
    print("\nValidierte Einträge:\n")
    for hour in sorted(parsed.keys()):
        print(f"  {hour:2d}:00 → {parsed[hour]:.2f} €/kWh")

    choice = input("\nSpeichern? (j/n): ").strip().lower()
    if choice != "j":
        print("→ Abgebrochen.\n")
        return

    # Speichern
    if save_entries(parsed):
        print("\n✓ Strompreise aktualisiert.\n")
    else:
        print("\n✗ Speichern fehlgeschlagen.\n")


register_command("11", "Strompreis-Wizard", strompreis_wizard, sort_order=110)
