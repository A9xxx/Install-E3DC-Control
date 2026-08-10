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
import ipaddress
import os
from pathlib import Path
import stat
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


class DenyLeaseBackend:
    """Fail-closed-Standard für Aufrufer ohne vorerzeugten Kernel-Lock."""

    def acquire(self, _key: str, _owner: str) -> bool:
        return False

    def release(self, _key: str, _owner: str) -> bool:
        return False


class FileOwnerLeaseBackend(DenyLeaseBackend):
    """Entfernte /tmp-Kompatibilitätsoberfläche; jede Freigabe bleibt gesperrt."""

    def __init__(self, *_args, **_kwargs):
        pass


DEFAULT_FILE_LEASE_BACKEND = DenyLeaseBackend()


class PrecreatedFileLeaseBackend:
    """Dienst- und Endpunktlocks auf root-vorerzeugten festen Inodes."""

    def __init__(self, path: str, endpoint_path: str):
        self.path = os.path.abspath(path)
        self.endpoint_path = os.path.abspath(endpoint_path)
        self._service_held: Optional[tuple[int, str]] = None
        self._endpoint_descriptor: Optional[int] = None
        self._endpoint_locks: Dict[str, tuple[int, str]] = {}

    @staticmethod
    def _canonical_endpoint_key(key: str) -> str:
        value = str(key or "").strip().lower()
        for prefix in (
            "transport:modbus-tcp:",
            "transport:luxtronik-shi:",
        ):
            if value.startswith(prefix):
                endpoint = value[len(prefix):]
                host, separator, port_text = endpoint.rpartition(":")
                try:
                    port = int(port_text) if separator else 0
                    host = str(ipaddress.ip_address(host))
                except (ValueError, TypeError):
                    return "transport:unknown"
                if not 1 <= port <= 65535:
                    return "transport:unknown"
                return f"transport:tcp:{host}:{port}"
        shelly_prefix = "transport:http-shelly:"
        if value.startswith(shelly_prefix):
            endpoint = value[len(shelly_prefix):]
            host, separator, relay_text = endpoint.partition(":switch:")
            try:
                host = str(ipaddress.ip_address(host))
                relay = int(relay_text) if separator else -1
            except (ValueError, TypeError):
                return "transport:unknown"
            if relay < 0:
                return "transport:unknown"
            return f"transport:http-shelly:{host}:switch:{relay}"
        # Unbekannte Transportformen teilen bewusst einen Sperrbereich. Das
        # kann Verfügbarkeit kosten, aber niemals Doppelsteuerung erlauben.
        return "transport:unknown"

    @classmethod
    def _lock_offset(cls, key: str) -> int:
        canonical = cls._canonical_endpoint_key(key)
        digest = hashlib.sha256(canonical.encode("utf-8", errors="strict")).digest()
        # Ein Hashzusammenstoß blockiert höchstens zwei verschiedene Endpunkte;
        # er kann niemals zwei Schreiber für denselben Endpunkt freigeben.
        return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)

    @staticmethod
    def _open_precreated(path: str) -> int:
        import grp

        namespace = Path("/run/e3dc-control")
        directory = namespace / "locks"
        for candidate in (namespace, directory):
            info = candidate.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or candidate.is_symlink()
                or info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o755
            ):
                raise OSError("unsicherer Wärme-Locknamespace")
        if path != str(directory / os.path.basename(path)):
            raise OSError("Wärme-Lock liegt außerhalb des gebundenen Namespace")
        descriptor = os.open(
            path,
            os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        named = os.lstat(path)
        expected_gid = int(grp.getgrnam("www-data").gr_gid)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != 0
            or opened.st_gid != expected_gid
            or stat.S_IMODE(opened.st_mode) != 0o660
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            os.close(descriptor)
            raise OSError("unsicherer Wärme-Lockinode")
        return descriptor

    def acquire(self, key: str, owner: str) -> bool:
        held = self._endpoint_locks.get(key)
        if held is not None:
            return held[1] == owner
        try:
            import fcntl

            if self._service_held is None:
                service_descriptor = self._open_precreated(self.path)
                try:
                    fcntl.flock(
                        service_descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except Exception:
                    os.close(service_descriptor)
                    raise
                self._service_held = (service_descriptor, owner)
            elif self._service_held[1] != owner:
                return False

            if self._endpoint_descriptor is None:
                self._endpoint_descriptor = self._open_precreated(self.endpoint_path)
            offset = self._lock_offset(key)
            fcntl.lockf(
                self._endpoint_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
                1,
                offset,
                os.SEEK_SET,
            )
            self._endpoint_locks[key] = (offset, owner)
            return True
        except (BlockingIOError, ImportError, KeyError, OSError):
            return False

    def release(self, key: str, owner: str) -> bool:
        held = self._endpoint_locks.get(key)
        if held is None or held[1] != owner:
            return False
        try:
            import fcntl

            if self._endpoint_descriptor is None:
                return False
            fcntl.lockf(
                self._endpoint_descriptor,
                fcntl.LOCK_UN,
                1,
                held[0],
                os.SEEK_SET,
            )
        except (ImportError, OSError):
            return False
        del self._endpoint_locks[key]
        return True


class PrecreatedEndpointLeaseBackend:
    """Hält nur den gemeinsamen physischen Endpunktlock.

    Aufrufer dürfen diesen Backend-Typ ausschließlich verwenden, wenn ihr
    eigener Prozess-Singleton bereits vor jedem Hardwareaufbau belegt ist.
    """

    def __init__(self, endpoint_path: str):
        self.endpoint_path = os.path.abspath(endpoint_path)
        self._endpoint_descriptor: Optional[int] = None
        self._endpoint_locks: Dict[str, tuple[int, str]] = {}

    def acquire(self, key: str, owner: str) -> bool:
        canonical = PrecreatedFileLeaseBackend._canonical_endpoint_key(key)
        held = self._endpoint_locks.get(canonical)
        if held is not None:
            return held[1] == owner
        try:
            import fcntl

            if self._endpoint_descriptor is None:
                self._endpoint_descriptor = PrecreatedFileLeaseBackend._open_precreated(
                    self.endpoint_path
                )
            offset = PrecreatedFileLeaseBackend._lock_offset(canonical)
            fcntl.lockf(
                self._endpoint_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
                1,
                offset,
                os.SEEK_SET,
            )
            self._endpoint_locks[canonical] = (offset, owner)
            return True
        except (BlockingIOError, ImportError, KeyError, OSError):
            return False

    def release(self, key: str, owner: str) -> bool:
        canonical = PrecreatedFileLeaseBackend._canonical_endpoint_key(key)
        held = self._endpoint_locks.get(canonical)
        if held is None or held[1] != owner:
            return False
        try:
            import fcntl

            if self._endpoint_descriptor is None:
                return False
            fcntl.lockf(
                self._endpoint_descriptor,
                fcntl.LOCK_UN,
                1,
                held[0],
                os.SEEK_SET,
            )
        except (ImportError, OSError):
            return False
        del self._endpoint_locks[canonical]
        return True


class HeatActuatorSafetyGate:
    """Prüft Kontext und exklusiven Owner vor jedem physischen Ausgang erneut."""

    def __init__(
        self,
        context_validator: Callable[[], object],
        *,
        owner: str,
        lease_backend=None,
        authority_validator: Optional[Callable[[], object]] = None,
    ):
        self.context_validator = context_validator
        self.owner = str(owner)
        self.lease_backend = lease_backend or DEFAULT_FILE_LEASE_BACKEND
        self.authority_validator = authority_validator
        self.last_authorization: Optional[Authorization] = None

    def authorize(
        self,
        driver_key: str,
        action: str,
        *,
        allow_release_on_invalid: bool = False,
        preserve_existing: bool = False,
    ) -> Authorization:
        if self.authority_validator is not None:
            try:
                authority = _normalise_verdict(self.authority_validator())
            except Exception as exc:
                authority = ContextVerdict(
                    False,
                    f"authority_validator_error:{type(exc).__name__}",
                )
            if not authority.valid:
                # HA-/Shadow-Autorität ist härter als ein lokaler
                # Release-Ausgang. Auch ein vermeintlich sicheres AUS darf
                # nicht von einer nicht autorisierten Instanz stammen.
                result = Authorization(
                    False,
                    False,
                    False,
                    f"{action}:authority_blocked:{authority.reason}",
                    bool(preserve_existing),
                )
                self.last_authorization = result
                return result

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


_PRODUCTION_LEASE_BACKENDS: Dict[str, PrecreatedFileLeaseBackend] = {}


def default_heat_actuator_gate(module_file: str, expected_relative_path: str, service: str):
    module_id = os.path.realpath(module_file)
    lock_names = {
        "energy_manager": "energy_manager.owner.lock",
        "heizstab_manager": "heizstab_manager.owner.lock",
    }
    lock_name = lock_names.get(str(service))
    if not lock_name:
        raise ValueError("Wärme-Aktorgate besitzt keinen kanonischen Service-Lock")
    lease_backend = _PRODUCTION_LEASE_BACKENDS.get(str(service))
    if lease_backend is None:
        lease_backend = PrecreatedFileLeaseBackend(
            os.path.join("/run/e3dc-control/locks", lock_name),
            os.path.join("/run/e3dc-control/locks", "heat_actuator_endpoints.lock"),
        )
        _PRODUCTION_LEASE_BACKENDS[str(service)] = lease_backend

    def _ha_authority_verdict():
        try:
            from Installer.ha_writer_admission import evaluate_writer_admission
        except ModuleNotFoundError:
            from ha_writer_admission import evaluate_writer_admission  # type: ignore
        result = evaluate_writer_admission()
        return ContextVerdict(
            result.get("allowed") is True,
            str(result.get("reason") or "ha_writer_admission_blocked"),
        )

    return HeatActuatorSafetyGate(
        NarrowRuntimeContext(module_file, expected_relative_path),
        owner=f"{service}:{os.getpid()}:{module_id}",
        lease_backend=lease_backend,
        authority_validator=_ha_authority_verdict,
    )
