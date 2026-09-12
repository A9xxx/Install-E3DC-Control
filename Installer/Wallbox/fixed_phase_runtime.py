"""Privater Sitzungsadapter für feste Wallboxen, ohne Hardwareausgänge."""

import copy
import hashlib
import json
import math
import os
import stat
import time
import uuid

from .fixed_phase_session import STATE_SCHEMA, update_fixed_phase_session


CHECKPOINT_FILE = "fixed_phase_control.json"
CHECKPOINT_SCHEMA = "fixed_phase_control_checkpoint_v1"
MAX_BYTES = 1024 * 1024
HEARTBEAT_S = 60.0


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_checkpoint_key")
        result[key] = value
    return result


def _directory_fd(directory):
    path = os.path.abspath(os.fspath(directory))
    if path == os.sep or os.geteuid() == 33:
        raise ValueError("private_directory_invalid")
    parts = [part for part in path.split(os.sep) if part]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(os.sep, flags)
    try:
        for index, part in enumerate(parts):
            leaf = index == len(parts) - 1
            if leaf:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            try:
                info = os.fstat(child)
                mode = stat.S_IMODE(info.st_mode)
                if info.st_uid not in (0, os.geteuid()):
                    raise ValueError("directory_owner_invalid")
                if leaf:
                    if info.st_uid != os.geteuid() or mode != 0o700:
                        raise ValueError("private_directory_mode_invalid")
                elif mode & 0o022 and not (info.st_uid == 0 and mode & stat.S_ISVTX):
                    raise ValueError("directory_parent_writable")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _file_token(info):
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def _validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"sessions", "energy_state"}:
        raise ValueError("checkpoint_payload_invalid")
    if not isinstance(payload["sessions"], dict) or not isinstance(payload["energy_state"], dict):
        raise ValueError("checkpoint_maps_invalid")
    if len(payload["sessions"]) > 64:
        raise ValueError("checkpoint_session_limit")
    for key, entry in payload["sessions"].items():
        if not str(key).isdigit() or not isinstance(entry, dict):
            raise ValueError("checkpoint_session_invalid")
        binding = entry.get("device_binding")
        sid = entry.get("session_id")
        if (not isinstance(binding, str) or len(binding) != 64
                or any(char not in "0123456789abcdef" for char in binding)
                or not isinstance(sid, str) or not 0 < len(sid) <= 128):
            raise ValueError("checkpoint_binding_invalid")
        state = entry.get("phase_state")
        if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA or state.get("session_id") != sid:
            raise ValueError("checkpoint_phase_state_invalid")
        phases = state.get("confirmed_phases")
        if type(phases) is not int or phases not in (0, 1, 2, 3):
            raise ValueError("checkpoint_phase_count_invalid")
        mask = state.get("confirmed_mask")
        if not isinstance(mask, list) or len(mask) != phases or len(set(mask)) != phases or any(type(value) is not int or value not in (1, 2, 3) for value in mask):
            raise ValueError("checkpoint_phase_mask_invalid")
    _canonical(payload)
    return payload


def _read(directory_fd):
    fd = os.open(CHECKPOINT_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or not 0 < info.st_size <= MAX_BYTES):
            raise ValueError("checkpoint_file_unsafe")
        chunks = []
        length = 0
        while True:
            data = os.read(fd, min(65536, MAX_BYTES + 1 - length))
            if not data:
                break
            chunks.append(data)
            length += len(data)
            if length > MAX_BYTES:
                raise ValueError("checkpoint_too_large")
        value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or value.get("schema") != CHECKPOINT_SCHEMA:
            raise ValueError("checkpoint_schema_invalid")
        payload = _validate_payload(value.get("payload"))
        if value.get("sha256") != hashlib.sha256(_canonical(payload)).hexdigest():
            raise ValueError("checkpoint_checksum_invalid")
        return payload, _file_token(info)
    finally:
        os.close(fd)


def generic_device_binding(box, config):
    """Binde Hardware und ausdrückliche Fahrzeugzuordnung ohne Klartextadresse."""
    cid = int(box.get("id", 0) or 0)
    charger = box.get("charger")
    suffix = "" if cid == 1 else str(cid)
    cfg = config if isinstance(config, dict) else {}
    source = {
        "id": cid,
        "class": charger.__class__.__name__ if charger is not None else str(box.get("_charger_class_name") or ""),
        "type": cfg.get("wb_native_type" + suffix),
        "ip": cfg.get("wb_native_ip" + suffix, getattr(charger, "ip", "")),
        "server_ip": cfg.get("server_ip", getattr(charger, "server_ip", "")),
        "wb_index": getattr(charger, "wb_index", None),
        "device_family": cfg.get("wb%d_e3dc_device_family" % cid, cfg.get("e3dc_device_family")),
        "vehicle": {key: cfg.get("wb%d_%s" % (cid, key)) for key in ("car_id", "vehicle_id", "selected_car_id", "car_profile", "obc_max_phases")},
    }
    return hashlib.sha256(_canonical(source)).hexdigest()


def _fresh(status):
    return bool(status.get("driver_status_valid") is True
                and status.get("driver_status_stale") is not True
                and status.get("driver_status_degraded") is not True
                and status.get("driver_status_glitch") is not True
                and status.get("driver_status_plausible") is not False
                and status.get("valid") is not False
                and status.get("stale") is not True)


def explicit_connected(status):
    """Fehlende oder widersprüchliche Steckerdaten bleiben unbekannt."""
    values = [status[key] for key in ("car_connected_rscp", "plug_state", "connected")
              if type(status.get(key)) is bool]
    if values:
        return values[0] if all(value == values[0] for value in values) else None
    return None


def _sample_id(status):
    seq = status.get("native_status_sample_seq")
    ts = status.get("native_status_sample_ts")
    if type(seq) in (int, float) and math.isfinite(seq) and seq > 0 and type(ts) in (int, float) and math.isfinite(ts) and ts > 0:
        return "native:%s:%s" % (seq, ts)
    ts = status.get("driver_status_last_sample_ts")
    if type(ts) in (int, float) and math.isfinite(ts) and ts > 0:
        return "driver:%s" % ts
    return None


def _eligible(box, status):
    charger = box.get("charger")
    name = charger.__class__.__name__ if charger is not None else str(box.get("_charger_class_name") or "")
    family = str(status.get("e3dc_device_family") or getattr(charger, "device_family", "") or "").lower()
    return bool(name in {"E3DCCharger", "E3DCMultiConnectCharger"}
                and family != "efy" and status.get("can_switch_phases") is not True
                and status.get("autonomous_phase_switch_capable") is not True
                # Lesender ALG-/Phasenstatus ist keine Python-Stromfreigabe.
                # Statusbeleg und tatsächlich gebundener Treiber müssen den
                # ausdrücklich gewählten flüchtigen WBchar6-Pfad bestätigen.
                and status.get("e3dc_control_backend") == "wbchar6_compat"
                and status.get("e3dc_wbchar6_compat_explicit") is True
                and getattr(charger, "control_backend", None) == "wbchar6_compat"
                and getattr(charger, "wbchar6_compat_explicit", None) is True)


def _six_amp_learning_allowed(box, config, cid):
    if box.get("_fixed_phase_learning_enabled", True) is not True:
        return False
    cfg = config if isinstance(config, dict) else {}
    configured = (cfg.get("wb%d_min_amp" % cid) or cfg.get("wb%d_minladestrom" % cid)
                  or cfg.get("wbminladestrom") or cfg.get("wb_min_amp") or 6)
    # Ein älterer Cache darf ein inzwischen höheres Minimum nicht unterschreiten.
    for value in (configured, box.get("_charger_min_amp", 6)):
        try:
            number = float(str(value).strip().replace(",", "."))
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(number) or number > 6.0:
            return False
    return True


class FixedPhaseRuntime:
    """Ein Writer für Sitzungsbelege und das vom Manager gelieferte Energiekonto."""

    def __init__(self, private_directory_callback):
        self._directory_callback = private_directory_callback
        self.sessions = {}
        self.energy_state = {}
        self.valid = True
        self.reason = "not_loaded"
        self._loaded = False
        self._token = None
        self._last_signature = None
        self._last_saved_s = None
        self._restore_pending = set()

    def _signature(self):
        return hashlib.sha256(_canonical({key: {
            "binding": entry.get("device_binding"), "session_id": entry.get("session_id"),
            "retired": entry.get("retired", False),
            "confirmed_phases": (entry.get("phase_state") or {}).get("confirmed_phases"),
            "confirmed_mask": (entry.get("phase_state") or {}).get("confirmed_mask"),
            "measurement_conflict": (entry.get("phase_state") or {}).get("measurement_conflict"),
        } for key, entry in self.sessions.items()})).hexdigest()

    def load(self):
        if self._loaded:
            return self.valid
        self._loaded = True
        directory_fd = None
        try:
            directory_fd = _directory_fd(self._directory_callback())
            try:
                payload, self._token = _read(directory_fd)
            except FileNotFoundError:
                self.reason = "checkpoint_missing_initial_state"
                return True
            self.sessions = copy.deepcopy(payload["sessions"])
            self.energy_state = copy.deepcopy(payload["energy_state"])
            for entry in self.sessions.values():
                # Während eines Prozessstillstands kann das Fahrzeug gewechselt
                # haben. Ohne Hardware-Steckepisoden-ID ist die alte Messung
                # kein Nachweis für die jetzt angeschlossene Fahrzeugobergrenze.
                previous = entry["phase_state"]
                entry["phase_state"] = {
                    "schema": STATE_SCHEMA, "session_id": entry["session_id"],
                    "confirmed_phases": 0, "confirmed_mask": [],
                    "disconnected": previous.get("disconnected") is True,
                    "measurement_conflict": False,
                }
            self._restore_pending = set(self.sessions)
            self._last_signature = self._signature()
            self.reason = "checkpoint_restored_waiting_for_fresh_connection"
            return True
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            self.valid = False
            self.reason = "checkpoint_untrusted"
            return False
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    def save(self, *, force=False, now_s=None):
        """Kanten sofort, sonst alle 60 s; Reservezusagen brauchen force=True."""
        if not self.load():
            return False
        directory_fd = None
        temporary = None
        try:
            now = time.monotonic() if now_s is None else float(now_s)
            if not math.isfinite(now) or now < 0:
                raise ValueError("checkpoint_clock_invalid")
            signature = self._signature()
            if (not force and self._token is not None and signature == self._last_signature
                    and self._last_saved_s is not None and 0 <= now - self._last_saved_s < HEARTBEAT_S):
                return True
            payload = _validate_payload({"sessions": self.sessions, "energy_state": self.energy_state})
            envelope = {"schema": CHECKPOINT_SCHEMA, "payload": payload,
                        "sha256": hashlib.sha256(_canonical(payload)).hexdigest()}
            raw = _canonical(envelope)
            if len(raw) > MAX_BYTES:
                raise ValueError("checkpoint_too_large")
            directory_fd = _directory_fd(self._directory_callback())
            try:
                _, current_token = _read(directory_fd)
            except FileNotFoundError:
                current_token = None
            if current_token != self._token:
                raise ValueError("checkpoint_changed_outside_owner")
            temporary = ".fixed-phase-%s.tmp" % uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory_fd)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb", closefd=False) as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, CHECKPOINT_FILE, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            temporary = None
            os.fsync(directory_fd)
            _, self._token = _read(directory_fd)
            self._last_signature = signature
            self._last_saved_s = now
            self.reason = "checkpoint_saved"
            return True
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            self.valid = False
            self.reason = "checkpoint_write_failed"
            return False
        finally:
            if directory_fd is not None:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except OSError:
                        pass
                os.close(directory_fd)

    def observe(self, boxes, statuslist, config, now_s):
        self.load()
        statuses = {int(item.get("id", 0) or 0): item.get("status") or {} for item in statuslist or [] if isinstance(item, dict)}
        contracts = {}
        for box in boxes or []:
            cid = int(box.get("id", 0) or 0)
            key = str(cid)
            status = statuses.get(cid, {})
            box.pop("_fixed_phase_session_contract", None)
            box.pop("_fixed_phase_device_binding", None)
            if cid <= 0 or not self.valid or not _eligible(box, status):
                continue
            fresh = _fresh(status) and _sample_id(status) is not None
            connected = explicit_connected(status) if fresh else None
            binding = generic_device_binding(box, config)
            box["_fixed_phase_device_binding"] = binding
            entry = self.sessions.get(key)
            if not isinstance(entry, dict) or entry.get("device_binding") != binding or entry.get("retired") is True:
                if connected is not True:
                    continue
                entry = {"device_binding": binding, "session_id": uuid.uuid4().hex, "phase_state": {}}
                self.sessions[key] = entry
                self._restore_pending.discard(key)
            restored_ready = key not in self._restore_pending
            if connected is True:
                self._restore_pending.discard(key)
                restored_ready = True
            learning_allowed = _six_amp_learning_allowed(box, config, cid)
            result = update_fixed_phase_session(
                entry.get("phase_state"), status=status, now_s=now_s,
                session_id=entry["session_id"], enabled=True, connected=connected,
                commanded_amp=box.get("current_set_amp") if learning_allowed else None,
                sample_id=_sample_id(status), evse_max_phases=3,
                vehicle_max_phases=box.get("_fixed_phase_vehicle_max_phases", 0),
            )
            entry["phase_state"] = result["state"]
            entry["retired"] = result["disconnect_confirmed"]
            contract = dict(result["phase_contract"], cycle_token=box.get("_wallbox_cycle_token"),
                            probe_required=result["probe_required"], persistence_valid=self.valid,
                            restored_connection_confirmed=restored_ready,
                            device_binding=binding)
            if not learning_allowed:
                contract["active"] = False
                contract["probe_required"] = False
                contract["learning_current_limit_active"] = False
                contract["reason"] = "six_amp_learning_disabled"
            elif not restored_ready:
                contract["active"] = False
                contract["probe_required"] = False
                contract["learning_current_limit_active"] = False
                contract["reason"] = "restore_waiting_for_fresh_connection"
            box["_fixed_phase_session_id"] = entry["session_id"]
            box["_fixed_phase_session_contract"] = contract
            contracts[cid] = contract
        if self.valid and not self.save(now_s=now_s):
            for contract in contracts.values():
                contract.update(active=False, probe_required=False, learning_current_limit_active=False,
                                persistence_valid=False, reason=self.reason)
        return contracts
