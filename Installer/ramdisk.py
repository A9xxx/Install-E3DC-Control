import grp
import hashlib
import os
import pwd
import secrets
import shlex
import stat
import subprocess
import tempfile
import time
# register_command NICHT importieren - Ramdisk wird automatisch via
# permissions.py (run_permissions_wizard) eingerichtet, kein eigener Menüpunkt.
from .utils import run_command
from .installer_config import get_install_path, get_install_user, get_home_dir
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning
from .ramdisk_guard import (
    ensure_ramdisk_service_dropins,
    probe_ramdisk_tmpfs,
)

INSTALL_PATH = get_install_path()
RAMDISK_PATH = "/var/www/html/ramdisk"
WEB_ROOT = os.path.dirname(RAMDISK_PATH)
WEB_ROOT_PARENT = os.path.dirname(WEB_ROOT)
RAMDISK_NAME = os.path.basename(RAMDISK_PATH)
GRABBER_SCRIPT = os.path.join(get_home_dir(get_install_user()), "get_live.sh")
FSTAB_PATH = "/etc/fstab"
FINDMNT_PATH = "/usr/bin/findmnt"
SUDO_PATH = "/usr/bin/sudo"
CHOWN_PATH = "/usr/bin/chown"
CHMOD_PATH = "/usr/bin/chmod"
INSTALL_TOOL_PATH = "/usr/bin/install"
MKDIR_PATH = "/usr/bin/mkdir"
MOUNT_PATH = "/usr/bin/mount"
MV_PATH = "/usr/bin/mv"
RM_PATH = "/usr/bin/rm"
SYNC_PATH = "/usr/bin/sync"
SYSTEMCTL_PATH = "/usr/bin/systemctl"
UMOUNT_PATH = "/usr/bin/umount"
CRON_COMMENT = "E3DC Live Grabber"
SERVICE_NAME = "e3dc-grabber"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
MAX_LEGACY_UNIT_BYTES = 256 * 1024
ramdisk_logger = get_or_create_logger("ramdisk")


def _command_error(result, fallback):
    return str(
        result.get("stderr")
        or result.get("stdout")
        or fallback
    ).strip()


def _inode_identity(metadata):
    return metadata.st_dev, metadata.st_ino


def _full_file_identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_directory_nofollow(path, *, dir_fd=None):
    """Öffnet genau den benannten Verzeichnisknoten ohne Link-Fallback."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise RuntimeError("Sicheres Verzeichnis-Öffnen wird nicht unterstützt")
    flags = (
        os.O_RDONLY
        | nofollow
        | directory
        | getattr(os, "O_CLOEXEC", 0)
    )
    if dir_fd is None:
        named = os.lstat(path)
        descriptor = os.open(path, flags)
    else:
        named = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        descriptor = os.open(path, flags, dir_fd=dir_fd)
    opened = os.fstat(descriptor)
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or _inode_identity(named) != _inode_identity(opened)
    ):
        os.close(descriptor)
        raise RuntimeError(f"Verzeichnisidentität ist nicht eindeutig: {path}")
    return descriptor


def _verify_named_directory(descriptor, path, *, dir_fd=None):
    opened = os.fstat(descriptor)
    if dir_fd is None:
        named = os.lstat(path)
    else:
        named = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or _inode_identity(named) != _inode_identity(opened)
    ):
        raise RuntimeError(f"Benannter Verzeichnisknoten driftete: {path}")
    return opened


def _require_trusted_root_directory_descriptor(descriptor, path):
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError(
            f"Elternverzeichnis der Systemdatei ist nicht vertrauenswürdig: {path}"
        )
    return metadata


def _set_bound_directory_contract(descriptor, uid, gid, mode, label):
    """Setzt Metadaten über den bereits gebundenen Inode statt über den Namen."""

    descriptor_path = f"/proc/{os.getpid()}/fd/{descriptor}"
    for command in (
        f"{SUDO_PATH} {CHOWN_PATH} --dereference "
        f"{int(uid)}:{int(gid)} -- {shlex.quote(descriptor_path)}",
        f"{SUDO_PATH} {CHMOD_PATH} {int(mode):04o} -- "
        + shlex.quote(descriptor_path),
    ):
        result = run_command(command, timeout=15)
        if not result.get("success"):
            freeze_result = run_command(
                f"{SUDO_PATH} {CHMOD_PATH} 0555 -- "
                + shlex.quote(descriptor_path),
                timeout=15,
            )
            freeze_note = (
                "; Inode wurde fail-closed auf 0555 eingefroren"
                if freeze_result.get("success")
                else "; auch das fail-closed Einfrieren auf 0555 schlug fehl"
            )
            raise RuntimeError(
                f"{label} konnte nicht gebunden gesetzt werden: "
                + _command_error(result, "unbekannter Metadatenfehler")
                + freeze_note
            )
    changed = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(changed.st_mode)
        or changed.st_uid != int(uid)
        or changed.st_gid != int(gid)
        or stat.S_IMODE(changed.st_mode) != int(mode)
    ):
        run_command(
            f"{SUDO_PATH} {CHMOD_PATH} 0555 -- "
            + shlex.quote(descriptor_path),
            timeout=15,
        )
        raise RuntimeError(f"{label} ist nach der Änderung nicht nachgewiesen")


def _require_trusted_web_parent():
    for path in ("/", "/var", WEB_ROOT_PARENT):
        metadata = os.lstat(path)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError(
                f"Elternverzeichnis ist nicht root-kontrolliert: {path}"
            )


def _enforce_web_root_contract():
    """Bindet den RAM-Disk-Namen an ein dauerhaft nicht beschreibbares Webroot."""

    _require_trusted_web_parent()
    web_gid = grp.getgrnam("www-data").gr_gid
    try:
        descriptor = _open_directory_nofollow(WEB_ROOT)
    except FileNotFoundError:
        create_result = run_command(
            f"{SUDO_PATH} {MKDIR_PATH} -m 0755 -- {shlex.quote(WEB_ROOT)}",
            timeout=15,
        )
        if not create_result.get("success"):
            raise RuntimeError(
                "Webroot konnte nicht erstellt werden: "
                + _command_error(create_result, "unbekannter mkdir-Fehler")
            )
        descriptor = _open_directory_nofollow(WEB_ROOT)
    try:
        _set_bound_directory_contract(
            descriptor,
            0,
            web_gid,
            0o755,
            "Webroot-Elternvertrag",
        )
        opened = _verify_named_directory(descriptor, WEB_ROOT)
        if (
            opened.st_uid != 0
            or opened.st_gid != web_gid
            or stat.S_IMODE(opened.st_mode) != 0o755
        ):
            raise RuntimeError("Webroot-Elternvertrag ist nicht exakt wirksam")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _prepare_ramdisk_mountpoint():
    """Erzeugt einen namenssicheren, root-kontrollierten Mountpoint."""

    web_descriptor = _enforce_web_root_contract()
    mount_descriptor = -1
    try:
        _verify_named_directory(web_descriptor, WEB_ROOT)
        try:
            mount_descriptor = _open_directory_nofollow(
                RAMDISK_NAME,
                dir_fd=web_descriptor,
            )
        except FileNotFoundError:
            create_result = run_command(
                f"{SUDO_PATH} {MKDIR_PATH} -m 0755 -- "
                + shlex.quote(RAMDISK_PATH),
                timeout=15,
            )
            if not create_result.get("success"):
                raise RuntimeError(
                    "RAM-Disk-Mountpoint konnte nicht erstellt werden: "
                    + _command_error(create_result, "unbekannter mkdir-Fehler")
                )
            mount_descriptor = _open_directory_nofollow(
                RAMDISK_NAME,
                dir_fd=web_descriptor,
            )

        initial_probe = probe_ramdisk_tmpfs()
        mounted_before = bool(initial_probe.get("ok"))
        if not mounted_before and initial_probe.get("reason") != "root_fallback":
            raise RuntimeError(
                "RAM-Disk-Ziel besitzt vor dem Setup keinen eindeutigen "
                f"Unmounted-Zustand: {initial_probe.get('reason') or 'unbekannt'}"
            )
        if not mounted_before:
            _set_bound_directory_contract(
                mount_descriptor,
                0,
                0,
                0o755,
                "persistenter RAM-Disk-Mountpoint",
            )
        _verify_named_directory(
            mount_descriptor,
            RAMDISK_NAME,
            dir_fd=web_descriptor,
        )
        return mounted_before
    finally:
        if mount_descriptor >= 0:
            os.close(mount_descriptor)
        os.close(web_descriptor)


def _set_runtime_ramdisk_contract(install_user):
    """Setzt 2775 am gebundenen tmpfs-Inode und prüft den Namen erneut."""

    install_uid = pwd.getpwnam(install_user).pw_uid
    web_gid = grp.getgrnam("www-data").gr_gid
    web_descriptor = _enforce_web_root_contract()
    mount_descriptor = -1
    try:
        mount_descriptor = _open_directory_nofollow(
            RAMDISK_NAME,
            dir_fd=web_descriptor,
        )
        if not probe_ramdisk_tmpfs().get("ok"):
            raise RuntimeError("RAM-Disk ist vor dem Rechtevertrag kein exaktes tmpfs")
        _set_bound_directory_contract(
            mount_descriptor,
            install_uid,
            web_gid,
            0o2775,
            "RAM-Disk-Laufzeitvertrag",
        )
        opened = _verify_named_directory(
            mount_descriptor,
            RAMDISK_NAME,
            dir_fd=web_descriptor,
        )
        if (
            opened.st_uid != install_uid
            or opened.st_gid != web_gid
            or stat.S_IMODE(opened.st_mode) != 0o2775
            or not probe_ramdisk_tmpfs().get("ok")
        ):
            raise RuntimeError("RAM-Disk-Laufzeitvertrag ist nicht exakt wirksam")
    finally:
        if mount_descriptor >= 0:
            os.close(mount_descriptor)
        os.close(web_descriptor)


def _validate_fstab_file(path):
    """Validate an fstab file when findmnt is available."""
    if not os.path.isfile(FINDMNT_PATH):
        return False, "findmnt fehlt; fstab kann nicht sicher validiert werden"
    result = run_command(
        f"{FINDMNT_PATH} --verify --tab-file {shlex.quote(path)}",
        timeout=20,
    )
    if result.get("success"):
        return True, result.get("stdout", "")
    return False, (result.get("stderr") or result.get("stdout") or "").strip()


def _trusted_regular_file(path):
    metadata = os.lstat(path)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError(f"Systemdatei ist nicht vertrauenswürdig: {path}")
    return metadata


def _capture_trusted_file_snapshot(path, *, max_bytes=None):
    before = _trusted_regular_file(path)
    if max_bytes is not None and before.st_size > int(max_bytes):
        raise RuntimeError(f"Systemdatei überschreitet das Größenlimit: {path}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise RuntimeError("Sicheres Systemdatei-Öffnen wird nicht unterstützt")
    descriptor = os.open(
        path,
        os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _full_file_identity(opened) != _full_file_identity(before)
        ):
            raise RuntimeError(f"Systemdatei driftete beim sicheren Öffnen: {path}")
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        opened_after = os.fstat(descriptor)
        named_after = _trusted_regular_file(path)
        if (
            len(payload) != before.st_size
            or _full_file_identity(opened_after) != _full_file_identity(before)
            or _full_file_identity(named_after) != _full_file_identity(before)
        ):
            raise RuntimeError(f"Systemdatei driftete beim Lesen: {path}")
        return {
            "path": path,
            "identity": _full_file_identity(before),
            "mode": stat.S_IMODE(before.st_mode),
            "uid": before.st_uid,
            "gid": before.st_gid,
            "bytes": payload,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    finally:
        os.close(descriptor)


def _same_file_snapshot(first, second, *, require_identity):
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    if require_identity and first.get("identity") != second.get("identity"):
        return False
    return (
        first.get("mode") == second.get("mode")
        and first.get("uid") == second.get("uid")
        and first.get("gid") == second.get("gid")
        and first.get("bytes") == second.get("bytes")
        and first.get("sha256") == second.get("sha256")
    )


def _read_fstab_lines():
    snapshot = _capture_trusted_file_snapshot(FSTAB_PATH)
    try:
        text = snapshot["bytes"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("fstab ist nicht gültig UTF-8-kodiert") from exc
    return snapshot, text.splitlines(keepends=True)


def _read_trusted_regular_bytes(path):
    return _capture_trusted_file_snapshot(path)["bytes"]


def _atomic_install_fstab(
    source_path,
    *,
    expected_content,
    mode,
    expected_prestate=None,
):
    """Installiert eine validierte fstab per Rename im root-kontrollierten /etc."""

    stage_path = (
        f"/etc/.fstab.e3dc-stage-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        stage_result = run_command(
            f"{SUDO_PATH} {INSTALL_TOOL_PATH} -o root -g root "
            f"-m {int(mode):04o} -- "
            f"{shlex.quote(source_path)} {shlex.quote(stage_path)}",
            timeout=20,
        )
        if not stage_result.get("success"):
            raise RuntimeError(
                "fstab-Staging fehlgeschlagen: "
                + _command_error(stage_result, "unbekannter install-Fehler")
            )
        _trusted_regular_file(stage_path)
        if _read_trusted_regular_bytes(stage_path) != bytes(expected_content):
            raise RuntimeError("fstab-Staging weicht vom gebundenen Sollinhalt ab")
        valid, message = _validate_fstab_file(stage_path)
        if not valid:
            raise RuntimeError(f"fstab-Staging ist ungültig: {message}")
        if expected_prestate is not None:
            rebound = _capture_trusted_file_snapshot(FSTAB_PATH)
            if not _same_file_snapshot(
                rebound,
                expected_prestate,
                require_identity=True,
            ):
                raise RuntimeError(
                    "fstab-Prestate driftete unmittelbar vor dem atomaren Commit"
                )
        replace_result = run_command(
            f"{SUDO_PATH} {MV_PATH} -fT -- "
            f"{shlex.quote(stage_path)} {shlex.quote(FSTAB_PATH)}",
            timeout=20,
        )
        if not replace_result.get("success"):
            raise RuntimeError(
                "Atomarer fstab-Ersatz fehlgeschlagen: "
                + _command_error(replace_result, "unbekannter mv-Fehler")
            )
        installed = _capture_trusted_file_snapshot(FSTAB_PATH)
        if int(installed["mode"]) != int(mode):
            raise RuntimeError("Installierte fstab besitzt nicht den gebundenen Modus")
        if installed["bytes"] != bytes(expected_content):
            raise RuntimeError("Installierte fstab weicht vom gebundenen Sollinhalt ab")
        if installed["sha256"] != hashlib.sha256(bytes(expected_content)).hexdigest():
            raise RuntimeError("Installierte fstab besitzt nicht den gebundenen SHA-256")
        valid, message = _validate_fstab_file(FSTAB_PATH)
        if not valid:
            raise RuntimeError(f"Installierte fstab ist ungültig: {message}")
        sync_result = run_command(
            f"{SUDO_PATH} {SYNC_PATH} -f {shlex.quote(FSTAB_PATH)}",
            timeout=20,
        )
        if not sync_result.get("success"):
            raise RuntimeError(
                "fstab konnte nicht dauerhaft synchronisiert werden: "
                + _command_error(sync_result, "unbekannter sync-Fehler")
            )
        persisted = _capture_trusted_file_snapshot(FSTAB_PATH)
        if not _same_file_snapshot(installed, persisted, require_identity=True):
            raise RuntimeError("Installierte fstab driftete während der Synchronisierung")
        return persisted
    finally:
        if os.path.lexists(stage_path):
            run_command(
                f"{SUDO_PATH} {RM_PATH} -f -- {shlex.quote(stage_path)}",
                timeout=10,
            )


def _write_fstab_safely(lines, expected_prestate):
    """Schreibt /etc/fstab validiert, gesichert und atomar."""

    backup_path = (
        f"{FSTAB_PATH}.e3dc-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        f"-{secrets.token_hex(6)}"
    )
    backup_source_path = ""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.writelines(lines)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    try:
        ok, message = _validate_fstab_file(tmp_path)
        if not ok:
            return False, None, f"Neue fstab ist ungültig: {message}"
        current_snapshot = _capture_trusted_file_snapshot(FSTAB_PATH)
        if not _same_file_snapshot(
            current_snapshot,
            expected_prestate,
            require_identity=True,
        ):
            return False, None, "fstab-Prestate driftete vor der Backup-Erzeugung"
        current_valid, current_message = _validate_fstab_file(FSTAB_PATH)
        if not current_valid:
            return (
                False,
                None,
                f"Bestehende fstab ist kein sicherer Rückfallstand: {current_message}",
            )
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as backup_source:
            backup_source.write(expected_prestate["bytes"])
            backup_source.flush()
            os.fsync(backup_source.fileno())
            backup_source_path = backup_source.name
        backup_result = run_command(
            f"{SUDO_PATH} {INSTALL_TOOL_PATH} -o root -g root "
            f"-m {int(expected_prestate['mode']):04o} -- "
            f"{shlex.quote(backup_source_path)} {shlex.quote(backup_path)}",
            timeout=20,
        )
        if not backup_result.get("success"):
            return (
                False,
                None,
                "fstab-Backup fehlgeschlagen: "
                + _command_error(backup_result, "unbekannter install-Fehler"),
            )
        backup_snapshot = _capture_trusted_file_snapshot(backup_path)
        if not _same_file_snapshot(
            backup_snapshot,
            expected_prestate,
            require_identity=False,
        ):
            return False, None, "fstab-Backup weicht vom gebundenen Prestate ab"
        backup_valid, backup_message = _validate_fstab_file(backup_path)
        if not backup_valid:
            return (
                False,
                None,
                f"fstab-Backup ist ungültig: {backup_message}",
            )
        transaction = {
            "backup_path": backup_path,
            "prestate": expected_prestate,
        }
        intended_bytes = "".join(lines).encode("utf-8")
        try:
            poststate = _atomic_install_fstab(
                tmp_path,
                expected_content=intended_bytes,
                mode=int(expected_prestate["mode"]),
                expected_prestate=expected_prestate,
            )
            transaction["poststate"] = poststate
        except Exception as exc:
            try:
                current_after_error = _capture_trusted_file_snapshot(FSTAB_PATH)
            except Exception:
                return (
                    False,
                    transaction,
                    f"Atomarer fstab-Ersatz fehlgeschlagen; Endzustand unlesbar: {exc}",
                )
            if _same_file_snapshot(
                current_after_error,
                expected_prestate,
                require_identity=True,
            ):
                return False, None, f"fstab-Commit vor Mutation abgebrochen: {exc}"
            intended_sha = hashlib.sha256(intended_bytes).hexdigest()
            if (
                current_after_error.get("bytes") != intended_bytes
                or current_after_error.get("sha256") != intended_sha
            ):
                return (
                    False,
                    transaction,
                    "fstab driftete außerhalb der gebundenen Transaktion; "
                    f"kein fremder Zustand wurde überschrieben: {exc}",
                )
            transaction["poststate"] = current_after_error
            if not _restore_fstab(transaction, reload_systemd=False):
                return (
                    False,
                    transaction,
                    f"Atomarer fstab-Ersatz fehlgeschlagen und Rückfall unsicher: {exc}",
                )
            return (
                False,
                None,
                f"Atomarer fstab-Ersatz fehlgeschlagen; Backup wiederhergestellt: {exc}",
            )
        return True, transaction, ""
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        if backup_source_path:
            try:
                os.unlink(backup_source_path)
            except Exception:
                pass


def _restore_fstab(transaction, *, reload_systemd=True):
    if isinstance(transaction, dict):
        backup_path = str(transaction.get("backup_path") or "")
        prestate = transaction.get("prestate")
        poststate = transaction.get("poststate")
    else:
        backup_path = ""
        prestate = None
        poststate = None
    if backup_path and isinstance(prestate, dict) and os.path.lexists(backup_path):
        try:
            backup_metadata = _trusted_regular_file(backup_path)
            valid, _ = _validate_fstab_file(backup_path)
            if not valid:
                return False
            backup_snapshot = _capture_trusted_file_snapshot(backup_path)
            if not _same_file_snapshot(
                backup_snapshot,
                prestate,
                require_identity=False,
            ):
                return False
            if not isinstance(poststate, dict):
                return False
            current = _capture_trusted_file_snapshot(FSTAB_PATH)
            if not _same_file_snapshot(
                current,
                poststate,
                require_identity=True,
            ):
                return False
            _atomic_install_fstab(
                backup_path,
                expected_content=prestate["bytes"],
                mode=stat.S_IMODE(backup_metadata.st_mode),
                expected_prestate=poststate,
            )
            restored = _capture_trusted_file_snapshot(FSTAB_PATH)
            if not _same_file_snapshot(
                restored,
                prestate,
                require_identity=False,
            ):
                return False
            if reload_systemd:
                reload_result = run_command(
                    f"{SUDO_PATH} {SYSTEMCTL_PATH} daemon-reload",
                    timeout=15,
                )
                return bool(reload_result.get("success"))
            return True
        except Exception:
            return False
    return False


def _rollback_mount_transaction(
    *,
    mounted_by_setup,
    fstab_changed,
    fstab_transaction,
):
    """Rollt nur den in dieser Transaktion neu erzeugten Mount zurück."""

    success = True
    if mounted_by_setup:
        unmount_result = run_command(
            f"{SUDO_PATH} {UMOUNT_PATH} -- {shlex.quote(RAMDISK_PATH)}",
            timeout=20,
        )
        unmounted_probe = probe_ramdisk_tmpfs()
        if (
            not unmount_result.get("success")
            or unmounted_probe.get("ok")
            or unmounted_probe.get("reason") != "root_fallback"
        ):
            success = False
            log_error(
                "ramdisk",
                "Neu erzeugter RAM-Disk-Mount konnte nicht sicher zurückgerollt "
                f"werden: {_command_error(unmount_result, 'Unmount nicht nachgewiesen')}",
            )
    if fstab_changed and not _restore_fstab(fstab_transaction):
        success = False
        log_error("ramdisk", "fstab konnte nicht atomar zurückgerollt werden")
    return success


def _remove_legacy_grabber_script():
    result = run_command(
        f"{SUDO_PATH} {RM_PATH} -f -- {shlex.quote(GRABBER_SCRIPT)}"
    )
    if result.get("success") and not os.path.lexists(GRABBER_SCRIPT):
        return True, ""
    return False, _command_error(result, "Datei ist weiterhin vorhanden")


def _legacy_systemd_state():
    result = run_command(
        f"{SYSTEMCTL_PATH} show --no-pager "
        "--property=LoadState --property=ActiveState --property=UnitFileState "
        + shlex.quote(SERVICE_NAME),
        timeout=15,
    )
    if not result.get("success"):
        raise RuntimeError(
            "Legacy-Grabber-Zustand ist nicht lesbar: "
            + _command_error(result, "unbekannter systemd-Fehler")
        )
    state = {}
    for line in str(result.get("stdout") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            state[key.strip()] = value.strip().lower()
    if not {"LoadState", "ActiveState", "UnitFileState"}.issubset(state):
        raise RuntimeError("Legacy-Grabber-Zustand ist unvollständig")
    return state


def _capture_legacy_unit_prestate():
    if os.path.lexists(SERVICE_PATH):
        file_snapshot = _capture_trusted_file_snapshot(
            SERVICE_PATH,
            max_bytes=MAX_LEGACY_UNIT_BYTES,
        )
    else:
        file_snapshot = None
    state = _legacy_systemd_state()
    if file_snapshot is None:
        if (
            state.get("LoadState") != "not-found"
            or state.get("ActiveState") != "inactive"
            or state.get("UnitFileState") not in {"", "disabled"}
        ):
            raise RuntimeError("Fehlende Legacy-Unit besitzt keinen inerten Zustand")
    else:
        if state.get("LoadState") != "loaded":
            raise RuntimeError("Vorhandene Legacy-Unit ist nicht eindeutig geladen")
        if state.get("ActiveState") not in {"active", "inactive"}:
            raise RuntimeError("Legacy-Unit befindet sich in einem Übergangszustand")
        if state.get("UnitFileState") not in {
            "enabled",
            "enabled-runtime",
            "disabled",
        }:
            raise RuntimeError("Legacy-Unit besitzt keinen rückrollbaren Enable-Zustand")
    return {
        "file": file_snapshot,
        "state": state,
        "removed": False,
        "committed": False,
    }


def _write_file_snapshot_atomically(path, snapshot):
    parent = os.path.dirname(path)
    filename = os.path.basename(path)
    parent_descriptor = _open_directory_nofollow(parent)
    temporary = f".{filename}.{os.getpid()}.{secrets.token_hex(8)}.restore"
    descriptor = -1
    try:
        _require_trusted_root_directory_descriptor(parent_descriptor, parent)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent_descriptor)
        payload = bytes(snapshot["bytes"])
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise RuntimeError("Systemdatei konnte nicht vollständig gestaged werden")
            offset += written
        os.fchown(descriptor, int(snapshot["uid"]), int(snapshot["gid"]))
        os.fchmod(descriptor, int(snapshot["mode"]))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.stat(filename, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError(f"Systemdateiname erschien vor dem Restore: {path}")
        os.replace(
            temporary,
            filename,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)
    restored = _capture_trusted_file_snapshot(path)
    if not _same_file_snapshot(restored, snapshot, require_identity=False):
        raise RuntimeError(f"Systemdatei-Preimage wurde nicht exakt restauriert: {path}")


def _restore_legacy_unit_prestate(transaction):
    if not isinstance(transaction, dict) or transaction.get("committed"):
        return bool(isinstance(transaction, dict) and transaction.get("committed"))
    pre_file = transaction.get("file")
    pre_state = transaction.get("state") or {}
    try:
        if pre_file is None:
            if os.path.lexists(SERVICE_PATH):
                return False
        else:
            try:
                current_file = _capture_trusted_file_snapshot(SERVICE_PATH)
            except FileNotFoundError:
                current_file = None
            if transaction.get("removed"):
                if current_file is None:
                    _write_file_snapshot_atomically(SERVICE_PATH, pre_file)
                elif not _same_file_snapshot(
                    current_file,
                    pre_file,
                    require_identity=False,
                ):
                    return False
            elif not _same_file_snapshot(
                current_file,
                pre_file,
                require_identity=True,
            ):
                return False
        reload_result = run_command(
            f"{SUDO_PATH} {SYSTEMCTL_PATH} daemon-reload",
            timeout=20,
        )
        if not reload_result.get("success"):
            return False
        unit_file_state = pre_state.get("UnitFileState")
        if unit_file_state == "enabled":
            enable_command = (
                f"{SUDO_PATH} {SYSTEMCTL_PATH} enable {shlex.quote(SERVICE_NAME)}"
            )
        elif unit_file_state == "enabled-runtime":
            enable_command = (
                f"{SUDO_PATH} {SYSTEMCTL_PATH} enable --runtime "
                + shlex.quote(SERVICE_NAME)
            )
        elif unit_file_state == "disabled" and pre_file is not None:
            enable_command = (
                f"{SUDO_PATH} {SYSTEMCTL_PATH} disable {shlex.quote(SERVICE_NAME)}"
            )
        else:
            enable_command = ""
        if enable_command:
            enable_result = run_command(enable_command, timeout=20)
            if not enable_result.get("success"):
                return False
        if pre_state.get("ActiveState") == "active":
            active_command = (
                f"{SUDO_PATH} {SYSTEMCTL_PATH} start {shlex.quote(SERVICE_NAME)}"
            )
        elif pre_file is not None:
            active_command = (
                f"{SUDO_PATH} {SYSTEMCTL_PATH} stop {shlex.quote(SERVICE_NAME)}"
            )
        else:
            active_command = ""
        if active_command:
            active_result = run_command(active_command, timeout=20)
            if not active_result.get("success"):
                return False
        restored_state = _legacy_systemd_state()
        if (
            restored_state.get("LoadState") != pre_state.get("LoadState")
            or restored_state.get("ActiveState") != pre_state.get("ActiveState")
            or restored_state.get("UnitFileState") != pre_state.get("UnitFileState")
        ):
            return False
        if pre_file is not None:
            restored_file = _capture_trusted_file_snapshot(SERVICE_PATH)
            if not _same_file_snapshot(
                restored_file,
                pre_file,
                require_identity=False,
            ):
                return False
        transaction["restored"] = True
        return True
    except Exception:
        return False


def _remove_legacy_unit_transactionally():
    transaction = _capture_legacy_unit_prestate()
    if transaction["file"] is None:
        transaction["committed"] = True
        return transaction
    try:
        stop_result = run_command(
            f"{SUDO_PATH} {SYSTEMCTL_PATH} stop {shlex.quote(SERVICE_NAME)}",
            timeout=20,
        )
        if not stop_result.get("success"):
            raise RuntimeError(
                "Legacy-Grabber konnte nicht gestoppt werden: "
                + _command_error(stop_result, "unbekannter stop-Fehler")
            )
        stopped_state = _legacy_systemd_state()
        if (
            stopped_state.get("LoadState") != "loaded"
            or stopped_state.get("ActiveState") != "inactive"
        ):
            raise RuntimeError("Legacy-Grabber ist nach stop nicht sicher inaktiv")
        disable_result = run_command(
            f"{SUDO_PATH} {SYSTEMCTL_PATH} disable {shlex.quote(SERVICE_NAME)}",
            timeout=20,
        )
        if not disable_result.get("success"):
            raise RuntimeError(
                "Legacy-Grabber konnte nicht deaktiviert werden: "
                + _command_error(disable_result, "unbekannter disable-Fehler")
            )
        disabled_state = _legacy_systemd_state()
        if (
            disabled_state.get("LoadState") != "loaded"
            or disabled_state.get("ActiveState") != "inactive"
            or disabled_state.get("UnitFileState") != "disabled"
        ):
            raise RuntimeError("Legacy-Grabber ist nicht sicher inaktiv und disabled")
        parent_descriptor = _open_directory_nofollow(os.path.dirname(SERVICE_PATH))
        try:
            _require_trusted_root_directory_descriptor(
                parent_descriptor,
                os.path.dirname(SERVICE_PATH),
            )
            filename = os.path.basename(SERVICE_PATH)
            bound_path = f"/proc/{os.getpid()}/fd/{parent_descriptor}/{filename}"
            rebound = _capture_trusted_file_snapshot(
                bound_path,
                max_bytes=MAX_LEGACY_UNIT_BYTES,
            )
            if not _same_file_snapshot(
                rebound,
                transaction["file"],
                require_identity=True,
            ):
                raise RuntimeError(
                    "Legacy-Unit driftete unmittelbar vor dem Entfernen"
                )
            os.unlink(filename, dir_fd=parent_descriptor)
            transaction["removed"] = True
            os.fsync(parent_descriptor)
            try:
                os.stat(filename, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("Legacy-Unit blieb nach dem Entfernen benannt")
        finally:
            os.close(parent_descriptor)
        reload_result = run_command(
            f"{SUDO_PATH} {SYSTEMCTL_PATH} daemon-reload",
            timeout=20,
        )
        if not reload_result.get("success"):
            raise RuntimeError("systemd-Reload nach Legacy-Entfernung fehlgeschlagen")
        removed_state = _legacy_systemd_state()
        if (
            os.path.lexists(SERVICE_PATH)
            or removed_state.get("LoadState") != "not-found"
            or removed_state.get("ActiveState") != "inactive"
            or removed_state.get("UnitFileState") not in {"", "disabled"}
        ):
            raise RuntimeError("Legacy-Unit ist nach Entfernung nicht sicher verschwunden")
        return transaction
    except Exception as exc:
        if not _restore_legacy_unit_prestate(transaction):
            raise RuntimeError(
                f"Legacy-Unit-Transaktion fehlgeschlagen und Rückfall unsicher: {exc}"
            ) from exc
        raise RuntimeError(
            f"Legacy-Unit-Transaktion fehlgeschlagen; Prestate restauriert: {exc}"
        ) from exc


def remove_legacy_grabber_unit_transactionally():
    """Entfernt ausschließlich die Legacy-Unit idempotent und transaktional."""

    try:
        transaction = _remove_legacy_unit_transactionally()
    except Exception as exc:
        log_error(
            "ramdisk",
            f"Legacy-Grabber-Unit konnte nicht transaktional entfernt werden: {exc}",
            exc,
        )
        return False
    transaction["committed"] = True
    return True


def _rollback_ramdisk_precommit(
    *,
    legacy_transaction,
    mounted_by_setup=False,
    fstab_transaction=None,
):
    storage_ok = _rollback_mount_transaction(
        mounted_by_setup=mounted_by_setup,
        fstab_changed=fstab_transaction is not None,
        fstab_transaction=fstab_transaction,
    )
    legacy_ok = _restore_legacy_unit_prestate(legacy_transaction)
    return storage_ok and legacy_ok


def setup_ramdisk():
    """Richtet die RAM-Disk ein."""
    print("\n=== RAM-Disk Setup ===\n")
    ramdisk_logger.info("Starte RAM-Disk Setup.")

    # Die Legacy-Unit wird erst nach vollständigem Prestate-Snapshot gestoppt,
    # deaktiviert und nachgewiesen inaktiv. Erst dann darf die gebundene Datei
    # verschwinden. Bis zum RAM-Disk-Kerncommit bleibt der Prestate rückrollbar.
    try:
        legacy_transaction = _remove_legacy_unit_transactionally()
    except Exception as exc:
        print(f"  ✗ Legacy-Grabber-Transaktion fehlgeschlagen: {exc}")
        log_error("ramdisk", f"Legacy-Grabber-Transaktion fehlgeschlagen: {exc}", exc)
        return False

    install_user = get_install_user()

    # 1. Der unmittelbare Elternknoten ist dauerhaft root-kontrolliert. Nur so
    # bleibt der Name auch vor dem Boot-Mount und nach einem Unmount bindend;
    # Rechte allein am später beschreibbaren tmpfs schützen keinen Directory-
    # Entry in einem durch den Laufzeitnutzer beschreibbaren Elternverzeichnis.
    print("→ Binde sicheren RAM-Disk-Mountpoint…")
    try:
        mounted_before = _prepare_ramdisk_mountpoint()
    except Exception as exc:
        print(f"  ✗ RAM-Disk-Mountpoint ist nicht sicher: {exc}")
        log_error("ramdisk", f"Unsicherer RAM-Disk-Mountpoint: {exc}", exc)
        if not _restore_legacy_unit_prestate(legacy_transaction):
            print("  ✗ Legacy-Unit-Prestate konnte nicht restauriert werden.")
        return False
    ramdisk_logger.info(
        "RAM-Disk-Mountpoint ist root-kontrolliert und namenssicher gebunden."
    )
    
    # 2. fstab Eintrag
    print("→ Konfiguriere /etc/fstab für tmpfs…")
    # UID/GID dynamisch ermitteln; numerische Distributionsannahmen sind für
    # den Boot-Mount nicht zulässig.
    try:
        user_uid = pwd.getpwnam(install_user).pw_uid
        www_data_gid = grp.getgrnam("www-data").gr_gid
    except Exception as e:
        print(f"  ✗ Fehler beim Ermitteln der RAM-Disk-Konten: {e}")
        log_error("ramdisk", f"RAM-Disk-Konten konnten nicht ermittelt werden: {e}", e)
        if not _restore_legacy_unit_prestate(legacy_transaction):
            print("  ✗ Legacy-Unit-Prestate konnte nicht restauriert werden.")
        return False
    fstab_entry = (
        f"tmpfs {RAMDISK_PATH} tmpfs "
        f"nodev,nosuid,size=32M,uid={user_uid},gid={www_data_gid},mode=2775 0 0"
    )
    
    fstab_transaction = None
    fstab_changed = False
    try:
        fstab_prestate, lines = _read_fstab_lines()
        new_lines = []
        replaced = False
        for line in lines:
            stripped = line.strip()
            fields = stripped.split() if stripped and not stripped.startswith("#") else []
            if len(fields) >= 2 and fields[1] == RAMDISK_PATH:
                if not replaced:
                    new_lines.append(fstab_entry + "\n")
                    replaced = True
                continue
            new_lines.append(line)
        if not replaced:
            if new_lines and not new_lines[-1].endswith(("\n", "\r")):
                new_lines[-1] += "\n"
            new_lines.append(fstab_entry + "\n")
            print("  ✓ Eintrag hinzugefügt")
            ramdisk_logger.info("fstab-Eintrag für RAM-Disk hinzugefügt.")
        else:
            print("  ✓ Eintrag überschrieben")
            ramdisk_logger.info("fstab-Eintrag für RAM-Disk aktualisiert.")
        fstab_changed = new_lines != lines
        if fstab_changed:
            ok, fstab_transaction, error = _write_fstab_safely(
                new_lines,
                fstab_prestate,
            )
            if not ok:
                raise RuntimeError(error)
            print("  → Lade systemd-Konfiguration neu…")
            rebound = _capture_trusted_file_snapshot(FSTAB_PATH)
            if not _same_file_snapshot(
                rebound,
                fstab_transaction.get("poststate"),
                require_identity=True,
            ):
                raise RuntimeError("fstab driftete vor dem systemd-Reload")
            reload_result = run_command(
                f"{SUDO_PATH} {SYSTEMCTL_PATH} daemon-reload"
            )
            if not reload_result.get("success"):
                raise RuntimeError(
                    _command_error(reload_result, "unbekannter daemon-reload-Fehler")
                )
            rebound = _capture_trusted_file_snapshot(FSTAB_PATH)
            if not _same_file_snapshot(
                rebound,
                fstab_transaction.get("poststate"),
                require_identity=True,
            ):
                raise RuntimeError("fstab driftete während des systemd-Reloads")
        else:
            print("  ✓ fstab-Eintrag ist bereits exakt aktuell")
    except Exception as e:
        print(f"  ✗ Fehler beim Bearbeiten von fstab: {e}")
        log_error("ramdisk", f"Fehler beim Bearbeiten von /etc/fstab: {e}", e)
        rollback_ok = _rollback_ramdisk_precommit(
            legacy_transaction=legacy_transaction,
            fstab_transaction=fstab_transaction,
        )
        if not rollback_ok:
            print("  ✗ fstab-/Legacy-Rückfall ist nicht vollständig nachgewiesen.")
        return False

    # 3. Mounten
    print("→ Mounte RAM-Disk…")
    mounted_by_setup = False
    if not mounted_before:
        mount_result = run_command(
            f"{SUDO_PATH} {MOUNT_PATH} {shlex.quote(RAMDISK_PATH)}",
            timeout=20,
        )
        if not mount_result.get("success"):
            mounted_by_setup = bool(probe_ramdisk_tmpfs().get("ok"))
            error = _command_error(mount_result, "unbekannter mount-Fehler")
            print(f"  ✗ RAM-Disk konnte nicht gemountet werden: {error.strip()}")
            log_error("ramdisk", f"RAM-Disk Mount fehlgeschlagen: {error}")
            if _rollback_ramdisk_precommit(
                legacy_transaction=legacy_transaction,
                mounted_by_setup=mounted_by_setup,
                fstab_transaction=fstab_transaction,
            ):
                print("  ✓ Mount-/fstab-/Legacy-Zustand vollständig zurückgerollt.")
            else:
                print("  ✗ Mount-/fstab-/Legacy-Rückfall ist nicht vollständig nachgewiesen.")
            return False
        mounted_by_setup = True

    mount_probe = probe_ramdisk_tmpfs()
    if not mount_probe.get("ok"):
        reason = str(mount_probe.get("reason") or "unbekannt")
        target = str(mount_probe.get("target") or "nicht erkannt")
        fstype = str(mount_probe.get("fstype") or "nicht erkannt")
        print(
            "  ✗ RAM-Disk-Vertrag verletzt: "
            f"Grund={reason}, Ziel={target}, Dateisystem={fstype}."
        )
        log_error(
            "ramdisk",
            "RAM-Disk ist nicht exakt als tmpfs gemountet "
            f"(Grund={reason}, Ziel={target}, Dateisystem={fstype}).",
        )
        if not _rollback_ramdisk_precommit(
            legacy_transaction=legacy_transaction,
            mounted_by_setup=mounted_by_setup,
            fstab_transaction=fstab_transaction,
        ):
            print("  ✗ Mount-/fstab-/Legacy-Rückfall ist nicht vollständig nachgewiesen.")
        return False

    # Besitz- und Setgid-Vertrag müssen vor dem ersten Dienststart wirksam
    # sein. Ein bloß erfolgreiches tmpfs-Mount reicht nicht: Ohne diese
    # Rechte können die getrennten Service-User keine gemeinsamen
    # Laufzeitdateien anlegen beziehungsweise weiterreichen.
    try:
        _set_runtime_ramdisk_contract(install_user)
    except Exception as exc:
        print(f"  ✗ RAM-Disk-Laufzeitrechte konnten nicht sicher gesetzt werden: {exc}")
        log_error("ramdisk", f"RAM-Disk-Laufzeitrechte fehlgeschlagen: {exc}", exc)
        if not _rollback_ramdisk_precommit(
            legacy_transaction=legacy_transaction,
            mounted_by_setup=mounted_by_setup,
            fstab_transaction=fstab_transaction,
        ):
            print("  ✗ Mount-/fstab-/Legacy-Rückfall ist nicht vollständig nachgewiesen.")
        return False
    ramdisk_logger.info("RAM-Disk gemountet und Berechtigungen gesetzt.")

    try:
        dropins = ensure_ramdisk_service_dropins()
    except Exception as exc:
        print(f"  ✗ tmpfs-Startsperren brachen außerhalb des Bundles ab: {exc}")
        log_error(
            "ramdisk",
            f"systemd-tmpfs-Bundle brach unerwartet ab: {exc}",
            exc,
        )
        print(
            "  ✗ Sicherer Mount/fstab-Zustand bleibt fail-closed bestehen, "
            "weil kein Drop-in-Rollbacknachweis vorliegt."
        )
        return False
    if not dropins.get("success"):
        rollback = dropins.get("rollback") or {}
        error = str(dropins.get("error") or "unbekannter Drop-in-Fehler")
        print(f"  ✗ tmpfs-Startsperren konnten nicht sicher installiert werden: {error}")
        log_error("ramdisk", f"systemd-tmpfs-Bundle fehlgeschlagen: {error}")
        if rollback.get("ok"):
            if not _rollback_ramdisk_precommit(
                legacy_transaction=legacy_transaction,
                mounted_by_setup=mounted_by_setup,
                fstab_transaction=fstab_transaction,
            ):
                print(
                    "  ✗ Drop-ins wurden restauriert, aber Mount-/fstab-/Legacy-"
                    "Rückfall ist nicht vollständig nachgewiesen."
                )
        else:
            print(
                "  ✗ Drop-in-Rollback ist unvollständig; Mount und fstab bleiben "
                "deshalb als sichere Startsperren-Voraussetzung bestehen."
            )
        return False
    changed_count = len(dropins.get("changed") or ())
    if changed_count:
        print(
            "  ✓ tmpfs-Startsperre für "
            f"{changed_count} E3DC-Dienste als Bundle installiert."
        )
    else:
        print("  ✓ tmpfs-Startsperren der E3DC-Dienste sind aktuell.")

    # COMMIT-GRENZE DER RAM-DISK-KERNTRANSAKTION:
    # Ab hier sind fstab, der exakte tmpfs-Mount, der Inode-/Rechtevertrag und
    # die Startsperren gemeinsam wirksam. Die folgende Legacy-Cron-Bereinigung
    # ist eine eigene Teiltransaktion. Bei deren Fehler darf der sichere Mount
    # nicht wieder entfernt werden: Teilweise installierte Startsperren würden
    # sonst Produktdienste blockieren, während ein alter Cronjob auf das
    # persistente Unterverzeichnis statt auf tmpfs schreiben könnte.
    legacy_transaction["committed"] = True
    ramdisk_logger.info("RAM-Disk-Kerntransaktion sicher abgeschlossen.")

    # 4. Cron bleibt bis zur persistenten Notifier-/Watchdog-Transaktion
    # bytegenau unverändert. Der Notifier übernimmt sowohl History-Sampling als
    # auch die gebundene Entfernung alter Cronjobs. Das historische Skript wird
    # hier nur inert gemacht, damit ein noch vorhandener Alt-Cron keinen Writer
    # gegen den neuen tmpfs-Vertrag starten kann.
    print("→ Crontab bleibt bis zur Notifier-Transaktion unverändert.")

    # 5. Alten Live-Grabber inert machen
    script_removed, error = _remove_legacy_grabber_script()
    if not script_removed:
        print(f"  ✗ Alter Live-Grabber konnte nicht entfernt werden: {error.strip()}")
        log_error("ramdisk", f"Alter Live-Grabber konnte nicht entfernt werden: {error}")
        return False

    print("\n✓ RAM-Disk erfolgreich eingerichtet.\n")
    log_task_completed("RAM-Disk Setup")
    return True

# Kein register_command: setup_ramdisk() wird automatisch von
# permissions.py aufgerufen wenn der tmpfs-Mount fehlt (ramdisk_not_mounted).
# Kein manueller Menüpunkt mehr nötig — verhindert ausserdem den Key-Konflikt
# mit rollback.py das ebenfalls Key '14' nutzt.
