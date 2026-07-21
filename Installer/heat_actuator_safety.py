#!/usr/bin/env python3
"""Enges Fail-closed-Gate für schreibende Wärmeaktoren.

Dieses Modul löst, migriert oder errät bewusst keine Installationspfade. Es
belegt, dass das gerade ausgeführte Modul zu einem kohärenten Produktbaum
gehört, und kombiniert diesen Nachweis unmittelbar vor jedem physischen
Ausgang mit einer treiberspezifischen Prozess-Lease.

Wärmepumpen-Aufrufer dürfen ``allow_release_on_invalid`` nicht verwenden: Geht
der lokale Kontext verloren, erhält das Ausbleiben eines Befehls einen bereits
laufenden Verdichter. Persistente Heizstab-Aufrufer dürfen die Option nur für
eine bestätigte, treiberspezifische AUS-/Release-Übergabe verwenden.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Callable, Dict, Optional


@dataclass(frozen=True)
class ContextVerdict:
    valid: bool
    reason: str
    root: str = ""


@dataclass(frozen=True)
class Authorization:
    allowed: bool
    context_valid: bool
    lease_owned: bool
    reason: str
    preserve_existing: bool = False


def _normalise_verdict(value) -> ContextVerdict:
    if isinstance(value, ContextVerdict):
        return value
    if isinstance(value, tuple):
        valid = bool(value[0]) if value else False
        reason = str(value[1]) if len(value) > 1 else ("ok" if valid else "invalid")
        root = str(value[2]) if len(value) > 2 else ""
        return ContextVerdict(valid, reason, root)
    if isinstance(value, bool):
        return ContextVerdict(value, "ok" if value else "invalid")
    return ContextVerdict(False, "invalid_context_verdict")


class NarrowRuntimeContext:
    """Prüft einen exakten Modulpfad gegen eine kleine unveränderliche Produktform."""

    _MARKERS = (
        "VERSION",
        "Installer/__init__.py",
        "Installer/e3dc_live.py",
    )

    def __init__(self, module_file: str, expected_relative_path: str):
        self.module_file = os.path.abspath(module_file)
        self.expected_relative_path = expected_relative_path.replace("\\", "/").strip("/")

    @staticmethod
    def _regular_single_link(path: Path) -> bool:
        try:
            lst = path.lstat()
            return stat.S_ISREG(lst.st_mode) and lst.st_nlink == 1 and not path.is_symlink()
        except OSError:
            return False

    def __call__(self) -> ContextVerdict:
        try:
            module_path = Path(self.module_file)
            parts = tuple(part for part in self.expected_relative_path.split("/") if part)
            if not parts:
                return ContextVerdict(False, "empty_expected_module_path")
            root = module_path.parents[len(parts) - 1]
            expected = root.joinpath(*parts)
            if os.path.normcase(str(expected)) != os.path.normcase(str(module_path)):
                return ContextVerdict(False, "module_path_mismatch", str(root))
            if not self._regular_single_link(module_path):
                return ContextVerdict(False, "module_not_regular_single_link", str(root))

            try:
                euid = os.geteuid()
            except AttributeError:
                return ContextVerdict(False, "posix_identity_unavailable", str(root))

            module_stat = module_path.stat()
            if euid != 0 and module_stat.st_uid != euid:
                return ContextVerdict(False, "module_owner_mismatch", str(root))
            if module_stat.st_mode & 0o022:
                return ContextVerdict(False, "module_group_or_world_writable", str(root))

            for marker_rel in self._MARKERS:
                marker = root.joinpath(*marker_rel.split("/"))
                if not self._regular_single_link(marker):
                    return ContextVerdict(False, f"invalid_product_marker:{marker_rel}", str(root))
                marker_stat = marker.stat()
                if euid != 0 and marker_stat.st_uid != euid:
                    return ContextVerdict(False, f"marker_owner_mismatch:{marker_rel}", str(root))
                if marker_stat.st_mode & 0o022:
                    return ContextVerdict(False, f"marker_group_or_world_writable:{marker_rel}", str(root))

            version = (root / "VERSION").read_text(encoding="utf-8", errors="strict").strip()
            if not version or len(version) > 64:
                return ContextVerdict(False, "invalid_version_marker", str(root))
            return ContextVerdict(True, "coherent_local_product_tree", str(root))
        except (IndexError, OSError, UnicodeError, ValueError) as exc:
            return ContextVerdict(False, f"context_error:{type(exc).__name__}")


class InMemoryLeaseBackend:
    """Deterministisches Backend für fokussierte Tests ohne Datei- oder Hardware-I/O."""

    def __init__(self):
        self.owners: Dict[str, str] = {}

    def acquire(self, key: str, owner: str) -> bool:
        current = self.owners.get(key)
        if current is None:
            self.owners[key] = owner
            return True
        return current == owner

    def release(self, key: str, owner: str) -> bool:
        if self.owners.get(key) != owner:
            return False
        del self.owners[key]
        return True


class FileOwnerLeaseBackend:
    """Nicht blockierende, benutzerübergreifende Lease mit Kernel-Lock.

    Die Lease-Datei enthält nur Diagnosedaten und wird bewusst geteilt, damit
    ein neu konfigurierter Dienstnutzer nach Freigabe des alten Writers denselben
    Inode öffnen kann. Exklusivität hängt nie vom Dateiinhalt ab: Das flock
    eines aktiven Owners kann kein anderer Prozess übernehmen.
    """

    def __init__(self, directory: Optional[str] = None):
        self.directory = Path(
            directory
            or os.path.join(tempfile.gettempdir(), "e3dc-control-heat-actuator-leases-v1")
        )
        self._held: Dict[str, tuple] = {}

    def _prepare_directory(self) -> bool:
        try:
            self.directory.mkdir(mode=0o1777, parents=False, exist_ok=True)
            st = self.directory.lstat()
            if not stat.S_ISDIR(st.st_mode) or self.directory.is_symlink():
                return False
            # Ein gemeinsames Sticky-Verzeichnis gibt allen Installationsnutzern
            # dieselbe Kollisionsdomäne und verhindert zugleich den Austausch
            # der Lease-Datei eines anderen Nutzers.
            if (st.st_mode & 0o1000) == 0 or (st.st_mode & 0o002) == 0:
                try:
                    os.chmod(self.directory, 0o1777)
                    st = self.directory.lstat()
                except OSError:
                    return False
            return bool((st.st_mode & 0o1000) and (st.st_mode & 0o002))
        except OSError:
            return False

    @staticmethod
    def _filename(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8", errors="strict")).hexdigest() + ".lease"

    def acquire(self, key: str, owner: str) -> bool:
        held = self._held.get(key)
        if held is not None:
            return held[1] == owner
        if os.name != "posix" or not self._prepare_directory():
            return False
        try:
            import fcntl

            path = self.directory / self._filename(key)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o666)
            st = os.fstat(fd)
            euid = os.geteuid()
            if st.st_uid == euid and stat.S_IMODE(st.st_mode) != 0o666:
                os.fchmod(fd, 0o666)
                st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_nlink != 1
                or stat.S_IMODE(st.st_mode) != 0o666
            ):
                os.close(fd)
                return False
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            payload = json.dumps(
                {"owner": owner, "pid": os.getpid(), "acquired_ts": int(time.time())},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)
            self._held[key] = (fd, owner, str(path))
            return True
        except (OSError, ImportError, BlockingIOError):
            try:
                if "fd" in locals():
                    os.close(fd)
            except OSError:
                pass
            return False

    def release(self, key: str, owner: str) -> bool:
        held = self._held.get(key)
        if held is None or held[1] != owner:
            return False
        fd = held[0]
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        del self._held[key]
        return True


DEFAULT_FILE_LEASE_BACKEND = FileOwnerLeaseBackend()


class HeatActuatorSafetyGate:
    """Prüft Kontext und exklusiven Owner vor jedem physischen Ausgang erneut."""

    def __init__(
        self,
        context_validator: Callable[[], object],
        *,
        owner: str,
        lease_backend=None,
    ):
        self.context_validator = context_validator
        self.owner = str(owner)
        self.lease_backend = lease_backend or DEFAULT_FILE_LEASE_BACKEND
        self.last_authorization: Optional[Authorization] = None

    def authorize(
        self,
        driver_key: str,
        action: str,
        *,
        allow_release_on_invalid: bool = False,
        preserve_existing: bool = False,
    ) -> Authorization:
        try:
            context = _normalise_verdict(self.context_validator())
        except Exception as exc:
            context = ContextVerdict(False, f"context_validator_error:{type(exc).__name__}")

        if not context.valid and not allow_release_on_invalid:
            result = Authorization(
                False,
                False,
                False,
                f"{action}:context_blocked:{context.reason}",
                bool(preserve_existing),
            )
            self.last_authorization = result
            return result

        lease_owned = bool(self.lease_backend.acquire(str(driver_key), self.owner))
        if not lease_owned:
            result = Authorization(
                False,
                context.valid,
                False,
                f"{action}:owner_lease_busy",
                bool(preserve_existing),
            )
            self.last_authorization = result
            return result

        reason = (
            f"{action}:authorized"
            if context.valid
            else f"{action}:authorized_safe_release:{context.reason}"
        )
        result = Authorization(True, context.valid, True, reason, False)
        self.last_authorization = result
        return result


def default_heat_actuator_gate(module_file: str, expected_relative_path: str, service: str):
    module_id = os.path.realpath(module_file)
    return HeatActuatorSafetyGate(
        NarrowRuntimeContext(module_file, expected_relative_path),
        owner=f"{service}:{os.getpid()}:{module_id}",
    )
