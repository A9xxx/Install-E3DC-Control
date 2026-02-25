#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diagrammphp.py - Diagramm-System & Web-Portal Installation

FEATURES:
- Extrahiert E3DC-Control.zip automatisch (Diagramm-Dateien)
- Installiert plot_soc_changes.py nach /home/<install_user>/E3DC-Control/
- Kopiert Web-Portal-Dateien (PHP/HTML/CSS) nach /var/www/html/
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
import socket
import logging
import shutil
import zipfile
import tempfile
from pathlib import Path
from . import core
from .installer_config import (
    get_install_path,
    get_install_user,
    get_user_ids,
    get_www_data_gid
)
from .permissions_helper import (
    set_file_ownership,
    set_directory_ownership_recursive,
    set_web_directory,
    set_executable_script
)

# Logging einrichten
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

INSTALL_PATH = get_install_path()
WWW_PATH = "/var/www/html"
TMP_PATH = "/var/www/html/tmp"
CONFIG_FILE = os.path.join(INSTALL_PATH, "diagram_config.json")
CRON_COMMENT = "E3DC-Control Diagram Auto-Update"
PLOT_SCRIPT_NAME = "plot_soc_changes.py"
BACKUP_CRON_COMMENT = "E3DC-Control History Backup" # Neuer Kommentar für den Backup-Cron
BACKUP_SCRIPT_PATH = "/var/www/html/backup_history.php" # Pfad zum Backup-Skript
PLOT_LIVE_HISTORY_NAME = "plot_live_history.py"
ZIP_NAME = "E3DC-Control.zip"
OLD_MODULE_DIRS = ["config", "parsing", "plotting"]
OBSOLETE_WEB_FILES = ["auto.php", "check.php", "config.php", "test.php", "mobile_history.php", "mobile_archiv.php"]


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
        self.install_user = get_install_user()
        self.install_uid, _ = get_user_ids()
        self.www_data_gid = get_www_data_gid()
    
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
        
        # plotly prüfen (mindestens 5.0 erforderlich)
        try:
            result = subprocess.run(
                ["python3", "-c", "import plotly; print(plotly.__version__)"],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                plotly_version = result.stdout.strip()
                print(f"✓ plotly {plotly_version}")

                def _parse_version(version_str):
                    parts = []
                    for token in version_str.split("."):
                        num = ""
                        for ch in token:
                            if ch.isdigit():
                                num += ch
                            else:
                                break
                        if num:
                            parts.append(int(num))
                        else:
                            break
                    while len(parts) < 3:
                        parts.append(0)
                    return tuple(parts[:3])

                if _parse_version(plotly_version) >= (5, 0, 0):
                    return True

                print("⚠ plotly ist zu alt (mindestens 5.0 erforderlich)")
                choice = input("\nPlotly auf >=5.0 aktualisieren? (j/n): ").strip().lower()
                if choice == 'j':
                    print("\nAktualisiere plotly...")
                    try:
                        subprocess.run(
                            ["pip3", "install", "--upgrade", "plotly>=5.0"],
                            check=True
                        )
                        print("✓ plotly erfolgreich aktualisiert")
                        return True
                    except subprocess.CalledProcessError:
                        print("❌ Update fehlgeschlagen")
                        print("   Manuell: pip3 install --upgrade 'plotly>=5.0'")
                        return False
                else:
                    print("⚠️  plotly >= 5.0 wird benötigt für Diagramme")
                    return False
            else:
                raise ImportError()
        
        except (FileNotFoundError, ImportError):
            print("❌ plotly nicht installiert!")
            choice = input("\nPlotly jetzt installieren? (j/n): ").strip().lower()
            
            if choice == 'j':
                print("\nInstalliere plotly...")
                try:
                    subprocess.run(
                        ["pip3", "install", "plotly>=5.0"],
                        check=True
                    )
                    print("✓ plotly erfolgreich installiert")
                    return True
                except subprocess.CalledProcessError:
                    print("❌ Installation fehlgeschlagen")
                    print("   Manuell: pip3 install 'plotly>=5.0'")
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
        Extrahiert E3DC-Control.zip und installiert Diagramm-System-Dateien:
        - plot_soc_changes.py → /home/<install_user>/E3DC-Control/
        - PHP/HTML Web-Portal-Dateien → /var/www/html/
        - Erstellt /var/www/html/tmp/
        - Setzt Rechte für www-data
        """
        print("\n" + "-" * 60)
        print("Installiere Diagramm-System aus ZIP-Datei...")
        print("-" * 60)
        
        # ZIP-Datei finden
        script_dir = os.path.dirname(os.path.abspath(__file__))
        zip_path = os.path.join(script_dir, ZIP_NAME)
        
        # Debug: Zeige gesuchte Pfade
        print(f"→ Suche ZIP-Datei...")
        print(f"  __file__: {__file__}")
        print(f"  script_dir: {script_dir}")
        print(f"  zip_path: {zip_path}")
        
        if not os.path.isfile(zip_path):
            print(f"❌ {ZIP_NAME} nicht gefunden!")
            print(f"   Gesucht in: {script_dir}")
            return False
        
        print(f"✓ ZIP gefunden: {zip_path}")
        
        # Debug: Zeige ZIP-Inhalte
        print("\n→ ZIP-Inhalte:")
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                for name in zip_ref.namelist()[:20]:  # Erste 20 Dateien
                    print(f"  {name}")
                if len(zip_ref.namelist()) > 20:
                    print(f"  ... und {len(zip_ref.namelist()) - 20} weitere")
        except Exception as e:
            print(f"  ⚠️  Fehler beim Lesen: {e}")
        
        # Temporäres Verzeichnis für Extraktion
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                # ZIP extrahieren
                print("\n→ Extrahiere ZIP-Datei...")
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
                
                print(f"✓ ZIP extrahiert nach: {temp_dir}")
                
                # 1) plot_soc_changes.py nach /home/<install_user>/E3DC-Control/ kopieren
                print("\n→ Installiere Diagramm-Skript (plot_soc_changes.py)...")
                source_script = os.path.join(temp_dir, PLOT_SCRIPT_NAME)
                
                if not os.path.isfile(source_script):
                    print(f"❌ {PLOT_SCRIPT_NAME} nicht in ZIP gefunden!")
                    return False
                
                os.makedirs(self.install_path, exist_ok=True)
                shutil.copy2(source_script, self.plot_script_path)

                # Sicherstellen: kein UTF-8 BOM (sonst bricht die Shebang unter Linux)
                try:
                    with open(self.plot_script_path, "rb") as f:
                        content = f.read()
                    bom = b"\xef\xbb\xbf"
                    if content.startswith(bom):
                        with open(self.plot_script_path, "wb") as f:
                            f.write(content[len(bom):])
                        print("✓ BOM aus plot_soc_changes.py entfernt")
                except Exception as e:
                    print(f"⚠️  Konnte BOM nicht prüfen/entfernen: {e}")
                
                # Setze Berechtigungen für das Skript (install_user:www-data, 775)
                try:
                    subprocess.run(
                        ["sudo", "chown", f"{self.install_user}:www-data", self.plot_script_path],
                        check=True,
                        capture_output=True
                    )
                    subprocess.run(
                        ["sudo", "chmod", "775", self.plot_script_path],
                        check=True,
                        capture_output=True
                    )
                except subprocess.CalledProcessError as e:
                    print(f"⚠️  Konnte Besitzer/Rechte für Skript nicht setzen: {e}")
                print(f"✓ {PLOT_SCRIPT_NAME} → {self.plot_script_path}")

                # 1b) plot_live_history.py (Live-48h-Diagramm) – aus ZIP oder aus Diagramm-Ordner
                live_history_dest = os.path.join(self.install_path, PLOT_LIVE_HISTORY_NAME)
                source_live = os.path.join(temp_dir, PLOT_LIVE_HISTORY_NAME)
                if not os.path.isfile(source_live):
                    diagramm_dir = os.path.join(os.path.dirname(zip_path), '..', '..', 'Diagramm')
                    source_live = os.path.join(diagramm_dir, PLOT_LIVE_HISTORY_NAME)
                if os.path.isfile(source_live):
                    shutil.copy2(source_live, live_history_dest)
                    try:
                        with open(live_history_dest, "rb") as f:
                            c = f.read()
                        if c.startswith(b"\xef\xbb\xbf"):
                            with open(live_history_dest, "wb") as f:
                                f.write(c[3:])
                    except Exception:
                        pass
                    try:
                        subprocess.run(["sudo", "chown", f"{self.install_user}:www-data", live_history_dest], check=True, capture_output=True)
                        subprocess.run(["sudo", "chmod", "775", live_history_dest], check=True, capture_output=True)
                    except subprocess.CalledProcessError:
                        pass
                    print(f"✓ {PLOT_LIVE_HISTORY_NAME} → {live_history_dest}")
                
                # 2) Web-Portal Dateien rekursiv nach /var/www/html/ kopieren
                print("\n→ Installiere Web-Portal-Dateien (PHP, CSS, JS, Icons)...")
                html_source = os.path.join(temp_dir, "html")
                
                if not os.path.isdir(html_source):
                    print(f"⚠️  Kein 'html'-Ordner in ZIP gefunden")
                else:
                    try:
                        # Modernes shutil.copytree zum Zusammenführen der Verzeichnisse
                        shutil.copytree(html_source, WWW_PATH, dirs_exist_ok=True)
                        print(f"✓ Dateien und Unterordner (icons) → {WWW_PATH}")
                    except Exception as e:
                        print(f"⚠️  Fehler beim Kopieren des Webportal-Ordners: {e}")
                
                # 2b) Veraltete Dateien im Web-Verzeichnis bereinigen
                for obs_file in OBSOLETE_WEB_FILES:
                    obs_path = os.path.join(WWW_PATH, obs_file)
                    if os.path.isfile(obs_path):
                        try:
                            os.remove(obs_path)
                            print(f"✓ Veraltete Datei entfernt: {obs_path}")
                        except Exception as e:
                            print(f"⚠️  Konnte veraltete Datei {obs_file} nicht entfernen: {e}")

                # 3) /var/www/html/tmp/ und icons/ Berechtigungen sicherstellen
                print("\n→ Überprüfe Verzeichnisstrukturen...")
                os.makedirs(TMP_PATH, exist_ok=True)
                # Backup-Verzeichnis für Historie erstellen
                history_backups_path = os.path.join(TMP_PATH, "history_backups")
                os.makedirs(history_backups_path, exist_ok=True)
                print(f"✓ tmp-Ordner sichergestellt: {TMP_PATH}")
                
                # 4) Rechte für das gesamte Webportal setzen (install_user:www-data, 775)
                print("\n→ Setze Berechtigungen...")
                try:
                    # Besitzer auf install_user:www-data setzen (-R für alles inkl. icons/)
                    subprocess.run(
                        ["sudo", "chown", "-R", f"{self.install_user}:www-data", WWW_PATH],
                        check=True,
                        capture_output=True
                    )
                    # Rechte setzen (-R für alles inkl. icons/)
                    # 775 = rwxrwxr-x (Owner & Gruppe haben alle Rechte)
                    subprocess.run(
                        ["sudo", "chmod", "-R", "775", WWW_PATH],
                        check=True,
                        capture_output=True
                    )
                    # Ordner auf 775, Dateien auf 664
                    for root, dirs, files in os.walk(WWW_PATH):
                        for d in dirs:
                            os.chmod(os.path.join(root, d), 0o775)
                        for f in files:
                            os.chmod(os.path.join(root, f), 0o664)
                    
                    # Spezielle Rechte für tmp (777, damit PHP/Python problemlos schreiben können)
                    subprocess.run(["sudo", "chmod", "777", TMP_PATH], check=True, capture_output=True)
                    
                    # Rechte für history_backups (775)
                    subprocess.run(["sudo", "chown", f"{self.install_user}:www-data", history_backups_path], check=True, capture_output=True)
                    subprocess.run(["sudo", "chmod", "775", history_backups_path], check=True, capture_output=True)
                    
                    print(f"✓ Besitzer für alle Dateien: {self.install_user}:www-data")
                    print(f"✓ Rechte gesetzt (Ordner 775, Dateien 664)")
                    
                except Exception as e:
                    print(f"⚠️  Berechtigungen konnten nicht gesetzt werden: {e}")
                
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
        print("Prüfe alte Modul-Ordner...")
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
        print("4 = Alle 15 Minuten")
        print("5 = Alle 30 Minuten")
        print("6 = Jede Stunde")
        print("7 = Eigene Eingabe")
        choice = input("Auswahl (1-7): ").strip()
        
        intervals = {"1": 1, "2": 5, "3": 10, "4": 15, "5": 30, "6": 60}
        
        if choice == "7":
            # Eigene Eingabe
            while True:
                try:
                    custom = input("Intervall in Minuten (1-1440): ").strip()
                    minutes = int(custom)
                    if 1 <= minutes <= 1440:  # Max 24 Stunden
                        return minutes
                    else:
                        print("⚠ Bitte Wert zwischen 1 und 1440 eingeben")
                except ValueError:
                    print("⚠ Ungültige Eingabe, bitte Zahl eingeben")
        
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
        try:
            print("\n" + "-" * 60)
            print("Richte crontab für Auto-Update ein...")
            print("-" * 60)
            
            # Script-Pfad (monolithisches plot_soc_changes.py)
            plot_script = self.plot_script_path
            awattar_data = os.path.join(self.install_path, "awattardebug.txt")
            cron_line_plot = ""
            if self.diagram_mode in ("auto", "hybrid"):
                cron_schedule_plot = self._get_cron_schedule(self.auto_interval)
                cron_line_plot = f"{cron_schedule_plot} /usr/bin/python3 {plot_script} {awattar_data} normal # {CRON_COMMENT}"
            
            # Cron-Linie für backup_history.php (täglich um Mitternacht)
            cron_schedule_backup = "0 0 * * *"
            cron_line_backup = f"{cron_schedule_backup} /usr/bin/php {BACKUP_SCRIPT_PATH} > /dev/null 2>&1 # {BACKUP_CRON_COMMENT}"
            
            # Existierende crons auslesen - WICHTIG: Als install_user!
            result = subprocess.run(
                ["sudo", "-u", self.install_user, "crontab", "-l"],
                capture_output=True,
                text=True
            )
            existing_crons = result.stdout if result.returncode == 0 else ""
            
            # Neueste cron-Linie (alte entfernen, neue hinzufügen)
            new_crons = []
            for line in existing_crons.split('\n'):
                if line.strip() and CRON_COMMENT not in line and BACKUP_CRON_COMMENT not in line:
                    new_crons.append(line)
            
            # Füge die neuen Cron-Einträge hinzu
            if cron_line_plot:
                new_crons.append(cron_line_plot)
            new_crons.append(cron_line_backup) # Backup-Cron immer hinzufügen

            new_crons_text = '\n'.join(new_crons)
            
            # Stelle sicher, dass der Text mit Newline endet
            if new_crons_text and not new_crons_text.endswith('\n'):
                new_crons_text += '\n'
            
            # In crontab eintragen - WICHTIG: Als install_user!
            process = subprocess.Popen(
                ["sudo", "-u", self.install_user, "crontab", "-"],
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
            if cron_line_plot:
                print(f"  Plot-Skript: {cron_schedule_plot} {plot_script}")
            print(f"  Backup-Skript: {cron_schedule_backup} {BACKUP_SCRIPT_PATH}")
            return True
        
        except Exception as e:
            logger.error(f"❌ Fehler beim crontab-Setup: {str(e)}")
            return False
    
    def remove_crontab(self):
        """Entfernt E3DC-Crontab"""
        try:
            # Lese crontab als install_user
            result = subprocess.run(
                ["sudo", "-u", self.install_user, "crontab", "-l"],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                print("ℹ️  Kein Crontab gefunden")
                return True
            
            # Entferne E3DC-Einträge
            new_crons = []
            for line in result.stdout.split('\n'):
                if line.strip() and CRON_COMMENT not in line and BACKUP_CRON_COMMENT not in line:
                    new_crons.append(line)
            
            new_crons_text = '\n'.join(new_crons)
            
            # Stelle sicher, dass der Text mit Newline endet
            if new_crons_text and not new_crons_text.endswith('\n'):
                new_crons_text += '\n'
            
            # Schreibe crontab als install_user
            process = subprocess.Popen(
                ["sudo", "-u", self.install_user, "crontab", "-"],
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
        elif minutes == 15:
            return "*/15 * * * *"
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
            try:
                os.chown(self.config_file, self.install_uid, self.www_data_gid)
                os.chmod(self.config_file, 0o664)
            except Exception as e:
                logger.warning(f"Konnte Berechtigungen nicht setzen: {e}")
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
            # Skript fehlt - erste Installation
            # Direkt fragen, ob aus ZIP installiert werden soll
            choice = input("\nDiagramm-System aus ZIP-Datei installieren? (j/n): ").strip().lower()
            
            if choice != 'j':
                print("Installation übersprungen")
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
        self.setup_crontab()
        
        # 6) Konfiguration speichern
        self.save_config()
        
        # 7) Zusammenfassung
        self.print_summary()
        return True
    
    def print_summary(self):
        """Zeigt Zusammenfassung"""
        host = self._get_local_host()
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
        print(f"➤ History-Backup: Täglich um Mitternacht")
        print(f"➤ Config: {self.config_file}")
        print("\n💡 Tipps:")
        print(f"  • Web-Interface: http://{host}/index.php")
        print(f"  • Diagramm direkt: http://{host}/diagramm.html")
        print("  • Manuell ausführen:")
        print(f"    python3 {self.plot_script_path}")
        if self.diagram_mode in ("auto", "hybrid"):
            print(f"  • Crontab prüfen: crontab -l")
            print(f"  • Crontab prüfen (für {self.install_user}): crontab -l")
        print("=" * 60 + "\n")

    @staticmethod
    def _get_local_host():
        """Ermittelt die lokale IP für die Anzeige im Tipp."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            ip_addr = sock.getsockname()[0]
            return ip_addr if ip_addr else "raspberrypi.local"
        except Exception:
            return "raspberrypi.local"
        finally:
            try:
                sock.close()
            except Exception:
                pass


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
    key="13",
    label="Diagramm-Installation & Automatisierung",
    func=install_diagramm,
    sort_order=130
)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    installer = DiagramInstaller()
    installer.run_installation()
