import os
import pwd
import subprocess
import logging
import json
import stat
from logging.handlers import RotatingFileHandler
import shutil
import shlex
import sys
from pathlib import Path

from .installer_config import _resolve_install_root
from .secure_file_transaction import (
    atomic_write_bound_file,
    exclusive_transaction_lock,
    restore_bound_file,
    snapshot_bound_file,
    snapshots_match,
)

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


APACHE_DEFAULT_SITE_CONF = "/etc/apache2/sites-available/000-default.conf"
WEBSOCKET_PROXY_LINE = 'ProxyPass "/ws" "ws://127.0.0.1:8765/"'


def ensure_apache_php_module():
    """Aktiviert den erforderlichen Apache-PHP-Vertrag fail-closed."""
    print("→ Aktiviere Apache PHP-Modul…")
    # Das Abschalten nicht verwendeter MPMs bleibt tolerant: Auf einem frischen
    # System können einzelne Module bereits fehlen. Das anschließende Aktivieren
    # von mpm_prefork muss dagegen nachweislich gelingen.
    run_command("sudo a2dismod mpm_event mpm_worker >/dev/null 2>&1 || true", timeout=30)
    prefork_result = run_command("sudo a2enmod mpm_prefork", timeout=30)
    if not prefork_result.get("success"):
        print(
            "✗ Apache mpm_prefork konnte nicht aktiviert werden: "
            + format_command_failure(prefork_result)
        )
        return False

    php_module_cmd = (
        "PHP_MOD=$(php -r 'echo \"php\".PHP_MAJOR_VERSION.\".\".PHP_MINOR_VERSION;' 2>/dev/null); "
        "if [ -n \"$PHP_MOD\" ]; then sudo a2enmod \"$PHP_MOD\"; "
        "else sudo a2enmod php8.4 || sudo a2enmod php8.3 || sudo a2enmod php8.2 || sudo a2enmod php8.1 || sudo a2enmod php7.4; fi"
    )
    result = run_command(php_module_cmd, timeout=30)
    if not result.get("success"):
        print(
            "✗ Apache PHP-Modul konnte nicht sicher aktiviert werden: "
            + format_command_failure(result)
        )
        return False

    configtest_result = run_command("sudo apache2ctl configtest", timeout=30)
    if not configtest_result.get("success"):
        print(
            "✗ Apache-Konfiguration ist nach der PHP-Aktivierung ungültig: "
            + format_command_failure(configtest_result)
        )
        return False

    print("✓ Apache PHP-Modul aktiv und Konfiguration gültig.")
    return True


def _websocket_proxy_config_is_exact(content):
    proxy_lines = [
        line.strip()
        for line in str(content or "").splitlines()
        if line.strip().startswith('ProxyPass "/ws"')
    ]
    return proxy_lines == [WEBSOCKET_PROXY_LINE]


def _render_websocket_proxy_config(content):
    """Erzeugt genau eine kanonische /ws-ProxyPass-Zeile."""
    lines = str(content or "").splitlines(keepends=True)
    proxy_indexes = [
        index
        for index, line in enumerate(lines)
        if line.strip().startswith('ProxyPass "/ws"')
    ]
    if len(proxy_indexes) > 1:
        return None

    if proxy_indexes:
        index = proxy_indexes[0]
        original = lines[index]
        indent = original[: len(original) - len(original.lstrip())]
        ending = "\r\n" if original.endswith("\r\n") else "\n"
        lines[index] = indent + WEBSOCKET_PROXY_LINE + ending
    else:
        closing_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.strip() == "</VirtualHost>"
            ),
            None,
        )
        if closing_index is None:
            return None
        closing_line = lines[closing_index]
        indent = closing_line[: len(closing_line) - len(closing_line.lstrip())]
        ending = "\r\n" if closing_line.endswith("\r\n") else "\n"
        lines.insert(closing_index, indent + "    " + WEBSOCKET_PROXY_LINE + ending)

    rendered = "".join(lines)
    return rendered if _websocket_proxy_config_is_exact(rendered) else None


def ensure_websocket_proxy_configuration(conf_path=APACHE_DEFAULT_SITE_CONF):
    """Schreibt die WebSocket-Proxyregel atomar und prüft den Endzustand."""
    try:
        with open(conf_path, "r", encoding="utf-8") as handle:
            original = handle.read()
    except OSError as exc:
        print(f"✗ Apache-Site-Konfiguration ist nicht lesbar: {conf_path}: {exc}")
        return False

    rendered = _render_websocket_proxy_config(original)
    if rendered is None:
        print(
            "✗ Apache-WebSocket-Proxyvertrag ist mehrdeutig oder besitzt "
            "keinen VirtualHost-Abschluss."
        )
        return False

    if rendered != original:
        local_tmp = None
        staged_path = f"{conf_path}.e3dc-control-{os.getpid()}.tmp"
        staged_created = False
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
                tmp.write(rendered)
                local_tmp = tmp.name

            install_result = run_command(
                "sudo install -o root -g root -m 0644 -- "
                + shlex.quote(local_tmp)
                + " "
                + shlex.quote(staged_path),
                timeout=30,
            )
            if not install_result.get("success"):
                print(
                    "✗ Apache-WebSocket-Konfiguration konnte nicht vorbereitet werden: "
                    + format_command_failure(install_result)
                )
                return False
            staged_created = True

            move_result = run_command(
                "sudo mv -f -- "
                + shlex.quote(staged_path)
                + " "
                + shlex.quote(conf_path),
                timeout=30,
            )
            if not move_result.get("success"):
                print(
                    "✗ Apache-WebSocket-Konfiguration konnte nicht atomar aktiviert werden: "
                    + format_command_failure(move_result)
                )
                return False
            staged_created = False
        finally:
            if local_tmp and os.path.exists(local_tmp):
                os.remove(local_tmp)
            if staged_created:
                run_command(
                    "sudo rm -f -- " + shlex.quote(staged_path),
                    timeout=30,
                )

    try:
        with open(conf_path, "r", encoding="utf-8") as handle:
            installed = handle.read()
    except OSError as exc:
        print(f"✗ Apache-WebSocket-Endzustand ist nicht lesbar: {conf_path}: {exc}")
        return False
    if not _websocket_proxy_config_is_exact(installed):
        print("✗ Apache-WebSocket-Endzustand enthält nicht exakt die erwartete ProxyPass-Regel.")
        return False

    print("  ✓ WebSocket-Proxyregel ist atomar und exakt auf Port 8765 gebunden.")
    return True


def pip_install(
    pkg,
    venv_path=None,
    user=None,
    *,
    require_venv=False,
    venv_name=None,
):
    """
    Installiert ein Python-Paket.

    Ein explizites oder erforderliches venv ist eine harte Vertrauensgrenze:
    fehlt dessen gebundener Python-/Pip-Vertrag, wird niemals auf das globale
    System-Python ausgewichen.
    """
    explicit_venv = venv_path is not None
    if explicit_venv or require_venv:
        install_user = user or get_install_user()
        try:
            if not venv_path:
                if explicit_venv and not require_venv:
                    raise RuntimeError("Expliziter Venv-Pfad ist leer")
                _venv_name, venv_path = resolve_venv_target(
                    install_user,
                    requested_venv_name=venv_name,
                )
            runtime = require_bound_venv_runtime(
                install_user=install_user,
                venv_name=venv_name,
                venv_path=venv_path,
            )
        except Exception as exc:
            print(f"✗ Gebundenes Python-venv ist nicht verwendbar: {exc}")
            return False

        python_bin = runtime["python"]
        check_cmd = _command_argv_as_user(
            [python_bin, "-I", "-m", "pip", "--isolated", "show", str(pkg)],
            install_user,
        )
        install_cmd = _command_argv_as_user(
            [
                python_bin,
                "-I",
                "-m",
                "pip",
                "--isolated",
                "--require-virtualenv",
                "install",
                str(pkg),
            ],
            install_user,
        )

        print(f"→ Prüfe Python-Paket {pkg} im venv…")
        check_result = run_command(
            check_cmd,
            timeout=60,
            use_shell=False,
        )
        if not check_result.get("success"):
            print(f"→ Installiere {pkg} im venv…")
            result = run_command(
                install_cmd,
                timeout=300,
                use_shell=False,
            )
            if result['success']:
                print(f"✓ {pkg} im venv installiert.")
                return True
            print(f"⚠ Fehler bei Installation im venv: {format_command_failure(result)}")
            return False
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
from .installer_config import get_install_path, get_install_user, get_home_dir, load_config, get_venv_name
import tempfile
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

system_logger = get_or_create_logger("system")
service_logger = get_or_create_logger("service_setup")

MANAGER_LOCK_NAMESPACE_ROOT = "/run/e3dc-control"
MANAGER_LOCK_DIRECTORY = f"{MANAGER_LOCK_NAMESPACE_ROOT}/locks"
MANAGER_LOCK_TMPFILES_CONFIG = "/etc/tmpfiles.d/e3dc-control-locks.conf"
MANAGER_LOCK_FILES = {
    "e3dc-storage-manager": f"{MANAGER_LOCK_DIRECTORY}/storage_manager.owner.lock",
    "e3dc-wallbox-manager": f"{MANAGER_LOCK_DIRECTORY}/wallbox_manager.owner.lock",
    "energy_manager": f"{MANAGER_LOCK_DIRECTORY}/energy_manager.owner.lock",
    "e3dc-heizstab": f"{MANAGER_LOCK_DIRECTORY}/heizstab_manager.owner.lock",
}
HEAT_ENDPOINT_LOCK_FILE = f"{MANAGER_LOCK_DIRECTORY}/heat_actuator_endpoints.lock"
MANAGER_LOCK_TMPFILES_CONTENT = f"""d {MANAGER_LOCK_NAMESPACE_ROOT} 0755 root root -
d {MANAGER_LOCK_DIRECTORY} 0755 root root -
f {MANAGER_LOCK_FILES['e3dc-storage-manager']} 0660 root www-data -
f {MANAGER_LOCK_FILES['e3dc-wallbox-manager']} 0660 root www-data -
f {MANAGER_LOCK_FILES['energy_manager']} 0660 root www-data -
f {MANAGER_LOCK_FILES['e3dc-heizstab']} 0660 root www-data -
f {HEAT_ENDPOINT_LOCK_FILE} 0660 root www-data -
"""


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


def _command_argv_as_user(argv, install_user):
    """Bindet eine Argumentliste ohne Shell an den Installationsbenutzer."""
    account = pwd.getpwnam(str(install_user))
    command = [str(item) for item in argv]
    if os.geteuid() == account.pw_uid:
        return command
    return ["sudo", "-H", "-u", account.pw_name, "--", *command]


def _validated_venv_name(value):
    """Akzeptiert nur einen einzelnen, ungefährlichen Verzeichnisnamen."""
    name = str(value or ".venv_e3dc").strip()
    if (
        not name
        or name in {".", ".."}
        or os.path.basename(name) != name
        or os.path.sep in name
        or (os.path.altsep and os.path.altsep in name)
        or any(not (char.isalnum() or char in "._-") for char in name)
    ):
        raise RuntimeError("Venv-Name ist ungültig")
    return name


def _bound_venv_target(
    install_user,
    requested_path=None,
    *,
    requested_name=None,
):
    """Bindet das venv exakt an Konto, Home und den konfigurierten Namen."""
    user = str(install_user or "").strip()
    if not user:
        raise RuntimeError("Installationsbenutzer fehlt")
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise RuntimeError("Installationsbenutzer existiert nicht") from exc
    if account.pw_uid == 0 or account.pw_name == "www-data":
        raise RuntimeError("Root und www-data sind keine gültigen venv-Benutzer")

    account_home = os.path.abspath(account.pw_dir)
    configured_home = os.path.abspath(get_home_dir(account.pw_name))
    if (
        not os.path.isabs(account_home)
        or os.path.realpath(account_home) != account_home
        or os.path.realpath(configured_home) != account_home
    ):
        raise RuntimeError("Home-Verzeichnis ist nicht eindeutig an das Benutzerkonto gebunden")
    try:
        home_info = os.lstat(account_home)
    except OSError as exc:
        raise RuntimeError("Gebundenes Home-Verzeichnis ist nicht lesbar") from exc
    if stat.S_ISLNK(home_info.st_mode) or not stat.S_ISDIR(home_info.st_mode):
        raise RuntimeError("Gebundenes Home-Verzeichnis ist kein echtes Verzeichnis")
    if home_info.st_uid != account.pw_uid:
        raise RuntimeError("Gebundenes Home-Verzeichnis gehört nicht dem Installationsbenutzer")
    if stat.S_IMODE(home_info.st_mode) & 0o022:
        raise RuntimeError("Gebundenes Home-Verzeichnis ist für Gruppe oder Andere schreibbar")

    venv_name = _validated_venv_name(
        get_venv_name() if requested_name is None else requested_name
    )
    expected_path = os.path.join(account_home, venv_name)
    candidate = os.path.abspath(str(requested_path or expected_path).strip())
    if candidate != expected_path:
        raise RuntimeError("Venv-Pfad widerspricht der Konto-/Home-Bindung")
    if os.path.dirname(candidate) != account_home:
        raise RuntimeError("Venv muss direkt im gebundenen Home-Verzeichnis liegen")
    return venv_name, candidate, account


def _trusted_executable(path, allowed_uids, label):
    """Prüft ein ausführbares Ziel einschließlich einer zulässigen Symlink-Kette."""
    try:
        link_info = os.lstat(path)
        target_path = os.path.realpath(path)
        target_info = os.stat(target_path)
    except OSError as exc:
        raise RuntimeError(f"{label} ist nicht eindeutig lesbar") from exc
    if stat.S_ISLNK(link_info.st_mode) and link_info.st_uid not in allowed_uids:
        raise RuntimeError(f"{label}-Symlink besitzt einen fremden Eigentümer")
    if not stat.S_ISREG(target_info.st_mode):
        raise RuntimeError(f"{label} verweist nicht auf eine reguläre Datei")
    if target_info.st_uid not in allowed_uids:
        raise RuntimeError(f"{label} besitzt einen fremden Eigentümer")
    if stat.S_IMODE(target_info.st_mode) & 0o022:
        raise RuntimeError(f"{label} ist für Gruppe oder Andere schreibbar")
    if not os.access(path, os.X_OK):
        raise RuntimeError(f"{label} ist nicht ausführbar")
    return path


def require_bound_venv_runtime(
    *,
    install_user=None,
    venv_path=None,
    venv_name=None,
):
    """Liefert ausschließlich ein echtes, konto- und laufzeitgebundenes venv."""
    user = install_user or get_install_user()
    _venv_name, target, account = _bound_venv_target(
        user,
        venv_path,
        requested_name=venv_name,
    )
    try:
        root_info = os.lstat(target)
    except OSError as exc:
        raise RuntimeError("Gebundenes venv fehlt") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise RuntimeError("Gebundenes venv ist kein echtes Verzeichnis")
    if root_info.st_uid != account.pw_uid:
        raise RuntimeError("Gebundenes venv gehört nicht dem Installationsbenutzer")
    if stat.S_IMODE(root_info.st_mode) & 0o022:
        raise RuntimeError("Gebundenes venv ist für Gruppe oder Andere schreibbar")

    for relative, expected_type in (
        ("pyvenv.cfg", "file"),
        ("bin", "directory"),
    ):
        path = os.path.join(target, relative)
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise RuntimeError(f"Venv-Bestandteil fehlt: {relative}") from exc
        is_expected = (
            stat.S_ISREG(info.st_mode)
            if expected_type == "file"
            else stat.S_ISDIR(info.st_mode)
        )
        if stat.S_ISLNK(info.st_mode) or not is_expected:
            raise RuntimeError(f"Venv-Bestandteil ist nicht eindeutig: {relative}")
        if info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeError(f"Venv-Bestandteil ist nicht vertrauenswürdig: {relative}")

    allowed_uids = {0, account.pw_uid}
    python_bin = _trusted_executable(
        os.path.join(target, "bin", "python3"),
        allowed_uids,
        "Venv-Python",
    )
    pip_bin = _trusted_executable(
        os.path.join(target, "bin", "pip"),
        allowed_uids,
        "Venv-Pip",
    )
    if os.path.commonpath((target, os.path.realpath(pip_bin))) != target:
        raise RuntimeError("Venv-Pip verweist aus dem gebundenen venv heraus")

    prefix_probe = (
        "import os,sys; expected=os.path.realpath(sys.argv[1]); "
        "active=os.path.realpath(sys.prefix); "
        "raise SystemExit(0 if active == expected and sys.prefix != sys.base_prefix else 23)"
    )
    probe_result = run_command(
        _command_argv_as_user(
            [python_bin, "-I", "-c", prefix_probe, target],
            account.pw_name,
        ),
        timeout=30,
        use_shell=False,
    )
    if not probe_result.get("success"):
        raise RuntimeError(
            "Python bestätigt die venv-Bindung nicht: "
            + format_command_failure(probe_result)
        )

    pip_probe = run_command(
        _command_argv_as_user(
            [python_bin, "-I", "-m", "pip", "--isolated", "--version"],
            account.pw_name,
        ),
        timeout=30,
        use_shell=False,
    )
    if not pip_probe.get("success"):
        raise RuntimeError(
            "Pip ist nicht an den gebundenen venv-Interpreter gekoppelt: "
            + format_command_failure(pip_probe)
        )
    return {"path": target, "python": python_bin, "pip": pip_bin}


def resolve_venv_target(install_user, *, requested_venv_name=None):
    """Ermittelt ausschließlich das konto- und Home-gebundene Ziel-venv."""
    venv_name, venv_path, _account = _bound_venv_target(
        install_user,
        requested_name=requested_venv_name,
    )
    return venv_name, venv_path


def _persist_bound_venv_binding(
    install_user,
    venv_name,
    venv_path,
    *,
    _venv_binding_lock_held=False,
):
    """Veröffentlicht lokale und Web-Venv-Metadaten gemeinsam oder gar nicht."""
    from .installer_config import (
        CONFIG_FILE,
        LEGACY_WEB_CONFIG_FILE,
        WEB_CONFIG_FILE,
        _ensure_web_metadata_directories,
        ensure_web_config,
        save_config,
    )

    if not _venv_binding_lock_held:
        with exclusive_transaction_lock("e3dc-venv-binding.lock"):
            return _persist_bound_venv_binding(
                install_user,
                venv_name,
                venv_path,
                _venv_binding_lock_held=True,
            )

    bound_name, bound_path = resolve_venv_target(
        install_user,
        requested_venv_name=venv_name,
    )
    if bound_name != venv_name or bound_path != venv_path:
        raise RuntimeError("Zu veröffentlichendes venv widerspricht dem gebundenen Ziel")
    require_bound_venv_runtime(
        install_user=install_user,
        venv_name=venv_name,
        venv_path=venv_path,
    )

    # Der Web-Paarwriter benötigt seine festen Elternverzeichnisse bereits für
    # ein belastbares Missing-Preimage. Die Verzeichnisse enthalten noch keine
    # Venv-Autorität und werden deshalb vor der Dateitransaktion gebunden.
    _ensure_web_metadata_directories(install_user)
    paths = (CONFIG_FILE, WEB_CONFIG_FILE, LEGACY_WEB_CONFIG_FILE)
    previous = {
        path: snapshot_bound_file(path, allow_missing=True, max_bytes=1024 * 1024)
        for path in paths
    }
    postimages = {}

    def _same_projected_state(actual, expected):
        if bool(actual.get("exists")) != bool(expected.get("exists")):
            return False
        actual_parent = tuple(actual.get("parent_identity") or ())
        expected_parent = tuple(expected.get("parent_identity") or ())
        if actual_parent[:2] != expected_parent[:2]:
            return False
        if not expected.get("exists"):
            return True
        return bool(
            actual.get("kind") == expected.get("kind") == "regular"
            and actual.get("sha256") == expected.get("sha256")
            and actual.get("uid") == expected.get("uid")
            and actual.get("gid") == expected.get("gid")
            and actual.get("mode") == expected.get("mode")
            and tuple(actual.get("identity") or ())[5:7]
            == tuple(expected.get("identity") or ())[5:7]
        )

    config = load_config()
    config["install_user"] = install_user
    config["venv_name"] = venv_name
    config["venv_path"] = venv_path
    try:
        save_config(config, _venv_binding_lock_held=True)
        postimages[CONFIG_FILE] = snapshot_bound_file(
            CONFIG_FILE,
            allow_missing=False,
            max_bytes=1024 * 1024,
        )
        persisted = load_config()
        if (
            persisted.get("install_user") != install_user
            or persisted.get("venv_name") != venv_name
            or persisted.get("venv_path") != venv_path
        ):
            raise RuntimeError("Installer-Metadaten bestätigen die Venv-Bindung nicht")

        if ensure_web_config(
            install_user,
            explicit_venv_name=venv_name,
            explicit_venv_path=venv_path,
            require_bound_venv=True,
            _venv_binding_lock_held=True,
        ) is not True:
            raise RuntimeError("Web-Metadaten konnten die Venv-Bindung nicht übernehmen")
        for path in (WEB_CONFIG_FILE, LEGACY_WEB_CONFIG_FILE):
            postimages[path] = snapshot_bound_file(
                path,
                allow_missing=False,
                max_bytes=1024 * 1024,
            )
            try:
                metadata = json.loads(
                    bytes(postimages[path]["payload"]).decode("utf-8-sig")
                )
            except (KeyError, UnicodeDecodeError, ValueError, TypeError) as exc:
                raise RuntimeError(
                    f"Venv-Metadaten-Postimage ist kein gültiges JSON: {path}"
                ) from exc
            if not isinstance(metadata, dict):
                raise RuntimeError(
                    f"Venv-Metadaten-Postimage ist kein JSON-Objekt: {path}"
                )
            if (
                metadata.get("install_user") != install_user
                or metadata.get("venv_name") != venv_name
                or metadata.get("venv_path") != venv_path
            ):
                raise RuntimeError(
                    f"Venv-Metadaten-Readback weicht vom Soll ab: {path}"
                )
        final_local = load_config()
        if (
            final_local.get("install_user") != install_user
            or final_local.get("venv_name") != venv_name
            or final_local.get("venv_path") != venv_path
        ):
            raise RuntimeError("Lokaler Venv-Anker driftete vor dem Gesamtcommit")
        for path in paths:
            final_snapshot = snapshot_bound_file(
                path,
                allow_missing=False,
                max_bytes=1024 * 1024,
            )
            if not snapshots_match(
                final_snapshot,
                postimages[path],
                exact_metadata=True,
            ):
                raise RuntimeError(
                    f"Venv-Metadaten drifteten vor dem Gesamtcommit: {path}"
                )
    except Exception as exc:
        rollback_errors = []
        for path in reversed(paths):
            try:
                current = snapshot_bound_file(
                    path,
                    allow_missing=True,
                    max_bytes=1024 * 1024,
                )
                if (
                    snapshots_match(current, previous[path], exact_metadata=True)
                    or _same_projected_state(current, previous[path])
                ):
                    continue
                expected = postimages.get(path)
                if expected is None or not (
                    snapshots_match(current, expected, exact_metadata=True)
                    or _same_projected_state(current, expected)
                ):
                    raise RuntimeError("Rollbackziel driftete fremd")
                restored = restore_bound_file(
                    previous[path],
                    expected_current=current,
                    staging_root=("/var/www" if path != CONFIG_FILE else None),
                    max_bytes=1024 * 1024,
                )
                if not _same_projected_state(restored, previous[path]):
                    raise RuntimeError("Rollback-Readback weicht vom Preimage ab")
            except Exception as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                f"Venv-Metadatenprojektion fehlgeschlagen ({exc}); "
                "Rollback unvollständig: " + "; ".join(rollback_errors)
            ) from exc
        raise


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
    "rsync",                # Webportal-Installation und HA-Datensynchronisation
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

def setup_venv(show_header=False, requested_venv_name=None):
    """Richtet das Python Virtual Environment ein."""
    if show_header:
        print("\n=== Python Virtual Environment einrichten ===\n")

    install_user = get_install_user()
    try:
        venv_name, venv_path = resolve_venv_target(
            install_user,
            requested_venv_name=requested_venv_name,
        )
    except Exception as exc:
        print(f"✗ Venv-Ziel ist nicht vertrauensgebunden: {exc}")
        return False

    print(f"→ Ziel: {venv_path}")

    if not os.path.lexists(venv_path):
        print("→ Erstelle venv…")
        try:
            system_python = _trusted_executable(
                "/usr/bin/python3",
                {0},
                "System-Python für die Venv-Erzeugung",
            )
            create_cmd = _command_argv_as_user(
                [
                    system_python,
                    "-I",
                    "-m",
                    "venv",
                    "--system-site-packages",
                    venv_path,
                ],
                install_user,
            )
        except Exception as exc:
            print(f"✗ Venv-Erzeugung ist nicht vertrauenswürdig vorbereitet: {exc}")
            return False
        res = run_command(create_cmd, timeout=60, use_shell=False)
        if res['success']:
            print("✓ venv erstellt.")
            system_logger.info(f"Virtual Environment erstellt: {venv_path}")
        else:
            print(f"✗ Fehler beim Erstellen: {format_command_failure(res)}")
            return False
    else:
        print("✓ venv existiert bereits.")

    try:
        require_bound_venv_runtime(
            install_user=install_user,
            venv_name=venv_name,
            venv_path=venv_path,
        )
    except Exception as exc:
        print(f"✗ Venv-Endzustand ist nicht vertrauenswürdig: {exc}")
        return False

    # Nutze zentrale Installationsfunktion
    if install_python_packages(
        install_user=install_user,
        venv_name=venv_name,
        venv_path=venv_path,
    ) is False:
        return False

    try:
        _persist_bound_venv_binding(
            install_user,
            venv_name,
            venv_path,
        )
    except Exception as exc:
        print(f"✗ Venv-Bindung konnte nicht sicher veröffentlicht werden: {exc}")
        return False
    print(f"✓ Venv-Vertrag bestätigt: {venv_path}")

    if show_header:
        print("\n✓ Python-Umgebung eingerichtet.\n")
        log_task_completed("Python venv eingerichtet")
    return True


def list_venv_packages():
    """Listet installierte Pakete im venv auf."""
    print("\n=== Python venv Pakete ===\n")

    install_user = get_install_user()
    try:
        _venv_name, venv_path = resolve_venv_target(install_user)
        runtime = require_bound_venv_runtime(
            install_user=install_user,
            venv_path=venv_path,
        )
    except Exception as exc:
        print(f"✗ Kein vertrauensgebundenes venv gefunden: {exc}")
        return

    res = run_command(
        _command_argv_as_user(
            [runtime["python"], "-I", "-m", "pip", "--isolated", "list"],
            install_user,
        ),
        timeout=60,
        use_shell=False,
    )
    if res['success']:
        print(res['stdout'])
    else:
        print(f"✗ Fehler: {format_command_failure(res)}")
    print()


def install_python_packages(
    *,
    install_user=None,
    venv_name=None,
    venv_path=None,
):
    """Installiert Python-Pakete ausschließlich im gebundenen venv."""
    install_user = install_user or get_install_user()
    resolved_name, resolved_path = resolve_venv_target(
        install_user,
        requested_venv_name=venv_name,
    )
    if venv_path is not None and os.path.abspath(str(venv_path)) != resolved_path:
        raise RuntimeError("Expliziter Venv-Pfad widerspricht dem gebundenen Ziel")
    venv_name, venv_path = resolved_name, resolved_path

    print(f"\n→ Installiere Python-Pakete im venv ({venv_name})…")
    system_logger.info(f"Installiere {len(PYTHON_PACKAGES)} Python-Pakete im venv.")

    failed = [
        pkg
        for pkg in PYTHON_PACKAGES
        if pip_install(
            pkg,
            venv_path=venv_path,
            user=install_user,
            require_venv=True,
            venv_name=venv_name,
        ) is False
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
        try:
            _venv_name, venv_path = resolve_venv_target(install_user)
            if os.path.lexists(venv_path):
                runtime = require_bound_venv_runtime(
                    install_user=install_user,
                    venv_path=venv_path,
                )
                uninstall_cmd = _command_argv_as_user(
                    [
                        runtime["python"],
                        "-I",
                        "-m",
                        "pip",
                        "--isolated",
                        "--require-virtualenv",
                        "uninstall",
                        "-y",
                        *legacy_pip,
                    ],
                    install_user,
                )
                run_command(uninstall_cmd, timeout=120, use_shell=False)
        except Exception as exc:
            print(f"⚠ Legacy-Pip-Bereinigung übersprungen: {exc}")
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

    # --- Apache PHP + WebSocket Reverse Proxy automatisch einrichten ---
    if ensure_apache_php_module() is not True:
        return False

    print("\n→ Konfiguriere Apache Reverse Proxy für WebSockets...")
    for module_name in ("proxy", "proxy_wstunnel"):
        module_result = run_command(f"sudo a2enmod {module_name}", timeout=30)
        if not module_result.get("success"):
            print(
                f"✗ Apache-Modul {module_name} konnte nicht aktiviert werden: "
                + format_command_failure(module_result)
            )
            return False

    if ensure_websocket_proxy_configuration() is not True:
        return False

    proxy_configtest = run_command("sudo apache2ctl configtest", timeout=30)
    if not proxy_configtest.get("success"):
        print(
            "✗ Apache-Konfiguration ist nach der WebSocket-Proxybindung ungültig: "
            + format_command_failure(proxy_configtest)
        )
        return False
    system_logger.info("Apache Proxy-Regel für WebSockets auf Port 8765 geprüft.")

    from .apache_security import ensure_apache_runtime_path_protection
    if not ensure_apache_runtime_path_protection(run_command, reload_apache=False):
        raise RuntimeError("Apache-Schutz für Daten-, Log-, Ramdisk- und Temp-Pfade konnte nicht aktiviert werden")

    apache_restart = run_command("sudo systemctl restart apache2")
    if not apache_restart.get("success"):
        raise RuntimeError(
            "Apache-Neustart nach Laufzeitpfadschutz fehlgeschlagen: "
            + format_command_failure(apache_restart)
        )
    # Bei einer Erstinstallation existiert der Produkt-Webbaum zu diesem
    # Zeitpunkt absichtlich noch nicht. HTTP-HEAD liefert für seine späteren
    # Runtimepfade daher 404 und kann die konfigurierte 403-Sperre noch nicht
    # belegen. Der echte Laufzeit-Endtest bleibt im atomaren Webportal-Schritt,
    # nachdem exakt dieser Baum publiziert und Apache neu gestartet wurde.
    # ------------------------------------------------------------------

    # Python Umgebung einrichten
    if use_venv:
        if setup_venv(show_header=False) is False:
            return False
    else:
        if install_python_packages() is False:
            return False

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



def setup_websocket_service(
    start_service=True,
    defer_activation=False,
    bundle_snapshot=None,
):
    """Richtet den E3DC WebSocket Server als Systemd-Dienst ein."""
    print("→ Richte e3dc-websocket Service ein...")
    return _create_service_file(
        "e3dc-websocket",
        "E3DC WebSocket Server für flüssige Dashboard-Animationen",
        "e3dc_websocket.py",
        "python3",
        restart_sec=5,
        start_service=start_service,
        enable_service=True,
        restart_policy="always",
        after_services=("apache2.service",),
        require_venv=True,
        defer_activation=defer_activation,
        syslog_identifier="e3dc-websocket",
        bundle_snapshot=bundle_snapshot,
    )


def _manager_lock_namespace_errors():
    """Prüft den root-kontrollierten Locknamespace ohne Zustände zu reparieren."""

    errors = []
    try:
        import grp

        www_data_gid = int(grp.getgrnam("www-data").gr_gid)
    except (ImportError, KeyError):
        return ["Systemgruppe www-data fehlt"]

    for directory in (MANAGER_LOCK_NAMESPACE_ROOT, MANAGER_LOCK_DIRECTORY):
        try:
            info = os.lstat(directory)
        except OSError as exc:
            errors.append(f"{directory} ist nicht lesbar: {exc}")
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            errors.append(f"{directory} ist kein echtes Verzeichnis")
            continue
        if info.st_uid != 0 or info.st_gid != 0:
            errors.append(f"{directory} gehört nicht root:root")
        if stat.S_IMODE(info.st_mode) != 0o755:
            errors.append(f"{directory} hat nicht Modus 0755")

    for lock_path in (*MANAGER_LOCK_FILES.values(), HEAT_ENDPOINT_LOCK_FILE):
        try:
            info = os.lstat(lock_path)
        except OSError as exc:
            errors.append(f"{lock_path} ist nicht lesbar: {exc}")
            continue
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            errors.append(f"{lock_path} ist keine reguläre Datei")
            continue
        if info.st_nlink != 1:
            errors.append(f"{lock_path} besitzt nicht genau einen Hardlink")
        if info.st_uid != 0 or info.st_gid != www_data_gid:
            errors.append(f"{lock_path} gehört nicht root:www-data")
        if stat.S_IMODE(info.st_mode) != 0o660:
            errors.append(f"{lock_path} hat nicht Modus 0660")
    return errors


def ensure_manager_lock_namespace():
    """Projiziert den rebootfesten tmpfiles-Vertrag und prüft seinen Istzustand."""

    preimage = None
    postimage = None
    try:
        preimage = snapshot_bound_file(
            MANAGER_LOCK_TMPFILES_CONFIG,
            allow_missing=True,
            max_bytes=16 * 1024,
        )
        if preimage.get("exists") and (
            preimage.get("uid") != 0
            or preimage.get("gid") != 0
            or preimage.get("mode") != 0o644
        ):
            raise RuntimeError(
                "Bestehender Locknamespace-Bootvertrag ist nicht root:root 0644"
            )
        postimage = atomic_write_bound_file(
            MANAGER_LOCK_TMPFILES_CONFIG,
            MANAGER_LOCK_TMPFILES_CONTENT.encode("utf-8"),
            uid=0,
            gid=0,
            mode=0o644,
            expected_snapshot=preimage,
            max_existing_bytes=16 * 1024,
        )

        create_result = run_command(
            "sudo /usr/bin/systemd-tmpfiles --create "
            + shlex.quote(MANAGER_LOCK_TMPFILES_CONFIG),
            timeout=30,
        )
        if not create_result.get("success"):
            raise RuntimeError(
                "Manager-Locknamespace konnte nicht erstellt werden: "
                + format_command_failure(create_result)
            )

        errors = _manager_lock_namespace_errors()
        if errors:
            raise RuntimeError("; ".join(errors))
        return True
    except Exception as exc:
        rollback_error = ""
        if preimage is not None and postimage is not None:
            try:
                restore_bound_file(
                    preimage,
                    expected_current=postimage,
                )
            except Exception as rollback_exc:
                rollback_error = f"; Bootvertrag-Rollback fehlgeschlagen: {rollback_exc}"
        service_logger.error("Manager-Locknamespace ist nicht sicher: %s", exc)
        print(f"✗ Manager-Locknamespace ist nicht sicher: {exc}{rollback_error}")
        return False


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
    start_limit_interval_sec=None,
    start_limit_burst=None,
    require_venv=True,
    script_args=(),
    defer_activation=False,
    wants_services=(),
    documentation="",
    syslog_identifier="",
    service_user=None,
    service_group="www-data",
    bundle_snapshot=None,
):
    """Generische Helper Funktion um Python-Daemons als Systemd Service anzulegen."""
    print(f"\n=== {description} Service einrichten ===\n")
    service_logger.info(f"Richte Dienst {service_name} ein für {python_script_rel_path}.")

    install_user = get_install_user()
    runtime_user = str(service_user or install_user).strip()
    runtime_group = str(service_group or "www-data").strip()
    if (
        not runtime_user
        or not runtime_group
        or any(char in runtime_user + runtime_group for char in "\r\n\x00")
    ):
        raise ValueError("Ungültiger systemd-Dienstbenutzer oder -gruppe")
    service_path = f"/etc/systemd/system/{service_name}.service"

    # Der Code kann aus einem versiegelten Release-Snapshot importiert sein.
    # Units dürfen ausschließlich auf den gebundenen Produktbaum zeigen.
    installer_dir = os.path.join(get_install_path(), "Installer")

    script_abs_path = os.path.normpath(os.path.join(installer_dir, python_script_rel_path))
    working_dir = os.path.dirname(script_abs_path)

    if not os.path.isfile(script_abs_path):
        service_logger.error(f"FATAL: Skript {script_abs_path} nicht gefunden!")
        print(f"✗ Skript für Service {service_name} fehlt: {script_abs_path}")
        return False

    # Alle über diesen Core-Helper installierten Dienste erben standardmäßig
    # den harten venv-Vertrag. Nur ein ausdrücklich nicht zum Core gehörender
    # Aufrufer darf den System-Python-Pfad freigeben.
    try:
        _venv_name, bound_venv_path = resolve_venv_target(install_user)
        if require_venv or os.path.lexists(bound_venv_path):
            runtime = require_bound_venv_runtime(
                install_user=install_user,
                venv_path=bound_venv_path,
            )
            script_executor = runtime["python"]
        else:
            script_executor = _trusted_executable(
                "/usr/bin/python3",
                {0},
                "System-Python für optionalen Nicht-Core-Dienst",
            )
    except Exception as exc:
        print(
            f"✗ Service {service_name} besitzt keinen vertrauensgebundenen "
            f"Python-Laufzeitvertrag: {exc}"
        )
        return False

    manager_lock_prestart = ""
    if service_name in MANAGER_LOCK_FILES:
        if ensure_manager_lock_namespace() is not True:
            return False
        manager_lock_prestart = (
            "ExecStartPre=+/usr/bin/systemd-tmpfiles --create "
            f"{MANAGER_LOCK_TMPFILES_CONFIG}\n"
        )

    after_units = ["network.target"]
    after_units.extend(
        str(item).strip()
        for item in (after_services or ())
        if str(item).strip()
    )
    wants_units = [
        str(item).strip()
        for item in (wants_services or ())
        if str(item).strip()
    ]
    documentation_line = (
        f"Documentation={str(documentation).strip()}\n"
        if str(documentation or "").strip()
        else ""
    )
    wants_line = f"Wants={' '.join(wants_units)}\n" if wants_units else ""
    syslog_line = (
        f"SyslogIdentifier={str(syslog_identifier).strip()}\n"
        if str(syslog_identifier or "").strip()
        else ""
    )
    service_tuning = ""
    if nice is not None:
        service_tuning += f"Nice={int(nice)}\n"
    if io_scheduling_class:
        service_tuning += f"IOSchedulingClass={str(io_scheduling_class).strip()}\n"

    start_limit_tuning = ""
    if (start_limit_interval_sec is None) != (start_limit_burst is None):
        raise ValueError("StartLimitIntervalSec und StartLimitBurst nur gemeinsam setzen")
    if start_limit_interval_sec is not None and start_limit_burst is not None:
        interval = int(start_limit_interval_sec)
        burst = int(start_limit_burst)
        if interval <= 0 or burst <= 0:
            raise ValueError("systemd-Startlimit muss positiv sein")
        start_limit_tuning = (
            f"StartLimitIntervalSec={interval}\n"
            f"StartLimitBurst={burst}\n"
        )

    exec_argv = [str(script_executor), str(script_abs_path)]
    for argument in script_args or ():
        value = str(argument)
        if not value or "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("Ungültiges Argument im systemd-ExecStart-Vertrag")
        exec_argv.append(value)
    exec_start = " ".join(shlex.quote(item) for item in exec_argv)

    service_content = f"""[Unit]
Description={description}
{documentation_line}{wants_line}After={' '.join(after_units)}
{start_limit_tuning}

[Service]
Type=simple
User={runtime_user}
Group={runtime_group}
WorkingDirectory={working_dir}
{manager_lock_prestart}ExecStart={exec_start}
Restart={restart_policy}
RestartSec={restart_sec}
{service_tuning}
StandardOutput=journal
StandardError=journal
{syslog_line}

[Install]
WantedBy=multi-user.target
"""
    tmp_path = None
    staged_service_path = f"{service_path}.e3dc-control-{os.getpid()}.tmp"
    staged_service_created = False
    service_unit_mutated = False
    outer_bundle_recorded = False
    try:
        service_snapshot = capture_systemd_service_bundle((service_name,))
    except Exception as exc:
        print(
            f"✗ Bestehender Unit-Zustand für {service_name} ist nicht sicher "
            f"gebunden: {exc}"
        )
        return False
    unit_name = _bundle_unit_name(service_name)
    if bundle_snapshot is not None:
        if unit_name not in bundle_snapshot:
            print(f"✗ Äußerer Bundle-Snapshot enthält {unit_name} nicht.")
            return False
        if service_snapshot[unit_name] != bundle_snapshot[unit_name]:
            print(f"✗ Unit-Vorzustand von {unit_name} driftete seit dem Bundle-Snapshot.")
            return False
    try:
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
            tmp.write(service_content)
            tmp_path = tmp.name

        stage_result = run_command(
            "sudo install -o root -g root -m 0644 -- "
            + shlex.quote(tmp_path)
            + " "
            + shlex.quote(staged_service_path),
            timeout=30,
        )
        if not stage_result.get("success"):
            raise RuntimeError(
                "Service-Unit konnte nicht vorbereitet werden: "
                + format_command_failure(stage_result)
            )
        staged_service_created = True

        move_result = run_command(
            "sudo mv -f -- "
            + shlex.quote(staged_service_path)
            + " "
            + shlex.quote(service_path),
            timeout=30,
        )
        if not move_result.get("success"):
            raise RuntimeError(
                "Service-Unit konnte nicht atomar aktiviert werden: "
                + format_command_failure(move_result)
            )
        staged_service_created = False
        service_unit_mutated = True
        record_systemd_service_bundle_postimage(
            service_snapshot,
            (service_name,),
            expected_bytes={unit_name: service_content.encode("utf-8")},
        )
        if bundle_snapshot is not None:
            record_systemd_service_bundle_postimage(
                bundle_snapshot,
                (service_name,),
                expected_bytes={unit_name: service_content.encode("utf-8")},
            )
            outer_bundle_recorded = True

        if defer_activation:
            print(f"✓ Service '{service_name}' transaktional vorbereitet; Aktivierung steht aus.\n")
        else:
            enabled_units = (service_name,) if enable_service else ()
            start_order = (service_name,) if enable_service and start_service else ()
            if not activate_systemd_service_bundle(
                service_snapshot,
                enabled_units=enabled_units,
                start_order=start_order,
                start_services=bool(enable_service and start_service),
            ):
                raise RuntimeError(
                    f"Service '{service_name}' konnte nicht gebunden aktiviert werden"
                )
            if enable_service and start_service:
                print(f"✓ Service '{service_name}' installiert und gestartet.\n")
            elif enable_service:
                print(
                    f"✓ Service '{service_name}' installiert und aktiviert; "
                    "Start wird gesammelt ausgeführt.\n"
                )
            else:
                print(f"✓ Service '{service_name}' installiert und bleibt ausgeschaltet.\n")

        log_task_completed(f"Service {service_name} eingerichtet")
        return True
    except Exception as e:
        print(f"✗ Fehler beim Erstellen des Services {service_name}: {e}")
        log_error("service_setup", f"Fehler Service {service_name}: {e}", e)
        if service_unit_mutated:
            rollback_ok = rollback_systemd_service_bundle(service_snapshot) is True
            if rollback_ok:
                if outer_bundle_recorded and bundle_snapshot is not None:
                    bundle_snapshot[unit_name].pop("postimage", None)
                    bundle_snapshot[unit_name].pop("post_effective", None)
                print(f"  ↳ Vorzustand von {service_name} vollständig wiederhergestellt.")
            else:
                print(
                    f"  ↳ Rückfall von {service_name} nicht vollständig bestätigt; "
                    "Dienst bleibt fail-closed gestoppt."
                )
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        if staged_service_created:
            run_command(
                "sudo rm -f -- " + shlex.quote(staged_service_path),
                timeout=15,
            )


_SYSTEMD_UNIT_FILE_STATES = {
    "enabled",
    "enabled-runtime",
    "disabled",
    "static",
    "indirect",
    "masked",
    "generated",
    "transient",
    "alias",
    "",
}
_SYSTEMD_ACTIVE_STATES = {
    "active",
    "inactive",
    "failed",
}


def _bundle_unit_name(service_name):
    value = str(service_name or "").strip()
    if value.endswith(".service"):
        value = value[:-8]
    if (
        not value
        or any(not (char.isalnum() or char in "@_.-") for char in value)
        or "/" in value
    ):
        raise RuntimeError("Ungültiger systemd-Dienstname im Bundle")
    return value + ".service"


def _systemd_show_contract(unit):
    result = run_command(
        "systemctl show --no-pager "
        + shlex.quote(unit)
        + " --property=LoadState --property=UnitFileState --property=ActiveState"
        + " --property=FragmentPath --property=DropInPaths --property=User"
        + " --property=ExecStart",
        timeout=15,
    )
    values = {}
    for line in str(result.get("stdout") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    load_state = values.get("LoadState", "").lower()
    unit_file_state = values.get("UnitFileState", "").lower()
    active_state = values.get("ActiveState", "").lower()
    fragment_path = values.get("FragmentPath", "")
    dropin_paths = tuple(
        item for item in values.get("DropInPaths", "").split() if item
    )
    service_user = values.get("User", "")
    exec_start = values.get("ExecStart", "")
    if load_state not in {"loaded", "not-found"}:
        raise RuntimeError(f"LoadState von {unit} ist nicht sicher lesbar: {load_state or 'leer'}")
    if unit_file_state not in _SYSTEMD_UNIT_FILE_STATES:
        raise RuntimeError(
            f"UnitFileState von {unit} ist nicht sicher lesbar: {unit_file_state or 'leer'}"
        )
    if load_state == "loaded" and active_state not in _SYSTEMD_ACTIVE_STATES:
        raise RuntimeError(
            f"ActiveState von {unit} ist nicht sicher lesbar: {active_state or 'leer'}"
        )
    if load_state == "not-found" and active_state not in {"", "inactive"}:
        raise RuntimeError(f"Nicht vorhandene Unit {unit} besitzt Aktivzustand {active_state}")
    if load_state == "loaded":
        if not os.path.isabs(fragment_path):
            raise RuntimeError(f"FragmentPath von {unit} ist nicht absolut gebunden")
        if any(not os.path.isabs(path) for path in dropin_paths):
            raise RuntimeError(f"DropInPaths von {unit} sind nicht absolut gebunden")
        if not service_user or not exec_start:
            raise RuntimeError(f"User/ExecStart von {unit} sind nicht wirksam lesbar")
    elif fragment_path or dropin_paths or service_user or exec_start:
        raise RuntimeError(f"Nicht vorhandene Unit {unit} besitzt wirksame Fragmente")
    return {
        "load_state": load_state,
        "unit_file_state": unit_file_state,
        "active_state": active_state,
        "fragment_path": fragment_path,
        "dropin_paths": dropin_paths,
        "service_user": service_user,
        "exec_start": exec_start,
    }


def _unit_file_effective_contract(payload, *, unit):
    """Extrahiert den bewusst einfachen User-/ExecStart-Vertrag unserer Units."""

    try:
        text = bytes(payload).decode("utf-8")
    except (TypeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Unit {unit} ist nicht als UTF-8 lesbar") from exc
    section = ""
    users = []
    commands = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "service" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user":
            users.append(value)
        elif key == "execstart":
            commands.append(value)
    if len(users) != 1 or not users[0] or len(commands) != 1 or not commands[0]:
        raise RuntimeError(f"Unit {unit} besitzt keinen eindeutigen User-/ExecStart-Vertrag")
    try:
        argv = tuple(shlex.split(commands[0]))
    except ValueError as exc:
        raise RuntimeError(f"ExecStart von {unit} ist nicht eindeutig zerlegbar") from exc
    if not argv or not os.path.isabs(argv[0]):
        raise RuntimeError(f"ExecStart von {unit} besitzt keinen absoluten Interpreter")
    return {
        "service_user": users[0],
        "exec_argv": argv,
    }


def _systemd_show_exec_argv(value, *, unit):
    marker = "argv[]="
    text = str(value or "")
    if marker not in text:
        raise RuntimeError(f"Wirksamer ExecStart von {unit} enthält kein argv[]")
    argv_text = text.split(marker, 1)[1].split(" ;", 1)[0].strip()
    try:
        argv = tuple(shlex.split(argv_text))
    except ValueError as exc:
        raise RuntimeError(f"Wirksamer ExecStart von {unit} ist nicht zerlegbar") from exc
    if not argv:
        raise RuntimeError(f"Wirksamer ExecStart von {unit} ist leer")
    return argv


def _systemd_effective_contract_matches(
    unit,
    state,
    expected,
    expected_dropins,
):
    expected_fragment = os.path.join("/etc/systemd/system", unit)
    expected_paths = set(expected_dropins or {})
    if not bool(
        state.get("load_state") == "loaded"
        and state.get("fragment_path") == expected_fragment
        and set(state.get("dropin_paths") or ()) == expected_paths
        and state.get("service_user") == expected.get("service_user")
        and _systemd_show_exec_argv(state.get("exec_start"), unit=unit)
        == tuple(expected.get("exec_argv") or ())
    ):
        return False
    try:
        for path, preimage in (expected_dropins or {}).items():
            current = _read_bound_unit_preimage(path)
            if not _unit_preimages_match(current, preimage):
                return False
    except Exception:
        return False
    return True


def _read_bound_unit_preimage(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) not in {0o600, 0o640, 0o644}
        or info.st_size > 256 * 1024
    ):
        raise RuntimeError(f"Unsichere bestehende systemd-Unit: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        chunks = []
        remaining = 256 * 1024 + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        named_after = os.lstat(path)
        if (
            len(data) > 256 * 1024
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            or (
                after.st_dev,
                after.st_ino,
                after.st_uid,
                after.st_gid,
                stat.S_IMODE(after.st_mode),
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            != (
                named_after.st_dev,
                named_after.st_ino,
                named_after.st_uid,
                named_after.st_gid,
                stat.S_IMODE(named_after.st_mode),
                named_after.st_nlink,
                named_after.st_size,
                named_after.st_mtime_ns,
                named_after.st_ctime_ns,
            )
        ):
            raise RuntimeError(f"systemd-Unit änderte sich beim Lesen: {path}")
        return {
            "bytes": data,
            "dev": before.st_dev,
            "ino": before.st_ino,
            "uid": before.st_uid,
            "gid": before.st_gid,
            "mode": stat.S_IMODE(before.st_mode),
            "nlink": before.st_nlink,
            "size": before.st_size,
            "mtime_ns": before.st_mtime_ns,
            "ctime_ns": before.st_ctime_ns,
        }
    finally:
        os.close(descriptor)


def capture_systemd_service_bundle(service_names):
    """Bindet Unitbytes, Enablement und Aktivität vor einer Bundle-Mutation."""

    snapshot = {}
    for requested in service_names:
        unit = _bundle_unit_name(requested)
        if unit in snapshot:
            raise RuntimeError(f"Doppelte Unit im Dienstbundle: {unit}")
        path = os.path.join("/etc/systemd/system", unit)
        preimage = _read_bound_unit_preimage(path)
        state = _systemd_show_contract(unit)
        if preimage is None and state["load_state"] == "loaded":
            raise RuntimeError(
                f"{unit} wird außerhalb des gebundenen /etc-Produktpfads geladen"
            )
        pre_effective = None
        dropin_preimages = {}
        allowed_dropin = os.path.join(
            "/etc/systemd/system",
            unit + ".d",
            "20-e3dc-ramdisk-tmpfs.conf",
        )
        named_dropins = set(state.get("dropin_paths") or ())
        foreign_dropins = named_dropins - {allowed_dropin}
        if foreign_dropins:
            raise RuntimeError(
                f"{unit} besitzt fremde systemd-Drop-ins: "
                + ", ".join(sorted(foreign_dropins))
            )
        dropin_preimage = _read_bound_unit_preimage(allowed_dropin)
        if preimage is None and (named_dropins or dropin_preimage is not None):
            raise RuntimeError(
                f"{unit} besitzt ohne gebundene Hauptunit einen Drop-in"
            )
        if preimage is not None:
            pre_effective = _unit_file_effective_contract(
                preimage["bytes"],
                unit=unit,
            )
            if (dropin_preimage is not None) != (allowed_dropin in named_dropins):
                raise RuntimeError(
                    f"On-Disk- und geladener Drop-in-Vertrag von {unit} weichen ab"
                )
            if dropin_preimage is not None:
                dropin_preimages[allowed_dropin] = dropin_preimage
            if state["load_state"] == "loaded" and not _systemd_effective_contract_matches(
                unit,
                state,
                pre_effective,
                dropin_preimages,
            ):
                raise RuntimeError(
                    f"{unit} besitzt einen fremden Fragment-/Drop-in-/ExecStart-Vertrag"
                )
        snapshot[unit] = {
            "path": path,
            "preimage": preimage,
            "pre_effective": pre_effective,
            "pre_dropins": dropin_preimages,
            **state,
        }
    return snapshot


def _unit_preimages_match(left, right):
    if left is None or right is None:
        return left is None and right is None
    bound_fields = (
        "bytes",
        "dev",
        "ino",
        "uid",
        "gid",
        "mode",
        "nlink",
        "size",
        "mtime_ns",
        "ctime_ns",
    )
    return all(left.get(field) == right.get(field) for field in bound_fields)


def record_systemd_service_bundle_postimage(
    snapshot,
    service_names,
    *,
    expected_bytes,
):
    """Versiegelt die von dieser Transaktion projizierten Unit-Inodes."""

    for requested in service_names:
        unit = _bundle_unit_name(requested)
        if unit not in snapshot:
            raise RuntimeError(f"Postimage gehört nicht zum Dienstbundle: {unit}")
        expected = expected_bytes.get(unit) if isinstance(expected_bytes, dict) else None
        if not isinstance(expected, bytes):
            raise RuntimeError(f"Erwartete Unitbytes fehlen für {unit}")
        current = _read_bound_unit_preimage(snapshot[unit]["path"])
        if current is None:
            raise RuntimeError(f"Vorbereitete Unit fehlt vor dem Bundle-Commit: {unit}")
        if current.get("bytes") != expected:
            raise RuntimeError(f"Vorbereitete Unitbytes weichen vom Payload ab: {unit}")
        for path, preimage in snapshot[unit].get("pre_dropins", {}).items():
            rebound = _read_bound_unit_preimage(path)
            if not _unit_preimages_match(rebound, preimage):
                raise RuntimeError(f"systemd-Drop-in driftete vor dem Bundle-Commit: {path}")
        snapshot[unit]["postimage"] = current
        snapshot[unit]["post_effective"] = _unit_file_effective_contract(
            current["bytes"],
            unit=unit,
        )
        snapshot[unit]["post_dropins"] = dict(snapshot[unit].get("pre_dropins", {}))
    return True


def _restore_bound_unit_file(unit_state):
    path = str(unit_state["path"])
    preimage = unit_state.get("preimage")
    current = _read_bound_unit_preimage(path)
    postimage = unit_state.get("postimage")
    if postimage is None:
        # Eine noch nicht von dieser Transaktion projizierte Unit darf nur im
        # bytegleichen Prestate stehen. Ein bereits erfolgreicher innerer
        # Rollback darf dabei einen neuen, aber inhaltlich identischen Inode
        # erzeugt haben; Metadaten prüft der Reader weiterhin strikt.
        if current is None or preimage is None:
            return current is None and preimage is None
        return current.get("bytes") == preimage.get("bytes")
    if not _unit_preimages_match(current, postimage):
        return False
    if preimage is None:
        result = run_command("sudo rm -f -- " + shlex.quote(path), timeout=20)
        return bool(result.get("success")) and _read_bound_unit_preimage(path) is None

    local_tmp = None
    sibling = f"{path}.e3dc-rollback-{os.getpid()}.tmp"
    try:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as handle:
            handle.write(preimage["bytes"])
            local_tmp = handle.name
        stage = run_command(
            "sudo install -o root -g root -m "
            + format(int(preimage["mode"]), "04o")
            + " -- "
            + shlex.quote(local_tmp)
            + " "
            + shlex.quote(sibling),
            timeout=20,
        )
        if not stage.get("success"):
            return False
        moved = run_command(
            "sudo mv -f -- " + shlex.quote(sibling) + " " + shlex.quote(path),
            timeout=20,
        )
        if not moved.get("success"):
            return False
        restored = _read_bound_unit_preimage(path)
        return bool(
            restored is not None
            and restored.get("bytes") == preimage.get("bytes")
        )
    finally:
        if local_tmp and os.path.exists(local_tmp):
            os.unlink(local_tmp)
        run_command("sudo rm -f -- " + shlex.quote(sibling), timeout=10)


def _restore_unit_enablement(unit, prior_state):
    state = str(prior_state or "")
    if state == "masked":
        command = "sudo systemctl mask " + shlex.quote(unit)
    elif state == "enabled-runtime":
        command = "sudo systemctl enable --runtime " + shlex.quote(unit)
    elif state == "enabled":
        command = "sudo systemctl enable " + shlex.quote(unit)
    elif state in {
        "",
        "disabled",
        "static",
        "indirect",
        "generated",
        "transient",
        "alias",
    }:
        command = "sudo systemctl disable " + shlex.quote(unit)
    else:
        return False
    result = run_command(command, timeout=20)
    if result.get("success"):
        return True
    current = _systemd_show_contract(unit)
    if state == "":
        return bool(
            current.get("load_state") == "not-found"
            and current.get("unit_file_state") == ""
        )
    return current.get("unit_file_state") == state


def _fail_closed_service_bundle(units):
    """Hält ein unvollständig restauriertes Bundle auch über Reboots aus."""

    unit_names = tuple(_bundle_unit_name(unit) for unit in units)
    recovery_nonce = os.urandom(16).hex()
    recovery_dropins = {}
    for unit in reversed(unit_names):
        run_command("sudo systemctl stop " + shlex.quote(unit), timeout=30)
        run_command("sudo systemctl disable " + shlex.quote(unit), timeout=30)
    run_command("sudo systemctl daemon-reload", timeout=30)
    # Zusätzlich verhindert ein root-eigenes Condition-Drop-in jeden
    # automatischen Start, auch wenn eine scheinbar deaktivierte/static Unit
    # später über eine Abhängigkeit eingezogen würde. Der Marker unter /run
    # wird vom Produkt bewusst nie erzeugt; erst eine manuelle, gebundene
    # Recovery darf den Drop-in wieder entfernen.
    blocked = True
    for unit in unit_names:
        dropin_dir = os.path.join("/etc/systemd/system", unit + ".d")
        dropin_path = os.path.join(
            dropin_dir,
            f"99-e3dc-recovery-required-{recovery_nonce}.conf",
        )
        approval_path = os.path.join(
            "/run/e3dc-control/service-bundle-recovery-approved",
            recovery_nonce,
            unit,
        )
        payload = (
            "[Unit]\n"
            f"ConditionPathExists={approval_path}\n"
        ).encode("utf-8")
        if os.path.lexists(approval_path) or os.path.lexists(dropin_path):
            blocked = False
            continue
        created_dir = run_command(
            "sudo install -d -o root -g root -m 0755 -- "
            + shlex.quote(dropin_dir),
            timeout=20,
        )
        if not created_dir.get("success"):
            blocked = False
            continue
        try:
            for directory_path in (
                "/etc",
                "/etc/systemd",
                "/etc/systemd/system",
                dropin_dir,
            ):
                directory_info = os.lstat(directory_path)
                if (
                    stat.S_ISLNK(directory_info.st_mode)
                    or not stat.S_ISDIR(directory_info.st_mode)
                    or directory_info.st_uid != 0
                    or directory_info.st_gid != 0
                    or stat.S_IMODE(directory_info.st_mode) & 0o022
                ):
                    raise RuntimeError("Unsicherer systemd-Drop-in-Pfad")
            missing = snapshot_bound_file(
                dropin_path,
                allow_missing=True,
                max_bytes=64 * 1024,
            )
            if missing.get("exists") or os.path.lexists(approval_path):
                raise RuntimeError("Recovery-Nonce ist nicht exklusiv")
        except Exception:
            blocked = False
            continue
        local_tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", delete=False) as handle:
                handle.write(payload)
                local_tmp = handle.name
            installed = run_command(
                "sudo install -o root -g root -m 0644 -- "
                + shlex.quote(local_tmp)
                + " "
                + shlex.quote(dropin_path),
                timeout=20,
            )
            if not installed.get("success"):
                blocked = False
                continue
        finally:
            if local_tmp and os.path.exists(local_tmp):
                os.unlink(local_tmp)
        try:
            rebound = _read_bound_unit_preimage(dropin_path)
        except Exception:
            blocked = False
        else:
            if rebound is None or rebound.get("bytes") != payload:
                blocked = False
            else:
                recovery_dropins[unit] = {
                    "path": dropin_path,
                    "approval_path": approval_path,
                    "postimage": rebound,
                }

    if not run_command("sudo systemctl daemon-reload", timeout=30).get("success"):
        blocked = False
    for unit in reversed(unit_names):
        run_command("sudo systemctl stop " + shlex.quote(unit), timeout=30)
    for unit in unit_names:
        try:
            state = _systemd_show_contract(unit)
            recovery = recovery_dropins[unit]
            rebound = _read_bound_unit_preimage(recovery["path"])
            if (
                state.get("active_state") not in {"", "inactive", "failed"}
                or recovery["path"] not in set(state.get("dropin_paths") or ())
                or not _unit_preimages_match(rebound, recovery["postimage"])
                or os.path.lexists(recovery["approval_path"])
            ):
                blocked = False
        except Exception:
            blocked = False
    return blocked


def rollback_systemd_service_bundle(snapshot):
    """Stellt ein Unitbundle vollständig wieder her oder hält alles gestoppt."""

    ok = True
    units = tuple(snapshot)
    for unit in reversed(units):
        if not run_command("sudo systemctl stop " + shlex.quote(unit), timeout=30).get("success"):
            # Ein noch nicht geladener, neu anzulegender Dienst darf beim Stop
            # fehlen; nach dem Restore wird sein Zustand erneut exakt geprüft.
            current = _systemd_show_contract(unit)
            if current["active_state"] not in {"", "inactive", "failed"}:
                ok = False
    for unit in units:
        if not _restore_bound_unit_file(snapshot[unit]):
            ok = False
    if not run_command("sudo systemctl daemon-reload", timeout=30).get("success"):
        ok = False
    for unit in units:
        prior = snapshot[unit]
        current_before_enablement = _systemd_show_contract(unit)
        if (
            prior.get("unit_file_state") != "masked"
            and current_before_enablement.get("unit_file_state") == "masked"
        ):
            unmask = run_command(
                "sudo systemctl unmask " + shlex.quote(unit),
                timeout=20,
            )
            if not unmask.get("success"):
                ok = False
        if not _restore_unit_enablement(unit, prior.get("unit_file_state")):
            ok = False
    if not ok:
        if not _fail_closed_service_bundle(units):
            raise RuntimeError(
                "Dienstbundle-Rollback und persistenter Bootblock sind unvollständig"
            )
        return False
    for unit in units:
        prior_active = snapshot[unit].get("active_state") == "active"
        action = "start" if prior_active else "stop"
        if prior_active:
            reset = run_command(
                "sudo systemctl reset-failed " + shlex.quote(unit),
                timeout=20,
            )
            if not reset.get("success"):
                if not _fail_closed_service_bundle(units):
                    raise RuntimeError(
                        "Dienstbundle-Reset und persistenter Bootblock sind unvollständig"
                    )
                return False
        result = run_command(
            f"sudo systemctl {action} " + shlex.quote(unit),
            timeout=30,
        )
        if not result.get("success"):
            current = _systemd_show_contract(unit)
            expected = "active" if prior_active else {"", "inactive", "failed"}
            if (
                current["active_state"] != expected
                if isinstance(expected, str)
                else current["active_state"] not in expected
            ):
                ok = False
                break

    if not ok:
        if not _fail_closed_service_bundle(units):
            raise RuntimeError(
                "Dienstbundle-Aktivitätsrollback und Bootblock sind unvollständig"
            )
        return False

    for unit in units:
        prior = snapshot[unit]
        try:
            current_preimage = _read_bound_unit_preimage(prior["path"])
            current_state = _systemd_show_contract(unit)
            prior_bytes = (
                prior["preimage"]["bytes"] if prior.get("preimage") is not None else None
            )
            current_bytes = (
                current_preimage["bytes"] if current_preimage is not None else None
            )
            if current_bytes != prior_bytes:
                ok = False
            if current_state["unit_file_state"] != prior.get("unit_file_state"):
                ok = False
            if prior.get("preimage") is None:
                if current_state.get("load_state") != "not-found":
                    ok = False
            elif not _systemd_effective_contract_matches(
                unit,
                current_state,
                prior.get("pre_effective") or {},
                prior.get("pre_dropins") or {},
            ):
                ok = False
            expected_active = "active" if prior.get("active_state") == "active" else {
                "",
                "inactive",
                "failed",
            }
            if (
                current_state["active_state"] != expected_active
                if isinstance(expected_active, str)
                else current_state["active_state"] not in expected_active
            ):
                ok = False
        except Exception:
            ok = False

    if not ok:
        if not _fail_closed_service_bundle(units):
            raise RuntimeError(
                "Dienstbundle-Endzustand und Bootblock sind unvollständig"
            )
    return ok


def activate_systemd_service_bundle(
    snapshot,
    *,
    enabled_units,
    start_order=(),
    start_services=True,
):
    """Aktiviert vorbereitete Units erst nach vollständiger Projektion."""

    all_units = set(snapshot)
    enabled = {_bundle_unit_name(unit) for unit in enabled_units}
    ordered = tuple(_bundle_unit_name(unit) for unit in start_order)
    if not enabled.issubset(all_units) or any(unit not in all_units for unit in ordered):
        raise RuntimeError("Dienstbundle-Aktivierung referenziert fremde Units")
    if len(set(ordered)) != len(ordered):
        raise RuntimeError("Dienstbundle-Startreihenfolge enthält Duplikate")

    def prepared_bundle_is_current(required_active=()):
        required_active_units = set(required_active)
        try:
            for checked_unit in snapshot:
                state = _systemd_show_contract(checked_unit)
                if state["load_state"] != "loaded":
                    return False
                if not _unit_preimages_match(
                    _read_bound_unit_preimage(snapshot[checked_unit]["path"]),
                    snapshot[checked_unit].get("postimage"),
                ):
                    return False
                if not _systemd_effective_contract_matches(
                    checked_unit,
                    state,
                    snapshot[checked_unit].get("post_effective") or {},
                    snapshot[checked_unit].get("post_dropins") or {},
                ):
                    return False
                if checked_unit in enabled:
                    if state["unit_file_state"] not in {"enabled", "enabled-runtime"}:
                        return False
                    if checked_unit in required_active_units and state["active_state"] != "active":
                        return False
                else:
                    if state["unit_file_state"] != "disabled":
                        return False
                    if state["active_state"] not in {"inactive", "failed"}:
                        return False
        except Exception:
            return False
        return True

    if not run_command("sudo systemctl daemon-reload", timeout=30).get("success"):
        return False
    for unit in snapshot:
        action = "enable" if unit in enabled else "disable"
        result = run_command(
            f"sudo systemctl {action} " + shlex.quote(unit),
            timeout=30,
        )
        if not result.get("success"):
            return False
        if unit not in enabled:
            stopped = run_command(
                "sudo systemctl stop " + shlex.quote(unit),
                timeout=30,
            )
            if not stopped.get("success"):
                state = _systemd_show_contract(unit)
                if state["active_state"] not in {"", "inactive", "failed"}:
                    return False

    # Nach dem einzigen daemon-reload muss das gesamte vorbereitete Bundle
    # bereits wirksam und byte-/metadatengebunden sein, bevor auch nur ein
    # Hardwaredienst neu gestartet werden darf.
    if not prepared_bundle_is_current():
        return False

    if start_services:
        started_units = set()
        for unit in ordered:
            if unit not in enabled:
                continue
            # Unmittelbar vor jedem einzelnen Restart erneut das gesamte
            # Bundle und alle bereits gestarteten Voraussetzungen binden.
            if not prepared_bundle_is_current(started_units):
                return False
            reset = run_command(
                "sudo systemctl reset-failed " + shlex.quote(unit),
                timeout=20,
            )
            if not reset.get("success"):
                return False
            result = run_command(
                "sudo systemctl restart " + shlex.quote(unit),
                timeout=45,
            )
            if not result.get("success"):
                return False
            started_units.add(unit)
            if not prepared_bundle_is_current(started_units):
                return False

    required_active = enabled if start_services else ()
    return prepared_bundle_is_current(required_active)


def install_e3dc_live_service(
    start_service=True,
    defer_activation=False,
    bundle_snapshot=None,
):
    """Richtet den e3dc-live Python RSCP Dienst als Systemd-Service ein.

    Dieser Dienst ist der native Python-Ersatz fuer den C++ Daten-Export.
    Er liest Echtzeit-Daten direkt per RSCP aus dem E3DC und schreibt sie
    alle 3 Sekunden in /var/www/html/ramdisk/live_data_py.json.
    """
    print("\n=== E3DC Live Data Service (Python RSCP) einrichten ===\n")
    service_logger.info("Richte e3dc-live Service ein.")

    return _create_service_file(
        "e3dc-live",
        "E3DC RSCP Live Data Service (Python Native)",
        "e3dc_live.py",
        "python3",
        restart_sec=15,
        start_service=start_service,
        enable_service=True,
        restart_policy="always",
        after_services=("network-online.target",),
        start_limit_interval_sec=300,
        start_limit_burst=3,
        require_venv=True,
        script_args=("--write", "--loops", "0", "--interval", "3"),
        defer_activation=defer_activation,
        wants_services=("network-online.target",),
        documentation="https://github.com/A9xxx/Install-E3DC-Control",
        syslog_identifier="e3dc-live",
        bundle_snapshot=bundle_snapshot,
    )
