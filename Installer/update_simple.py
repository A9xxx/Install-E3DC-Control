#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Schlanker Bare-Metal-Releasewechsel: Backup, Austausch, Rechte, Neustart.

Der vorhandene Produktbaum ist ausschließlich Backup- und Reparaturquelle.
Insbesondere wird sein ``.git`` weder gelesen noch als Voraussetzung benutzt.
Lokale Produktänderungen, fehlende Dateien und falsche Rechte werden nach einem
verifizierten Vollbackup durch den heruntergeladenen Zielbaum ersetzt.
"""

from __future__ import annotations

import argparse
import fcntl
import grp
import hashlib
import ipaddress
import json
import os
import pwd
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


RELEASE_ROOT = Path(__file__).resolve().parent.parent
if str(RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(RELEASE_ROOT))


PRESERVED_WEB_DIRS = frozenset({"data", "logs", "ramdisk", "tmp"})
PRESERVED_WEB_FILES = frozenset(
    {
        "e3dc.config.txt",
        "e3dc.strompreise.txt",
        "e3dc.wallbox.out",
        "e3dc.wallbox.txt",
        "e3dc_paths.json",
        "live_history.txt",
    }
)
PRESERVED_WEB_ENTRIES = PRESERVED_WEB_DIRS | PRESERVED_WEB_FILES | {"history_backups"}
CORE_RESULT_SERVICES = (
    "e3dc-live.service",
    "e3dc-epex-manager.service",
    "e3dc-weather-manager.service",
    "e3dc-storage-simulator.service",
    "e3dc-storage-manager.service",
    "e3dc-websocket.service",
    "e3dc-notifier.service",
)
EXPLICIT_CUTOVER_SERVICES = frozenset(
    {
        "energy_manager.service",
        "luxtronik.service",
        "wp-manager.service",
        "piguard.service",
        "apache2.service",
        "e3dc.service",
    }
)
LEGACY_SERVICE_MIGRATIONS = {
    "wp-manager.service": "energy_manager.service",
}
OPTIONAL_APT_PACKAGES_BY_MODULE = {
    "mqtt": ("mosquitto-clients",),
}
SERVICE_WRAPPER_ACTIONS = ("start", "stop", "restart", "status", "enable", "disable")
ROOT_UPDATE_LOCK = Path("/run/lock/e3dc-control/update.lock")
SERVICE_WRAPPER = Path("/usr/local/sbin/e3dc-service-control")
WEB_UPDATE_LAUNCHER = Path("/usr/local/sbin/e3dc-web-update-launcher")
SUDOERS_FILE = Path("/etc/sudoers.d/020_e3dc_services")
ROLE_ANCHOR_FILE = Path("/etc/e3dc-control/instance_role.json")
RAMDISK_PATH = Path("/var/www/html/ramdisk")
APACHE_SECURITY_ENABLE_LINK = Path(
    "/etc/apache2/conf-enabled/e3dc-control-security.conf"
)
ROLE_SERVICE_BY_MODE = {
    "master": "e3dc-ha.service",
    "slave": "e3dc-ha.service",
    "shadow": "e3dc-shadow-sync.service",
}
STOP_PRIORITY = (
    "e3dc-ha.service",
    "e3dc-shadow-sync.service",
    "piguard.service",
    "e3dc-wallbox-manager.service",
    "energy_manager.service",
    "e3dc-heizstab.service",
    "e3dc-climate-control.service",
    "e3dc-storage-manager.service",
)
CONFIRMED_HARDWARE_WRITER_SCRIPTS = frozenset(
    {
        "storage_manager.py",
        "storage_manager_next.py",
        "storage_manager_legacy.py",
        "wallbox_manager.py",
        "energy_manager.py",
        "heizstab_manager.py",
        "climate_control.py",
    }
)


@dataclass(frozen=True)
class UpdateFailure(RuntimeError):
    code: str
    happened: str
    solution: str
    system_state: str = (
        "Das Update wurde vor dem Dateiaustausch beendet; "
        "Installation und laufende Dienste blieben unverändert."
    )

    def __str__(self) -> str:
        return self.happened


@dataclass(frozen=True)
class ServicePrestate:
    """Gebundener Dienstzustand unmittelbar vor dem Releasewechsel."""

    active: tuple[str, ...]
    enabled: tuple[str, ...]
    present: tuple[str, ...]
    catalog_active: tuple[str, ...]
    catalog_enabled: tuple[str, ...]
    catalog_present: tuple[str, ...]
    cutover_scope: tuple[str, ...]
    unknown_active_e3dc: tuple[str, ...]
    confirmed_unknown_writers: tuple[str, ...] = ()
    apache_security_enabled: bool = False


def _fail(
    code: str,
    happened: str,
    solution: str,
    *,
    system_state: str = "",
) -> None:
    if system_state:
        raise UpdateFailure(
            code=code,
            happened=happened,
            solution=solution,
            system_state=system_state,
        )
    raise UpdateFailure(code=code, happened=happened, solution=solution)


def _run(
    argv: Iterable[str | os.PathLike[str]],
    *,
    check: bool = False,
    timeout: int = 300,
    user: str = "",
) -> subprocess.CompletedProcess[str]:
    command = [os.fspath(item) for item in argv]
    if user and pwd.getpwnam(user).pw_uid != os.geteuid():
        command = ["/usr/bin/sudo", "-n", "-u", user, *command]
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or f"Exit {result.returncode}").strip()
        raise RuntimeError(f"{' '.join(command)}: {detail}")
    return result


def _service_load_state(unit: str) -> str:
    result = _run(
        ["/usr/bin/systemctl", "show", unit, "--property=LoadState", "--value"],
        timeout=20,
    )
    return result.stdout.strip().lower() if result.returncode == 0 else "unknown"


def _service_exists(unit: str) -> bool:
    return _service_load_state(unit) == "loaded"


def _service_present_or_masked(unit: str) -> bool:
    return _service_load_state(unit) in {"loaded", "masked"}


def _service_masked(unit: str) -> bool:
    return _service_load_state(unit) == "masked"


def _service_active(unit: str) -> bool:
    return _run(["/usr/bin/systemctl", "is-active", "--quiet", unit], timeout=20).returncode == 0


def _service_enabled(unit: str) -> bool:
    result = _run(["/usr/bin/systemctl", "is-enabled", unit], timeout=20)
    state = result.stdout.strip().lower()
    return state in {"enabled", "enabled-runtime", "linked", "linked-runtime"}


def _role_service_intended(role: str) -> bool:
    """Bindet Rollenbetrieb; nur ein vorhandener bewusst ausgeschalteter Dienst bleibt aus."""

    unit = ROLE_SERVICE_BY_MODE.get(role)
    if unit is None:
        return False
    load_state = _service_load_state(unit)
    if load_state == "masked":
        return False
    if load_state == "not-found":
        return True
    if load_state == "loaded":
        return _service_active(unit) or _service_enabled(unit)
    _fail(
        "E3DC-UPD-SERVICE-DISCOVERY-002",
        f"Der Zustand des Rollendienstes {unit} ist nicht eindeutig ({load_state or 'leer'}).",
        f"Führe sudo systemctl daemon-reload und danach systemctl show {unit} -p LoadState aus; starte anschließend denselben Updatebefehl erneut.",
    )
    return False


def _normalize_unit(value: str) -> str:
    name = str(value or "").strip()
    return name if name.endswith(".service") else f"{name}.service"


def _is_update_runtime_unit(unit: str) -> bool:
    """Der im Hintergrund laufende Updater darf sich nicht selbst stoppen."""

    name = _normalize_unit(unit).lower()
    return name.startswith("e3dc-") and "update" in name


def _loaded_e3dc_services() -> tuple[str, ...]:
    result = _run(
        [
            "/usr/bin/systemctl",
            "list-units",
            "--type=service",
            "--all",
            "--no-legend",
            "--plain",
            "--no-pager",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "keine Detailausgabe").strip()
        _fail(
            "E3DC-UPD-SERVICE-DISCOVERY-001",
            f"Die aktuell geladenen systemd-Dienste konnten nicht ermittelt werden: {detail}",
            "Führe sudo systemctl daemon-reload aus und starte danach denselben Updatebefehl erneut.",
        )
    units: set[str] = set()
    for raw_line in result.stdout.splitlines():
        fields = raw_line.strip().lstrip("●").strip().split()
        if not fields:
            continue
        unit = _normalize_unit(fields[0])
        if unit.startswith("e3dc") and not _is_update_runtime_unit(unit):
            units.add(unit)
    return tuple(sorted(units))


def _is_confirmed_competing_hardware_writer(unit: str) -> bool:
    """Erkennt nur belegte alte Writer; ein unbekannter Name genügt nie."""

    result = _run(
        [
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=ExecStart",
            "--value",
        ],
        timeout=20,
    )
    if result.returncode != 0:
        return False
    payload = str(result.stdout or "")
    for script_name in CONFIRMED_HARDWARE_WRITER_SCRIPTS:
        if re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(script_name)}(?![A-Za-z0-9_.-])",
            payload,
        ):
            return True
    return False


def _capture_service_prestate() -> ServicePrestate:
    from Installer.service_catalog import allowed_services

    catalog = {_normalize_unit(item) for item in allowed_services()}
    dynamic = set(_loaded_e3dc_services())
    known_candidates = catalog | set(EXPLICIT_CUTOVER_SERVICES)
    present = {
        unit
        for unit in known_candidates
        if _service_present_or_masked(unit) or _service_exists(unit)
    } | dynamic
    inspected = {
        unit
        for unit in present | dynamic
        if not _is_update_runtime_unit(unit)
    }
    active = {unit for unit in inspected if _service_active(unit)}
    enabled_candidates = catalog | set(EXPLICIT_CUTOVER_SERVICES) | active
    enabled = {unit for unit in enabled_candidates if _service_enabled(unit)}
    unknown_active = {
        unit
        for unit in active
        if unit.startswith("e3dc")
        and unit not in catalog
        and unit != "e3dc.service"
    }
    confirmed_unknown_writers = {
        unit for unit in unknown_active if _is_confirmed_competing_hardware_writer(unit)
    }
    scope = {
        unit
        for unit in present
        if unit in known_candidates or unit == "e3dc.service"
    } | confirmed_unknown_writers
    return ServicePrestate(
        active=tuple(sorted(active)),
        enabled=tuple(sorted(enabled)),
        present=tuple(sorted(present)),
        catalog_active=tuple(sorted(active & catalog)),
        catalog_enabled=tuple(sorted(enabled & catalog)),
        catalog_present=tuple(sorted(present & catalog)),
        cutover_scope=tuple(sorted(scope)),
        unknown_active_e3dc=tuple(sorted(unknown_active)),
        confirmed_unknown_writers=tuple(sorted(confirmed_unknown_writers)),
        apache_security_enabled=os.path.lexists(APACHE_SECURITY_ENABLE_LINK),
    )


def _capture_active_services() -> tuple[str, ...]:
    """Kompatibilitätshelfer für lokale Alt-Regressionen."""

    return _capture_service_prestate().active


def _acquire_update_lock() -> int:
    """Verhindert genau einen konkurrierenden alten oder neuen Updatelauf."""

    ROOT_UPDATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        ROOT_UPDATE_LOCK,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        _fail(
            "E3DC-UPD-BUSY-001",
            "Ein anderer E3DC-Control Update- oder Installationslauf ist noch aktiv.",
            "Warte bis dieser Lauf beendet ist und starte danach denselben Updatebefehl erneut.",
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _load_policy(release_root: Path) -> dict:
    try:
        policy = json.loads((release_root / "UPDATE_POLICY.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(
            "E3DC-UPD-TARGET-001",
            f"Die Updatebeschreibung des veröffentlichten Releases ist nicht lesbar: {exc}",
            "Lade den Ein-Datei-Updater erneut herunter und starte ihn nochmals.",
        )
    if policy.get("update_strategy") != "backup_replace_restart_v1":
        _fail(
            "E3DC-UPD-TARGET-002",
            "Der Ziel-Release besitzt nicht den freigegebenen einfachen Updatevertrag.",
            "Verwende ausschließlich den neuesten veröffentlichten e3dc-update-bootstrap.",
        )
    return policy


def _load_existing_config(target_root: Path) -> dict:
    """Liest nur vorhandene Nutzereinstellungen; ungültige Altdateien blockieren nicht."""

    merged: dict = {}
    paths = (
        target_root / "Installer/installer_config.json",
        Path("/var/www/html/data/e3dc_v4.json"),
    )
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"[WARNUNG] Bestehende Konfiguration ist nicht lesbar ({path}): {exc}")
            continue
        if not isinstance(value, dict):
            print(f"[WARNUNG] Bestehende Konfiguration ist kein Objekt: {path}")
            continue
        nested = value.get("config")
        if isinstance(nested, dict):
            merged.update(nested)
        merged.update({key: item for key, item in value.items() if key != "config"})
    return merged


def _normalized_peer_ip(value: object) -> str:
    try:
        raw = str(value or "").strip()
        return str(ipaddress.ip_address(raw)) if raw else ""
    except ValueError:
        return ""


def _read_valid_role_anchor() -> dict:
    """Liest einen gültigen root-eigenen Rollenanker, sonst reparierbar leer."""

    try:
        metadata = ROLE_ANCHOR_FILE.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size > 64 * 1024
        ):
            return {}
        value = json.loads(ROLE_ANCHOR_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != 1:
        return {}
    mode = str(value.get("mode") or "").strip().lower()
    if mode not in {"off", "master", "slave", "shadow"}:
        return {}
    return {
        "mode": mode,
        "peer_ip": _normalized_peer_ip(value.get("peer_ip")),
    }


def _bind_role_context(role: str, config: dict, *, require_peer: bool = False) -> dict:
    """Bindet die bereits erkannte Rolle vor jeder Updatewirkung an ihre Daten."""

    result = dict(config)
    anchor = _read_valid_role_anchor()
    if anchor and anchor.get("mode") != role:
        _fail(
            "E3DC-UPD-CONFIG-004",
            "Der gültige root-eigene Rollenanker widerspricht der erkannten Instanzrolle.",
            "Prüfe /etc/e3dc-control/instance_role.json und starte danach denselben Updatebefehl erneut.",
        )
    if anchor and anchor.get("mode") == role and anchor.get("peer_ip"):
        result["ha_peer_ip"] = anchor["peer_ip"]
    result["ha_mode"] = role
    if role in {"master", "slave"}:
        peer_ip = _normalized_peer_ip(result.get("ha_peer_ip"))
        if require_peer and not peer_ip:
            _fail(
                "E3DC-UPD-CONFIG-002",
                "Für die erkannte HA-Rolle fehlt eine gültige numerische Peer-IP.",
                "Ergänze ha_peer_ip in /var/www/html/data/e3dc_v4.json und starte danach denselben Updatebefehl erneut.",
            )
        if peer_ip:
            result["ha_peer_ip"] = peer_ip
    return result


def _validate_inputs(
    target_root: Path,
    release_root: Path,
    install_user: str,
    tag: str,
) -> None:
    if os.geteuid() != 0:
        _fail(
            "E3DC-UPD-PRIV-001",
            "Der Updater läuft nicht mit Root-Rechten.",
            "Starte: sudo /bin/sh ./e3dc-update-bootstrap",
        )
    if target_root in {Path("/"), Path("/home"), Path("/usr"), Path("/var")}:
        _fail(
            "E3DC-UPD-PATH-001",
            f"Der erkannte Installationspfad ist zu weit gefasst: {target_root}",
            "Ergänze beim Start den angezeigten Installationspfad als einziges Argument.",
        )
    for path, label in ((target_root, "Installation"), (release_root, "Ziel-Release")):
        try:
            metadata = path.lstat()
        except Exception as exc:
            _fail(
                "E3DC-UPD-PATH-002",
                f"{label} ist nicht erreichbar: {path} ({exc})",
                "Prüfe den Installationspfad und starte denselben Updatebefehl erneut.",
            )
        if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
            _fail(
                "E3DC-UPD-PATH-003",
                f"{label} ist kein normales Verzeichnis: {path}",
                "Verwende den echten Installationsordner ohne Symlink.",
            )
    try:
        account = pwd.getpwnam(install_user)
    except KeyError:
        _fail(
            "E3DC-UPD-USER-001",
            f"Der erkannte Installationsbenutzer existiert nicht: {install_user}",
            "Prüfe den Benutzer des laufenden e3dc-live.service und starte danach erneut.",
        )
    if account.pw_uid == 0 or install_user in {"root", "www-data"}:
        _fail(
            "E3DC-UPD-USER-002",
            "Der Installationsbenutzer darf weder root noch www-data sein.",
            "Starte den Updater für die laufende E3DC-Control-Instanz erneut.",
        )
    expected_version = tag.removeprefix("v")
    actual_version = (release_root / "VERSION").read_text(encoding="utf-8").strip()
    if actual_version != expected_version:
        _fail(
            "E3DC-UPD-TARGET-004",
            f"Release-Tag und VERSION widersprechen sich ({tag} / {actual_version}).",
            "Lade den veröffentlichten Ein-Datei-Updater erneut herunter.",
        )


def _create_backup(target_root: Path) -> Path:
    print("[1/4] Erstelle verifiziertes Vollbackup …", flush=True)
    try:
        from Installer.backup import REPAIR_UPDATE_BACKUP_PROFILE, backup_current_version

        backup = backup_current_version(
            install_path=str(target_root),
            profile=REPAIR_UPDATE_BACKUP_PROFILE,
        )
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        normalized = detail.lower()
        if "no space left" in normalized or "kein platz" in normalized:
            solution = (
                "Schaffe auf dem in der Ursache genannten Dateisystem freien Speicherplatz "
                "und starte danach denselben Updatebefehl erneut."
            )
        elif "permission" in normalized or "berechtigung" in normalized or "read-only" in normalized:
            solution = (
                "Prüfe den in der Ursache genannten Mount- oder Nutzerdatenpfad auf "
                "Schreib-/Leserechte und starte danach denselben Updatebefehl erneut."
            )
        else:
            solution = (
                "Prüfe den in der Ursache genannten Nutzerdaten- oder Backup-Pfad, "
                "behebe genau diesen Befund und starte danach denselben Updatebefehl erneut."
            )
        _fail(
            "E3DC-UPD-BACKUP-001",
            "Das verifizierte Vollbackup konnte nicht erstellt werden. "
            f"Ursache: {detail}",
            solution,
        )
    if not backup:
        _fail(
            "E3DC-UPD-BACKUP-001",
            "Das verifizierte Vollbackup lieferte keinen Sicherungspfad.",
            "Prüfe die unmittelbar davor ausgegebene Backup-Ursache und starte danach denselben Updatebefehl erneut.",
        )
    return Path(backup)


def _validated_apt_packages(
    policy: dict,
    selected_catalog_units: Iterable[str] = (),
) -> tuple[str, ...]:
    from Installer.service_catalog import get_module

    selected_units = {_normalize_unit(item) for item in selected_catalog_units}
    selected_optional_packages: set[str] = set()
    all_optional_packages = {
        package
        for values in OPTIONAL_APT_PACKAGES_BY_MODULE.values()
        for package in values
    }
    for module_key, module_packages in OPTIONAL_APT_PACKAGES_BY_MODULE.items():
        module = get_module(module_key)
        if module is not None and module.service_unit in selected_units:
            selected_optional_packages.update(module_packages)
    packages: list[str] = []
    for key in ("apt_packages", "managed_venv_apt_packages"):
        raw = policy.get(key) or ()
        if not isinstance(raw, (list, tuple)):
            _fail(
                "E3DC-UPD-TARGET-005",
                f"Die Paketliste {key} im Ziel-Release ist ungültig.",
                "Lade den veröffentlichten Ein-Datei-Updater erneut herunter.",
            )
        for item in raw:
            package = str(item or "").strip()
            if (
                not package
                or len(package) > 128
                or re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", package) is None
            ):
                _fail(
                    "E3DC-UPD-TARGET-005",
                    f"Der Paketname im Ziel-Release ist ungültig: {package!r}",
                    "Lade den veröffentlichten Ein-Datei-Updater erneut herunter.",
                )
            if package not in packages:
                if (
                    package not in all_optional_packages
                    or package in selected_optional_packages
                ):
                    packages.append(package)
    for package in sorted(selected_optional_packages):
        if package not in packages:
            packages.append(package)
    return tuple(packages)


def _apt_package_installed(package: str) -> bool:
    result = _run(
        ["/usr/bin/dpkg-query", "-W", "-f=${Status}", package],
        timeout=20,
    )
    return result.returncode == 0 and result.stdout.strip() == "install ok installed"


def _configured_venv_name(config: dict) -> str:
    name = str(config.get("venv_name") or ".venv_e3dc").strip()
    if (
        not name
        or name in {".", ".."}
        or Path(name).name != name
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) is None
    ):
        return ".venv_e3dc"
    return name


def _configured_venv_python_preexists(install_user: str, config: dict) -> bool:
    try:
        home = Path(pwd.getpwnam(install_user).pw_dir)
    except KeyError:
        return False
    python = home / _configured_venv_name(config) / "bin/python3"
    return python.is_file() and os.access(python, os.X_OK)


def _validated_managed_venv_pip_packages(policy: dict) -> tuple[str, ...]:
    raw = policy.get("managed_venv_pip_packages") or ()
    if not isinstance(raw, (list, tuple)):
        _fail(
            "E3DC-UPD-TARGET-005",
            "Die Paketliste managed_venv_pip_packages im Ziel-Release ist ungültig.",
            "Lade den veröffentlichten Ein-Datei-Updater erneut herunter.",
        )
    packages: list[str] = []
    for item in raw:
        package = str(item or "").strip()
        if (
            not package
            or len(package) > 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", package) is None
        ):
            _fail(
                "E3DC-UPD-TARGET-005",
                f"Der Python-Paketname im Ziel-Release ist ungültig: {package!r}",
                "Lade den veröffentlichten Ein-Datei-Updater erneut herunter.",
            )
        if package not in packages:
            packages.append(package)
    return tuple(packages)


def _repair_managed_venv_pip_packages(
    policy: dict,
    install_user: str,
    python: Path,
    *,
    venv_preexisted: bool,
) -> None:
    packages = _validated_managed_venv_pip_packages(policy)
    if not packages:
        return
    runuser = Path("/usr/sbin/runuser")
    if not runuser.is_file() or not os.access(runuser, os.X_OK):
        _fail(
            "E3DC-UPD-DEP-004",
            "runuser fehlt; die freigegebenen Python-Pakete können nicht als Installationsbenutzer repariert werden.",
            "Installiere util-linux und starte danach denselben Updatebefehl erneut.",
        )
    probe = _run(
        [
            runuser,
            "-u",
            install_user,
            "--",
            python,
            "-c",
            (
                "import importlib.metadata as m, sys; "
                "print('\\n'.join(name for name in sys.argv[1:] "
                "if not any(d.metadata.get('Name', '').lower().replace('_', '-').replace('.', '-') == "
                "name.lower().replace('_', '-').replace('.', '-') for d in m.distributions())))"
            ),
            *packages,
        ],
        timeout=60,
    )
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "unbekannter Metadatenfehler").strip()
        _fail(
            "E3DC-UPD-DEP-004",
            f"Die vorhandenen Python-Pakete im venv konnten nicht gelesen werden: {detail}",
            f"Prüfe die Python-Umgebung mit: sudo -u {install_user} {python} -m pip check",
        )
    missing = tuple(
        package
        for package in probe.stdout.splitlines()
        if package in packages
    )
    if not missing:
        return
    pip_options = ["--no-deps"] if venv_preexisted else ["--prefer-binary"]
    pip_command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--quiet",
        *pip_options,
        "--",
        *missing,
    ]
    installed = _run(
        [runuser, "-u", install_user, "--", *pip_command],
        timeout=600,
    )
    if installed.returncode != 0:
        detail = (installed.stderr or installed.stdout or "unbekannter pip-Fehler").strip()
        package_names = ", ".join(missing)
        repair_command = shlex.join(["sudo", "-u", install_user, *pip_command])
        _fail(
            "E3DC-UPD-DEP-004",
            f"Die Python-Pakete im venv konnten nicht repariert werden ({package_names}): {detail}",
            f"Behebe die angezeigte pip-/Netzwerkursache und prüfe sie mit: {repair_command}",
        )


def _ensure_minimal_venv(install_user: str, config: dict) -> Path:
    account = pwd.getpwnam(install_user)
    home = Path(account.pw_dir)
    if not home.is_absolute() or not home.is_dir():
        _fail(
            "E3DC-UPD-DEP-003",
            f"Das Home-Verzeichnis des Installationsbenutzers fehlt: {home}",
            f"Lege {home} für {install_user} an und starte danach denselben Updatebefehl erneut.",
        )
    venv_name = _configured_venv_name(config)
    venv = home / venv_name
    python = venv / "bin/python3"
    if python.is_file() and os.access(python, os.X_OK):
        config["venv_name"] = venv_name
        config["venv_path"] = str(venv)
        return python
    if os.path.lexists(venv) and (venv.is_symlink() or not venv.is_dir()):
        _fail(
            "E3DC-UPD-DEP-003",
            f"Das Python-Umgebungsziel ist kein normales Verzeichnis: {venv}",
            f"Verschiebe {venv} beiseite und starte danach denselben Updatebefehl erneut.",
        )
    runner = Path("/usr/sbin/runuser")
    if not runner.is_file() or not os.access(runner, os.X_OK):
        _fail(
            "E3DC-UPD-DEP-003",
            "runuser fehlt; die Python-Umgebung kann nicht als Installationsbenutzer erzeugt werden.",
            "Installiere util-linux und starte danach denselben Updatebefehl erneut.",
        )
    created = _run(
        [
            runner,
            "-u",
            install_user,
            "--",
            "/usr/bin/python3",
            "-m",
            "venv",
            "--system-site-packages",
            venv,
        ],
        timeout=180,
    )
    if created.returncode != 0 or not python.is_file() or not os.access(python, os.X_OK):
        _fail(
            "E3DC-UPD-DEP-003",
            f"Die minimale Python-Umgebung konnte für {install_user} nicht erzeugt werden.",
            f"Prüfe die Schreibrechte von {home} und starte danach denselben Updatebefehl erneut.",
        )
    config["venv_name"] = venv_name
    config["venv_path"] = str(venv)
    return python


def _repair_packages(
    policy: dict,
    install_user: str,
    config: dict,
    selected_catalog_units: Iterable[str] = (),
) -> Path:
    packages = _validated_apt_packages(policy, selected_catalog_units)
    missing = [package for package in packages if not _apt_package_installed(package)]
    if missing:
        installed = _run(
            [
                "/usr/bin/apt-get",
                "install",
                "-y",
                "--no-remove",
                "--no-upgrade",
                "--",
                *missing,
            ],
            timeout=600,
        )
        missing_after = [package for package in missing if not _apt_package_installed(package)]
        if installed.returncode != 0 or missing_after:
            detail = (installed.stderr or installed.stdout or "unbekannter Apt-Fehler").strip()
            unresolved = ", ".join(missing_after or missing)
            _fail(
                "E3DC-UPD-DEP-002",
                f"Die fehlenden Systempakete konnten nicht installiert werden ({unresolved}): {detail}",
                "Repariere APT mit sudo apt-get -f install und starte danach denselben Updatebefehl erneut.",
            )
    venv_preexisted = _configured_venv_python_preexists(install_user, config)
    python = _ensure_minimal_venv(install_user, config)
    if not python.is_file() or not os.access(python, os.X_OK):
        _fail(
            "E3DC-UPD-DEP-003",
            "Der vorbereitete Python-Interpreter fehlt oder ist nicht ausführbar.",
            "Prüfe den freien Speicherplatz und starte danach denselben Updatebefehl erneut.",
        )
    _repair_managed_venv_pip_packages(
        policy,
        install_user,
        python,
        venv_preexisted=venv_preexisted,
    )
    return python


def _archive_file_before_removal(path: Path, quarantine: Path) -> None:
    if not os.path.lexists(path):
        return
    quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(quarantine, 0, 0)
    os.chmod(quarantine, 0o700)
    destination = quarantine / path.as_posix().lstrip("/").replace("/", "__")
    metadata = path.lstat()
    if stat.S_ISREG(metadata.st_mode):
        shutil.copy2(path, destination, follow_symlinks=False)
    elif stat.S_ISLNK(metadata.st_mode):
        destination.write_text(f"Symlinkziel: {os.readlink(path)}\n", encoding="utf-8")
    else:
        destination.write_text(
            "Alter Pfadtyp: "
            f"mode={stat.S_IFMT(metadata.st_mode):#o}, "
            f"uid={metadata.st_uid}, gid={metadata.st_gid}\n",
            encoding="utf-8",
        )
    os.chown(destination, 0, 0)
    os.chmod(destination, 0o600)
    if stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _clear_legacy_update_blockers(backup_path: Path) -> Path | None:
    """Archiviert Sperren alter Updateversuche und entfernt ihre Systemwirkung."""

    safety_root = Path("/var/lib/e3dc-update-safety")
    quarantine = backup_path.parent / f".{backup_path.name}.legacy-update-state"
    archived = False
    for name in (
        "recovery-journal.json",
        "recovery-context.json",
        "recovery-surface.json",
        "systemd-recovery-surface.json",
        "transaction.json",
        "quiesced-overlay.json",
        "prepared-packages.json",
        "recovery.block",
    ):
        try:
            path = safety_root / name
            if os.path.lexists(path):
                _archive_file_before_removal(path, quarantine)
                archived = True
        except Exception as exc:
            _fail(
                "E3DC-UPD-WRITE-001",
                f"Der alte Updatezustand konnte nicht archiviert werden ({path}): {exc}",
                f"Verschiebe oder entferne {path} und starte danach denselben Updatebefehl erneut.",
            )

    try:
        Path("/var/www/html/ramdisk/watchdog.update_pause").unlink(missing_ok=True)
    except OSError as exc:
        _fail(
            "E3DC-UPD-WRITE-002",
            f"Eine alte Watchdog-Pause konnte nicht entfernt werden: {exc}",
            "Entferne /var/www/html/ramdisk/watchdog.update_pause und starte danach denselben Updatebefehl erneut.",
        )

    from Installer.service_catalog import allowed_services

    units = {_normalize_unit(item) for item in allowed_services()}
    units.update({"piguard.service", "e3dc.service"})
    for unit in units:
        dropin = Path("/etc/systemd/system") / f"{unit}.d" / "00-e3dc-recovery-bootblock.conf"
        try:
            if os.path.lexists(dropin):
                _archive_file_before_removal(dropin, quarantine)
                archived = True
        except Exception as exc:
            _fail(
                "E3DC-UPD-WRITE-003",
                f"Die alte Dienstsperre für {unit} konnte nicht archiviert werden: {exc}",
                f"Entferne {dropin} und starte danach denselben Updatebefehl erneut.",
            )
    reload_result = _run(["/usr/bin/systemctl", "daemon-reload"], timeout=60)
    if reload_result.returncode != 0:
        _fail(
            "E3DC-UPD-SERVICE-RELOAD-001",
            "systemd konnte die bereinigten Dienstdefinitionen nicht neu laden.",
            "Führe sudo systemctl daemon-reload aus und starte danach denselben Updatebefehl erneut.",
        )
    return quarantine if archived else None


def _disable_competing_controllers(
    role: str,
    extra_units: Iterable[str] = (),
) -> None:
    """Deaktiviert abgelöste Regler erst nach bestätigtem Ersatz."""

    selected_role_unit = ROLE_SERVICE_BY_MODE.get(role)
    units = [
        unit
        for unit in sorted(set(ROLE_SERVICE_BY_MODE.values()))
        if unit != selected_role_unit
    ]
    units.extend(_normalize_unit(unit) for unit in extra_units)
    units.append("e3dc.service")
    for unit in dict.fromkeys(units):
        if unit == selected_role_unit:
            continue
        if not _service_exists(unit):
            continue
        disabled = _run(["/usr/bin/systemctl", "disable", "--now", unit], timeout=60)
        if (
            disabled.returncode != 0
            or _service_enabled(unit)
            or _service_active(unit)
        ):
            if unit == "e3dc.service":
                code = "E3DC-UPD-SERVICE-LEGACY-001"
                happened = "Der alte C++-Regler e3dc.service konnte nach dem erfolgreichen Wechsel nicht deaktiviert werden."
            elif unit in set(ROLE_SERVICE_BY_MODE.values()):
                code = "E3DC-UPD-SERVICE-ROLE-001"
                happened = f"Der nicht gewählte Rollendienst {unit} konnte nach dem erfolgreichen Wechsel nicht deaktiviert werden."
            else:
                code = "E3DC-UPD-SERVICE-STANDBY-001"
                happened = f"Der in der Rolle {role} stillzuhaltende Dienst {unit} konnte nach dem erfolgreichen Wechsel nicht deaktiviert werden."
            _fail(
                code,
                happened,
                f"Führe sudo systemctl disable --now {unit} aus und starte danach denselben Updatebefehl erneut.",
            )
        _run(["/usr/bin/systemctl", "reset-failed", unit], timeout=20)


def _read_service_state(unit: str) -> dict[str, str | int]:
    result = _run(
        [
            "/usr/bin/systemctl",
            "show",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            unit,
        ],
        timeout=20,
    )
    values: dict[str, str] = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"LoadState", "ActiveState", "SubState", "MainPID"}:
                values[key] = value.strip()
    try:
        main_pid = int(values.get("MainPID", "-1"))
    except ValueError:
        main_pid = -1
    return {
        "load": values.get("LoadState", "unknown").lower(),
        "active": values.get("ActiveState", "unknown").lower(),
        "sub": values.get("SubState", "unknown").lower(),
        "pid": main_pid,
    }


def _cutover_service_scope(extra_units: Iterable[str] = ()) -> tuple[str, ...]:
    from Installer.service_catalog import allowed_services

    units = {_normalize_unit(item) for item in allowed_services()}
    units.update(EXPLICIT_CUTOVER_SERVICES)
    units.update(_normalize_unit(item) for item in extra_units)
    return tuple(
        sorted(
            unit
            for unit in units
            if not _is_update_runtime_unit(unit) and _service_exists(unit)
        )
    )


def _stop_for_cutover(extra_units: Iterable[str] = ()) -> tuple[str, ...]:
    print("[2/4] Stoppe E3DC-Control-Dienste einmalig für den kurzen Dateiaustausch …", flush=True)
    scope = _cutover_service_scope(extra_units)
    ordered = [unit for unit in STOP_PRIORITY if unit in scope]
    ordered.extend(
        unit
        for unit in scope
        if unit not in set(STOP_PRIORITY) | {"apache2.service"}
    )
    if "apache2.service" in scope:
        ordered.append("apache2.service")
    stopped: list[str] = []
    for unit in ordered:
        stop = _run(["/usr/bin/systemctl", "stop", unit], timeout=60)
        state = _read_service_state(unit)
        if state["active"] == "failed" and state["pid"] == 0:
            _run(["/usr/bin/systemctl", "reset-failed", unit], timeout=20)
            state = _read_service_state(unit)
        stopped_ok = (
            state["pid"] == 0
            and state["active"] == "inactive"
            and state["sub"] in {"dead", "exited"}
        )
        if not stopped_ok:
            detail = (stop.stderr or stop.stdout or "keine Detailausgabe").strip()
            _fail(
                "E3DC-UPD-SERVICE-STOP-001",
                f"{unit} wurde nicht vollständig gestoppt "
                f"({state['active']}/{state['sub']}, MainPID={state['pid']}): {detail}",
                f"Beende {unit} mit sudo systemctl stop {unit} und starte danach denselben Updatebefehl erneut.",
            )
        stopped.append(unit)
    return tuple(stopped)


def _retire_unknown_active_e3dc(prestate: ServicePrestate) -> list[str]:
    """Deaktiviert belegte Alt-Writer erst nach bestätigtem Ersatz."""

    warnings: list[str] = []
    for unit in prestate.confirmed_unknown_writers:
        disabled = _run(
            ["/usr/bin/systemctl", "disable", "--now", unit],
            timeout=60,
        )
        if (
            disabled.returncode != 0
            or _service_enabled(unit)
            or _service_active(unit)
        ):
            detail = (disabled.stderr or disabled.stdout or "keine Detailausgabe").strip()
            _fail(
                "E3DC-UPD-SERVICE-UNKNOWN-001",
                f"Der abgelöste alte Dienst {unit} konnte nach dem erfolgreichen Wechsel nicht deaktiviert werden: {detail}",
                f"Führe sudo systemctl disable --now {unit} aus und starte danach denselben Updatebefehl erneut.",
            )
        _run(["/usr/bin/systemctl", "reset-failed", unit], timeout=20)
        warnings.append(
            f"Abgelöster alter E3DC-Dienst {unit} wurde nach dem erfolgreichen "
            "Wechsel gestoppt und deaktiviert."
        )
    return warnings


def _confirm_cutover_quiet(extra_units: Iterable[str] = ()) -> None:
    """Bestätigt nach der Altblocker-Bereinigung, dass kein Writer zurückkehrte."""

    running: list[str] = []
    for unit in _cutover_service_scope(extra_units):
        state = _read_service_state(unit)
        if state["pid"] != 0 or state["active"] not in {"inactive", "failed"}:
            running.append(f"{unit}={state['active']}/{state['sub']}/PID{state['pid']}")
    if running:
        _fail(
            "E3DC-UPD-SERVICE-STOP-002",
            "Nach der Bereinigung alter Updateblocker wurde ein Dienst wieder aktiv: "
            + ", ".join(running),
            "Stoppe den genannten Dienst einmal mit systemctl stop und starte danach denselben Updatebefehl erneut.",
        )


def _create_stopped_data_backup(target_root: Path, backup_path: Path) -> tuple[Path, object]:
    """Sichert nach bestätigtem Stopp nur noch die zuletzt veränderbaren Daten."""

    from Installer.backup import create_quiesced_overlay
    from Installer.backup_integrity import SYSTEM_BACKUP_KIND, verify_backup

    manifest = verify_backup(backup_path, expected_kind=SYSTEM_BACKUP_KIND)
    transaction = hashlib.sha256(
        f"{target_root}\0{backup_path}\0{os.getpid()}\0{time.time_ns()}".encode("utf-8")
    ).hexdigest()
    # Der Integritätsvertrag verwendet intern weiterhin den historischen
    # Namensbaustein ``quiesced``. Nutzerseitig heißt das Artefakt ausschließlich
    # „Ruhende Daten-Nachsicherung“; es ist kein Ausführungs-Snapshot.
    overlay = backup_path.parent / f".{backup_path.name}.quiesced-{transaction}"
    created, _manifest, guard = create_quiesced_overlay(
        overlay,
        install_path=target_root,
        transaction_id=transaction,
        parent_backup_dir=backup_path,
        parent_backup_id=str(manifest.get("backup_id") or ""),
    )
    return Path(created), guard


def _copy_release_files(release_root: Path, target_root: Path) -> None:
    """Überschreibt Produktdateien, ohne unbekannte Nutzerdaten zu löschen."""

    rsync = shutil.which("rsync")
    if not rsync:
        _fail(
            "E3DC-UPD-DEP-001",
            "Das für den Dateiaustausch benötigte rsync fehlt.",
            "Installiere es mit: sudo apt-get install -y rsync",
        )
    _run(
        [
            rsync,
            "-a",
            "--checksum",
            "--delay-updates",
            "--exclude=/.git",
            str(release_root) + "/",
            str(target_root) + "/",
        ],
        check=True,
        timeout=180,
    )


def _set_release_tree_ownership(
    release_root: Path,
    target_root: Path,
    install_user: str,
) -> None:
    """Setzt Rechte nur für Dateien des neuen Releases, nicht für fremde Daten."""

    account = pwd.getpwnam(install_user)
    web_gid = grp.getgrnam("www-data").gr_gid
    os.chown(target_root, account.pw_uid, web_gid)
    os.chmod(target_root, 0o755)
    for source_dir, dirnames, filenames in os.walk(release_root, followlinks=False):
        relative_dir = Path(source_dir).relative_to(release_root)
        if relative_dir.parts and relative_dir.parts[0] == ".git":
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name != ".git"]
        target_dir = target_root / relative_dir
        if target_dir.exists() and not target_dir.is_symlink():
            os.chown(target_dir, account.pw_uid, web_gid)
        for name in filenames:
            if not relative_dir.parts and name == ".git":
                continue
            target = target_dir / name
            if target.exists() or target.is_symlink():
                os.lchown(target, account.pw_uid, web_gid)


def _replace_product_tree(
    target_root: Path,
    release_root: Path,
    install_user: str,
) -> None:
    """Projiziert den Zielbaum; vorhandene Git-Metadaten bleiben unbeachtet."""

    _copy_release_files(release_root, target_root)
    _set_release_tree_ownership(release_root, target_root, install_user)


def _delete_approved_stale_paths(target_root: Path, policy: dict) -> None:
    """Entfernt nur die im Ziel-Release ausdrücklich benannten alten Dateien."""

    allowed_roots = (target_root, Path("/var/www/html"))
    errors: list[str] = []
    for raw in policy.get("delete_files") or ():
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            candidate = target_root / candidate
        candidate = Path(os.path.abspath(candidate))
        if not any(
            os.path.commonpath((str(candidate), str(root))) == str(root)
            for root in allowed_roots
        ):
            errors.append(f"außerhalb des Produktbereichs: {candidate}")
            continue
        if candidate in allowed_roots:
            errors.append(f"Produktwurzel darf nicht als Löschziel verwendet werden: {candidate}")
            continue
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        try:
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                if os.path.ismount(candidate):
                    errors.append(f"Löschziel ist ein Mountpunkt: {candidate}")
                    continue
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    if errors:
        _fail(
            "E3DC-UPD-WRITE-004",
            "Bekannte veraltete Dateien konnten nicht vollständig entfernt werden: "
            + "; ".join(errors),
            "Prüfe die Schreibrechte der genannten Pfade und starte danach denselben Updatebefehl erneut.",
        )


def _render_service_unit(
    *,
    description: str,
    install_user: str,
    working_directory: Path,
    argv: tuple[str, ...],
    restart_seconds: int,
    after: tuple[str, ...] = (),
    wants: tuple[str, ...] = (),
    start_limit: bool = False,
    manager_lock: bool = False,
    documentation: str = "",
    syslog_identifier: str = "",
    service_user: str = "",
    service_group: str = "www-data",
    start_condition: tuple[str, ...] = (),
    environment: tuple[str, ...] = (),
) -> bytes:
    """Rendert die wenigen Pflicht-Units ohne Prüfung des Altzustands."""

    after_units = tuple(
        dict.fromkeys(
            (
                "network.target",
                *after,
                *(("e3dc-ha.service",) if start_condition else ()),
            )
        )
    )
    unit_lines = ["[Unit]", f"Description={description}"]
    if documentation:
        unit_lines.append(f"Documentation={documentation}")
    if wants:
        unit_lines.append("Wants=" + " ".join(wants))
    unit_lines.append("After=" + " ".join(after_units))
    if start_limit:
        unit_lines.extend(("StartLimitIntervalSec=300", "StartLimitBurst=3"))

    service_lines = [
        "",
        "[Service]",
        "Type=simple",
        f"User={service_user or install_user}",
        f"Group={service_group}",
        f"WorkingDirectory={working_directory}",
    ]
    if start_condition:
        service_lines.append(
            "ExecCondition=/usr/bin/env "
            + " ".join(shlex.quote(item) for item in start_condition)
        )
    for assignment in environment:
        value = str(assignment)
        if not value or any(character in value for character in "\r\n\x00\""):
            raise ValueError("Ungültige systemd-Umgebungsvariable")
        service_lines.append(f'Environment="{value}"')
    if manager_lock:
        service_lines.append(
            "ExecStartPre=+/usr/bin/systemd-tmpfiles --create "
            "/etc/tmpfiles.d/e3dc-control-locks.conf"
        )
    service_lines.extend(
        (
            "ExecStart=" + " ".join(shlex.quote(item) for item in argv),
            "Restart=always",
            f"RestartSec={restart_seconds}",
            "StandardOutput=journal",
            "StandardError=journal",
        )
    )
    if syslog_identifier:
        service_lines.append(f"SyslogIdentifier={syslog_identifier}")
    service_lines.extend(("", "[Install]", "WantedBy=multi-user.target", ""))
    return "\n".join((*unit_lines, *service_lines)).encode("utf-8")


def _replace_core_dropins(unit: str, payload: bytes | None) -> None:
    """Ersetzt alte verwaltete Drop-ins nach dem Backup durch den Releasevertrag."""

    directory = Path("/etc/systemd/system") / f"{unit}.d"
    if os.path.lexists(directory):
        metadata = directory.lstat()
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            shutil.rmtree(directory)
        else:
            directory.unlink()
    if payload is None:
        return
    directory.mkdir(parents=True, mode=0o755)
    os.chown(directory, 0, 0)
    os.chmod(directory, 0o755)
    _atomic_write_file(
        directory / "20-e3dc-ramdisk-tmpfs.conf",
        payload,
        uid=0,
        gid=0,
        mode=0o644,
    )


def _probe_ramdisk_tmpfs() -> bool:
    findmnt = Path("/usr/bin/findmnt")
    if not findmnt.is_file() or not os.access(findmnt, os.X_OK):
        return False
    try:
        result = _run(
            [
                findmnt,
                "--kernel",
                "--first-only",
                "--mountpoint",
                RAMDISK_PATH,
                "--types",
                "tmpfs",
                "--noheadings",
                "--output",
                "TARGET",
            ],
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == str(RAMDISK_PATH)


def _prepare_ramdisk_dropin(install_user: str) -> tuple[bytes | None, list[str]]:
    warnings: list[str] = []
    if not _probe_ramdisk_tmpfs():
        mount_tool = Path("/usr/bin/mount")
        if not mount_tool.is_file() or not os.access(mount_tool, os.X_OK):
            warnings.append(
                "RAM-Disk ist kein bestätigtes tmpfs und /usr/bin/mount fehlt. "
                "Die Startsperren wurden weggelassen; prüfe /etc/fstab."
            )
            return None, warnings
        try:
            mount = _run([mount_tool, RAMDISK_PATH], timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            warnings.append(
                "RAM-Disk ist kein bestätigtes tmpfs. Der einmalige Mount-Versuch "
                f"schlug fehl ({exc}); die Startsperren wurden weggelassen."
            )
            return None, warnings
        if mount.returncode != 0 or not _probe_ramdisk_tmpfs():
            warnings.append(
                "RAM-Disk ist kein bestätigtes tmpfs. Die Startsperren wurden weggelassen; "
                "prüfe /etc/fstab und führe sudo mount /var/www/html/ramdisk aus."
            )
            return None, warnings
    account = pwd.getpwnam(install_user)
    www_gid = grp.getgrnam("www-data").gr_gid
    try:
        os.chown(RAMDISK_PATH, account.pw_uid, www_gid)
        os.chmod(RAMDISK_PATH, 0o2775)
    except OSError as exc:
        warnings.append(
            f"RAM-Disk-Rechte konnten nicht repariert werden ({exc}); Startsperren wurden weggelassen."
        )
        return None, warnings
    from Installer.ramdisk_guard import render_ramdisk_service_dropin

    return render_ramdisk_service_dropin().encode("utf-8"), warnings


def _ensure_core_services(
    target_root: Path,
    install_user: str,
    python: Path,
    dropin_payload: bytes | None,
    role: str = "off",
) -> None:
    """Ersetzt die Pflicht-Units direkt; der Altzustand ist keine Hürde."""

    installer = target_root / "Installer"
    specs = (
        {
            "unit": "e3dc-live.service",
            "description": "E3DC RSCP Live Data Service (Python Native)",
            "script": "e3dc_live.py",
            "args": ("--write", "--loops", "0", "--interval", "3"),
            "restart": 15,
            "after": ("network-online.target",),
            "wants": ("network-online.target",),
            "start_limit": True,
            "documentation": "https://github.com/A9xxx/Install-E3DC-Control",
            "syslog": "e3dc-live",
        },
        {
            "unit": "e3dc-epex-manager.service",
            "description": "E3DC EPEX Manager",
            "script": "epex_manager.py",
            "restart": 60,
        },
        {
            "unit": "e3dc-weather-manager.service",
            "description": "E3DC Wetter & PV Forecast",
            "script": "Forecast/pv_forecast_service.py",
            "restart": 60,
        },
        {
            "unit": "e3dc-storage-simulator.service",
            "description": "E3DC Storage Simulator",
            "script": "storage_simulator.py",
            "restart": 60,
        },
        {
            "unit": "e3dc-storage-manager.service",
            "description": "E3DC Storage Manager",
            "script": "storage_manager.py",
            "restart": 5,
            "after": ("e3dc-live.service",),
            "start_limit": True,
            "manager_lock": True,
        },
        {
            "unit": "e3dc-websocket.service",
            "description": "E3DC WebSocket Server für flüssige Dashboard-Animationen",
            "script": "e3dc_websocket.py",
            "restart": 5,
            "after": ("apache2.service",),
            "syslog": "e3dc-websocket",
        },
        {
            "unit": "e3dc-notifier.service",
            "description": "E3DC Notification Manager",
            "script": "notification_manager.py",
            "restart": 10,
        },
    )

    tmpfiles_payload = (
        "d /run/e3dc-control 0755 root root -\n"
        "d /run/e3dc-control/locks 0755 root root -\n"
        "f /run/e3dc-control/locks/storage_manager.owner.lock 0660 root www-data -\n"
        "f /run/e3dc-control/locks/wallbox_manager.owner.lock 0660 root www-data -\n"
        "f /run/e3dc-control/locks/energy_manager.owner.lock 0660 root www-data -\n"
        "f /run/e3dc-control/locks/heizstab_manager.owner.lock 0660 root www-data -\n"
        "f /run/e3dc-control/locks/heat_actuator_endpoints.lock 0660 root www-data -\n"
    ).encode("utf-8")
    _atomic_write_file(
        Path("/etc/tmpfiles.d/e3dc-control-locks.conf"),
        tmpfiles_payload,
        uid=0,
        gid=0,
        mode=0o644,
    )
    created = _run(
        ["/usr/bin/systemd-tmpfiles", "--create", "/etc/tmpfiles.d/e3dc-control-locks.conf"],
        timeout=30,
    )
    if created.returncode != 0:
        _fail(
            "E3DC-UPD-SERVICE-006",
            "Der Laufzeitordner für die Manager-Sperren konnte nicht angelegt werden.",
            "Führe sudo systemd-tmpfiles --create /etc/tmpfiles.d/e3dc-control-locks.conf aus und starte danach denselben Updatebefehl erneut.",
        )

    start_condition: tuple[str, ...] = ()
    if role in {"master", "slave", "shadow"}:
        start_gate = installer / "ha_systemd_start_gate.py"
        if not start_gate.is_file():
            _fail(
                "E3DC-UPD-SERVICE-004",
                f"Das lokale HA-Starttor des neuen Releases fehlt: {start_gate}",
                "Lade den Ein-Datei-Updater erneut herunter und starte ihn nochmals.",
            )
        start_condition = (str(python), str(start_gate))

    for spec in specs:
        script = installer / str(spec["script"])
        if not script.is_file():
            _fail(
                "E3DC-UPD-SERVICE-004",
                f"Das Pflichtskript des neuen Releases fehlt: {script}",
                "Lade den Ein-Datei-Updater erneut herunter und starte ihn nochmals.",
            )
        argv = (str(python), str(script), *(str(item) for item in spec.get("args", ())))
        payload = _render_service_unit(
            description=str(spec["description"]),
            install_user=install_user,
            working_directory=script.parent,
            argv=argv,
            restart_seconds=int(spec["restart"]),
            after=tuple(spec.get("after", ())),
            wants=tuple(spec.get("wants", ())),
            start_limit=bool(spec.get("start_limit")),
            manager_lock=bool(spec.get("manager_lock")),
            documentation=str(spec.get("documentation") or ""),
            syslog_identifier=str(spec.get("syslog") or ""),
            start_condition=start_condition,
        )
        _atomic_write_file(
            Path("/etc/systemd/system") / str(spec["unit"]),
            payload,
            uid=0,
            gid=0,
            mode=0o644,
        )
        _replace_core_dropins(str(spec["unit"]), dropin_payload)


def _ensure_role_service(
    target_root: Path,
    install_user: str,
    python: Path,
    role: str,
    dropin_payload: bytes | None,
    *,
    release_root: Path | None = None,
) -> str | None:
    unit = ROLE_SERVICE_BY_MODE.get(role)
    if unit is None:
        return None
    if _service_masked(unit):
        # Eine systemd-Maske ist ein ausdrückliches Nutzer-Aus. Sie bleibt
        # bytegenau erhalten und wird vom Reparatur-Updater nicht entmaskiert.
        return unit
    if unit == "e3dc-ha.service":
        from Installer.ha_root_runtime import project_ha_root_runtime

        description = "E3DC-Control High Availability Manager"
        bundle, bound_root, bound_user = project_ha_root_runtime(
            (release_root or RELEASE_ROOT) / "Installer",
            install_root=target_root,
            install_user=install_user,
        )
        script = bundle / "ha_manager.py"
        working_directory = Path("/")
        argv = (
            "/usr/bin/python3",
            "-B",
            "-E",
            "-s",
            str(script),
            "--install-root",
            str(bound_root),
            "--install-user",
            bound_user,
        )
        environment = (
            "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONNOUSERSITE=1",
            "PYTHONPATH=",
        )
        restart_seconds = 10
        service_user = "root"
        service_group = "root"
        syslog_identifier = "e3dc-ha"
    else:
        description = "E3DC-Control Shadow Synchronisation"
        script = target_root / "Installer/shadow_sync.py"
        working_directory = script.parent
        argv = (str(python), str(script))
        environment = ()
        restart_seconds = 5
        service_user = install_user
        service_group = "www-data"
        syslog_identifier = "e3dc-shadow-sync"
    if not script.is_file():
        _fail(
            "E3DC-UPD-SERVICE-004",
            f"Das Rollenskript des neuen Releases fehlt: {script}",
            "Lade den Ein-Datei-Updater erneut herunter und starte ihn nochmals.",
        )
    payload = _render_service_unit(
        description=description,
        install_user=install_user,
        working_directory=working_directory,
        argv=argv,
        restart_seconds=restart_seconds,
        after=("network-online.target",),
        wants=("network-online.target",),
        syslog_identifier=syslog_identifier,
        service_user=service_user,
        service_group=service_group,
        environment=environment,
    )
    _atomic_write_file(
        Path("/etc/systemd/system") / unit,
        payload,
        uid=0,
        gid=0,
        mode=0o644,
    )
    _replace_core_dropins(unit, dropin_payload)
    return unit


def _catalog_target_path(installer: Path, relative: str, *, label: str) -> Path:
    candidate = (installer / str(relative or "")).resolve()
    try:
        candidate.relative_to(installer.resolve())
    except ValueError as exc:
        _fail(
            "E3DC-UPD-TARGET-006",
            f"Der {label} des Ziel-Releases liegt außerhalb von Installer/: {relative}",
            "Lade den veröffentlichten Ein-Datei-Updater erneut herunter.",
        )
    return candidate


def _npm_executable() -> Path | None:
    for candidate in (Path("/usr/bin/npm"), Path("/usr/local/bin/npm")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _prepare_npm_module(
    workdir: Path,
    install_user: str,
    module_name: str,
) -> tuple[Path, list[str]]:
    warnings: list[str] = []
    npm = _npm_executable()
    if npm is None:
        warnings.append(
            f"Optionales Modul {module_name}: npm fehlt; die Unit wurde repariert, "
            "der Dienst kann aber erst nach Installation von Node.js/npm starten."
        )
        return Path("/usr/bin/npm"), warnings
    package_json = workdir / "package.json"
    package_lock = workdir / "package-lock.json"
    if not package_json.is_file() or not package_lock.is_file():
        warnings.append(
            f"Optionales Modul {module_name}: package.json oder package-lock.json fehlt; "
            "npm-Abhängigkeiten wurden nicht verändert."
        )
        return npm, warnings
    runuser = Path("/usr/sbin/runuser")
    if not runuser.is_file() or not os.access(runuser, os.X_OK):
        warnings.append(
            f"Optionales Modul {module_name}: runuser fehlt; npm-Abhängigkeiten "
            "wurden nicht automatisch repariert."
        )
        return npm, warnings
    prepared = _run(
        [
            runuser,
            "-u",
            install_user,
            "--",
            npm,
            "--prefix",
            workdir,
            "ci",
            "--omit=dev",
            "--ignore-scripts",
        ],
        timeout=240,
    )
    if prepared.returncode != 0:
        detail = (prepared.stderr or prepared.stdout or "unbekannter npm-Fehler").strip()
        warnings.append(
            f"Optionales Modul {module_name}: npm-Abhängigkeiten konnten nicht "
            f"repariert werden ({detail})."
        )
    return npm, warnings


def _ensure_selected_catalog_services(
    target_root: Path,
    install_user: str,
    python: Path,
    role: str,
    dropin_payload: bytes | None,
    selected_units: Iterable[str],
) -> list[str]:
    """Projiziert nur optionale Module, die vorher aktiv oder enabled waren."""

    from Installer.service_catalog import LOAD_ACTIVE_CONTROL, get_module_by_service, service_load_profile

    warnings: list[str] = []
    installer = target_root / "Installer"
    excluded = set(CORE_RESULT_SERVICES) | set(ROLE_SERVICE_BY_MODE.values())
    selected = {
        _normalize_unit(unit)
        for unit in selected_units
        if _normalize_unit(unit) not in excluded
    }
    start_condition: tuple[str, ...] = ()
    if selected and role in {"master", "slave", "shadow"}:
        start_gate = installer / "ha_systemd_start_gate.py"
        if not start_gate.is_file():
            _fail(
                "E3DC-UPD-SERVICE-004",
                f"Das lokale HA-Starttor des neuen Releases fehlt: {start_gate}",
                "Lade den veröffentlichten Ein-Datei-Updater erneut herunter.",
            )
        start_condition = (str(python), str(start_gate))
    for unit in sorted(selected):
        module = get_module_by_service(unit)
        if module is None or not module.script:
            continue
        script = _catalog_target_path(installer, module.script, label="Modulskript")
        workdir = _catalog_target_path(
            installer,
            module.working_directory or ".",
            label="Modul-Arbeitsverzeichnis",
        )
        load_profile = service_load_profile(module)
        if not script.is_file() or not workdir.is_dir():
            message = (
                f"Das Ziel-Release enthält das zuvor verwendete Modul {unit} nicht vollständig "
                f"(Script: {script}, Arbeitsverzeichnis: {workdir})."
            )
            if load_profile == LOAD_ACTIVE_CONTROL:
                _fail(
                    "E3DC-UPD-SERVICE-004",
                    message,
                    "Lade den veröffentlichten Ein-Datei-Updater erneut herunter.",
                )
            warnings.append(message)
            continue

        runner = str(module.runner or "python").strip().lower()
        after = ("network-online.target",)
        wants = ("network-online.target",)
        if runner == "python":
            argv = (str(python), "-u", str(script))
        elif runner == "npm":
            npm, npm_warnings = _prepare_npm_module(workdir, install_user, unit)
            warnings.extend(npm_warnings)
            argv = (str(npm), "run", "start")
            after = ("network-online.target", "avahi-daemon.service")
            wants = ("network-online.target", "avahi-daemon.service")
        else:
            message = f"Optionales Modul {unit} verwendet den unbekannten Runner {runner!r}."
            if load_profile == LOAD_ACTIVE_CONTROL:
                _fail(
                    "E3DC-UPD-SERVICE-004",
                    message,
                    "Lade den veröffentlichten Ein-Datei-Updater erneut herunter.",
                )
            warnings.append(message)
            continue

        payload = _render_service_unit(
            description=f"E3DC-Control {module.display_name}",
            install_user=install_user,
            working_directory=workdir,
            argv=argv,
            restart_seconds=10,
            after=after,
            wants=wants,
            manager_lock=module.key in {"heatpump", "heizstab"},
            syslog_identifier=str(module.service or module.key),
            start_condition=start_condition,
        )
        _atomic_write_file(
            Path("/etc/systemd/system") / unit,
            payload,
            uid=0,
            gid=0,
            mode=0o644,
        )
        _replace_core_dropins(unit, dropin_payload)
    return warnings


def _sync_web_tree(
    target_root: Path,
    install_user: str,
    destination: Path = Path("/var/www/html"),
) -> None:
    source = target_root / "html"
    destination.mkdir(parents=True, exist_ok=True)
    rsync = shutil.which("rsync")
    if not rsync:
        raise RuntimeError("rsync fehlt trotz vorbereitender Paketinstallation")
    command = [rsync, "-a", "--checksum", "--delay-updates"]
    for name in sorted(PRESERVED_WEB_ENTRIES):
        command.extend(["--exclude", name])
    command.extend([str(source) + "/", str(destination) + "/"])
    _run(command, check=True, timeout=180)

    account = pwd.getpwnam(install_user)
    www_gid = grp.getgrnam("www-data").gr_gid
    os.chown(destination, 0, www_gid)
    os.chmod(destination, 0o755)
    for source_dir, dirnames, filenames in os.walk(source, followlinks=False):
        relative = Path(source_dir).relative_to(source)
        target_dir = destination / relative
        os.chown(target_dir, 0, www_gid)
        os.chmod(target_dir, 0o755)
        for name in filenames:
            target = target_dir / name
            os.chown(target, 0, www_gid)
            os.chmod(target, 0o644)

    for name in PRESERVED_WEB_DIRS:
        runtime_dir = destination / name
        runtime_dir.mkdir(parents=True, exist_ok=True)
        os.chown(runtime_dir, account.pw_uid, www_gid)
        os.chmod(runtime_dir, 0o2775)

    preserved_modes = {
        "e3dc.config.txt": 0o640,
        "e3dc.strompreise.txt": 0o640,
        "e3dc.wallbox.out": 0o664,
        "e3dc.wallbox.txt": 0o640,
        "live_history.txt": 0o664,
    }
    for name, mode in preserved_modes.items():
        preserved = destination / name
        if preserved.is_file() and not preserved.is_symlink():
            os.chown(preserved, account.pw_uid, www_gid)
            os.chmod(preserved, mode)

    for name in ("VERSION", "CHANGELOG.md", "UPDATE_POLICY.json"):
        target = destination / name
        shutil.copy2(target_root / name, target)
        os.chown(target, 0, www_gid)
        os.chmod(target, 0o644)


def _atomic_write_file(path: Path, payload: bytes, *, uid: int, gid: int, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.update-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != mode
        or path.read_bytes() != payload
    ):
        raise RuntimeError(f"Datei stimmt nach dem Schreiben nicht mit dem Ziel überein: {path}")


def _read_json_dict(path: Path, *, missing_ok: bool) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise
    if not isinstance(value, dict):
        raise ValueError("JSON-Wurzel ist kein Objekt")
    return value


def _project_path_metadata(
    target_root: Path,
    install_user: str,
    role: str,
    config: dict,
    *,
    require_peer: bool = False,
) -> None:
    """Schreibt die ermittelte laufende Instanz als künftige Pfadautorität."""

    from Installer.config_secret_permissions import config_secret_dir_mode, config_secret_file_mode

    account = pwd.getpwnam(install_user)
    www_gid = grp.getgrnam("www-data").gr_gid
    installer_config = target_root / "Installer/installer_config.json"
    local: dict = {}
    try:
        local.update(_read_json_dict(installer_config, missing_ok=True))
    except (OSError, UnicodeError, ValueError):
        pass
    # Die vor dem Backup gebundene V4-/Rollen-Konfiguration ist die aktuelle
    # Autorität. Alte Installer-Metadaten dürfen sie nicht wieder überschreiben.
    local.update(config)
    local.update(
        {
            "install_user": install_user,
            "home_dir": account.pw_dir,
            "install_path": str(target_root),
            "ha_mode": role,
        }
    )
    peer_ip = _normalized_peer_ip(config.get("ha_peer_ip"))
    if role in {"master", "slave"}:
        if require_peer and not peer_ip:
            _fail(
                "E3DC-UPD-CONFIG-002",
                "Für die erkannte HA-Rolle fehlt die vor dem Update gebundene Peer-IP.",
                "Ergänze ha_peer_ip in /var/www/html/data/e3dc_v4.json und starte danach denselben Updatebefehl erneut.",
            )
        if peer_ip:
            local["ha_peer_ip"] = peer_ip
    local_payload = (json.dumps(local, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_file(
        installer_config,
        local_payload,
        uid=account.pw_uid,
        gid=www_gid,
        mode=0o640,
    )

    data_dir = Path("/var/www/html/data")
    data_dir.mkdir(parents=True, exist_ok=True)
    v4_path = data_dir / "e3dc_v4.json"
    try:
        v4 = _read_json_dict(v4_path, missing_ok=True)
    except (OSError, UnicodeError, ValueError) as exc:
        v4 = None
        print(
            f"[WARNUNG] {v4_path} ist nicht als JSON lesbar und bleibt unverändert: {exc}",
            flush=True,
        )
    if v4 is not None:
        v4.update(
            {
                "install_user": install_user,
                "home_dir": account.pw_dir,
                "install_path": str(target_root),
                "ha_mode": role,
            }
        )
        for key in ("venv_name", "venv_path"):
            if local.get(key):
                v4[key] = local[key]
        if role in {"master", "slave"} and peer_ip:
            v4["ha_peer_ip"] = peer_ip
        v4_payload = (json.dumps(v4, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        os.chown(data_dir, account.pw_uid, www_gid)
        os.chmod(data_dir, config_secret_dir_mode(v4))
        _atomic_write_file(
            v4_path,
            v4_payload,
            uid=account.pw_uid,
            gid=www_gid,
            mode=config_secret_file_mode(v4),
        )

    paths = {
        "install_user": install_user,
        "home_dir": account.pw_dir,
        "install_path": str(target_root),
    }
    for key in ("venv_name", "venv_path"):
        if local.get(key):
            paths[key] = local[key]
    _atomic_write_file(
        Path("/var/www/html/e3dc_paths.json"),
        (json.dumps(paths, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        uid=account.pw_uid,
        gid=www_gid,
        mode=0o640,
    )

    role_parent = Path("/etc/e3dc-control")
    if os.path.lexists(role_parent):
        metadata = role_parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            _fail(
                "E3DC-UPD-CONFIG-003",
                f"Der Rollenordner ist kein echtes Verzeichnis: {role_parent}",
                "Verschiebe /etc/e3dc-control beiseite, lege es als Verzeichnis an und starte danach denselben Updatebefehl erneut.",
            )
    else:
        role_parent.mkdir(parents=True, mode=0o755)
    os.chown(role_parent, 0, 0)
    os.chmod(role_parent, 0o755)
    role_payload = (
        json.dumps(
            {
                "schema": 1,
                "node_id": socket.gethostname().strip(),
                "mode": role,
                "peer_ip": peer_ip if role in {"master", "slave"} else "",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    _atomic_write_file(
        role_parent / "instance_role.json",
        role_payload,
        uid=0,
        gid=www_gid,
        mode=0o640,
    )


def _release_script(path: Path, *, label: str) -> bytes:
    payload = path.read_bytes()
    if (
        not payload.startswith(b"#!/bin/bash\n")
        or b"\r" in payload
        or len(payload) > 64 * 1024
    ):
        raise RuntimeError(f"{label}-Vorlage im Ziel-Release ist ungültig")
    return payload


def _canonical_shell_literal(value: str) -> bytes:
    """Kodiert Dispatcherwerte immer in der von der Discovery gelesenen Form."""

    return ("'" + value.replace("'", "'\"'\"'") + "'").encode("utf-8")


def _install_privileged_entrypoints(
    release_root: Path,
    target_root: Path,
    install_user: str,
) -> list[str]:
    """Installiert Launcher und sudoers direkt aus dem Ziel-Release, nie aus Nutzer-Git."""

    warnings: list[str] = []
    entrypoint_parents = tuple(
        sorted(
            {
                SERVICE_WRAPPER.parent.parent,
                SERVICE_WRAPPER.parent,
                WEB_UPDATE_LAUNCHER.parent.parent,
                WEB_UPDATE_LAUNCHER.parent,
            },
            key=lambda path: len(path.parts),
        )
    )
    for parent in entrypoint_parents:
        if os.path.lexists(parent):
            metadata = parent.lstat()
            if parent.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(f"Launcher-Elternpfad ist kein echtes Verzeichnis: {parent}")
        else:
            parent.mkdir(mode=0o755)
        os.chown(parent, 0, 0)
        os.chmod(parent, 0o755)

    service_payload = _release_script(
        release_root / "Installer/service_wrapper.sh",
        label="Service-Launcher",
    )
    template = _release_script(
        release_root / "Installer/web_update_launcher.sh",
        label="Web-Update-Launcher",
    )
    root_marker = b"@E3DC_INSTALL_ROOT@"
    user_marker = b"@E3DC_INSTALL_USER@"
    if template.count(root_marker) != 1 or template.count(user_marker) != 1:
        raise RuntimeError("Web-Update-Launcher besitzt keine eindeutigen Platzhalter")
    launcher_payload = template.replace(
        root_marker,
        _canonical_shell_literal(str(target_root)),
    ).replace(
        user_marker,
        _canonical_shell_literal(install_user),
    )

    for path, payload, label in (
        (SERVICE_WRAPPER, service_payload, "Service-Launcher"),
        (WEB_UPDATE_LAUNCHER, launcher_payload, "Web-Update-Launcher"),
    ):
        _atomic_write_file(path, payload, uid=0, gid=0, mode=0o755)
        syntax = _run(["/bin/bash", "-n", path], timeout=20)
        if syntax.returncode != 0:
            raise RuntimeError(f"{label} besitzt nach dem Schreiben einen Syntaxfehler")
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o755
        ):
            raise RuntimeError(f"{label} blieb nach der automatischen Rechtereparatur unsicher")

    from Installer.service_catalog import allowed_services

    wrapper_services = tuple(
        re.findall(
            rb'^\s+"([A-Za-z0-9_.@-]+\.service)"\s*$',
            service_payload,
            re.MULTILINE,
        )
    )
    wrapper_service_names = tuple(item.decode("ascii") for item in wrapper_services)
    if set(wrapper_service_names) != set(allowed_services()):
        raise RuntimeError("Service-Launcher und Dienstkatalog des Ziel-Releases widersprechen sich")
    sudoers_lines = [
        "# E3DC-Control WebUI and unattended-update wrapper permissions",
        "# Managed by the E3DC-Control updater. Do not add direct systemctl commands here.",
    ]
    sudoers_lines.extend(
        f"www-data ALL=(root) NOPASSWD: {SERVICE_WRAPPER} {action} {unit}"
        for action in SERVICE_WRAPPER_ACTIONS
        for unit in wrapper_service_names
    )
    sudoers_lines.extend(
        (
            f'www-data ALL=(root) NOPASSWD: {WEB_UPDATE_LAUNCHER} ""',
            f'{install_user} ALL=(root) NOPASSWD: {WEB_UPDATE_LAUNCHER} ""',
            "",
        )
    )
    sudoers_payload = "\n".join(sudoers_lines).encode("utf-8")
    visudo = Path("/usr/sbin/visudo")
    if not visudo.is_file() or not os.access(visudo, os.X_OK):
        raise RuntimeError("visudo fehlt; sudoers kann nicht sicher aktualisiert werden")
    validation_fd, validation_name = tempfile.mkstemp(prefix="e3dc-sudoers-", dir="/run")
    validation = Path(validation_name)
    try:
        offset = 0
        while offset < len(sudoers_payload):
            offset += os.write(validation_fd, sudoers_payload[offset:])
        os.fchmod(validation_fd, 0o440)
        os.close(validation_fd)
        validation_fd = -1
        syntax = _run([visudo, "-cf", validation], timeout=20)
        if syntax.returncode != 0:
            raise RuntimeError("Der neue sudoers-Eintrag besitzt einen Syntaxfehler")
    finally:
        if validation_fd >= 0:
            os.close(validation_fd)
        validation.unlink(missing_ok=True)
    _atomic_write_file(SUDOERS_FILE, sudoers_payload, uid=0, gid=0, mode=0o440)
    syntax = _run([visudo, "-cf", "/etc/sudoers"], timeout=20)
    if syntax.returncode != 0:
        warnings.append(
            "Die neue E3DC-sudoers-Datei ist gültig, aber eine fremde bestehende "
            "sudoers-Datei enthält einen Fehler. Prüfe sudo visudo -c."
        )
    return warnings


def _repair_units_and_permissions(
    target_root: Path,
    release_root: Path,
    install_user: str,
    role: str,
    config: dict,
    policy: dict,
    venv_python: Path,
    service_prestate: ServicePrestate | None = None,
    selected_catalog_units: Iterable[str] = (),
    *,
    require_role_peer: bool = False,
) -> list[str]:
    warnings: list[str] = []
    _delete_approved_stale_paths(target_root, policy)
    dropin_payload, ramdisk_warnings = _prepare_ramdisk_dropin(install_user)
    warnings.extend(ramdisk_warnings)
    _ensure_core_services(
        target_root,
        install_user,
        venv_python,
        dropin_payload,
        role,
    )
    _ensure_role_service(
        target_root,
        install_user,
        venv_python,
        role,
        dropin_payload,
        release_root=release_root,
    )
    if service_prestate is not None:
        warnings.extend(
            _ensure_selected_catalog_services(
                target_root,
                install_user,
                venv_python,
                role,
                dropin_payload,
                selected_catalog_units
                or (
                    *service_prestate.catalog_active,
                    *service_prestate.catalog_enabled,
                ),
            )
        )
    _project_path_metadata(
        target_root,
        install_user,
        role,
        config,
        require_peer=require_role_peer,
    )
    warnings.extend(_install_privileged_entrypoints(release_root, target_root, install_user))

    apache_source = target_root / "Installer/apache/e3dc-control-security.conf"
    apache_target = Path("/etc/apache2/conf-available/e3dc-control-security.conf")
    if apache_source.is_file() and apache_target.parent.is_dir():
        installed = _run(
            [
                "/usr/bin/install",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0644",
                str(apache_source),
                str(apache_target),
            ],
            timeout=30,
        )
        enabled = _run(["/usr/sbin/a2enconf", "e3dc-control-security"], timeout=30)
        syntax = _run(["/usr/sbin/apache2ctl", "configtest"], timeout=30)
        if installed.returncode != 0 or enabled.returncode != 0 or syntax.returncode != 0:
            _fail(
                "E3DC-UPD-SERVICE-005",
                "Die Apache-Konfiguration konnte nicht vollständig aus dem neuen Release aktiviert werden.",
                "Führe sudo apache2ctl configtest aus, behebe die ausgegebene Zeile und starte danach denselben Updatebefehl erneut.",
            )
    return warnings


def _services_to_start(
    policy: dict,
    active_before: tuple[str, ...],
    role: str,
    enabled_before: tuple[str, ...] = (),
    *,
    role_service_intended: bool | None = None,
) -> tuple[str, ...]:
    from Installer.service_catalog import allowed_services

    catalog = {_normalize_unit(item) for item in allowed_services()}
    role_service = ROLE_SERVICE_BY_MODE.get(role)
    role_units = set(ROLE_SERVICE_BY_MODE.values())
    role_intended = (
        bool(
            role_service
            and role_service in (set(active_before) | set(enabled_before))
        )
        if role_service_intended is None
        else role_service_intended
    )
    if role == "off":
        services = [
            _normalize_unit(item)
            for item in policy.get("restart_services") or ()
        ]
        services.extend(
            unit
            for unit in active_before
            if unit not in {"apache2.service", "e3dc.service"}
            and unit not in role_units
            and (
                unit in catalog
                or unit in {"piguard.service", "luxtronik.service"}
            )
        )
        services.extend(
            target
            for source, target in LEGACY_SERVICE_MIGRATIONS.items()
            if source in set(active_before)
        )
    else:
        # Bei HA und Shadow startet ausschließlich der Rollenmanager die von
        # ihm verwalteten Dienste. Ein bewusst deaktivierter Rollenmanager
        # bleibt aus.
        services = [role_service] if role_intended and role_service else []
        if role in {"master", "slave"} and "piguard.service" in active_before:
            services.append("piguard.service")
    services.append("apache2.service")
    seen: set[str] = set()
    return tuple(unit for unit in services if not (unit in seen or seen.add(unit)))


def _services_to_enable(
    prestate: ServicePrestate,
    services: Iterable[str],
    role: str,
    *,
    role_service_intended: bool | None = None,
) -> tuple[str, ...]:
    service_set = {_normalize_unit(unit) for unit in services}
    enabled_before = set(prestate.enabled)
    enabled = enabled_before & service_set
    role_service = ROLE_SERVICE_BY_MODE.get(role)
    role_intended = (
        bool(
            role_service
            and role_service in (set(prestate.active) | enabled_before)
        )
        if role_service_intended is None
        else role_service_intended
    )
    enabled.add("apache2.service")
    for source, target in LEGACY_SERVICE_MIGRATIONS.items():
        if source in enabled_before or (
            role in {"master", "slave"} and source in set(prestate.active)
        ):
            enabled.add(target)
    role_service_missing = bool(
        role_service is not None and role_service not in set(prestate.present)
    )
    if (
        role_intended
        and role_service is not None
        and role_service_missing
    ):
        # Eine aus dem Release neu projizierte Rollenunit muss auch nach dem
        # nächsten Reboot weiterlaufen. Eine bereits vorhandene, bewusst
        # deaktivierte Unit bleibt dagegen unverändert aus.
        enabled.add(role_service)
    if role == "off":
        enabled.update(CORE_RESULT_SERVICES)
    elif role in {"master", "slave"} and role_intended:
        # Der HA-Manager startet die Writer selbst. Sie werden deshalb nur
        # rebootfest vorbereitet und niemals mit `enable --now` gestartet.
        enabled.update(enabled_before & set(CORE_RESULT_SERVICES))
        if role_service in enabled_before or role_service_missing:
            enabled.update(CORE_RESULT_SERVICES)
    return tuple(sorted(enabled))


def _required_services(
    role: str,
    selected_catalog_units: Iterable[str] = (),
    *,
    role_service_intended: bool = False,
) -> tuple[str, ...]:
    from Installer.service_catalog import LOAD_ACTIVE_CONTROL, get_module_by_service, service_load_profile

    selected_units = {_normalize_unit(unit) for unit in selected_catalog_units}
    role_service = ROLE_SERVICE_BY_MODE.get(role)
    if role != "off":
        if (
            role in {"master", "slave"}
            and role_service_intended
            and role_service is not None
            and (role == "master" or bool(selected_units))
        ):
            # Der HA-Manager bleibt der einzige Starter. Der Updater wartet
            # jedoch auf genau die Dienste, die auf diesem Master oder zuvor
            # aktiven Slave-Failover vor dem kurzen Cutover aktiv waren. Ein
            # reiner Standby-Slave erhält keine zusätzliche Startforderung.
            return tuple(dict.fromkeys((role_service, *sorted(selected_units))))
        return (
            (role_service,)
            if role_service_intended and role_service is not None
            else ()
        )
    active_controllers = []
    for raw_unit in selected_catalog_units:
        unit = _normalize_unit(raw_unit)
        module = get_module_by_service(unit)
        if module is not None and service_load_profile(module) == LOAD_ACTIVE_CONTROL:
            active_controllers.append(unit)
    return tuple(
        dict.fromkeys(
            (
                *CORE_RESULT_SERVICES,
                *active_controllers,
            )
        )
    )


def _forbidden_services_after_start(
    prestate: ServicePrestate,
    role: str,
    *,
    role_service_intended: bool | None = None,
) -> tuple[str, ...]:
    """Liefert Dienste, die im rollenrichtigen Endzustand aus bleiben müssen."""

    from Installer.service_catalog import allowed_services

    role_service = ROLE_SERVICE_BY_MODE.get(role)
    role_intended = (
        bool(
            role_service
            and role_service in (set(prestate.active) | set(prestate.enabled))
        )
        if role_service_intended is None
        else role_service_intended
    )
    forbidden = {
        *prestate.confirmed_unknown_writers,
        "e3dc.service",
        *LEGACY_SERVICE_MIGRATIONS,
        *(
            unit
            for unit in set(ROLE_SERVICE_BY_MODE.values())
            if unit != role_service
        ),
    }
    if role == "shadow" or (role in {"master", "slave"} and not role_intended):
        forbidden.update(_normalize_unit(item) for item in allowed_services())
        forbidden.update({"piguard.service", "luxtronik.service"})
        if role_intended and role_service is not None:
            forbidden.discard(role_service)
    return tuple(sorted(forbidden))


def _services_to_disable_after_success(
    prestate: ServicePrestate,
    role: str,
    *,
    role_service_intended: bool | None = None,
) -> tuple[str, ...]:
    """Persistiert ausschließlich den bereits bestätigten Rollen-Endzustand."""

    from Installer.service_catalog import allowed_services

    role_service = ROLE_SERVICE_BY_MODE.get(role)
    role_intended = (
        bool(
            role_service
            and role_service in (set(prestate.active) | set(prestate.enabled))
        )
        if role_service_intended is None
        else role_service_intended
    )
    if role != "shadow" and not (
        role in {"master", "slave"} and not role_intended
    ):
        return ()
    units = {
        _normalize_unit(item)
        for item in allowed_services()
        if _normalize_unit(item) != role_service
    }
    units.update({"piguard.service", "luxtronik.service"})
    return tuple(sorted(units))


def _start_services(
    services: tuple[str, ...],
    *,
    required: tuple[str, ...],
    enable_services: tuple[str, ...],
) -> list[str]:
    print("[4/4] Starte Dienste neu …", flush=True)
    reload_result = _run(["/usr/bin/systemctl", "daemon-reload"], timeout=60)
    if reload_result.returncode != 0:
        _fail(
            "E3DC-UPD-SERVICE-RELOAD-002",
            "systemd konnte die neuen Dienstdefinitionen nicht laden.",
            "Führe sudo systemctl daemon-reload aus und starte danach denselben Updatebefehl erneut.",
        )
    warnings: list[str] = []
    required_set = set(required) | {"apache2.service"}
    enable_set = set(enable_services)
    for unit in services:
        if not _service_exists(unit):
            if unit in required_set:
                _fail(
                    "E3DC-UPD-SERVICE-001",
                    f"Der Pflichtdienst {unit} fehlt nach dem Update.",
                    "Starte den Ein-Datei-Updater erneut; das Vollbackup bleibt erhalten.",
                )
            warnings.append(f"Optionaler Dienst fehlt: {unit}")
            continue
        _run(["/usr/bin/systemctl", "reset-failed", unit], timeout=30)
        started = _run(["/usr/bin/systemctl", "start", unit], timeout=90)
        if started.returncode != 0 or not _service_active(unit):
            if unit in required_set:
                _fail(
                    "E3DC-UPD-SERVICE-002",
                    f"Der Pflichtdienst {unit} konnte nicht gestartet werden.",
                    f"Zeige die Ursache mit: sudo journalctl -u {unit} --no-pager -n 120",
                )
            warnings.append(f"Optionaler Dienst startet nicht: {unit}")
    for unit in sorted(enable_set):
        if not _service_exists(unit):
            if unit in required_set or unit in CORE_RESULT_SERVICES:
                _fail(
                    "E3DC-UPD-SERVICE-001",
                    f"Der rebootfest benötigte Dienst {unit} fehlt nach dem Update.",
                    "Starte den Ein-Datei-Updater erneut; das Vollbackup bleibt erhalten.",
                )
            warnings.append(f"Optionaler Dienst fehlt: {unit}")
            continue
        enabled = _run(["/usr/bin/systemctl", "enable", unit], timeout=60)
        if enabled.returncode != 0 or not _service_enabled(unit):
            if unit in required_set or unit in CORE_RESULT_SERVICES:
                _fail(
                    "E3DC-UPD-SERVICE-003",
                    f"Der Dienst {unit} konnte nicht rebootfest aktiviert werden.",
                    f"Führe sudo systemctl enable {unit} aus und starte danach denselben Updatebefehl erneut.",
                )
            warnings.append(f"Optionaler Dienst konnte nicht aktiviert werden: {unit}")
    return warnings


def _final_confirmation(
    target_root: Path,
    expected_version: str,
    required_services: tuple[str, ...],
    forbidden_services: Iterable[str] = (),
    *,
    required_writer_admission_mode: str = "",
) -> list[str]:
    version = (target_root / "VERSION").read_text(encoding="utf-8").strip()
    if version != expected_version:
        _fail(
            "E3DC-UPD-END-001",
            "Die installierte VERSION stimmt nach dem Austausch nicht mit dem Ziel überein.",
            "Starte den Ein-Datei-Updater erneut; das Vollbackup bleibt erhalten.",
        )
    web_version = Path("/var/www/html/VERSION").read_text(encoding="utf-8").strip()
    if web_version != expected_version:
        _fail(
            "E3DC-UPD-END-003",
            "Die vom Webserver verwendete VERSION stimmt nach dem Austausch nicht mit dem Ziel überein.",
            "Starte den Ein-Datei-Updater erneut; das Vollbackup bleibt erhalten.",
        )

    required = set(required_services) | {"apache2.service"}
    forbidden = {_normalize_unit(unit) for unit in forbidden_services}
    pending = set(required)
    deadline = time.monotonic() + 30.0
    green_since: float | None = None
    stable = False
    while time.monotonic() < deadline:
        competing = {unit for unit in forbidden if _service_active(unit)}
        if competing:
            unit = sorted(competing)[0]
            _fail(
                "E3DC-UPD-END-005",
                "Ein abgelöster alter oder konkurrierender Dienst wurde nach dem Neustart wieder aktiv: "
                + ", ".join(sorted(competing)),
                f"Stoppe und deaktiviere ihn mit: sudo systemctl disable --now {unit}",
            )
        pending = {unit for unit in required if not _service_active(unit)}
        if required_writer_admission_mode:
            try:
                from Installer.ha_writer_admission import evaluate_writer_admission

                admission = evaluate_writer_admission()
            except Exception:
                admission = {"allowed": False, "mode": ""}
            if not (
                admission.get("allowed") is True
                and admission.get("mode") == required_writer_admission_mode
            ):
                pending.add("HA-Schreiberfreigabe")
        if pending:
            green_since = None
        elif green_since is None:
            green_since = time.monotonic()
        elif time.monotonic() - green_since >= 5.0:
            stable = True
            break
        time.sleep(1.0)
    if not stable:
        unstable = sorted(pending or required)
        unit = (
            "e3dc-ha.service"
            if "HA-Schreiberfreigabe" in unstable
            else unstable[0]
        )
        _fail(
            "E3DC-UPD-END-004",
            "Die Pflichtdienste blieben nach dem Neustart nicht fünf Sekunden stabil: "
            + ", ".join(unstable),
            f"Zeige die Ursache mit: sudo journalctl -u {unit} --no-pager -n 120",
        )

    http_ok = False
    http_deadline = time.monotonic() + 20.0
    while time.monotonic() < http_deadline:
        http = _run(
            [
                "/usr/bin/curl",
                "-sS",
                "--connect-timeout",
                "2",
                "--max-time",
                "5",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}\n%{redirect_url}",
                "http://127.0.0.1/index.php",
            ],
            timeout=10,
        )
        response = http.stdout.splitlines()
        code = response[0].strip() if response else ""
        redirect = response[1].strip() if len(response) > 1 else ""
        local_redirect = False
        if code.startswith("3") and len(code) == 3 and redirect:
            parsed = urlparse(redirect)
            local_redirect = (
                parsed.scheme in {"http", "https"}
                and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
            )
        if (
            http.returncode == 0
            and len(code) == 3
            and (code.startswith("2") or local_redirect)
        ):
            http_ok = True
            break
        time.sleep(1.0)
    if not http_ok:
        return [
            "Apache läuft, aber http://127.0.0.1/index.php antwortet nicht mit "
            "HTTP 2xx oder einer lokalen Weiterleitung. Eine abweichende lokale "
            "Port-/VirtualHost-Konfiguration kann die Ursache sein; prüfe bei Bedarf "
            "sudo journalctl -u apache2 --no-pager -n 120."
        ]
    return []


def _start_previous_services_best_effort(
    active_before: tuple[str, ...],
    *,
    allow_legacy: bool,
    role: str = "",
    extra_units: Iterable[str] = (),
    exact_prestate: bool = False,
) -> tuple[str, ...]:
    from Installer.service_catalog import allowed_services

    restartable = {_normalize_unit(item) for item in allowed_services()}
    catalog = set(restartable)
    restartable.update(EXPLICIT_CUTOVER_SERVICES)
    extra_set = {_normalize_unit(item) for item in extra_units}
    restartable.update(extra_set)
    selected_role_service = ROLE_SERVICE_BY_MODE.get(role)
    role_units = set(ROLE_SERVICE_BY_MODE.values())
    role_was_active = bool(
        selected_role_service and selected_role_service in active_before
    )
    _run(["/usr/bin/systemctl", "daemon-reload"], timeout=60)
    ordered = list(active_before)
    if role_was_active and selected_role_service is not None:
        ordered.remove(selected_role_service)
        ordered.insert(0, selected_role_service)
    failed: list[str] = []
    for unit in ordered:
        if unit not in restartable:
            continue
        if not exact_prestate and role and unit in role_units and unit != selected_role_service:
            continue
        if not exact_prestate and role in {"master", "slave", "shadow"}:
            if unit in LEGACY_SERVICE_MIGRATIONS:
                continue
            if unit in extra_set:
                # Ein unbekannter Alt-Writer besitzt kein HA-Lease-Starttor und
                # wird unter einer Rolleninstanz niemals direkt reaktiviert.
                continue
            if unit in catalog:
                if unit != selected_role_service:
                    continue
            if role == "shadow" and unit in {
                "piguard.service",
                "luxtronik.service",
            }:
                continue
            if unit == "e3dc.service":
                continue
        if unit == "e3dc.service" and not allow_legacy:
            continue
        if _service_exists(unit):
            started = _run(["/usr/bin/systemctl", "start", unit], timeout=60)
            if started.returncode != 0 or not _service_active(unit):
                failed.append(unit)
    return tuple(failed)


def _restore_service_enablement_best_effort(
    prestate: ServicePrestate,
    touched_units: Iterable[str],
) -> None:
    """Stellt nach einem fehlgeschlagenen Wechsel den Enable-Vorzustand her."""

    enabled_before = set(prestate.enabled)
    for raw_unit in dict.fromkeys(_normalize_unit(item) for item in touched_units):
        unit = _normalize_unit(raw_unit)
        if not _service_exists(unit):
            continue
        should_enable = unit in enabled_before
        if _service_enabled(unit) == should_enable:
            continue
        action = "enable" if should_enable else "disable"
        _run(["/usr/bin/systemctl", action, unit], timeout=60)


def _restore_apache_security_enablement_best_effort(
    enabled_before: bool,
) -> tuple[bool, str]:
    """Stellt ausschließlich den vor dem Wechsel gebundenen a2enconf-Zustand her."""

    expected = bool(enabled_before)
    if os.path.lexists(APACHE_SECURITY_ENABLE_LINK) == expected:
        return True, ""
    action = "a2enconf" if expected else "a2disconf"
    command = Path("/usr/sbin") / action
    try:
        result = _run([command, "e3dc-control-security"], timeout=30)
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        return False, f"{action} konnte nicht ausgeführt werden ({detail})"
    if (
        result.returncode != 0
        or os.path.lexists(APACHE_SECURITY_ENABLE_LINK) != expected
    ):
        detail = (result.stderr or result.stdout or f"Exit {result.returncode}").strip()
        return False, f"{action} stellte den früheren Zustand nicht her ({detail})"
    return True, ""


def _stop_services_for_restore_best_effort(units: Iterable[str]) -> tuple[str, ...]:
    """Stoppt nach einem Zielteilfehler jeden möglicherweise neuen Dienst."""

    failed: list[str] = []
    ordered = tuple(dict.fromkeys(_normalize_unit(item) for item in units))
    for unit in reversed(ordered):
        if not _service_exists(unit) or not _service_active(unit):
            continue
        stopped = _run(["/usr/bin/systemctl", "stop", unit], timeout=60)
        if stopped.returncode != 0 or _service_active(unit):
            failed.append(unit)
    return tuple(sorted(failed))


def _restore_preupdate_state(
    *,
    target_root: Path,
    backup_path: Path,
    quiesced_backup: Path,
    quiesced_guard: object,
    prestate: ServicePrestate,
    target_services: Iterable[str],
    target_enable_services: Iterable[str],
    role: str,
) -> tuple[bool, str]:
    """Stellt nach begonnener Zielmutation den gesicherten Altstand wieder her."""

    stop_scope = tuple(
        dict.fromkeys(
            (
                *prestate.cutover_scope,
                *tuple(target_services),
                *tuple(target_enable_services),
            )
        )
    )
    stop_failed = _stop_services_for_restore_best_effort(stop_scope)
    if stop_failed:
        return (
            False,
            "Der automatische Rücklauf begann nicht, weil diese Zieldienste nicht "
            "sicher gestoppt werden konnten: " + ", ".join(stop_failed),
        )

    try:
        from Installer.backup import restore_quiesced_overlay, restore_verified_backup

        restore_verified_backup(backup_path, install_path=target_root)
        restore_quiesced_overlay(
            quiesced_backup,
            install_path=target_root,
            guard=quiesced_guard,
        )
        _clear_legacy_update_blockers(backup_path)
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        return (
            False,
            "Der automatische Rücklauf aus Vollbackup und ruhender "
            f"Daten-Nachsicherung blieb unvollständig ({detail}). Die E3DC-Control-"
            "Dienste bleiben gestoppt.",
        )

    apache_restored, apache_detail = _restore_apache_security_enablement_best_effort(
        prestate.apache_security_enabled
    )
    if not apache_restored:
        action = "a2enconf" if prestate.apache_security_enabled else "a2disconf"
        return (
            False,
            "Der gesicherte Dateistand wurde wiederhergestellt, aber die frühere "
            "Apache-Konfigurationsaktivierung blieb abweichend: "
            + apache_detail
            + f". Führe sudo {action} e3dc-control-security aus; die "
            "E3DC-Control-Dienste bleiben bis dahin gestoppt.",
        )

    touched_units = tuple(
        dict.fromkeys(
            (
                *prestate.present,
                *tuple(target_services),
                *tuple(target_enable_services),
            )
        )
    )
    _restore_service_enablement_best_effort(prestate, touched_units)
    enabled_before = set(prestate.enabled)
    enablement_mismatch = tuple(
        sorted(
            unit
            for unit in prestate.present
            if _service_exists(unit)
            and _service_enabled(unit) != (unit in enabled_before)
        )
    )
    if enablement_mismatch:
        return (
            False,
            "Der gesicherte Dateistand wurde wiederhergestellt, aber der frühere "
            "systemd-Enable-Zustand blieb für folgende Dienste abweichend: "
            + ", ".join(enablement_mismatch)
            + ". Die E3DC-Control-Dienste bleiben gestoppt.",
        )

    start_failed = _start_previous_services_best_effort(
        prestate.active,
        allow_legacy=True,
        role=role,
        extra_units=prestate.confirmed_unknown_writers,
        exact_prestate=True,
    )
    if start_failed:
        return (
            False,
            "Produktstand, ruhende Nutzerdaten und Dienstfreigaben wurden "
            "wiederhergestellt; folgende zuvor aktive Dienste starteten jedoch nicht: "
            + ", ".join(start_failed),
        )
    return (
        True,
        "Der gesicherte Produktstand einschließlich der ruhenden Nutzerdaten und "
        "der zuvor aktive rollenrichtige Dienstsatz wurden wiederhergestellt.",
    )


def _recover_failed_cutover(
    *,
    target_root: Path,
    backup_path: Path | None,
    quiesced_backup: Path | None,
    quiesced_guard: object | None,
    prestate: ServicePrestate | None,
    active_before: tuple[str, ...],
    services: tuple[str, ...],
    enable_services: tuple[str, ...],
    role: str,
    product_mutated: bool,
    replacement_confirmed: bool,
) -> tuple[str, str | None]:
    """Ordnet den Fehlerpfad ausschließlich nach der erreichten Updatephase."""

    if replacement_confirmed:
        return (
            "Der neue Release- und Dienststand wurde bereits bestätigt und deshalb "
            "nicht zurückgesetzt. Die abschließende Altbereinigung blieb unvollständig. "
            f"Vollbackup: {backup_path}",
            None,
        )
    if product_mutated:
        if (
            backup_path is None
            or quiesced_backup is None
            or quiesced_guard is None
            or prestate is None
        ):
            return (
                "Der Zielbaum wurde bereits verändert, aber der vollständige "
                "Rücklaufkontext fehlt. Die E3DC-Control-Dienste bleiben gestoppt. "
                f"Vollbackup: {backup_path or 'nicht verfügbar'}; ruhende "
                f"Daten-Nachsicherung: {quiesced_backup or 'nicht verfügbar'}",
                "Starte denselben Ein-Datei-Updater erneut; er repariert den Zielstand aus dem veröffentlichten Release.",
            )
        restored, detail = _restore_preupdate_state(
            target_root=target_root,
            backup_path=backup_path,
            quiesced_backup=quiesced_backup,
            quiesced_guard=quiesced_guard,
            prestate=prestate,
            target_services=services,
            target_enable_services=enable_services,
            role=role,
        )
        return (
            f"{detail} Vollbackup: {backup_path}; ruhende Daten-Nachsicherung: "
            f"{quiesced_backup}",
            None
            if restored
            else "Starte denselben Ein-Datei-Updater erneut; er repariert den Zielstand aus dem veröffentlichten Release.",
        )

    failed = _start_previous_services_best_effort(
        active_before,
        allow_legacy=True,
        role=role,
        extra_units=(
            prestate.confirmed_unknown_writers if prestate is not None else ()
        ),
        exact_prestate=True,
    )
    if failed:
        return (
            "Produktdateien wurden noch nicht ausgetauscht. Folgende zuvor aktive "
            "Dienste starteten nach dem frühen Abbruch nicht wieder: "
            + ", ".join(failed)
            + f". Vollbackup: {backup_path}",
            None,
        )
    return (
        "Produktdateien wurden noch nicht ausgetauscht; die zuvor aktiven Dienste "
        f"wurden wieder gestartet. Vollbackup: {backup_path}",
        None,
    )


def _safe_recover_failed_cutover(**kwargs: object) -> tuple[str, str | None]:
    """Hält auch einen unerwarteten Fehler des Rücklaufs als Lösung sichtbar."""

    try:
        return _recover_failed_cutover(**kwargs)  # type: ignore[arg-type]
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        return (
            "Der automatische Rücklauf konnte nicht vollständig ausgeführt werden "
            f"({detail}). E3DC-Control-Dienste können gestoppt sein. Vollbackup: "
            f"{kwargs.get('backup_path') or 'nicht verfügbar'}; ruhende "
            f"Daten-Nachsicherung: {kwargs.get('quiesced_backup') or 'nicht verfügbar'}",
            "Starte denselben Ein-Datei-Updater erneut; er repariert den Zielstand aus dem veröffentlichten Release.",
        )


def perform_update(
    *,
    target_root: Path,
    install_user: str,
    tag: str,
    role: str,
) -> int:
    release_root = RELEASE_ROOT
    policy = _load_policy(release_root)
    _validate_inputs(target_root, release_root, install_user, tag)

    lock_descriptor = _acquire_update_lock()
    try:
        existing_config = _load_existing_config(target_root)
        role_service_intended = False
        config = dict(existing_config)
        active_before: tuple[str, ...] = ()
        service_prestate: ServicePrestate | None = None
        backup_path: Path | None = None
        quiesced_backup: Path | None = None
        quiesced_guard: object | None = None
        cutover_started = False
        product_mutated = False
        replacement_confirmed = False
        services: tuple[str, ...] = ()
        enable_services: tuple[str, ...] = ()
        warnings: list[str] = []
        try:
            backup_path = _create_backup(target_root)
            print(f"[OK] Vollbackup: {backup_path}", flush=True)
            role_service_intended = _role_service_intended(role)
            config = _bind_role_context(
                role,
                existing_config,
                require_peer=(
                    role in {"master", "slave"} and role_service_intended
                ),
            )
            service_prestate = _capture_service_prestate()
            selected_catalog_units = tuple(
                dict.fromkeys(
                    (
                        *service_prestate.catalog_active,
                        *service_prestate.catalog_enabled,
                        *(
                            target
                            for source, target in LEGACY_SERVICE_MIGRATIONS.items()
                            if source
                            in (
                                set(service_prestate.active)
                                | set(service_prestate.enabled)
                            )
                        ),
                    )
                )
            )
            migrated_active_targets = tuple(
                dict.fromkeys(
                    target
                    for source, target in LEGACY_SERVICE_MIGRATIONS.items()
                    if source in set(service_prestate.active)
                )
            )
            venv_python = _repair_packages(
                policy,
                install_user,
                config,
                selected_catalog_units,
            )
            if not shutil.which("rsync"):
                _fail(
                    "E3DC-UPD-DEP-001",
                    "rsync fehlt auch nach dem automatischen Reparaturversuch.",
                    "Installiere es mit: sudo apt-get install -y rsync",
                )
            active_before = service_prestate.active
            cutover_started = True
            _stop_for_cutover(service_prestate.cutover_scope)
            quiesced_backup, quiesced_guard = _create_stopped_data_backup(
                target_root,
                backup_path,
            )
            print(f"[OK] Ruhende Daten-Nachsicherung: {quiesced_backup}", flush=True)
            quarantine = _clear_legacy_update_blockers(backup_path)
            if quarantine is not None:
                print(f"[OK] Alter Updatezustand archiviert: {quarantine}", flush=True)
            _confirm_cutover_quiet(service_prestate.cutover_scope)
            print("[3/4] Ersetze Produktdateien und repariere Rechte …", flush=True)
            product_mutated = True
            _replace_product_tree(target_root, release_root, install_user)
            _sync_web_tree(target_root, install_user)
            warnings.extend(
                _repair_units_and_permissions(
                    target_root,
                    release_root,
                    install_user,
                    role,
                    config,
                    policy,
                    venv_python,
                    service_prestate,
                    selected_catalog_units,
                    require_role_peer=(
                        role in {"master", "slave"} and role_service_intended
                    ),
                )
            )
            services = _services_to_start(
                policy,
                active_before,
                role,
                service_prestate.enabled,
                role_service_intended=role_service_intended,
            )
            enable_services = _services_to_enable(
                service_prestate,
                services,
                role,
                role_service_intended=role_service_intended,
            )
            required = _required_services(
                role,
                (
                    *service_prestate.catalog_active,
                    *migrated_active_targets,
                ),
                role_service_intended=role_service_intended,
            )
            required_writer_admission_mode = (
                role
                if role in {"master", "slave"}
                and any(
                    unit != ROLE_SERVICE_BY_MODE[role]
                    for unit in required
                )
                else ""
            )
            warnings.extend(
                _start_services(
                    services,
                    required=required,
                    enable_services=enable_services,
                )
            )
            warnings.extend(
                _final_confirmation(
                    target_root,
                    tag.removeprefix("v"),
                    required,
                    _forbidden_services_after_start(
                        service_prestate,
                        role,
                        role_service_intended=role_service_intended,
                    ),
                    required_writer_admission_mode=required_writer_admission_mode,
                )
            )
            replacement_confirmed = True
            warnings.extend(_retire_unknown_active_e3dc(service_prestate))
            _disable_competing_controllers(
                role,
                (
                    *_services_to_disable_after_success(
                        service_prestate,
                        role,
                        role_service_intended=role_service_intended,
                    ),
                    *(
                        source
                        for source in LEGACY_SERVICE_MIGRATIONS
                        if source in set(service_prestate.present)
                    ),
                ),
            )
        except UpdateFailure as exc:
            if cutover_started:
                state, recovery_solution = _safe_recover_failed_cutover(
                    target_root=target_root,
                    backup_path=backup_path,
                    quiesced_backup=quiesced_backup,
                    quiesced_guard=quiesced_guard,
                    prestate=service_prestate,
                    active_before=active_before,
                    services=services,
                    enable_services=enable_services,
                    role=role,
                    product_mutated=product_mutated,
                    replacement_confirmed=replacement_confirmed,
                )
            elif backup_path is not None:
                recovery_solution = None
                state = (
                    "Produktdateien und laufende Dienste wurden noch nicht für den "
                    "Austausch verändert; vorbereitende Paket- oder Altupdate-Reparaturen "
                    f"können erfolgt sein. Vollbackup: {backup_path}"
                )
            else:
                recovery_solution = None
                state = exc.system_state
            raise UpdateFailure(
                code=exc.code,
                happened=exc.happened,
                solution=recovery_solution or exc.solution,
                system_state=state,
            ) from exc
        except Exception as exc:
            if cutover_started:
                state, recovery_solution = _safe_recover_failed_cutover(
                    target_root=target_root,
                    backup_path=backup_path,
                    quiesced_backup=quiesced_backup,
                    quiesced_guard=quiesced_guard,
                    prestate=service_prestate,
                    active_before=active_before,
                    services=services,
                    enable_services=enable_services,
                    role=role,
                    product_mutated=product_mutated,
                    replacement_confirmed=replacement_confirmed,
                )
            else:
                recovery_solution = None
                state = (
                    "Produktdateien und laufende Dienste blieben unverändert. "
                    f"Vollbackup: {backup_path or 'noch nicht erstellt'}"
                )
            _fail(
                "E3DC-UPD-APPLY-001",
                f"Der Dateiaustausch konnte nicht vollständig abgeschlossen werden: {exc}",
                recovery_solution
                or "Starte den Ein-Datei-Updater erneut; das vorhandene Vollbackup bleibt erhalten.",
                system_state=state,
            )
    finally:
        os.close(lock_descriptor)
    print("\n[OK] Update abgeschlossen.")
    print(f"Version: {tag.removeprefix('v')}")
    print(f"Backup: {backup_path}")
    for warning in warnings:
        print(f"[WARNUNG] {warning}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Einfacher E3DC-Control Releasewechsel")
    parser.add_argument("--target", required=True)
    parser.add_argument("--install-user", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--role", choices=("off", "master", "slave", "shadow"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return perform_update(
            target_root=Path(args.target).absolute(),
            install_user=args.install_user,
            tag=args.tag,
            role=args.role,
        )
    except UpdateFailure as exc:
        print(f"\n[ABBRUCH] {exc.code}", file=sys.stderr)
        print(f"Was ist passiert: {exc.happened}", file=sys.stderr)
        print(f"Systemzustand: {exc.system_state}", file=sys.stderr)
        print(f"Lösung: {exc.solution}", file=sys.stderr)
        return 1
    except Exception as exc:
        print("\n[ABBRUCH] E3DC-UPD-UNEXPECTED-001", file=sys.stderr)
        print(f"Was ist passiert: Der Updater konnte nicht fortfahren: {exc}", file=sys.stderr)
        print(
            "Systemzustand: Der genaue Zustand ist der vorstehenden Ausgabe zu entnehmen; "
            "ein bereits genanntes Vollbackup bleibt erhalten.",
            file=sys.stderr,
        )
        print(
            "Lösung: Starte denselben Ein-Datei-Updater erneut. Bleibt der Fehler bestehen, "
            "sende die vollständige Ausgabe ab [1/4].",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
