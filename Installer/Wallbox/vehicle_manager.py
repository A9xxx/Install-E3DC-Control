"""Konservative Fassade fuer die bestehenden Fahrzeug-Vertraege.

``VehicleSocTracker`` bleibt alleiniger Besitzer der generischen SoC-Schaetzung
und ihrer Persistenz. Die openWB-Pro-Schaetzung verbleibt im Treiber. Dieses
Modul buendelt nur die bisherige Profil-, Identitaets- und Statusanreicherung,
ohne neue Fahrzeug- oder Regelungslogik einzufuehren.
"""

import json
import os
import time

from . import decision as wallbox_decision
from . import soc_tracker as wallbox_soc_tracker


VehicleSocTracker = wallbox_soc_tracker.VehicleSocTracker


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
    """Return the current manager's fresh per-wallbox identity fallback."""

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
    """Return the existing live-first, manual-fallback vehicle session key."""

    st = status or {}
    live_key = compact_vehicle_identifier(
        st.get("car_id")
        or st.get("vehicle_id")
        or st.get("rfid_tag")
    )
    if live_key:
        return live_key
    identity = manual_identity
    if identity is None:
        if callable(manual_identity_loader):
            identity = manual_identity_loader(charger_id)
        else:
            identity = manual_soc_vehicle_identity(
                charger_id,
                ramdisk_dir=ramdisk_dir,
                now_ts=now_ts,
            )
    return compact_vehicle_identifier(
        identity.get("car_id")
        or identity.get("vehicle_id")
        or identity.get("rfid_tag")
    )


def vehicle_max_ac_phases(config, charger_id, status=None, profiles=None):
    """Delegate phase-capability evaluation to the existing pure decision helper."""

    profile_list = load_saved_car_profiles() if profiles is None else profiles
    return wallbox_decision.vehicle_max_ac_phases_from_profiles(
        config,
        charger_id,
        profile_list,
        status,
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
        """Wende den bisherigen Tracker-Patch 1:1 auf den WB-Status an."""

        soc_info = self.update(
            wb_id,
            config,
            status,
            charger_class=charger_class,
        )
        if not soc_info:
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
        status["car_soc_rule_confirmed"] = bool(
            soc_info.get("soc_rule_confirmed", False)
        )
        status["car_id"] = soc_info.get("car_id", status.get("car_id"))
        status["vehicle_id"] = soc_info.get(
            "vehicle_id",
            status.get("vehicle_id"),
        )
        status["car_name"] = soc_info.get("name", status.get("car_name", ""))
        status["car_capacity_kwh"] = soc_info.get(
            "capacity",
            status.get("car_capacity_kwh", 0.0),
        )
        if float(soc_info.get("range_km") or 0.0) > 0.0:
            status["car_range"] = float(soc_info.get("range_km") or 0.0)
            status["range_km"] = status["car_range"]
            status["car_range_source"] = soc_info.get(
                "range_source",
                "wallbox_estimated_consumption",
            )
        if float(soc_info.get("charged_range_km") or 0.0) > 0.0:
            status["car_charged_range"] = float(
                soc_info.get("charged_range_km") or 0.0
            )
            status["charged_range_km"] = status["car_charged_range"]
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
    def vehicle_max_ac_power_kw(config, charger_id, status=None, profiles=None):
        return vehicle_max_ac_power_kw(
            config,
            charger_id,
            status=status,
            profiles=profiles,
        )


__all__ = [
    "VehicleManager",
    "VehicleSocTracker",
    "compact_vehicle_identifier",
    "load_saved_car_profiles",
    "manual_soc_vehicle_identity",
    "resolve_vehicle_profile",
    "session_vehicle_key_from_status",
    "vehicle_max_ac_phases",
    "vehicle_max_ac_power_kw",
]
