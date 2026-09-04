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
import errno
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
from contextlib import contextmanager
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
SIMPLE_UPDATE_RUNTIME_APT_PACKAGES = ("php-cli",)
SERVICE_WRAPPER_ACTIONS = ("start", "stop", "restart", "status", "enable", "disable")
MATTER_RESET_ACTION = "reset-matter-pairing"
MATTER_RESET_UNIT = "e3dc-matter-bridge.service"
MATTER_RESET_QUARANTINE_NAME = ".matter-storage-reset-quarantine"
MATTER_RESET_QUARANTINE_PREPARE_NAME = ".matter-storage-reset-quarantine.prepare"
MATTER_RESET_RECEIPT_NAME = ".e3dc-matter-reset-transaction.json"
MATTER_RESET_STAGE_PREFIX = ".matter-storage-reset-stage-"
MATTER_RESET_PROTECTED_DATA_NAMES = frozenset(
    {
        MATTER_RESET_QUARANTINE_NAME,
        MATTER_RESET_QUARANTINE_PREPARE_NAME,
        MATTER_RESET_RECEIPT_NAME,
    }
)
MATTER_RESET_PROTECTED_DATA_PREFIXES = (MATTER_RESET_STAGE_PREFIX,)
ROOT_UPDATE_LOCK = Path("/run/lock/e3dc-control/update.lock")
UPDATE_LOCK_ENV = "E3DC_UPDATE_LOCK_FD"
SERVICE_WRAPPER = Path("/usr/local/sbin/e3dc-service-control")
WEB_UPDATE_LAUNCHER = Path("/usr/local/sbin/e3dc-web-update-launcher")
RUNTIME_PERMISSIONS_LAUNCHER = Path(
    "/usr/local/sbin/e3dc-runtime-permissions-repair"
)
RUNTIME_PERMISSIONS_CONTRACT = Path(
    "/etc/e3dc-control/runtime_permissions_contract.json"
)
UPDATE_DRIFT_CONFIRM_ENV = "E3DC_UPDATE_CONFIRM_LOCAL_DRIFT"
UPDATE_DRIFT_CONFIRM_FILE = Path(
    "/run/e3dc-update-credentials/local-drift.token"
)
_UPDATE_DRIFT_CONFIRM_TOKEN: str | None = None
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


@dataclass
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
    target_bound_unknown_units: tuple[str, ...] = ()
    enable_states: tuple[tuple[str, str], ...] = ()
    fragment_paths: tuple[tuple[str, str], ...] = ()
    masked_persistent: tuple[str, ...] = ()
    masked_runtime: tuple[str, ...] = ()
    apache_security_enabled: bool = False

    @property
    def masked(self) -> tuple[str, ...]:
        return tuple(
            sorted(set(self.masked_persistent) | set(self.masked_runtime))
        )

    @property
    def retired_unknown_units(self) -> tuple[str, ...]:
        """Nur fachlich belegte Alt-Writer werden dauerhaft stillgelegt."""

        return tuple(sorted(set(self.confirmed_unknown_writers)))

    @property
    def cutover_unknown_units(self) -> tuple[str, ...]:
        """Alle unbekannten Zielroot-Dienste bleiben beim Austausch in Ruhe."""

        return tuple(
            sorted(
                set(self.confirmed_unknown_writers)
                | set(self.target_bound_unknown_units)
            )
        )

    @property
    def target_bound_observers(self) -> tuple[str, ...]:
        """Zielroot-Dienste ohne belegten konkurrierenden Hardwarezugriff."""

        return tuple(
            sorted(
                set(self.target_bound_unknown_units)
                - set(self.confirmed_unknown_writers)
            )
        )

    @property
    def enable_state_map(self) -> dict[str, str]:
        states = dict(self.enable_states)
        if states:
            return states
        enabled = set(self.enabled)
        return {
            unit: ("enabled" if unit in enabled else "disabled")
            for unit in self.present
        }

    @property
    def fragment_path_map(self) -> dict[str, str]:
        return dict(self.fragment_paths)


@dataclass(frozen=True)
class DirectoryMetadataPrestate:
    """Exakter, rückrollbarer Metadatenvertrag eines benannten Verzeichnisses."""

    path: str
    parent_device: int
    parent_inode: int
    device: int
    inode: int
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class DirectoryMetadataTransition:
    """Rückfallvertrag einer bestätigten Verzeichnismetadaten-Projektion."""

    previous: DirectoryMetadataPrestate
    projected: DirectoryMetadataPrestate


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
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or f"Exit {result.returncode}").strip()
        raise RuntimeError(f"{' '.join(command)}: {detail}")
    return result


def _apply_failure_solution(detail: str, target_root: Path) -> str:
    """Leitet aus einem Dateiaustauschfehler den nächsten konkreten Schritt ab."""

    normalized = str(detail or "").lower()
    target = shlex.quote(str(target_root))
    if any(
        marker in normalized
        for marker in ("no space left", "disk quota exceeded", "kein platz")
    ):
        return (
            "Schaffe auf dem betroffenen Dateisystem freien Speicherplatz. Prüfe zuerst: "
            f"df -h {target} /var/www/html /run ; starte danach denselben Ein-Datei-Updater erneut."
        )
    if any(
        marker in normalized
        for marker in ("read-only file system", "read only file system", "nur-lese-dateisystem")
    ):
        return (
            "Prüfe und behebe den schreibgeschützten Mount. Diagnose: "
            f"findmnt -T {target} ; starte danach denselben Ein-Datei-Updater erneut."
        )
    if any(
        marker in normalized
        for marker in ("permission denied", "operation not permitted", "berechtigung")
    ):
        return (
            "Prüfe Rechte, Mount und unveränderliche Dateiattribute des Installationspfads. "
            f"Diagnose: sudo namei -l {target} ; sudo lsattr -d {target} ; "
            "starte danach denselben Ein-Datei-Updater erneut."
        )
    return (
        "Prüfe die unmittelbar genannte Ursache des Dateiaustauschs und starte danach "
        "denselben Ein-Datei-Updater erneut; das vorhandene Vollbackup bleibt erhalten."
    )


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


def _service_enable_state(unit: str) -> str:
    result = _run(["/usr/bin/systemctl", "is-enabled", unit], timeout=20)
    return result.stdout.strip().lower()


def _service_fragment_path(unit: str) -> str:
    result = _run(
        [
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=FragmentPath",
            "--value",
            "--no-pager",
        ],
        timeout=20,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _is_exact_dev_null_mask(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
        return (
            stat.S_ISLNK(metadata.st_mode)
            and metadata.st_uid == 0
            and os.readlink(path) == "/dev/null"
        )
    except OSError:
        return False


def _service_mask_scopes(unit: str) -> tuple[bool, bool]:
    """Liefert persistente/runtime Maske; uneindeutige Symlinks brechen ab."""

    enabled_state = _service_enable_state(unit)
    persistent = any(
        _is_exact_dev_null_mask(root / unit)
        for root in (
            Path("/etc/systemd/system"),
            Path("/usr/local/lib/systemd/system"),
            Path("/usr/lib/systemd/system"),
            Path("/lib/systemd/system"),
        )
    )
    runtime = _is_exact_dev_null_mask(Path("/run/systemd/system") / unit)
    reports_mask = enabled_state in {"masked", "masked-runtime"}
    if reports_mask != (persistent or runtime):
        _fail(
            "E3DC-UPD-SERVICE-MASK-001",
            f"Der Maskenzustand von {unit} ist nicht eindeutig (is-enabled={enabled_state or 'leer'}).",
            f"Prüfe systemctl is-enabled {unit} und sudo find /etc/systemd/system /run/systemd/system -maxdepth 1 -name {shlex.quote(unit)} -ls; "
            "stelle eine exakte /dev/null-Maske her oder entferne den fehlerhaften Symlink und starte danach denselben Updatebefehl erneut.",
        )
    if reports_mask and _service_load_state(unit) != "masked":
        _fail(
            "E3DC-UPD-SERVICE-MASK-001",
            f"{unit} wird als {enabled_state} gemeldet, aber systemd hat die Maske nicht geladen.",
            f"Führe sudo systemctl daemon-reload aus, prüfe systemctl show {unit} -p LoadState und starte danach denselben Updatebefehl erneut.",
        )
    if enabled_state == "masked-runtime" and not runtime:
        _fail(
            "E3DC-UPD-SERVICE-MASK-001",
            f"Die Runtime-Maske von {unit} ist kein exakter /dev/null-Link.",
            f"Führe sudo systemctl mask --runtime {unit} aus und starte danach denselben Updatebefehl erneut.",
        )
    return persistent, runtime


def _service_mask_mismatches(prestate: ServicePrestate) -> tuple[str, ...]:
    expected_persistent = set(prestate.masked_persistent)
    expected_runtime = set(prestate.masked_runtime)
    mismatches: list[str] = []
    for unit in prestate.present:
        persistent, runtime = _service_mask_scopes(unit)
        if persistent != (unit in expected_persistent) or runtime != (unit in expected_runtime):
            expected = "+".join(
                scope
                for scope, selected in (
                    ("persistent", unit in expected_persistent),
                    ("runtime", unit in expected_runtime),
                )
                if selected
            ) or "unmasked"
            actual = "+".join(
                scope
                for scope, selected in (("persistent", persistent), ("runtime", runtime))
                if selected
            ) or "unmasked"
            mismatches.append(f"{unit}={actual} statt {expected}")
    return tuple(mismatches)


def _restore_service_masks_best_effort(prestate: ServicePrestate) -> tuple[str, ...]:
    """Stellt den gebundenen Aus-Zustand vor jedem Rücklauf-Neustart wieder her."""

    try:
        if not _service_mask_mismatches(prestate):
            return ()
    except Exception:
        pass
    expected_persistent = set(prestate.masked_persistent)
    expected_runtime = set(prestate.masked_runtime)
    failed: list[str] = []
    for unit in prestate.present:
        try:
            persistent, runtime = _service_mask_scopes(unit)
        except Exception:
            persistent, runtime = False, False
        desired_persistent = unit in expected_persistent
        desired_runtime = unit in expected_runtime
        if (persistent, runtime) == (desired_persistent, desired_runtime):
            continue
        _run(["/usr/bin/systemctl", "unmask", "--runtime", unit], timeout=30)
        _run(["/usr/bin/systemctl", "unmask", unit], timeout=30)
        if desired_persistent:
            _run(["/usr/bin/systemctl", "mask", unit], timeout=30)
        if desired_runtime:
            _run(["/usr/bin/systemctl", "mask", "--runtime", unit], timeout=30)
    _run(["/usr/bin/systemctl", "daemon-reload"], timeout=60)
    try:
        failed.extend(_service_mask_mismatches(prestate))
    except Exception as exc:
        failed.append(str(exc).strip() or exc.__class__.__name__)
    return tuple(failed)


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


def _loaded_services() -> tuple[str, ...]:
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
        if not _is_update_runtime_unit(unit):
            units.add(unit)
    return tuple(sorted(units))


def _loaded_e3dc_services() -> tuple[str, ...]:
    """Kompatibilitätssicht auf geladene E3DC-Dienste."""

    return tuple(unit for unit in _loaded_services() if unit.startswith("e3dc"))


def _enabled_service_units() -> tuple[str, ...]:
    """Liefert rebootfeste Units als Kandidaten für eine klare Zielroot-Bindung."""

    result = _run(
        [
            "/usr/bin/systemctl",
            "list-unit-files",
            "--type=service",
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
            f"Die aktivierten systemd-Dienste konnten nicht ermittelt werden: {detail}",
            "Führe sudo systemctl daemon-reload aus und starte danach denselben Updatebefehl erneut.",
        )
    enabled_states = {"enabled", "enabled-runtime", "linked", "linked-runtime"}
    units: set[str] = set()
    for raw_line in result.stdout.splitlines():
        fields = raw_line.strip().split()
        if len(fields) < 2 or fields[1].strip().lower() not in enabled_states:
            continue
        unit = _normalize_unit(fields[0])
        if not _is_update_runtime_unit(unit):
            units.add(unit)
    return tuple(sorted(units))


def _systemd_unescape(value: str) -> str:
    """Dekodiert nur systemd-\\xNN-Sequenzen; andere Inhalte bleiben wörtlich."""

    return re.sub(
        r"\\x([0-9A-Fa-f]{2})",
        lambda match: chr(int(match.group(1), 16)),
        str(value or ""),
    )


def _path_is_within_target(value: str, target_root: Path) -> bool:
    raw = str(value or "").strip()
    if raw.startswith("-"):
        raw = raw[1:]
    if not raw or not os.path.isabs(raw):
        return False
    candidate = Path(os.path.abspath(raw))
    target = Path(os.path.abspath(target_root))
    try:
        candidate.relative_to(target)
        return True
    except ValueError:
        return False


def _execstart_mentions_target(value: str, target_root: Path) -> bool:
    """Bindet nur ein vollständiges absolutes Zielroot-Pfadsegment in ExecStart."""

    payload = _systemd_unescape(value)
    target = re.escape(os.path.abspath(target_root))
    return re.search(
        rf"(?<![A-Za-z0-9_.\-/]){target}(?:/|(?=$|[\s;,'\"}}\]]))",
        payload,
    ) is not None


def _service_target_bindings(unit: str, target_root: Path) -> tuple[str, ...]:
    """Belegt eine Zielroot-Bindung ohne Namens- oder Skriptheuristik."""

    result = _run(
        [
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=WorkingDirectory",
            "--property=ExecStart",
            "--property=MainPID",
            "--no-pager",
        ],
        timeout=20,
    )
    if result.returncode != 0:
        return ()
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"WorkingDirectory", "ExecStart", "MainPID"}:
            values[key] = value.strip()

    evidence: list[str] = []
    working_directory = _systemd_unescape(values.get("WorkingDirectory", ""))
    if _path_is_within_target(working_directory, target_root):
        evidence.append("WorkingDirectory")
    if _execstart_mentions_target(values.get("ExecStart", ""), target_root):
        evidence.append("ExecStart")
    try:
        main_pid = int(values.get("MainPID", "0"))
    except ValueError:
        main_pid = 0
    if main_pid > 1:
        try:
            process_cwd = os.readlink(f"/proc/{main_pid}/cwd")
        except OSError:
            process_cwd = ""
        if _path_is_within_target(process_cwd, target_root):
            evidence.append("MainPID-CWD")
    return tuple(evidence)


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


def _capture_service_prestate(target_root: Path | None = None) -> ServicePrestate:
    from Installer.service_catalog import allowed_services

    catalog = {_normalize_unit(item) for item in allowed_services()}
    loaded = (
        set(_loaded_services())
        if target_root is not None
        else set(_loaded_e3dc_services())
    )
    dynamic = {unit for unit in loaded if unit.startswith("e3dc")}
    enabled_unit_files = set(_enabled_service_units()) if target_root is not None else set()
    known_candidates = catalog | set(EXPLICIT_CUTOVER_SERVICES)
    target_bound = (
        {
            unit
            for unit in loaded | enabled_unit_files
            if _service_target_bindings(unit, target_root)
        }
        if target_root is not None
        else set()
    )
    target_bound_unknown = target_bound - known_candidates
    present = {
        unit
        for unit in known_candidates
        if _service_present_or_masked(unit) or _service_exists(unit)
    } | dynamic | target_bound
    inspected = {
        unit
        for unit in present | dynamic
        if not _is_update_runtime_unit(unit)
    }
    masked_persistent: set[str] = set()
    masked_runtime: set[str] = set()
    enable_states: dict[str, str] = {}
    fragment_paths: dict[str, str] = {}
    for unit in sorted(inspected):
        enable_state = _service_enable_state(unit)
        enable_states[unit] = enable_state
        if enable_state in {"enabled", "enabled-runtime", "linked", "linked-runtime"}:
            fragment_paths[unit] = _service_fragment_path(unit)
        persistent_mask, runtime_mask = _service_mask_scopes(unit)
        if persistent_mask:
            masked_persistent.add(unit)
        if runtime_mask:
            masked_runtime.add(unit)
    active = {unit for unit in inspected if _service_active(unit)}
    enabled_candidates = catalog | set(EXPLICIT_CUTOVER_SERVICES) | active | target_bound
    enabled = {
        unit
        for unit in enabled_candidates
        if enable_states.get(unit) in {"enabled", "enabled-runtime", "linked", "linked-runtime"}
    }
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
    } | confirmed_unknown_writers | target_bound_unknown
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
        target_bound_unknown_units=tuple(sorted(target_bound_unknown)),
        enable_states=tuple(sorted(enable_states.items())),
        fragment_paths=tuple(sorted(fragment_paths.items())),
        masked_persistent=tuple(sorted(masked_persistent)),
        masked_runtime=tuple(sorted(masked_runtime)),
        apache_security_enabled=os.path.lexists(APACHE_SECURITY_ENABLE_LINK),
    )


def _capture_active_services(target_root: Path | None = None) -> tuple[str, ...]:
    """Kompatibilitätshelfer für lokale Alt-Regressionen."""

    return _capture_service_prestate(target_root).active


def _open_update_lock_directory(lock_path: Path) -> int:
    """Bindet oder normalisiert den privaten Update-Locknamespace nofollow."""

    lock_path = Path(lock_path)
    private_path = lock_path.parent
    shared_path = private_path.parent
    if (
        not lock_path.is_absolute()
        or lock_path.name != "update.lock"
        or private_path.name != "e3dc-control"
        or shared_path.name != "lock"
    ):
        raise RuntimeError("Update-Lockpfad weicht vom festen Produktvertrag ab")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory:
        raise RuntimeError("Sichere Update-Lockbindung ist nicht verfügbar")
    flags = os.O_RDONLY | nofollow | directory | cloexec

    shared_named = os.lstat(shared_path)
    shared_mode = stat.S_IMODE(shared_named.st_mode)
    if (
        stat.S_ISLNK(shared_named.st_mode)
        or not stat.S_ISDIR(shared_named.st_mode)
        or shared_named.st_uid != 0
        or shared_named.st_gid != 0
        or (
            shared_mode & (stat.S_IWGRP | stat.S_IWOTH)
            and not shared_named.st_mode & stat.S_ISVTX
        )
    ):
        raise RuntimeError("Systemweites Update-Lockverzeichnis ist nicht vertrauenswürdig")

    shared_fd = os.open(shared_path, flags)
    private_fd = -1
    try:
        shared_opened = os.fstat(shared_fd)
        if (
            (shared_opened.st_dev, shared_opened.st_ino)
            != (shared_named.st_dev, shared_named.st_ino)
            or not stat.S_ISDIR(shared_opened.st_mode)
        ):
            raise RuntimeError("Systemweiter Update-Locknamespace driftete beim Öffnen")
        try:
            os.mkdir(private_path.name, 0o700, dir_fd=shared_fd)
        except FileExistsError:
            pass

        private_named = os.stat(
            private_path.name,
            dir_fd=shared_fd,
            follow_symlinks=False,
        )
        private_fd = os.open(private_path.name, flags, dir_fd=shared_fd)
        private_opened = os.fstat(private_fd)
        private_mode = stat.S_IMODE(private_opened.st_mode)
        if (
            not stat.S_ISDIR(private_named.st_mode)
            or not stat.S_ISDIR(private_opened.st_mode)
            or (private_named.st_dev, private_named.st_ino)
            != (private_opened.st_dev, private_opened.st_ino)
            or private_opened.st_uid != 0
            or private_opened.st_gid != 0
            or private_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Privater Update-Locknamespace ist nicht sicher bindbar")

        # Ein älterer direkter Ziel-Updater konnte diesen root-eigenen, nicht
        # fremdbeschreibbaren Ordner mit 0755 anlegen. Genau dieser sichere
        # Altfall wird auf den gemeinsamen 0700-Vertrag normalisiert.
        os.fchmod(private_fd, 0o700)
        os.fsync(private_fd)
        private_after = os.fstat(private_fd)
        private_rebound = os.stat(
            private_path.name,
            dir_fd=shared_fd,
            follow_symlinks=False,
        )
        if (
            (private_after.st_dev, private_after.st_ino)
            != (private_opened.st_dev, private_opened.st_ino)
            or (private_rebound.st_dev, private_rebound.st_ino)
            != (private_opened.st_dev, private_opened.st_ino)
            or private_after.st_uid != 0
            or private_after.st_gid != 0
            or stat.S_IMODE(private_after.st_mode) != 0o700
        ):
            raise RuntimeError("Privater Update-Locknamespace blieb nach der Bindung abweichend")
        result = private_fd
        private_fd = -1
        return result
    finally:
        if private_fd >= 0:
            os.close(private_fd)
        os.close(shared_fd)


def _acquire_update_lock() -> int:
    """Verhindert genau einen konkurrierenden alten oder neuen Updatelauf."""

    try:
        directory_fd = _open_update_lock_directory(ROOT_UPDATE_LOCK)
        try:
            before = None
            try:
                before = os.stat(
                    ROOT_UPDATE_LOCK.name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            descriptor = os.open(
                ROOT_UPDATE_LOCK.name,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
            opened = os.fstat(descriptor)
            named = os.stat(
                ROOT_UPDATE_LOCK.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(named.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != 0
                or opened.st_gid != 0
                or stat.S_IMODE(opened.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                or (
                    before is not None
                    and (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_nlink != 1
                        or before.st_uid != 0
                        or before.st_gid != 0
                        or stat.S_IMODE(before.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
                        or (before.st_dev, before.st_ino)
                        != (opened.st_dev, opened.st_ino)
                    )
                )
            ):
                raise RuntimeError("Update-Lockdatei besitzt keinen sicheren Vertrag")
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            secured = os.fstat(descriptor)
            rebound = os.stat(
                ROOT_UPDATE_LOCK.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                (secured.st_dev, secured.st_ino)
                != (rebound.st_dev, rebound.st_ino)
                or secured.st_nlink != 1
                or secured.st_uid != 0
                or secured.st_gid != 0
                or stat.S_IMODE(secured.st_mode) != 0o600
            ):
                raise RuntimeError("Update-Lockdatei driftete nach der Bindung")
        finally:
            os.close(directory_fd)
    except Exception as exc:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass
        _fail(
            "E3DC-UPD-LOCK-001",
            f"Der gemeinsame Update-Lock ist nicht sicher bindbar: {exc}",
            "Prüfe /run/lock/e3dc-control auf einen echten root-eigenen Ordner und starte danach denselben Updatebefehl erneut.",
        )

    try:
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


@contextmanager
def _bound_update_lock_environment(descriptor: int):
    """Reicht den Lock an synchrone Recovery- und Backup-Primitiven weiter."""

    previous = os.environ.get(UPDATE_LOCK_ENV)
    os.environ[UPDATE_LOCK_ENV] = str(int(descriptor))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(UPDATE_LOCK_ENV, None)
        else:
            os.environ[UPDATE_LOCK_ENV] = previous


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


def _load_existing_config(
    target_root: Path,
    target_binding: DirectoryMetadataPrestate,
) -> dict:
    """Liest Alt-Konfigurationen nofollow; syntaktische Altfehler bleiben reparierbar."""

    merged: dict = {}
    readers = (
        (
            target_root / "Installer/installer_config.json",
            lambda: _read_live_installer_json(
                target_binding,
                "installer_config.json",
                missing_ok=True,
            ),
        ),
        (
            Path("/var/www/html/data/e3dc_v4.json"),
            lambda: _read_live_web_v4_json(missing_ok=True),
        ),
    )
    for path, reader in readers:
        try:
            value = reader()
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"[WARNUNG] Bestehende Konfiguration ist nicht lesbar ({path}): {exc}")
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


def _bind_role_context(
    role: str,
    config: dict,
    *,
    bound_peer_ip: str = "",
    require_peer: bool = False,
) -> dict:
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
    elif role in {"master", "slave"} and bound_peer_ip:
        normalized_bound_peer = _normalized_peer_ip(bound_peer_ip)
        if not normalized_bound_peer:
            _fail(
                "E3DC-UPD-CONFIG-005",
                "Die gebundene HA-Peer-IP ist nicht gültig.",
                "Starte denselben Ein-Datei-Updater erneut, damit die Instanzbindung frisch ermittelt wird.",
            )
        configured_peer = _normalized_peer_ip(result.get("ha_peer_ip"))
        if configured_peer and configured_peer != normalized_bound_peer:
            _fail(
                "E3DC-UPD-CONFIG-005",
                "Die HA-Peer-IP änderte sich zwischen Installationserkennung und Ziel-Updater.",
                "Prüfe ha_peer_ip in der genannten Konfiguration und starte danach denselben Updatebefehl erneut.",
            )
        result["ha_peer_ip"] = normalized_bound_peer
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


def _assert_root_update_authority() -> None:
    if os.geteuid() != 0:
        _fail(
            "E3DC-UPD-PRIV-001",
            "Der Updater läuft nicht mit Root-Rechten.",
            "Starte: sudo /bin/sh ./e3dc-update-bootstrap",
        )


def _validate_inputs(
    target_root: Path,
    release_root: Path,
    install_user: str,
    tag: str,
) -> None:
    _assert_root_update_authority()
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


def _create_backup(
    target_root: Path,
    target_binding: DirectoryMetadataPrestate,
) -> Path:
    print(
        "[1/4] Sicherung und Integritätsprüfung (Anlage läuft weiter) …",
        flush=True,
    )
    try:
        from Installer.backup import REPAIR_UPDATE_BACKUP_PROFILE, backup_current_version

        def verify_source_binding(*_args: object) -> None:
            _assert_named_directory_binding(target_binding)

        verify_source_binding()
        backup = backup_current_version(
            install_path=str(target_root),
            profile=REPAIR_UPDATE_BACKUP_PROFILE,
            verified_pre_chown_callback=verify_source_binding,
            expected_install_root_identity=(
                target_binding.parent_device,
                target_binding.parent_inode,
                target_binding.device,
                target_binding.inode,
            ),
        )
        verify_source_binding()
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
    for package in SIMPLE_UPDATE_RUNTIME_APT_PACKAGES:
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


def _normalized_distribution_name(value: str) -> str:
    """Normalisiert Python-Distributionsnamen entsprechend ihrer Vergleichsform."""

    return re.sub(r"[-_.]+", "-", str(value or "").strip()).lower()


def _managed_pip_check_conflicts(
    output: str,
    packages: Iterable[str],
) -> tuple[str, ...]:
    """Filtert ``pip check`` strikt auf Konflikte verwalteter Pakete."""

    managed = {
        _normalized_distribution_name(package)
        for package in packages
        if _normalized_distribution_name(package)
    }
    conflicts: list[str] = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s+", line)
        if match and _normalized_distribution_name(match.group(1)) in managed:
            conflicts.append(line)
    return tuple(conflicts)


def _runuser_binary(*, code: str = "E3DC-UPD-DEP-003") -> Path:
    runuser = Path("/usr/sbin/runuser")
    if not runuser.is_file() or not os.access(runuser, os.X_OK):
        _fail(
            code,
            "runuser fehlt; die Python-Umgebung kann nicht sicher als Installationsbenutzer vorbereitet werden.",
            "Installiere util-linux und starte danach denselben Updatebefehl erneut.",
        )
    return runuser


def _venv_python_usable_by_install_user(
    install_user: str,
    venv: Path,
    python: Path,
) -> bool:
    """Führt den Interpreter real als Zielnutzer aus und belegt Schreibbarkeit."""

    try:
        account = pwd.getpwnam(install_user)
        metadata = venv.lstat()
    except (KeyError, OSError):
        return False
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != account.pw_uid
    ):
        return False
    runuser = _runuser_binary()
    probe = _run(
        [
            runuser,
            "-u",
            install_user,
            "--",
            python,
            "-c",
            (
                "import os,pathlib,sys,sysconfig,tempfile; "
                "expected=os.path.realpath(sys.argv[1]); "
                "actual=os.path.realpath(sys.prefix); "
                "assert actual==expected, (actual,expected); "
                "assert os.geteuid()==int(sys.argv[2]); "
                "roots=(pathlib.Path(sys.prefix),"
                'pathlib.Path(sysconfig.get_path("purelib")),'
                "pathlib.Path(sysconfig.get_path('scripts'))); "
                "[(lambda pair:(os.close(pair[0]),os.unlink(pair[1])))"
                "(tempfile.mkstemp(prefix='.e3dc-update-write-',dir=root)) for root in roots]"
            ),
            venv,
            str(account.pw_uid),
        ],
        timeout=60,
    )
    return probe.returncode == 0


def _managed_venv_package_probe(
    runuser: Path,
    install_user: str,
    python: Path,
    packages: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    return _run(
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


def _target_venv_name(target_version: str, ordinal: int = 1) -> str:
    version = re.sub(r"[^A-Za-z0-9]+", "_", str(target_version or "")).strip("_").lower()
    if not version:
        version = "target"
    base = f"venv_e3dc_release_{version[:80]}"
    return base if ordinal == 1 else f"{base}_{ordinal}"


def _target_venv_marker_payload(
    target_version: str,
    packages: tuple[str, ...],
) -> bytes:
    return (
        json.dumps(
            {
                "schema": 1,
                "target_version": str(target_version),
                "managed_packages": list(packages),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _target_venv_marker_matches(
    venv: Path,
    target_version: str,
    packages: tuple[str, ...],
    install_uid: int,
) -> bool:
    marker = venv / ".e3dc-target-venv.json"
    descriptor = -1
    try:
        metadata = marker.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != install_uid
            or metadata.st_size > 16384
        ):
            return False
        descriptor = os.open(
            marker,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        payload = b""
        while len(payload) <= 16384:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            payload += chunk
        if len(payload) > 16384:
            return False
        return payload == _target_venv_marker_payload(target_version, packages)
    except (OSError, UnicodeError, ValueError):
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _prepared_target_venv_ready(
    policy: dict,
    install_user: str,
    venv: Path,
    target_version: str,
) -> bool:
    packages = _validated_managed_venv_pip_packages(policy)
    account = pwd.getpwnam(install_user)
    python = venv / "bin/python3"
    if not _target_venv_marker_matches(
        venv,
        target_version,
        packages,
        account.pw_uid,
    ):
        return False
    if not _venv_python_usable_by_install_user(install_user, venv, python):
        return False
    runuser = _runuser_binary()
    package_probe = _managed_venv_package_probe(
        runuser,
        install_user,
        python,
        packages,
    )
    if package_probe.returncode != 0 or package_probe.stdout.strip():
        return False
    pip_check = _run(
        [runuser, "-u", install_user, "--", python, "-m", "pip", "check"],
        timeout=120,
    )
    return not _managed_pip_check_conflicts(
        "\n".join((pip_check.stdout, pip_check.stderr)),
        packages,
    )


def _write_target_venv_marker(
    runuser: Path,
    install_user: str,
    venv: Path,
    payload: bytes,
) -> bool:
    marker = venv / ".e3dc-target-venv.json"
    written = _run(
        [
            runuser,
            "-u",
            install_user,
            "--",
            "/usr/bin/python3",
            "-c",
            (
                "import os,sys; path=sys.argv[1]; data=sys.argv[2].encode('utf-8'); "
                "flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0); "
                "fd=os.open(path,flags,0o600); "
                "handle=os.fdopen(fd,'wb'); handle.write(data); handle.flush(); "
                "os.fsync(handle.fileno()); handle.close()"
            ),
            marker,
            payload.decode("utf-8"),
        ],
        timeout=30,
    )
    return written.returncode == 0


def _remove_created_target_venv_best_effort(
    runuser: Path,
    install_user: str,
    home: Path,
    venv: Path,
) -> bool:
    """Entfernt nur den in diesem Lauf neu angelegten, nutzereigenen Kandidaten."""

    try:
        removed = _run(
            [
                runuser,
                "-u",
                install_user,
                "--",
                "/usr/bin/python3",
                "-c",
                (
                    "import os,shutil,stat,sys; home=os.path.abspath(sys.argv[1]); "
                    "path=os.path.abspath(sys.argv[2]); "
                    "assert os.path.dirname(path)==home; st=os.lstat(path); "
                    "assert stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode); "
                    "assert st.st_uid==os.geteuid(); shutil.rmtree(path)"
                ),
                home,
                venv,
            ],
            timeout=120,
        )
    except Exception:
        return False
    return removed.returncode == 0 and not os.path.lexists(venv)


def _repair_managed_venv_pip_packages(
    policy: dict,
    install_user: str,
    python: Path,
    *,
    venv_preexisted: bool,
) -> None:
    packages = _validated_managed_venv_pip_packages(policy)
    runuser = _runuser_binary(code="E3DC-UPD-DEP-004")
    probe = _managed_venv_package_probe(
        runuser,
        install_user,
        python,
        packages,
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
    pip_command_prefix = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--quiet",
        "--prefer-binary",
    ]
    install_targets = missing if venv_preexisted else packages
    if install_targets:
        pip_command = [
            *pip_command_prefix,
            "--",
            *install_targets,
        ]
        installed = _run(
            [runuser, "-u", install_user, "--", *pip_command],
            timeout=600,
        )
        if installed.returncode != 0:
            detail = (installed.stderr or installed.stdout or "unbekannter pip-Fehler").strip()
            package_names = ", ".join(install_targets)
            repair_command = shlex.join(["sudo", "-u", install_user, *pip_command])
            _fail(
                "E3DC-UPD-DEP-004",
                f"Die Python-Pakete im venv konnten nicht repariert werden ({package_names}): {detail}",
                f"Behebe die angezeigte pip-/Netzwerkursache und prüfe sie mit: {repair_command}",
            )

    check_command = [str(python), "-m", "pip", "check"]
    checked = _run(
        [runuser, "-u", install_user, "--", *check_command],
        timeout=120,
    )
    managed_conflicts = _managed_pip_check_conflicts(
        "\n".join((checked.stdout, checked.stderr)),
        packages,
    )
    if not managed_conflicts:
        return

    repair_command = [
        *pip_command_prefix,
        "--upgrade",
        "--",
        *packages,
    ]
    repaired = _run(
        [runuser, "-u", install_user, "--", *repair_command],
        timeout=600,
    )
    checked_again = _run(
        [runuser, "-u", install_user, "--", *check_command],
        timeout=120,
    )
    remaining_managed_conflicts = _managed_pip_check_conflicts(
        "\n".join((checked_again.stdout, checked_again.stderr)),
        packages,
    )
    if repaired.returncode != 0 or remaining_managed_conflicts:
        detail = (
            "\n".join(remaining_managed_conflicts)
            or repaired.stderr
            or repaired.stdout
            or "unbekannter pip-check-Fehler"
        ).strip()
        _fail(
            "E3DC-UPD-DEP-004",
            f"Die Python-Umgebung enthält weiterhin unvollständige Abhängigkeiten: {detail}",
            f"Prüfe die Umgebung mit: sudo -u {install_user} {python} -m pip check; starte danach denselben Updatebefehl erneut.",
        )


def _ensure_minimal_venv(
    policy: dict,
    install_user: str,
    config: dict,
    target_version: str,
) -> Path:
    """Bereitet den Zielstand in einem eigenen venv vor; das Alt-venv bleibt unverändert."""

    account = pwd.getpwnam(install_user)
    home = Path(account.pw_dir)
    if not home.is_absolute() or not home.is_dir():
        _fail(
            "E3DC-UPD-DEP-003",
            f"Das Home-Verzeichnis des Installationsbenutzers fehlt: {home}",
            f"Lege {home} für {install_user} an und starte danach denselben Updatebefehl erneut.",
        )
    runuser = _runuser_binary()
    packages = _validated_managed_venv_pip_packages(policy)
    selected_name = ""
    selected_venv: Path | None = None
    for ordinal in range(1, 65):
        candidate_name = _target_venv_name(target_version, ordinal)
        candidate = home / candidate_name
        if os.path.lexists(candidate):
            if _prepared_target_venv_ready(
                policy,
                install_user,
                candidate,
                target_version,
            ):
                config["venv_name"] = candidate_name
                config["venv_path"] = str(candidate)
                return candidate / "bin/python3"
            continue
        selected_name = candidate_name
        selected_venv = candidate
        break
    if selected_venv is None:
        _fail(
            "E3DC-UPD-DEP-003",
            "Es ist kein freier, transaktionaler Zielplatz für die Python-Umgebung verfügbar.",
            f"Entferne nicht mehr verwendete venv_e3dc_release_*-Ordner in {home} und starte danach denselben Updatebefehl erneut.",
        )

    python = selected_venv / "bin/python3"
    created = _run(
        [
            runuser,
            "-u",
            install_user,
            "--",
            "/usr/bin/python3",
            "-m",
            "venv",
            "--system-site-packages",
            selected_venv,
        ],
        timeout=180,
    )
    try:
        if created.returncode != 0 or not _venv_python_usable_by_install_user(
            install_user,
            selected_venv,
            python,
        ):
            detail = (created.stderr or created.stdout or "keine Detailausgabe").strip()
            _fail(
                "E3DC-UPD-DEP-003",
                f"Die neue Python-Umgebung konnte für {install_user} nicht vorbereitet werden: {detail}",
                f"Prüfe die Schreibrechte von {home} und den freien Speicherplatz; starte danach denselben Updatebefehl erneut.",
            )
        _repair_managed_venv_pip_packages(
            policy,
            install_user,
            python,
            venv_preexisted=False,
        )
        marker_payload = _target_venv_marker_payload(target_version, packages)
        if not _write_target_venv_marker(
            runuser,
            install_user,
            selected_venv,
            marker_payload,
        ) or not _prepared_target_venv_ready(
            policy,
            install_user,
            selected_venv,
            target_version,
        ):
            _fail(
                "E3DC-UPD-DEP-004",
                "Die neue Python-Umgebung blieb nach Installation und pip check unvollständig.",
                f"Prüfe freien Speicherplatz und Python/pip für {install_user}; starte danach denselben Updatebefehl erneut.",
            )
    except Exception:
        _remove_created_target_venv_best_effort(
            runuser,
            install_user,
            home,
            selected_venv,
        )
        raise
    config["venv_name"] = selected_name
    config["venv_path"] = str(selected_venv)
    return python


def _repair_packages(
    policy: dict,
    install_user: str,
    config: dict,
    selected_catalog_units: Iterable[str] = (),
    *,
    target_version: str = "",
) -> Path:
    if not target_version:
        target_version = (RELEASE_ROOT / "VERSION").read_text(encoding="utf-8").strip()
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
    python = _ensure_minimal_venv(
        policy,
        install_user,
        config,
        target_version,
    )
    if not python.is_file() or not os.access(python, os.X_OK):
        _fail(
            "E3DC-UPD-DEP-003",
            "Der vorbereitete Python-Interpreter fehlt oder ist nicht ausführbar.",
            "Prüfe den freien Speicherplatz und starte danach denselben Updatebefehl erneut.",
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


def _prepare_full_update_recovery_entry(
    target_root: Path,
    lock_descriptor: int,
) -> None:
    """Nutzt vor jeder Simple-Transaktion den vollständigen Recovery-Vertrag."""

    try:
        from Installer import update as full_update

        with _bound_update_lock_environment(lock_descriptor):
            full_update.prepare_recovery_namespace_for_bound_updater(
                str(target_root),
            )
    except Exception as exc:
        raw_detail = str(getattr(exc, "detail", "") or exc).strip()
        raw_detail = raw_detail or exc.__class__.__name__
        code = str(getattr(exc, "code", "") or "").strip()
        embedded = re.match(
            r"^\[(E3DC-UPD-[A-Z0-9-]{1,96})\]\s*(.*)$",
            raw_detail,
            flags=re.DOTALL,
        )
        if embedded is not None:
            if not code:
                code = embedded.group(1)
            raw_detail = embedded.group(2).strip()
        if re.fullmatch(r"E3DC-UPD-[A-Z0-9-]{1,96}", code) is None:
            code = "E3DC-UPD-RECOVERY-001"
        detail = raw_detail.partition("Lösung:")[0].strip() or raw_detail
        structured_solution = str(getattr(exc, "solution", "") or "").strip()
        solution = structured_solution or (
            "Lösche keine Recovery-Datei, keinen systemd-Drop-in und kein Backup "
            "manuell. Sichere die vollständige Ausgabe und prüfe den gebundenen "
            "Systemjob mit sudo journalctl -u e3dc-web-update.service "
            "--no-pager -n 200; behebe ausschließlich die dort genannte Ursache."
        )
        structured_state = str(
            getattr(exc, "system_state", "") or ""
        ).strip()
        _fail(
            code,
            "Eine offene Update-Transaktion konnte vor dem neuen "
            f"Releasewechsel nicht sicher abgeschlossen werden. Ursache: {detail}",
            solution,
            system_state=structured_state or (
                "Der aktuelle Simple-Releasewechsel hat noch kein neues Vollbackup "
                "und keinen neuen Dateiaustausch begonnen. Der Zustand der vorherigen "
                "Transaktion bleibt fail-closed und muss anhand ihres Recovery-"
                "Vertrags bestimmt werden."
            ),
        )


def _rebind_emergency_veto_after_recovery(was_active: bool) -> bool:
    """Bindet den Incident erneut und bestätigt den Writer nach Recovery."""

    active_now = _prepare_active_emergency_veto_before_update()
    if was_active and not active_now:
        _fail(
            "E3DC-UPD-EMERGENCY-004",
            "Der vor dem Recovery-Lauf gebundene Incident-Latch war bei der "
            "anschließenden Writer-Prüfung nicht mehr vorhanden.",
            "Starte keinen manuellen Speicherbefehl. Prüfe den Incident-Latch, "
            "seinen systemd-Generator und e3dc-storage-manager.service; der "
            "Releasewechsel bleibt bis zur eindeutigen Klärung gesperrt.",
            system_state=(
                "Die vorherige Update-Recovery wurde betreten, aber der zuvor "
                "aktive Notfallvertrag kann nicht mehr eindeutig gebunden "
                "werden. Der neue Dateiaustausch wurde nicht begonnen."
            ),
        )
    # ``_prepare_active_emergency_veto_before_update`` kehrt bei aktivem
    # Incident ausschließlich mit nachweislich inaktivem Storage-Writer zurück.
    # Dadurch wird auch ein erst während der Recovery erschienener Latch vor
    # jeder neuen Target-/Config-Bindung wirksam.
    return active_now


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
    print(
        "[2/4] Kurze Anlagenunterbrechung beginnt: Dienste werden für den "
        "Dateiaustausch kontrolliert gestoppt …",
        flush=True,
    )
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
    """Deaktiviert belegte Altunits erst nach bestätigtem Ersatz."""

    warnings: list[str] = []
    for unit in prestate.retired_unknown_units:
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
            f"Abgelöster alter E3DC-Dienst {unit} wurde nach dem bestätigten "
            "Wechsel gestoppt und deaktiviert."
        )
    return warnings


def _restart_target_bound_observers_after_success(
    prestate: ServicePrestate,
) -> list[str]:
    """Startet zuvor aktive Beobachter wieder, ohne ihre Freigabe zu verändern.

    Der neue Release ist zu diesem Zeitpunkt bereits bestätigt. Ein fehlender
    oder nicht startbarer zuvor aktiver Zusatzdienst löst deshalb keinen
    Rückfall aus, darf aber auch nicht als vollständig erfolgreicher
    Updateabschluss erscheinen.
    """

    warnings: list[str] = []
    active_before = set(prestate.active)
    masked = set(prestate.masked)
    for unit in prestate.target_bound_observers:
        if unit not in active_before or unit in masked:
            continue
        if not _service_exists(unit):
            _fail(
                "E3DC-UPD-SERVICE-OBSERVER-001",
                f"Der zuvor aktive Zusatzdienst {unit} ist nach dem bestätigten Releasewechsel nicht mehr vorhanden.",
                f"Prüfe die Unit mit sudo systemctl status {unit} --no-pager und stelle sie wieder her oder deaktiviere sie bewusst; der neue E3DC-Control-Release bleibt installiert.",
            )
        started = _run(["/usr/bin/systemctl", "start", unit], timeout=60)
        if started.returncode != 0 or not _service_active(unit):
            detail = (started.stderr or started.stdout or "keine Detailausgabe").strip()
            _fail(
                "E3DC-UPD-SERVICE-OBSERVER-002",
                f"Der zuvor aktive Zusatzdienst {unit} konnte nach dem bestätigten Releasewechsel nicht wieder gestartet werden: {detail}",
                f"Prüfe sudo journalctl -u {unit} --no-pager -n 120 und starte den Dienst anschließend mit sudo systemctl start {unit}; der neue E3DC-Control-Release bleibt installiert.",
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


def _create_stopped_data_backup(
    target_root: Path,
    backup_path: Path,
    target_binding: DirectoryMetadataPrestate,
) -> tuple[Path, object]:
    """Sichert nach bestätigtem Stopp nur noch die zuletzt veränderbaren Daten."""

    from Installer.backup import create_quiesced_overlay
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
        expected_install_root_identity=(
            target_binding.parent_device,
            target_binding.parent_inode,
            target_binding.device,
            target_binding.inode,
        ),
    )
    return Path(created), guard


def _remove_stopped_data_backup(overlay_path: Path, guard: object) -> None:
    """Entfernt nur das terminal bestätigte, vollständig guardgebundene Overlay."""

    from Installer.backup_retention import delete_bound_quiesced_overlay

    delete_bound_quiesced_overlay(overlay_path, guard=guard)  # type: ignore[arg-type]
    if os.path.lexists(str(overlay_path)):
        raise RuntimeError(
            "Ruhende Daten-Nachsicherung blieb nach bestätigtem Cleanup vorhanden"
        )


def _retry_backup_retention_after_confirmed_start(
    target_root: Path,
    backup_path: Path,
    quiesced_backup: Path | None,
    lock_descriptor: int,
) -> list[str]:
    """Rotiert nach dem Neustart best-effort und schützt den aktuellen Rückweg."""

    warnings: list[str] = []
    preserve_paths = [backup_path]
    if quiesced_backup is not None:
        preserve_paths.append(quiesced_backup)
    try:
        from Installer.backup_retention import prune_install_backups

        with _bound_update_lock_environment(lock_descriptor):
            retention = prune_install_backups(
                target_root,
                backup_root=backup_path.parent,
                preserve_paths=preserve_paths,
            )
        if not isinstance(retention, dict):
            raise RuntimeError("Backup-Retention lieferte keinen Ergebnisvertrag")
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        return [
            "Der bestätigte neue Release bleibt aktiv; der nachgelagerte "
            "Backup-Limit-Lauf konnte nicht ausgeführt werden: {}. Das aktuelle "
            "Vollbackup und eine gegebenenfalls verbliebene ruhende "
            "Daten-Nachsicherung bleiben erhalten.".format(detail)
        ]

    if retention.get("blocked"):
        warnings.append(
            "Der bestätigte neue Release bleibt aktiv; der nachgelagerte "
            "Backup-Limit-Lauf war noch blockiert: {}.".format(
                retention.get("blocker")
                or "Updateabschluss oder Recovery-Zustand ist noch offen"
            )
        )
    if retention.get("success") is not True:
        warnings.append(
            "Der bestätigte neue Release bleibt aktiv; der nachgelagerte "
            "Backup-Limit-Lauf meldete einen Bereinigungsfehler. Das aktuelle "
            "Vollbackup bleibt geschützt."
        )
    if retention.get("limit_satisfied") is True and not warnings:
        print(
            "[OK] Backup-Limit nach bestätigtem Dienststart angewendet.",
            flush=True,
        )
        return warnings

    update_result = retention.get("update_backups")
    web_result = retention.get("web_installer_backups")
    quiesced_result = retention.get("quiesced_overlays")

    def verified_count_within_limit(payload: object) -> tuple[bool, int, int]:
        if not isinstance(payload, dict):
            return False, -1, -1
        count = payload.get("verified_count_after")
        keep = payload.get("keep_count")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not isinstance(keep, int)
            or isinstance(keep, bool)
            or count < 0
            or keep < 1
        ):
            return False, -1, -1
        return count <= keep, count, keep

    system_ok, system_count, system_limit = verified_count_within_limit(
        update_result
    )
    web_ok, web_count, web_limit = verified_count_within_limit(web_result)
    quiesced_clean = bool(
        isinstance(quiesced_result, dict)
        and quiesced_result.get("success") is True
        and not quiesced_result.get("skipped")
    )
    unclassified_entries = [
        entry
        for payload in (update_result, web_result)
        if isinstance(payload, dict)
        for entry in (payload.get("unclassified") or [])
        if isinstance(entry, dict)
    ]
    unclassified_count = len(unclassified_entries)
    unverified_count = sum(
        1
        for entry in unclassified_entries
        if str(entry.get("reason") or "") == "nicht verifiziert"
    )
    if (
        not warnings
        and system_ok
        and web_ok
        and quiesced_clean
        and unclassified_count
    ):
        old_inventory_text = (
            "1 nicht sicher klassifizierbarer Altbestand blieb unverändert "
            "außerhalb der Rotation; dieser Bestand zählt nicht als "
            "verifiziertes System- oder Web-Backup."
            if unclassified_count == 1
            else (
                f"{unclassified_count} nicht sicher klassifizierbare "
                "Altbestände blieben unverändert außerhalb der Rotation; "
                "diese Bestände zählen nicht als verifizierte System- oder "
                "Web-Backups."
            )
        )
        unverified_text = ""
        if unverified_count:
            unverified_text = (
                " Davon konnte 1 sicherungsähnlicher Bestand nicht "
                "verifiziert werden."
                if unverified_count == 1
                else (
                    f" Davon konnten {unverified_count} sicherungsähnliche "
                    "Bestände nicht verifiziert werden."
                )
            )
        print(
            "[INFO] Die verifizierten Backup-Grenzen sind eingehalten "
            f"(System {system_count}/{system_limit}, Web {web_count}/{web_limit}). "
            f"{old_inventory_text}"
            f"{unverified_text}",
            flush=True,
        )
        return warnings

    warnings.append(
        "Die Grenze von maximal drei verifizierten System-Backup-Familien "
        "und drei Web-Sicherungen ist noch offen; geschützte oder nicht "
        "sicher klassifizierbare Sicherungen wurden nicht gelöscht."
    )
    return warnings


def _fd_mount_id(descriptor: int) -> str:
    """Bindet einen offenen Deskriptor zusätzlich zur Geräte-ID an seinen Mount."""

    try:
        payload = Path(f"/proc/self/fdinfo/{descriptor}").read_text(
            encoding="ascii",
            errors="strict",
        )
    except OSError as exc:
        raise RuntimeError(
            "Die Mount-Bindung für den sicheren Dateiaustausch ist nicht verfügbar"
        ) from exc
    for line in payload.splitlines():
        if line.startswith("mnt_id:"):
            value = line.partition(":")[2].strip()
            if value.isdigit():
                return value
    raise RuntimeError("Die Mount-ID des Zielpfads konnte nicht gebunden werden")


def _projection_failure(path: Path, detail: str) -> None:
    _fail(
        "E3DC-UPD-PROJECTION-001",
        f"Der Zielpfad kann nicht sicher durch den neuen Releasebaum ersetzt werden: {path} ({detail}).",
        f"Prüfe den Pfad mit sudo namei -l {shlex.quote(str(path))} und sudo findmnt -T {shlex.quote(str(path))}; "
        "entferne dort Symlink, Spezialdatei oder verschachtelten Mount und starte danach denselben Ein-Datei-Updater erneut.",
    )


def _open_bound_projection_directory(
    parent_fd: int,
    name: str,
    display_path: Path,
    *,
    root_device: int,
    root_mount_id: str,
    uid: int,
    gid: int,
    mode: int,
) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory:
        raise RuntimeError("Sichere fd-relative Releaseprojektion ist nicht verfügbar")
    flags = os.O_RDONLY | nofollow | directory | cloexec
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        _projection_failure(display_path, str(exc))
        raise AssertionError("unreachable")
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != root_device
            or _fd_mount_id(descriptor) != root_mount_id
        ):
            _projection_failure(display_path, "anderes Dateisystem oder verschachtelter Mount")
        # Solange in diesem Verzeichnis noch benannte Tempdateien publiziert
        # werden, darf der Installationsnutzer es nicht parallel verändern.
        # Die endgültigen Metadaten setzt der Aufrufer erst nach dem fsync des
        # vollständigen Unterbaums.
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o700)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _project_regular_file(
    source: Path,
    parent_fd: int,
    name: str,
    display_path: Path,
    *,
    root_device: int,
    root_mount_id: str,
    uid: int,
    gid: int,
    mode: int | None = None,
    executable_from_shebang: bool = False,
) -> None:
    """Publiziert eine neue Inode; Ziel-Hardlinks und Symlink-Referenten bleiben unberührt."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow:
        raise RuntimeError("Sichere fd-relative Releaseprojektion ist nicht verfügbar")
    source_fd = os.open(source, os.O_RDONLY | nofollow | cloexec)
    temporary_name = f".e3dc-release-{os.getpid()}-{os.urandom(12).hex()}"
    temporary_fd = -1
    replaced = False
    try:
        source_before = os.stat(source, follow_symlinks=False)
        source_opened = os.fstat(source_fd)
        if (
            not stat.S_ISREG(source_opened.st_mode)
            or (source_before.st_dev, source_before.st_ino)
            != (source_opened.st_dev, source_opened.st_ino)
        ):
            raise RuntimeError(f"Releasequelle ist keine gebundene reguläre Datei: {source}")

        try:
            target_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            target_metadata = None
        if target_metadata is not None:
            if stat.S_ISDIR(target_metadata.st_mode):
                _projection_failure(display_path, "Verzeichnis steht anstelle einer Release-Datei")
            if stat.S_ISREG(target_metadata.st_mode):
                target_fd = os.open(name, os.O_RDONLY | nofollow | cloexec, dir_fd=parent_fd)
                try:
                    opened_target = os.fstat(target_fd)
                    if (
                        opened_target.st_dev != root_device
                        or _fd_mount_id(target_fd) != root_mount_id
                    ):
                        _projection_failure(
                            display_path,
                            "Datei liegt auf einem verschachtelten Mount",
                        )
                finally:
                    os.close(target_fd)
            elif not stat.S_ISLNK(target_metadata.st_mode):
                _projection_failure(display_path, "Spezialdatei steht anstelle einer Release-Datei")

        if mode is not None and executable_from_shebang:
            raise RuntimeError(
                "Releaseprojektion besitzt zwei widersprüchliche Dateimodusregeln"
            )
        if executable_from_shebang:
            header = os.pread(source_fd, 2, 0)
            final_mode = 0o755 if header == b"#!" else 0o644
        else:
            final_mode = (
                stat.S_IMODE(source_opened.st_mode) & 0o777
                if mode is None
                else mode
            )

        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | cloexec,
            0o600,
            dir_fd=parent_fd,
        )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                offset += os.write(temporary_fd, chunk[offset:])
        os.fchown(temporary_fd, uid, gid)
        os.fchmod(temporary_fd, final_mode)
        os.fsync(temporary_fd)
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        published = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 1
            or published.st_uid != uid
            or published.st_gid != gid
            or stat.S_IMODE(published.st_mode) != final_mode
        ):
            raise RuntimeError(
                f"Projizierte Release-Datei besitzt falsche Metadaten: {display_path}"
            )
        replaced = True
    finally:
        os.close(source_fd)
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if not replaced:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _project_release_symlink(
    source_root: Path,
    source: Path,
    parent_fd: int,
    name: str,
    display_path: Path,
    *,
    uid: int,
    gid: int,
) -> None:
    """Projiziert ausschließlich relative, innerhalb des Releasebaums bleibende Links."""

    link_target = os.readlink(source)
    if (
        not link_target
        or os.path.isabs(link_target)
        or any(character in link_target for character in ("\x00", "\r", "\n"))
    ):
        raise RuntimeError(f"Unsicheres Symlinkziel im Ziel-Release: {source}")
    lexical_target = Path(os.path.abspath(source.parent / link_target))
    try:
        lexical_target.relative_to(source_root)
    except ValueError as exc:
        raise RuntimeError(
            f"Symlink im Ziel-Release verlässt den Releasebaum: {source} -> {link_target}"
        ) from exc

    try:
        target_metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        target_metadata = None
    if target_metadata is not None:
        if stat.S_ISDIR(target_metadata.st_mode):
            _projection_failure(
                display_path,
                "Verzeichnis steht anstelle eines Release-Symlinks",
            )
        if not (
            stat.S_ISREG(target_metadata.st_mode)
            or stat.S_ISLNK(target_metadata.st_mode)
        ):
            _projection_failure(
                display_path,
                "Spezialdatei steht anstelle eines Release-Symlinks",
            )

    temporary_name = f".e3dc-release-link-{os.getpid()}-{os.urandom(12).hex()}"
    published = False
    try:
        os.symlink(link_target, temporary_name, dir_fd=parent_fd)
        os.chown(
            temporary_name,
            uid,
            gid,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        published = True
    finally:
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _project_release_tree(
    source_root: Path,
    target_root: Path,
    *,
    uid: int,
    gid: int,
    root_mode: int,
    directory_mode: int | None = None,
    file_mode: int | None = None,
    executable_from_shebang: bool = False,
    excluded_top_level: frozenset[str] = frozenset(),
    expected_target_binding: DirectoryMetadataPrestate | None = None,
) -> DirectoryMetadataPrestate:
    """Projiziert Release-Dateien fd-relativ, nofollow und mountgebunden."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory:
        raise RuntimeError("Sichere fd-relative Releaseprojektion ist nicht verfügbar")
    if file_mode is not None and executable_from_shebang:
        raise RuntimeError(
            "Releaseprojektion besitzt zwei widersprüchliche Dateimodusregeln"
        )
    source_root = source_root.resolve(strict=True)
    target_root = Path(os.path.normpath(os.path.abspath(str(target_root))))
    if expected_target_binding is None:
        target_root.mkdir(parents=True, exist_ok=True)
    elif str(target_root) != expected_target_binding.path:
        raise RuntimeError("Releaseprojektion erhielt einen fremden Zielpfadvertrag")
    parent_fd, root_fd, parent_metadata, root_metadata = _open_bound_named_directory(
        target_root
    )
    original = _directory_prestate(target_root, root_metadata, parent_metadata)
    if expected_target_binding is not None and original != expected_target_binding:
        os.close(root_fd)
        os.close(parent_fd)
        raise RuntimeError(
            f"Der Produktstamm driftete vor der Releaseprojektion: {target_root}"
        )
    try:
        root_device = root_metadata.st_dev
        root_mount_id = _fd_mount_id(root_fd)
        os.fchown(root_fd, 0, 0)
        os.fchmod(root_fd, 0o700)

        def project_directory(source_dir: Path, destination_fd: int, relative: tuple[str, ...]) -> None:
            for entry in sorted(os.scandir(source_dir), key=lambda item: item.name):
                if not relative and entry.name in excluded_top_level:
                    continue
                source_path = source_dir / entry.name
                metadata = entry.stat(follow_symlinks=False)
                display = target_root.joinpath(*relative, entry.name)
                if stat.S_ISDIR(metadata.st_mode):
                    mode = directory_mode
                    if mode is None:
                        mode = stat.S_IMODE(metadata.st_mode) & 0o777
                    child_fd = _open_bound_projection_directory(
                        destination_fd,
                        entry.name,
                        display,
                        root_device=root_device,
                        root_mount_id=root_mount_id,
                        uid=uid,
                        gid=gid,
                        mode=mode,
                    )
                    try:
                        project_directory(source_path, child_fd, (*relative, entry.name))
                        os.fsync(child_fd)
                        os.fchown(child_fd, uid, gid)
                        os.fchmod(child_fd, mode)
                        os.fsync(child_fd)
                        secured = os.fstat(child_fd)
                        if (
                            secured.st_uid != uid
                            or secured.st_gid != gid
                            or stat.S_IMODE(secured.st_mode) != mode
                        ):
                            raise RuntimeError(
                                f"Projiziertes Release-Verzeichnis besitzt falsche Metadaten: {display}"
                            )
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(metadata.st_mode):
                    _project_regular_file(
                        source_path,
                        destination_fd,
                        entry.name,
                        display,
                        root_device=root_device,
                        root_mount_id=root_mount_id,
                        uid=uid,
                        gid=gid,
                        mode=file_mode,
                        executable_from_shebang=executable_from_shebang,
                    )
                elif stat.S_ISLNK(metadata.st_mode):
                    _project_release_symlink(
                        source_root,
                        source_path,
                        destination_fd,
                        entry.name,
                        display,
                        uid=uid,
                        gid=gid,
                    )
                else:
                    raise RuntimeError(
                        f"Ziel-Release enthält einen nicht unterstützten Spezialpfad: {source_path}"
                    )

        project_directory(source_root, root_fd, ())
        os.fsync(root_fd)
        os.fchown(root_fd, uid, gid)
        os.fchmod(root_fd, root_mode)
        os.fsync(root_fd)
        secured_root = os.fstat(root_fd)
        if (
            secured_root.st_uid != uid
            or secured_root.st_gid != gid
            or stat.S_IMODE(secured_root.st_mode) != root_mode
        ):
            raise RuntimeError(
                f"Projizierter Release-Stamm besitzt falsche Metadaten: {target_root}"
            )
        rebound = _read_bound_directory_prestate(target_root)
        if (
            rebound.parent_device != original.parent_device
            or rebound.parent_inode != original.parent_inode
            or rebound.device != secured_root.st_dev
            or rebound.inode != secured_root.st_ino
            or rebound.uid != uid
            or rebound.gid != gid
            or rebound.mode != root_mode
        ):
            raise RuntimeError(
                f"Der benannte Release-Stamm driftete während der Projektion: {target_root}"
            )
        return rebound
    except Exception as original_error:
        rollback_error: Exception | None = None
        try:
            os.fchown(root_fd, root_metadata.st_uid, root_metadata.st_gid)
            os.fchmod(root_fd, stat.S_IMODE(root_metadata.st_mode))
            os.fsync(root_fd)
            restored = os.fstat(root_fd)
            if (
                restored.st_uid != root_metadata.st_uid
                or restored.st_gid != root_metadata.st_gid
                or stat.S_IMODE(restored.st_mode)
                != stat.S_IMODE(root_metadata.st_mode)
            ):
                raise RuntimeError("Metadaten des gebundenen Zielstamms blieben abweichend")
        except Exception as exc:
            rollback_error = exc
        if rollback_error is not None:
            raise RuntimeError(
                "Die Releaseprojektion scheiterte und die Metadaten des gebundenen "
                f"Zielstamms konnten nicht sicher zurückgestellt werden: {target_root} "
                f"({rollback_error})"
            ) from original_error
        raise
    finally:
        os.close(root_fd)
        os.close(parent_fd)


def _replace_product_tree(
    target_root: Path,
    release_root: Path,
    install_user: str,
    expected_target_binding: DirectoryMetadataPrestate,
) -> DirectoryMetadataPrestate:
    """Projiziert den Zielbaum; vorhandene Git-Metadaten bleiben unbeachtet."""

    account = pwd.getpwnam(install_user)
    web_gid = grp.getgrnam("www-data").gr_gid
    return _project_release_tree(
        release_root,
        target_root,
        uid=account.pw_uid,
        gid=web_gid,
        root_mode=0o755,
        directory_mode=0o755,
        executable_from_shebang=True,
        excluded_top_level=frozenset({".git"}),
        expected_target_binding=expected_target_binding,
    )


def _open_absolute_directory_chain(path: Path) -> int:
    """Öffnet einen absoluten Verzeichnispfad komponentenweise ohne Symlinks."""

    normalized = Path(os.path.normpath(os.path.abspath(str(path))))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory or not normalized.is_absolute():
        raise RuntimeError("Sichere Verzeichnisbindung ist nicht verfügbar")
    descriptor = os.open(os.sep, flags)
    try:
        for component in normalized.parts[1:]:
            named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode):
                raise RuntimeError(
                    f"Pfadkomponente ist kein echtes Verzeichnis: {normalized}"
                )
            child = os.open(component, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (named.st_dev, named.st_ino)
            ):
                os.close(child)
                raise RuntimeError(
                    f"Pfadkomponente driftete beim Öffnen: {normalized}"
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_bound_named_directory(
    path: Path,
) -> tuple[int, int, os.stat_result, os.stat_result]:
    """Bindet Elternpfad und benanntes Verzeichnis ohne Symlinkauflösung."""

    normalized = Path(os.path.normpath(os.path.abspath(str(path))))
    if normalized == Path(os.sep):
        raise RuntimeError("Der Dateisystemstamm ist kein zulässiger Produktpfad")
    parent = _open_absolute_directory_chain(normalized.parent)
    descriptor = -1
    try:
        parent_metadata = os.fstat(parent)
        named = os.stat(normalized.name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(
            normalized.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(named.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise RuntimeError(f"Verzeichnis driftete beim Öffnen: {normalized}")
        return parent, descriptor, parent_metadata, opened
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)
        raise


def _directory_prestate(
    path: Path,
    metadata: os.stat_result,
    parent_metadata: os.stat_result,
) -> DirectoryMetadataPrestate:
    return DirectoryMetadataPrestate(
        path=str(path),
        parent_device=int(parent_metadata.st_dev),
        parent_inode=int(parent_metadata.st_ino),
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        uid=int(metadata.st_uid),
        gid=int(metadata.st_gid),
        mode=int(stat.S_IMODE(metadata.st_mode)),
    )


def _read_bound_directory_prestate(path: Path) -> DirectoryMetadataPrestate:
    normalized = Path(os.path.normpath(os.path.abspath(str(path))))
    parent, descriptor, parent_metadata, opened = _open_bound_named_directory(
        normalized
    )
    try:
        return _directory_prestate(normalized, opened, parent_metadata)
    finally:
        os.close(descriptor)
        os.close(parent)


def _assert_named_directory_binding(expected: DirectoryMetadataPrestate) -> None:
    """Bestätigt Namen, Elternpfad, Inode und Metadaten eines Verzeichnisses."""

    current = _read_bound_directory_prestate(Path(expected.path))
    if current != expected:
        raise RuntimeError(
            "Der gebundene Installationspfad änderte sich während des Updates: "
            f"{expected.path}"
        )


def _assert_named_directory_identity(expected: DirectoryMetadataPrestate) -> None:
    """Bestätigt Elternpfad und Inode, auch wenn Restore Metadaten ändert."""

    current = _read_bound_directory_prestate(Path(expected.path))
    if (
        current.parent_device != expected.parent_device
        or current.parent_inode != expected.parent_inode
        or current.device != expected.device
        or current.inode != expected.inode
    ):
        raise RuntimeError(
            "Der gebundene Installationspfad wurde durch einen anderen Baum ersetzt: "
            f"{expected.path}"
        )


def _web_account_can_traverse_bound_directory(
    expected: DirectoryMetadataPrestate,
) -> bool:
    """Prüft x-only-Zugriff als www-data auf exakt dem gebundenen Inode."""

    runuser = Path("/usr/sbin/runuser")
    test_binary = Path("/usr/bin/test")
    if not runuser.is_file() or not os.access(runuser, os.X_OK):
        raise RuntimeError("runuser fehlt für die gebundene Webzugriffsprüfung")
    if not test_binary.is_file() or not os.access(test_binary, os.X_OK):
        raise RuntimeError("/usr/bin/test fehlt für die gebundene Webzugriffsprüfung")

    path = Path(expected.path)
    parent, descriptor, parent_metadata, opened = _open_bound_named_directory(path)
    try:
        current = _directory_prestate(path, opened, parent_metadata)
        if current != expected:
            raise RuntimeError(
                f"Installationspfad-Vorfahre driftete vor der Webzugriffsprüfung: {path}"
            )
        try:
            result = subprocess.run(
                [
                    os.fspath(runuser),
                    "-u",
                    "www-data",
                    "--",
                    "/usr/bin/env",
                    "-i",
                    "PATH=/usr/bin:/bin",
                    os.fspath(test_binary),
                    "-x",
                    f"/proc/self/fd/{descriptor}",
                ],
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=20,
                pass_fds=(descriptor,),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                f"Webzugriff auf Installationspfad-Vorfahre konnte nicht geprüft werden: {path}"
            ) from exc
        rebound = _read_bound_directory_prestate(path)
        if rebound != expected or _directory_prestate(path, os.fstat(descriptor), parent_metadata) != expected:
            raise RuntimeError(
                f"Installationspfad-Vorfahre driftete während der Webzugriffsprüfung: {path}"
            )
        return result.returncode == 0
    finally:
        os.close(descriptor)
        os.close(parent)


def _bound_directory_has_extended_acl(
    expected: DirectoryMetadataPrestate,
) -> bool:
    """Erkennt ACLs am exakt gebundenen Vorfahren vor jeder chmod-Mutation."""

    path = Path(expected.path)
    parent, descriptor, parent_metadata, opened = _open_bound_named_directory(path)
    try:
        if _directory_prestate(path, opened, parent_metadata) != expected:
            raise RuntimeError(
                f"Installationspfad-Vorfahre driftete vor der ACL-Prüfung: {path}"
            )
        try:
            names = set(os.listxattr(descriptor))
        except OSError as exc:
            if exc.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
                names = set()
            else:
                raise RuntimeError(
                    f"ACLs des Installationspfad-Vorfahren konnten nicht gebunden geprüft werden: {path}"
                ) from exc
        rebound = _read_bound_directory_prestate(path)
        if rebound != expected or _directory_prestate(path, os.fstat(descriptor), parent_metadata) != expected:
            raise RuntimeError(
                f"Installationspfad-Vorfahre driftete während der ACL-Prüfung: {path}"
            )
        return bool(
            {"system.posix_acl_access", "system.posix_acl_default"} & names
        )
    finally:
        os.close(descriptor)
        os.close(parent)


def _web_traversal_projection_strategy(
    previous: DirectoryMetadataPrestate,
    *,
    install_user: str,
    install_uid: int,
    web_gid: int,
) -> str:
    """Klassifiziert eine enge Pfadfreigabe, ohne globale Rechte zu öffnen."""

    if previous.mode & 0o001 or (
        previous.gid == int(web_gid) and previous.mode & 0o010
    ) or _web_account_can_traverse_bound_directory(previous):
        return "ready"
    if previous.uid != int(install_uid):
        raise RuntimeError(
            "Ein nicht traversierbarer Installationspfad-Vorfahre gehört nicht "
            f"dem Installationsbenutzer: {previous.path}"
        )

    if _bound_directory_has_extended_acl(previous):
        quoted = shlex.quote(previous.path)
        raise RuntimeError(
            "Ein Installationspfad-Vorfahre mit vorhandener POSIX-ACL wird "
            "nicht automatisch per chgrp/chmod verändert: "
            f"{previous.path}. Ergänze gezielt den bestehenden ACL-Vertrag "
            f"(`sudo setfacl -m u:www-data:--x -- {quoted}`) und starte "
            "denselben Updatebefehl erneut."
        )

    group_has_other_users = any(
        entry.pw_name != install_user and entry.pw_gid == previous.gid
        for entry in pwd.getpwall()
    )
    try:
        group_has_other_users = group_has_other_users or any(
            member != install_user
            for member in grp.getgrgid(previous.gid).gr_mem
        )
    except KeyError:
        group_has_other_users = True

    if previous.gid == int(web_gid):
        return "web-group"
    if not group_has_other_users and not (previous.mode & stat.S_ISGID):
        return "private-group-rebind"
    quoted = shlex.quote(previous.path)
    raise RuntimeError(
        "Ein gemeinsam genutzter oder setgid-Installationspfad-Vorfahre kann "
        "nicht automatisch freigegeben werden, ohne globale Rechte zu öffnen: "
        f"{previous.path}. Richte gezielt eine POSIX-ACL ein "
        f"(`sudo setfacl -m u:www-data:--x -- {quoted}`) oder verlege den "
        "Installationspfad unter einen owner-privaten, nicht-setgid Vorfahren."
    )


def _apply_bound_directory_transition(
    expected: DirectoryMetadataPrestate,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> DirectoryMetadataPrestate:
    """Ändert nur den exakt erwarteten Inode und bindet seinen Namen erneut."""

    path = Path(expected.path)
    parent = _open_absolute_directory_chain(path.parent)
    descriptor = -1
    rebound_parent = -1
    changed = False
    try:
        parent_before = os.fstat(parent)
        named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        current = _directory_prestate(path, opened, parent_before)
        if (
            not stat.S_ISDIR(named.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
            or current != expected
        ):
            raise RuntimeError(f"Verzeichnismetadaten drifteten: {path}")
        os.fchown(descriptor, int(uid), int(gid))
        changed = True
        os.fchmod(descriptor, int(mode))
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        projected = _directory_prestate(path, after, parent_before)
        rebound_parent = _open_absolute_directory_chain(path.parent)
        rebound_parent_metadata = os.fstat(rebound_parent)
        rebound = os.stat(path.name, dir_fd=rebound_parent, follow_symlinks=False)
        if (
            (rebound_parent_metadata.st_dev, rebound_parent_metadata.st_ino)
            != (parent_before.st_dev, parent_before.st_ino)
            or (after.st_dev, after.st_ino) != (expected.device, expected.inode)
            or (rebound.st_dev, rebound.st_ino) != (after.st_dev, after.st_ino)
            or projected.uid != int(uid)
            or projected.gid != int(gid)
            or projected.mode != int(mode)
            or rebound.st_uid != int(uid)
            or rebound.st_gid != int(gid)
            or stat.S_IMODE(rebound.st_mode) != int(mode)
        ):
            raise RuntimeError(f"Verzeichnismetadaten blieben abweichend: {path}")
        return projected
    except Exception as original_error:
        if changed and descriptor >= 0:
            rollback_error: Exception | None = None
            try:
                os.fchown(descriptor, expected.uid, expected.gid)
                os.fchmod(descriptor, expected.mode)
                os.fsync(descriptor)
                restored = os.fstat(descriptor)
                restored_parent = _open_absolute_directory_chain(path.parent)
                try:
                    restored_parent_metadata = os.fstat(restored_parent)
                    restored_named = os.stat(
                        path.name,
                        dir_fd=restored_parent,
                        follow_symlinks=False,
                    )
                    if (
                        (restored_parent_metadata.st_dev, restored_parent_metadata.st_ino)
                        != (expected.parent_device, expected.parent_inode)
                        or (restored.st_dev, restored.st_ino)
                        != (expected.device, expected.inode)
                        or (restored_named.st_dev, restored_named.st_ino)
                        != (expected.device, expected.inode)
                        or restored.st_uid != expected.uid
                        or restored.st_gid != expected.gid
                        or stat.S_IMODE(restored.st_mode) != expected.mode
                        or restored_named.st_uid != expected.uid
                        or restored_named.st_gid != expected.gid
                        or stat.S_IMODE(restored_named.st_mode) != expected.mode
                    ):
                        raise RuntimeError(
                            f"Verzeichnismetadaten wurden nicht vollständig restauriert: {path}"
                        )
                finally:
                    os.close(restored_parent)
            except Exception as exc:
                rollback_error = exc
            if rollback_error is not None:
                raise RuntimeError(
                    "Die Verzeichnismetadaten-Projektion scheiterte und ihr lokaler "
                    f"Rückfall blieb unvollständig: {path} ({rollback_error})"
                ) from original_error
        raise
    finally:
        if rebound_parent >= 0:
            os.close(rebound_parent)
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _project_web_home_traversal(
    install_user: str,
    target_root: Path,
) -> tuple[DirectoryMetadataTransition, ...]:
    """Gibt www-data nur auf echten nötigen Pfadvorfahren Traversierrecht."""

    account = pwd.getpwnam(install_user)
    home = Path(os.path.normpath(os.path.abspath(account.pw_dir)))
    normalized_target = Path(os.path.normpath(os.path.abspath(str(target_root))))
    try:
        relative_to_home = normalized_target.relative_to(home)
    except ValueError:
        return ()
    if not relative_to_home.parts:
        return ()

    ancestors = [home]
    current = home
    for component in relative_to_home.parts[:-1]:
        current /= component
        ancestors.append(current)

    web_gid = int(grp.getgrnam("www-data").gr_gid)
    plan: list[tuple[DirectoryMetadataPrestate, str]] = []
    for ancestor in ancestors:
        previous = _read_bound_directory_prestate(ancestor)
        strategy = _web_traversal_projection_strategy(
            previous,
            install_user=install_user,
            install_uid=int(account.pw_uid),
            web_gid=web_gid,
        )
        plan.append((previous, strategy))

    transitions: list[DirectoryMetadataTransition] = []
    try:
        for previous, strategy in plan:
            if strategy == "ready":
                transitions.append(
                    DirectoryMetadataTransition(previous=previous, projected=previous)
                )
                continue
            if strategy == "web-group":
                desired_gid = web_gid
                desired_mode = previous.mode | 0o010
            elif strategy == "private-group-rebind":
                desired_gid = web_gid
                desired_mode = (previous.mode & ~0o070) | 0o010
            else:
                raise RuntimeError(
                    f"Unbekannte Traversal-Projektionsstrategie: {strategy}"
                )

            projected = _apply_bound_directory_transition(
                previous,
                uid=previous.uid,
                gid=desired_gid,
                mode=desired_mode,
            )
            transitions.append(
                DirectoryMetadataTransition(previous=previous, projected=projected)
            )
        return tuple(transitions)
    except Exception as original_error:
        try:
            _restore_web_home_traversal(tuple(transitions))
        except Exception as rollback_error:
            raise RuntimeError(
                "Die Traversierrechte-Projektion scheiterte und der Rückfall ihrer "
                f"bereits geänderten Vorfahren blieb unvollständig ({rollback_error})"
            ) from original_error
        raise


def _preflight_web_home_traversal(install_user: str, target_root: Path) -> None:
    """Belegt vor Backup und Cutover, dass keine globale Freigabe nötig wird."""

    account = pwd.getpwnam(install_user)
    home = Path(os.path.normpath(os.path.abspath(account.pw_dir)))
    normalized_target = Path(os.path.normpath(os.path.abspath(str(target_root))))
    try:
        relative_to_home = normalized_target.relative_to(home)
    except ValueError:
        return
    if not relative_to_home.parts:
        return
    current = home
    ancestors = [home]
    for component in relative_to_home.parts[:-1]:
        current /= component
        ancestors.append(current)
    web_gid = int(grp.getgrnam("www-data").gr_gid)
    for ancestor in ancestors:
        previous = _read_bound_directory_prestate(ancestor)
        _web_traversal_projection_strategy(
            previous,
            install_user=install_user,
            install_uid=int(account.pw_uid),
            web_gid=web_gid,
        )


def _restore_web_home_traversal(
    transitions: Iterable[DirectoryMetadataTransition] | None,
) -> None:
    failures: list[str] = []
    for transition in reversed(tuple(transitions or ())):
        if transition.previous == transition.projected:
            continue
        try:
            _apply_bound_directory_transition(
                transition.projected,
                uid=transition.previous.uid,
                gid=transition.previous.gid,
                mode=transition.previous.mode,
            )
        except Exception as exc:
            failures.append(f"{transition.previous.path}: {exc}")
    if failures:
        raise RuntimeError(
            "Traversierrechte konnten nicht vollständig restauriert werden: "
            + "; ".join(failures)
        )


def _validate_live_install_context(target_root: Path) -> None:
    """Belegt den projizierten Produktstamm real aus Sicht des Webservers."""

    runuser = _runuser_binary(code="E3DC-UPD-PROJECTION-002")
    php = Path("/usr/bin/php")
    if not php.is_file() or not os.access(php, os.X_OK):
        _fail(
            "E3DC-UPD-PROJECTION-002",
            "PHP fehlt; der Installationskontext kann nach der Projektion nicht bestätigt werden.",
            "Installiere das Paket php-cli und starte danach denselben Ein-Datei-Updater erneut.",
        )

    # Simple-/Full-Updater sind Bare-Metal-Pfade und weisen Docker vorher ab.
    # Deshalb wird bewusst keine private oder optionale venv als www-data
    # geöffnet, sondern derselbe feste Systeminterpreter wie im Bare-Metal-UI.
    probe_python = Path("/usr/bin/python3")
    if not probe_python.is_file() or not os.access(probe_python, os.X_OK):
        _fail(
            "E3DC-UPD-PROJECTION-002",
            "Der von der Installationszentrale verwendete Python-Interpreter fehlt; "
            "die Installer-Importkette kann nicht bestätigt werden.",
            "Installiere das Paket python3 und starte danach denselben Ein-Datei-Updater erneut.",
        )

    php_probe = _run(
        [
            runuser,
            "-u",
            "www-data",
            "--",
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "HOME=/var/www",
            php,
            "-r",
            (
                "require '/var/www/html/helpers.php';"
                "$p=getInstallPaths();"
                "echo json_encode($p, JSON_UNESCAPED_SLASHES);"
            ),
        ],
        timeout=30,
    )
    try:
        paths = json.loads(php_probe.stdout)
    except (TypeError, ValueError, json.JSONDecodeError):
        paths = None
    expected_root = str(target_root.resolve(strict=True))
    actual_root = ""
    if isinstance(paths, dict):
        actual_root = str(paths.get("install_path") or "").rstrip("/")
    if (
        php_probe.returncode != 0
        or not isinstance(paths, dict)
        or paths.get("valid") is not True
        or actual_root != expected_root
    ):
        detail = ""
        if isinstance(paths, dict):
            detail = str(paths.get("error") or "").strip()
        if not detail:
            detail = (php_probe.stderr or php_probe.stdout or "keine PHP-Antwort").strip()
        _fail(
            "E3DC-UPD-PROJECTION-002",
            "Der Webserver kann den projizierten Installationspfad nicht eindeutig lesen"
            + (f": {detail}" if detail else "."),
            "Prüfe die Produktrechte mit sudo namei -l "
            + shlex.quote(str(target_root / "Installer/installer_config.py"))
            + " und starte danach denselben Ein-Datei-Updater erneut.",
        )

    import_probe = _run(
        [
            runuser,
            "-u",
            "www-data",
            "--",
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "HOME=/var/www",
            probe_python,
            "-I",
            "-B",
            "-c",
            (
                "import os,sys;"
                "root=os.path.realpath(sys.argv[1]);"
                "sys.path.insert(0,root);"
                "import Installer.web_installer as module;"
                "loaded=os.path.realpath(module.__file__);"
                "assert loaded.startswith(root+os.sep), (loaded,root)"
            ),
            target_root,
        ],
        timeout=60,
    )
    if import_probe.returncode != 0:
        detail = (import_probe.stderr or import_probe.stdout or "Import fehlgeschlagen").strip()
        _fail(
            "E3DC-UPD-PROJECTION-002",
            "Der Webserver kann die projizierte Installer-Importkette nicht lesen: "
            + detail,
            "Prüfe die Produktrechte mit sudo namei -l "
            + shlex.quote(str(target_root / "Installer/web_installer.py"))
            + " und starte danach denselben Ein-Datei-Updater erneut.",
        )


def _delete_approved_stale_paths(
    target_root: Path,
    policy: dict,
    *,
    target_binding: DirectoryMetadataPrestate | None = None,
) -> None:
    """Entfernt freigegebene Altdateien fd-relativ, nofollow und mountgebunden."""

    raw_deletes = tuple(policy.get("delete_files") or ())
    if not raw_deletes:
        return

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        _fail(
            "E3DC-UPD-WRITE-004",
            "Sichere fd-relative Löschung veralteter Dateien ist auf diesem System nicht verfügbar. Es wurden keine Altdateien entfernt.",
            "Python beziehungsweise das Linux-System aktualisieren und danach denselben Updatebefehl erneut starten.",
        )

    normalized_target_root = Path(os.path.abspath(target_root))
    if target_binding is not None and Path(target_binding.path) != normalized_target_root:
        raise RuntimeError("Stale-Löschung besitzt eine fremde Zielroot-Bindung")

    normalized_deletes: list[Path] = []
    unbound_target_deletes: list[Path] = []
    for raw in raw_deletes:
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            candidate = target_root / candidate
        candidate = Path(os.path.abspath(candidate))
        normalized_deletes.append(candidate)
        try:
            target_relative = candidate.relative_to(normalized_target_root)
        except ValueError:
            continue
        if target_relative.parts and target_binding is None:
            unbound_target_deletes.append(candidate)
    if unbound_target_deletes:
        _fail(
            "E3DC-UPD-WRITE-004",
            "Freigegebene Altdateien unter dem Installationspfad können ohne "
            "gebundene Zielroot nicht sicher entfernt werden. Es wurden keine "
            "Altdateien entfernt: "
            + ", ".join(str(path) for path in unbound_target_deletes),
            "Starte den aktuellen Ein-Datei-Updater erneut. Alte Aufrufer dürfen "
            "ohne Zielroot-Bindung weiterhin ausschließlich Webdateien entfernen.",
        )

    def verify_root_name(
        root: Path,
        parent_fd: int,
        root_fd: int,
        expected: tuple[int, int, int, int],
    ) -> None:
        parent = os.fstat(parent_fd)
        opened_root = os.fstat(root_fd)
        named_root = os.stat(
            root.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        actual = (
            int(parent.st_dev),
            int(parent.st_ino),
            int(opened_root.st_dev),
            int(opened_root.st_ino),
        )
        if (
            actual != expected
            or not stat.S_ISDIR(named_root.st_mode)
            or (named_root.st_dev, named_root.st_ino)
            != (opened_root.st_dev, opened_root.st_ino)
        ):
            raise RuntimeError(f"Stale-Löschwurzel wurde ausgetauscht: {root}")

    allowed_root_candidates = [
        normalized_target_root,
        Path("/var/www/html"),
    ]
    # Nur ein tatsächlich bestätigter eigener tmpfs-Mount benötigt einen
    # separaten Löschroot. Auf Altanlagen kann ``ramdisk`` ein gewöhnliches
    # Verzeichnis im Webbaum sein; dann bleibt der bewährte Webroot zuständig.
    # Ein unbestätigter Fremdmount wird beim fd-relativen Abstieg weiterhin
    # über die abweichende Mount-ID fail-closed erkannt.
    if _probe_ramdisk_tmpfs():
        allowed_root_candidates.append(RAMDISK_PATH)
    allowed_roots = tuple(
        sorted(
            allowed_root_candidates,
            key=lambda item: len(str(item)),
            reverse=True,
        )
    )
    errors: list[str] = []
    for candidate in normalized_deletes:
        selected_root: Path | None = None
        relative: Path | None = None
        for root in allowed_roots:
            try:
                bound_relative = candidate.relative_to(root)
            except ValueError:
                continue
            if bound_relative.parts:
                selected_root = root
                relative = bound_relative
                break
        if selected_root is None or relative is None:
            errors.append(f"außerhalb des Produktbereichs: {candidate}")
            continue

        cloexec = getattr(os, "O_CLOEXEC", 0)
        root_parent_fd = -1
        root_fd = -1
        opened: list[int] = []
        try:
            (
                root_parent_fd,
                root_fd,
                root_parent_metadata,
                root_metadata,
            ) = _open_bound_named_directory(selected_root)
            root_identity = (
                int(root_parent_metadata.st_dev),
                int(root_parent_metadata.st_ino),
                int(root_metadata.st_dev),
                int(root_metadata.st_ino),
            )
            if selected_root == normalized_target_root and target_binding is not None:
                expected_target_identity = (
                    target_binding.parent_device,
                    target_binding.parent_inode,
                    target_binding.device,
                    target_binding.inode,
                )
                if root_identity != expected_target_identity:
                    raise RuntimeError(
                        "Installationsroot driftete vor der Stale-Löschung"
                    )
            verify_root_name(
                selected_root,
                root_parent_fd,
                root_fd,
                root_identity,
            )
            root_device = root_metadata.st_dev
            root_mount_id = _fd_mount_id(root_fd)
            if selected_root == RAMDISK_PATH:
                # Die pfadbasierte tmpfs-Bestätigung wird an genau den bereits
                # geöffneten nofollow-FD gebunden. Ein Un-/Ummount während der
                # Probe darf nicht unbemerkt einen neuen Löschroot autorisieren.
                if not _probe_ramdisk_tmpfs():
                    raise RuntimeError("RAM-Disk ist kein bestätigtes tmpfs")
                confirmation_parent_fd = -1
                confirmation_fd = -1
                try:
                    (
                        confirmation_parent_fd,
                        confirmation_fd,
                        _confirmation_parent,
                        confirmation_metadata,
                    ) = _open_bound_named_directory(selected_root)
                    if (
                        (confirmation_metadata.st_dev, confirmation_metadata.st_ino)
                        != (root_metadata.st_dev, root_metadata.st_ino)
                        or _fd_mount_id(confirmation_fd) != root_mount_id
                    ):
                        raise RuntimeError(
                            "RAM-Disk-Mount änderte sich während der Bestätigung"
                        )
                finally:
                    if confirmation_fd >= 0:
                        os.close(confirmation_fd)
                    if confirmation_parent_fd >= 0:
                        os.close(confirmation_parent_fd)
            parent_fd = root_fd
            missing = False
            for depth, component in enumerate(relative.parts[:-1], start=1):
                try:
                    named_child = os.stat(
                        component,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    child_fd = os.open(
                        component,
                        os.O_RDONLY | nofollow | directory | cloexec,
                        dir_fd=parent_fd,
                    )
                except FileNotFoundError:
                    missing = True
                    break
                child_metadata = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(named_child.st_mode)
                    or (named_child.st_dev, named_child.st_ino)
                    != (child_metadata.st_dev, child_metadata.st_ino)
                    or child_metadata.st_dev != root_device
                    or _fd_mount_id(child_fd) != root_mount_id
                ):
                    os.close(child_fd)
                    raise RuntimeError(
                        "verschachtelter Mount in "
                        + str(selected_root.joinpath(*relative.parts[:depth]))
                    )
                opened.append(child_fd)
                parent_fd = child_fd
            if missing:
                continue
            name = relative.parts[-1]
            try:
                metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("Löschliste darf keine Verzeichnisse entfernen")
            if stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise RuntimeError("Löschziel besitzt Hardlinks")
                target_fd = os.open(name, os.O_RDONLY | nofollow | cloexec, dir_fd=parent_fd)
                try:
                    bound = os.fstat(target_fd)
                    if (
                        (bound.st_dev, bound.st_ino)
                        != (metadata.st_dev, metadata.st_ino)
                        or bound.st_nlink != 1
                        or bound.st_dev != root_device
                        or _fd_mount_id(target_fd) != root_mount_id
                    ):
                        raise RuntimeError("Löschziel driftete oder liegt auf Fremdmount")
                    current = os.stat(
                        name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    rebound = os.fstat(target_fd)
                    if (
                        not stat.S_ISREG(current.st_mode)
                        or (current.st_dev, current.st_ino, current.st_nlink)
                        != (bound.st_dev, bound.st_ino, 1)
                        or (rebound.st_dev, rebound.st_ino, rebound.st_nlink)
                        != (bound.st_dev, bound.st_ino, 1)
                    ):
                        raise RuntimeError("Löschziel wurde vor unlink ersetzt")
                    os.unlink(name, dir_fd=parent_fd)
                    if os.fstat(target_fd).st_nlink != 0:
                        raise RuntimeError("Entferntes Löschziel blieb verlinkt")
                finally:
                    os.close(target_fd)
            elif not stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError("Löschziel ist eine Spezialdatei")
            else:
                os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            verify_root_name(
                selected_root,
                root_parent_fd,
                root_fd,
                root_identity,
            )
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
        except RuntimeError as exc:
            errors.append(f"{candidate}: {exc}")
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)
            if root_fd >= 0:
                os.close(root_fd)
            if root_parent_fd >= 0:
                os.close(root_parent_fd)
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
    emergency_quiesce_gate: bool = False,
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
    if emergency_quiesce_gate:
        from Installer.emergency_release import PERSISTENT_EMERGENCY_LATCH_PATH

        unit_lines.append(
            f"ConditionPathExists=!{PERSISTENT_EMERGENCY_LATCH_PATH}"
        )
        unit_lines.append(
            f"ConditionPathIsSymbolicLink=!{PERSISTENT_EMERGENCY_LATCH_PATH}"
        )
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
    if payload is not None:
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
    if unit == "e3dc-storage-manager.service":
        from Installer.emergency_release import ensure_persistent_emergency_start_veto

        # Dieser root-eigene Drop-in liegt bewusst außerhalb des Releasebaums.
        # Er wird nach jeder Drop-in-Projektion neu gebunden, damit selbst ein
        # späterer Rücklauf auf eine alte ungated Unit den Latch nicht umgeht.
        ensure_persistent_emergency_start_veto()


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
    masked_units: Iterable[str] = (),
    *,
    release_root: Path | None = None,
) -> None:
    """Ersetzt die Pflicht-Units direkt; der Altzustand ist keine Hürde."""

    installer = target_root / "Installer"
    release_installer = (release_root or target_root) / "Installer"
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
            "emergency_quiesce_gate": True,
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
        if not (release_installer / "ha_systemd_start_gate.py").is_file():
            _fail(
                "E3DC-UPD-SERVICE-004",
                f"Das lokale HA-Starttor des neuen Releases fehlt: {start_gate}",
                "Lade den Ein-Datei-Updater erneut herunter und starte ihn nochmals.",
            )
        start_condition = (str(python), str(start_gate))

    masked = {_normalize_unit(unit) for unit in masked_units}
    for spec in specs:
        if str(spec["unit"]) in masked:
            continue
        script = installer / str(spec["script"])
        if not (release_installer / str(spec["script"])).is_file():
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
            emergency_quiesce_gate=bool(spec.get("emergency_quiesce_gate")),
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
    masked_units: Iterable[str] = (),
) -> str | None:
    unit = ROLE_SERVICE_BY_MODE.get(role)
    if unit is None:
        return None
    if unit in {_normalize_unit(item) for item in masked_units}:
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
        source_script = script
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
        source_script = (release_root or RELEASE_ROOT) / "Installer/shadow_sync.py"
        working_directory = script.parent
        argv = (str(python), str(script))
        environment = ()
        restart_seconds = 5
        service_user = install_user
        service_group = "www-data"
        syslog_identifier = "e3dc-shadow-sync"
    if not source_script.is_file():
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
    *,
    stage_before_cutover: bool = False,
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
    if (
        not stage_before_cutover
        and (not runuser.is_file() or not os.access(runuser, os.X_OK))
    ):
        warnings.append(
            f"Optionales Modul {module_name}: runuser fehlt; npm-Abhängigkeiten "
            "wurden nicht automatisch repariert."
        )
        return npm, warnings
    npm_command = [
        npm,
        "--prefix",
        workdir,
        "ci",
        "--omit=dev",
        "--ignore-scripts",
    ]
    command = (
        npm_command
        if stage_before_cutover
        else [runuser, "-u", install_user, "--", *npm_command]
    )
    prepared = _run(command, timeout=240)
    if prepared.returncode != 0:
        detail = (prepared.stderr or prepared.stdout or "unbekannter npm-Fehler").strip()
        if stage_before_cutover:
            shutil.rmtree(workdir / "node_modules", ignore_errors=True)
        warnings.append(
            f"Optionales Modul {module_name}: npm-Abhängigkeiten konnten nicht "
            f"repariert werden ({detail})."
        )
    return npm, warnings


def _prepare_selected_release_dependencies(
    release_root: Path,
    install_user: str,
    selected_units: Iterable[str],
) -> tuple[frozenset[str], list[str]]:
    """Erledigt optionale Netz-/Paketarbeit vollständig vor dem Dienststopp."""

    from Installer.service_catalog import get_module_by_service

    prepared: set[str] = set()
    warnings: list[str] = []
    installer = release_root / "Installer"
    for raw_unit in sorted({_normalize_unit(unit) for unit in selected_units}):
        module = get_module_by_service(raw_unit)
        if module is None or str(module.runner or "python").strip().lower() != "npm":
            continue
        workdir = _catalog_target_path(
            installer,
            module.working_directory or ".",
            label="Modul-Arbeitsverzeichnis",
        )
        _npm, npm_warnings = _prepare_npm_module(
            workdir,
            install_user,
            raw_unit,
            stage_before_cutover=True,
        )
        if npm_warnings:
            detail = "; ".join(npm_warnings)
            _fail(
                "E3DC-UPD-DEP-005",
                f"Die Abhängigkeiten des zuvor verwendeten Dienstes {raw_unit} konnten vor dem Dienststopp nicht vorbereitet werden: {detail}",
                "Prüfe freien Speicherplatz, Netzwerkzugang sowie npm --version und starte danach denselben Ein-Datei-Updater erneut; die laufenden Dienste wurden noch nicht gestoppt.",
            )
        prepared.add(raw_unit)
    return frozenset(prepared), warnings


def _ensure_selected_catalog_services(
    target_root: Path,
    release_root: Path,
    install_user: str,
    python: Path,
    role: str,
    dropin_payload: bytes | None,
    selected_units: Iterable[str],
    masked_units: Iterable[str] = (),
    prepared_npm_units: Iterable[str] = (),
) -> list[str]:
    """Projiziert nur optionale Module, die vorher aktiv oder enabled waren."""

    from Installer.service_catalog import LOAD_ACTIVE_CONTROL, get_module_by_service, service_load_profile

    warnings: list[str] = []
    installer = target_root / "Installer"
    release_installer = release_root / "Installer"
    excluded = set(CORE_RESULT_SERVICES) | set(ROLE_SERVICE_BY_MODE.values())
    masked = {_normalize_unit(unit) for unit in masked_units}
    prepared_npm = {_normalize_unit(unit) for unit in prepared_npm_units}
    selected = {
        _normalize_unit(unit)
        for unit in selected_units
        if _normalize_unit(unit) not in excluded
        and _normalize_unit(unit) not in masked
    }
    start_condition: tuple[str, ...] = ()
    if selected and role in {"master", "slave", "shadow"}:
        start_gate = installer / "ha_systemd_start_gate.py"
        if not (release_installer / "ha_systemd_start_gate.py").is_file():
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
        release_script = _catalog_target_path(
            release_installer,
            module.script,
            label="Modulskript",
        )
        release_workdir = _catalog_target_path(
            release_installer,
            module.working_directory or ".",
            label="Modul-Arbeitsverzeichnis",
        )
        script = installer / release_script.relative_to(release_installer)
        workdir = installer / release_workdir.relative_to(release_installer)
        load_profile = service_load_profile(module)
        if not release_script.is_file() or not release_workdir.is_dir():
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
            if unit in prepared_npm:
                npm = _npm_executable() or Path("/usr/bin/npm")
            else:
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


def _release_web_program_contract(
    release_root: Path,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Bindet die vollständige Web-Produktliste des entpackten Releases."""

    source = (release_root / "html").resolve(strict=True)
    files = {"VERSION", "CHANGELOG.md", "UPDATE_POLICY.json"}
    directories = set()

    def walk(directory: Path, relative: tuple[str, ...]) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            metadata = entry.stat(follow_symlinks=False)
            child = (*relative, entry.name)
            if not relative and entry.name in PRESERVED_WEB_ENTRIES:
                raise RuntimeError(
                    f"Release-Webquelle kollidiert mit erhaltener Laufzeitfläche: {entry.name}"
                )
            projected = "/".join(child)
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(projected)
                walk(directory / entry.name, child)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                files.add(projected)
            else:
                raise RuntimeError(
                    f"Release-Webquelle enthält Link, Spezialdatei oder Hardlink: {projected}"
                )

    walk(source, ())
    if not {"index.php", "helpers.php", "retention.php"}.issubset(files):
        raise RuntimeError("Release-Webquelle ist unvollständig")
    return tuple(sorted(files)), tuple(
        sorted(directories, key=lambda value: (value.count("/"), value))
    )


def _sync_web_tree(
    release_root: Path,
    destination: Path = Path("/var/www/html"),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    source = release_root / "html"
    program_contract = _release_web_program_contract(release_root)
    www_gid = grp.getgrnam("www-data").gr_gid
    web_binding = _project_release_tree(
        source,
        destination,
        uid=0,
        gid=www_gid,
        root_mode=0o755,
        directory_mode=0o755,
        file_mode=0o644,
        excluded_top_level=PRESERVED_WEB_ENTRIES,
    )

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    parent_fd, root_fd, _parent_metadata, opened_root = _open_bound_named_directory(
        destination
    )
    try:
        root_metadata = os.fstat(root_fd)
        if (
            (opened_root.st_dev, opened_root.st_ino)
            != (web_binding.device, web_binding.inode)
        ):
            raise RuntimeError(
                f"Der Webroot driftete nach der Releaseprojektion: {destination}"
            )
        root_device = root_metadata.st_dev
        root_mount_id = _fd_mount_id(root_fd)
        for name in sorted(PRESERVED_WEB_DIRS):
            # Die produktive RAM-Disk ist absichtlich ein eigenes tmpfs. Ihr
            # Inhalt ist bereits von der Releaseprojektion ausgeschlossen und
            # wird später über den Laufzeit-Rechtevertrag normalisiert. Nur
            # dieser exakt bestätigte Mount wird hier nicht wie ein gewöhnliches
            # Verzeichnis gegen die Geräte-/Mount-ID des Webroots geprüft.
            if destination / name == RAMDISK_PATH and _probe_ramdisk_tmpfs():
                continue
            runtime_fd = _open_bound_projection_directory(
                root_fd,
                name,
                destination / name,
                root_device=root_device,
                root_mount_id=root_mount_id,
                uid=0,
                gid=www_gid,
                mode=0o700,
            )
            try:
                os.fchown(runtime_fd, 0, www_gid)
                os.fchmod(runtime_fd, 0o700)
                os.fsync(runtime_fd)
            finally:
                os.close(runtime_fd)
        for name in ("VERSION", "CHANGELOG.md", "UPDATE_POLICY.json"):
            _project_regular_file(
                release_root / name,
                root_fd,
                name,
                destination / name,
                root_device=root_device,
                root_mount_id=root_mount_id,
                uid=0,
                gid=www_gid,
                mode=0o644,
            )
    finally:
        os.close(root_fd)
        os.close(parent_fd)
    _assert_named_directory_binding(web_binding)
    return program_contract


def _permission_contract_for_preserved_entry(
    top_name: str,
    relative: tuple[str, ...],
    *,
    install_uid: int,
    www_uid: int,
    www_gid: int,
    data_dir_mode: int,
    data_file_mode: int,
) -> tuple[int, int, int, int]:
    """Liefert Owner und Modi für genau einen erhaltenen Web-Eintrag."""

    if top_name == "data":
        if relative and relative[0] == "matter-storage":
            return install_uid, www_gid, 0o700, 0o600
        if relative and relative[0] == ".wallbox_plan_jobs":
            return www_uid, www_gid, 0o700, 0o600
        if relative[:2] == ("config_backups", "aux_inverter_migration"):
            return install_uid, www_gid, 0o700, 0o600
        if relative == (".e3dc_v4.transaction.lock",):
            return www_uid, www_gid, data_dir_mode, 0o660
        if relative in {
            ("wallbox_mode5_user_start_request.json",),
            ("wallbox_mode5_user_start_request.json.lock",),
        }:
            return www_uid, www_gid, data_dir_mode, 0o660
        if relative == ("external_pv_topology.json",):
            return install_uid, www_gid, data_dir_mode, 0o664
        return install_uid, www_gid, data_dir_mode, data_file_mode
    if top_name == "history_backups":
        return install_uid, www_gid, 0o750, 0o640
    if top_name == "tmp" and relative and relative[0] in {
        "rule_calm_current",
        "rule_calm_uploads",
    }:
        return www_uid, www_gid, 0o700, 0o600
    if (
        top_name == "ramdisk"
        and relative
        and relative[0].startswith("rule_calm_analysis.json")
    ):
        return www_uid, www_gid, 0o2775, 0o600
    return install_uid, www_gid, 0o2775, 0o664


def _copy_bound_regular_file(
    parent_fd: int,
    name: str,
    source_fd: int,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    """Bricht einen Mehrfachlink im gebundenen Verzeichnis ohne Fremd-Chown."""

    temporary_name = f".e3dc-permissions-{os.getpid()}-{os.urandom(12).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
    replaced = False
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                offset += os.write(temporary_fd, chunk[offset:])
        os.fchown(temporary_fd, uid, gid)
        os.fchmod(temporary_fd, mode)
        os.fsync(temporary_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        source = os.fstat(source_fd)
        if (current.st_dev, current.st_ino) != (source.st_dev, source.st_ino):
            raise RuntimeError(f"Erhaltener Web-Eintrag wechselte während der Rechteprojektion: {name}")
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        secured = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(secured.st_mode)
            or secured.st_nlink != 1
            or secured.st_uid != uid
            or secured.st_gid != gid
            or stat.S_IMODE(secured.st_mode) != mode
        ):
            raise RuntimeError(f"Mehrfachlink-Rechteprojektion blieb unvollständig: {name}")
        replaced = True
    finally:
        os.close(temporary_fd)
        if not replaced:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _normalize_preserved_web_permissions(
    destination: Path,
    install_user: str,
    config: dict,
) -> None:
    """Normalisiert erhaltene Nutzer-/Laufzeitdaten fd-relativ und symlinkfrei."""

    from Installer.config_secret_permissions import config_secret_dir_mode, config_secret_file_mode

    account = pwd.getpwnam(install_user)
    web_account = pwd.getpwnam("www-data")
    www_gid = grp.getgrnam("www-data").gr_gid
    data_dir_mode = config_secret_dir_mode(config)
    data_file_mode = config_secret_file_mode(config)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory_flag:
        raise RuntimeError("Sichere fd-relative Rechteprojektion ist nicht verfügbar")
    open_flags = os.O_RDONLY | nofollow | directory_flag | getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open(destination, open_flags)

    def normalize_regular(
        parent_fd: int,
        name: str,
        before: os.stat_result,
        *,
        uid: int,
        gid: int,
        mode: int,
        device: int,
    ) -> None:
        flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        try:
            current = os.fstat(descriptor)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_dev != device
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            ):
                return
            if current.st_nlink > 1:
                _copy_bound_regular_file(
                    parent_fd,
                    name,
                    descriptor,
                    uid=uid,
                    gid=gid,
                    mode=mode,
                )
                return
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, mode)
            secured = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                (secured.st_dev, secured.st_ino) != (named.st_dev, named.st_ino)
                or secured.st_uid != uid
                or secured.st_gid != gid
                or stat.S_IMODE(secured.st_mode) != mode
            ):
                raise RuntimeError(f"Rechteprojektion blieb unvollständig: {name}")
        finally:
            os.close(descriptor)

    def normalize_directory(
        parent_fd: int,
        name: str,
        top_name: str,
        relative: tuple[str, ...],
        device: int,
    ) -> None:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode) or before.st_dev != device:
            return
        descriptor = os.open(name, open_flags, dir_fd=parent_fd)
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError(f"Erhaltener Web-Pfad wechselte beim Öffnen: {name}")
            for child_name in sorted(os.listdir(descriptor)):
                if (
                    top_name == "data"
                    and not relative
                    and (
                        child_name in MATTER_RESET_PROTECTED_DATA_NAMES
                        or child_name.startswith(MATTER_RESET_PROTECTED_DATA_PREFIXES)
                    )
                ):
                    continue
                child = os.stat(child_name, dir_fd=descriptor, follow_symlinks=False)
                child_relative = (*relative, child_name)
                if (
                    top_name == "data"
                    and child_relative == (".e3dc_v4.transaction.lock",)
                    and (
                        not stat.S_ISREG(child.st_mode)
                        or child.st_nlink != 1
                    )
                ):
                    raise RuntimeError(
                        "Config-Transaktionslock ist keine reguläre Einzeldatei"
                    )
                uid, gid, dir_mode, file_mode = _permission_contract_for_preserved_entry(
                    top_name,
                    child_relative,
                    install_uid=account.pw_uid,
                    www_uid=web_account.pw_uid,
                    www_gid=www_gid,
                    data_dir_mode=data_dir_mode,
                    data_file_mode=data_file_mode,
                )
                if stat.S_ISDIR(child.st_mode):
                    normalize_directory(
                        descriptor,
                        child_name,
                        top_name,
                        child_relative,
                        device,
                    )
                elif stat.S_ISREG(child.st_mode):
                    normalize_regular(
                        descriptor,
                        child_name,
                        child,
                        uid=uid,
                        gid=gid,
                        mode=file_mode,
                        device=device,
                    )
            uid, gid, dir_mode, _file_mode = _permission_contract_for_preserved_entry(
                top_name,
                relative,
                install_uid=account.pw_uid,
                www_uid=web_account.pw_uid,
                www_gid=www_gid,
                data_dir_mode=data_dir_mode,
                data_file_mode=data_file_mode,
            )
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, dir_mode)
        finally:
            os.close(descriptor)

    try:
        root = os.fstat(root_fd)
        if not stat.S_ISDIR(root.st_mode):
            raise RuntimeError("Webroot ist kein echtes Verzeichnis")
        top_file_modes = {
            "e3dc.config.txt": 0o640,
            "e3dc.strompreise.txt": 0o640,
            "e3dc.wallbox.out": 0o664,
            "e3dc.wallbox.txt": 0o640,
            "e3dc_paths.json": 0o640,
            "live_history.txt": 0o664,
        }
        for name in sorted(PRESERVED_WEB_ENTRIES):
            try:
                entry = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(entry.st_mode):
                normalize_directory(root_fd, name, name, (), entry.st_dev)
            elif stat.S_ISREG(entry.st_mode) and name in top_file_modes:
                normalize_regular(
                    root_fd,
                    name,
                    entry,
                    uid=account.pw_uid,
                    gid=www_gid,
                    mode=top_file_modes[name],
                    device=entry.st_dev,
                )
    finally:
        os.close(root_fd)


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


def _verify_bound_directory_handles(
    expected: DirectoryMetadataPrestate,
    parent_fd: int,
    directory_fd: int,
) -> None:
    """Bestätigt einen gehaltenen Verzeichnis-FD samt weiterhin gebundenem Namen."""

    path = Path(expected.path)
    parent = os.fstat(parent_fd)
    opened = os.fstat(directory_fd)
    named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    current = _directory_prestate(path, opened, parent)
    if (
        current != expected
        or not stat.S_ISDIR(named.st_mode)
        or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        or named.st_uid != expected.uid
        or named.st_gid != expected.gid
        or stat.S_IMODE(named.st_mode) != expected.mode
    ):
        raise RuntimeError(f"Gebundenes Konfigurationsverzeichnis driftete: {path}")


def _read_bound_directory_json(
    directory_binding: DirectoryMetadataPrestate,
    name: str,
    *,
    missing_ok: bool,
    maximum_bytes: int = 1024 * 1024,
) -> dict:
    """Liest eine einzelne JSON-Datei fd-relativ und ohne Symlinkfolge."""

    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise RuntimeError("Konfigurationsdateiname ist ungültig")
    parent_fd, directory_fd, _parent, _opened = _open_bound_named_directory(
        Path(directory_binding.path)
    )
    descriptor = -1
    try:
        _verify_bound_directory_handles(directory_binding, parent_fd, directory_fd)
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            if missing_ok:
                _verify_bound_directory_handles(
                    directory_binding,
                    parent_fd,
                    directory_fd,
                )
                return {}
            raise
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > int(maximum_bytes)
        ):
            raise RuntimeError("Konfiguration ist keine sichere Einzeldatei")
        payload = b""
        while len(payload) < metadata.st_size:
            block = os.read(descriptor, metadata.st_size - len(payload))
            if not block:
                raise RuntimeError("Konfiguration endet unerwartet")
            payload += block
        if os.read(descriptor, 1):
            raise RuntimeError("Konfiguration wuchs während des Lesens")
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            != (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            or (named.st_dev, named.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise RuntimeError("Konfiguration driftete beim Lesen")
        _verify_bound_directory_handles(directory_binding, parent_fd, directory_fd)
        value = json.loads(payload.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("JSON-Wurzel ist kein Objekt")
        return value
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)
        os.close(parent_fd)


def _write_bound_directory_file(
    directory_binding: DirectoryMetadataPrestate,
    name: str,
    payload: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    """Publiziert eine Datei atomar innerhalb eines exakt gebundenen Verzeichnisses."""

    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or len(payload) > 1024 * 1024:
        raise RuntimeError("Konfigurationsdateivertrag ist ungültig")
    parent_fd, directory_fd, _parent, _opened = _open_bound_named_directory(
        Path(directory_binding.path)
    )
    temporary_name = f".{name}.update-{os.getpid()}-{os.urandom(12).hex()}"
    descriptor = -1
    published = False
    try:
        _verify_bound_directory_handles(directory_binding, parent_fd, directory_fd)
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise RuntimeError("Konfigurationsziel ist keine sichere Einzeldatei")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        staged = os.fstat(descriptor)
        _verify_bound_directory_handles(directory_binding, parent_fd, directory_fd)
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        published = True
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            (named.st_dev, named.st_ino) != (staged.st_dev, staged.st_ino)
            or named.st_uid != uid
            or named.st_gid != gid
            or stat.S_IMODE(named.st_mode) != mode
            or named.st_size != len(payload)
        ):
            raise RuntimeError("Konfigurationsdatei blieb nach dem Schreiben abweichend")
        _verify_bound_directory_handles(directory_binding, parent_fd, directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)
        os.close(parent_fd)
    _assert_named_directory_binding(directory_binding)


def _read_live_web_v4_json(*, missing_ok: bool) -> dict:
    """Liest die V4-Konfiguration nur aus einem echten, stabil gebundenen data-Ordner."""

    data_dir = Path("/var/www/html/data")
    try:
        binding = _read_bound_directory_prestate(data_dir)
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise
    return _read_bound_directory_json(
        binding,
        "e3dc_v4.json",
        missing_ok=missing_ok,
    )


def _bind_live_installer_directory(
    target_binding: DirectoryMetadataPrestate,
) -> tuple[int, int, int, os.stat_result]:
    """Bindet Produktstamm und Installer-Unterverzeichnis komponentenweise."""

    target_root = Path(target_binding.path)
    parent_fd, root_fd, parent_metadata, root_metadata = _open_bound_named_directory(
        target_root
    )
    installer_fd = -1
    try:
        current_root = _directory_prestate(
            target_root,
            root_metadata,
            parent_metadata,
        )
        if current_root != target_binding:
            raise RuntimeError("Produktstamm driftete vor der Installer-Bindung")
        installer_named = os.stat(
            "Installer",
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        installer_fd = os.open(
            "Installer",
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=root_fd,
        )
        installer_opened = os.fstat(installer_fd)
        if (
            not stat.S_ISDIR(installer_named.st_mode)
            or not stat.S_ISDIR(installer_opened.st_mode)
            or (installer_named.st_dev, installer_named.st_ino)
            != (installer_opened.st_dev, installer_opened.st_ino)
        ):
            raise RuntimeError("Installer-Verzeichnis driftete bei der Bindung")
        return parent_fd, root_fd, installer_fd, installer_opened
    except Exception:
        if installer_fd >= 0:
            os.close(installer_fd)
        os.close(root_fd)
        os.close(parent_fd)
        raise


def _verify_live_installer_binding(
    target_binding: DirectoryMetadataPrestate,
    parent_fd: int,
    root_fd: int,
    installer_fd: int,
    installer_expected: os.stat_result,
) -> None:
    target_root = Path(target_binding.path)
    root_opened = os.fstat(root_fd)
    root_named = os.stat(
        target_root.name,
        dir_fd=parent_fd,
        follow_symlinks=False,
    )
    installer_opened = os.fstat(installer_fd)
    installer_named = os.stat(
        "Installer",
        dir_fd=root_fd,
        follow_symlinks=False,
    )
    if (
        (root_opened.st_dev, root_opened.st_ino)
        != (target_binding.device, target_binding.inode)
        or (root_named.st_dev, root_named.st_ino)
        != (target_binding.device, target_binding.inode)
        or root_opened.st_uid != target_binding.uid
        or root_opened.st_gid != target_binding.gid
        or stat.S_IMODE(root_opened.st_mode) != target_binding.mode
        or (installer_opened.st_dev, installer_opened.st_ino)
        != (installer_expected.st_dev, installer_expected.st_ino)
        or (installer_named.st_dev, installer_named.st_ino)
        != (installer_expected.st_dev, installer_expected.st_ino)
        or installer_opened.st_uid != installer_expected.st_uid
        or installer_opened.st_gid != installer_expected.st_gid
        or stat.S_IMODE(installer_opened.st_mode)
        != stat.S_IMODE(installer_expected.st_mode)
        or installer_named.st_uid != installer_expected.st_uid
        or installer_named.st_gid != installer_expected.st_gid
        or stat.S_IMODE(installer_named.st_mode)
        != stat.S_IMODE(installer_expected.st_mode)
    ):
        raise RuntimeError("Produkt- oder Installer-Bindung driftete")


def _read_live_installer_json(
    target_binding: DirectoryMetadataPrestate,
    name: str,
    *,
    missing_ok: bool,
) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise RuntimeError("Installer-Dateiname ist ungültig")
    parent_fd, root_fd, installer_fd, installer_metadata = (
        _bind_live_installer_directory(target_binding)
    )
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=installer_fd,
            )
        except FileNotFoundError:
            if missing_ok:
                _verify_live_installer_binding(
                    target_binding,
                    parent_fd,
                    root_fd,
                    installer_fd,
                    installer_metadata,
                )
                return {}
            raise
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > 1024 * 1024
        ):
            raise RuntimeError("Installer-Konfiguration ist keine sichere Einzeldatei")
        payload = b""
        while len(payload) < metadata.st_size:
            block = os.read(descriptor, metadata.st_size - len(payload))
            if not block:
                raise RuntimeError("Installer-Konfiguration endet unerwartet")
            payload += block
        if os.read(descriptor, 1):
            raise RuntimeError("Installer-Konfiguration wuchs während des Lesens")
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=installer_fd, follow_symlinks=False)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
            or (named.st_dev, named.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise RuntimeError("Installer-Konfiguration driftete beim Lesen")
        _verify_live_installer_binding(
            target_binding,
            parent_fd,
            root_fd,
            installer_fd,
            installer_metadata,
        )
        value = json.loads(payload.decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("JSON-Wurzel ist kein Objekt")
        return value
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(installer_fd)
        os.close(root_fd)
        os.close(parent_fd)


def _write_live_installer_file(
    target_binding: DirectoryMetadataPrestate,
    name: str,
    payload: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or len(payload) > 1024 * 1024:
        raise RuntimeError("Installer-Dateivertrag ist ungültig")
    parent_fd, root_fd, installer_fd, installer_metadata = (
        _bind_live_installer_directory(target_binding)
    )
    temporary_name = f".{name}.update-{os.getpid()}-{os.urandom(12).hex()}"
    descriptor = -1
    published = False
    try:
        try:
            existing = os.stat(name, dir_fd=installer_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise RuntimeError("Installer-Zieldatei ist keine sichere Einzeldatei")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=installer_fd,
        )
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        staged = os.fstat(descriptor)
        _verify_live_installer_binding(
            target_binding,
            parent_fd,
            root_fd,
            installer_fd,
            installer_metadata,
        )
        os.replace(
            temporary_name,
            name,
            src_dir_fd=installer_fd,
            dst_dir_fd=installer_fd,
        )
        os.fsync(installer_fd)
        published = True
        named = os.stat(name, dir_fd=installer_fd, follow_symlinks=False)
        if (
            (named.st_dev, named.st_ino) != (staged.st_dev, staged.st_ino)
            or named.st_uid != uid
            or named.st_gid != gid
            or stat.S_IMODE(named.st_mode) != mode
            or named.st_size != len(payload)
        ):
            raise RuntimeError("Installer-Datei blieb nach dem Schreiben abweichend")
        _verify_live_installer_binding(
            target_binding,
            parent_fd,
            root_fd,
            installer_fd,
            installer_metadata,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=installer_fd)
            except FileNotFoundError:
                pass
        os.close(installer_fd)
        os.close(root_fd)
        os.close(parent_fd)


def _project_path_metadata(
    target_root: Path,
    target_binding: DirectoryMetadataPrestate,
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
    local: dict = {}
    try:
        local.update(
            _read_live_installer_json(
                target_binding,
                "installer_config.json",
                missing_ok=True,
            )
        )
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
    _write_live_installer_file(
        target_binding,
        "installer_config.json",
        local_payload,
        uid=account.pw_uid,
        gid=www_gid,
        mode=0o640,
    )

    data_dir = Path("/var/www/html/data")
    data_binding = _read_bound_directory_prestate(data_dir)
    v4_path = data_dir / "e3dc_v4.json"
    try:
        v4 = _read_bound_directory_json(
            data_binding,
            "e3dc_v4.json",
            missing_ok=True,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        v4 = None
        print(
            f"[WARNUNG] {v4_path} ist nicht als JSON lesbar und bleibt unverändert: {exc}",
            flush=True,
        )
    secret_mode_source = {} if v4 is None else (v4 if v4 else config)
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
        _write_bound_directory_file(
            data_binding,
            "e3dc_v4.json",
            v4_payload,
            uid=account.pw_uid,
            gid=www_gid,
            mode=config_secret_file_mode(secret_mode_source),
        )
    _apply_bound_directory_transition(
        data_binding,
        uid=account.pw_uid,
        gid=www_gid,
        mode=config_secret_dir_mode(secret_mode_source),
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


def _release_python_script(path: Path, *, label: str) -> bytes:
    payload = path.read_bytes()
    if (
        not payload.startswith(b"#!/usr/bin/python3\n")
        or b"\r" in payload
        or len(payload) > 512 * 1024
    ):
        raise RuntimeError(f"{label}-Vorlage im Ziel-Release ist ungültig")
    try:
        compile(payload.decode("utf-8", errors="strict"), str(path), "exec")
    except (SyntaxError, UnicodeError) as exc:
        raise RuntimeError(f"{label}-Vorlage besitzt ungültigen Python-Code") from exc
    if (
        payload.count(b'e3dc_runtime_permissions_v1') != 1
        or payload.count(b'e3dc_runtime_permissions_cli_v3') != 1
    ):
        raise RuntimeError(f"{label}-Vorlage besitzt keinen eindeutigen Vertragsmarker")
    return payload


def _stable_file_evidence(path: Path) -> tuple[str, int]:
    before = path.stat(follow_symlinks=False)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    after = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise RuntimeError(f"Release-Datei driftete bei der Rechteinventur: {path}")
    return digest.hexdigest(), int(after.st_size)


def _runtime_permissions_contract_payload(
    release_root: Path,
    target_root: Path,
    install_user: str,
    config: dict,
    *,
    web_program_files: tuple[str, ...],
    web_program_directories: tuple[str, ...],
) -> bytes:
    """Versiegelt ausschließlich bekannte Produkt- und feste Runtime-Pfade."""

    from Installer.config_secret_permissions import (
        config_secret_dir_mode,
        config_secret_file_mode,
    )

    release_root = release_root.resolve(strict=True)
    install_entries: list[dict[str, object]] = []
    generated_directory_names = frozenset(
        {"__pycache__", ".pytest_cache", ".venv", "node_modules"}
    )

    def walk_release(directory: Path, relative: tuple[str, ...]) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            if not relative and entry.name == ".git":
                continue
            if entry.name in generated_directory_names:
                continue
            if entry.name.endswith((".pyc", ".pyo")):
                continue
            metadata = entry.stat(follow_symlinks=False)
            child = (*relative, entry.name)
            projected = "/".join(child)
            source = directory / entry.name
            if stat.S_ISDIR(metadata.st_mode):
                install_entries.append(
                    {
                        "path": projected,
                        "kind": "directory",
                        "owner": "install",
                        "group": "www-data",
                        "mode": 0o755,
                    }
                )
                walk_release(source, child)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                with source.open("rb") as handle:
                    executable = handle.read(2) == b"#!"
                source_sha256, source_size = _stable_file_evidence(source)
                install_entries.append(
                    {
                        "path": projected,
                        "kind": "file",
                        "owner": "install",
                        "group": "www-data",
                        "mode": 0o755 if executable else 0o644,
                        "sha256": source_sha256,
                        "size": source_size,
                    }
                )
            elif stat.S_ISLNK(metadata.st_mode):
                # Release-Symlinks bleiben bewusst außerhalb des schreibenden
                # Metadatenpfads; der Voll-Updater bindet sie separat.
                continue
            else:
                raise RuntimeError(
                    f"Ziel-Release enthält einen unsicheren Inventarpfad: {source}"
                )

    walk_release(release_root, ())

    web_entries: list[dict[str, object]] = []
    for relative in web_program_directories:
        web_entries.append(
            {
                "path": relative,
                "kind": "directory",
                "owner": "install",
                "group": "www-data",
                "mode": 0o755,
            }
        )
    for relative in web_program_files:
        source = (
            release_root / relative
            if relative in {"VERSION", "CHANGELOG.md", "UPDATE_POLICY.json"}
            else release_root / "html" / relative
        )
        source_sha256, source_size = _stable_file_evidence(source)
        web_entries.append(
            {
                "path": relative,
                "kind": "file",
                "owner": "install",
                "group": "www-data",
                "mode": 0o644,
                "sha256": source_sha256,
                "size": source_size,
            }
        )

    secret_dir_mode = int(config_secret_dir_mode(config))
    secret_file_mode = int(config_secret_file_mode(config))
    contract = {
        "schema": "e3dc_runtime_permissions_v1",
        "launcher_feature": "e3dc_runtime_permissions_cli_v3",
        "install_user": install_user,
        "install_root": str(target_root),
        "roots": [
            {
                "path": str(target_root),
                "owner": "install",
                "group": "www-data",
                "mode": 0o755,
                "entries": install_entries,
            },
            {
                "path": "/var/www/html",
                "owner": "root",
                "group": "www-data",
                "mode": 0o755,
                "entries": web_entries,
            },
            {
                "path": "/var/www/html/data",
                "owner": "install",
                "group": "www-data",
                "mode": secret_dir_mode,
                "entries": [
                    {
                        "path": "e3dc_v4.json",
                        "kind": "file",
                        "owner": "install",
                        "group": "www-data",
                        "mode": secret_file_mode,
                        "optional": True,
                    },
                    {
                        "path": ".e3dc_v4.transaction.lock",
                        "kind": "file",
                        "owner": "www-data",
                        "group": "www-data",
                        "mode": 0o660,
                        "optional": True,
                    },
                    {
                        "path": "wallbox_mode5_user_start_request.json",
                        "kind": "file",
                        "owner": "www-data",
                        "group": "www-data",
                        "mode": 0o660,
                        "optional": True,
                    },
                    {
                        "path": "wallbox_mode5_user_start_request.json.lock",
                        "kind": "file",
                        "owner": "www-data",
                        "group": "www-data",
                        "mode": 0o660,
                        "optional": True,
                    },
                    {
                        "path": "external_pv_topology.json",
                        "kind": "file",
                        "owner": "install",
                        "group": "www-data",
                        "mode": 0o664,
                        "optional": True,
                    },
                ],
            },
            {
                "path": "/var/www/html/logs",
                "owner": "install",
                "group": "www-data",
                "mode": 0o2775,
                "entries": [],
            },
            {
                "path": "/var/www/html/ramdisk",
                "owner": "install",
                "group": "www-data",
                "mode": 0o2775,
                "entries": [
                    {
                        "path": "manual_soc_wb1.json",
                        "kind": "file",
                        "owner": "install",
                        "group": "www-data",
                        "mode": 0o664,
                        "optional": True,
                    },
                    {
                        "path": "manual_soc_wb2.json",
                        "kind": "file",
                        "owner": "install",
                        "group": "www-data",
                        "mode": 0o664,
                        "optional": True,
                    },
                ],
            },
            {
                "path": "/var/www/html/tmp",
                "owner": "install",
                "group": "www-data",
                "mode": 0o2775,
                "entries": [
                    {
                        "path": "car_charge_session.json",
                        "kind": "file",
                        "owner": "install",
                        "group": "www-data",
                        "mode": 0o664,
                        "optional": True,
                    },
                    {
                        "path": "car_charge_session_wb2.json",
                        "kind": "file",
                        "owner": "install",
                        "group": "www-data",
                        "mode": 0o664,
                        "optional": True,
                    },
                    {
                        "path": "vital_stats.lock",
                        "kind": "file",
                        "owner": "install",
                        "group": "www-data",
                        "mode": 0o664,
                        "optional": True,
                    },
                ],
            },
        ],
    }
    roots = contract["roots"]
    entry_count = sum(len(root.get("entries", ())) for root in roots)
    if len(roots) > 8 or entry_count > 5000:
        raise RuntimeError(
            "Rechtevertrag überschreitet die feste Wurzel- oder Positivlistengrenze"
        )
    payload = (json.dumps(contract, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if len(payload) > 1024 * 1024:
        raise RuntimeError("Rechtevertrag überschreitet die sichere Größenbegrenzung")
    return payload


def _validate_service_wrapper_embedded_python(payload: bytes) -> None:
    """Kompiliert den privilegierten Inline-Pythonblock zusätzlich zu bash -n."""

    begin = b"# E3DC_MATTER_RESET_PYTHON_BEGIN\n"
    end = b"# E3DC_MATTER_RESET_PYTHON_END\n"
    if payload.count(begin) != 1 or payload.count(end) != 1:
        raise RuntimeError("Service-Launcher besitzt keinen eindeutigen Matter-Reset-Pythonblock")
    source = payload.split(begin, 1)[1].split(end, 1)[0]
    try:
        compile(source.decode("utf-8", errors="strict"), "<matter-reset>", "exec")
    except (SyntaxError, UnicodeError) as exc:
        raise RuntimeError("Matter-Reset-Pythonblock im Service-Launcher ist ungültig") from exc


def _canonical_shell_literal(value: str) -> bytes:
    """Kodiert Dispatcherwerte immer in der von der Discovery gelesenen Form."""

    return ("'" + value.replace("'", "'\"'\"'") + "'").encode("utf-8")


def _install_privileged_entrypoints(
    release_root: Path,
    target_root: Path,
    install_user: str,
    config: dict,
    *,
    web_program_files: tuple[str, ...],
    web_program_directories: tuple[str, ...],
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
                RUNTIME_PERMISSIONS_LAUNCHER.parent.parent,
                RUNTIME_PERMISSIONS_LAUNCHER.parent,
                RUNTIME_PERMISSIONS_CONTRACT.parent,
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
    _validate_service_wrapper_embedded_python(service_payload)
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
    permissions_payload = _release_python_script(
        release_root / "Installer/runtime_permissions_repair.py",
        label="Rechte-Launcher",
    )
    permissions_contract_payload = _runtime_permissions_contract_payload(
        release_root,
        target_root,
        install_user,
        config,
        web_program_files=web_program_files,
        web_program_directories=web_program_directories,
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

    _atomic_write_file(
        RUNTIME_PERMISSIONS_LAUNCHER,
        permissions_payload,
        uid=0,
        gid=0,
        mode=0o755,
    )
    _atomic_write_file(
        RUNTIME_PERMISSIONS_CONTRACT,
        permissions_contract_payload,
        uid=0,
        gid=0,
        mode=0o644,
    )
    for path, mode, label in (
        (RUNTIME_PERMISSIONS_LAUNCHER, 0o755, "Rechte-Launcher"),
        (RUNTIME_PERMISSIONS_CONTRACT, 0o644, "Rechtevertrag"),
    ):
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise RuntimeError(f"{label} blieb nach der Installation unsicher")

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
    sudoers_lines.append(
        f"www-data ALL=(root) NOPASSWD: {SERVICE_WRAPPER} "
        f"{MATTER_RESET_ACTION} {MATTER_RESET_UNIT}"
    )
    sudoers_lines.extend(
        (
            f'www-data ALL=(root) NOPASSWD: {WEB_UPDATE_LAUNCHER} ""',
            f'www-data ALL=(root) NOPASSWD: {WEB_UPDATE_LAUNCHER} --check-local-drift-json',
            f'www-data ALL=(root) NOPASSWD: {WEB_UPDATE_LAUNCHER} --confirm-local-drift',
            f'www-data ALL=(root) NOPASSWD: {RUNTIME_PERMISSIONS_LAUNCHER} --check-json',
            f'www-data ALL=(root) NOPASSWD: {RUNTIME_PERMISSIONS_LAUNCHER} ""',
            f'www-data ALL=(root) NOPASSWD: {RUNTIME_PERMISSIONS_LAUNCHER} --confirm-content-drift',
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
    prepared_npm_units: Iterable[str] = (),
    *,
    require_role_peer: bool = False,
    target_binding: DirectoryMetadataPrestate,
    web_program_files: tuple[str, ...] | None = None,
    web_program_directories: tuple[str, ...] | None = None,
) -> list[str]:
    warnings: list[str] = []
    _assert_named_directory_binding(target_binding)
    _delete_approved_stale_paths(
        target_root,
        policy,
        target_binding=target_binding,
    )
    _assert_named_directory_binding(target_binding)
    dropin_payload, ramdisk_warnings = _prepare_ramdisk_dropin(install_user)
    warnings.extend(ramdisk_warnings)
    _ensure_core_services(
        target_root,
        install_user,
        venv_python,
        dropin_payload,
        role,
        (() if service_prestate is None else service_prestate.masked),
        release_root=release_root,
    )
    _ensure_role_service(
        target_root,
        install_user,
        venv_python,
        role,
        dropin_payload,
        release_root=release_root,
        masked_units=(() if service_prestate is None else service_prestate.masked),
    )
    if service_prestate is not None:
        warnings.extend(
            _ensure_selected_catalog_services(
                target_root,
                release_root,
                install_user,
                venv_python,
                role,
                dropin_payload,
                selected_catalog_units
                or (
                    *service_prestate.catalog_active,
                    *service_prestate.catalog_enabled,
                ),
                service_prestate.masked,
                prepared_npm_units,
            )
        )
    _assert_named_directory_binding(target_binding)
    _project_path_metadata(
        target_root,
        target_binding,
        install_user,
        role,
        config,
        require_peer=require_role_peer,
    )
    _assert_named_directory_binding(target_binding)
    _normalize_preserved_web_permissions(
        Path("/var/www/html"),
        install_user,
        config,
    )
    from Installer.permissions import harden_web_program_permissions

    if not harden_web_program_permissions(
        web_root="/var/www/html",
        install_user=install_user,
        program_files=web_program_files,
        program_directories=web_program_directories,
    ):
        raise RuntimeError(
            "Der Web-Programmbaum konnte nicht auf den gemeinsamen Live-Vertrag gehärtet werden"
        )
    _validate_live_install_context(target_root)
    _assert_named_directory_binding(target_binding)
    warnings.extend(
        _install_privileged_entrypoints(
            release_root,
            target_root,
            install_user,
            config,
            web_program_files=tuple(web_program_files or ()),
            web_program_directories=tuple(web_program_directories or ()),
        )
    )

    apache_source = release_root / "Installer/apache/e3dc-control-security.conf"
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
    _assert_named_directory_binding(target_binding)
    return warnings


def _services_to_start(
    policy: dict,
    active_before: tuple[str, ...],
    role: str,
    enabled_before: tuple[str, ...] = (),
    *,
    role_service_intended: bool | None = None,
    masked_units: Iterable[str] = (),
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
    masked = {_normalize_unit(unit) for unit in masked_units}
    seen: set[str] = set()
    return tuple(
        unit
        for unit in services
        if unit not in masked and not (unit in seen or seen.add(unit))
    )


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
    enabled.difference_update(prestate.masked)
    return tuple(sorted(enabled))


def _required_services(
    role: str,
    selected_catalog_units: Iterable[str] = (),
    *,
    role_service_intended: bool = False,
    masked_units: Iterable[str] = (),
) -> tuple[str, ...]:
    from Installer.service_catalog import LOAD_ACTIVE_CONTROL, get_module_by_service, service_load_profile

    selected_units = {_normalize_unit(unit) for unit in selected_catalog_units}
    masked = {_normalize_unit(unit) for unit in masked_units}
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
            return tuple(
                unit
                for unit in dict.fromkeys((role_service, *sorted(selected_units)))
                if unit not in masked
            )
        return (
            (role_service,)
            if role_service_intended and role_service is not None and role_service not in masked
            else ()
        )
    active_controllers = []
    for raw_unit in selected_catalog_units:
        unit = _normalize_unit(raw_unit)
        module = get_module_by_service(unit)
        if module is not None and service_load_profile(module) == LOAD_ACTIVE_CONTROL:
            active_controllers.append(unit)
    return tuple(
        unit
        for unit in dict.fromkeys(
            (
                *CORE_RESULT_SERVICES,
                *active_controllers,
            )
        )
        if unit not in masked
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
        *prestate.cutover_unknown_units,
        *prestate.masked,
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
    units.difference_update(prestate.masked)
    return tuple(sorted(units))


def _emergency_storage_latch_present() -> bool:
    from Installer.emergency_release import emergency_latch_present

    return emergency_latch_present()


def _required_services_with_emergency_veto(
    required: Iterable[str],
) -> tuple[set[str], bool]:
    effective = {_normalize_unit(unit) for unit in required}
    latched = _emergency_storage_latch_present()
    if latched:
        effective.discard("e3dc-storage-manager.service")
    return effective, latched


def _probe_emergency_storage_writer_state() -> tuple[str, str]:
    """Beweist den Storage-Stillstand ohne die globale Aktivsemantik zu ändern."""

    unit = "e3dc-storage-manager.service"
    try:
        result = _run(
            [
                "/usr/bin/systemctl",
                "show",
                "--no-pager",
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
                "--property=ControlPID",
                unit,
            ],
            timeout=20,
        )
    except Exception as exc:
        return "unknown", str(exc).strip() or exc.__class__.__name__
    detail = (result.stderr or result.stdout or f"Exit {result.returncode}").strip()
    if result.returncode != 0:
        return "unknown", detail

    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {
            "ActiveState",
            "SubState",
            "MainPID",
            "ControlPID",
        }:
            values[key] = value.strip()
    if set(values) != {"ActiveState", "SubState", "MainPID", "ControlPID"}:
        return "unknown", "systemctl show lieferte keinen vollständigen Zustandsvertrag"
    try:
        main_pid = int(values["MainPID"])
        control_pid = int(values["ControlPID"])
    except (TypeError, ValueError):
        return "unknown", "systemctl show lieferte keine gültigen Prozess-IDs"
    if main_pid < 0 or control_pid < 0:
        return "unknown", "systemctl show lieferte negative Prozess-IDs"

    active = values["ActiveState"].lower()
    sub = values["SubState"].lower()
    state_detail = (
        f"{active}/{sub}, MainPID={main_pid}, ControlPID={control_pid}"
    )
    if (
        active == "inactive"
        and sub in {"dead", "exited"}
        and main_pid == 0
        and control_pid == 0
    ):
        return "inactive", state_detail
    if active in {
        "active",
        "activating",
        "deactivating",
        "failed",
        "reloading",
        "maintenance",
        "inactive",
    } or main_pid > 0 or control_pid > 0:
        return "not-inactive", state_detail
    return "unknown", state_detail


def _require_emergency_storage_writer_inactive(
    *,
    code: str,
    phase: str,
) -> None:
    """Akzeptiert bei aktivem Incident ausschließlich belegten Writer-Stillstand."""

    writer_state, writer_detail = _probe_emergency_storage_writer_state()
    if writer_state == "inactive":
        return
    if writer_state == "unknown":
        happened = (
            f"Der Emergency-Latch ist aktiv, aber der Storage-Writer ist {phase} "
            f"nicht sicher lesbar: {writer_detail}"
        )
    else:
        happened = (
            f"Der Storage-Writer ist trotz aktivem Emergency-Latch {phase} "
            f"nicht nachweislich stillgesetzt: {writer_detail}"
        )
    _fail(
        code,
        happened,
        "Der Incident bleibt verriegelt. Prüfe systemctl show "
        "e3dc-storage-manager.service sowie die lokale systemd-/DBus-Verbindung; "
        "führe keinen manuellen Hardwarebefehl aus.",
        system_state=(
            "Der persistente Incident-Latch bleibt aktiv; der Storage-Writer-"
            "Stillstand ist jedoch unbestätigt und der Updatepfad wurde "
            "fail-closed angehalten."
        ),
    )


def _prepare_active_emergency_veto_before_update() -> bool:
    """Bindet einen vorhandenen Incident vor Backup und Versionswechsel."""

    if not _emergency_storage_latch_present():
        return False
    from Installer.emergency_release import ensure_persistent_emergency_start_veto

    try:
        ensure_persistent_emergency_start_veto()
    except Exception as exc:
        _fail(
            "E3DC-UPD-EMERGENCY-000",
            f"Der aktive Emergency-Latch konnte nicht an seinen root-eigenen Startschutz gebunden werden: {exc}",
            "Prüfe Eigentümer, Modus und Inhalt von /etc/systemd/system-generators/e3dc-emergency-quiesce-generator sowie dem Storage-Drop-in; der Incident-Latch darf nicht entfernt werden.",
            system_state=(
                "Der Incident-Latch ist aktiv. Seine rebootfeste systemd-Sperre "
                "und der Stillstand des Storage-Writers sind noch nicht bestätigt; "
                "Produktdateien wurden nicht verändert."
            ),
        )
    reloaded = _run(["/usr/bin/systemctl", "daemon-reload"], timeout=60)
    if reloaded.returncode != 0:
        _fail(
            "E3DC-UPD-EMERGENCY-001",
            "Der persistente Emergency-Startschutz konnte nicht in systemd geladen werden.",
            "Prüfe systemctl daemon-reload und den root-eigenen Drop-in von e3dc-storage-manager.service; der Incident-Latch bleibt aktiv.",
            system_state=(
                "Der Incident-Latch ist aktiv und der Startschutz wurde auf Platte "
                "projiziert, seine systemd-Wirksamkeit sowie der Writer-Stillstand "
                "sind jedoch unbestätigt; Produktdateien wurden nicht verändert."
            ),
        )
    writer_state, writer_detail = _probe_emergency_storage_writer_state()
    if writer_state == "unknown":
        _fail(
            "E3DC-UPD-EMERGENCY-003",
            "Der Emergency-Latch ist aktiv, aber der Zustand des Storage-Writers "
            f"konnte nicht sicher gelesen werden: {writer_detail}",
            "Der Incident bleibt verriegelt. Prüfe systemctl show "
            "e3dc-storage-manager.service und die lokale systemd-/DBus-Verbindung; "
            "führe keinen manuellen Hardwarebefehl aus.",
            system_state=(
                "Der Incident-Latch und sein systemd-Startschutz sind aktiv, "
                "der Stillstand des Storage-Writers ist jedoch unbestätigt; "
                "Produktdateien wurden nicht verändert."
            ),
        )
    if writer_state != "inactive":
        stopped = _run(
            ["/usr/bin/systemctl", "stop", "e3dc-storage-manager.service"],
            timeout=90,
        )
        writer_state, writer_detail = _probe_emergency_storage_writer_state()
        if stopped.returncode != 0 or writer_state != "inactive":
            stop_detail = (
                stopped.stderr or stopped.stdout or f"Exit {stopped.returncode}"
            ).strip()
            _fail(
                "E3DC-UPD-EMERGENCY-002",
                "Der Emergency-Latch ist aktiv, aber der Storage-Writer ließ sich "
                f"nicht nachweislich stillsetzen ({writer_detail}; {stop_detail}).",
                "Der Incident bleibt verriegelt. Prüfe sudo systemctl status e3dc-storage-manager.service und führe keinen manuellen Hardwarebefehl aus.",
                system_state=(
                    "Der Incident-Latch und sein systemd-Startschutz sind aktiv, "
                    "der Storage-Writer kann aber weiterhin laufen; Produktdateien "
                    "wurden nicht verändert."
                ),
            )
    print(
        "[INFO] Emergency-Quiesce ist aktiv: Der Storage-Writer bleibt beim "
        "Update und bei einem Rücklauf absichtlich gesperrt.",
        flush=True,
    )
    return True


def _rebind_active_emergency_veto_for_recovery() -> tuple[bool, str]:
    """Hält den Incident auch bei unvollständigem Versionsrücklauf monoton."""

    if not _emergency_storage_latch_present():
        return True, ""
    try:
        from Installer.emergency_release import ensure_persistent_emergency_start_veto

        ensure_persistent_emergency_start_veto()
        reloaded = _run(["/usr/bin/systemctl", "daemon-reload"], timeout=60)
        if reloaded.returncode != 0:
            raise RuntimeError("systemd daemon-reload fehlgeschlagen")
        writer_state, writer_detail = _probe_emergency_storage_writer_state()
        if writer_state == "unknown":
            raise RuntimeError(
                "Storage-Writer-Zustand blieb trotz Incident unbestätigt: "
                + writer_detail
            )
        if writer_state != "inactive":
            stopped = _run(
                ["/usr/bin/systemctl", "stop", "e3dc-storage-manager.service"],
                timeout=90,
            )
            writer_state, writer_detail = _probe_emergency_storage_writer_state()
            if stopped.returncode != 0 or writer_state != "inactive":
                raise RuntimeError(
                    "Storage-Writer blieb trotz Incident unbestätigt: "
                    + writer_detail
                )
    except Exception as exc:
        return False, str(exc).strip() or exc.__class__.__name__
    return True, ""


def _start_services(
    services: tuple[str, ...],
    *,
    required: tuple[str, ...],
    enable_services: tuple[str, ...],
    masked_units: Iterable[str] = (),
) -> list[str]:
    print(
        "[4/4] Regelung und Weboberfläche werden neu gestartet und geprüft …",
        flush=True,
    )
    reload_result = _run(["/usr/bin/systemctl", "daemon-reload"], timeout=60)
    if reload_result.returncode != 0:
        _fail(
            "E3DC-UPD-SERVICE-RELOAD-002",
            "systemd konnte die neuen Dienstdefinitionen nicht laden.",
            "Führe sudo systemctl daemon-reload aus und starte danach denselben Updatebefehl erneut.",
        )
    warnings: list[str] = []
    masked = {_normalize_unit(unit) for unit in masked_units}
    required_set = (set(required) | {"apache2.service"}) - masked
    enable_set = set(enable_services) - masked
    storage_unit = "e3dc-storage-manager.service"
    for unit in services:
        if unit in masked:
            continue
        if unit == storage_unit and _emergency_storage_latch_present():
            # Unmittelbar vor dem Zielstart zählt der persistente Incident als
            # absichtliche Writersperre, nicht als fehlgeschlagener Pflichtstart.
            required_set.discard(storage_unit)
            _require_emergency_storage_writer_inactive(
                code="E3DC-UPD-EMERGENCY-005",
                phase="unmittelbar vor dem Zielstart",
            )
            continue
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
    target_binding: DirectoryMetadataPrestate,
    expected_version: str,
    required_services: tuple[str, ...],
    forbidden_services: Iterable[str] = (),
    *,
    required_writer_admission_mode: str = "",
    masked_units: Iterable[str] = (),
) -> list[str]:
    _assert_named_directory_binding(target_binding)
    version = (target_root / "VERSION").read_text(encoding="utf-8").strip()
    _assert_named_directory_binding(target_binding)
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

    masked = {_normalize_unit(unit) for unit in masked_units}
    required = (set(required_services) | {"apache2.service"}) - masked
    storage_unit = "e3dc-storage-manager.service"
    forbidden = {_normalize_unit(unit) for unit in forbidden_services}
    pending = set(required)
    effective_required = set(required)
    deadline = time.monotonic() + 30.0
    green_since: float | None = None
    stable = False
    while time.monotonic() < deadline:
        effective_required, emergency_latched = _required_services_with_emergency_veto(
            required
        )
        if emergency_latched:
            _require_emergency_storage_writer_inactive(
                code="E3DC-UPD-EMERGENCY-004",
                phase="während der Abschlussprüfung",
            )
        competing = {unit for unit in forbidden if _service_active(unit)}
        if competing:
            unit = sorted(competing)[0]
            _fail(
                "E3DC-UPD-END-005",
                "Ein abgelöster alter oder konkurrierender Dienst wurde nach dem Neustart wieder aktiv: "
                + ", ".join(sorted(competing)),
                f"Stoppe und deaktiviere ihn mit: sudo systemctl disable --now {unit}",
            )
        pending = {
            unit for unit in effective_required if not _service_active(unit)
        }
        role_service = ROLE_SERVICE_BY_MODE.get(required_writer_admission_mode)
        writer_admission_required = bool(
            required_writer_admission_mode
            and any(
                unit not in {role_service, "apache2.service"}
                for unit in effective_required
            )
        )
        if writer_admission_required:
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
        unstable = sorted(pending or effective_required)
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
        _assert_named_directory_binding(target_binding)
        return [
            "Apache läuft, aber http://127.0.0.1/index.php antwortet nicht mit "
            "HTTP 2xx oder einer lokalen Weiterleitung. Eine abweichende lokale "
            "Port-/VirtualHost-Konfiguration kann die Ursache sein; prüfe bei Bedarf "
            "sudo journalctl -u apache2 --no-pager -n 120."
        ]
    _assert_named_directory_binding(target_binding)
    return []


def _postflight_python_programming_warnings(
    units: Iterable[str],
) -> list[str]:
    """Meldet frische interne Python-Fehler rein diagnostisch.

    Die systemd-Invocation bindet den Befund an genau den soeben gestarteten
    Prozess. Journalfehler und fachliche Laufzeitfehler verändern weder das
    strukturelle Endgate noch den bestätigten Releasezustand.
    """

    programming_error = re.compile(
        r"(?:\b(?:NameError|UnboundLocalError|SyntaxError|IndentationError|TabError)\s*:"
        r"|\bname ['\"][A-Za-z_][A-Za-z0-9_]*['\"] is not defined\b"
        r"|\bcannot access local variable ['\"][^'\"]+['\"] where it is not associated with a value\b"
        r"|\blocal variable ['\"][^'\"]+['\"] referenced before assignment\b)"
    )
    loop_exception = re.compile(
        r"(?:Fehler im Regel-Durchlauf|Unerwarteter Fehler im [^:]*Loop|"
        r"Fehler im (?:[^: ]+ )?Loop):\s*(.+)",
        re.IGNORECASE,
    )
    warnings: list[str] = []
    seen: set[str] = set()
    for raw_unit in units:
        unit = _normalize_unit(raw_unit)
        if not raw_unit or unit in seen or _is_update_runtime_unit(unit):
            continue
        seen.add(unit)
        try:
            if not _service_active(unit):
                continue
            invocation_result = _run(
                [
                    "/usr/bin/systemctl",
                    "show",
                    unit,
                    "--property=InvocationID",
                    "--value",
                ],
                timeout=5,
            )
            invocation_id = invocation_result.stdout.strip().lower()
            if (
                invocation_result.returncode != 0
                or not re.fullmatch(r"[0-9a-f]{32}", invocation_id)
                or invocation_id == "0" * 32
            ):
                continue
            journal = _run(
                [
                    "/usr/bin/journalctl",
                    "--quiet",
                    "--no-pager",
                    "--output=cat",
                    "-u",
                    unit,
                    f"_SYSTEMD_INVOCATION_ID={invocation_id}",
                    "-n",
                    "200",
                ],
                timeout=5,
            )
            if journal.returncode != 0:
                continue
            journal_text = journal.stdout or ""
            has_programming_error = programming_error.search(journal_text) is not None
            loop_counts: dict[str, int] = {}
            for line in journal_text.splitlines():
                match = loop_exception.search(line)
                if match is None:
                    continue
                signature = match.group(1).strip()
                if signature:
                    loop_counts[signature] = loop_counts.get(signature, 0) + 1
            repeated_loop_exception = any(count >= 2 for count in loop_counts.values())
            if not has_programming_error and not repeated_loop_exception:
                continue
            reasons = []
            if has_programming_error:
                reasons.append("einen eindeutigen internen Python-Programmierfehler")
            if repeated_loop_exception:
                reasons.append("eine wiederholte frische Loop-Ausnahme")
            journal_command = (
                f"sudo journalctl -u {shlex.quote(unit)} "
                f"_SYSTEMD_INVOCATION_ID={invocation_id} --no-pager -n 120"
            )
            warnings.append(
                f"{unit} meldet in der aktuellen systemd-Invocation "
                f"{' und '.join(reasons)}. Das Update bleibt bestätigt. "
                f"Details: {journal_command}"
            )
        except Exception:
            # Diese Nachdiagnose ist ausdrücklich kein neues Update-Endgate.
            continue
    return warnings


def _start_previous_services_best_effort(
    active_before: tuple[str, ...],
    *,
    allow_legacy: bool,
    role: str = "",
    extra_units: Iterable[str] = (),
    enabled_before: Iterable[str] = (),
    exact_prestate: bool = False,
) -> tuple[str, ...]:
    from Installer.service_catalog import allowed_services

    storage_unit = "e3dc-storage-manager.service"
    if _emergency_storage_latch_present():
        from Installer.emergency_release import ensure_persistent_emergency_start_veto

        # Ein Backup kann eine alte Storage-Unit samt altem Drop-in-Verzeichnis
        # restaurieren. Vor daemon-reload wird das versionsunabhängige Veto
        # deshalb erneut projiziert.
        ensure_persistent_emergency_start_veto()
    restartable = {_normalize_unit(item) for item in allowed_services()}
    catalog = set(restartable)
    restartable.update(EXPLICIT_CUTOVER_SERVICES)
    extra_set = {_normalize_unit(item) for item in extra_units}
    enabled_set = {_normalize_unit(item) for item in enabled_before}
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
    role_managed = {
        unit
        for unit in ordered
        if role_was_active
        and role in {"master", "slave"}
        and unit in catalog
        and unit in enabled_set
        and unit != selected_role_service
        and _service_exists(unit)
    }
    for unit in ordered:
        if unit == storage_unit and _emergency_storage_latch_present():
            continue
        if unit in restartable and _service_exists(unit):
            # Ein fehlgeschlagener Zielstart darf den bestätigten Altstand
            # nicht anschließend über systemds StartLimit blockieren.
            _run(["/usr/bin/systemctl", "reset-failed", unit], timeout=20)
    for unit in ordered:
        if unit not in restartable:
            continue
        if unit == storage_unit and _emergency_storage_latch_present():
            # Live-Recheck unmittelbar vor dem Rücklaufstart: Der frühere
            # Aktivzustand wird bei aktivem Incident bewusst nicht restauriert.
            # Falls der Latch erst nach Eintritt in diese Funktion entstand,
            # muss auch das versionsunabhängige Reboot-Veto jetzt noch gebunden
            # werden; ein bloßes Überspringen dieses einen Starts wäre nicht
            # rollbackfest.
            veto_ok, _veto_detail = _rebind_active_emergency_veto_for_recovery()
            if not veto_ok:
                failed.append(unit)
                continue
            if _emergency_storage_latch_present():
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
        if unit in role_managed:
            # Bei Master/Slave übernimmt ausschließlich der zuvor aktive
            # HA-Manager den ebenfalls zuvor aktivierten Katalog-Dienstsatz.
            # Es gibt hier bewusst kein zweites Zeit-Endgate: Peerprüfung und
            # Auto-Recovery dürfen länger dauern, ohne den Rücklauf erneut als
            # fehlgeschlagen zu deklarieren. Shadow verwaltet diese Dienste
            # nicht; zuvor aktive, aber deaktivierte Dienste werden ebenfalls
            # direkt wiederhergestellt.
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
    """Stellt den gebundenen persistenten/runtime Enable-Zustand wieder her."""

    desired_states = prestate.enable_state_map
    desired_fragments = prestate.fragment_path_map
    masked = set(prestate.masked)
    enabled_states = {"enabled", "enabled-runtime", "linked", "linked-runtime"}
    for raw_unit in dict.fromkeys(_normalize_unit(item) for item in touched_units):
        unit = _normalize_unit(raw_unit)
        if unit in masked:
            continue
        desired = desired_states.get(
            unit,
            "enabled" if unit in set(prestate.enabled) else "disabled",
        )
        current = _service_enable_state(unit)
        fragment = desired_fragments.get(unit, "")
        fragment_matches = not (
            desired in enabled_states
            and fragment
            and _service_fragment_path(unit) != fragment
        )
        if current == desired and fragment_matches:
            continue
        _run(["/usr/bin/systemctl", "disable", "--runtime", unit], timeout=60)
        _run(["/usr/bin/systemctl", "disable", unit], timeout=60)
        if desired in {"enabled", "enabled-runtime"}:
            command = ["/usr/bin/systemctl", "enable"]
            if desired == "enabled-runtime":
                command.append("--runtime")
            command.append(fragment or unit)
            _run(command, timeout=60)
        elif desired in {"linked", "linked-runtime"} and fragment:
            command = ["/usr/bin/systemctl", "link"]
            if desired == "linked-runtime":
                command.append("--runtime")
            command.append(fragment)
            _run(command, timeout=60)


def _service_enablement_mismatches(
    prestate: ServicePrestate,
    units: Iterable[str],
) -> tuple[str, ...]:
    """Vergleicht Zustand und Linkquelle ohne masked-Zustände zu duplizieren."""

    desired_states = prestate.enable_state_map
    desired_fragments = prestate.fragment_path_map
    masked = set(prestate.masked)
    mismatches: list[str] = []
    for raw_unit in dict.fromkeys(_normalize_unit(item) for item in units):
        unit = _normalize_unit(raw_unit)
        if unit in masked:
            continue
        desired = desired_states.get(
            unit,
            "enabled" if unit in set(prestate.enabled) else "disabled",
        )
        actual = _service_enable_state(unit)
        if actual != desired:
            mismatches.append(f"{unit}={actual or 'leer'} statt {desired or 'leer'}")
            continue
        expected_fragment = desired_fragments.get(unit, "")
        if (
            desired in {"enabled", "enabled-runtime", "linked", "linked-runtime"}
            and expected_fragment
        ):
            actual_fragment = _service_fragment_path(unit)
            if actual_fragment != expected_fragment:
                mismatches.append(
                    f"{unit}=FragmentPath {actual_fragment or 'leer'} statt {expected_fragment}"
                )
    return tuple(mismatches)


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
    install_user: str,
    config: dict,
    role: str,
    home_traversal_transition: tuple[DirectoryMetadataTransition, ...] | None,
    target_prebinding: DirectoryMetadataPrestate,
) -> tuple[bool, str]:
    """Stellt nach begonnener Zielmutation den gesicherten Altstand wieder her."""

    traversal_restore_error = ""
    try:
        _restore_web_home_traversal(home_traversal_transition)
    except Exception as exc:
        traversal_restore_error = str(exc).strip() or exc.__class__.__name__

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
        traversal_detail = (
            "; zusätzlich blieben die früheren Pfad-Traversierrechte abweichend: "
            + traversal_restore_error
            if traversal_restore_error
            else ""
        )
        return (
            False,
            "Der automatische Rücklauf begann nicht, weil diese Zieldienste nicht "
            "sicher gestoppt werden konnten: "
            + ", ".join(stop_failed)
            + traversal_detail,
        )

    try:
        from Installer.backup import restore_quiesced_overlay, restore_verified_backup
        from Installer.backup_integrity import bind_persistent_install_root

        def assert_product_identity(*_args: object) -> None:
            _assert_named_directory_identity(target_prebinding)

        expected_identity = (
            target_prebinding.parent_device,
            target_prebinding.parent_inode,
            target_prebinding.device,
            target_prebinding.inode,
        )
        with bind_persistent_install_root(
            target_root,
            expected_identity=expected_identity,
        ) as bound_install_root:
            assert_product_identity()
            restore_verified_backup(
                backup_path,
                install_path=target_root,
                verified_manifest_guard=assert_product_identity,
                restored_payload_guard=assert_product_identity,
                bound_install_root=bound_install_root,
            )
            assert_product_identity()
            restore_quiesced_overlay(
                quiesced_backup,
                install_path=target_root,
                guard=quiesced_guard,
                restored_payload_guard=assert_product_identity,
                bound_install_root=bound_install_root,
            )
            assert_product_identity()
            _clear_legacy_update_blockers(backup_path)
            # Die Zielprojektion schützt erhaltene Laufzeitverzeichnisse während
            # des Austauschs vor parallelen Schreibern. Nach einem Rücklauf müssen
            # deren veröffentlichte Alt-Rechte vor jedem Dienststart wieder gelten.
            _normalize_preserved_web_permissions(
                Path("/var/www/html"),
                install_user,
                config,
            )
            assert_product_identity()
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        veto_ok, veto_detail = _rebind_active_emergency_veto_for_recovery()
        traversal_detail = (
            "; frühere Pfad-Traversierrechte blieben ebenfalls abweichend: "
            + traversal_restore_error
            if traversal_restore_error
            else ""
        )
        return (
            False,
            "Der automatische Rücklauf aus Vollbackup und ruhender "
            f"Daten-Nachsicherung blieb unvollständig ({detail}). Die E3DC-Control-"
            "Dienste bleiben gestoppt"
            + traversal_detail
            + (
                "; der persistente Emergency-Startschutz blieb ebenfalls unbestätigt: "
                + veto_detail
                if not veto_ok
                else ""
            )
            + ".",
        )

    veto_ok, veto_detail = _rebind_active_emergency_veto_for_recovery()
    if not veto_ok:
        return (
            False,
            "Der gesicherte Datei- und Webstand wurde wiederhergestellt, aber der "
            "persistente Emergency-Startschutz konnte vor keinem weiteren "
            f"Dienststart bestätigt werden ({veto_detail}). Die Dienste bleiben gestoppt.",
        )

    if traversal_restore_error:
        return (
            False,
            "Der gesicherte Datei- und Webstand wurde wiederhergestellt, aber die "
            "früheren Pfad-Traversierrechte blieben abweichend: "
            + traversal_restore_error
            + ". Die E3DC-Control-Dienste bleiben gestoppt.",
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

    mask_mismatch = _restore_service_masks_best_effort(prestate)
    if mask_mismatch:
        return (
            False,
            "Der gesicherte Dateistand wurde wiederhergestellt, aber der frühere "
            "systemd-Maskenzustand blieb abweichend: "
            + "; ".join(mask_mismatch)
            + ". Die E3DC-Control-Dienste bleiben gestoppt.",
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
    enablement_mismatch = _service_enablement_mismatches(
        prestate,
        prestate.present,
    )
    if enablement_mismatch:
        return (
            False,
            "Der gesicherte Dateistand wurde wiederhergestellt, aber der frühere "
            "systemd-Enable-Zustand blieb für folgende Dienste abweichend: "
            + ", ".join(enablement_mismatch)
            + ". Die E3DC-Control-Dienste bleiben gestoppt.",
        )

    try:
        _assert_named_directory_identity(target_prebinding)
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        return (
            False,
            "Der Altstand wurde restauriert, aber sein gebundener Produktstamm "
            f"driftete vor dem Dienststart ({detail}). Die Dienste bleiben gestoppt.",
        )

    start_failed = _start_previous_services_best_effort(
        prestate.active,
        allow_legacy=True,
        role=role,
        extra_units=prestate.cutover_unknown_units,
        enabled_before=prestate.enabled,
        exact_prestate=True,
    )
    if start_failed:
        return (
            False,
            "Produktstand, ruhende Nutzerdaten und Dienstfreigaben wurden "
            "wiederhergestellt; folgende zuvor aktive Dienste starteten jedoch nicht: "
            + ", ".join(start_failed),
        )
    emergency_detail = (
        " Der Storage-Writer blieb wegen des weiterhin aktiven Emergency-Latch "
        "absichtlich gesperrt."
        if _emergency_storage_latch_present()
        else ""
    )
    return (
        True,
        "Der gesicherte Produktstand einschließlich der ruhenden Nutzerdaten und "
        "der zuvor aktive rollenrichtige Dienstsatz wurden wiederhergestellt."
        + emergency_detail,
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
    install_user: str,
    config: dict,
    role: str,
    product_mutated: bool,
    target_prebinding: DirectoryMetadataPrestate | None,
    replacement_confirmed: bool,
    home_traversal_transition: tuple[DirectoryMetadataTransition, ...] | None,
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
            or target_prebinding is None
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
            install_user=install_user,
            config=config,
            role=role,
            home_traversal_transition=home_traversal_transition,
            target_prebinding=target_prebinding,
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
            prestate.cutover_unknown_units if prestate is not None else ()
        ),
        enabled_before=(prestate.enabled if prestate is not None else ()),
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
        veto_ok, veto_detail = _rebind_active_emergency_veto_for_recovery()
        return (
            "Der automatische Rücklauf konnte nicht vollständig ausgeführt werden "
            f"({detail}). E3DC-Control-Dienste können gestoppt sein. Vollbackup: "
            f"{kwargs.get('backup_path') or 'nicht verfügbar'}; ruhende "
            f"Daten-Nachsicherung: {kwargs.get('quiesced_backup') or 'nicht verfügbar'}"
            + (
                "; der persistente Emergency-Startschutz blieb unbestätigt: "
                + veto_detail
                if not veto_ok
                else ""
            ),
            "Starte denselben Ein-Datei-Updater erneut; er repariert den Zielstand aus dem veröffentlichten Release.",
        )


def _consume_update_drift_confirmation_token(confirmation: str) -> str:
    """Liest die root-eigene Einmal-Credential ohne argv-/Log-Offenlegung."""

    global _UPDATE_DRIFT_CONFIRM_TOKEN
    if _UPDATE_DRIFT_CONFIRM_TOKEN is not None:
        return _UPDATE_DRIFT_CONFIRM_TOKEN
    if confirmation != "1":
        _UPDATE_DRIFT_CONFIRM_TOKEN = ""
        return ""
    parent = UPDATE_DRIFT_CONFIRM_FILE.parent
    try:
        runtime_parent_metadata = os.lstat(parent.parent)
        parent_metadata = os.lstat(parent)
        metadata = os.lstat(UPDATE_DRIFT_CONFIRM_FILE)
    except OSError:
        _fail(
            "E3DC-UPD-DRIFT-002",
            "Die private Dateilistenbindung der Driftfreigabe fehlt.",
            "Starte die Nur-Lese-Prüfung erneut und übergib deren Token per "
            "Standardeingabe an den veröffentlichten Launcher.",
        )
    if (
        not stat.S_ISDIR(runtime_parent_metadata.st_mode)
        or stat.S_ISLNK(runtime_parent_metadata.st_mode)
        or runtime_parent_metadata.st_uid != 0
        or runtime_parent_metadata.st_gid != 0
        or stat.S_IMODE(runtime_parent_metadata.st_mode) & 0o022
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != 0
        or parent_metadata.st_gid != 0
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size != 65
    ):
        _fail(
            "E3DC-UPD-DRIFT-002",
            "Die private Dateilistenbindung besitzt unsichere Metadaten.",
            "Verwirf den fehlerhaften Updateauftrag und starte die Nur-Lese-Prüfung erneut.",
        )
    descriptor = os.open(
        UPDATE_DRIFT_CONFIRM_FILE,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        payload = os.read(descriptor, 66)
        rebound = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    named = os.stat(UPDATE_DRIFT_CONFIRM_FILE, follow_symlinks=False)
    if (
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (rebound.st_dev, rebound.st_ino, rebound.st_size, rebound.st_mtime_ns)
        or (rebound.st_dev, rebound.st_ino) != (named.st_dev, named.st_ino)
    ):
        _fail(
            "E3DC-UPD-DRIFT-002",
            "Die private Dateilistenbindung driftete beim Lesen.",
            "Starte die Nur-Lese-Prüfung erneut.",
        )
    try:
        UPDATE_DRIFT_CONFIRM_FILE.unlink()
    except OSError:
        _fail(
            "E3DC-UPD-DRIFT-002",
            "Die Einmal-Credential konnte nach dem Lesen nicht verworfen werden.",
            "Prüfe /run/e3dc-update-credentials und starte danach erneut.",
        )
    try:
        token = payload.decode("ascii", errors="strict").removesuffix("\n")
    except UnicodeError:
        token = ""
    if (
        len(payload) != 65
        or not payload.endswith(b"\n")
        or len(token) != 64
        or any(character not in "0123456789abcdef" for character in token)
    ):
        _fail(
            "E3DC-UPD-DRIFT-002",
            "Die Dateilistenbindung der Driftfreigabe ist ungültig.",
            "Starte die Nur-Lese-Prüfung erneut und bestätige genau deren aktuelle Dateiliste.",
        )
    _UPDATE_DRIFT_CONFIRM_TOKEN = token
    return token


def _verify_update_drift_authority(
    target_root: Path,
    release_root: Path,
    *,
    phase: str,
    emit_details: bool,
) -> dict:
    """Bindet die Überschreibfreigabe exakt an Release, Liste und Fingerprints."""

    from Installer.update_drift import inspect_update_drift

    confirmation = str(os.environ.get(UPDATE_DRIFT_CONFIRM_ENV) or "0")
    if confirmation not in {"0", "1"}:
        _fail(
            "E3DC-UPD-DRIFT-002",
            "Die Freigabe lokaler Inhaltsabweichungen besitzt einen ungültigen Wert.",
            "Starte den veröffentlichten Updater ohne fremde Umgebungsvariablen erneut.",
        )
    confirmation_token = _consume_update_drift_confirmation_token(confirmation)

    drift_result = inspect_update_drift(
        target_root=target_root,
        release_root=release_root,
    )
    baseline_complete = bool(drift_result.get("baseline_complete"))
    missing_files = list(drift_result.get("missing_product_files") or ())
    content_drift = list(drift_result.get("content_drift") or ())
    requires_confirmation = bool(drift_result.get("requires_confirmation"))
    expected_confirmation_token = str(
        drift_result.get("confirmation_token") or ""
    )

    if emit_details and missing_files:
        print(
            f"[INFO] {len(missing_files)} veröffentlichte Produktdatei(en) "
            "fehlen und werden aus dem Zielrelease wiederhergestellt:",
            flush=True,
        )
        for item in missing_files:
            print(f"  - {item.get('path', '')}", flush=True)
    if emit_details and content_drift:
        print(
            f"[WARNUNG] {len(content_drift)} lokal veränderte oder "
            "kollidierende Produktdatei(en) würden durch das Zielrelease "
            "ersetzt oder als ausdrücklich freigegebener Altpfad gelöscht:",
            flush=True,
        )
        for item in content_drift:
            print(
                f"  - {item.get('path', '')} ({item.get('status', 'abweichend')})",
                flush=True,
            )
    if emit_details and not baseline_complete:
        print(
            "[WARNUNG] Für diesen Altbestand konnte weder ein sicherer "
            "root-eigener Inhaltsvertrag noch der veröffentlichte Tag der "
            "installierten Version als Altbaseline gebunden werden. Eine "
            "bewusste generische Freigabe ist erforderlich; unbekannte Dateien "
            "außerhalb der Zielprojektion bleiben unberührt.",
            flush=True,
        )

    authority_matches = (
        confirmation == "1"
        and bool(expected_confirmation_token)
        and confirmation_token == expected_confirmation_token
    )
    # Auch eine inzwischen verschwundene oder sonst veränderte Liste macht eine
    # bereits erteilte Freigabe ungültig. Sie wird niemals auf einen neuen
    # Zielzustand übertragen.
    stale_authority = confirmation == "1" and (
        not requires_confirmation or not authority_matches
    )
    if (requires_confirmation and not authority_matches) or stale_authority:
        reason = (
            "Lokal geänderte Produktinhalte wurden erkannt."
            if content_drift
            else (
                "Die veröffentlichte Altbaseline konnte nicht sicher gebunden werden."
                if not baseline_complete
                else "Die zuvor bestätigte Inhaltsliste hat sich geändert."
            )
        )
        _fail(
            "E3DC-UPD-DRIFT-001",
            f"{reason} Die bestätigte Liste samt Fingerprints passt in der "
            f"Prüfphase {phase} nicht exakt zum aktuellen Stand; ohne neue "
            "Freigabe werden keine Produktdateien überschrieben.",
            "Starte die Nur-Lese-Prüfung erneut. An der Konsole liefert "
            "sudo /usr/local/sbin/e3dc-web-update-launcher "
            "--check-local-drift-json den gebundenen confirmation_token; "
            "übergib ausschließlich diesen Token per Standardeingabe an "
            "denselben Launcher mit --confirm-local-drift.",
        )
    if requires_confirmation:
        print(
            "[BESTÄTIGT] Die Überschreibfreigabe stimmt in der Prüfphase "
            f"{phase} exakt mit Zielrelease, Liste und Inhaltsfingerprints überein.",
            flush=True,
        )
    return drift_result


def perform_update(
    *,
    target_root: Path,
    install_user: str,
    tag: str,
    role: str,
    bound_peer_ip: str = "",
) -> int:
    update_started_at = time.monotonic()
    cutover_started_at: float | None = None
    services_confirmed_at: float | None = None
    _assert_root_update_authority()
    emergency_enforced = _prepare_active_emergency_veto_before_update()
    release_root = RELEASE_ROOT
    try:
        policy = _load_policy(release_root)
        _validate_inputs(target_root, release_root, install_user, tag)
        lock_descriptor = _acquire_update_lock()
    except UpdateFailure as exc:
        if emergency_enforced:
            raise UpdateFailure(
                code=exc.code,
                happened=exc.happened,
                solution=exc.solution,
                system_state=(
                    "Der Incident-Latch ist aktiv und der Storage-Writer wurde vor "
                    "dem Update-Preflight absichtlich gestoppt. Produktdateien "
                    "wurden nicht verändert."
                ),
            ) from exc
        raise
    try:
        try:
            _prepare_full_update_recovery_entry(target_root, lock_descriptor)
        finally:
            emergency_enforced = _rebind_emergency_veto_after_recovery(
                emergency_enforced
            )
        target_prebinding = _read_bound_directory_prestate(target_root)
        _preflight_web_home_traversal(install_user, target_root)
        _assert_named_directory_binding(target_prebinding)
        existing_config = _load_existing_config(target_root, target_prebinding)
        _assert_named_directory_binding(target_prebinding)
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
        prepared_npm_units: frozenset[str] = frozenset()
        home_traversal_transition: tuple[DirectoryMetadataTransition, ...] | None = None
        warnings: list[str] = []
        try:
            _assert_named_directory_binding(target_prebinding)
            _verify_update_drift_authority(
                target_root,
                release_root,
                phase="vor dem Vollbackup",
                emit_details=True,
            )
            with _bound_update_lock_environment(lock_descriptor):
                backup_path = _create_backup(target_root, target_prebinding)
            _assert_named_directory_binding(target_prebinding)
            print(f"[OK] Vollbackup: {backup_path}", flush=True)
            print(
                "[STATUS] Vollbackup bestätigt. Vorbereitungen laufen; die Anlage "
                "arbeitet weiter.",
                flush=True,
            )
            role_service_intended = _role_service_intended(role)
            config = _bind_role_context(
                role,
                existing_config,
                bound_peer_ip=bound_peer_ip,
                require_peer=(
                    role in {"master", "slave"} and role_service_intended
                ),
            )
            service_prestate = _capture_service_prestate(target_root)
            _assert_named_directory_binding(target_prebinding)
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
                target_version=tag.removeprefix("v"),
            )
            prepared_npm_units, npm_warnings = _prepare_selected_release_dependencies(
                release_root,
                install_user,
                selected_catalog_units,
            )
            warnings.extend(npm_warnings)
            _assert_named_directory_binding(target_prebinding)
            active_before = service_prestate.active
            cutover_started = True
            cutover_started_at = time.monotonic()
            _stop_for_cutover(service_prestate.cutover_scope)
            _assert_named_directory_binding(target_prebinding)
            quiesced_backup, quiesced_guard = _create_stopped_data_backup(
                target_root,
                backup_path,
                target_prebinding,
            )
            _assert_named_directory_binding(target_prebinding)
            print(f"[OK] Ruhende Daten-Nachsicherung: {quiesced_backup}", flush=True)
            quarantine = _clear_legacy_update_blockers(backup_path)
            if quarantine is not None:
                print(f"[OK] Alter Updatezustand archiviert: {quarantine}", flush=True)
            _confirm_cutover_quiet(service_prestate.cutover_scope)
            _verify_update_drift_authority(
                target_root,
                release_root,
                phase="nach Writer-Ruhe unmittelbar vor dem Dateiaustausch",
                emit_details=False,
            )
            print(
                "[3/4] Produktdateien und Rechte werden aktualisiert; die Anlage "
                "bleibt kurz unterbrochen …",
                flush=True,
            )
            product_mutated = True
            target_binding = _replace_product_tree(
                target_root,
                release_root,
                install_user,
                target_prebinding,
            )
            home_traversal_transition = _project_web_home_traversal(
                install_user,
                target_root,
            )
            _assert_named_directory_binding(target_binding)
            web_program_files, web_program_directories = _sync_web_tree(release_root)
            _assert_named_directory_binding(target_binding)
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
                    prepared_npm_units,
                    require_role_peer=(
                        role in {"master", "slave"} and role_service_intended
                    ),
                    target_binding=target_binding,
                    web_program_files=web_program_files,
                    web_program_directories=web_program_directories,
                )
            )
            services = _services_to_start(
                policy,
                active_before,
                role,
                service_prestate.enabled,
                role_service_intended=role_service_intended,
                masked_units=service_prestate.masked,
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
                masked_units=service_prestate.masked,
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
                    masked_units=service_prestate.masked,
                )
            )
            mask_mismatch = _service_mask_mismatches(service_prestate)
            if mask_mismatch:
                _fail(
                    "E3DC-UPD-SERVICE-MASK-002",
                    "Ein ausdrücklich maskierter Dienstzustand wurde beim Releasewechsel verändert: "
                    + "; ".join(mask_mismatch),
                    "Stelle die genannte Unit mit sudo systemctl mask <unit> beziehungsweise sudo systemctl mask --runtime <unit> wieder auf Aus und starte danach denselben Updatebefehl erneut.",
                )
            warnings.extend(
                _final_confirmation(
                    target_root,
                    target_binding,
                    tag.removeprefix("v"),
                    required,
                    _forbidden_services_after_start(
                        service_prestate,
                        role,
                        role_service_intended=role_service_intended,
                    ),
                    required_writer_admission_mode=required_writer_admission_mode,
                    masked_units=service_prestate.masked,
                )
            )
            replacement_confirmed = True
            services_confirmed_at = time.monotonic()
            print(
                "[STATUS] Regelung und Weboberfläche laufen wieder. "
                "Abschlussbereinigung läuft.",
                flush=True,
            )
            warnings.extend(
                _postflight_python_programming_warnings(
                    (*services, *required),
                )
            )
            warnings.extend(_retire_unknown_active_e3dc(service_prestate))
            warnings.extend(
                _restart_target_bound_observers_after_success(service_prestate)
            )
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
            if quiesced_backup is not None and quiesced_guard is not None:
                try:
                    with _bound_update_lock_environment(lock_descriptor):
                        _remove_stopped_data_backup(
                            quiesced_backup,
                            quiesced_guard,
                        )
                    print(
                        "[OK] Temporäre ruhende Daten-Nachsicherung entfernt.",
                        flush=True,
                    )
                    quiesced_backup = None
                    quiesced_guard = None
                except Exception as exc:
                    detail = str(exc).strip() or exc.__class__.__name__
                    warnings.append(
                        "Die bestätigte neue Version bleibt aktiv; die temporäre "
                        "ruhende Daten-Nachsicherung konnte nicht sicher bestätigt "
                        "entfernt werden: {}. Prüfe später im Install-Center das "
                        "Backup-Limit; lösche das Verzeichnis nicht manuell.".format(detail)
                    )
            warnings.extend(
                _retry_backup_retention_after_confirmed_start(
                    target_root,
                    backup_path,
                    quiesced_backup,
                    lock_descriptor,
                )
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
                    install_user=install_user,
                    config=config,
                    role=role,
                    product_mutated=product_mutated,
                    target_prebinding=target_prebinding,
                    replacement_confirmed=replacement_confirmed,
                    home_traversal_transition=home_traversal_transition,
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
                    install_user=install_user,
                    config=config,
                    role=role,
                    product_mutated=product_mutated,
                    target_prebinding=target_prebinding,
                    replacement_confirmed=replacement_confirmed,
                    home_traversal_transition=home_traversal_transition,
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
                or _apply_failure_solution(str(exc), target_root),
                system_state=state,
            )
    finally:
        os.close(lock_descriptor)
    print("\n[OK] Update abgeschlossen.")
    print(f"Version: {tag.removeprefix('v')}")
    print(f"Backup: {backup_path}")
    total_seconds = max(0, round(time.monotonic() - update_started_at))
    print(f"Gesamtdauer: {total_seconds // 60} min {total_seconds % 60} s")
    if cutover_started_at is not None and services_confirmed_at is not None:
        cutover_seconds = max(0, round(services_confirmed_at - cutover_started_at))
        print(
            "Kontrollierte Anlagenunterbrechung: "
            f"{cutover_seconds // 60} min {cutover_seconds % 60} s"
        )
    for warning in warnings:
        print(f"[WARNUNG] {warning}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Einfacher E3DC-Control Releasewechsel")
    parser.add_argument("--target", required=True)
    parser.add_argument("--install-user", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--role", choices=("off", "master", "slave", "shadow"), required=True)
    parser.add_argument("--peer-ip", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return perform_update(
            target_root=Path(args.target).absolute(),
            install_user=args.install_user,
            tag=args.tag,
            role=args.role,
            bound_peer_ip=args.peer_ip,
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
