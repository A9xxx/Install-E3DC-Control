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
        final_mode = (
            stat.S_IMODE(source_opened.st_mode) & 0o777
            if mode is None
            else mode
        )
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
    excluded_top_level: frozenset[str] = frozenset(),
) -> None:
    """Projiziert Release-Dateien fd-relativ, nofollow und mountgebunden."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory:
        raise RuntimeError("Sichere fd-relative Releaseprojektion ist nicht verfügbar")
    source_root = source_root.resolve(strict=True)
    target_root.mkdir(parents=True, exist_ok=True)
    root_fd = os.open(target_root, os.O_RDONLY | nofollow | directory | cloexec)
    completed = False
    try:
        root_metadata = os.fstat(root_fd)
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
        completed = True
    finally:
        if not completed:
            try:
                os.fchown(root_fd, root_metadata.st_uid, root_metadata.st_gid)
                os.fchmod(root_fd, stat.S_IMODE(root_metadata.st_mode))
            except (OSError, UnboundLocalError):
                pass
        os.close(root_fd)


def _replace_product_tree(
    target_root: Path,
    release_root: Path,
    install_user: str,
) -> None:
    """Projiziert den Zielbaum; vorhandene Git-Metadaten bleiben unbeachtet."""

    account = pwd.getpwnam(install_user)
    web_gid = grp.getgrnam("www-data").gr_gid
    _project_release_tree(
        release_root,
        target_root,
        uid=account.pw_uid,
        gid=web_gid,
        root_mode=0o755,
        excluded_top_level=frozenset({".git"}),
    )


def _delete_approved_stale_paths(target_root: Path, policy: dict) -> None:
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

    allowed_root_candidates = [
        Path(os.path.abspath(target_root)),
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
    for raw in raw_deletes:
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            candidate = target_root / candidate
        candidate = Path(os.path.abspath(candidate))
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
        root_fd = -1
        opened: list[int] = []
        try:
            root_fd = os.open(
                selected_root,
                os.O_RDONLY | nofollow | directory | cloexec,
            )
            root_metadata = os.fstat(root_fd)
            root_device = root_metadata.st_dev
            root_mount_id = _fd_mount_id(root_fd)
            if selected_root == RAMDISK_PATH:
                # Die pfadbasierte tmpfs-Bestätigung wird an genau den bereits
                # geöffneten nofollow-FD gebunden. Ein Un-/Ummount während der
                # Probe darf nicht unbemerkt einen neuen Löschroot autorisieren.
                if not _probe_ramdisk_tmpfs():
                    raise RuntimeError("RAM-Disk ist kein bestätigtes tmpfs")
                confirmation_fd = os.open(
                    selected_root,
                    os.O_RDONLY | nofollow | directory | cloexec,
                )
                try:
                    confirmation_metadata = os.fstat(confirmation_fd)
                    if (
                        (confirmation_metadata.st_dev, confirmation_metadata.st_ino)
                        != (root_metadata.st_dev, root_metadata.st_ino)
                        or _fd_mount_id(confirmation_fd) != root_mount_id
                    ):
                        raise RuntimeError(
                            "RAM-Disk-Mount änderte sich während der Bestätigung"
                        )
                finally:
                    os.close(confirmation_fd)
            parent_fd = root_fd
            missing = False
            for depth, component in enumerate(relative.parts[:-1], start=1):
                try:
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
                    child_metadata.st_dev != root_device
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
                target_fd = os.open(name, os.O_RDONLY | nofollow | cloexec, dir_fd=parent_fd)
                try:
                    if (
                        os.fstat(target_fd).st_dev != root_device
                        or _fd_mount_id(target_fd) != root_mount_id
                    ):
                        raise RuntimeError("Löschziel ist ein verschachtelter Mount")
                finally:
                    os.close(target_fd)
            elif not stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError("Löschziel ist eine Spezialdatei")
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
        except RuntimeError as exc:
            errors.append(f"{candidate}: {exc}")
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)
            if root_fd >= 0:
                os.close(root_fd)
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
    masked_units: Iterable[str] = (),
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

    masked = {_normalize_unit(unit) for unit in masked_units}
    for spec in specs:
        if str(spec["unit"]) in masked:
            continue
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


def _sync_web_tree(
    target_root: Path,
    install_user: str,
    destination: Path = Path("/var/www/html"),
) -> None:
    source = target_root / "html"
    www_gid = grp.getgrnam("www-data").gr_gid
    _project_release_tree(
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
    root_fd = os.open(destination, os.O_RDONLY | nofollow | directory | cloexec)
    try:
        root_metadata = os.fstat(root_fd)
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
                target_root / name,
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
        if relative in {
            ("wallbox_mode5_user_start_request.json",),
            ("wallbox_mode5_user_start_request.json.lock",),
        }:
            return www_uid, www_gid, data_dir_mode, 0o660
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
                child = os.stat(child_name, dir_fd=descriptor, follow_symlinks=False)
                child_relative = (*relative, child_name)
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
        _atomic_write_file(
            v4_path,
            v4_payload,
            uid=account.pw_uid,
            gid=www_gid,
            mode=config_secret_file_mode(secret_mode_source),
        )
    os.chown(data_dir, account.pw_uid, www_gid)
    os.chmod(data_dir, config_secret_dir_mode(secret_mode_source))

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
    prepared_npm_units: Iterable[str] = (),
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
        (() if service_prestate is None else service_prestate.masked),
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
    _project_path_metadata(
        target_root,
        install_user,
        role,
        config,
        require_peer=require_role_peer,
    )
    _normalize_preserved_web_permissions(
        Path("/var/www/html"),
        install_user,
        config,
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


def _start_services(
    services: tuple[str, ...],
    *,
    required: tuple[str, ...],
    enable_services: tuple[str, ...],
    masked_units: Iterable[str] = (),
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
    masked = {_normalize_unit(unit) for unit in masked_units}
    required_set = (set(required) | {"apache2.service"}) - masked
    enable_set = set(enable_services) - masked
    for unit in services:
        if unit in masked:
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
    expected_version: str,
    required_services: tuple[str, ...],
    forbidden_services: Iterable[str] = (),
    *,
    required_writer_admission_mode: str = "",
    masked_units: Iterable[str] = (),
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

    masked = {_normalize_unit(unit) for unit in masked_units}
    required = (set(required_services) | {"apache2.service"}) - masked
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
        if unit in restartable and _service_exists(unit):
            # Ein fehlgeschlagener Zielstart darf den bestätigten Altstand
            # nicht anschließend über systemds StartLimit blockieren.
            _run(["/usr/bin/systemctl", "reset-failed", unit], timeout=20)
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
        # Die Zielprojektion schützt erhaltene Laufzeitverzeichnisse während
        # des Austauschs vor parallelen Schreibern. Nach einem Rücklauf müssen
        # deren veröffentlichte Alt-Rechte vor jedem Dienststart wieder gelten.
        _normalize_preserved_web_permissions(
            Path("/var/www/html"),
            install_user,
            config,
        )
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
    install_user: str,
    config: dict,
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
            install_user=install_user,
            config=config,
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
    bound_peer_ip: str = "",
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
        prepared_npm_units: frozenset[str] = frozenset()
        warnings: list[str] = []
        try:
            backup_path = _create_backup(target_root)
            print(f"[OK] Vollbackup: {backup_path}", flush=True)
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
                    prepared_npm_units,
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
                    install_user=install_user,
                    config=config,
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
                or _apply_failure_solution(str(exc), target_root),
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
