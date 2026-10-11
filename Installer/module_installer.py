#!/usr/bin/python3 -I
"""Enger, root-eigener Einstieg für optionale Modulinstallationen."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import grp
import subprocess
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid
import contextvars

sys.dont_write_bytecode = True
SCHEMA = "e3dc_module_installer_v1"
LAUNCHER = Path("/usr/local/sbin/e3dc-module-installer")
CONTRACT = Path("/etc/e3dc-control/module_installer_contract.json")
CODE_ROOT = Path("/etc/e3dc-control/module-installer")
RUNTIME = Path("/run/e3dc-module-installer")
STATUS = RUNTIME / "status.json"
LOG = Path("/var/log/e3dc-control/module-installer.log")
LOCK_ROOT = Path("/run/lock/e3dc-control")
ACTIONS = ("install", "remove")
EXCLUDED_MODULES = frozenset({"forecast_evidence", "ha", "shadow"})
EXCLUDED_UNITS = frozenset({"e3dc-ha.service", "e3dc-shadow-sync.service"})
WRAPPERS = ("Installer/service_wrapper.sh", "Installer/installer_wrapper.sh",
            "Installer/web_update_launcher.sh", "Installer/runtime_permissions_repair.py")
LOG_MAX_BYTES = 1024 * 1024
_INSPECTION = contextvars.ContextVar("module_inspection", default=None)
MAX_BYTES = 32 * 1024 * 1024
JOB_MAX_SECONDS = 1800
JOB_UNIT = "e3dc-module-installer.service"


class ModuleError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def _fingerprint(meta):
    return (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_gid, meta.st_mode,
            meta.st_nlink, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)


def _root_directory(path, *, create=False, mode=0o755):
    """Prüft jede Elternkomponente; folgt keinem Symlink."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ModuleError("path_unsafe", "Systempfad ist nicht eindeutig gebunden.")
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        if create and not os.path.lexists(current):
            current.mkdir(mode=mode if current == path else 0o755)
        meta = current.lstat()
        shared_lock_root_safe = (current == Path("/run/lock") and meta.st_uid == 0
                                 and meta.st_gid == 0 and bool(meta.st_mode & stat.S_ISVTX))
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != 0 or meta.st_gid != 0
                or (stat.S_IMODE(meta.st_mode) & 0o022 and not shared_lock_root_safe)):
            raise ModuleError("path_unsafe", "Systemverzeichnis ist nicht root-kontrolliert.")
    return path


def _root_bytes(path, *, mode=None, maximum=MAX_BYTES):
    path = Path(path)
    _root_directory(path.parent)
    before = path.lstat()
    # Ausschließlich der vom Systemupdate projizierte Rollenanker ist weblesbar.
    role_anchor = path == Path("/etc/e3dc-control/instance_role.json")
    groups = {0, int(grp.getgrnam("www-data").gr_gid)} if role_anchor else {0}
    anchor_mode_unsafe = role_anchor and bool(stat.S_IMODE(before.st_mode) & ~0o640)
    if (anchor_mode_unsafe or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != 0 or before.st_gid not in groups
            or stat.S_IMODE(before.st_mode) & 0o022
            or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
            or before.st_size > maximum):
        raise ModuleError("file_unsafe", "Root-eigene Datei besitzt unsichere Metadaten.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        chunks = []
        size = 0
        while size <= maximum:
            chunk = os.read(fd, min(1024 * 1024, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (size > maximum or _fingerprint(before) != _fingerprint(opened)
            or _fingerprint(opened) != _fingerprint(after)
            or _fingerprint(after) != _fingerprint(path.lstat())):
        raise ModuleError("file_drift", "Root-eigene Datei änderte sich beim Lesen.")
    return b"".join(chunks)


def _relative(value):
    if not isinstance(value, str) or "\\" in value:
        raise ModuleError("contract_invalid", "Vertrag enthält einen ungültigen Codepfad.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(value).is_absolute():
        raise ModuleError("contract_invalid", "Vertrag enthält einen ungültigen Codepfad.")
    return value


def _validate_contract(contract):
    if not isinstance(contract, dict):
        raise ModuleError("contract_invalid", "Modulvertrag ist ungültig.")
    _validate_contract_body(contract)
    return contract


def read_contract():
    try:
        contract = json.loads(_root_bytes(CONTRACT, mode=0o644, maximum=1024 * 1024))
    except (ValueError, UnicodeError) as exc:
        raise ModuleError("contract_invalid", "Modulvertrag ist nicht lesbar.") from exc
    return _validate_contract(contract)


def _validate_contract_body(contract):
    if (contract.get("schema") != SCHEMA
            or not re.fullmatch(r"[0-9a-f]{40}", str(contract.get("commit", "")))):
        raise ModuleError("commit_unbound", "Modul-Launcher ist nicht an einen freigegebenen Commit gebunden.")
    account = str(contract.get("install_user", ""))
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", account) or account in {"root", "www-data"}:
        raise ModuleError("identity_invalid", "Installationsbenutzer ist nicht sicher gebunden.")
    identity = pwd.getpwnam(account)
    if identity.pw_uid == 0:
        raise ModuleError("identity_invalid", "Module dürfen nicht als root laufen.")
    root = str(contract.get("install_root", ""))
    if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", root) or ".." in Path(root).parts
            or root in {"/", "/etc", "/usr", "/home", "/var"}):
        raise ModuleError("path_unsafe", "Installationspfad ist für systemd nicht eindeutig gebunden.")
    if contract.get("code_root") != str(CODE_ROOT / contract["commit"]):
        raise ModuleError("contract_invalid", "Ausführungscode liegt außerhalb des Modulvertrags.")
    modules = contract.get("modules")
    if not isinstance(modules, dict) or not modules or len(modules) > 64:
        raise ModuleError("allowlist_invalid", "Root-eigene Modulliste fehlt.")
    for key, value in modules.items():
        if key in EXCLUDED_MODULES or (isinstance(value, dict) and value.get("unit") in EXCLUDED_UNITS):
            raise ModuleError("allowlist_invalid", "Dieses Modul verwendet ausschließlich seine eigene Einrichtung über das Systemupdate.")
        if (not re.fullmatch(r"[a-z0-9_]+", key) or not isinstance(value, dict)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+\.service", str(value.get("unit", "")))):
            raise ModuleError("allowlist_invalid", "Root-eigene Modulliste ist ungültig.")
    venv = contract.get("venv_name", ".venv_e3dc")
    if (not isinstance(venv, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", venv)
            or venv in {".", ".."}):
        raise ModuleError("venv_invalid", "Gebundener venv-Name ist ungültig.")
    return contract


@contextlib.contextmanager
def inspection_scope():
    """Ein Prüfergebnis je Anfrage; außerhalb der Anfrage niemals wiederverwenden."""
    if _INSPECTION.get() is not None:
        yield
        return
    token = _INSPECTION.set({})
    try:
        yield
    finally:
        _INSPECTION.reset(token)


def inspect():
    cache = _INSPECTION.get()
    if cache is not None and "state" in cache:
        return cache["state"]
    state = _inspect_uncached()
    if cache is not None:
        cache["state"] = state
    return state


def _inspect_uncached():
    try:
        contract = read_contract()
        if digest(_root_bytes(LAUNCHER, mode=0o755)) != contract.get("launcher_sha256"):
            raise ModuleError("launcher_drift", "Modul-Launcher weicht vom freigegebenen Commit ab.")
        files = contract.get("files")
        if not isinstance(files, dict) or not files or len(files) > 600:
            raise ModuleError("code_unbound", "Commitgebundener Installer-Code fehlt.")
        code_root = Path(contract["code_root"])
        _root_directory(code_root)
        for relative, expected in files.items():
            payload = _root_bytes(code_root / _relative(relative), mode=0o444)
            if digest(payload) != expected:
                raise ModuleError("code_drift", "Installer-Code weicht vom freigegebenen Commit ab.")
        actual = _snapshot_files(code_root)
        if actual != set(files):
            raise ModuleError("code_drift", "Ausführungssnapshot enthält nicht freigegebene Dateien.")
        required = {"VERSION", "installer_main.py", "Installer/__init__.py",
                    "Installer/web_installer.py", "Installer/module_installer.py"}
        if not required <= set(files):
            raise ModuleError("code_unbound", "Ausführungssnapshot ist unvollständig.")
        if files["Installer/module_installer.py"] != contract["launcher_sha256"]:
            raise ModuleError("launcher_drift", "Launcher und Ausführungscode widersprechen sich.")
        return {"success": True, "contract": contract, "commit": contract["commit"]}
    except (OSError, KeyError, TypeError, ModuleError, ValueError) as exc:
        return {"success": False, "reason_code": getattr(exc, "code", "launcher_missing"),
                "message": str(exc) if isinstance(exc, ModuleError) else "Modul-Launcher fehlt oder ist nicht sicher prüfbar."}


def _snapshot_files(code_root):
    actual = set()
    for p in code_root.rglob("*"):
        meta = p.lstat()
        if stat.S_ISDIR(meta.st_mode):
            _root_directory(p)
        elif stat.S_ISREG(meta.st_mode):
            actual.add(str(p.relative_to(code_root)))
        else:
            raise ModuleError("code_drift", "Ausführungssnapshot enthält nicht freigegebene Dateitypen.")
    return actual


def sudoers_lines(contract):
    return [f"www-data ALL=(root) NOPASSWD: {LAUNCHER} {action} {key}"
            for key in sorted(contract["modules"]) for action in ACTIONS]


def _atomic_root_file(path, payload, mode):
    path = Path(path)
    _root_directory(path.parent, create=True)
    before = None
    if os.path.lexists(path):
        _root_bytes(path)
        before = _fingerprint(path.lstat())
    fd, name = tempfile.mkstemp(prefix=".module-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchown(handle.fileno(), 0, 0)
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        if (before is None and os.path.lexists(path)) or (before is not None and before != _fingerprint(path.lstat())):
            raise ModuleError("file_drift", "Systemdatei änderte sich vor dem Schreiben.")
        os.replace(name, path)
        fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


def provision(entries, commit, install_root, install_user, modules, *, venv_name=".venv_e3dc"):
    """Nur aus bereits gebundenen Releasebytes aufgerufen, niemals vom Webaufruf."""
    if os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{40}", str(commit)):
        raise ModuleError("commit_unbound", "Moduleinrichtung benötigt root und einen gebundenen Commit.")
    os.umask(0o022)
    selected = {}
    for relative, entry in entries.items():
        if relative in {"VERSION", "installer_main.py", *WRAPPERS} or (relative.startswith("Installer/") and relative.endswith(".py")):
            payload = entry[0] if isinstance(entry, tuple) else entry
            selected[_relative(relative)] = bytes(payload).replace(b"\r\n", b"\n")
    required = {"VERSION", "installer_main.py", "Installer/__init__.py",
                "Installer/web_installer.py", "Installer/module_installer.py", *WRAPPERS}
    if not required <= set(selected):
        raise ModuleError("code_unbound", "Ausführungssnapshot ist vor der Einrichtung unvollständig.")
    source = selected["Installer/module_installer.py"]
    compile(source, "module_installer.py", "exec")
    if not source.startswith(b"#!/usr/bin/python3 -I\n"):
        raise ModuleError("code_unbound", "Launcherquelle hat keinen isolierten Interpretervertrag.")
    previous = None
    warnings = []
    damaged_contract = None
    if os.path.lexists(CONTRACT):
        try:
            previous_record = read_contract()
            previous = previous_record["commit"]
            if previous == commit:
                previous = previous_record.get("previous_commit")
            if previous is not None and not re.fullmatch(r"[0-9a-f]{40}", str(previous)):
                raise ModuleError("commit_unbound", "Bisheriger Snapshot ist nicht eindeutig gebunden.")
        except (OSError, ValueError, TypeError, KeyError, ModuleError):
            _root_directory(CONTRACT.parent)
            damaged_contract = _fingerprint(CONTRACT.lstat())
            previous = None
            warnings.append("Bisheriger Modulvertrag ist nicht sicher lesbar; Altbestand bleibt erhalten.")
    code_root = CODE_ROOT / commit
    _validate_contract({"schema": SCHEMA, "commit": commit, "install_root": str(install_root),
                        "install_user": install_user, "code_root": str(code_root), "modules": modules,
                        "venv_name": venv_name})
    _root_directory(CODE_ROOT, create=True)
    expected = {relative: digest(payload) for relative, payload in selected.items()}
    if os.path.lexists(code_root):
        _root_directory(code_root)
        actual = _snapshot_files(code_root)
        if actual != set(expected):
            raise ModuleError("code_drift", "Vorhandener Modul-Code stimmt nicht mit dem Release überein.")
        for relative, sha in expected.items():
            if digest(_root_bytes(code_root / relative, mode=0o444)) != sha:
                raise ModuleError("code_drift", "Vorhandener Modul-Code ist verändert.")
    else:
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=CODE_ROOT))
        try:
            stage.chmod(0o755)
            for relative, payload in selected.items():
                target = stage / relative
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                _atomic_root_file(target, payload, 0o444)
            os.rename(stage, code_root)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    contract = {"schema": SCHEMA, "commit": commit, "install_root": str(install_root),
                "install_user": install_user, "code_root": str(code_root),
                "launcher_sha256": digest(source), "files": expected, "modules": modules,
                "venv_name": venv_name, "previous_commit": previous}
    _atomic_root_file(CODE_ROOT / (commit + ".json"),
                      (json.dumps({"commit": commit, "files": expected}, sort_keys=True) + "\n").encode(), 0o644)
    _atomic_root_file(LAUNCHER, source, 0o755)
    if damaged_contract is not None:
        if _fingerprint(CONTRACT.lstat()) != damaged_contract:
            raise ModuleError("file_drift", "Bisheriger Modulvertrag änderte sich vor dem Ersetzen.")
        preserved = CONTRACT.with_name(CONTRACT.name + ".unverified-" + uuid.uuid4().hex)
        os.rename(CONTRACT, preserved)
    _atomic_root_file(CONTRACT, (json.dumps(contract, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(), 0o644)
    result = inspect()
    if not result["success"]:
        raise ModuleError(result["reason_code"], result["message"])
    if damaged_contract is None:
        warnings.extend(_prune_snapshots({commit, previous}))
    return dict(contract, warnings=warnings)


def _prune_snapshots(keep):
    """Entfernt nur anhand root-eigener Hashlisten vollständig verifizierte Altstände."""
    warnings = []
    _root_directory(CODE_ROOT)
    for path in sorted(CODE_ROOT.iterdir()):
        if not re.fullmatch(r"[0-9a-f]{40}", path.name) or path.name in keep:
            continue
        try:
            _root_directory(path)
            manifest = CODE_ROOT / (path.name + ".json")
            if os.path.lexists(manifest):
                record = json.loads(_root_bytes(manifest, mode=0o644, maximum=1024 * 1024))
            else:
                raise ModuleError("snapshot_unsafe", "Alter Ausführungssnapshot besitzt keine gebundene Hashliste.")
            files = record.get("files")
            if record.get("commit") != path.name or not isinstance(files, dict) or not files or len(files) > 600:
                raise ModuleError("snapshot_unsafe", "Alter Ausführungssnapshot ist nicht sicher inventarisiert.")
            if _snapshot_files(path) != set(files):
                raise ModuleError("snapshot_unsafe", "Alter Ausführungssnapshot enthält fremde Dateien.")
            for relative, expected in files.items():
                if digest(_root_bytes(path / _relative(relative), mode=0o444)) != expected:
                    raise ModuleError("snapshot_unsafe", "Alter Ausführungssnapshot besitzt veränderte Dateien.")
            shutil.rmtree(path)
            manifest.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, KeyError, ModuleError):
            warnings.append("Alter Modul-Snapshot ist nicht sicher verifizierbar; Altbestand bleibt erhalten.")
    return warnings


@contextlib.contextmanager
def execution_lock(*, status_only=False):
    _root_directory(LOCK_ROOT, create=True, mode=0o700)
    if stat.S_IMODE(LOCK_ROOT.lstat().st_mode) != 0o700:
        raise ModuleError("lock_unsafe", "Wartungssperrverzeichnis ist nicht privat.")
    descriptors = []
    try:
        names = ("module-installer.lock",) if status_only else ("update.lock", "runtime-permissions-repair.lock", "module-installer.lock")
        for name in names:
            path = LOCK_ROOT / name
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            descriptors.append(fd)
            meta = os.fstat(fd)
            if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_uid != 0
                    or meta.st_gid != 0 or stat.S_IMODE(meta.st_mode) != 0o600
                    or (meta.st_dev, meta.st_ino) != (path.lstat().st_dev, path.lstat().st_ino)):
                raise ModuleError("lock_unsafe", "Wartungssperre ist nicht root-gebunden.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ModuleError("busy", "Eine Installation, ein Update oder eine Rechte-Reparatur läuft bereits.") from exc
        yield descriptors[0]
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _publish(result):
    result = dict(result, updated_at=time.time())
    # Einzelheiten bleiben ausschließlich im root-eigenen Protokoll.
    log_detail = result.pop("_log_detail", None)
    _atomic_root_file(STATUS, (json.dumps(result, ensure_ascii=False) + "\n").encode(), 0o644)
    _root_directory(LOG.parent, create=True)
    log_record = dict(result)
    if log_detail:
        log_record["detail"] = log_detail
    line = (json.dumps(log_record, ensure_ascii=False) + "\n").encode()
    fd = os.open(LOG, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
    try:
        meta = os.fstat(fd)
        named = LOG.lstat()
        if (meta.st_uid != 0 or meta.st_gid != 0 or meta.st_nlink != 1
                or not stat.S_ISREG(meta.st_mode) or (meta.st_dev, meta.st_ino) != (named.st_dev, named.st_ino)):
            raise ModuleError("log_unsafe", "Installationsprotokoll ist nicht root-gebunden.")
        # Auch nach logrotate einen sicheren Modus direkt am gebundenen Deskriptor herstellen.
        os.fchmod(fd, 0o640)
        if meta.st_size + len(line) > LOG_MAX_BYTES:
            previous = LOG.with_name(LOG.name + ".1")
            if os.path.lexists(previous):
                old = previous.lstat()
                if (not stat.S_ISREG(old.st_mode) or old.st_uid != 0 or old.st_gid != 0 or old.st_nlink != 1):
                    raise ModuleError("log_unsafe", "Vorgängerprotokoll ist nicht root-gebunden.")
            os.replace(LOG, previous)
            os.close(fd)
            fd = os.open(LOG, os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
            os.fchmod(fd, 0o640)
        offset = 0
        while offset < len(line):
            offset += os.write(fd, line[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    return result


def execute(argv, *, load_backend=None, detached=False, run_id=None):
    if len(argv) != 2 or argv[0] not in ACTIONS or not re.fullmatch(r"[a-z0-9_]+", argv[1]):
        raise ModuleError("arguments_invalid", "Erlaubt sind ausschließlich eine feste Aktion und ein Modulschlüssel.")
    if os.geteuid() != 0:
        raise ModuleError("root_required", "Modul-Launcher benötigt den festen sudo-Aufruf.")
    os.umask(0o022)
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        raise ModuleError("docker", "Docker-Module werden über Konfiguration und Container-Neustart eingerichtet.")
    if detached:
        return _start_system_job(argv)
    with execution_lock() as update_lock_fd, inspection_scope():
        try:
            return _execute_bound(argv, load_backend=load_backend, update_lock_fd=update_lock_fd, run_id=run_id)
        except Exception as exc:
            try:
                _publish(dict(_failure(exc), **({"run_id": run_id} if run_id else {})))
            except Exception:
                pass
            raise


def _failure(exc):
    return {"success": False, "state": "blocked", "reason_code": getattr(exc, "code", "execution_failed"),
            "message": str(exc) if isinstance(exc, ModuleError) else "Modulauftrag abgebrochen; Installer und Dienststatus administrativ prüfen."}


def _execute_bound(argv, *, load_backend=None, update_lock_fd, run_id=None):
    state = inspect()
    if not state["success"]:
        raise ModuleError(state["reason_code"], state["message"])
    contract = state["contract"]
    action, key = argv
    if key not in contract["modules"]:
        raise ModuleError("module_unknown", "Modul ist nicht in der root-eigenen Freigabeliste enthalten.")
    code_root = contract["code_root"]
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                   "HOME": "/root", "PYTHONDONTWRITEBYTECODE": "1",
                   "E3DC_BOOTSTRAP_ROOT": contract["install_root"],
                   "E3DC_BOOTSTRAP_RUNNER_ROOT": code_root,
                   "E3DC_BOOTSTRAP_USER": contract["install_user"],
                   "E3DC_MODULE_VENV_NAME": contract.get("venv_name", ".venv_e3dc"),
                   "E3DC_INSTALL_ROOT": contract["install_root"],
                   "E3DC_RELEASE_EXPECTED_COMMIT": contract["commit"],
                   "E3DC_UPDATE_LOCK_FD": str(update_lock_fd)}
    os.environ.clear()
    os.environ.update(environment)
    if load_backend is None:
        if any(name == "Installer" or name.startswith("Installer.") for name in sys.modules):
            raise ModuleError("code_unbound", "Installer wurde vor der Codeprüfung geladen.")
        sys.path.insert(0, code_root)
        from Installer import web_installer as web
    else:
        web = load_backend(contract)
    web._MODULE_LAUNCHER_STATE = state
    config = web.load_config()
    if not isinstance(config, dict) or not config:
        raise ModuleError("config_invalid", "Modulkonfiguration ist nicht lesbar.")
    role = str(config.get("ha_mode", "off")).strip().lower()
    anchor = Path("/etc/e3dc-control/instance_role.json")
    if os.path.lexists(anchor):
        value = json.loads(_root_bytes(anchor, maximum=65536))
        if not isinstance(value, dict) or value.get("schema") != 1 or value.get("mode") not in {"off", "master", "slave", "shadow"}:
            raise ModuleError("ha_system", "Rollenbindung unklar. Auf HA-Systemen Module über das Systemupdate einrichten.")
        if value["mode"] != "off":
            role = value["mode"]
    if role != "off":
        raise ModuleError("ha_system", "Auf HA-Systemen Module über das Systemupdate einrichten.")
    module = web.get_module(key)
    if module is None or not module.optional or module.service_unit != contract["modules"][key]["unit"]:
        raise ModuleError("allowlist_invalid", "Modulvertrag und gebundener Dienstkatalog widersprechen sich.")
    unit_path = Path("/etc/systemd/system") / module.service_unit
    _root_directory(unit_path.parent)
    if os.path.lexists(unit_path):
        _root_bytes(unit_path)
    # Der Codeimport läuft aus dem root-eigenen Snapshot mit System-Python.
    # Nur die unprivilegierte Dienstlaufzeit verwendet das lokale venv.
    try:
        web.python_executable()
    except Exception as exc:
        raise ModuleError("venv_missing", "Das gebundene Python-venv fehlt oder ist unsicher; Installation per Konsole prüfen.") from exc
    request = {"success": True, "state": "running", "action": action, "module": key,
               "unit": module.service_unit, "commit": contract["commit"], "run_id": run_id or str(uuid.uuid4())}
    _publish(request)
    try:
        return _run_module(web, key, action, request)
    except BaseException as exc:
        _publish(dict(_failure(exc), run_id=request["run_id"], action=action, module=key))
        raise


def _run_module(web, key, action, request):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = web.install_module(key) if action == "install" else web.remove_module(key)
    ok = bool(result.get("success"))
    reasons = []
    reason_code = "completed" if ok else "installer_blocked"
    backup_fields = {}
    if not ok:
        safe_labels = {"Readiness-Prüfkontext", "Root-eigener Service-Launcher", "Root-eigener Web-Update-Launcher",
                       "Service-Wrapper", "Installer-Wrapper", "Web-Update-Wrapper",
                       "Wrapperintegrität gegen lokalen Git-HEAD", "Wrapperintegrität gegen Root-Snapshot", "sudoers-Datei", "sudoers: Service-Wrapper erlaubt",
                       "sudoers: effektive www-data-Rechte exakt gebunden", "sudoers: Update-Launcher für Installationsbenutzer",
                       "sudoers: kein breiter privilegierter Installer-Webzugang", "keine freien systemctl-Kommandos",
                       "keine direkten www-data-Kommandos", "alter C++ Dienst bleibt gesperrt", "kein aktiver Job-Lock"}
        reasons = ["Freigabeprüfung: " + item["label"] for item in result.get("readiness", {}).get("hard_blockers", [])
                   if item.get("label") in safe_labels]
        for reason in result.get("blocked_reasons", []):
            if isinstance(reason, str) and reason.startswith("Konfiguration unvollständig"):
                reasons.append("Konfiguration des Moduls ist unvollständig.")
            elif isinstance(reason, str) and (reason.startswith("Script fehlt") or reason == "Python-/Node-Script fehlt"):
                reasons.append("Programmdatei des Moduls fehlt.")
            elif isinstance(reason, str) and reason.startswith("package.json fehlt"):
                reasons.append("Paketbeschreibung der Modullaufzeit fehlt.")
            elif reason == "Für den E3DC-Leistungsmesser (PM) ist der Wärmepumpen-Manager nicht erforderlich.":
                reasons.append(reason)
            elif reason == "Dieses Modul wird ausschließlich über das Systemupdate eingerichtet.":
                reasons.append(reason)
        for dependency_key, state in result.get("dependencies", {}).items():
            if not state.get("exists") or not state.get("active"):
                dependency = web.get_module(dependency_key)
                reasons.append("Abhängigkeit muss installiert und aktiv sein: "
                               + (dependency.display_name if dependency is not None else dependency_key))
        if result.get("backup_snapshot", {}).get("success") is False:
            reasons = ["Backup-Snapshot konnte nicht bestätigt werden."]
            snapshot = result.get("backup_snapshot", {})
            code = snapshot.get("reason")
            messages = {"backup_root_unusable": "Backup-Ablage ist nicht verwendbar. Marker, Rechte und Verzeichnis prüfen.",
                        "backup_marker_foreign": "Backup-Marker gehört nicht zu dieser Installation.",
                        "backup_write_failed": "Backup konnte nicht sicher geschrieben und bestätigt werden."}
            if code not in messages:
                code = "backup_write_failed"
            reasons.append(messages[code])
            backup_fields = {"backup_reason_code": code,
                             "_log_detail": snapshot.get("detail") or snapshot.get("message")}
            reason_code = "backup_failed"
        for step in result.get("steps", []):
            if step.get("ok") is False and step.get("step") in {"prepare_runtime_files", "prepare_python_dependencies", "prepare_runner", "restart", "verify_service", "verify_service_stable", "daemon_reload", "enable", "stop", "disable", "remove_unit"}:
                messages = {"prepare_runtime_files": "Laufzeitdateien konnten nicht sicher vorbereitet werden.",
                            "prepare_python_dependencies": "Python-Abhängigkeiten im gebundenen venv fehlen.",
                            "prepare_runner": "Modullaufzeit konnte nicht vorbereitet werden.",
                            "restart": "Moduldienst konnte nicht gestartet werden.",
                            "verify_service": "Stabiler Moduldienst wurde nicht bestätigt.",
                            "verify_service_stable": "Stabiler Moduldienst wurde nicht bestätigt.",
                            "daemon_reload": "Dienstverwaltung konnte die Unit-Änderung nicht übernehmen.",
                            "enable": "Autostart des Moduldienstes konnte nicht aktiviert werden.",
                            "stop": "Moduldienst konnte nicht gestoppt werden.",
                            "disable": "Autostart des Moduldienstes konnte nicht deaktiviert werden.",
                            "remove_unit": "Unit des Moduldienstes konnte nicht entfernt werden."}
                reasons.append(messages[step["step"]])
        if not reasons:
            reasons = ["Modulvoraussetzungen oder stabiler Dienstzustand sind nicht bestätigt; Diagnose öffnen."]
    unit_remains = not ok and action == "install" and any(
        step.get("step") == "write_unit" and step.get("ok") is True for step in result.get("steps", [])
    )
    failure_message = "Modulauftrag fehlgeschlagen. Freigabe, Backup und Dienstlaufzeit in der Installationszentrale prüfen."
    dependency_messages = [reason for reason in reasons if reason.startswith("Abhängigkeit muss installiert und aktiv sein: ")]
    if dependency_messages:
        failure_message += " " + " ".join(dependency_messages)
    if unit_remains:
        failure_message += " Die Unit bleibt eingerichtet und kann über „Entfernen“ zurückgebaut werden."
    # Keine Befehlsausgabe, Konfiguration oder Installationspfade veröffentlichen.
    return _publish({**request, **backup_fields, "success": ok, "state": "done" if ok else "blocked",
                     "reason_code": reason_code,
                     "backup_confirmed": bool(result.get("backup_snapshot", {}).get("success")),
                     "message": "Modulauftrag abgeschlossen." if ok else failure_message,
                     "blocked_reasons": reasons,
                     "steps": [{"index": index + 1, "ok": bool(s.get("ok"))} for index, s in enumerate(result.get("steps", []))]})


def _start_system_job(argv):
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    with execution_lock():
        contract = read_contract()
        if digest(_root_bytes(LAUNCHER, mode=0o755)) != contract.get("launcher_sha256"):
            raise ModuleError("launcher_drift", "Modul-Launcher weicht vom freigegebenen Commit ab.")
        if argv[1] not in contract["modules"]:
            raise ModuleError("module_unknown", "Modul ist nicht in der root-eigenen Freigabeliste enthalten.")
        active = subprocess.run(["/usr/bin/systemctl", "is-active", JOB_UNIT],
                                capture_output=True, text=True, timeout=5, env=environment)
        if active.stdout.strip() in {"active", "activating", "deactivating"}:
            raise ModuleError("busy", "Ein Modulauftrag läuft bereits.")
        run_id = str(uuid.uuid4())
        request = {"success": True, "state": "running", "action": argv[0], "module": argv[1],
                   "run_id": run_id, "commit": contract["commit"], "message": "Modulauftrag gestartet. Ergebnis wird aus der Statusdatei gelesen."}
        _publish(request)
        command = ["/usr/bin/systemd-run", "--quiet", "--collect", "--service-type=exec",
                   "--unit=" + JOB_UNIT, "--property=RuntimeMaxSec=" + str(JOB_MAX_SECONDS),
                   "--property=TimeoutStopSec=15", "--property=KillMode=control-group",
                   "--property=StandardOutput=null", "--property=StandardError=null", "--property=UMask=0022",
                   "--property=ExecStopPost=" + str(LAUNCHER) + " --finish " + run_id,
                   str(LAUNCHER), "--worker", run_id, *argv]
        try:
            started = subprocess.run(command, capture_output=True, text=True, timeout=10, env=environment)
            if started.returncode:
                raise ModuleError("start_failed", "Modulauftrag konnte nicht als Systemdienst gestartet werden.")
        except Exception as exc:
            _publish(dict(_failure(exc), run_id=run_id))
            raise
        return request


def _finish_system_job(run_id):
    """systemd ruft dies nach Ende sämtlicher Prozesse der Auftrags-Unit auf."""
    try:
        with execution_lock(status_only=True):
            try:
                current = json.loads(_root_bytes(STATUS, mode=0o644, maximum=1024 * 1024))
            except (OSError, ValueError, ModuleError):
                return {"success": True}
            if current.get("run_id") != run_id or current.get("state") != "running":
                return {"success": True}
            timeout = os.environ.get("SERVICE_RESULT") == "timeout"
            return _publish(dict(current, success=False, state="blocked",
                                 reason_code="job_timeout" if timeout else "job_interrupted",
                                 message="Gesamtzeitgrenze des Modulauftrags erreicht. Backup und Dienststatus prüfen."
                                 if timeout else "Modulauftrag unterbrochen. Backup und Dienststatus prüfen."))
    except ModuleError as exc:
        if exc.code != "busy":
            raise
        return {"success": True}


def main():
    try:
        args = sys.argv[1:]
        if len(args) in {2, 4} and args[0] in {"--worker", "--finish"}:
            if os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", args[1]):
                raise ModuleError("arguments_invalid", "Interner Systemauftrag ist nicht gültig.")
            if args[0] == "--finish" and len(args) == 2:
                result = _finish_system_job(args[1])
            elif args[0] == "--worker" and len(args) == 4:
                # Startdienst hält die Wartungssperre bis systemd den Prozess bestätigt.
                deadline = time.monotonic() + 5
                while True:
                    try:
                        result = execute(args[2:], run_id=args[1])
                        break
                    except ModuleError as exc:
                        if exc.code != "busy" or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
            else:
                raise ModuleError("arguments_invalid", "Interner Systemauftrag ist nicht gültig.")
        else:
            result = execute(args, detached=True)
    except Exception as exc:
        result = _failure(exc)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
