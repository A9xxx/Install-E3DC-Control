import os
import subprocess
import logging
from logging.handlers import RotatingFileHandler
import shutil
import zipfile
import sys

# Standard-Ausgabe auf UTF-8 erzwingen
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


_logging_initialized = False

def setup_logging():
    """Initialisiert das Logging in eine Datei."""
    global _logging_initialized
    if _logging_initialized:
        return
    
    # Logfile im Ordner Logs oberhalb von Installer
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(os.path.dirname(script_dir), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "install.log")
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not root_logger.handlers:
        handler = RotatingFileHandler(log_file, maxBytes=2*1024*1024, backupCount=2, encoding='utf-8')
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
        
    _logging_initialized = True

def run_command(cmd, timeout=10, use_shell=True):
    """Führt Shell-Kommando aus mit vollständiger Fehlerbehandlung und Logging."""
    setup_logging()
    logging.info(f"Kommando: {cmd}")
    try:
        result = subprocess.run(
            cmd, shell=use_shell, timeout=timeout,
            capture_output=True, text=True
        )
        if result.stdout.strip():
            logging.info(f"STDOUT: {result.stdout.strip()[:1000]}...") # Gekürzt für das Log
        if result.stderr.strip():
            logging.error(f"STDERR: {result.stderr.strip()[:1000]}...")
            
        return {
            'success': result.returncode == 0,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode
        }
    except subprocess.TimeoutExpired:
        logging.error("Fehler: Timeout ausgegeben")
        return {'success': False, 'stdout': '', 'stderr': 'Timeout', 'returncode': -1}
    except Exception as e:
        logging.error(f"Fehler bei Ausführung: {str(e)}")
        return {'success': False, 'stdout': '', 'stderr': str(e), 'returncode': -1}


def replace_in_file(path, key, new_line):
    """Ersetzt eine Konfigurationszeile in einer Datei."""
    if not os.path.exists(path):
        return False
    
    try:
        lines = []
        found = False
        
        with open(path, "r") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(key + " ") or stripped.startswith(key + "="):
                    lines.append(new_line + "\n")
                    found = True
                else:
                    lines.append(line)
        
        if not found:
            lines.append(new_line + "\n")
        
        with open(path, "w") as f:
            f.writelines(lines)
        
        return True
    except Exception as e:
        return False


def write_param(f, key, value, enabled=True):
    """Schreibt einen Parameter aktiv oder auskommentiert."""
    prefix = "" if enabled else "#"
    f.write(f"{prefix}{key} = {value}\n")


def apt_install(pkg):
    """Installiert apt-Paket wenn nicht vorhanden."""
    print(f"→ Prüfe {pkg}…")
    result = subprocess.run(
        f"dpkg -s {pkg}",
        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if result.returncode != 0:
        print(f"→ Installiere {pkg}…")
        cmd_result = run_command(f"sudo apt-get install -y {pkg}", timeout=300)
        if cmd_result['success']:
            print(f"✓ {pkg} installiert.")
        else:
            print(f"⚠ {pkg} möglicherweise nicht korrekt installiert.")
    else:
        print(f"✓ {pkg} bereits installiert.")


def pip_install(pkg):
    """Installiert Python-Paket wenn nicht vorhanden."""
    print(f"→ Prüfe Python-Paket {pkg}…")
    result = subprocess.run(
        f"pip3 show {pkg}",
        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if result.returncode != 0:
        print(f"→ Installiere {pkg}…")
        cmd_result = run_command(f"sudo pip3 install {pkg} --break-system-packages", timeout=60)
        if cmd_result['success']:
            print(f"✓ {pkg} installiert.")
        else:
            print(f"⚠ {pkg} möglicherweise nicht korrekt installiert.")
    else:
        print(f"✓ {pkg} bereits installiert.")


def ensure_dir(path):
    """Erstellt Verzeichnis wenn nicht vorhanden."""
    try:
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        return True
    except Exception:
        return False


def command_exists(cmd):
    """Prüft, ob ein Befehl im System verfügbar ist."""
    return shutil.which(cmd) is not None

def get_web_version():
    """Liest die Version aus /var/www/html/VERSION."""
    path = "/var/www/html/VERSION"
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return f.read().strip()
        except Exception:
            return "0.0.0"
    return "0.0.0"

def get_installer_bundle_version():
    """Liest die Version aus der E3DC-Control.zip (html/VERSION)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    zip_path = os.path.join(script_dir, "E3DC-Control.zip")
    
    if not os.path.exists(zip_path):
        return "0.0.0"
        
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # Pfad im ZIP kann variieren (mit/ohne Root-Ordner), daher flexibel suchen
            version_file_path = None
            for name in zf.namelist():
                if name.endswith('html/VERSION'):
                    version_file_path = name
                    break
            
            if version_file_path:
                with zf.open(version_file_path) as f:
                    return f.read().decode('utf-8').strip()
    except Exception:
        return "0.0.0"
    return "0.0.0"


def cleanup_pycache(start_path):
    """
    Bereinigt alle __pycache__-Ordner in einem gegebenen Pfad.
    """
    setup_logging()
    logging.info(f"Starte __pycache__-Bereinigung in {start_path}")
    
    for root, dirs, files in os.walk(start_path):
        if "__pycache__" in dirs:
            pycache_path = os.path.join(root, "__pycache__")
            logging.info(f"Entferne {pycache_path}")
            try:
                shutil.rmtree(pycache_path)
                print(f"✓ Cache in {os.path.basename(root)} entfernt.")
            except Exception as e:
                logging.error(f"Fehler beim Entfernen von {pycache_path}: {e}")
                print(f"⚠ Fehler beim Entfernen des Caches in {os.path.basename(root)}.")
    
    logging.info("__pycache__-Bereinigung abgeschlossen.")
