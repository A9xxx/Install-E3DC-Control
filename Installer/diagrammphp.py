#!/usr/bin/env python3
import os
import json
import zipfile
import shutil
import subprocess

from .core import register_command
from .utils import run_command, ensure_dir

INSTALL_PATH = "/home/pi/E3DC-Control"


def install_diagramm():
    """Installiert Diagramm-System (Plotly & PHP)."""
    print("\n" + "=" * 50)
    print("    E3DC-Control Plot Installer")
    print("=" * 50 + "\n")

    # ============================================================
    # PHASE 1: Installation Mode Selection
    # ============================================================
    print("Was möchtest du tun?")
    print("1 = Komplette Installation")
    print("2 = Nur Konfiguration erzeugen")
    mode = input("Auswahl: ").strip()
    only_config = (mode == "2")

    # ============================================================
    # PHASE 2: Plotly Installation (optional)
    # ============================================================
    print("\nPlotly wird für die Diagrammerstellung benötigt.")
    print("1 = Plotly installieren")
    print("2 = Überspringen")
    plotly_choice = input("Auswahl: ").strip()

    if plotly_choice == "1":
        print("\n→ Installiere Plotly…")
        result = run_command("sudo pip3 install plotly --break-system-packages --ignore-installed", timeout=60)
        if result['success']:
            print("✓ Plotly installiert.\n")
        else:
            print("⚠ Plotly-Installation möglicherweise nicht erfolgreich.\n")
    else:
        print("\n→ Plotly-Installation übersprungen.\n")

    # ============================================================
    # PHASE 3: Installation Path Setup
    # ============================================================
    default_path = INSTALL_PATH
    install_path = input(f"Installationspfad [{default_path}]: ").strip() or default_path
    
    if not install_path.startswith("/"):
        print("✗ Invalidier Pfad (muss absolut sein)\n")
        return
    
    print(f"\n→ Verwende Installationspfad: {install_path}")

    # ============================================================
    # PHASE 4: Full Installation (ZIP extraction + file copy)
    # ============================================================
    if not only_config:
        if not ensure_dir(install_path):
            print(f"✗ Konnte Verzeichnis nicht erstellen\n")
            return
        
        # Get ZIP path
        script_dir = os.path.dirname(os.path.abspath(__file__))
        default_zip = os.path.join(script_dir, "E3DC-Control.zip")
        zip_path = input(f"Pfad zur ZIP-Datei [{default_zip}]: ").strip() or default_zip

        if not os.path.exists(zip_path):
            print("✗ ZIP-Datei nicht gefunden.\n")
            return

        # Extract ZIP
        print("\n→ Entpacke ZIP-Datei…")
        temp_extract = "/tmp/e3dc_install_extract"
        try:
            if os.path.exists(temp_extract):
                shutil.rmtree(temp_extract)
            os.makedirs(temp_extract, exist_ok=True)

            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_extract)
            print("✓ ZIP-Datei entpackt.")
        except Exception as e:
            print(f"✗ Fehler beim Entpacken: {e}\n")
            return

        source_root = os.path.join(temp_extract, "E3DC-Control")
        
        if not os.path.exists(source_root):
            print(f"✗ Struktur in ZIP ungültig\n")
            return

        # Copy files
        print(f"→ Kopiere Dateien nach {install_path}…")
        try:
            for item in os.listdir(source_root):
                src = os.path.join(source_root, item)
                dst = os.path.join(install_path, item)

                if item == "html":
                    continue

                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
            print("✓ Module installiert.")
        except Exception as e:
            print(f"✗ Fehler beim Kopieren: {e}\n")
            return

        # ========================================================
        # PHASE 5: Web Interface Setup
        # ========================================================
        print("→ Installiere Weboberfläche nach /var/www/html…")
        try:
            html_src = os.path.join(source_root, "html")
            if os.path.exists(html_src):
                html_dst = "/var/www/html"
                shutil.copytree(html_src, html_dst, dirs_exist_ok=True)
                print("✓ Weboberfläche installiert.")
            else:
                print("⚠ HTML-Verzeichnis nicht gefunden")
        except Exception as e:
            print(f"✗ Fehler bei Weboberfläche: {e}\n")

        # Create tmp folder
        tmp_path = "/var/www/html/tmp"
        try:
            if not os.path.exists(tmp_path):
                print("→ Erstelle tmp-Ordner…")
                os.makedirs(tmp_path, exist_ok=True)
        except Exception as e:
            print(f"✗ Fehler beim Erstellen von tmp: {e}\n")

        # ========================================================
        # PHASE 6: Permissions
        # ========================================================
        print("\n→ Setze Dateirechte…")
        commands = [
            ("sudo chown -R pi:www-data /var/www/html", "Webportal Besitzer"),
            ("sudo chmod -R 775 /var/www/html", "Webportal Rechte"),
            ("sudo chmod -R 777 /var/www/html/tmp", "tmp-Ordner Rechte"),
            ("sudo chmod o+rx /home/pi", "/home/pi lesbar"),
            (f"sudo chown -R pi:pi {install_path}", f"{install_path} Besitzer"),
            (f"sudo chmod -R 755 {install_path}", f"{install_path} Rechte"),
            (f"sudo chmod o+r {install_path}/plot_soc_changes.py", "plot_soc_changes.py lesbar")
        ]
        
        all_ok = True
        for cmd, desc in commands:
            result = run_command(cmd, timeout=30)
            if result['success']:
                print(f"  ✓ {desc}")
            else:
                print(f"  ✗ {desc}")
                all_ok = False
        
        if all_ok:
            print("✓ Rechte erfolgreich gesetzt.")
        else:
            print("⚠ Einige Rechte konnten nicht gesetzt werden.")

    # ============================================================
    # PHASE 7: Configuration Generation (always)
    # ============================================================
    print("\nWelche Daten sollen verarbeitet werden?")
    print("1 = Wallbox")
    print("2 = Wärmepumpe")
    print("3 = Beides")
    print("4 = Keine")
    choice = input("Auswahl: ").strip()

    config = {
        "enable_wallbox": choice in ["1", "3"],
        "enable_heatpump": choice in ["2", "3"]
    }

    try:
        config_path = os.path.join(install_path, "config.json")
        with open(config_path, "w") as f:
            json.dump(config, f, indent=4)
        os.chmod(config_path, 0o664)
        print(f"\n✓ Konfiguration gespeichert in {config_path}")
    except Exception as e:
        print(f"\n✗ Fehler beim Speichern der Konfiguration: {e}\n")
        return

    # ============================================================
    # PHASE 8: Test Run (only full installation)
    # ============================================================
    if not only_config:
        print("→ Starte Testlauf…")
        input_file = os.path.join(install_path, "awattardebug.txt")

        if not os.path.exists(input_file):
            print("⚠ Kein Testlauf möglich – es liegen noch keine Daten vor.")
            print("   E3DC-Control muss zuerst einmal laufen.")
        else:
            try:
                result = subprocess.run(
                    ["python3", os.path.join(install_path, "plot_soc_changes.py")],
                    capture_output=True,
                    text=True,
                    cwd=install_path,
                    timeout=30
                )

                if result.returncode == 0:
                    print("✓ Testlauf erfolgreich.")
                else:
                    print("✗ Testlauf fehlgeschlagen.")
                    if result.stderr:
                        print(f"Fehlerausgabe:\n{result.stderr}")
            except subprocess.TimeoutExpired:
                print("⚠ Testlauf hat zu lange gedauert")
            except Exception as e:
                print(f"✗ Fehler beim Testlauf: {e}")

    # ============================================================
    # COMPLETION
    # ============================================================
    print("\n" + "=" * 50)
    if only_config:
        print("✓ Konfiguration erfolgreich erstellt.")
    else:
        print("✓ Installation abgeschlossen.")
    print("=" * 50 + "\n")


def diagramm_menu():
    """Menü-Wrapper."""
    install_diagramm()


register_command("4", "Diagramm und PHP einrichten", diagramm_menu, sort_order=40)