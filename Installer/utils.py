import os
import subprocess
import logging
import json
from logging.handlers import RotatingFileHandler
import shutil
import shlex
import sys
from pathlib import Path

from .installer_config import _resolve_install_root

# ---------------------------------------------------------------------------
# Zentrale Pfad-Aufloesung (V4) — NIEMALS Pfade hartcodieren!
# Lese-Reihenfolge: e3dc_v4.json → e3dc_paths.json → installer_config.json → Defaults
# Alle V4-Skripte sollen get_paths() statt eigener Aufloesung verwenden.
# ---------------------------------------------------------------------------
_paths_cache = None

_INSTALLER_DIR = Path(__file__).resolve().parent
_MODULE_INSTALL_ROOT = _INSTALLER_DIR.parent
_PATH_METADATA_FILES = (
    Path("/var/www/html/data/e3dc_v4.json"),
    Path("/var/www/html/e3dc_paths.json"),
    _INSTALLER_DIR / "installer_config.json",
)


def _read_path_metadata(path: Path) -> dict:
    """Liest nur eine kleine reguläre JSON-Metadatendatei und niemals einen Symlink."""
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_size > 1024 * 1024:
            return {}
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    nested = data.get("config")
    if isinstance(nested, dict):
        merged = dict(nested)
        merged.update(data)
        merged.pop("config", None)
        return merged
    return data


def _validated_install_root(value) -> Path:
    """Akzeptiert nur einen vorhandenen Produktroot mit Release-Markern."""
    candidate = Path(str(value or "").strip())
    if not candidate.is_absolute():
        raise RuntimeError("Installationspfad fehlt oder ist nicht absolut")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Installationspfad ist nicht aufloesbar") from exc
    markers = (
        resolved / "VERSION",
        resolved / "installer_main.py",
        resolved / "Installer" / "installer_config.py",
    )
    if not all(marker.is_file() for marker in markers):
        raise RuntimeError("Installationspfad besitzt nicht alle Release-Marker")
    return resolved


def _validated_optional_absolute(value, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = Path(text)
    if not candidate.is_absolute():
        raise RuntimeError(f"{label} ist nicht absolut")
    return str(candidate)


def get_paths() -> dict:
    """Löst Laufzeitpfade auf, ohne ein Benutzerverzeichnis zu suchen oder zu raten.

    Der Produktstamm stammt aus einem expliziten Umgebungswert, kanonischen
    Installermetadaten oder exakt diesem Release-Baum. Fehlende Konto- und
    Venv-Metadaten bleiben leer, damit Verbraucher fehlersicher sperren können.
    """
    global _paths_cache
    if _paths_cache is not None:
        return _paths_cache

    metadata = [_read_path_metadata(path) for path in _PATH_METADATA_FILES]
    explicit_root = str(os.environ.get("E3DC_INSTALL_ROOT") or "").strip()
    configured_root = next(
        (str(data.get("install_path") or "").strip() for data in metadata if data.get("install_path")),
        "",
    )
    root = Path(_resolve_install_root(_MODULE_INSTALL_ROOT, explicit_root, configured_root))

    def first_value(key: str, env_name: str = "") -> str:
        if env_name:
            explicit = str(os.environ.get(env_name) or "").strip()
            if explicit:
                return explicit
        return next((str(data.get(key) or "").strip() for data in metadata if data.get(key)), "")

    home_dir = _validated_optional_absolute(first_value("home_dir", "E3DC_HOME_DIR"), "Home-Verzeichnis")
    venv_path = _validated_optional_absolute(first_value("venv_path", "E3DC_VENV_PATH"), "Venv-Pfad")
    install_user = first_value("install_user", "E3DC_INSTALL_USER")
    normalized_user = install_user.replace("-", "").replace("_", "").replace(".", "")
    if install_user and not normalized_user.isalnum():
        raise RuntimeError("Installationsbenutzer ist ungültig")

    result = {
        'install_path': str(root),
        'home_dir': home_dir,
        'install_user': install_user,
        'venv_path': venv_path,
        'ramdisk_dir': '/var/www/html/ramdisk',
        'data_dir': '/var/www/html/data',
        'web_dir': '/var/www/html',
    }

    _paths_cache = result
    return result

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

    # Bei einem Release-Bootstrap läuft dieses Modul aus einem versiegelten
    # Ausführungssnapshot. Installationslogs gehören dennoch ausschließlich
    # in den explizit gebundenen Produktbaum.
    log_dir = os.path.join(get_install_path(), "logs")
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

def run_command(cmd, timeout=10, use_shell=True, cwd=None):
    """Führt Shell-Kommando aus mit vollständiger Fehlerbehandlung und Logging."""
    setup_logging()
    logging.info(f"Kommando: {cmd}")
    try:
        result = subprocess.run(
            cmd, shell=use_shell, timeout=timeout,
            capture_output=True, text=True, cwd=cwd
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


def format_command_failure(result):
    """Liefert eine hilfreiche Fehlermeldung aus stdout, stderr und Returncode."""
    parts = []
    stderr = (result.get('stderr') or '').strip()
    stdout = (result.get('stdout') or '').strip()
    if stderr:
        parts.append(stderr)
    if stdout:
        parts.append(stdout)
    if result.get('returncode') is not None:
        parts.append(f"Returncode: {result.get('returncode')}")
    return "\n".join(parts) if parts else "kein Fehlertext vom System"


def command_as_user(command, user=None):
    """Fuehrt ein Kommando nur dann via sudo -u aus, wenn der Zielnutzer abweicht."""
    if not user:
        return command
    try:
        import pwd
        if os.geteuid() == pwd.getpwnam(user).pw_uid:
            return command
    except Exception:
        pass
    return f"sudo -u {shlex.quote(user)} {command}"


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


def ensure_apache_php_module():
    """Stellt sicher, dass Apache PHP-Dateien ausfuehrt statt als Text auszuliefern."""
    print("→ Aktiviere Apache PHP-Modul…")
    run_command("sudo a2dismod mpm_event mpm_worker >/dev/null 2>&1 || true", timeout=30)
    run_command("sudo a2enmod mpm_prefork", timeout=30)
    php_module_cmd = (
        "PHP_MOD=$(php -r 'echo \"php\".PHP_MAJOR_VERSION.\".\".PHP_MINOR_VERSION;' 2>/dev/null); "
        "if [ -n \"$PHP_MOD\" ]; then sudo a2enmod \"$PHP_MOD\"; "
        "else sudo a2enmod php8.4 || sudo a2enmod php8.3 || sudo a2enmod php8.2 || sudo a2enmod php8.1 || sudo a2enmod php7.4; fi"
    )
    result = run_command(php_module_cmd, timeout=30)
    if result['success']:
        print("✓ Apache PHP-Modul aktiv.")
    else:
        print("⚠ Apache PHP-Modul konnte nicht sicher aktiviert werden.")
    run_command("sudo apache2ctl configtest", timeout=30)


def pip_install(pkg, venv_path=None, user=None):
    """
    Installiert Python-Paket. Bevorzugt in ein Virtual Environment (venv).
    """
    if venv_path and os.path.exists(os.path.join(venv_path, "bin", "pip")):
        pip_bin = os.path.join(venv_path, "bin", "pip")
        check_cmd = command_as_user(f"{shlex.quote(pip_bin)} show {shlex.quote(pkg)}", user)
        install_cmd = command_as_user(f"{shlex.quote(pip_bin)} install {shlex.quote(pkg)}", user)

        print(f"→ Prüfe Python-Paket {pkg} im venv…")
        res = subprocess.run(check_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode != 0:
            print(f"→ Installiere {pkg} im venv…")
            result = run_command(install_cmd, timeout=300)
            if result['success']:
                print(f"✓ {pkg} im venv installiert.")
                return True
            print(f"⚠ Fehler bei Installation im venv: {format_command_failure(result)}")
            return False
        else:
            print(f"✓ {pkg} bereits im venv vorhanden.")
            return True

    # Fallback: Globale Installation (System-Python)
    print(f"→ Prüfe Python-Paket {pkg} (global)…")
    pkg_quoted = shlex.quote(pkg)
    result = subprocess.run(
        f"pip3 show {pkg_quoted}",
        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if result.returncode != 0:
        print(f"→ Installiere {pkg} systemweit (global)…")
        # PEP 668 Fallback: --break-system-packages for current OS releases
        cmd_result = run_command(f"sudo pip3 install {pkg_quoted} --break-system-packages", timeout=60)
        if cmd_result['success']:
            print(f"✓ {pkg} global installiert.")
            return True
        print(f"⚠ {pkg} global möglicherweise nicht korrekt installiert.")
        return False
    else:
        print(f"✓ {pkg} bereits global vorhanden.")
    return True


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
    return get_web_version()


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

# --- MIGRATED FROM system.py & service_setup.py ---
# --- MIGRATED FROM system.py & service_setup.py ---
from .installer_config import get_install_path, get_install_user, get_home_dir, load_config, get_venv_path, get_venv_name, get_venv_pip, get_user_ids
import tempfile
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

system_logger = get_or_create_logger("system")
service_logger = get_or_create_logger("service_setup")


PYTHON_PACKAGES = [
    "paho-mqtt",            # MQTT (wallbox_manager, mqtt_hub)
    "requests",             # HTTP (bluelink, APIs)
    "websocket-client",     # Luxtronik WebSocket (lux_live.py)
    "websockets",           # e3dc_websocket.py server
    "pymodbus",             # Modbus-TCP (energy_manager, idm_live)
    "hyundai_kia_connect_api", # Bluelink / Fahrzeug SoC
    "pywebpush",            # Web-Push Notifications (braucht rustc/cargo!)
    "py3rijndael"           # RSCP AES Verschluesselung (rscp_client.py)
]


def resolve_venv_target(install_user):
    """Ermittelt das Ziel-venv und aktiviert den Standardpfad fuer Reparaturen."""
    venv_name = get_venv_name()
    if venv_name:
        return venv_name, get_venv_path(install_user)

    venv_name = ".venv_e3dc"
    venv_path = os.path.join(get_home_dir(install_user), venv_name)
    try:
        from .installer_config import save_config, ensure_web_config

        config = load_config()
        config["install_user"] = install_user
        config["venv_name"] = venv_name
        config["venv_path"] = venv_path
        save_config(config)
        ensure_web_config(install_user)
        print(f"→ Venv-Konfiguration aktiviert: {venv_name}")
    except Exception as exc:
        print(f"⚠ Venv-Konfiguration konnte nicht gespeichert werden: {exc}")
    return venv_name, venv_path


V4_SYSTEM_PACKAGES = [
    # Web-Server
    "apache2", "php", "libapache2-mod-php", "php-curl", "php-sqlite3", "php-mbstring",
    # Python Grundausstattung
    "python3", "python3-pip", "python3-venv",
    "python3-websockets",
    # Python Bibliotheken (via apt, fuer ML / Datenverarbeitung)
    "python3-sklearn", "python3-numpy", "python3-cryptography",
    "python3-bs4",          # Luxtronik WebSocket-Scraping (lux_live.py)
    # System-Hilfspakete
    "curl",                 # allgemein nuetzlich
    "git",                  # Self-Update (UPDATE_POLICY.json)
    # Rust (pywebpush braucht dies beim pip-compile)
    "rustc", "cargo", "libffi-dev", "python3-dev",
]

# Matter ist eine optionale Produktfunktion. Diese Pakete dürfen deshalb weder
# den Core-Updatepfad noch eine Snapshot- oder Neuinstallation blockieren.
# Ausschließlich der explizite Matter-Installer installiert sie gemeinsam.
MATTER_SYSTEM_PACKAGES = (
    "nodejs",
    "npm",
    "avahi-daemon",
    "avahi-utils",
    "dbus",
)

LEGACY_CPP_PACKAGES = [
    "build-essential", "cmake",
    "libcurl4-openssl-dev", "libssl-dev",
    "libmosquitto-dev", "libjsoncpp-dev",
    "libsqlite3-dev",
    "jq",
]


def get_required_system_packages(include_legacy_cpp=None):
    """Liefert die Apt-Paketliste fuer Native V4 und optional den alten C++-Pfad."""
    packages = list(V4_SYSTEM_PACKAGES)
    if include_legacy_cpp is None:
        include_legacy_cpp = os.path.exists("/etc/systemd/system/e3dc.service")
    if include_legacy_cpp:
        packages.extend(LEGACY_CPP_PACKAGES)
    return packages


def install_apt_package_list(packages, *, log_label="Systempakete"):
    print("→ Installiere Systempakete…\n")
    system_logger.info(f"Installiere {len(packages)} {log_label}.")
    for pkg in packages:
        apt_install(pkg)


def _apt_package_installed(package):
    """Prüft den dpkg-Status eines Pakets ohne Seiteneffekt."""
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status}", package],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "install ok installed"


def install_apt_package_transaction(packages, *, log_label="optionale Systempakete"):
    """Installiert eine explizite Paketgruppe in genau einer Apt-Transaktion.

    Paketentfernungen sind untersagt. Ein Apt-Fehler oder ein unvollständiger
    Endzustand wird als ``False`` zurückgegeben, damit der Aufrufer fail-closed
    vor weiteren Produkt- oder Servicewirkungen abbrechen kann.
    """
    normalized = []
    for package in packages:
        package = str(package).strip()
        if not package or any(not (char.isalnum() or char in "+-.") for char in package):
            print(f"✗ Ungültiger Apt-Paketname: {package!r}")
            return False
        if package not in normalized:
            normalized.append(package)

    if not normalized:
        return True

    missing = [package for package in normalized if not _apt_package_installed(package)]
    if not missing:
        print(f"✓ {log_label} bereits vollständig installiert.")
        return True

    print(f"→ Installiere {log_label} gemeinsam: {', '.join(normalized)}")
    apt_argv = ["sudo", "apt-get", "install", "-y", "--no-remove", "--", *normalized]
    installation = run_command(apt_argv, timeout=300, use_shell=False)
    if not installation.get("success"):
        detail = (installation.get("stderr") or installation.get("stdout") or "unbekannter Apt-Fehler").strip()
        print(f"✗ Apt-Installation für {log_label} fehlgeschlagen: {detail}")
        return False

    missing_after = [package for package in normalized if not _apt_package_installed(package)]
    if missing_after:
        print(f"✗ Apt-Endzustand unvollständig; weiterhin fehlend: {', '.join(missing_after)}")
        return False

    print(f"✓ {log_label} vollständig installiert.")
    return True


def prepare_system_packages_for_snapshot(use_venv=True):
    """Installiert nur die wiederverwendbare Paketbasis und beendet danach."""
    print("\n=== Systempakete für Snapshot vorbereiten ===\n")
    print("Dieser Modus installiert Apt-Pakete und Python-Abhängigkeiten.")
    print("Projektkonfiguration, Dienste und Webportal werden hier nicht eingerichtet.\n")

    cpp_still_active = os.path.exists("/etc/systemd/system/e3dc.service")
    if cpp_still_active:
        print("  [i] Legacy e3dc.service erkannt - installiere auch C++ Build-Abhängigkeiten.")
    else:
        print("  [i] V4 Native Mode - C++ Build-Pakete werden nicht installiert.")

    packages = get_required_system_packages(include_legacy_cpp=cpp_still_active)
    if not install_apt_package_transaction(packages, log_label="Snapshot-Systempakete"):
        return False

    if use_venv:
        if setup_venv(show_header=False) is False:
            return False
    else:
        if install_python_packages() is False:
            return False

    print("\n✓ Systempakete und Python-Abhängigkeiten vorbereitet.")
    print("  Du kannst jetzt einen Container-/VM-Snapshot erstellen und den Installer beenden.\n")
    system_logger.info("Snapshot-Paketbasis vorbereitet.")
    log_task_completed("Systempakete für Snapshot vorbereiten")
    return True

def setup_venv(show_header=False):
    """Richtet das Python Virtual Environment ein."""
    if show_header:
        print("\n=== Python Virtual Environment einrichten ===\n")

    install_user = get_install_user()
    venv_name, venv_path = resolve_venv_target(install_user)

    # Altes venv im E3DC-Control Ordner bereinigen (Migration)
    old_venv_path = os.path.join(get_install_path(), venv_name)
    if os.path.exists(old_venv_path) and os.path.isdir(old_venv_path):
        if os.path.abspath(old_venv_path) != os.path.abspath(venv_path):
            print(f"→ Entferne veraltetes venv: {old_venv_path}")
            try:
                shutil.rmtree(old_venv_path)
                print("✓ Altes venv bereinigt.")
            except Exception as e:
                print(f"⚠ Konnte altes venv nicht löschen: {e}")

    print(f"→ Ziel: {venv_path}")

    if not os.path.exists(venv_path):
        print("→ Erstelle venv…")
        # Erstelle venv mit Zugriff auf System-Pakete (für apt-installierte Module wie RPi.GPIO falls nötig)
        create_cmd = command_as_user(
            f"python3 -m venv {shlex.quote(venv_path)} --system-site-packages",
            install_user,
        )
        res = run_command(create_cmd, timeout=60)
        if res['success']:
            print("✓ venv erstellt.")
            system_logger.info(f"Virtual Environment erstellt: {venv_path}")
        else:
            print(f"✗ Fehler beim Erstellen: {format_command_failure(res)}")
            return False
    else:
        print("✓ venv existiert bereits.")

    venv_pip = get_venv_pip(install_user)

    # Nutze zentrale Installationsfunktion
    if install_python_packages() is False:
        return False

    if show_header:
        print("\n✓ Python-Umgebung eingerichtet.\n")
        log_task_completed("Python venv eingerichtet")
    return True


def list_venv_packages():
    """Listet installierte Pakete im venv auf."""
    print("\n=== Python venv Pakete ===\n")

    install_user = get_install_user()
    venv_name = get_venv_name() or ".venv_e3dc"
    venv_pip = os.path.join(get_home_dir(install_user), venv_name, "bin", "pip")

    if not os.path.exists(venv_pip):
        print("✗ Kein venv gefunden.")
        return

    res = run_command(command_as_user(f"{shlex.quote(venv_pip)} list", install_user))
    if res['success']:
        print(res['stdout'])
    else:
        print(f"✗ Fehler: {format_command_failure(res)}")
    print()


def install_python_packages():
    """Installiert Python-Pakete (bevorzugt im venv)."""
    install_user = get_install_user()
    venv_name, venv_path = resolve_venv_target(install_user)

    print(f"\n→ Installiere Python-Pakete im venv ({venv_name})…")
    system_logger.info(f"Installiere {len(PYTHON_PACKAGES)} Python-Pakete im venv.")

    failed = [
        pkg
        for pkg in PYTHON_PACKAGES
        if pip_install(pkg, venv_path=venv_path, user=install_user) is False
    ]
    if failed:
        print("✗ Python-Paketinstallation unvollständig: " + ", ".join(failed))
        return False
    return True


def cleanup_legacy_python_packages(use_venv=True):
    """Entfernt alte, schwere Diagramm-Pakete, um Speicherplatz (SD-Karte) freizugeben."""
    print("\n→ Bereinige veraltete Python-Pakete (Plotly, Pandas)...")
    legacy_apt = ["python3-plotly", "python3-pandas"]
    run_command("sudo apt-get remove -y " + " ".join(legacy_apt))
    run_command("sudo apt-get autoremove -y")

    legacy_pip = ["plotly", "pandas", "pandas-stubs", "matplotlib", "pytz", "kaleido"]
    install_user = get_install_user()
    if use_venv:
        venv_name = get_venv_name() or ".venv_e3dc"
        venv_pip = os.path.join(get_home_dir(install_user), venv_name, "bin", "pip")
        if os.path.exists(venv_pip):
            uninstall_cmd = f"{shlex.quote(venv_pip)} uninstall -y " + " ".join(shlex.quote(pkg) for pkg in legacy_pip)
            run_command(command_as_user(uninstall_cmd, install_user))
    else:
        run_command("sudo pip3 uninstall -y --break-system-packages " + " ".join(legacy_pip))
    print("  ✓ Veraltete Pakete entfernt (Speicherplatz freigegeben).")


def install_system_packages(use_venv=True):
    """Installiert alle notwendigen Systempakete."""
    print("\n=== Systempakete installieren ===\n")
    system_logger.info("Starte Installation der System- und Python-Pakete.")

    cpp_still_active = os.path.exists("/etc/systemd/system/e3dc.service")
    if cpp_still_active:
        print("  [i] Legacy e3dc.service erkannt - installiere auch C++ Build-Abhängigkeiten.")
    else:
        print("  [i] V4 Native Mode - C++ Build-Pakete werden nicht installiert.")

    packages = get_required_system_packages(include_legacy_cpp=cpp_still_active)
    if not install_apt_package_transaction(packages):
        return False

    cleanup_legacy_python_packages(use_venv)

    # --- NEU: Apache PHP + WebSocket Reverse Proxy automatisch einrichten ---
    ensure_apache_php_module()
    print("\n→ Konfiguriere Apache Reverse Proxy für WebSockets...")
    run_command("sudo a2enmod proxy")
    run_command("sudo a2enmod proxy_wstunnel")

    conf_path = "/etc/apache2/sites-available/000-default.conf"
    if os.path.exists(conf_path):
        with open(conf_path, "r") as f:
            content = f.read()

        modified = False
        if 'ProxyPass "/ws"' not in content:
            content = content.replace("</VirtualHost>", '    ProxyPass "/ws" "ws://127.0.0.1:8765/"\n</VirtualHost>')
            modified = True
        elif '127.0.0.1:8080' in content:
            content = content.replace('127.0.0.1:8080', '127.0.0.1:8765')
            modified = True

        if modified:
            with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
                tmp.write(content)
                tmp_name = tmp.name
            run_command(f"sudo cp {tmp_name} {conf_path}")
            os.remove(tmp_name)
            print("  ✓ Proxy-Regel erfolgreich in Apache-Konfiguration auf Port 8765 gesetzt.")
            system_logger.info("Apache Proxy-Regel für WebSockets auf Port 8765 aktualisiert.")
        else:
            print("  ✓ Proxy-Regel existiert bereits korrekt.")

    run_command("sudo systemctl restart apache2")
    # ------------------------------------------------------------------

    # Python Umgebung einrichten
    if use_venv:
        if setup_venv(show_header=False) is False:
            return False
    else:
        if install_python_packages() is False:
            return False

    # WebSocket Service am Ende der Paket-Installation mit einrichten
    installer_dir = os.path.join(get_install_path(), "Installer")
    ws_script = os.path.join(installer_dir, "e3dc_websocket.py")
    if os.path.exists(ws_script):
        setup_websocket_service()

    # Wrapper & Sudoers für Web-UI Task Manager einrichten
    setup_service_wrapper()

    print("\n✓ Systempakete vollständig installiert.\n")
    system_logger.info("Installation der Pakete abgeschlossen.")
    log_task_completed("Systempakete installieren")
    return True


def setup_service_wrapper():
    """Delegiert Wrapper und sudoers an den zentralen fail-closed Reparaturpfad."""
    print("→ Richte Web-UI Service Wrapper ein...")
    from . import web_installer

    if web_installer.is_docker():
        print("  ✓ Docker: kein systemd-/sudoers-Wrapper erforderlich.")
        return True

    result = web_installer.repair_permissions(repair_runtime=False)
    if not result.get("success"):
        message = result.get("message") or "Wrapper-/sudoers-Reparatur fehlgeschlagen."
        system_logger.error("Zentrale Wrapper-/sudoers-Reparatur fehlgeschlagen: %s", result)
        raise RuntimeError(message)

    head = str((result.get("wrapper_integrity") or {}).get("head") or "")[:12]
    suffix = f" (Git-HEAD {head})" if head else ""
    print(f"  ✓ Wrapperintegrität und sudoers atomar geprüft{suffix}.")
    return True



def setup_websocket_service():
    """Richtet den E3DC WebSocket Server als Systemd-Dienst ein."""
    print("→ Richte e3dc-websocket Service ein...")
    install_user = get_install_user()
    install_path = get_install_path()
    installer_dir = os.path.join(get_install_path(), "Installer")

    service_content = f"""[Unit]
Description=E3DC WebSocket Server fuer fluessige Dashboard Animationen
After=network.target apache2.service

[Service]
Type=simple
User={install_user}
Group=www-data
WorkingDirectory={installer_dir}
ExecStart=/usr/bin/python3 {installer_dir}/e3dc_websocket.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=e3dc-websocket

[Install]
WantedBy=multi-user.target
"""
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(service_content)
        tmp_name = tmp.name

    run_command(f"sudo cp {tmp_name} /etc/systemd/system/e3dc-websocket.service")
    os.remove(tmp_name)
    run_command("sudo systemctl daemon-reload")
    run_command("sudo systemctl enable e3dc-websocket.service")
    run_command("sudo systemctl start e3dc-websocket.service")
    print("  ✓ Service e3dc-websocket erstellt und gestartet.")


def _create_service_file(
    service_name,
    description,
    python_script_rel_path,
    script_executor="python3",
    restart_sec=60,
    start_service=True,
    enable_service=True,
    restart_policy="always",
    nice=None,
    io_scheduling_class=None,
    after_services=(),
):
    """Generische Helper Funktion um Python-Daemons als Systemd Service anzulegen."""
    print(f"\n=== {description} Service einrichten ===\n")
    service_logger.info(f"Richte Dienst {service_name} ein für {python_script_rel_path}.")

    install_user = get_install_user()
    service_path = f"/etc/systemd/system/{service_name}.service"

    # Der Code kann aus einem versiegelten Release-Snapshot importiert sein.
    # Units dürfen ausschließlich auf den gebundenen Produktbaum zeigen.
    installer_dir = os.path.join(get_install_path(), "Installer")

    script_abs_path = os.path.normpath(os.path.join(installer_dir, python_script_rel_path))
    working_dir = os.path.dirname(script_abs_path)

    if not os.path.exists(script_abs_path):
        service_logger.error(f"FATAL: Skript {script_abs_path} nicht gefunden!")
        # Fallback falls relativer Pfad komisch war
        # INSTALL_PATH import fehlt vielleicht, wir nutzen installer_dir

    # Virtual Environment laden, falls genutzt
    script_executor = "python3"
    venv_name = load_config().get("venv_name", ".venv_e3dc")
    if os.path.exists(os.path.join(get_install_path(), venv_name, "bin", "python")):
        script_executor = os.path.join(get_install_path(), venv_name, "bin", "python")
    else:
        configured_venv = str(get_paths().get("venv_path") or "").strip()
        configured_python = os.path.join(configured_venv, "bin", "python") if configured_venv else ""
        if configured_python and os.path.exists(configured_python):
            script_executor = configured_python

    after_units = ["network.target"]
    after_units.extend(
        str(item).strip()
        for item in (after_services or ())
        if str(item).strip()
    )
    service_tuning = ""
    if nice is not None:
        service_tuning += f"Nice={int(nice)}\n"
    if io_scheduling_class:
        service_tuning += f"IOSchedulingClass={str(io_scheduling_class).strip()}\n"

    service_content = f"""[Unit]
Description={description}
After={' '.join(after_units)}

[Service]
Type=simple
User={install_user}
Group=www-data
WorkingDirectory={working_dir}
ExecStart={script_executor} {script_abs_path}
Restart={restart_policy}
RestartSec={restart_sec}
{service_tuning}

[Install]
WantedBy=multi-user.target
"""
    try:
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
            tmp.write(service_content)
            tmp_path = tmp.name

        run_command(f"sudo mv {tmp_path} {service_path}")
        run_command(f"sudo chmod 644 {service_path}")
        run_command("sudo systemctl daemon-reload")
        if enable_service:
            run_command(f"sudo systemctl enable {service_name}")
            run_command(f"sudo systemctl reset-failed {service_name} 2>/dev/null || true")
            if start_service:
                run_command(f"sudo systemctl restart {service_name}")
                print(f"✓ Service '{service_name}' installiert und gestartet.\n")
            else:
                print(f"✓ Service '{service_name}' installiert und aktiviert; Start wird gesammelt ausgeführt.\n")
        else:
            run_command(f"sudo systemctl disable {service_name} 2>/dev/null || true")
            run_command(f"sudo systemctl stop {service_name} 2>/dev/null || true")
            print(f"✓ Service '{service_name}' installiert und bleibt ausgeschaltet.\n")
        log_task_completed(f"Service {service_name} eingerichtet")
        return True
    except Exception as e:
        print(f"✗ Fehler beim Erstellen des Services {service_name}: {e}")
        log_error("service_setup", f"Fehler Service {service_name}: {e}", e)
        return False


def install_e3dc_live_service(start_service=True):
    """Richtet den e3dc-live Python RSCP Dienst als Systemd-Service ein.

    Dieser Dienst ist der native Python-Ersatz fuer den C++ Daten-Export.
    Er liest Echtzeit-Daten direkt per RSCP aus dem E3DC und schreibt sie
    alle 3 Sekunden in /var/www/html/ramdisk/live_data_py.json.
    """
    print("\n=== E3DC Live Data Service (Python RSCP) einrichten ===\n")
    service_logger.info("Richte e3dc-live Service ein.")

    install_user = get_install_user()
    service_path = "/etc/systemd/system/e3dc-live.service"

    installer_dir = os.path.join(get_install_path(), "Installer")
    script_path = os.path.join(installer_dir, "e3dc_live.py")

    if not os.path.exists(script_path):
        print(f"[!] Skript nicht gefunden: {script_path}")
        service_logger.error(f"e3dc_live.py nicht gefunden unter {script_path}")
        return False

    venv_name = load_config().get("venv_name", ".venv_e3dc")
    python_exec = "/usr/bin/python3"
    configured_venv = str(get_paths().get("venv_path") or "").strip()
    for candidate in [
        os.path.join(get_install_path(), venv_name, "bin", "python3"),
        os.path.join(configured_venv, "bin", "python3") if configured_venv else "",
    ]:
        if os.path.exists(candidate):
            python_exec = candidate
            break

    service_content = f"""[Unit]
Description=E3DC RSCP Live Data Service (Python Native)
Documentation=https://github.com/A9xxx/Install-E3DC-Control
After=network.target network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User={install_user}
Group=www-data
WorkingDirectory={installer_dir}
ExecStart={python_exec} {script_path} --write --loops 0 --interval 3
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=e3dc-live

[Install]
WantedBy=multi-user.target
"""
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.service', delete=False) as tmp:
            tmp.write(service_content)
            tmp_path = tmp.name

        run_command(f"sudo mv {tmp_path} {service_path}")
        run_command(f"sudo chmod 644 {service_path}")
        run_command("sudo systemctl daemon-reload")
        run_command("sudo systemctl enable e3dc-live")
        run_command("sudo systemctl reset-failed e3dc-live 2>/dev/null || true")

        if start_service:
            run_command("sudo systemctl stop e3dc-live")
            result = run_command("sudo systemctl start e3dc-live")
        else:
            result = {"success": True, "stderr": ""}

        if result['success']:
            if start_service:
                print("[OK] Service 'e3dc-live' installiert, aktiviert und gestartet.")
            else:
                print("[OK] Service 'e3dc-live' installiert und aktiviert; Start wird gesammelt ausgeführt.")
            log_task_completed("e3dc-live Service eingerichtet")
        else:
            print(f"[!] Service konnte nicht gestartet werden: {result.get('stderr', '')}")
            log_warning("service_setup", f"e3dc-live Start-Warnung: {result.get('stderr', '')}")

        ramdisk = "/var/www/html/ramdisk"
        if not os.path.exists(ramdisk):
            run_command(f"sudo mkdir -p {ramdisk}")
        run_command(f"sudo chown {install_user}:www-data {ramdisk}")
        run_command(f"sudo chmod 775 {ramdisk}")

        return True
    except Exception as exc:
        print(f"[!] Fehler beim Einrichten des e3dc-live Service: {exc}")
        log_error("service_setup", f"Fehler e3dc-live Service: {exc}", exc)
        return False
