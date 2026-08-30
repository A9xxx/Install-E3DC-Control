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
