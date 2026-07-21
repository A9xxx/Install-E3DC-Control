#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manifestgebundene Aufbewahrung für eigene E3DC-Sicherungssammlungen."""

from __future__ import annotations

import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

try:
    from .backup_integrity import (
        BackupIntegrityError,
        ROOT_MARKER_NAME,
        SYSTEM_BACKUP_KIND,
        WEB_SNAPSHOT_KIND,
        _assert_no_symlink_components,
        _lexical_absolute,
        validate_existing_backup_root,
        verify_backup,
    )
except ImportError:  # pragma: no cover - Rückfall für direkte Skriptausführung
    from backup_integrity import (
        BackupIntegrityError,
        ROOT_MARKER_NAME,
        SYSTEM_BACKUP_KIND,
        WEB_SNAPSHOT_KIND,
        _assert_no_symlink_components,
        _lexical_absolute,
        validate_existing_backup_root,
        verify_backup,
    )


PathValue = Union[str, os.PathLike]


def _int_from_env(name: str, default: int, minimum: int = 1, maximum: int = 200) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


UPDATE_BACKUP_KEEP_COUNT = _int_from_env("E3DC_BACKUP_KEEP_COUNT", 12)
WEB_INSTALLER_BACKUP_KEEP_COUNT = _int_from_env("E3DC_WEB_BACKUP_KEEP_COUNT", 20)
UPDATE_BACKUP_MIN_KEEP_COUNT = _int_from_env("E3DC_BACKUP_MIN_KEEP_COUNT", 3)
WEB_INSTALLER_BACKUP_MIN_KEEP_COUNT = _int_from_env("E3DC_WEB_BACKUP_MIN_KEEP_COUNT", 3)
UPDATE_BACKUP_MAX_AGE_DAYS = _int_from_env("E3DC_BACKUP_MAX_AGE_DAYS", 90, minimum=0, maximum=3650)
WEB_INSTALLER_BACKUP_MAX_AGE_DAYS = _int_from_env("E3DC_WEB_BACKUP_MAX_AGE_DAYS", 90, minimum=0, maximum=3650)


def _log(logger: Any, level: str, message: str) -> None:
    if logger is None:
        return
    try:
        getattr(logger, level)(message)
    except Exception:
        pass


def _remove_directory_entry(
    parent_descriptor: int,
    name: str,
    expected_dev: Optional[int] = None,
    expected_ino: Optional[int] = None,
) -> None:
    """Entfernt einen bereits isolierten Verzeichnisbaum, ohne Symlinks zu folgen."""

    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if expected_dev is not None and (opened.st_dev, opened.st_ino) != (expected_dev, expected_ino):
            raise BackupIntegrityError("Quarantäne-Eintrag wurde vor dem Öffnen ausgetauscht.")
        for child in sorted(os.listdir(descriptor)):
            metadata = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupIntegrityError("Symlink im zu entfernenden Backup: {}".format(child))
            if stat.S_ISDIR(metadata.st_mode):
                _remove_directory_entry(descriptor, child, metadata.st_dev, metadata.st_ino)
            elif stat.S_ISREG(metadata.st_mode):
                os.unlink(child, dir_fd=descriptor)
            else:
                raise BackupIntegrityError("Unerlaubter Dateityp im Backup: {}".format(child))
    finally:
        os.close(descriptor)
    current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if expected_dev is not None and (current.st_dev, current.st_ino) != (expected_dev, expected_ino):
        raise BackupIntegrityError("Quarantäne-Eintrag wurde vor dem Entfernen ausgetauscht.")
    os.rmdir(name, dir_fd=parent_descriptor)


def prune_backup_dir(
    backup_root: PathValue,
    keep_count: int,
    preserve_names: Optional[Iterable[str]] = None,
    logger: Any = None,
    dry_run: bool = False,
    max_age_days: Optional[int] = None,
    min_keep_count: int = 1,
    now: Optional[float] = None,
    expected_kind: str = SYSTEM_BACKUP_KIND,
    preserve_paths: Optional[Iterable[PathValue]] = None,
) -> Dict[str, Any]:
    """Rotiert nur verifizierte direkte Kind-Manifeste des ``expected_kind``.

    Unbekannte Verzeichnisse, Symlinks, unvollständige Sicherungen und fremde
    Artefakte sind niemals Kandidaten. Zum Löschen wird das ausgewählte
    Verzeichnis zunächst innerhalb seiner vertrauenswürdigen Sammlung atomar
    umbenannt und danach Deskriptor für Deskriptor entfernt.
    """

    root = _lexical_absolute(backup_root)
    keep_count = max(1, int(keep_count or 1))
    min_keep_count = max(1, min(int(min_keep_count or 1), keep_count))
    max_age_days = None if max_age_days is None else max(0, int(max_age_days))
    now_ts = time.time() if now is None else float(now)
    preserved_names = {str(name) for name in (preserve_names or ())}
    preserved_paths = {_lexical_absolute(path) for path in (preserve_paths or ())}
    result: Dict[str, Any] = {
        "success": True,
        "root": str(root),
        "expected_kind": expected_kind,
        "keep_count": keep_count,
        "min_keep_count": min_keep_count,
        "max_age_days": max_age_days,
        "removed": [],
        "kept": [],
        "skipped": [],
        "dry_run": bool(dry_run),
    }
    if not root.exists():
        result["missing"] = True
        return result
    try:
        _assert_no_symlink_components(root)
        root_descriptor = os.open(
            str(root),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except Exception as exc:
        result["success"] = False
        result["error"] = str(exc)
        return result

    candidates: List[Tuple[float, str, Path, int, int]] = []
    try:
        for name in sorted(os.listdir(root_descriptor)):
            candidate = root / name
            if name == ROOT_MARKER_NAME or name in preserved_names or candidate in preserved_paths:
                result["skipped"].append({"path": str(candidate), "reason": "geschützt"})
                continue
            try:
                metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    result["skipped"].append({"path": str(candidate), "reason": "Symlink"})
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    result["skipped"].append({"path": str(candidate), "reason": "kein Backup-Verzeichnis"})
                    continue
                verify_backup(candidate, expected_kind=expected_kind)
                verified_metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino) != (verified_metadata.st_dev, verified_metadata.st_ino):
                    raise BackupIntegrityError("Backup wurde während der Verifikation ausgetauscht.")
                candidates.append((metadata.st_mtime, name, candidate, metadata.st_dev, metadata.st_ino))
            except Exception as exc:
                result["skipped"].append({"path": str(candidate), "reason": "nicht verifiziert: {}".format(exc)})

        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        count_keep = {item[2] for item in candidates[:keep_count]}
        min_keep = {item[2] for item in candidates[:min_keep_count]}
        cutoff = None
        if max_age_days:
            cutoff = now_ts - max_age_days * 86400

        for mtime, name, path, verified_dev, verified_ino in candidates:
            too_many = path not in count_keep
            too_old = cutoff is not None and mtime < cutoff and path not in min_keep
            if not too_many and not too_old:
                result["kept"].append(str(path))
                continue
            reason = "max_count" if too_many else "max_age"
            if dry_run:
                result["removed"].append({"path": str(path), "reason": reason, "dry_run": True})
                continue
            quarantine = ".e3dc-prune-{}".format(uuid.uuid4().hex)
            try:
                os.rename(name, quarantine, src_dir_fd=root_descriptor, dst_dir_fd=root_descriptor)
                quarantined = os.stat(quarantine, dir_fd=root_descriptor, follow_symlinks=False)
                if (quarantined.st_dev, quarantined.st_ino) != (verified_dev, verified_ino):
                    raise BackupIntegrityError("Backupname wurde vor dem Quarantäne-Rename ausgetauscht.")
                verify_backup(root / quarantine, expected_kind=expected_kind)
                _remove_directory_entry(root_descriptor, quarantine, verified_dev, verified_ino)
                result["removed"].append({"path": str(path), "reason": reason})
                _log(logger, "info", "Backup-Retention entfernt verifiziertes Backup: {}".format(path))
            except Exception as exc:
                result["success"] = False
                try:
                    os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    try:
                        os.rename(quarantine, name, src_dir_fd=root_descriptor, dst_dir_fd=root_descriptor)
                    except Exception:
                        pass
                except Exception:
                    pass
                result["skipped"].append({"path": str(path), "reason": "Löschen fehlgeschlagen: {}".format(exc)})
    finally:
        os.close(root_descriptor)
    return result


def delete_verified_backup(
    backup_path: PathValue,
    collection_root: PathValue,
    expected_kind: str = SYSTEM_BACKUP_KIND,
) -> None:
    """Löscht ein verifiziertes direktes Kind über eine atomare Quarantäne."""

    root = _assert_no_symlink_components(collection_root)
    target = _assert_no_symlink_components(backup_path)
    if target.parent != root or target.name.startswith(".e3dc-prune-"):
        raise BackupIntegrityError("Backup liegt nicht direkt im freigegebenen Collection-Root.")
    root_descriptor = os.open(
        str(root),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    quarantine = ".e3dc-prune-{}".format(uuid.uuid4().hex)
    try:
        before = os.stat(target.name, dir_fd=root_descriptor, follow_symlinks=False)
        verify_backup(target, expected_kind=expected_kind)
        after = os.stat(target.name, dir_fd=root_descriptor, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise BackupIntegrityError("Backup wurde während der Verifikation ausgetauscht.")
        os.rename(target.name, quarantine, src_dir_fd=root_descriptor, dst_dir_fd=root_descriptor)
        try:
            quarantined = os.stat(quarantine, dir_fd=root_descriptor, follow_symlinks=False)
            if (quarantined.st_dev, quarantined.st_ino) != (before.st_dev, before.st_ino):
                raise BackupIntegrityError("Backupname wurde vor dem Quarantäne-Rename ausgetauscht.")
            verify_backup(root / quarantine, expected_kind=expected_kind)
            _remove_directory_entry(root_descriptor, quarantine, before.st_dev, before.st_ino)
        except Exception:
            try:
                os.stat(target.name, dir_fd=root_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(quarantine, target.name, src_dir_fd=root_descriptor, dst_dir_fd=root_descriptor)
            raise
    finally:
        os.close(root_descriptor)


def prune_heavy_backup_payloads(
    backup_root: PathValue,
    logger: Any = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Veraltete wirkungslose Funktion: Manifestinhalte bleiben nach Abschluss unveränderlich."""

    return {
        "success": True,
        "root": str(_lexical_absolute(backup_root)),
        "removed": [],
        "skipped": [{"reason": "Manifest-Backups werden niemals nachträglich verändert."}],
        "dry_run": bool(dry_run),
    }


def prune_install_backups(
    install_path: PathValue,
    logger: Any = None,
    backup_root: Optional[PathValue] = None,
    preserve_paths: Optional[Iterable[PathValue]] = None,
) -> Dict[str, Any]:
    """Wendet die Aufbewahrung nur im validierten eigenen Sicherungsnamensraum an."""

    install = _lexical_absolute(install_path)
    if backup_root is None:
        configured = os.environ.get("E3DC_BACKUP_ROOT", "").strip()
        root = _lexical_absolute(configured) if configured else install.parent / "e3dc-control-backups"
    else:
        root = _lexical_absolute(backup_root)
    root = validate_existing_backup_root(root, install)
    update_result = prune_backup_dir(
        root,
        keep_count=UPDATE_BACKUP_KEEP_COUNT,
        min_keep_count=UPDATE_BACKUP_MIN_KEEP_COUNT,
        max_age_days=UPDATE_BACKUP_MAX_AGE_DAYS,
        expected_kind=SYSTEM_BACKUP_KIND,
        preserve_names={"web_installer"},
        preserve_paths=preserve_paths,
        logger=logger,
    )
    web_result = prune_backup_dir(
        root / "web_installer",
        keep_count=WEB_INSTALLER_BACKUP_KEEP_COUNT,
        min_keep_count=WEB_INSTALLER_BACKUP_MIN_KEEP_COUNT,
        max_age_days=WEB_INSTALLER_BACKUP_MAX_AGE_DAYS,
        expected_kind=WEB_SNAPSHOT_KIND,
        logger=logger,
    )
    return {
        "success": bool(update_result.get("success") and web_result.get("success")),
        "payload_cleanup": prune_heavy_backup_payloads(root, logger=logger),
        "update_backups": update_result,
        "web_installer_backups": web_result,
    }
