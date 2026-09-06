"""Fassade für Fahrzeugprofile, SoC und Statusanreicherung.

``VehicleSocTracker`` besitzt die profilgebundene SoC-Schätzung und Persistenz;
die direkte openWB-Pro-Schätzung verbleibt im Treiber. Fehlt ein brauchbarer
SoC beim Anstecken, kann dieses Modul den vorhandenen Cloud-Dienst einmalig
beauftragen. Ladefreigabe, Phasen- und Stromregelung bleiben davon unberührt.
"""

import json
import os
import tempfile
import time

from . import decision as wallbox_decision
from . import soc_tracker as wallbox_soc_tracker
from .modes import MODE_OFF, normalize_wb_mode


VehicleSocTracker = wallbox_soc_tracker.VehicleSocTracker


def request_missing_openwb_cloud_soc(wb_id, config, status, soc_info=None, now=None):
    """Fordere fehlenden SoC einmal pro Stecksession beim Cloud-Dienst an.

    Hier findet weder ein Cloud-Aufruf noch ein Fahrzeug-/Wallbox-Befehl statt.
    Die vorhandene Force-Datei bleibt alleiniger Auftrag an den Bluelink-Dienst.
    """
    now = time.time() if now is None else float(now)
    config, status = config or {}, status or {}
    if str(config.get("wb_native_enable", "0")).lower() not in ("1", "true"):
        return False
    legacy_mode = wallbox_soc_tracker._safe_float(config.get("wb_native_mode"), 0)
    configured_mode = config.get(f"wb{wb_id}_mode")
    if configured_mode in (None, ""):
        configured_mode = legacy_mode if legacy_mode > 2 else MODE_OFF
    if (
        normalize_wb_mode(configured_mode) == MODE_OFF
        or wallbox_soc_tracker._contract_flag_active(config.get(f"wb{wb_id}_locked"))
        or wallbox_soc_tracker._contract_flag_active(config.get(f"wb{wb_id}_manual_pause"))
        or not str(config.get("bluelink_refresh_token") or "").strip()
        or status.get("driver_status_valid") is not True
        or status.get("plug_state") is not True
        or wallbox_soc_tracker._soc_record_vetoed(status)
        or not wallbox_soc_tracker._fresh_timestamp(
            status.get("driver_status_last_sample_ts"), now,
            wallbox_soc_tracker.OPENWB_PRO_STATUS_MAX_AGE_S,
        )
        or wallbox_soc_tracker._openwb_pro_direct_soc_fresh(status, now=now)
        or (isinstance(soc_info, dict) and soc_info.get("soc_rule_confirmed") is True)
    ):
        return False
    session_id = str(status.get("plug_session_id") or "").strip()
    if not wallbox_soc_tracker._plug_session_started_ts(session_id, now):
        return False
    driver_session_start = wallbox_soc_tracker._timestamp(status.get("_session_start_ts"), 0)
    if driver_session_start <= 0 or driver_session_start > now + 300:
        return False
    selected_id = str(config.get(f"wb{wb_id}_car_id") or "").strip()
    profile = wallbox_soc_tracker._unique_saved_profile(selected_id)
    if (
        not profile or not str(profile.get("cloud_vehicle_id") or "").strip()
        or not wallbox_soc_tracker._configured_vehicle_binding_unique(config, wb_id, selected_id)
    ):
        return False
    aliases = wallbox_soc_tracker._compact_aliases(profile)
    if any(
        wallbox_soc_tracker._compact_id(status.get(key))
        and wallbox_soc_tracker._compact_id(status.get(key)) not in aliases
        for key in wallbox_soc_tracker.LIVE_STATUS_ID_KEYS
    ):
        return False
    base = wallbox_soc_tracker.RAMDISK_DIR
    state_path = os.path.join(base, "vehicle_soc_cloud_refresh.json")
    state = wallbox_soc_tracker._read_json(state_path, {})
    state = state if isinstance(state, dict) else {}
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    if sessions.get(str(wb_id)) == session_id:
        return False
    cloud = wallbox_soc_tracker._read_json(os.path.join(base, "vehicles.json"), {})
    refresh = cloud.get("refresh", {}) if isinstance(cloud, dict) else {}
    refresh = refresh if isinstance(refresh, dict) else {}
    last_force = wallbox_soc_tracker._timestamp(refresh.get("attempt_ts"), 0) if refresh.get("mode") == "force" else 0
    last_request = max(wallbox_soc_tracker._timestamp(state.get("last_request_ts"), 0), last_force)
    interval_s = max(5.0, wallbox_soc_tracker._safe_float(config.get("bluelink_interval"), 15)) * 60
    if last_request > 0 and now - last_request < interval_s:
        return False
    flag_path = os.path.join(base, "force_bluelink.flag")
    temporary = None
    try:
        # Vollständig veröffentlichen, ohne einen parallelen manuellen
        # Auftrag zu überschreiben oder einen vorhandenen Symlink zu öffnen.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=base,
                                         prefix=".force_bluelink.", delete=False) as handle:
            temporary = handle.name
            os.fchmod(handle.fileno(), 0o664)
            json.dump({
                "schema": "vehicle_soc_refresh_request_v1",
                "source": "openwb_plug",
                "vehicle_id": str(profile["cloud_vehicle_id"]).strip(),
                "wb": int(wb_id), "plug_session_id": session_id,
                "driver_session_start_ts": driver_session_start,
                "requested_at": int(now),
            }, handle, separators=(",", ":"))
            handle.flush()
            try:
                os.link(temporary, flag_path)
            except FileExistsError:
                # Der bestehende Auftrag kann zu einer anderen Wallbox
                # gehören. Unsere Session ist dann noch nicht beauftragt.
                return False
        sessions[str(wb_id)] = session_id
        wallbox_soc_tracker._write_json_atomic(state_path, {
            "sessions": sessions, "last_request_ts": now,
        })
        return True
    except OSError as error:
        wallbox_soc_tracker.logger.debug("Cloud-SoC-Anforderung nicht möglich: %s", error)
        return False
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def compact_vehicle_identifier(value):
    """Delegiere die bestehende Normalisierung der Fahrzeugkennung."""

    return wallbox_decision.compact_vehicle_identifier(value)


def load_saved_car_profiles():
    """Load saved vehicle profiles using the manager's existing cache rules."""

    path = wallbox_soc_tracker.SAVED_CARS_FILE
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        load_saved_car_profiles._cache = (0.0, [])
        return []
    cached_mtime, cached_profiles = getattr(
        load_saved_car_profiles,
        "_cache",
        (None, None),
    )
    if cached_mtime == mtime and isinstance(cached_profiles, list):
        return cached_profiles
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        profiles = data if isinstance(data, list) else data.get("vehicles", [])
        if not isinstance(profiles, list):
            profiles = []
    except Exception:
        profiles = []
    load_saved_car_profiles._cache = (mtime, profiles)
    return profiles


def resolve_vehicle_profile(config, wb_id, selected_id, fallback_name=""):
    """Delegate to the profile contract already used by ``VehicleSocTracker``."""

    return wallbox_soc_tracker._profile_for(
        config,
        wb_id,
        selected_id,
        fallback_name=fallback_name,
    )


def manual_soc_vehicle_identity(
    charger_id,
    max_age_s=12 * 3600,
    *,
    ramdisk_dir=None,
    now_ts=None,
):
    """Liefere SoC-/Profilmetadaten, niemals eine Stecksession-Bestätigung."""

    try:
        cid = int(charger_id or 1)
    except (TypeError, ValueError):
        cid = 1
    base_dir = wallbox_soc_tracker.RAMDISK_DIR if ramdisk_dir is None else ramdisk_dir
    path = os.path.join(base_dir, f"manual_soc_wb{cid}.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {}
        if int(data.get("wb", cid) or cid) != cid:
            return {}
        if data.get("plugged") is False:
            return {}
        ts = float(data.get("ts", 0.0) or 0.0)
        current_ts = time.time() if now_ts is None else float(now_ts)
        if ts <= 0.0 or current_ts - ts > float(max_age_s):
            return {}
        return {
            key: data.get(key)
            for key in ("car_id", "vehicle_id", "rfid_tag", "name")
            if str(data.get(key) or "").strip()
        }
    except Exception:
        return {}


def session_vehicle_key_from_status(
    charger_id,
    status=None,
    *,
    manual_identity=None,
    manual_identity_loader=None,
    ramdisk_dir=None,
    now_ts=None,
):
    """Liefere nur den bestätigten Schlüssel der aktuellen Stecksession.

    Die alten Manual-/SoC-Parameter bleiben vorerst Teil der kompatiblen
    Aufruffläche, werden aber absichtlich nicht mehr als Identitätsbeweis
    verwendet.
    """

    _ = (
        charger_id,
        manual_identity,
        manual_identity_loader,
        ramdisk_dir,
        now_ts,
    )
    identity = wallbox_decision.confirmed_session_vehicle_identity(status)
    return str(identity.get("key") or "")


def confirmed_session_vehicle_identity(status=None):
    """Delegiere den reinen Sitzungsidentitätsvertrag."""

    return wallbox_decision.confirmed_session_vehicle_identity(status)


def vehicle_max_ac_phases(config, charger_id, status=None, profiles=None):
    """Delegate phase-capability evaluation to the existing pure decision helper."""

    profile_list = load_saved_car_profiles() if profiles is None else profiles
    return wallbox_decision.vehicle_max_ac_phases_from_profiles(
        config,
        charger_id,
        profile_list,
        status,
    )


def vehicle_phase_capability(status=None, profiles=None, config=None, charger_id=1):
    """Liefere den expliziten, sitzungsgebundenen Fahrzeug-Phasenbeleg."""

    profile_list = load_saved_car_profiles() if profiles is None else profiles
    return wallbox_decision.vehicle_phase_capability_from_profiles(
        profile_list,
        status,
        config=config,
        charger_id=charger_id,
    )


def vehicle_control_pilot_interruption_capability(
    status=None,
    profiles=None,
    config=None,
    charger_id=1,
):
    """Liefere die live- oder Ladepunktprofil-gebundene CP-Anforderung."""

    profile_list = load_saved_car_profiles() if profiles is None else profiles
    return wallbox_decision.vehicle_control_pilot_interruption_capability_from_profiles(
        profile_list,
        status,
        config=config,
        charger_id=charger_id,
    )


def vehicle_max_ac_power_kw(config, charger_id, status=None, profiles=None):
    """Liefere nur eine profil- oder OBC-gebundene AC-Leistungsgrenze."""

    profile_list = load_saved_car_profiles() if profiles is None else profiles
    return wallbox_decision.vehicle_max_ac_power_kw_from_profiles(
        config,
        charger_id,
        profile_list,
        status,
    )


def vehicle_current_capability(
    charger_id,
    phase_count,
    status=None,
    profiles=None,
):
    """Liefere den bestätigten phasenabhängigen Fahrzeug-Stromdeckel."""

    _ = charger_id
    profile_list = load_saved_car_profiles() if profiles is None else profiles
    return wallbox_decision.vehicle_current_capability_from_profiles(
        profile_list,
        status,
        phase_count=phase_count,
    )


class VehicleManager:
    """Facade composing the established tracker, profile and identity APIs."""

    def __init__(self, soc_tracker=None):
        self._soc_tracker = (
            soc_tracker
            if soc_tracker is not None
            else VehicleSocTracker()
        )

    @property
    def soc_tracker(self):
        return self._soc_tracker

    def update(self, wb_id, config, status, charger_class=""):
        """Return the existing tracker status patch without altering it."""

        return self._soc_tracker.update(
            wb_id,
            config,
            status,
            charger_class=charger_class,
        )

    def update_status(
        self,
        wb_id,
        config,
        status,
        charger_class="",
        write_status=None,
    ):
        """Wende den Tracker-Patch an, ohne aktuelle Treiber-Reichweiten zu verdrängen."""

        explicit_total_range = wallbox_soc_tracker.current_explicit_openwb_total_range(
            status,
        )
        explicit_charged_range = wallbox_soc_tracker.current_explicit_openwb_charged_range(
            status,
        )
        # Eine verworfene explizite Reichweite darf auch dann nicht sichtbar
        # bleiben, wenn ohne Profil kein rechnerischer Fallback möglich ist.
        reported_total_source = str(status.get("car_range_source") or "").strip().lower()
        invalid_total_range = bool(
            status.get("car_range_valid") is True
            and reported_total_source in wallbox_soc_tracker.OPENWB_EXPLICIT_TOTAL_RANGE_SOURCES
            and explicit_total_range is None
        )
        if invalid_total_range:
            status.update({
                "car_range": 0.0,
                "range_km": 0.0,
                "car_range_source": "",
                "car_range_valid": False,
                "car_range_observed_ts": 0,
                "car_range_source_ts": None,
                "car_range_source_ts_explicit": False,
                "car_range_vehicle_key": "",
            })
        reported_charged_source = str(status.get("car_charged_range_source") or "").strip().lower()
        invalid_charged_range = bool(
            status.get("car_charged_range_valid") is True
            and reported_charged_source in wallbox_soc_tracker.OPENWB_EXPLICIT_CHARGED_RANGE_SOURCES
            and explicit_charged_range is None
        )
        if invalid_charged_range:
            status.update({
                "car_charged_range": 0.0,
                "charged_range_km": 0.0,
                "car_charged_range_source": "",
                "car_charged_range_valid": False,
                "car_charged_range_observed_ts": 0,
                "car_charged_range_source_ts": None,
                "car_charged_range_source_ts_explicit": False,
                "car_charged_range_vehicle_key": "",
            })
        soc_info = self.update(
            wb_id,
            config,
            status,
            charger_class=charger_class,
        )
        if str(charger_class or "") == "OpenWBProCharger":
            request_missing_openwb_cloud_soc(wb_id, config, status, soc_info=soc_info)
        if not soc_info:
            if (invalid_total_range or invalid_charged_range) and write_status is not None:
                try:
                    write_status()
                except Exception:
                    pass
            return soc_info
        status["car_soc"] = soc_info.get("soc", status.get("car_soc", 0))
        status["car_soc_source"] = soc_info.get(
            "source",
            status.get("car_soc_source", ""),
        )
        status["car_soc_raw_ts"] = soc_info.get(
            "raw_soc_ts",
            status.get("car_soc_raw_ts"),
        )
        status["car_soc_source_ts"] = soc_info.get(
            "soc_source_ts",
            soc_info.get("raw_soc_ts", status.get("car_soc_source_ts")),
        )
        # Regelwahrheit ist ein strikt typisierter Vertrag.  Werte wie
        # ``"false"`` oder ``1`` dürfen hier nicht durch Python-Truthiness zu
        # einer Bestätigung werden.
        status["car_soc_rule_confirmed"] = (
            soc_info.get("soc_rule_confirmed") is True
        )
        age_contract = wallbox_soc_tracker.vehicle_soc_age_contract(
            status.get("car_soc_source"), config,
        ) or {}
        status["car_soc_age_contract"] = age_contract.get("schema_version")
        status["car_soc_age_contract_source"] = age_contract.get("source")
        status["car_soc_max_age_s"] = age_contract.get("max_age_s")
        # Profil-/Cloud-/SoC-Zuordnung bleibt ein eigener Diagnosebereich.
        # Sie darf die vom Treiber gelieferte aktuelle Stecksession-ID nicht
        # ersetzen und damit keinen OBC-Hardcap aktivieren.
        status["car_soc_profile_id"] = soc_info.get(
            "profile_id",
            soc_info.get("car_id", ""),
        )
        status["car_soc_vehicle_id"] = soc_info.get("vehicle_id", "")
        status["car_soc_profile_bound"] = (
            soc_info.get("soc_profile_bound") is True
        )
        status["car_soc_identity_scope"] = str(
            soc_info.get("identity_scope", "soc_profile_only") or "soc_profile_only"
        )
        status["car_name"] = soc_info.get("name", status.get("car_name", ""))
        status["car_capacity_kwh"] = soc_info.get(
            "capacity",
            status.get("car_capacity_kwh", 0.0),
        )
        candidate_range = float(soc_info.get("range_km") or 0.0)
        candidate_range_source = str(soc_info.get("range_source") or "wallbox_estimated_consumption")
        if explicit_total_range:
            range_info = explicit_total_range
        elif candidate_range > 0.0:
            range_info = {
                "range_km": candidate_range,
                "range_source": candidate_range_source,
                "range_observed_ts": soc_info.get("range_observed_ts", 0),
                "range_source_ts": soc_info.get("range_source_ts"),
                "range_source_ts_explicit": bool(
                    soc_info.get("range_source_ts_explicit", False)
                ),
                "range_vehicle_key": soc_info.get("range_vehicle_key", ""),
                "range_explicit": bool(soc_info.get("range_explicit", False)),
            }
        else:
            range_info = None
        if range_info:
            status["car_range"] = float(range_info["range_km"])
            status["range_km"] = status["car_range"]
            status["car_range_source"] = str(range_info["range_source"])
            status["car_range_valid"] = bool(range_info.get("range_explicit", False))
            status["car_range_observed_ts"] = int(range_info.get("range_observed_ts") or 0)
            status["car_range_source_ts"] = range_info.get("range_source_ts")
            status["car_range_source_ts_explicit"] = bool(
                range_info.get("range_source_ts_explicit", False)
            )
            status["car_range_vehicle_key"] = str(range_info.get("range_vehicle_key") or "")

        candidate_charged_present = "charged_range_km" in soc_info
        candidate_charged = float(soc_info.get("charged_range_km") or 0.0)
        candidate_charged_source = str(soc_info.get("charged_range_source") or "wallbox_estimated_consumption")
        if explicit_charged_range:
            charged_info = explicit_charged_range
        elif candidate_charged_present and candidate_charged >= 0.0:
            charged_info = {
                "range_km": candidate_charged,
                "range_source": candidate_charged_source,
                "range_observed_ts": soc_info.get("charged_range_observed_ts", 0),
                "range_source_ts": soc_info.get("charged_range_source_ts"),
                "range_source_ts_explicit": bool(
                    soc_info.get("charged_range_source_ts_explicit", False)
                ),
                "range_vehicle_key": soc_info.get("charged_range_vehicle_key", ""),
                "range_explicit": bool(soc_info.get("charged_range_explicit", False)),
            }
        else:
            charged_info = None
        if charged_info:
            status["car_charged_range"] = float(charged_info["range_km"])
            status["charged_range_km"] = status["car_charged_range"]
            status["car_charged_range_source"] = str(charged_info["range_source"])
            status["car_charged_range_valid"] = bool(charged_info.get("range_explicit", False))
            status["car_charged_range_observed_ts"] = int(charged_info.get("range_observed_ts") or 0)
            status["car_charged_range_source_ts"] = charged_info.get("range_source_ts")
            status["car_charged_range_source_ts_explicit"] = bool(
                charged_info.get("range_source_ts_explicit", False)
            )
            status["car_charged_range_vehicle_key"] = str(charged_info.get("range_vehicle_key") or "")
        if float(soc_info.get("consumption_kwh_100km") or 0.0) > 0.0:
            status["car_consumption_kwh_100km"] = float(
                soc_info.get("consumption_kwh_100km") or 0.0
            )
        if write_status is not None:
            try:
                write_status()
            except Exception:
                pass
        return soc_info

    @staticmethod
    def load_saved_car_profiles():
        return load_saved_car_profiles()

    @staticmethod
    def resolve_vehicle_profile(config, wb_id, selected_id, fallback_name=""):
        return resolve_vehicle_profile(
            config,
            wb_id,
            selected_id,
            fallback_name=fallback_name,
        )

    @staticmethod
    def compact_vehicle_identifier(value):
        return compact_vehicle_identifier(value)

    @staticmethod
    def manual_soc_vehicle_identity(
        charger_id,
        max_age_s=12 * 3600,
        *,
        ramdisk_dir=None,
        now_ts=None,
    ):
        return manual_soc_vehicle_identity(
            charger_id,
            max_age_s=max_age_s,
            ramdisk_dir=ramdisk_dir,
            now_ts=now_ts,
        )

    @staticmethod
    def session_vehicle_key_from_status(
        charger_id,
        status=None,
        *,
        manual_identity=None,
        manual_identity_loader=None,
        ramdisk_dir=None,
        now_ts=None,
    ):
        return session_vehicle_key_from_status(
            charger_id,
            status=status,
            manual_identity=manual_identity,
            manual_identity_loader=manual_identity_loader,
            ramdisk_dir=ramdisk_dir,
            now_ts=now_ts,
        )

    @staticmethod
    def vehicle_max_ac_phases(config, charger_id, status=None, profiles=None):
        return vehicle_max_ac_phases(
            config,
            charger_id,
            status=status,
            profiles=profiles,
        )

    @staticmethod
    def vehicle_phase_capability(status=None, profiles=None, config=None, charger_id=1):
        return vehicle_phase_capability(
            status=status,
            profiles=profiles,
            config=config,
            charger_id=charger_id,
        )

    @staticmethod
    def vehicle_control_pilot_interruption_capability(
        status=None,
        profiles=None,
        config=None,
        charger_id=1,
    ):
        return vehicle_control_pilot_interruption_capability(
            status=status,
            profiles=profiles,
            config=config,
            charger_id=charger_id,
        )

    @staticmethod
    def confirmed_session_vehicle_identity(status=None):
        return confirmed_session_vehicle_identity(status)

    @staticmethod
    def vehicle_max_ac_power_kw(config, charger_id, status=None, profiles=None):
        return vehicle_max_ac_power_kw(
            config,
            charger_id,
            status=status,
            profiles=profiles,
        )

    @staticmethod
    def vehicle_current_capability(
        charger_id,
        phase_count,
        status=None,
        profiles=None,
    ):
        return vehicle_current_capability(
            charger_id,
            phase_count,
            status=status,
            profiles=profiles,
        )


__all__ = [
    "VehicleManager",
    "VehicleSocTracker",
    "compact_vehicle_identifier",
    "confirmed_session_vehicle_identity",
    "load_saved_car_profiles",
    "manual_soc_vehicle_identity",
    "resolve_vehicle_profile",
    "session_vehicle_key_from_status",
    "vehicle_current_capability",
    "vehicle_phase_capability",
    "vehicle_max_ac_phases",
    "vehicle_max_ac_power_kw",
]
