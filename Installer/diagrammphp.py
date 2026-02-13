#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diagrammphp.py - E3DC-Control Komplett-Installation

FEATURES:
- Extrahiert E3DC-Control.zip automatisch
- Installiert plot_soc_changes.py nach /home/pi/E3DC-Control/
- Kopiert PHP/HTML-Dateien nach /var/www/html/
- Erstellt /var/www/html/tmp/ Verzeichnis
- Setzt korrekte Berechtigungen (www-data)
- Python-Umgebung prüfen (Python 3 + plotly)
- Automatisch/Manuell/Hybrid Diagramm-Aktualisierung
- crontab-Integration mit konfigurierbarem Intervall
- Config-Datei für Einstellungen
"""
import os
import sys
import json
import subprocess
import platform
import logging
import shutil
import zipfile
import tempfile
from pathlib import Path
from . import core

# Logging einrichten
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

INSTALL_PATH = "/home/pi/E3DC-Control"
WWW_PATH = "/var/www/html"
TMP_PATH = "/var/www/html/tmp"
CONFIG_FILE = os.path.join(INSTALL_PATH, "diagram_config.json")
CRON_COMMENT = "E3DC-Control Diagram Auto-Update"
PLOT_SCRIPT_NAME = "plot_soc_changes.py"
ZIP_NAME = "E3DC-Control.zip"
OLD_MODULE_DIRS = ["config", "parsing", "plotting"]


class DiagramInstaller:
    """
    E3DC-Control Diagramm-Installationssystem mit crontab-Automatisierung
    """
    
    def __init__(self):
        self.install_path = INSTALL_PATH
        self.config_file = CONFIG_FILE
        self.diagram_mode = "manual"  # auto oder manual
        self.auto_interval = 5  # Minuten
        self.enable_wallbox = True
        self.enable_heatpump = True
        self.plot_script_path = os.path.join(self.install_path, PLOT_SCRIPT_NAME)
    
    # ============================================================
    # SYSTEM-PRÜFUNGEN
    # ============================================================
    
    def check_python_requirements(self):
        """
        Prüft ob Python 3 und plotly installiert sind.
        Bietet Installation an, falls notwendig.
        """
        print("\n" + "-" * 60)
        print("Prüfe Python-Umgebung...")
        print("-" * 60)
        
        # Python Version prüfen
        try:
            result = subprocess.run(
                ["python3", "--version"],
                capture_output=True,
                text=True
            )
            python_version = result.stdout.strip()
            print(f"✓ {python_version}")
        except FileNotFoundError:
            print("❌ Python 3 nicht gefunden!")
            print("   Installation: sudo apt-get install python3")
            return False
        
        # plotly prüfen
        try:
            result = subprocess.run(
                ["python3", "-c", "import plotly; print(plotly.__version__)"],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                plotly_version = result.stdout.strip()
                print(f"✓ plotly {plotly_version}")
                return True
            else:
                raise ImportError()
        
        except (FileNotFoundError, ImportError):
            print("❌ plotly nicht installiert!")
            choice = input("\nPlotly jetzt installieren? (j/n): ").strip().lower()
            
            if choice == 'j':
                print("\nInstalliere plotly...")
                try:
                    subprocess.run(
                        ["pip3", "install", "plotly"],
                        check=True
                    )
                    print("✓ plotly erfolgreich installiert")
                    return True
                except subprocess.CalledProcessError:
                    print("❌ Installation fehlgeschlagen")
                    print("   Manuell: pip3 install plotly")
                    return False
            else:
                print("⚠️  plotly wird benötigt für Diagramme")
                return False
    
    def check_script_installed(self):
        """
        Prüft, ob plot_soc_changes.py bereits installiert ist.
        """
        return os.path.isfile(self.plot_script_path)
    
    def extract_and_install_from_zip(self):
        """
        Extrahiert E3DC-Control.zip und installiert alle Dateien:
        - plot_soc_changes.py → /home/pi/E3DC-Control/
        - PHP/HTML Dateien → /var/www/html/
        - Erstellt /var/www/html/tmp/
        - Setzt Rechte für www-data
        """
        print("\n" + "-" * 60)
        print("Installiere E3DC-Control aus ZIP-Datei...")
        print("-" * 60)
        
        # ZIP-Datei finden
        script_dir = os.path.dirname(os.path.abspath(__file__))
        zip_path = os.path.join(script_dir, ZIP_NAME)
        
        if not os.path.isfile(zip_path):
            print(f"❌ {ZIP_NAME} nicht gefunden!")
            print(f"   Gesucht in: {script_dir}")
            return False
        
        print(f"✓ ZIP gefunden: {zip_path}")
        
        # Temporäres Verzeichnis für Extraktion
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                # ZIP extrahieren
                print("\n→ Extrahiere ZIP-Datei...")
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
                
                print(f"✓ ZIP extrahiert nach: {temp_dir}")
                
                # 1) plot_soc_changes.py nach /home/pi/E3DC-Control/ kopieren
                print("\n→ Installiere Python-Skript...")
                source_script = os.path.join(temp_dir, PLOT_SCRIPT_NAME)
                
                if not os.path.isfile(source_script):
                    print(f"❌ {PLOT_SCRIPT_NAME} nicht in ZIP gefunden!")
                    return False
                
                os.makedirs(self.install_path, exist_ok=True)
                shutil.copy2(source_script, self.plot_script_path)
                os.chmod(self.plot_script_path, 0o775)
                try:
                    subprocess.run(
                        ["sudo", "chown", "pi:www-data", self.plot_script_path],
                        check=True,
                        capture_output=True
                    )
                except subprocess.CalledProcessError as e:
                    print(f"⚠️  Konnte Besitzer nicht setzen: {e}")
                print(f"✓ {PLOT_SCRIPT_NAME} → {self.plot_script_path}")
                
                # 2) PHP/HTML Dateien nach /var/www/html/ kopieren
                print("\n→ Installiere Web-Dateien...")
                html_source = os.path.join(temp_dir, "html")
                
                if not os.path.isdir(html_source):
                    print(f"⚠️  Kein 'html'-Ordner in ZIP gefunden")
                else:
                    # Dateien kopieren
                    file_count = 0
                    for filename in os.listdir(html_source):
                        source_file = os.path.join(html_source, filename)
                        if os.path.isfile(source_file):
                            dest_file = os.path.join(WWW_PATH, filename)
                            shutil.copy2(source_file, dest_file)
                            file_count += 1
                    
                    print(f"✓ {file_count} Dateien → {WWW_PATH}")
                
                # 3) /var/www/html/tmp/ erstellen
                print("\n→ Erstelle tmp-Verzeichnis...")
                os.makedirs(TMP_PATH, exist_ok=True)
                os.chmod(TMP_PATH, 0o777)
                print(f"✓ tmp-Ordner erstellt: {TMP_PATH}")
                
                # 4) Rechte setzen für www-data
                print("\n→ Setze Berechtigungen...")
                try:
                    # Besitzer auf www-data setzen
                    subprocess.run(
                        ["sudo", "chown", "-R", "pi:www-data", WWW_PATH],
                        check=True,
                        capture_output=True
                    )
                    print(f"✓ Besitzer: pi:www-data")
                    
                    # Rechte setzen
                    subprocess.run(
                        ["sudo", "chmod", "-R", "775", WWW_PATH],
                        check=True,
                        capture_output=True
                    )
                    print(f"✓ Rechte: 775")
                    
                except subprocess.CalledProcessError as e:
                    print(f"⚠️  Berechtigungen konnten nicht gesetzt werden: {e}")
                    print("   Manuell ausführen:")
                    print(f"   sudo chown -R pi:www-data {WWW_PATH}")
                    print(f"   sudo chmod -R 775 {WWW_PATH}")
                
                print("\n✓ Installation abgeschlossen")
                return True
            
            except Exception as e:
                logger.error(f"❌ Fehler bei der Installation: {str(e)}")
                import traceback
                traceback.print_exc()
                return False

    def cleanup_old_modules(self):
        """
        Loescht alte Modul-Ordner im E3DC-Control Verzeichnis.
        """
        print("\n" + "-" * 60)
        print("Pruefe alte Modul-Ordner...")
        print("-" * 60)

        removed = 0
        for dirname in OLD_MODULE_DIRS:
            candidate = os.path.join(self.install_path, dirname)
            if os.path.isdir(candidate):
                try:
                    shutil.rmtree(candidate)
                    print(f"✓ Entfernt: {candidate}")
                    removed += 1
                except Exception as e:
                    print(f"⚠️  Konnte nicht entfernen: {candidate} ({e})")

        if removed == 0:
            print("✓ Keine alten Modul-Ordner gefunden")
    
    def print_header(self):
        """ASCII-Header"""
        print("\n" + "=" * 60)
        print("    E3DC-Control Diagramm-Installation & Automatisierung")
        print("=" * 60 + "\n")
    
    # ============================================================
    # DIAGRAMM-AKTUALISIERUNGSMODUS
    # ============================================================
    
    def select_diagram_mode(self):
        """
        Fragt, ob Diagramme automatisch oder manuell aktualisiert werden.
        
        AUTOMATISCH (crontab):
        - Alle X Minuten aktualisieren
        - Immer aktuell
        - Belastet den Server
        
        MANUELL (Button):
        - Nur bei Klick auf "Aktualisieren"
        - Weniger Belastung
        - Benutzer muss manuell aktualisieren
        """
        print("\n" + "-" * 60)
        print("Wie sollen Diagramme aktualisiert werden?")
        print("-" * 60)
        print("\n1 = AUTOMATISCH (crontab-gesteuert)")
        print("   • Alle X Minuten automatisch aktualisieren")
        print("   • Immer aktuelle Daten")
        print("   • Höhere CPU-Belastung")
        print("\n2 = MANUELL (nur Button auf Webseite)")
        print("   • Nur bei Klick auf 'Aktualisieren'")
        print("   • Weniger Belastung")
        print("   • Benutzer steuert manuell")
        print("\n3 = HYBRID (Auto + Button)")
        print("   • Auto-Update alle X Minuten")
        print("   • + Manueller Button für sofort")
        print()
        
        choice = input("Auswahl (1-3): ").strip()
        
        if choice == "1":
            self.diagram_mode = "auto"
            self.auto_interval = self._select_interval()
        elif choice == "2":
            self.diagram_mode = "manual"
        elif choice == "3":
            self.diagram_mode = "hybrid"
            self.auto_interval = self._select_interval()
        else:
            print("❌ Ungültige Auswahl, verwende MANUAL als default")
            self.diagram_mode = "manual"
        
        print(f"\n✓ Modus: {self.diagram_mode.upper()}")
        if self.diagram_mode in ("auto", "hybrid"):
            print(f"  Auto-Update: Alle {self.auto_interval} Minuten")
    
    def _select_interval(self):
        """Fragt nach Auto-Update Intervall"""
        print("\nAuto-Update Intervall:")
        print("1 = Jede Minute (höchste Aktualität, höchste Last)")
        print("2 = Alle 5 Minuten")
        print("3 = Alle 10 Minuten")
        print("4 = Alle 30 Minuten")
        print("5 = Jede Stunde")
        choice = input("Auswahl (1-5): ").strip()
        
        intervals = {"1": 1, "2": 5, "3": 10, "4": 30, "5": 60}
        return intervals.get(choice, 5)
    
    # ============================================================
    # DIAGRAMM-OPTIONEN
    # ============================================================
    
    def select_diagram_features(self):
        """Fragt, welche Features aktiviert sein sollen"""
        print("\n" + "-" * 60)
        print("Welche Diagramm-Features aktivieren?")
        print("-" * 60)
        
        choice = input("\n1 = Wallbox\n2 = Wärmepumpe\n3 = Beides\n4 = Keine\nAuswahl (1-4): ").strip()
        
        if choice == "1":
            self.enable_wallbox = True
            self.enable_heatpump = False
        elif choice == "2":
            self.enable_wallbox = False
            self.enable_heatpump = True
        elif choice == "3":
            self.enable_wallbox = True
            self.enable_heatpump = True
        else:
            self.enable_wallbox = False
            self.enable_heatpump = False
        
        print(f"\n✓ Wallbox: {self.enable_wallbox} | Wärmepumpe: {self.enable_heatpump}")
    
    # ============================================================
    # CRONTAB MANAGEMENT
    # ============================================================
    
    def setup_crontab(self):
        """
        Richtet crontab für automatische Diagramm-Aktualisierung ein.
        """
        if self.diagram_mode == "manual":
            print("\n✓ Crontab nicht nötig (manueller Modus)")
            return True
        
        try:
            print("\n" + "-" * 60)
            print("Richte crontab für Auto-Update ein...")
            print("-" * 60)
            
            # Script-Pfad (monolithisches plot_soc_changes.py)
            plot_script = self.plot_script_path
            awattar_data = os.path.join(self.install_path, "awattardebug.txt")
            
            # Cron-Linie je nach Intervall
            cron_schedule = self._get_cron_schedule(self.auto_interval)
            cron_line = f"{cron_schedule} /usr/bin/python3 {plot_script} {awattar_data} normal # {CRON_COMMENT}"
            
            # Existierende crons auslesen
            result = subprocess.run(
                ["crontab", "-l"],
                capture_output=True,
                text=True
            )
            existing_crons = result.stdout if result.returncode == 0 else ""
            
            # Neueste cron-Linie (alte entfernen, neue hinzufügen)
            new_crons = []
            for line in existing_crons.split('\n'):
                if CRON_COMMENT not in line:
                    new_crons.append(line)
            
            new_crons.append(cron_line)
            new_crons_text = '\n'.join(new_crons)
            
            # In crontab eintragen
            process = subprocess.Popen(
                ["crontab", "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = process.communicate(new_crons_text)
            
            if process.returncode != 0:
                logger.error(f"❌ Fehler beim Einrichten des crontab: {stderr}")
                return False
            
            print(f"✓ Crontab eingerichtet:")
            print(f"  Schedule: {cron_schedule}")
            print(f"  Skript: {plot_script}")
            return True
        
        except Exception as e:
            logger.error(f"❌ Fehler beim crontab-Setup: {str(e)}")
            return False
    
    def remove_crontab(self):
        """Entfernt E3DC-Crontab"""
        try:
            result = subprocess.run(
                ["crontab", "-l"],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                print("ℹ️  Kein Crontab gefunden")
                return True
            
            # Entferne E3DC-Einträge
            new_crons = []
            for line in result.stdout.split('\n'):
                if CRON_COMMENT not in line:
                    new_crons.append(line)
            
            new_crons_text = '\n'.join(new_crons)
            
            process = subprocess.Popen(
                ["crontab", "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            process.communicate(new_crons_text)
            
            print("✓ Crontab-Eintrag entfernt")
            return True
        
        except Exception as e:
            logger.error(f"❌ Fehler beim Entfernen des crontab: {str(e)}")
            return False
    
    @staticmethod
    def _get_cron_schedule(minutes):
        """Erzeugt cron-Schedule basiert auf Minuten"""
        if minutes == 1:
            return "* * * * *"  # jede Minute
        elif minutes == 5:
            return "*/5 * * * *"  # jede 5. Minute
        elif minutes == 10:
            return "*/10 * * * *"
        elif minutes == 30:
            return "*/30 * * * *"
        elif minutes == 60:
            return "0 * * * *"  # jede Stunde
        else:
            return f"*/{minutes} * * * *"
    
    # ============================================================
    # CONFIG speichern/laden
    # ============================================================
    
    def save_config(self):
        """Speichert Konfiguration als JSON"""
        config = {
            "diagram_mode": self.diagram_mode,
            "auto_interval_minutes": self.auto_interval,
            "enable_wallbox": self.enable_wallbox,
            "enable_heatpump": self.enable_heatpump,
        }
        
        try:
            os.makedirs(self.install_path, exist_ok=True)
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)
            print(f"\n✓ Konfiguration gespeichert: {self.config_file}")
            return True
        except Exception as e:
            logger.error(f"❌ Fehler beim Speichern der Konfiguration: {str(e)}")
            return False
    
    def load_config(self):
        """Lädt gespeicherte Konfiguration"""
        if not os.path.exists(self.config_file):
            return False
        
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)
            
            self.diagram_mode = config.get("diagram_mode", "manual")
            self.auto_interval = config.get("auto_interval_minutes", 5)
            self.enable_wallbox = config.get("enable_wallbox", True)
            self.enable_heatpump = config.get("enable_heatpump", True)
            return True
        except Exception as e:
            logger.error(f"⚠️  Fehler beim Laden der Konfiguration: {str(e)}")
            return False
    
    # ============================================================
    # Main Installation
    # ============================================================
    
    def run_installation(self):
        """Führt komplette Installation durch"""
        self.print_header()
        
        # 0) Python-Umgebung prüfen (IMMER ZUERST)
        if not self.check_python_requirements():
            print("\n❌ Installation abgebrochen: Python-Requirements fehlen")
            return False
        
        # 1) Skript-Status prüfen
        script_exists = self.check_script_installed()
        
        if script_exists:
            print(f"\n✓ {PLOT_SCRIPT_NAME} ist bereits installiert")
            print(f"  Pfad: {self.plot_script_path}")
            
            # Frage: Neu-Installation oder nur Konfiguration
            print("\nWas möchtest du tun?")
            print("1 = Nur Konfiguration ändern (Dateien bleiben)")
            print("2 = Komplett neu installieren (aus ZIP)")
            print("3 = Nur crontab ändern")
            print("4 = Abbrechen")
            choice = input("Auswahl (1-4): ").strip()
            
            if choice == "4":
                print("Installation abgebrochen")
                return False
            elif choice == "2":
                if not self.extract_and_install_from_zip():
                    print("❌ Installation fehlgeschlagen")
                    return False
            elif choice == "3":
                self.select_diagram_mode()
                self.setup_crontab()
                self.save_config()
                self.print_summary()
                return True
            # Bei choice == "1" wird nur Konfiguration gemacht (unten)
        
        else:
            # Skript fehlt - Installation anbieten
            print(f"\n⚠️  {PLOT_SCRIPT_NAME} nicht gefunden")
            
            choice = input("\nMöchtest du E3DC-Control installieren? (j/n): ").strip().lower()
            
            if choice != 'j':
                print("Installation abgebrochen")
                return False
            
            # Aus ZIP installieren
            if not self.extract_and_install_from_zip():
                print("❌ Installation fehlgeschlagen")
                return False
        
        # 2) Alte Modul-Ordner entfernen (falls vorhanden)
        self.cleanup_old_modules()

        # 3) Features konfigurieren
        self.select_diagram_features()
        
        # 4) Diagramm-Modus
        self.select_diagram_mode()
        
        # 5) Crontab
        if self.diagram_mode in ("auto", "hybrid"):
            self.setup_crontab()
        
        # 6) Konfiguration speichern
        self.save_config()
        
        # 7) Zusammenfassung
        self.print_summary()
        return True
    
    def print_summary(self):
        """Zeigt Zusammenfassung"""
        print("\n" + "=" * 60)
        print("INSTALLATION ABGESCHLOSSEN")
        print("=" * 60)
        print(f"➤ Python-Skript: {PLOT_SCRIPT_NAME}")
        print(f"  Pfad: {self.plot_script_path}")
        print(f"➤ Web-Dateien: {WWW_PATH}")
        print(f"➤ tmp-Ordner: {TMP_PATH}")
        print(f"➤ Modus: {self.diagram_mode.upper()}")
        print(f"➤ Wallbox: {self.enable_wallbox}")
        print(f"➤ Wärmepumpe: {self.enable_heatpump}")
        if self.diagram_mode in ("auto", "hybrid"):
            print(f"➤ Auto-Update: Alle {self.auto_interval} Minuten")
        print(f"➤ Config: {self.config_file}")
        print("\n💡 Tipps:")
        print(f"  • Web-Interface: http://raspberrypi.local/")
        print(f"  • Diagramm direkt: http://raspberrypi.local/diagramm.html")
        print("  • Manuell ausführen:")
        print(f"    python3 {self.plot_script_path}")
        if self.diagram_mode in ("auto", "hybrid"):
            print(f"  • Crontab prüfen: crontab -l")
        print("=" * 60 + "\n")


# ============================================================
# INTEGRATIONS-FUNKTIONEN
# ============================================================

def install_diagramm():
    """
    Wrapper-Funktion für die Integration in install_all.py
    Installiert und konfiguriert das Diagramm-System
    """
    installer = DiagramInstaller()
    installer.run_installation()


# ============================================================
# MENÜ-INTEGRATION
# ============================================================

core.register_command(
    key="4",
    label="Diagramm-Installation & Automatisierung",
    func=install_diagramm,
    sort_order=40
)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    installer = DiagramInstaller()
    installer.run_installation()
