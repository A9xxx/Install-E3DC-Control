#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import os
import sys
import json
import datetime
import pickle
import grp
import pwd
import secrets
import stat
import hashlib
import re
import statistics
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - Produktion läuft unter Linux; zur Laufzeit fail-closed.
    fcntl = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Standard-Ausgabe auf UTF-8 erzwingen (verhindert UnicodeEncodeError)
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

try:
    from sklearn.ensemble import RandomForestRegressor
    import numpy as np
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

DB_PATH = "/var/www/html/data/e3dc_stats.db"
MODEL_DIR = os.environ.get("E3DC_ML_MODEL_DIR", "/var/lib/e3dc-control/ml")
MODEL_PATH = os.path.join(MODEL_DIR, "ml_model.pkl")
LEGACY_MODEL_PATH = "/var/www/html/data/ml_model.pkl"
PREDICTION_PATH = "/var/www/html/ramdisk/ml_prediction.json"
V4_CONFIG_FILE = "/var/www/html/data/e3dc_v4.json"
ML_CONSUMPTION_EVAL_PATH = "/var/www/html/logs/ml_consumption_eval.json"
ML_DAILY_FORECAST_MIN_COVERAGE_SLOTS = 80
ML_DAILY_FORECAST_LEGACY_FULL_DAY_CUTOFF_HOUR = 4
LIVE_HISTORY_PATH = "/var/www/html/ramdisk/live_history.txt"
CLIMATE_HISTORY_DIR = "/var/www/html/data/climate_history"
ML_MODEL_SCHEMA_VERSION = 1
ML_MODEL_FORMAT = "e3dc-sklearn-pickle"
ML_MODEL_MAX_BYTES = 128 * 1024 * 1024
_MODEL_FILE_RE = re.compile(r"^ml_model-([0-9a-f]{64})\.pkl$")
ML_PREDICTION_SCHEMA_VERSION = 2
ML_EMPIRICAL_MIN_SAMPLES = 48
ML_EMPIRICAL_RECENT_DAYS = 56


def _set_shared_web_file(path, mode=0o664):
    """Keep generated ML artifacts readable/writable for services and WebUI."""
    try:
        os.chmod(path, mode)
    except Exception:
        pass
    try:
        os.chown(path, -1, grp.getgrnam("www-data").gr_gid)
    except Exception:
        pass


def _trusted_ml_owner_uids():
    try:
        _uid, _gid, trusted_uids, _trusted_gids = _trusted_ml_identity()
        return trusted_uids
    except Exception:
        return set()


def _trusted_ml_identity():
    """Löst unabhängig von Laufzeitpfad-APIs nur den Owner des privaten Speichers auf."""

    model_dir = os.path.dirname(os.path.abspath(MODEL_PATH))
    try:
        directory = os.lstat(model_dir)
    except FileNotFoundError:
        directory = None
    if directory is not None:
        if (
            stat.S_ISLNK(directory.st_mode)
            or not stat.S_ISDIR(directory.st_mode)
            or stat.S_IMODE(directory.st_mode) != 0o700
        ):
            raise PermissionError("Privater ML-Store besitzt keinen sicheren Verzeichnisvertrag")
        uid, gid = int(directory.st_uid), int(directory.st_gid)
    else:
        uid, gid = os.geteuid(), os.getegid()

    try:
        account = pwd.getpwuid(uid)
    except KeyError as exc:
        raise PermissionError("ML-Store-Owner ist kein lokales Systemkonto") from exc
    if account.pw_name == "www-data":
        raise PermissionError("ML-Modell darf nicht dem Web-Benutzer gehoeren")
    return uid, gid, {0, uid}, {0, gid}


def _ml_parent_security_error(path, trusted_uids):
    candidate = os.path.abspath(str(path or ""))
    if not os.path.isabs(str(path or "")) or os.path.realpath(candidate) != candidate:
        return "Modellpfad besitzt keine sichere absolute Elternkette"

    current = os.path.dirname(candidate) or os.sep
    while True:
        try:
            info = os.lstat(current)
        except OSError:
            return "Modellverzeichnis konnte nicht sicher geprueft werden"
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return "Eine Elternkomponente des Modellpfads ist nicht sicher"
        if info.st_uid not in trusted_uids:
            return "Eine Elternkomponente gehört keinem bestätigten Besitzer"
        writable = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable:
            # Ein root-eigenes Sticky-Verzeichnis ist nur die Systemgrenze;
            # darunter müssen ausnahmslos sichere Komponenten liegen.
            if info.st_uid == 0 and (info.st_mode & stat.S_ISVTX):
                break
            return "Eine Elternkomponente ist gruppen- oder weltbeschreibbar"
        parent = os.path.dirname(current.rstrip(os.sep)) or os.sep
        if parent == current:
            break
        current = parent
    return None


def _ml_file_stat_security_error(file_stat, owner_uid):
    if not stat.S_ISREG(file_stat.st_mode):
        return "Modellpfad ist keine regulaere Datei"
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        return "ML-Modell besitzt nicht den vorgeschriebenen Modus 0600"
    if file_stat.st_uid != owner_uid:
        return "ML-Modell gehört nicht dem bestätigten Installationsbenutzer"
    if file_stat.st_nlink != 1:
        return "ML-Modell besitzt eine unzulaessige Hardlink-Anzahl"
    return None


def _ml_manifest_name(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return f"{stem}.manifest.json"


def _ml_lock_name(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return f".{stem}.lock"


def _ml_model_directory_security_error(path, owner_uid, trusted_uids):
    parent_error = _ml_parent_security_error(path, trusted_uids)
    if parent_error:
        return parent_error
    try:
        directory_stat = os.lstat(os.path.dirname(os.path.abspath(path)))
    except OSError:
        return "Privates ML-Modellverzeichnis fehlt"
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        return "Privates ML-Modellverzeichnis ist kein echtes Verzeichnis"
    if directory_stat.st_uid != owner_uid:
        return "Privates ML-Modellverzeichnis besitzt einen falschen Owner"
    if stat.S_IMODE(directory_stat.st_mode) != 0o700:
        return "Privates ML-Modellverzeichnis besitzt nicht den Modus 0700"
    return None


def _ensure_private_ml_model_directory(path):
    """Erstellt nur das endgültige private Verzeichnis unter einem bereits vertrauenswürdigen Elternpfad."""
    owner_uid, owner_gid, trusted_uids, _trusted_gids = _trusted_ml_identity()
    model_dir = os.path.dirname(os.path.abspath(path))
    parent = os.path.dirname(model_dir.rstrip(os.sep)) or os.sep
    leaf = os.path.basename(model_dir)
    if not leaf or os.path.realpath(parent) != parent:
        raise PermissionError("ML-Modellverzeichnis besitzt keinen sicheren Elternpfad")

    parent_error = _ml_parent_security_error(os.path.join(parent, ".ml-parent-check"), trusted_uids)
    if parent_error:
        raise PermissionError(parent_error)

    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(parent, parent_flags)
    try:
        created = False
        try:
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        model_fd = os.open(leaf, dir_flags, dir_fd=parent_fd)
        try:
            if created:
                os.fchown(model_fd, owner_uid, owner_gid)
                os.fchmod(model_fd, 0o700)
                os.fsync(model_fd)
            info = os.fstat(model_fd)
            if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o700:
                raise PermissionError("Privates ML-Modellverzeichnis besitzt falschen Owner oder Modus")
            if not stat.S_ISDIR(info.st_mode):
                raise PermissionError("Privates ML-Modellverzeichnis ist kein Verzeichnis")
        finally:
            os.close(model_fd)
    finally:
        os.close(parent_fd)


def _open_private_ml_model_directory(path, create=False):
    if create and not os.path.lexists(os.path.dirname(os.path.abspath(path))):
        _ensure_private_ml_model_directory(path)
    owner_uid, owner_gid, trusted_uids, _trusted_gids = _trusted_ml_identity()
    security_error = _ml_model_directory_security_error(path, owner_uid, trusted_uids)
    if security_error:
        raise PermissionError(security_error)
    model_dir = os.path.dirname(os.path.abspath(path))
    before = os.lstat(model_dir)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    dir_fd = os.open(model_dir, flags)
    after = os.fstat(dir_fd)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(dir_fd)
        raise PermissionError("ML-Modellverzeichnis wurde während der Prüfung ausgetauscht")
    return dir_fd, owner_uid, owner_gid


def _read_secure_regular_file(dir_fd, name, owner_uid, max_bytes):
    if not name or name in {".", ".."} or os.path.basename(name) != name:
        raise PermissionError("Ungültiger ML-Dateiname")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        before = os.fstat(file_fd)
        security_error = _ml_file_stat_security_error(before, owner_uid)
        if security_error:
            raise PermissionError(security_error)
        if before.st_size < 1 or before.st_size > max_bytes:
            raise PermissionError("ML-Datei besitzt eine unzulässige Größe")
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(file_fd)
        signature_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        signature_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if remaining or signature_before != signature_after:
            raise PermissionError("ML-Datei wurde während der Prüfung verändert")
        return b"".join(chunks)
    finally:
        os.close(file_fd)


def _write_secure_atomic_file(dir_fd, name, payload, owner_uid, owner_gid):
    existing = None
    try:
        existing = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    if existing is not None:
        security_error = _ml_file_stat_security_error(existing, owner_uid)
        if security_error:
            raise PermissionError(security_error)

    tmp_name = f".{name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    tmp_fd = None
    try:
        tmp_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=dir_fd,
        )
        # Die Vertraulichkeit wird durch Owner-UID und Modus 0600 erzwungen;
        # die Dateigruppe besitzt deshalb keinerlei Leserecht. Dienste laufen
        # teilweise absichtlich als ``User=pi, Group=www-data``. Ein fchown auf
        # die primäre Installationsgruppe (z. B. pi:pi) ist dann für denselben
        # UID nicht erlaubt und hatte das sichere wochenweise Neutraining mit
        # EPERM blockiert. Nur eine wirklich abweichende Owner-UID wird
        # korrigiert; die beim O_CREAT entstandene effektive Gruppe bleibt bei
        # privaten 0600-Dateien sicher und ist kein Reader-Vertragsmerkmal.
        created = os.fstat(tmp_fd)
        if created.st_uid != owner_uid:
            os.fchown(tmp_fd, owner_uid, -1)
        os.fchmod(tmp_fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(tmp_fd, view)
            if written <= 0:
                raise OSError("ML-Datei konnte nicht vollständig geschrieben werden")
            view = view[written:]
        os.fsync(tmp_fd)
        security_error = _ml_file_stat_security_error(os.fstat(tmp_fd), owner_uid)
        if security_error:
            raise PermissionError(security_error)
        os.close(tmp_fd)
        tmp_fd = None
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


@contextmanager
def _ml_model_lock(path, exclusive=False, create_directory=False):
    if fcntl is None:
        raise PermissionError("ML-Modellsperre ist auf diesem System nicht verfügbar")
    dir_fd, owner_uid, owner_gid = _open_private_ml_model_directory(path, create=create_directory)
    lock_fd = None
    try:
        lock_name = _ml_lock_name(path)
        created = False
        try:
            lock_fd = os.open(
                lock_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=dir_fd,
            )
            created = True
        except FileExistsError:
            lock_fd = os.open(
                lock_name,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=dir_fd,
            )
        if created:
            created_info = os.fstat(lock_fd)
            if os.geteuid() == 0:
                os.fchown(lock_fd, owner_uid, owner_gid)
            elif created_info.st_uid != owner_uid:
                # Dienste können absichtlich mit Group=www-data laufen. Die
                # private 0600-Datei benötigt nur den gebundenen Owner; ein
                # Gruppenwechsel wäre ohne Root nicht auf jedem System erlaubt.
                os.fchown(lock_fd, owner_uid, -1)
            os.fchmod(lock_fd, 0o600)
            os.fsync(lock_fd)
        lock_info = os.fstat(lock_fd)
        lock_error = _ml_file_stat_security_error(lock_info, owner_uid)
        if lock_error:
            raise PermissionError(lock_error)
        if created and os.geteuid() == 0 and lock_info.st_gid != owner_gid:
            raise PermissionError("ML-Modellsperre besitzt nicht die gebundene Store-Gruppe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        path_info = os.stat(lock_name, dir_fd=dir_fd, follow_symlinks=False)
        if (path_info.st_dev, path_info.st_ino) != (lock_info.st_dev, lock_info.st_ino):
            raise PermissionError("ML-Modellsperre wurde vor der Nutzung ausgetauscht")
        yield dir_fd, owner_uid, owner_gid
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(dir_fd)


def _read_verified_ml_payload(path):
    with _ml_model_lock(path, exclusive=False) as (dir_fd, owner_uid, _owner_gid):
        manifest_bytes = _read_secure_regular_file(
            dir_fd, _ml_manifest_name(path), owner_uid, 64 * 1024
        )
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError("ML-Manifest ist unlesbar") from exc
        if not isinstance(manifest, dict):
            raise ValueError("ML-Manifest besitzt kein Objektformat")
        if manifest.get("schema_version") != ML_MODEL_SCHEMA_VERSION:
            raise ValueError("ML-Manifest besitzt eine unbekannte Version")
        if manifest.get("format") != ML_MODEL_FORMAT:
            raise ValueError("ML-Manifest besitzt ein unbekanntes Modellformat")
        model_file = str(manifest.get("model_file") or "")
        expected_hash = str(manifest.get("model_sha256") or "")
        match = _MODEL_FILE_RE.fullmatch(model_file)
        if match is None or match.group(1) != expected_hash:
            raise ValueError("ML-Manifest verweist nicht auf ein hashgebundenes Modell")
        payload = _read_secure_regular_file(dir_fd, model_file, owner_uid, ML_MODEL_MAX_BYTES)
        actual_hash = hashlib.sha256(payload).hexdigest()
        if not secrets.compare_digest(actual_hash, expected_hash):
            raise ValueError("ML-Modellhash stimmt nicht mit dem Manifest ueberein")
        return payload, manifest


def _ml_model_security_error(path):
    try:
        _read_verified_ml_payload(path)
        return None
    except Exception as exc:
        return str(exc) or "ML-Modell konnte nicht sicher geprueft werden"


def _write_ml_model_safely(path, models):
    payload = pickle.dumps(models, protocol=pickle.HIGHEST_PROTOCOL)
    payload_hash = hashlib.sha256(payload).hexdigest()
    stem, extension = os.path.splitext(os.path.basename(path))
    artifact_name = f"{stem}-{payload_hash}{extension}"
    manifest = {
        "schema_version": ML_MODEL_SCHEMA_VERSION,
        "format": ML_MODEL_FORMAT,
        "model_file": artifact_name,
        "model_sha256": payload_hash,
        "trained_at": str(models.get("trained_at") or "") if isinstance(models, dict) else "",
    }
    manifest_payload = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")

    with _ml_model_lock(path, exclusive=True, create_directory=True) as (dir_fd, owner_uid, owner_gid):
        try:
            existing_payload = _read_secure_regular_file(
                dir_fd, artifact_name, owner_uid, ML_MODEL_MAX_BYTES
            )
        except FileNotFoundError:
            _write_secure_atomic_file(dir_fd, artifact_name, payload, owner_uid, owner_gid)
        else:
            if not secrets.compare_digest(hashlib.sha256(existing_payload).hexdigest(), payload_hash):
                raise PermissionError("Vorhandenes ML-Artefakt stimmt nicht mit seinem Dateihash ueberein")
        _write_secure_atomic_file(
            dir_fd, _ml_manifest_name(path), manifest_payload, owner_uid, owner_gid
        )

    security_error = _ml_model_security_error(path)
    if security_error:
        raise PermissionError(security_error)


def _load_ml_model_safely(path):
    payload, _manifest = _read_verified_ml_payload(path)
    models = pickle.loads(payload)
    if not isinstance(models, dict) or "home" not in models or "wp" not in models:
        raise ValueError("ML-Modell hat kein erwartetes Format")
    return models


def ml_model_is_ready(path=MODEL_PATH):
    try:
        _read_verified_ml_payload(path)
        return True
    except Exception:
        return False


def _legacy_ml_model_notice():
    if os.path.lexists(LEGACY_MODEL_PATH):
        return (
            f"Legacy-Modell wird nicht geladen oder uebernommen: {LEGACY_MODEL_PATH}. "
            "Neutraining aus lokalen SQLite-/JSON-/Text-Trainingsdaten erforderlich."
        )
    return None


def _activate_conservative_ml_fallback(reason):
    try:
        os.unlink(PREDICTION_PATH)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"WARNUNG: Alte ML-Prognose konnte nicht entfernt werden: {exc}")
    print(f"ML-Prognose nicht verfügbar; konservativer System-Fallback bleibt aktiv ({reason}).")


def _ensure_ml_training_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ml_training_data (
            id TEXT PRIMARY KEY,
            date TEXT,
            time_gmt REAL,
            pv_prog_pct REAL,
            pv_real_pct REAL,
            home_kwh_cum REAL,
            wp_kwh_cum REAL,
            temp_c REAL,
            grid_kwh_cum REAL
        )
    ''')


def _as_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _truthy(value, default=False):
    if value is None or value == "":
        return bool(default)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "ja", "ein", "aktiv")


def _load_v4_config_dict():
    try:
        if os.path.exists(V4_CONFIG_FILE):
            with open(V4_CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _parse_live_ts(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(float(value))
        except Exception:
            return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _seed_ml_training_from_live_history(min_rows=50, refresh=False):
    """Erzeugt oder aktualisiert ML-Trainingspunkte aus live_history.txt.

    Docker-Installationen haben oft keine alten C++-Ertrag-Dateien. Die
    persistenten Tageswerte reichen fuer Statistiken, aber nicht fuer das
    15-Minuten-ML-Training. Die Live-Historie enthaelt bereits bereinigte
    Haus-/WP-Werte und ist daher ein guter, konservativer Startpunkt.
    """
    if not os.path.exists(LIVE_HISTORY_PATH):
        return 0

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    _ensure_ml_training_table(conn)
    try:
        existing_rows = c.execute("SELECT COUNT(*) FROM ml_training_data").fetchone()[0]
    except Exception:
        existing_rows = 0
    if existing_rows >= min_rows and not refresh:
        conn.close()
        return 0

    last_by_day = {}
    totals_by_day = {}
    slot_rows = {}

    try:
        with open(LIVE_HISTORY_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue

                dt = _parse_live_ts(entry.get("ts"))
                if dt is None:
                    continue
                day = dt.date().isoformat()
                totals = totals_by_day.setdefault(day, {"home": 0.0, "wp": 0.0, "grid": 0.0})
                prev = last_by_day.get(day)

                if prev is not None:
                    delta_h = (dt - prev["dt"]).total_seconds() / 3600.0
                    # Live-Historie ist normalerweise minutenweise. Groessere
                    # Luecken nicht hochrechnen, sonst entstehen Kunstlasten.
                    if 0.0 < delta_h <= 0.25:
                        totals["home"] += max(0.0, prev["home_w"]) * delta_h / 1000.0
                        totals["wp"] += max(0.0, prev["wp_w"]) * delta_h / 1000.0
                        totals["grid"] += max(0.0, prev["grid_w"]) * delta_h / 1000.0

                slot_minute = (dt.hour * 60 + dt.minute) // 15 * 15
                time_gmt = (slot_minute / 60.0)
                record_id = f"{day}_{time_gmt:.2f}_live"
                temp_c = _as_float(
                    entry.get("Aussentemp", entry.get("Aussentemperatur", entry.get("forecast_temp_c"))),
                    8.5
                )
                slot_rows[record_id] = (
                    record_id,
                    day,
                    time_gmt,
                    0.0,
                    0.0,
                    round(totals["home"], 5),
                    round(totals["wp"], 5),
                    temp_c,
                    round(totals["grid"], 5),
                )

                last_by_day[day] = {
                    "dt": dt,
                    "home_w": _as_float(entry.get("home", entry.get("home_raw")), 0.0),
                    "wp_w": _as_float(entry.get("wp"), 0.0),
                    "grid_w": _as_float(entry.get("grid"), 0.0),
                }
    except Exception as e:
        conn.close()
        print(f"Live-History-Fallback fuer ML konnte nicht gelesen werden: {e}")
        return 0

    if not slot_rows:
        conn.close()
        return 0

    c.executemany('''
        INSERT OR REPLACE INTO ml_training_data
        (id, date, time_gmt, pv_prog_pct, pv_real_pct, home_kwh_cum, wp_kwh_cum, temp_c, grid_kwh_cum)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', list(slot_rows.values()))
    conn.commit()
    try:
        _set_shared_web_file(DB_PATH)
    except Exception:
        pass
    total_rows = c.execute("SELECT COUNT(*) FROM ml_training_data").fetchone()[0]
    conn.close()

    added = len(slot_rows)
    print(
        f"ML-Live-History-Fallback: {added} 15-Minuten-Punkte aus {LIVE_HISTORY_PATH} importiert "
        f"(gesamt {total_rows})."
    )
    return added


def _load_ml_home_cap() -> float:
    """Liest ml_home_cap_kw aus e3dc_v4.json. Default 6.0 kW.
    Verhindert dass Sensor-Artefakte oder alte WB-Mischungen das ML-Training verzerren.
    6.0 kW deckt Induktionskochfeld + Backofen gleichzeitig ab.
    """
    try:
        cfg = _load_v4_config_dict()
        if cfg:
            return float(cfg.get('ml_home_cap_kw', 6.0))
    except Exception:
        pass
    return 6.0


def _load_ml_home_daily_cap() -> float:
    """Obergrenze für den plausiblen reinen Hausverbrauch pro Tag.

    Wallbox/WP/Heizstab werden separat geregelt. Wenn alte Trainings- oder
    Tageswerte diese Verbraucher noch im Hausverbrauch enthalten, darf daraus
    keine künstlich hohe Grundlast gelernt werden.
    """
    try:
        cfg = _load_v4_config_dict()
        if cfg:
            return float(cfg.get('ml_home_daily_cap_kwh', 24.0))
    except Exception:
        pass
    return 24.0


def _load_ml_home_reference_caps():
    """Grenzen für Tage, die als normale Hausverbrauchs-Referenz taugen."""
    caps = {
        "wallbox_kwh": 3.0,
        "climate_kwh": 3.0,
    }
    try:
        cfg = _load_v4_config_dict()
        if cfg:
            caps["wallbox_kwh"] = max(0.0, float(cfg.get('ml_home_reference_max_wallbox_kwh', caps["wallbox_kwh"])))
            caps["climate_kwh"] = max(0.0, float(cfg.get('ml_home_reference_max_climate_kwh', caps["climate_kwh"])))
    except Exception:
        pass
    return caps


def _home_reference_min_kwh(daily_cap=None):
    """Untergrenze für plausible volle Haus-Tageswerte.

    Sehr kleine Tageswerte entstehen nach Neuaufbau/Import oft aus
    unvollständigen Zählern. Sie dürfen die Hausprognose nicht nach unten
    ziehen; echte Niedrigverbraucher können den Wert über die Tagescap senken.
    """
    if daily_cap is None:
        daily_cap = _load_ml_home_daily_cap()
    try:
        daily_cap = max(1.0, float(daily_cap or 24.0))
    except Exception:
        daily_cap = 24.0
    return max(3.0, min(6.0, daily_cap * 0.18))


def _home_reference_day_is_quiet(wb_total_kwh=0.0, climate_kwh=0.0, caps=None):
    """Nur ruhige Tage für die Haus-Grundlast verwenden.

    Klima und Wallbox sind separat bilanziert. Sie werden hier nicht nochmals
    vom Hausverbrauch abgezogen, sondern markieren den Tag als schlechte
    Referenz für die normale Haus-Grundlast.
    """
    caps = caps or _load_ml_home_reference_caps()
    try:
        wb_total_kwh = max(0.0, float(wb_total_kwh or 0.0))
    except Exception:
        wb_total_kwh = 0.0
    try:
        climate_kwh = max(0.0, float(climate_kwh or 0.0))
    except Exception:
        climate_kwh = 0.0
    return (
        wb_total_kwh <= float(caps.get("wallbox_kwh", 3.0))
        and climate_kwh <= float(caps.get("climate_kwh", 3.0))
    )


def _accuracy_log_home_reference_is_quiet(entry, caps=None):
    wb_total = (
        _as_float(entry.get("actual_wb_kwh", entry.get("wb_consumption", 0.0)), 0.0)
        + _as_float(entry.get("actual_wb2_kwh", entry.get("wb2_consumption", 0.0)), 0.0)
    )
    climate = _as_float(entry.get("actual_climate_kwh", entry.get("climate_consumption", 0.0)), 0.0)
    return _home_reference_day_is_quiet(wb_total, climate, caps=caps)


def _percentile(values, q):
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    idx = int(round((len(ordered) - 1) * max(0.0, min(1.0, float(q)))))
    return ordered[max(0, min(len(ordered) - 1, idx))]


def _extract_outdoor_temp(row, lux_index=None, dt=None):
    for key in ("Aussentemp", "Aussentemperatur", "Außentemperatur", "forecast_temp_c", "temp_c"):
        if key in row:
            value = _as_float(row.get(key), None)
            if value is not None:
                return value
    if lux_index and dt is not None:
        return lux_index.get((dt.date().isoformat(), int(dt.hour)))
    return None


def _iter_climate_history_samples(max_files=45):
    lux_index = _build_luxtronik_temp_index()
    rows = []

    if os.path.isdir(CLIMATE_HISTORY_DIR):
        files = sorted(
            (fn for fn in os.listdir(CLIMATE_HISTORY_DIR) if fn.endswith(".jsonl")),
            reverse=True,
        )[:max_files]
        for filename in files:
            path = os.path.join(CLIMATE_HISTORY_DIR, filename)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except Exception:
                            continue
                        dt = _parse_live_ts(row.get("ts"))
                        if dt is None:
                            continue
                        temp = _extract_outdoor_temp(row, lux_index=lux_index, dt=dt)
                        if temp is None:
                            continue
                        rows.append({
                            "dt": dt,
                            "temp_c": float(temp),
                            "power_w": max(0.0, _as_float(row.get("power_w"), 0.0)),
                            "active": bool(row.get("active", False)),
                            "source": "climate_history",
                        })
            except Exception:
                continue

    if os.path.exists(LIVE_HISTORY_PATH):
        try:
            with open(LIVE_HISTORY_PATH, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    dt = _parse_live_ts(row.get("ts"))
                    if dt is None:
                        continue
                    power_w = max(0.0, _as_float(row.get("climate", row.get("climate_power_w")), 0.0))
                    if power_w <= 0.0 and "climate_active" not in row:
                        continue
                    temp = _extract_outdoor_temp(row, lux_index=lux_index, dt=dt)
                    if temp is None:
                        continue
                    rows.append({
                        "dt": dt,
                        "temp_c": float(temp),
                        "power_w": power_w,
                        "active": bool(row.get("climate_active", power_w > 50.0)),
                        "source": "live_history",
                    })
        except Exception:
            pass

    return rows


def _build_climate_forecast_profile(cfg=None):
    cfg = cfg if isinstance(cfg, dict) else _load_v4_config_dict()
    if not _truthy(cfg.get("climate_enable"), False):
        return {"enabled": False, "reason": "climate_disabled", "samples": 0}
    if not _truthy(cfg.get("climate_forecast_enable"), False):
        return {"enabled": False, "reason": "climate_forecast_disabled", "samples": 0}

    min_power_w = max(20.0, _as_float(cfg.get("climate_min_power_w"), 50.0))
    samples = _iter_climate_history_samples()
    active = [
        s for s in samples
        if max(0.0, float(s.get("power_w", 0.0) or 0.0)) >= min_power_w
        or bool(s.get("active"))
    ]
    if len(active) < 6:
        return {
            "enabled": False,
            "reason": "insufficient_climate_history",
            "samples": len(samples),
            "active_samples": len(active),
        }

    active_temps = [float(s["temp_c"]) for s in active]
    activation_temp_c = round(_percentile(active_temps, 0.25), 1)
    active_powers = [max(min_power_w, float(s.get("power_w", 0.0) or 0.0)) for s in active]
    avg_active_kw = max(0.0, sum(active_powers) / len(active_powers) / 1000.0)

    slot_samples = {}
    for s in active:
        dt = s["dt"]
        slot_key = dt.hour * 4 + int(dt.minute / 15)
        slot_samples.setdefault(slot_key, []).append(max(min_power_w, float(s.get("power_w", 0.0) or 0.0)) / 1000.0)
    slot_kw = {
        str(slot): round(sum(values) / len(values), 4)
        for slot, values in slot_samples.items()
        if values
    }

    return {
        "enabled": True,
        "reason": "ok",
        "source": "climate_history_temperature",
        "samples": len(samples),
        "active_samples": len(active),
        "activation_temp_c": activation_temp_c,
        "avg_active_kw": round(avg_active_kw, 4),
        "slot_kw": slot_kw,
    }


def _climate_power_kw_for_slot(profile, slot_temp_c, slot_key):
    if not isinstance(profile, dict) or not profile.get("enabled"):
        return 0.0
    try:
        if float(slot_temp_c) < float(profile.get("activation_temp_c", 99.0)):
            return 0.0
    except Exception:
        return 0.0
    slot_kw = profile.get("slot_kw") if isinstance(profile.get("slot_kw"), dict) else {}
    return max(0.0, _as_float(slot_kw.get(str(slot_key)), _as_float(profile.get("avg_active_kw"), 0.0)))


def _load_consumption_eval_log():
    try:
        if os.path.exists(ML_CONSUMPTION_EVAL_PATH):
            with open(ML_CONSUMPTION_EVAL_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("forecasts", {})
                    data.setdefault("daily_log", [])
                    return data
    except Exception:
        pass
    return {"forecasts": {}, "daily_log": []}


def _write_consumption_eval_log(data):
    try:
        os.makedirs(os.path.dirname(ML_CONSUMPTION_EVAL_PATH), exist_ok=True)
        tmp_path = ML_CONSUMPTION_EVAL_PATH + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, ML_CONSUMPTION_EVAL_PATH)
        try:
            os.chmod(ML_CONSUMPTION_EVAL_PATH, 0o664)
        except Exception:
            pass
    except Exception as e:
        print(f"ML-Verbrauchs-Accuracy-Log konnte nicht geschrieben werden: {e}")


def _daily_forecasts_from_timeline(timeline):
    days = {}
    for slot in timeline or []:
        try:
            start_ts = float(slot["start_timestamp"])
            day = datetime.datetime.fromtimestamp(start_ts / 1000.0).date().isoformat()
            entry = days.setdefault(day, {
                "home_kwh": 0.0,
                "wp_kwh": 0.0,
                "climate_kwh": 0.0,
                "coverage_slots": 0,
                "first_ts": int(start_ts),
                "last_ts": int(start_ts),
            })
            # timeline.home_kwh/wp_kwh enthalten kW Durchschnittsleistung je 15-Min-Slot.
            entry["home_kwh"] += max(0.0, float(slot.get("home_kwh", 0.0))) * 0.25
            entry["wp_kwh"] += max(0.0, float(slot.get("wp_kwh", 0.0))) * 0.25
            entry["climate_kwh"] += max(0.0, float(slot.get("climate_kwh", 0.0))) * 0.25
            entry["coverage_slots"] += 1
            entry["first_ts"] = min(entry["first_ts"], int(start_ts))
            entry["last_ts"] = max(entry["last_ts"], int(start_ts))
        except Exception:
            continue
    return {
        day: {
            "home_kwh": round(values["home_kwh"], 3),
            "wp_kwh": round(values["wp_kwh"], 3),
            "climate_kwh": round(values.get("climate_kwh", 0.0), 3),
            "coverage_slots": int(values.get("coverage_slots", 0) or 0),
            "coverage_fraction": round(
                min(1.0, float(values.get("coverage_slots", 0) or 0) / 96.0),
                3,
            ),
            "first_ts": int(values.get("first_ts", 0) or 0),
            "last_ts": int(values.get("last_ts", 0) or 0),
        }
        for day, values in days.items()
    }


def _daily_forecast_has_full_coverage(values, min_slots=ML_DAILY_FORECAST_MIN_COVERAGE_SLOTS):
    try:
        return int(values.get("coverage_slots", 0) or 0) >= int(min_slots)
    except Exception:
        return False


def _accuracy_log_entry_has_full_day_forecast(entry):
    try:
        slots = entry.get("forecast_coverage_slots")
        if slots is not None:
            return int(slots or 0) >= ML_DAILY_FORECAST_MIN_COVERAGE_SLOTS
    except Exception:
        return False

    forecast_ts = str(entry.get("forecast_ts") or "")
    if not forecast_ts:
        return True
    try:
        ts = datetime.datetime.fromisoformat(forecast_ts[:19])
        return ts.hour < ML_DAILY_FORECAST_LEGACY_FULL_DAY_CUTOFF_HOUR
    except Exception:
        return False


def _compute_consumption_bias(eval_log, min_days=3):
    daily_log = eval_log.get("daily_log", []) if isinstance(eval_log, dict) else []
    daily_cap = _load_ml_home_daily_cap()
    min_home_reference_kwh = _home_reference_min_kwh(daily_cap)
    reference_caps = _load_ml_home_reference_caps()
    usable = [
        e for e in daily_log[-14:]
        if e.get("forecast_home_kwh", 0) > 1.0 and e.get("actual_home_kwh", 0) > 0.5
        and float(e.get("actual_home_kwh", 0) or 0) >= min_home_reference_kwh
        and float(e.get("actual_home_kwh", 0) or 0) <= daily_cap * 1.35
        and _accuracy_log_home_reference_is_quiet(e, caps=reference_caps)
        and _accuracy_log_entry_has_full_day_forecast(e)
    ]
    home_ratios = [
        max(0.55, min(1.60, float(e["actual_home_kwh"]) / float(e["forecast_home_kwh"])))
        for e in usable
    ]
    wp_ratios = []
    for e in daily_log[-14:]:
        if not _accuracy_log_entry_has_full_day_forecast(e):
            continue
        try:
            forecast_wp = float(e.get("forecast_wp_kwh", 0) or 0.0)
            actual_wp = float(e.get("actual_wp_kwh", 0) or 0.0)
        except (TypeError, ValueError):
            continue
        if forecast_wp <= 0.5 or actual_wp < 0.0:
            continue
        # WP-aus-Tage sind echte Lerndaten. Nicht auf exakt 0 klemmen,
        # damit die Prognose wieder hochlernen kann, wenn die WP zurückkehrt.
        wp_ratios.append(max(0.05, min(2.0, actual_wp / forecast_wp)))

    bias_home = sum(home_ratios[-7:]) / len(home_ratios[-7:]) if len(home_ratios) >= min_days else 1.0
    bias_wp = sum(wp_ratios[-7:]) / len(wp_ratios[-7:]) if len(wp_ratios) >= min_days else 1.0
    return bias_home, bias_wp, len(home_ratios), len(wp_ratios)


def _recent_home_consumption_baseline(days=14):
    """Robuste Hausverbrauchs-Basis aus den letzten echten Tageswerten.

    Das schuetzt die Prognose vor einzelnen Trainingsartefakten, ohne echte
    Koch-/Haushaltsspitzen im Livebetrieb abzuschneiden.
    """
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        cols = {
            row[1]
            for row in c.execute("PRAGMA table_info(daily_stats)").fetchall()
        }
        wb2_expr = "wb2_consumption" if "wb2_consumption" in cols else "0"
        climate_expr = "climate_consumption" if "climate_consumption" in cols else "0"
        c.execute(f"""
            SELECT home_consumption, wb_consumption, {wb2_expr}, wp_consumption, {climate_expr}
            FROM daily_stats
            WHERE date < date('now') AND home_consumption > 0.5
            ORDER BY date DESC
            LIMIT ?
        """, (days,))
        values = []
        raw_values = []
        quiet_raw_values = []
        daily_cap = _load_ml_home_daily_cap()
        min_home_reference_kwh = _home_reference_min_kwh(daily_cap)
        reference_caps = _load_ml_home_reference_caps()
        for home, wb1, wb2, wp, climate in c.fetchall():
            if home is None:
                continue
            home = float(home or 0.0)
            wb_total = max(0.0, float(wb1 or 0.0)) + max(0.0, float(wb2 or 0.0))
            climate_total = max(0.0, float(climate or 0.0))
            raw_values.append(home)

            # Tage mit deutlich aktivem Zusatzverbraucher sind für die normale
            # Haus-Grundlast keine gute Referenz. Nicht doppelt abziehen, sondern
            # als Referenztag ausblenden.
            candidate = home
            if not _home_reference_day_is_quiet(wb_total, climate_total, caps=reference_caps):
                continue
            if wb_total > 1.0 and home > 18.0:
                corrected = home - wb_total
                if 8.0 <= corrected <= daily_cap * 1.2:
                    candidate = corrected

            quiet_raw_values.append(candidate)
            if min_home_reference_kwh <= candidate <= daily_cap * 1.35:
                values.append(candidate)

        conn.close()
        if len(values) < 3:
            values = [v for v in quiet_raw_values if min_home_reference_kwh <= v <= daily_cap * 1.35]
        if len(values) < 3:
            return None
        values.sort()
        # Der reine Hausverbrauch ist als Regelbasis absichtlich konservativ:
        # obere Ausreisser stammen oft von Wallbox/WP/Heizstab oder Alt-Daten.
        values = values[:max(3, int(len(values) * 0.65))]
        mid = len(values) // 2
        if len(values) % 2:
            baseline = values[mid]
        else:
            baseline = (values[mid - 1] + values[mid]) / 2.0
        return min(baseline, daily_cap)
    except Exception as e:
        print(f"ML-Verbrauchs-Sanity: Tagesbasis konnte nicht gelesen werden: {e}")
        return None


def _recent_wp_consumption_baseline(days=14):
    """Robuste Wärmepumpen-Basis aus den letzten echten Tageswerten."""
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            SELECT wp_consumption
            FROM daily_stats
            WHERE date < date('now') AND wp_consumption IS NOT NULL
            ORDER BY date DESC
            LIMIT ?
        """, (days,))
        values = []
        for (wp,) in c.fetchall():
            try:
                value = max(0.0, float(wp or 0.0))
            except (TypeError, ValueError):
                continue
            if value <= 60.0:
                values.append(value)
        conn.close()
        if len(values) < 3:
            return None
        values.sort()
        idx = min(len(values) - 1, max(0, int(round((len(values) - 1) * 0.75))))
        return values[idx]
    except Exception as e:
        print(f"ML-Verbrauchs-Sanity: WP-Tagesbasis konnte nicht gelesen werden: {e}")
        return None


def _apply_home_forecast_sanity(timeline):
    baseline = _recent_home_consumption_baseline()
    if not baseline or baseline <= 0:
        return 1.0, None

    # Nur deutliche Prognose-Ausreisser bremsen. Echte Haushalte koennen mit
    # Waschmaschine, Klima, Kochen usw. auch 25-30 kWh verbrauchen; abgefangen
    # werden vor allem Wallbox-/Alt-Daten, die >40 kWh Grundlast vortaeuschen.
    trigger = baseline * 1.35
    target = baseline * 1.25
    day_sums = _daily_forecasts_from_timeline(timeline)
    factors = []

    for day, values in day_sums.items():
        forecast_home = float(values.get("home_kwh", 0.0) or 0.0)
        if forecast_home <= trigger:
            continue
        factor = max(0.35, min(1.0, target / forecast_home))
        factors.append(factor)
        for slot in timeline:
            try:
                slot_day = datetime.datetime.fromtimestamp(
                    float(slot["start_timestamp"]) / 1000.0
                ).date().isoformat()
            except Exception:
                continue
            if slot_day == day:
                slot["home_kwh"] = round(float(slot.get("home_kwh", 0.0) or 0.0) * factor, 4)

    if factors:
        applied = min(factors)
        print(
            f"ML-Verbrauchs-Sanity: Hausprognose gedeckelt "
            f"(Basis {baseline:.1f} kWh/Tag, Faktor min. x{applied:.2f})"
        )
        return applied, baseline
    return 1.0, baseline


def _apply_wp_forecast_sanity(timeline):
    baseline = _recent_wp_consumption_baseline()
    if baseline is None:
        return 1.0, None

    trigger = max(2.0, baseline * 2.2)
    target = max(0.8, baseline * 1.6)
    day_sums = _daily_forecasts_from_timeline(timeline)
    factors = []

    for day, values in day_sums.items():
        forecast_wp = float(values.get("wp_kwh", 0.0) or 0.0)
        if forecast_wp <= trigger:
            continue
        factor = max(0.05, min(1.0, target / forecast_wp))
        factors.append(factor)
        for slot in timeline:
            try:
                slot_day = datetime.datetime.fromtimestamp(
                    float(slot["start_timestamp"]) / 1000.0
                ).date().isoformat()
            except Exception:
                continue
            if slot_day == day:
                slot["wp_kwh"] = round(float(slot.get("wp_kwh", 0.0) or 0.0) * factor, 4)

    if factors:
        applied = min(factors)
        print(
            f"ML-Verbrauchs-Sanity: WP-Prognose gedeckelt "
            f"(Basis {baseline:.1f} kWh/Tag, Faktor min. x{applied:.2f})"
        )
        return applied, baseline
    return 1.0, baseline


def _update_consumption_accuracy_log(timeline):
    eval_log = _load_consumption_eval_log()
    now = datetime.datetime.now()
    today = now.date().isoformat()
    forecasts = eval_log.setdefault("forecasts", {})

    for day, values in _daily_forecasts_from_timeline(timeline).items():
        if day >= today:
            if not _daily_forecast_has_full_coverage(values):
                continue
            existing_forecast = forecasts.get(day)
            if isinstance(existing_forecast, dict):
                try:
                    existing_slots = int(existing_forecast.get("coverage_slots", 0) or 0)
                except Exception:
                    existing_slots = 0
                if existing_slots >= ML_DAILY_FORECAST_MIN_COVERAGE_SLOTS and values["coverage_slots"] < existing_slots:
                    continue
            forecasts[day] = {
                "ts": now.isoformat(),
                "home_kwh": values["home_kwh"],
                "wp_kwh": values["wp_kwh"],
                "climate_kwh": values.get("climate_kwh", 0.0),
                "coverage_slots": values["coverage_slots"],
                "coverage_fraction": values["coverage_fraction"],
                "first_ts": values["first_ts"],
                "last_ts": values["last_ts"],
            }

    actual_by_day = {}
    try:
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            cols = {
                row[1]
                for row in c.execute("PRAGMA table_info(daily_stats)").fetchall()
            }
            wb2_expr = "COALESCE(wb2_consumption, 0)" if "wb2_consumption" in cols else "0"
            climate_expr = "COALESCE(climate_consumption, 0)" if "climate_consumption" in cols else "0"
            c.execute(f"""
                SELECT date, home_consumption, wp_consumption,
                       COALESCE(wb_consumption, 0), {wb2_expr}, {climate_expr}
                FROM daily_stats
                WHERE date >= date('now', '-21 days')
                ORDER BY date ASC
            """)
            for day, home, wp, wb1, wb2, climate in c.fetchall():
                actual_by_day[str(day)] = {
                    "home_kwh": float(home or 0.0),
                    "wp_kwh": float(wp or 0.0),
                    "wb_kwh": float(wb1 or 0.0),
                    "wb2_kwh": float(wb2 or 0.0),
                    "climate_kwh": float(climate or 0.0),
                }
            conn.close()
    except sqlite3.OperationalError:
        actual_by_day = {}
    except Exception as e:
        print(f"ML-Verbrauchs-Accuracy-Log: daily_stats konnte nicht gelesen werden: {e}")

    existing = {str(e.get("date")): e for e in eval_log.get("daily_log", []) if e.get("date")}
    for day, forecast in forecasts.items():
        if day >= today or day not in actual_by_day:
            continue
        actual = actual_by_day[day]
        forecast_home = float(forecast.get("home_kwh", 0.0) or 0.0)
        forecast_wp = float(forecast.get("wp_kwh", 0.0) or 0.0)
        forecast_climate = float(forecast.get("climate_kwh", 0.0) or 0.0)
        forecast_coverage_slots = int(forecast.get("coverage_slots", 0) or 0)
        if forecast_coverage_slots and forecast_coverage_slots < ML_DAILY_FORECAST_MIN_COVERAGE_SLOTS:
            continue
        existing[day] = {
            "date": day,
            "forecast_ts": forecast.get("ts"),
            "forecast_coverage_slots": forecast_coverage_slots or None,
            "forecast_coverage_fraction": forecast.get("coverage_fraction"),
            "forecast_home_kwh": round(forecast_home, 3),
            "actual_home_kwh": round(actual["home_kwh"], 3),
            "actual_wb_kwh": round(actual.get("wb_kwh", 0.0), 3),
            "actual_wb2_kwh": round(actual.get("wb2_kwh", 0.0), 3),
            "home_ratio": round(actual["home_kwh"] / forecast_home, 3) if forecast_home > 0.2 else None,
            "forecast_wp_kwh": round(forecast_wp, 3),
            "actual_wp_kwh": round(actual["wp_kwh"], 3),
            "wp_ratio": round(actual["wp_kwh"] / forecast_wp, 3) if forecast_wp > 0.2 else None,
            "forecast_climate_kwh": round(forecast_climate, 3),
            "actual_climate_kwh": round(actual.get("climate_kwh", 0.0), 3),
            "climate_ratio": round(actual.get("climate_kwh", 0.0) / forecast_climate, 3) if forecast_climate > 0.2 else None,
        }

    eval_log["daily_log"] = [existing[day] for day in sorted(existing.keys())][-21:]
    cutoff = (now.date() - datetime.timedelta(days=21)).isoformat()
    eval_log["forecasts"] = {day: data for day, data in forecasts.items() if day >= cutoff}
    _write_consumption_eval_log(eval_log)
    return eval_log

def load_training_data():
    if not os.path.exists(DB_PATH): return None, None

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    _ensure_ml_training_table(conn)
    conn.commit()
    row_count = c.execute("SELECT COUNT(*) FROM ml_training_data").fetchone()[0]
    conn.close()

    # Auch bei bereits großer, aber alter Tabelle die aktuelle Live-Historie
    # idempotent nachziehen. INSERT OR REPLACE verhindert Dubletten. Damit
    # trainiert ein System nach Monaten nicht dauerhaft nur auf dem alten
    # Installationsfenster weiter.
    _seed_ml_training_from_live_history(refresh=True)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT date, time_gmt, home_kwh_cum, wp_kwh_cum, temp_c FROM ml_training_data ORDER BY date, time_gmt")
    rows = c.fetchall()
    conn.close()

    if len(rows) < 50:
        print(f"Nicht genug Trainingsdaten: {len(rows)} Datensaetze (benoetigt: 50).")
        return None, None

    X = []; y_home = []; y_wp = []
    last_date = None; last_home_cum = 0; last_wp_cum = 0; last_time_gmt = 0

    for row in rows:
        date_str, time_gmt, home_cum, wp_cum, temp_c = row
        try:
            dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            day_of_week = dt.weekday() # 0 = Mo, 6 = So
            month = dt.month
        except: continue

        if last_date == date_str:
            time_diff = time_gmt - last_time_gmt
            if time_diff < 0: time_diff += 24.0 # Sollte durch ORDER BY nicht passieren

            # Ignoriere Lücken > 2 Stunden
            if 0 < time_diff <= 2.0:
                home_delta = max(0, home_cum - last_home_cum)
                wp_delta = max(0, wp_cum - last_wp_cum)

                # Normierung auf Leistung (kW) macht das Training immun gegen unregelmäßige Zeitabstände
                home_kw = home_delta / time_diff
                wp_kw = wp_delta / time_diff

                # M2: Konfigurierbarer Cap (Standard 6.0 kW) statt hardcoded 1.5 kW.
                # Schuetzt vor Sensor-Artefakten (Neustart 30kW etc.) ohne echte Kochspitzen abzuschneiden.
                # (WB ist seit V4 RSCP separat erfasst und nicht mehr in home_kw enthalten)
                _home_cap = _load_ml_home_cap()
                if home_kw >= 0 and home_kw < 30.0 and wp_kw >= 0 and wp_kw < 20.0:
                    home_kw = min(home_kw, _home_cap)
                    X.append([time_gmt, day_of_week, month, temp_c])
                    y_home.append(home_kw)
                    y_wp.append(wp_kw)

        last_date = date_str; last_home_cum = home_cum; last_wp_cum = wp_cum; last_time_gmt = time_gmt

    return np.array(X), (np.array(y_home), np.array(y_wp))


def load_training_data_enriched(lux_index):
    """Wie load_training_data(), ersetzt aber temp_c durch gemessene Luxtronik-Aussentemperatur
    aus dem uebergebenen Index {(date_str, hour): avg_temp_c}.
    Fuer Zeitslots ohne Archivwert bleibt der DB-Wert erhalten.
    """
    if not os.path.exists(DB_PATH): return None, None

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT date, time_gmt, home_kwh_cum, wp_kwh_cum, temp_c FROM ml_training_data ORDER BY date, time_gmt")
    rows = c.fetchall()
    conn.close()

    if len(rows) < 50:
        return None, None

    X = []; y_home = []; y_wp = []
    last_date = None; last_home_cum = 0; last_wp_cum = 0; last_time_gmt = 0

    for row in rows:
        date_str, time_gmt, home_cum, wp_cum, temp_c_db = row
        try:
            dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            day_of_week = dt.weekday()
            month = dt.month
        except: continue

        if last_date == date_str:
            time_diff = time_gmt - last_time_gmt
            if time_diff < 0: time_diff += 24.0

            if 0 < time_diff <= 2.0:
                home_delta = max(0, home_cum - last_home_cum)
                wp_delta   = max(0, wp_cum   - last_wp_cum)
                home_kw = home_delta / time_diff
                wp_kw   = wp_delta   / time_diff

                # M2: Konfigurierbarer Cap (Standard 6.0 kW) statt hardcoded 1.5 kW
                _home_cap = _load_ml_home_cap()
                if home_kw >= 0 and home_kw < 30.0 and wp_kw >= 0 and wp_kw < 20.0:
                    home_kw = min(home_kw, _home_cap)

                    # Luxtronik-Archiv-Temperatur bevorzugen (Stunden-Rundung)
                    hour = int(time_gmt)
                    real_temp = lux_index.get((date_str, hour))
                    temp_c = real_temp if real_temp is not None else (temp_c_db or 8.5)

                    X.append([time_gmt, day_of_week, month, temp_c])
                    y_home.append(home_kw)
                    y_wp.append(wp_kw)

        last_date = date_str; last_home_cum = home_cum; last_wp_cum = wp_cum; last_time_gmt = time_gmt

    if len(X) < 50:
        return None, None
    return np.array(X), (np.array(y_home), np.array(y_wp))


def _build_luxtronik_temp_index():
    """Liest alle verfuegbaren Luxtronik-Archivdateien und baut einen
    Stunden-Index {(date_str, hour_int): avg_temp_c} auf.
    Unterstuetzt:
      - /var/www/html/data/luxtronik_archive/luxtronik_YYYY-MM-DD.json  (JSONL, Messung/60s)
      - /var/www/html/ramdisk/luxtronik.json                             (Live-Objekt, Messung aktuell)
      - /var/www/html/ramdisk/waermepumpe.json                           (alternativer Live-Key)
    Gibt {} zurueck wenn keine Quellen vorhanden.
    """
    temp_index = {}  # {(date_str, hour): [temp_c, ...]}

    def _add(date_str, hour, temp):
        key = (date_str, int(hour))
        if key not in temp_index:
            temp_index[key] = []
        temp_index[key].append(float(temp))

    # 1) Luxtronik-Archiv (eine JSONL-Datei pro Tag)
    archive_dir = "/var/www/html/data/luxtronik_archive"
    if os.path.isdir(archive_dir):
        for fname in os.listdir(archive_dir):
            if not fname.startswith("luxtronik_") or not fname.endswith(".json"):
                continue
            # luxtronik_2026-04-11.json -> Datum extrahieren
            try:
                date_str = fname[len("luxtronik_"):-len(".json")]  # "YYYY-MM-DD"
                datetime.datetime.strptime(date_str, "%Y-%m-%d")   # Format-Check
            except Exception:
                continue
            fpath = os.path.join(archive_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        # Temperatur-Key: normalisierter Aussentemp aus data-Block bevorzugt
                        temp = None
                        data = obj.get("data", {})
                        for key in ("Aussentemp", "Aussentemperatur", u"Au\xdfentemperatur"):
                            if key in data:
                                temp = data[key]
                                break
                        if temp is None:
                            temp = obj.get("Aussentemp") or obj.get("Aussentemperatur")
                        if temp is None:
                            continue
                        # Uhrzeit aus Timestamp
                        ts = obj.get("ts")
                        if isinstance(ts, str):
                            try:
                                dt = datetime.datetime.fromisoformat(ts)
                                _add(date_str, dt.hour, temp)
                            except Exception:
                                pass
                        elif isinstance(ts, (int, float)) and ts > 1e9:
                            dt = datetime.datetime.fromtimestamp(ts)
                            _add(date_str, dt.hour, temp)
            except Exception:
                pass

    # 2) Live-Dateien (heutiger Tag - als Ergaenzung wenn noch kein Archiveintrag fuer heute)
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    for live_path in [
        "/var/www/html/ramdisk/luxtronik.json",
        "/var/www/html/ramdisk/waermepumpe.json",
    ]:
        if not os.path.exists(live_path):
            continue
        try:
            with open(live_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            temp = None
            data = obj.get("data", {}) if isinstance(obj, dict) else {}
            for key in ("Aussentemp", "Aussentemperatur", u"Au\xdfentemperatur"):
                if key in data:
                    temp = data[key]
                    break
            if temp is None:
                temp = obj.get("Aussentemp") or obj.get("Aussentemperatur")
            if temp is not None:
                now_h = datetime.datetime.now().hour
                _add(today_str, now_h, temp)
        except Exception:
            pass

    # Mittelwert pro Stunde bilden
    result = {}
    for (date_str, hour), vals in temp_index.items():
        result[(date_str, hour)] = sum(vals) / len(vals)

    return result


def _trimmed_mean(values):
    """Robuster Erwartungswert für historische Leistungsstichproben."""

    finite = sorted(
        float(value)
        for value in values
        if isinstance(value, (int, float)) and float(value) >= 0.0
    )
    if not finite:
        return None
    if len(finite) >= 10:
        trim = max(1, int(len(finite) * 0.1))
        finite = finite[trim:-trim] or finite
    return statistics.fmean(finite)


def _load_empirical_consumption_profile(now=None):
    """Liest ein reines, nicht ausführbares Historienprofil ohne Pickle.

    Der Pfad ist der sichere Rückfall, wenn noch kein manifestgebundenes
    ML-Modell existiert. Er deserialisiert ausdrücklich kein Legacy-Modell.
    Aus den kumulativen 15-Minuten-Historien werden robuste Leistungswerte je
    Tagesart und Viertelstunde gebildet. Die jüngsten verfügbaren Tage sind
    führend; ihr Alter bleibt als Qualitätsmetadatum sichtbar.
    """

    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ml_training_data'"
        ).fetchone()
        if not table:
            conn.close()
            return None
        rows = conn.execute(
            """
            SELECT date, time_gmt, home_kwh_cum, wp_kwh_cum, temp_c
            FROM ml_training_data
            ORDER BY date, time_gmt
            """
        ).fetchall()
        conn.close()
    except Exception as exc:
        print(f"Historisches Verbrauchsprofil konnte nicht gelesen werden: {exc}")
        return None

    parsed_dates = []
    for row in rows:
        try:
            parsed_dates.append(datetime.datetime.strptime(str(row[0]), "%Y-%m-%d").date())
        except Exception:
            continue
    if not parsed_dates:
        return None
    latest_date = max(parsed_dates)
    recent_start = latest_date - datetime.timedelta(days=ML_EMPIRICAL_RECENT_DAYS - 1)
    home_cap = _load_ml_home_cap()
    samples = []
    previous = None

    for date_str, time_gmt, home_cum, wp_cum, temp_c in rows:
        try:
            day = datetime.datetime.strptime(str(date_str), "%Y-%m-%d").date()
            hour_value = float(time_gmt)
            home_value = float(home_cum)
            wp_value = float(wp_cum)
        except Exception:
            previous = None
            continue
        if day < recent_start:
            continue
        if previous is not None and previous["day"] == day:
            delta_h = hour_value - previous["hour"]
            if delta_h < 0.0:
                delta_h += 24.0
            if 0.0 < delta_h <= 2.0:
                home_kw = max(0.0, home_value - previous["home"]) / delta_h
                wp_kw = max(0.0, wp_value - previous["wp"]) / delta_h
                if home_kw < 30.0 and wp_kw < 20.0:
                    slot = int(round(hour_value * 4.0)) % 96
                    samples.append({
                        "slot": slot,
                        "day_type": "weekend" if day.weekday() >= 5 else "weekday",
                        "home_kw": min(home_kw, home_cap),
                        "wp_kw": wp_kw,
                        "temp_c": _as_float(temp_c, 8.5),
                        "date": day.isoformat(),
                    })
        previous = {
            "day": day,
            "hour": hour_value,
            "home": home_value,
            "wp": wp_value,
        }

    if len(samples) < ML_EMPIRICAL_MIN_SAMPLES:
        return None

    current_date = (now or datetime.datetime.now()).date()
    return {
        "samples": samples,
        "sample_count": len(samples),
        "home_positive_samples": sum(1 for row in samples if row["home_kw"] > 0.0),
        "wp_positive_samples": sum(1 for row in samples if row["wp_kw"] > 0.0),
        "first_date": min(row["date"] for row in samples),
        "last_date": latest_date.isoformat(),
        "age_days": max(0, (current_date - latest_date).days),
        "recent_days": ML_EMPIRICAL_RECENT_DAYS,
    }


def _wp_forecast_capable(cfg, profile):
    try:
        if int(cfg.get("wp_type", -1)) > 0:
            return True
    except Exception:
        pass
    if int(profile.get("wp_positive_samples", 0) or 0) >= 4:
        return True
    for key in (
        "luxtronik",
        "native_heatpump_enable",
        "market_heatpump_enable",
        "heat_policy_runtime_enable",
    ):
        if _truthy(cfg.get(key), False):
            return True
    return False


def _empirical_power_for_slot(profile, target_ts, slot_temp, cfg):
    samples = list(profile.get("samples") or [])
    slot = target_ts.hour * 4 + int(target_ts.minute / 15)
    day_type = "weekend" if target_ts.weekday() >= 5 else "weekday"

    candidates = [
        row for row in samples
        if row.get("slot") == slot and row.get("day_type") == day_type
    ]
    source = "historical_slot_daytype"
    if len(candidates) < 2:
        candidates = [row for row in samples if row.get("slot") == slot]
        source = "historical_slot_all_days"
    if len(candidates) < 2:
        candidates = [
            row for row in samples
            if min((int(row.get("slot", 0)) - slot) % 96, (slot - int(row.get("slot", 0))) % 96) <= 2
        ]
        source = "historical_neighbour_slots"
    if not candidates:
        candidates = samples
        source = "historical_global_profile"

    home_kw = _trimmed_mean([row.get("home_kw") for row in candidates])
    if home_kw is None or home_kw <= 0.0:
        home_kw = 0.5
        home_source = "static_fallback_no_home_history"
        home_quality = "fallback"
    else:
        home_source = source
        home_quality = "empirical"

    if _wp_forecast_capable(cfg, profile):
        # Für die WP aus demselben Zeitslot die zur Wetterprognose nächsten
        # historischen Temperaturen bevorzugen. Mittelwert statt Median bildet
        # den erwarteten Taktanteil ab, ohne einzelne Leistungsspitzen zu kopieren.
        ranked = sorted(
            candidates,
            key=lambda row: abs(_as_float(row.get("temp_c"), 8.5) - float(slot_temp)),
        )
        wp_candidates = ranked[: min(12, max(3, len(ranked)))]
        wp_kw = _trimmed_mean([row.get("wp_kw") for row in wp_candidates])
        if wp_kw is None or int(profile.get("wp_positive_samples", 0) or 0) < 4:
            wp_kw = 0.3
            wp_source = "static_fallback_no_wp_history"
            wp_quality = "fallback"
        else:
            wp_source = source + "_temperature_matched"
            wp_quality = "empirical"
    else:
        wp_kw = 0.0
        wp_source = "not_applicable"
        wp_quality = "not_applicable"

    return {
        "home_kw": max(0.0, float(home_kw)),
        "wp_kw": max(0.0, float(wp_kw)),
        "home_source": home_source,
        "home_quality": home_quality,
        "wp_source": wp_source,
        "wp_quality": wp_quality,
        "sample_count": len(candidates),
    }


def train_model():
    if not HAS_SKLEARN:
        print("FEHLER: scikit-learn ist nicht installiert. Bitte fuehre im Installer Punkt 3 aus.")
        _activate_conservative_ml_fallback("scikit-learn fehlt")
        return False

    legacy_notice = _legacy_ml_model_notice()
    if legacy_notice:
        print(legacy_notice)

    print("Lade und bereite historische Daten auf...")
    res = load_training_data()
    if res[0] is None:
        _activate_conservative_ml_fallback("noch keine ausreichenden Trainingsdaten")
        return False

    X, (y_home, y_wp) = res

    # --- Temperaturen aus Luxtronik-Archiv anreichern (wenn vorhanden) ---
    # Dadurch lernt das Modell echte Aussentemperaturen statt einen gemeinsamen Fallback-Wert.
    # Nutzer ohne WP haben kein Archiv -> Index bleibt leer -> temp_c aus DB bleibt unveraendert.
    print("Baue Luxtronik-Temperaturindex auf (kann einen Moment dauern)...")
    lux_index = _build_luxtronik_temp_index()
    if lux_index:
        print(f"  Luxtronik-Index: {len(lux_index)} Stunden-Slots aus Archiv geladen.")
        X_enriched, res_e = load_training_data_enriched(lux_index)
        if X_enriched is not None and len(X_enriched) > 0:
            X, y_home, y_wp = X_enriched, res_e[0], res_e[1]
            print(f"  Temperatur-Anreicherung: {len(X)} Datensaetze mit echten Luxtronik-Aussentemps.")
    else:
        print("  Kein Luxtronik-Archiv gefunden - nutze temp_c aus Datenbank (Fallback).")

    print(f"Trainiere Machine Learning Modell mit {len(X)} Datensaetzen...")

    # Random Forest ist extrem stark darin, nicht-lineare Verhaltensmuster zu erkennen!
    model_home = RandomForestRegressor(n_estimators=30, max_depth=10, random_state=42)
    model_wp   = RandomForestRegressor(n_estimators=30, max_depth=10, random_state=42)

    model_home.fit(X, y_home)
    model_wp.fit(X,   y_wp)

    try:
        _write_ml_model_safely(
            MODEL_PATH,
            {'home': model_home, 'wp': model_wp, 'trained_at': datetime.datetime.now().isoformat()},
        )
    except Exception as exc:
        print(f"FEHLER: ML-Modell konnte nicht sicher gespeichert werden: {exc}")
        _activate_conservative_ml_fallback("sicheres Modell konnte nicht veroeffentlicht werden")
        return False

    print(f"[OK] ML-Modell erfolgreich trainiert ({MODEL_PATH}).", flush=True)
    return True

def predict_today():
    now = datetime.datetime.now()
    forecast_mode = "verified_ml_model"
    model_ready = False
    model_reason = ""
    model_home = None
    model_wp = None
    empirical_profile = None

    # Das signierte Modell bleibt die führende Quelle. Fehlt sklearn oder ist
    # das Modell/Manifest nicht vertrauenswürdig, wird niemals ein Legacy-
    # Pickle geladen. Statt des groben 500/300/0-Systemfallbacks erzeugen wir
    # jedoch ein reines Historienprofil aus den bereits validierten lokalen
    # Zählerdaten. Damit bleibt die Verbrauchsprognose variabel, bis ein neues
    # Modell sicher trainiert und veröffentlicht wurde.
    if not HAS_SKLEARN:
        model_reason = "scikit-learn_fehlt"
    elif not ml_model_is_ready(MODEL_PATH):
        model_reason = "kein_vertrauenswuerdiges_modell"
    else:
        try:
            models = _load_ml_model_safely(MODEL_PATH)
            model_home = models['home']
            model_wp = models['wp']
            model_ready = True
        except Exception as exc:
            model_reason = f"modellpruefung_fehlgeschlagen:{type(exc).__name__}"
            print(f"FEHLER: ML-Modell wird aus Sicherheitsgründen nicht geladen: {exc}")

    if not model_ready:
        legacy_notice = _legacy_ml_model_notice()
        if legacy_notice:
            print(legacy_notice)
        empirical_profile = _load_empirical_consumption_profile(now=now)
        if not empirical_profile:
            reason = model_reason or "keine_ausreichende_historie"
            _activate_conservative_ml_fallback(reason)
            return False
        forecast_mode = "historical_profile"
        print(
            "Kein nutzbares sicheres ML-Modell; verwende variables Historienprofil "
            f"aus {empirical_profile['sample_count']} Intervallen "
            f"({empirical_profile['first_date']} bis {empirical_profile['last_date']})."
        )
    else:
        print("Verbrauchsprognose verwendet das manifest- und hashgeprüfte ML-Modell.")

    # --- Schritt 1: Aktuelle Aussentemperatur als Fallback ---
    # Prioritaet: 1) weather_forecast.json (Open-Meteo, aktuelle Stunde) - zuverlaessig!
    #             2) Aussentemp aus Luxtronik-Live-Daten
    #             3) Fixwert 8.5 C  (pvi_temperature_c = Wechselrichter-Innentemp, NICHT Aussen!)
    current_temp = 8.5
    weather_path = "/var/www/html/ramdisk/weather_forecast.json"
    if os.path.exists(weather_path):
        try:
            import time as _t
            with open(weather_path, "r") as wf:
                wdata = json.load(wf)
            now_ts = int(datetime.datetime.now().replace(minute=0, second=0, microsecond=0).timestamp())
            hourly = wdata.get("hourly", {})
            if str(now_ts) in hourly:
                current_temp = float(hourly[str(now_ts)]["temp_c"])
            elif now_ts in hourly:
                current_temp = float(hourly[now_ts]["temp_c"])
        except Exception as e:
            pass  # Fallback auf naechste Quelle

    # Luxtronik Aussentemperatur als Fallback (aber NIEMALS pvi_temperature_c!)
    if current_temp == 8.5:
        for fpath in ["/var/www/html/ramdisk/live_data_py.json"]:
            if os.path.exists(fpath):
                try:
                    with open(fpath, "r") as f:
                        d = json.load(f)
                    if "Aussentemp" in d:
                        current_temp = float(d["Aussentemp"])
                        break
                    # pvi_temperature_c ist Wechselrichter-Innentemp (kann 30-50 C sein!) -> NIEMALS nutzen!
                except:
                    pass


    # --- Schritt 2: Wetter-Prognose laden (stundengenaue Temperatur pro Slot) ---
    # weather_forecast.json wird von pv_forecast_service.py (Open-Meteo) befuellt.
    # Enthaelt {unix_ts_str: {temp_c, radiation_wm2}} fuer die naechsten 96h.
    weather_hourly = {}  # {unix_ts_int: temp_c}
    weather_path = "/var/www/html/ramdisk/weather_forecast.json"
    if os.path.exists(weather_path):
        try:
            with open(weather_path, "r") as f:
                wdata = json.load(f)
            for ts_str, slot in wdata.get("hourly", {}).items():
                weather_hourly[int(ts_str)] = slot["temp_c"]
            print(f"Wetter-Prognose geladen: {len(weather_hourly)} Stunden-Slots verfuegbar.")
        except Exception as e:
            print(f"Wetter-Prognose konnte nicht geladen werden ({e}) - nutze aktuelle Temp als Fallback.")
    else:
        print(f"Keine Wetter-Prognose gefunden ({weather_path}) - nutze {current_temp}C gleichmaessig.")

    total_home_today = 0
    total_wp_today   = 0
    total_climate_today = 0
    timeline = []
    slots_with_forecast = 0
    slots_with_fallback = 0
    cfg = _load_v4_config_dict()
    climate_profile = _build_climate_forecast_profile(cfg)
    if climate_profile.get("enabled"):
        print(
            "Klima-Prognose aktiv: Einschaltgrenze ab %.1fC, %d aktive Historienpunkte."
            % (
                float(climate_profile.get("activation_temp_c", 0.0) or 0.0),
                int(climate_profile.get("active_samples", 0) or 0),
            )
        )
    elif _truthy(cfg.get("climate_enable"), False):
        print(f"Klima-Prognose inaktiv: {climate_profile.get('reason', 'unknown')}.")

    # 72 Stunden rollierend ab der vollen Stunde
    start_time = now.replace(minute=0, second=0, microsecond=0)
    print(f"Berechne 72h Profil ab {start_time.strftime('%H:%M')} (Aktuell: {current_temp}C)...")

    for offset in range(72 * 4):  # 288 Viertelstunden
        target_ts = start_time + datetime.timedelta(minutes=offset * 15)
        d_of_w    = target_ts.weekday()
        m         = target_ts.month
        time_gmt  = target_ts.hour + (target_ts.minute / 60.0)

        # --- Slot-genaue Temperatur aus Wetter-Prognose ---
        # Wir runden auf die volle Stunde des Slots (Open-Meteo liefert stundlich)
        slot_hour_ts = int(target_ts.replace(minute=0, second=0, microsecond=0).timestamp())
        if slot_hour_ts in weather_hourly:
            slot_temp = weather_hourly[slot_hour_ts]
            slots_with_forecast += 1
        else:
            slot_temp = current_temp  # Fallback: aktuelle Aussentemperatur
            slots_with_fallback += 1
        slot_key = target_ts.hour * 4 + int(target_ts.minute / 15)

        if forecast_mode == "verified_ml_model":
            # Feature-Vektor: [Stunde_GMT, Wochentag, Monat,
            # Aussentemperatur_Prognose]. Die Reihenfolge muss exakt dem
            # Training entsprechen.
            X_pred = np.array([[time_gmt, d_of_w, m, slot_temp]])
            power_home = max(0, model_home.predict(X_pred)[0])
            power_wp = max(0, model_wp.predict(X_pred)[0])
            consumption_slot = {
                "home_source": "verified_ml_model",
                "home_quality": "model",
                "wp_source": "verified_ml_model",
                "wp_quality": "model",
                "sample_count": None,
            }
        else:
            consumption_slot = _empirical_power_for_slot(
                empirical_profile,
                target_ts,
                slot_temp,
                cfg,
            )
            power_home = consumption_slot["home_kw"]
            power_wp = consumption_slot["wp_kw"]

        # Leistung in kW (Durchschnittsleistung im 15-Min-Slot)
        power_climate = _climate_power_kw_for_slot(climate_profile, slot_temp, slot_key)
        climate_source = (
            str(climate_profile.get("source") or "historical_climate_profile")
            if climate_profile.get("enabled")
            else "not_applicable"
        )
        climate_quality = "empirical" if climate_profile.get("enabled") else "not_applicable"

        # Energie in kWh = Leistung * 0.25h (für aufsummierte Tages-Statistik)
        energy_home = power_home * 0.25
        energy_wp   = power_wp   * 0.25
        energy_climate = power_climate * 0.25

        timeline.append({
            "start_timestamp": int(target_ts.timestamp() * 1000),
            "end_timestamp":   int((target_ts + datetime.timedelta(minutes=15)).timestamp() * 1000),
            "home_kwh":        round(power_home, 4),  # kW Durchschnittsleistung im Slot
            "wp_kwh":          round(power_wp,   4),
            "climate_kwh":      round(power_climate, 4),
            "home_source": consumption_slot["home_source"],
            "home_quality": consumption_slot["home_quality"],
            "wp_source": consumption_slot["wp_source"],
            "wp_quality": consumption_slot["wp_quality"],
            "climate_source": climate_source,
            "climate_quality": climate_quality,
            "historical_sample_count": consumption_slot.get("sample_count"),
            "climate_forecast_active": bool(power_climate > 0.0),
            "forecast_temp_c": round(slot_temp,  1),  # NEU: verwendete Prognosetemp (Debug/Dashboard)
        })

        if offset < (24 * 4):  # Erste 24h für die Konsolen-Tagessumme
            total_home_today += energy_home
            total_wp_today   += energy_wp
            total_climate_today += energy_climate

    # --- Schritt 3: Bias-Korrektur aus den letzten 7 Tagen ---
    # Der RandomForest lernt Muster, aber systematische Abweichungen (z.B. WP läuft
    # häufiger als historisch) werden nicht automatisch ausgeglichen.
    # Wir berechnen einen EMA-Korrekturfaktor aus Ist- vs. Prognose-Tageswerten.
    bias_home = 1.0
    bias_wp   = 1.0
    if forecast_mode == "verified_ml_model":
        try:
            eval_log = _load_consumption_eval_log()
            bias_home, bias_wp, n_home, n_wp = _compute_consumption_bias(eval_log)
            if bias_home != 1.0 or bias_wp != 1.0:
                print(f"Bias-Korrektur (ML-Verbrauch): Haus x{bias_home:.2f} ({n_home} Tage), WP x{bias_wp:.2f} ({n_wp} Tage)")
            else:
                print("Bias-Korrektur: zu wenig ML-Verbrauchs-Accuracy-Daten - neutraler Bias.")
        except Exception as be:
            print(f"Bias-Korrektur konnte nicht berechnet werden: {be}")
    else:
        # Das Historienprofil ist bereits ein robuster Erwartungswert realer
        # Intervalle. Ein alter ML-Bias würde diese Werte ein zweites Mal
        # korrigieren und kann nach Modellverlust gerade in die falsche
        # Richtung wirken.
        print("Bias-Korrektur: für das empirische Historienprofil neutral.")

    # Bias auf Timeline anwenden
    if bias_home != 1.0 or bias_wp != 1.0:
        for slot in timeline:
            slot['home_kwh'] = round(slot['home_kwh'] * bias_home, 4)
            slot['wp_kwh']   = round(slot['wp_kwh']   * bias_wp,   4)
        total_home_today *= bias_home
        total_wp_today   *= bias_wp

    sanity_factor, sanity_baseline = _apply_home_forecast_sanity(timeline)
    if sanity_factor != 1.0:
        total_home_today = sum(
            max(0.0, float(slot.get('home_kwh', 0.0) or 0.0)) * 0.25
            for slot in timeline[:24 * 4]
        )
    sanity_wp_factor, sanity_wp_baseline = _apply_wp_forecast_sanity(timeline)
    if sanity_wp_factor != 1.0:
        total_wp_today = sum(
            max(0.0, float(slot.get('wp_kwh', 0.0) or 0.0)) * 0.25
            for slot in timeline[:24 * 4]
        )

    _update_consumption_accuracy_log(timeline)

    pct_forecast = round(100 * slots_with_forecast / (slots_with_forecast + slots_with_fallback))
    print(
        f"[OK] Vorhersage für heute (nächste 24h): Haus ~{total_home_today:.1f} kWh | "
        f"WP ~{total_wp_today:.1f} kWh | Klima ~{total_climate_today:.1f} kWh."
    )
    print(
        f"     Temperatur-Prognose: {slots_with_forecast}/{slots_with_forecast+slots_with_fallback} Slots "
        f"mit Wetter-Prognose ({pct_forecast}%), {slots_with_fallback} Slots mit Fallback ({current_temp}C)."
    )

    # Fuer den Energy Manager und Dashboard in der Ramdisk ablegen.
    # Atomic write verhindert, dass der Storage Simulator ein halb geschriebenes
    # ml_prediction.json liest und dann auf den konservativen Fallback springt.
    def _timeline_values(field):
        return sorted({
            str(slot.get(field))
            for slot in timeline
            if slot.get(field) not in (None, "")
        })

    history_profile_meta = None
    if empirical_profile:
        history_profile_meta = {
            key: empirical_profile.get(key)
            for key in (
                "sample_count",
                "home_positive_samples",
                "wp_positive_samples",
                "first_date",
                "last_date",
                "age_days",
                "recent_days",
            )
        }

    prediction_payload = {
        "schema_version": ML_PREDICTION_SCHEMA_VERSION,
        "ts":         datetime.datetime.now().isoformat(),
        "forecast_mode": forecast_mode,
        "model_ready": bool(model_ready),
        "model_reason": model_reason or None,
        "history_profile": history_profile_meta,
        "consumer_sources": {
            "home": {
                "sources": _timeline_values("home_source"),
                "quality": _timeline_values("home_quality"),
            },
            "wp": {
                "sources": _timeline_values("wp_source"),
                "quality": _timeline_values("wp_quality"),
            },
            "climate": {
                "sources": _timeline_values("climate_source"),
                "quality": _timeline_values("climate_quality"),
            },
            "wallbox": {
                "sources": ["excluded_dynamic_without_explicit_plan"],
                "quality": ["not_applicable"],
            },
            "heating_element": {
                "sources": ["included_in_home_or_explicit_planned_load"],
                "quality": ["no_dedicated_history_series"],
            },
            "domestic_hot_water_heatpump": {
                "sources": ["included_in_home_or_explicit_planned_load"],
                "quality": ["no_dedicated_history_series"],
            },
        },
        "home_kwh":   round(total_home_today, 2),
        "wp_kwh":     round(total_wp_today,   2),
        "climate_kwh": round(total_climate_today, 2),
        "climate_forecast": {
            "enabled": bool(climate_profile.get("enabled", False)),
            "reason": climate_profile.get("reason", ""),
            "source": climate_profile.get("source", ""),
            "samples": int(climate_profile.get("samples", 0) or 0),
            "active_samples": int(climate_profile.get("active_samples", 0) or 0),
            "activation_temp_c": climate_profile.get("activation_temp_c"),
            "avg_active_kw": climate_profile.get("avg_active_kw"),
        },
        "temp_source": "weather_forecast" if slots_with_forecast > slots_with_fallback else "fallback",
        "bias_home":  round(bias_home, 3),
        "bias_wp":    round(bias_wp,   3),
        "sanity_home_factor": round(sanity_factor, 3),
        "sanity_home_baseline_kwh": round(sanity_baseline, 2) if sanity_baseline else None,
        "sanity_wp_factor": round(sanity_wp_factor, 3),
        "sanity_wp_baseline_kwh": round(sanity_wp_baseline, 2) if sanity_wp_baseline is not None else None,
        "timeline":   timeline
    }
    os.makedirs(os.path.dirname(PREDICTION_PATH), exist_ok=True)
    tmp_prediction_path = PREDICTION_PATH + ".tmp"
    with open(tmp_prediction_path, 'w', encoding='utf-8') as f:
        json.dump(prediction_payload, f)
    os.replace(tmp_prediction_path, PREDICTION_PATH)

    _set_shared_web_file(PREDICTION_PATH)
    return True

def analyze_data():
    if not os.path.exists(DB_PATH):
        print("Keine Datenbank gefunden.")
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    print("\n=== Tageswerte aus der Datenbank (daily_stats) ===")
    print("Tageszaehler aus dem Live-System (Bereinigt)", flush=True)
    print(f"{'Datum':<12} | {'Haus (kWh)':<10} | {'WP (kWh)':<10} | {'Wallbox (kWh)':<12}")
    print("-" * 55)
    c.execute("SELECT date, home_consumption, wp_consumption, wb_consumption FROM daily_stats ORDER BY date DESC LIMIT 14")
    for row in c.fetchall():
        print(f"{row[0]:<12} | {row[1] or 0:<10.2f} | {row[2] or 0:<10.2f} | {row[3] or 0:<12.2f}")

    print("\n=== ML-Trainingsdaten (Summiert aus Ertrag.X.txt) ===")
    print("Rohe C++ Logdateien von Eba-M (Trainingsgrundlage der KI)")
    print(f"{'Datum':<12} | {'Haus (kWh)':<10} | {'WP (kWh)':<10}")
    print("-" * 55)
    c.execute("SELECT date, MAX(home_kwh_cum), MAX(wp_kwh_cum) FROM ml_training_data GROUP BY date ORDER BY date DESC LIMIT 14")
    for row in c.fetchall():
        print(f"{row[0]:<12} | {row[1] or 0:<10.2f} | {row[2] or 0:<10.2f}")
    conn.close()
    print()

if __name__ == "__main__":
    if "--train" in sys.argv:
        raise SystemExit(0 if train_model() else 1)
    elif "--predict" in sys.argv:
        raise SystemExit(0 if predict_today() else 1)
    elif "--model-ready" in sys.argv:
        raise SystemExit(0 if ml_model_is_ready(MODEL_PATH) else 1)
    elif "--analyze" in sys.argv:
        analyze_data()
