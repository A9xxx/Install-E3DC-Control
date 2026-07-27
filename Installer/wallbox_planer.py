"""
E3DC-Control Wallbox Planer - zentrale Ladeplanung.

Implementiert:
  - generate_native_charging_schedule(): EPEX-basierter 15-min Ladefahrplan mit
    integriertem Battery-Care AI-Algorithmus (aus ai_scheduler.py).
  - get_planned_charging_status(): Prueft ob aktuell ein Ladefenster aktiv ist.

Battery-Care Zwei-Phasen Strategie (inspiriert von Wallbox/ai_scheduler.py):
  - Phase 1 (Bulk):             ~85% des Ladebedarfs in den guenstigsten Slots.
  - Phase 2 (Pre-Conditioning): ~15% des Ladebedarfs kurz vor der Abfahrtszeit.
    -> Batterie ist zum Abfahrtszeitpunkt warm -> bessere Reichweite und Lebenszeit.
  - Just-In-Time Score:         Bei gleichem Preis werden spaetere ("naehere") Slots
    bevorzugt (aus ai_scheduler.py Battery-Care Algorithmus).
"""
import argparse
import hashlib
import os
import json
import math
import stat
import time
import datetime
import logging

try:
    from .Wallbox.config import (
        get_config,
        CONFIG_FILE,
        V4_CONFIG_FILE,
        RAMDISK_DIR,
        INSTALL_DIR,
        _configured_billing_price_now as configured_billing_price_for_timestamp,
    )
    from .config_secret_permissions import apply_config_secret_permissions
    from .tariff_schedule import (
        tariff_type as configured_tariff_type,
        uses_recurring_tariff_axis,
    )
except ImportError:  # Aufruf als Script-Modul direkt aus dem Installer-Verzeichnis
    from Wallbox.config import (
        get_config,
        CONFIG_FILE,
        V4_CONFIG_FILE,
        RAMDISK_DIR,
        INSTALL_DIR,
        _configured_billing_price_now as configured_billing_price_for_timestamp,
    )
    from config_secret_permissions import apply_config_secret_permissions
    from tariff_schedule import (
        tariff_type as configured_tariff_type,
        uses_recurring_tariff_axis,
    )

logger = logging.getLogger("WallboxManager.Planer")

# ---------------------------------------------------------------------------
# Battery-Care Konstanten
# ---------------------------------------------------------------------------
# Anteil des Gesamtladebedarfs, der als "Pre-Conditioning" kurz vor Abfahrt
# reserviert wird (Batterie vorwaermen fuer bessere Fahrreichweite).
PRE_CONDITION_PERCENT  = 15    # % des Gesamtladebedarfs
PRE_CONDITION_WINDOW_H = 1.5   # Zeitfenster vor Abfahrt (Stunden)
# Pre-Conditioning darf den Preisanker nicht überstimmen. Kleine Toleranz
# deckt Rundungsrauschen ab, verhindert aber Sprünge in den nächsten Tarifblock.
PRE_CONDITION_MAX_PRICE_PREMIUM_CT = 0.5

# JIT-Score: Wie stark "naehere" Slots bei gleichem Preis bevorzugt werden.
# 0.005 ct pro Stunde Abstand zur Abfahrt = sehr sanft, Preis hat klar Vorrang.
JIT_BONUS_CT_PER_H = 0.005

MISSING_SOC_WARNING_INTERVAL_S = 600
_last_missing_soc_warning_ts = 0.0
_CANDIDATE_MODE = False

_CANDIDATE_SCHEMA = "wallbox_plan_candidate_v1"
_CANDIDATE_RESULT_SCHEMA = "wallbox_plan_candidate_result_v1"
_CANDIDATE_REQUEST_FILE = "candidate_request.json"
_CANDIDATE_CONFIG_FILE = "candidate_config.json"
_CANDIDATE_RESULT_FILE = "planner_result.json"
_CANDIDATE_PLAN_FILES = (
    "native_wallbox_schedule_wb1.json",
    "native_wallbox_schedule_wb2.json",
    "native_wallbox_schedule.json",
)
_MAX_CANDIDATE_JSON_BYTES = 4 * 1024 * 1024

def _tariff_type(config):
    return configured_tariff_type(config)


def _uses_recurring_tariff_axis(config):
    """True only for tariffs whose daily prices are fully defined by config."""
    return uses_recurring_tariff_axis(config)


def _warn_missing_vehicle_soc(now=None):
    global _last_missing_soc_warning_ts
    if now is None:
        now = time.time()
    if now - _last_missing_soc_warning_ts < MISSING_SOC_WARNING_INTERVAL_S:
        return False
    _last_missing_soc_warning_ts = now
    logger.warning(
        "[Scheduler] Kein SoC bekannt - nehme 0% an (konservativ). "
        "Bitte manuellen SoC in der UI eintragen!"
    )
    return True


def _eco_slot_timestamp(entry):
    """Return a 15-minute slot timestamp in seconds for an eco/tariff entry."""
    raw = entry.get("start_timestamp")
    if raw is None:
        raw = entry.get("ts", entry.get("timestamp", 0))
    try:
        raw = float(raw)
    except Exception:
        return None
    # epex_manager stores milliseconds, older helpers used seconds.
    if raw > 10_000_000_000:
        raw = raw / 1000.0
    slot_ts = int(raw)
    return (slot_ts // 900) * 900


def _clamp_eco_dirty_score(value):
    """Normalize eco score to 0.0 clean .. 1.0 dirty."""
    try:
        value = float(value)
    except Exception:
        return None
    if value > 1.0:
        value = value / 100.0
    return max(0.0, min(1.0, value))


def _slot_price_ct(slot):
    try:
        price = float(slot.get("price_ct"))
    except Exception:
        return None
    return price if math.isfinite(price) else None


def _read_schedule_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _remove_schedule_file(path, wb_id=None, reason=""):
    if not os.path.exists(path):
        return
    try:
        os.remove(path)
        if reason:
            logger.info("[Scheduler] WB%s: %s", wb_id or "?", reason)
    except Exception as e:
        logger.warning("[Scheduler] WB%s: Schedule konnte nicht geloescht werden: %s", wb_id or "?", e)


def _clear_consumed_manual_plan(wb_id, reason=""):
    """Set manual wallbox planning hours to 0 after a concrete plan was used."""
    path = V4_CONFIG_FILE
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("[Scheduler] WB%s: Config fuer Plan-Reset nicht lesbar: %s", wb_id or "?", e)
        return False
    if not isinstance(data, dict):
        return False

    keys = [
        f"wb{int(wb_id)}_plan_hours",
        f"wb{int(wb_id)}_wbhour",
        f"wb{int(wb_id)}_sofort",
    ]
    if int(wb_id) == 1:
        keys.extend(["wbhour", "Wbhour", "wb_sofort"])

    changed = False
    targets = [data]
    if isinstance(data.get("config"), dict):
        targets.append(data["config"])
    for target in targets:
        for key in keys:
            old = target.get(key)
            if old is not None and str(old).strip() != "0":
                target[key] = "0"
                changed = True
        # Always write the explicit per-wallbox key, so legacy global wbhour
        # cannot resurrect a consumed WB1 plan on the next scheduler pass.
        primary = f"wb{int(wb_id)}_plan_hours"
        if str(target.get(primary, "")).strip() != "0":
            target[primary] = "0"
            changed = True

    if not changed:
        return False
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        try:
            if os.path.abspath(path) == os.path.abspath(V4_CONFIG_FILE):
                apply_config_secret_permissions(path, data=data if isinstance(data, dict) else None)
            elif _CANDIDATE_MODE:
                os.chmod(path, 0o600)
            else:
                os.chmod(path, 0o664)
        except Exception:
            pass
        logger.info("[Scheduler] WB%d: Ladeplanung auf 0 gesetzt%s", int(wb_id), f" - {reason}" if reason else ".")
        return True
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        logger.warning("[Scheduler] WB%s: Plan-Reset konnte nicht gespeichert werden: %s", wb_id or "?", e)
        return False


def _write_schedule_file(path, slots, wb_id=None, combined=False):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(slots, f)
    except Exception as e:
        label = "kombinierten Plan" if combined else "Plan"
        logger.error("[Scheduler] Fehler beim Schreiben des %s: %s", label, e)
        return False
    try:
        os.chmod(path, 0o600 if _CANDIDATE_MODE else 0o664)
    except PermissionError:
        # Datei kann von www-data angelegt sein. Solange das Schreiben gelang,
        # ist das kein Planungsfehler und soll nicht jede Runde den Log fuellen.
        logger.debug("[Scheduler] chmod uebersprungen fuer %s", path)
    except Exception as e:
        logger.debug("[Scheduler] chmod fuer %s nicht moeglich: %s", path, e)
    return True


def _schedule_has_current_or_future_slot(slots, current_ts):
    for slot in slots or []:
        try:
            slot_ts = int(slot.get("ts", 0))
        except Exception:
            continue
        if slot_ts + 900 > current_ts:
            return True
    return False


def _current_schedule_slots(slots, current_ts):
    active = []
    for slot in slots or []:
        try:
            slot_ts = int(slot.get("ts", 0))
        except Exception:
            continue
        if slot_ts <= current_ts < slot_ts + 900:
            active.append(dict(slot))
    return active


def _merge_running_schedule_hysteresis(old_slots, selected_slots, all_slots, current_ts, total_slots_needed, wb_id):
    """Keep a running planned charge alive when a user only extends/overlaps it."""
    active_old = _current_schedule_slots(old_slots, current_ts)
    if not active_old:
        return selected_slots, False

    allowed_ts = set()
    for slot in all_slots or []:
        try:
            allowed_ts.add(int(slot.get("ts", 0)))
        except Exception:
            continue

    active_old = [
        slot for slot in active_old
        if int(slot.get("ts", 0) or 0) in allowed_ts
    ]
    if not active_old:
        return selected_slots, False

    old_future = []
    for slot in old_slots or []:
        try:
            slot_ts = int(slot.get("ts", 0))
        except Exception:
            continue
        if slot_ts + 900 <= current_ts or slot_ts not in allowed_ts:
            continue
        old_future.append(dict(slot))

    # If the new request is an extension or equivalent window, preserve all
    # still-open old slots. If it is shorter, preserve at least the active slot
    # so the car is not toggled by a harmless UI save inside the same window.
    preserved = old_future if len(old_future) <= max(1, int(total_slots_needed or 0)) else active_old
    if not preserved:
        return selected_slots, False

    merged = []
    used_ts = set()
    for source in (preserved, selected_slots):
        for slot in source or []:
            try:
                slot_ts = int(slot.get("ts", 0))
            except Exception:
                continue
            if slot_ts in used_ts:
                continue
            copy = dict(slot)
            copy.pop("ai_score", None)
            copy["wb_id"] = wb_id
            merged.append(copy)
            used_ts.add(slot_ts)

    target_count = max(len(preserved), int(total_slots_needed or 0))
    if len(merged) > target_count:
        merged = merged[:target_count]
    merged = sorted(merged, key=lambda item: int(item.get("ts", 0)))
    active_ts = {int(slot.get("ts", 0) or 0) for slot in active_old}
    if not active_ts.issubset({int(slot.get("ts", 0) or 0) for slot in merged}):
        for slot in active_old:
            slot_ts = int(slot.get("ts", 0) or 0)
            if slot_ts not in {int(item.get("ts", 0) or 0) for item in merged}:
                copy = dict(slot)
                copy.pop("ai_score", None)
                copy["wb_id"] = wb_id
                merged.append(copy)
        merged = sorted(merged, key=lambda item: int(item.get("ts", 0)))
    return merged, True


# ---------------------------------------------------------------------------
# Battery-Care Scoring (Just-In-Time)
# ---------------------------------------------------------------------------
def _score_slots_battery_care(slots, bis_dt):
    """
    Berechnet den AI-Score fuer jeden Slot nach dem Battery-Care Algorithmus.

    Aus ai_scheduler.py (Originalkonzept):
    - Je naeher ein Slot an der Abfahrtszeit liegt, desto "billiger" erscheint
      er dem Algorithmus (JIT-Bonus).
    - Dadurch ladet das System bei gleichem Preis lieber 05:00 als 02:00 Uhr
      (Just-In-Time), was eine waeremere Batterie zum Abfahrtszeitpunkt ergibt.
    - JIT-Bonus ist bewusst klein, damit guenstigere Preise immer Vorrang haben.

    Formel: ai_score = base_price + (hours_to_departure * JIT_BONUS_CT_PER_H)
    -> kleinerer Score = bevorzugt.
    """
    departure_ts = int(bis_dt.timestamp())

    for slot in slots:
        slot_ts  = slot['ts']
        base_price = slot['price_ct']
        hours_to_departure = max(0.0, (departure_ts - slot_ts) / 3600.0)

        # Naehere Slots haben weniger "virtuelle Kosten" -> werden bevorzugt
        slot['ai_score'] = base_price + (hours_to_departure * JIT_BONUS_CT_PER_H)

    return slots


# ---------------------------------------------------------------------------
# Hauptfunktion: Schedule generieren
# ---------------------------------------------------------------------------
def generate_native_charging_schedule(config, wb_id=None):
    """
    Erstellt einen optimierten 15-Minuten Ladefahrplan (native_wallbox_schedule.json).

    Zwei-Phasen Battery-Care Strategie:
    - Phase 1 (Bulk):       Guenstigste Slots (nach JIT-Score) fuer den Grossteil.
    - Phase 2 (Pre-Cond.):  Slots kurz vor Abfahrt fuer die letzten ~15% des Bedarfs.

    Der Schedule wird nur neu berechnet wenn:
    - er aelter als 5 Minuten ist, ODER
    - Config oder SoC-Datei neuer als der Schedule sind.
    """
    if wb_id is None:
        combined_file = os.path.join(RAMDISK_DIR, "native_wallbox_schedule.json")
        all_slots = []
        for _wb_id in (1, 2):
            if _wb_id == 2:
                wb2_type = str(config.get("wb2_type", config.get("wb_native_type2", ""))).strip().lower()
                wb2_has_plan = any(str(config.get(k, "")).strip() not in ("", "0", "0.0")
                                   for k in ("wb2_plan_hours", "wb2_wbhour", "wb2_wbvon", "wb2_wbbis"))
                if not wb2_type and not wb2_has_plan:
                    continue
            all_slots.extend(generate_native_charging_schedule(config, wb_id=_wb_id) or [])

        all_slots = sorted(all_slots, key=lambda x: (int(x.get("ts", 0)), int(x.get("wb_id", 1))))
        if not all_slots:
            if os.path.exists(combined_file):
                _remove_schedule_file(combined_file, reason="Kein kombinierter Ladeplan aktiv - alten Schedule geloescht.")
            return []

        try:
            if os.path.exists(combined_file):
                old_slots = _read_schedule_file(combined_file)
                if old_slots == all_slots:
                    return all_slots
        except Exception:
            pass

        _write_schedule_file(combined_file, all_slots, combined=True)
        return all_slots

    wb_id = int(wb_id or 1)
    schedule_file = os.path.join(RAMDISK_DIR, f"native_wallbox_schedule_wb{wb_id}.json")

    smart_enable   = str(config.get(f"wb{wb_id}_smart_wbhour_enable", config.get("smart_wbhour_enable", "0"))).strip().lower() in ("1", "true", "yes")
    wb_native      = str(config.get("wb_native_enable", "0")).strip().lower() in ("1", "true", "yes")
    native_type_key = "wb_native_type2" if wb_id == 2 else "wb_native_type"
    wb_native_type = str(config.get(f"wb{wb_id}_type", config.get(native_type_key, config.get("wb_native_type", "")))).strip().lower()
    legacy_sofort  = config.get("wb_sofort", "0") if wb_id == 1 else "0"
    wb_sofort_flag = str(config.get(f"wb{wb_id}_sofort", legacy_sofort)).strip() == "1"

    def _parse_int(value, default=0):
        try:
            return int(float(str(value or default).replace(",", ".")))
        except Exception:
            return default

    def _parse_float(value, default=0.0):
        try:
            return float(str(value if value is not None and str(value).strip() != "" else default).replace(",", "."))
        except Exception:
            return default

    wb_mode_raw = config.get(f"wb{wb_id}_mode", config.get("wb_mode", None))
    wb_mode_explicit = wb_mode_raw is not None and str(wb_mode_raw).strip() != ""
    wb_mode = _parse_int(wb_mode_raw, 0) if wb_mode_explicit else None
    if wb_mode_explicit and wb_mode == 0:
        if os.path.exists(schedule_file):
            _remove_schedule_file(schedule_file, wb_id, "Wallbox-Modus aus - alten Schedule geloescht.")
        return []

    NATIVE_TYPES = {'openwb', 'openwb_pro', 'go-e', 'e3dc', 'e3dc_auto', 'e3dc_efy', 'e3dc_easy_connect', 'e3dc_multi', 'e3dc_multi_connect', 'e3dc_multi_connect_ii', 'dummy'}
    use_native_soc_path = wb_native and wb_native_type in NATIVE_TYPES
    # Native Systeme duerfen den Fahrzeug-SoC liefern, aber ein automatischer
    # Zielplan entsteht nur, wenn die Smart-Zielplanung explizit aktiv ist.
    use_smart_soc       = smart_enable
    wb_car_id           = str(config.get(f"wb{wb_id}_car_id", "")).strip()
    target_unit         = str(config.get(f"wb{wb_id}_target_unit", config.get("car_target_unit", "soc"))).strip().lower()
    if target_unit not in ("soc", "kwh"):
        target_unit = "soc"
    no_vehicle_selected = wb_car_id in ("__none", "no_vehicle", "kein_fahrzeug")
    if use_smart_soc and no_vehicle_selected and target_unit != "kwh":
        logger.info(f"[Scheduler] WB{wb_id}: Kein Fahrzeug ausgewaehlt - keine automatische SoC-Ladeplanung.")
        use_smart_soc = False

    legacy_hours = config.get("wbhour", config.get("Wbhour", 0)) if wb_id == 1 else 0
    manual_wbhour_cfg = _parse_int(
        config.get(f"wb{wb_id}_plan_hours", config.get(f"wb{wb_id}_wbhour", legacy_hours)),
        0
    )
    planning_enabled = use_smart_soc or manual_wbhour_cfg > 0 or wb_sofort_flag
    if not planning_enabled:
        if os.path.exists(schedule_file):
            try:
                os.remove(schedule_file)
                logger.info("[Scheduler] WB%d: Kein Ladeplan aktiv - alten Schedule geloescht.", wb_id)
            except Exception:
                pass
        return []

    wbhour      = 0
    current_soc = None
    target_soc  = 80.0
    target_kwh  = 0.0
    capacity    = 72.0
    charge_kw   = 11.0

    # -----------------------------------------------------------------------
    # wbhour aus SoC-Daten berechnen (nativer / smarter Pfad)
    # -----------------------------------------------------------------------
    if use_smart_soc:
        try:
            target_soc = float(config.get(f"wb{wb_id}_target_soc", config.get("car_target_soc", 80)))
            capacity   = float(config.get(f"wb{wb_id}_capacity",   config.get("car_capacity",   72.0)))
            charge_kw  = float(config.get(f"wb{wb_id}_charge_power", config.get("car_charge_power", 11.0)))
            if charge_kw <= 0:
                charge_kw = 11.0
            if target_unit == "kwh":
                target_kwh = max(0.0, _parse_float(
                    config.get(f"wb{wb_id}_target_kwh", config.get("car_target_kwh", 0.0)),
                    0.0,
                ))
                # Direct kWh planning intentionally does not need a vehicle SoC.
                # Reuse the existing SoC formula without reading fallback SoC files.
                current_soc = 0.0
                target_soc = 100.0 if target_kwh > 0 else 0.0
                capacity = target_kwh / 1.10 if target_kwh > 0 else capacity

            # Fallback-Kette fuer IST-SoC
            # 1. manuel_soc_wb1.json (openWB MQTT oder manuelle UI-Eingabe)
            for soc_file in [
                os.path.join(RAMDISK_DIR, f"manual_soc_wb{wb_id}.json"),
                "/var/www/html/tmp/manual_soc.json" if wb_id == 1 and not _CANDIDATE_MODE else "",
            ]:
                if not soc_file:
                    continue
                if os.path.exists(soc_file):
                    try:
                        with open(soc_file) as f:
                            soc_data = json.load(f)
                        soc_val = float(soc_data.get("soc", 0))
                        soc_ts  = float(soc_data.get("ts", 0))
                        soc_age = time.time() - soc_ts
                        # SoC-Werte bis zu 8h akzeptieren (Auto morgens abgesteckt)
                        if soc_val > 0 and soc_age < 28800:
                            current_soc = soc_val
                            logger.debug(
                                f"[Scheduler] SoC aus {os.path.basename(soc_file)}: "
                                f"{current_soc:.1f}% (Alter: {soc_age / 3600:.1f}h)"
                            )
                            break
                    except Exception:
                        pass

            # 2. SoC aus Config (manuell eingetragen in UI)
            if current_soc is None and wb_car_id != "__none":
                cfg_soc = config.get(f"wb{wb_id}_current_soc", config.get("car_current_soc", ""))
                if cfg_soc and str(cfg_soc).strip():
                    try:
                        current_soc = float(str(cfg_soc).strip())
                        logger.info(f"[Scheduler] SoC aus Config: {current_soc}%")
                    except Exception:
                        pass

            # 3. Spezifisches Fahrzeug via wbX_car_id aus vehicles.json
            if current_soc is None:
                if wb_car_id and wb_car_id not in ("none", "__none"):
                    veh_file = os.path.join(RAMDISK_DIR, "vehicles.json")
                    if os.path.exists(veh_file):
                        try:
                            with open(veh_file) as f:
                                v_data = json.load(f)

                            v_list = v_data.get('vehicles', []) if isinstance(v_data, dict) else v_data
                            for v in v_list:
                                if str(v.get("id")) == wb_car_id:
                                    v_soc = float(v.get("soc", 0))
                                    # Use the global TS if the vehicle has no individual TS
                                    ts = float(v.get("ts", v_data.get("ts", 0)))
                                    v_age = time.time() - ts
                                    if v_soc > 0 and v_age < 28800: # bis zu 8h akzeptieren
                                        current_soc = v_soc
                                        logger.info(f"[Scheduler] WB{wb_id}: Spezifischer SoC fuer {v.get('name', wb_car_id)} aus vehicles.json: {current_soc}%")
                                        break
                        except Exception:
                            pass

            # 4. Bluelink / MQTT-SoC-Cache Fallback (max. 2h alt)
            if current_soc is None and not wb_car_id:
                for bl_file in [
                    os.path.join(RAMDISK_DIR, "bluelink_soc.json"),
                    os.path.join(RAMDISK_DIR, "car_soc.json"),
                ]:
                    if os.path.exists(bl_file):
                        try:
                            with open(bl_file) as f:
                                bl = json.load(f)
                            bl_soc = float(bl.get("soc", bl.get("battery_soc", 0)))
                            bl_age = time.time() - float(bl.get("ts", 0))
                            if bl_soc > 0 and bl_age < 7200:
                                current_soc = bl_soc
                                logger.info(
                                    f"[Scheduler] SoC aus Fallback Cache {os.path.basename(bl_file)}: {current_soc}%"
                                )
                                break
                        except Exception:
                            pass

                # 4b. Letztes Fallback: Erstes Fahrzeug in vehicles.json nehmen
                if current_soc is None:
                    veh_file = os.path.join(RAMDISK_DIR, "vehicles.json")
                    if os.path.exists(veh_file):
                        try:
                            with open(veh_file) as f:
                                v_data = json.load(f)
                            v_list = v_data.get('vehicles', []) if isinstance(v_data, dict) else v_data
                            if v_list:
                                v = v_list[0]
                                v_soc = float(v.get("soc", 0))
                                ts = float(v.get("ts", v_data.get("ts", 0))) if isinstance(v_data, dict) else 0
                                v_age = time.time() - ts
                                if v_soc > 0 and v_age < 28800:
                                    current_soc = v_soc
                                    logger.info(f"[Scheduler] Fallback SoC aus vehicles.json (erstes Fahrzeug): {current_soc}%")
                        except Exception:
                            pass

            # 4. Pessimistischer Fallback: 0% (laedt bis Ziel-SoC komplett durch)
            if current_soc is None:
                current_soc = 0.0
                _warn_missing_vehicle_soc()

            soc_delta  = max(0.0, target_soc - current_soc)
            needed_kwh = (soc_delta / 100.0) * capacity * 1.10   # +10% Ladeverlust
            wbhour     = int(math.ceil(needed_kwh / charge_kw)) if needed_kwh > 0 else 0
            if target_unit == "kwh":
                logger.debug(
                    f"[Scheduler] WB{wb_id}: Lademenge={target_kwh:.1f} kWh, "
                    f"Ladeleistung={charge_kw:.1f} kW -> wbhour={wbhour}h"
                )
            else:
                logger.debug(
                    f"[Scheduler] IST={current_soc:.1f}%, ZIEL={target_soc:.1f}%, "
                    f"Bedarf={needed_kwh:.1f} kWh -> wbhour={wbhour}h"
                )
            # Abort-Flag: NUR Auto-SoC-Planung pausieren (nicht manuelle wbhour-Eingaben!)
            abort_flag = os.path.join(RAMDISK_DIR, "native_schedule_aborted.flag")
            if os.path.exists(abort_flag):
                logger.info("[Scheduler] Abort-Flag: Automatische SoC-Planung pausiert.")
                wbhour = 0

        except Exception as e:
            logger.warning(f"[Scheduler] Fehler bei wbhour-Berechnung: {e}")
            wbhour = 0

    else:
        # Manuell: wbhour direkt aus Config lesen (gesetzt durch UI-Slider oder C++ energy_manager)
        # Abort-Flag greift hier NICHT - manuelle Eingaben werden immer respektiert!
        try:
            wbhour = manual_wbhour_cfg
        except Exception:
            wbhour = 0

    # -----------------------------------------------------------------------
    # Kein Ladebedarf -> Schedule loeschen
    # -----------------------------------------------------------------------
    if wbhour <= 0:
        _remove_schedule_file(schedule_file, wb_id)
        return []


    # -----------------------------------------------------------------------
    # Preisquellen laden. Wiederkehrende Tarife besitzen eine vollständige,
    # lokal konfigurierte Tagesachse. Dynamische Tarife dürfen dagegen nur
    # tatsächlich vorhandene Markt-/Abrechnungsslots verwenden.
    # -----------------------------------------------------------------------
    recurring_tariff_axis = _uses_recurring_tariff_axis(config)
    epex_file = os.path.join(RAMDISK_DIR, "epex_daten.json")
    epex_data = []
    if os.path.exists(epex_file):
        try:
            with open(epex_file, "r") as f:
                raw_epex_data = json.load(f)
            if isinstance(raw_epex_data, list):
                epex_data = raw_epex_data
        except Exception:
            epex_data = []
    if not recurring_tariff_axis and not epex_data:
        logger.warning(
            "[Scheduler] WB%d: Dynamischer Tarif ohne gültige zukünftige Preisdaten.",
            wb_id,
        )
        return []

    # Leerstring-sicherer Parse: Wenn UI-Felder geleert wurden, steht ein '' in der Config.
    # float('') wirft ValueError -> daher or-Fallback auf Default-Wert.
    _awmwst_raw       = config.get("awmwst", "")
    _awnebenkosten_raw = config.get("awnebenkosten", "")
    try:
        awmwst        = float(_awmwst_raw) if str(_awmwst_raw).strip() != "" else 19.0
    except (ValueError, TypeError):
        awmwst        = 19.0
    try:
        awnebenkosten = float(_awnebenkosten_raw) if str(_awnebenkosten_raw).strip() != "" else 15.915
    except (ValueError, TypeError):
        awnebenkosten = 15.915

    wbvon_default = config.get("wbvon", config.get("Wbvon", "00:00")) if wb_id == 1 else "00:00"
    wbbis_default = config.get("wbbis", config.get("Wbbis", "23:59")) if wb_id == 1 else "07:00"
    wbvon = config.get(f"wb{wb_id}_wbvon", wbvon_default)
    wbbis = config.get(f"wb{wb_id}_wbbis", wbbis_default)
    no_time_limit = str(config.get("wb_no_time_limit", "0")).strip() in ("1", "true", "yes")
    start_now = str(wbvon).strip().lower() in ("now", "jetzt")
    # Sentinel: wbvon=0 UND wbbis=0 = kein Zeitfenster (24h/Octopus Modus)
    try:
        if start_now:
            von_h, von_m = 0, 0
        else:
            von_h, von_m = map(int, str(wbvon).replace('"', '').split(':'))
        bis_h, bis_m = map(int, str(wbbis).replace('"', '').split(':'))
    except Exception:
        von_h, von_m = 0,  0
        bis_h, bis_m = 23, 59
    if von_h == 0 and von_m == 0 and bis_h == 0 and bis_m == 0:
        no_time_limit = True

    wb_sofort = str(config.get("wb_sofort", "0")).strip() == "1"
    eco_mode  = str(config.get(f"wb{wb_id}_native_eco", config.get("wb_native_eco", "0"))).strip() == "1"
    if wb_sofort:
        no_time_limit = True

    if wbhour >= 99:
        no_time_limit = True

    # -----------------------------------------------------------------------
    # Tarif- und Eco-Daten laden.
    # epex_daten.json enthaelt den Rohmarktpreis. Fuer Octopus Heat/statische
    # Tarife ist aber billing_price aus eco_score.json die Nutzerwirklichkeit.
    # -----------------------------------------------------------------------
    eco_scores = {}
    tariff_prices = {}
    eco_file = os.path.join(RAMDISK_DIR, "eco_score.json")
    if os.path.exists(eco_file):
        try:
            with open(eco_file, encoding="utf-8") as f:
                eco_data = json.load(f)
            for entry in eco_data:
                slot_ts = _eco_slot_timestamp(entry)
                if slot_ts is None:
                    continue

                if entry.get("billing_price") is not None:
                    try:
                        tariff_prices[slot_ts] = float(entry.get("billing_price"))
                    except Exception:
                        pass

                if eco_mode:
                    dirty_score = None
                    if entry.get("score") is not None or entry.get("eco_score") is not None:
                        dirty_score = _clamp_eco_dirty_score(entry.get("score", entry.get("eco_score")))
                    elif entry.get("pure_eco_score") is not None:
                        clean_score = _clamp_eco_dirty_score(entry.get("pure_eco_score"))
                        dirty_score = None if clean_score is None else 1.0 - clean_score
                    elif entry.get("optimization_score") is not None:
                        clean_score = _clamp_eco_dirty_score(entry.get("optimization_score"))
                        dirty_score = None if clean_score is None else 1.0 - clean_score
                    if dirty_score is not None:
                        eco_scores[slot_ts] = dirty_score

            if tariff_prices:
                logger.debug("[Scheduler] %d Tarifpreise aus eco_score.json geladen.", len(tariff_prices))
            if eco_mode:
                logger.debug("[Scheduler] Eco-Modus aktiv: %d Eco-Score Eintraege geladen.", len(eco_scores))
        except Exception as e:
            logger.warning("[Scheduler] Eco-/Tarif-Datei nicht lesbar: %s", e)

    now    = datetime.datetime.now()
    window_rolled_after_expiry = False

    if no_time_limit:
        # Kein Zeitfenster: rollendes 48h Fenster ab jetzt (guenstigste Stunden 24/7)
        von_dt = now - datetime.timedelta(hours=1)
        bis_dt = now + datetime.timedelta(hours=48)
        logger.debug("[Scheduler] Kein Zeitfenster aktiv: 48h Rolling Window.")
    elif start_now:
        # "Jetzt" ist ein rollender Startanker: nie in die Vergangenheit planen,
        # aber den laufenden 15-Minuten-Slot noch beruecksichtigen.
        von_dt = now - datetime.timedelta(minutes=15)
        bis_dt = now.replace(hour=bis_h, minute=bis_m, second=0, microsecond=0)
        if now >= bis_dt:
            config_mtime = os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0
            if config_mtime <= bis_dt.timestamp() + 60:
                _clear_consumed_manual_plan(
                    wb_id,
                    "Ladefenster %s-%s ist abgelaufen - Plan verbraucht." % (wbvon, wbbis),
                )
                _remove_schedule_file(
                    schedule_file,
                    wb_id,
                    "Ladefenster %s-%s ist abgelaufen - Plan verbraucht." % (wbvon, wbbis),
                )
                return []
            bis_dt += datetime.timedelta(days=1)
    else:
        von_dt = now.replace(hour=von_h, minute=von_m, second=0, microsecond=0)
        bis_dt = now.replace(hour=bis_h, minute=bis_m, second=0, microsecond=0)

        if von_h > bis_h:
            if now.hour < bis_h:
                von_dt -= datetime.timedelta(days=1)
            else:
                bis_dt += datetime.timedelta(days=1)

        if now > bis_dt:
            von_dt += datetime.timedelta(days=1)
            bis_dt += datetime.timedelta(days=1)
            window_rolled_after_expiry = True

    von_ts = int(von_dt.timestamp())
    bis_ts = int(bis_dt.timestamp())

    # Ein erzeugter Plan ist eine konkrete Slot-Auswahl, kein alle 5 Minuten
    # neu zu fuellender Restbedarf. Sonst wird aus "1h bis 06:00" im laufenden
    # Fenster immer wieder "noch 1h" und die Wallbox laedt bis zum Fensterende.
    old_slots_for_continuity = []
    if os.path.exists(schedule_file):
        try:
            config_mtime = os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0
            soc_path = os.path.join(RAMDISK_DIR, f"manual_soc_wb{wb_id}.json")
            soc_mtime = os.path.getmtime(soc_path) if os.path.exists(soc_path) else 0
            # A manual hour plan is a concrete slot order. Vehicle SoC updates
            # during charging must not turn "1h planned" into a freshly
            # recalculated "still 1h to go". Only Smart/target-SoC planning may
            # adapt to a newer measured SoC.
            newest_input = max(config_mtime, soc_mtime) if use_smart_soc else config_mtime
            file_mtime = os.path.getmtime(schedule_file)
            old_slots = _read_schedule_file(schedule_file)
            old_slots_for_continuity = old_slots
            if old_slots and file_mtime >= newest_input:
                current_ts = int(now.timestamp())
                if _schedule_has_current_or_future_slot(old_slots, current_ts):
                    return old_slots
                if manual_wbhour_cfg > 0 or wb_sofort_flag:
                    _clear_consumed_manual_plan(
                        wb_id,
                        "alle geplanten Slots abgeschlossen - Plan verbraucht.",
                    )
                    _remove_schedule_file(
                        schedule_file,
                        wb_id,
                        "Alle geplanten Slots abgeschlossen - Plan verbraucht.",
                    )
                    return []
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Alle 15-min Slots im Ladefenster zusammenstellen. Der Marktpreis bleibt
    # eine eigene optionale Evidenz und wird niemals durch einen Tarifpreis
    # ersetzt. Für statische, Octopus-Heat- und Spezialtarife kann deshalb auch
    # ein morgiges Fenster geplant werden, bevor neue EPEX-Slots vorliegen.
    # -----------------------------------------------------------------------
    market_prices_by_slot = {}
    for entry in epex_data:
        try:
            e_start = int(float(entry["start_timestamp"]) / 1000)
            e_end = int(float(entry["end_timestamp"]) / 1000)
            price_raw = float(entry.get("marketprice"))
        except Exception:
            continue
        market_price_ct = (price_raw / 10.0) * (1.0 + (awmwst / 100.0)) + awnebenkosten

        cur = e_start
        while cur < e_end:
            if von_ts <= cur < bis_ts:
                slot_ts_q = (cur // 900) * 900
                market_prices_by_slot[slot_ts_q] = market_price_ct
            cur += 900

    # set mode to 'manual' if smart_enable is false or no_time_limit is true
    # 'manual' is interpreted by the frontend to render the yellow blocks
    slot_mode = "manual" if (wb_sofort or wbhour >= 99) else "auto"
    all_slots = []
    if recurring_tariff_axis:
        first_slot_ts = ((von_ts + 899) // 900) * 900
        for slot_ts in range(first_slot_ts, bis_ts, 900):
            runtime_tariff_price = tariff_prices.get(slot_ts)
            if runtime_tariff_price is None:
                runtime_tariff_price = configured_billing_price_for_timestamp(
                    config,
                    now_ts=slot_ts,
                )
                price_source = "configured_tariff"
            else:
                price_source = "runtime_tariff"
            try:
                price_ct = float(runtime_tariff_price)
            except Exception:
                continue
            if not math.isfinite(price_ct):
                continue
            market_price_ct = market_prices_by_slot.get(slot_ts)
            all_slots.append({
                "ts": slot_ts,
                "price_ct": price_ct,
                "market_price_ct": market_price_ct,
                "price_source": price_source,
                "mode": slot_mode,
            })
    else:
        for slot_ts in sorted(market_prices_by_slot):
            market_price_ct = market_prices_by_slot[slot_ts]
            price_ct = tariff_prices.get(
                slot_ts,
                tariff_prices.get(
                    slot_ts - 900,
                    tariff_prices.get(slot_ts + 900, market_price_ct),
                ),
            )
            try:
                price_ct = float(price_ct)
            except Exception:
                continue
            if not math.isfinite(price_ct):
                continue
            all_slots.append({
                "ts": slot_ts,
                "price_ct": price_ct,
                "market_price_ct": market_price_ct,
                "price_source": "runtime_tariff" if slot_ts in tariff_prices else "dynamic_market",
                "mode": slot_mode,
            })

    if not all_slots:
        logger.warning(
            "[Scheduler] WB%d: Keine gültigen Tarif-/Preisslots im Fenster %s-%s gefunden.",
            wb_id,
            wbvon,
            wbbis,
        )
        return []

    try:
        price_limit = float(config.get("wallbox_price_limit_ct", config.get("dvcarlimit", 0)) or 0)
    except Exception:
        price_limit = 0.0
    if price_limit > 0:
        logger.debug(
            "[Scheduler] WB%d: Preislimit %.1f ct/kWh wirkt nur fuer Modus 5; geplante Ladefenster bleiben gueltig.",
            wb_id,
            price_limit,
        )

    # -----------------------------------------------------------------------
    # Battery-Care: JIT-Score + Eco-Bonus berechnen
    # -----------------------------------------------------------------------
    all_slots = _score_slots_battery_care(all_slots, bis_dt)

    # Eco-Modifikation: Guter Eco-Score reduziert den ai_score (= wird bevorzugt)
    # Eco-Score 0.0 = maximal oekologisch (+0 Bonus), 1.0 = maximal dreckig (+5ct Malus)
    # Der Malus verschiebt max. 5ct des Preissignals -> Preis hat immer noch Vorrang!
    if eco_mode and eco_scores:
        ECO_WEIGHT_CT = 5.0  # max. 5 ct/kWh Eco-Malus fuer schmutzigen Strom
        for slot in all_slots:
            slot_ts_q = (slot['ts'] // 900) * 900
            # Naechsten verfuegbaren Eco-Score suchen (+/- 15min Toleranz)
            eco_val = eco_scores.get(slot_ts_q,
                      eco_scores.get(slot_ts_q - 900,
                      eco_scores.get(slot_ts_q + 900, None)))
            if eco_val is not None:
                slot['ai_score'] += eco_val * ECO_WEIGHT_CT  # hoher Score = teurer
        logger.debug("[Scheduler] Eco-Bonus angewendet (max %.1f ct/kWh Gewicht).", ECO_WEIGHT_CT)

    total_slots_needed = wbhour * 4  # 4x15min = 1h

    # --- Phase 2: Pre-Conditioning (kurz vor Abfahrt laden) -----------------
    pre_cond_needed = max(1, int(total_slots_needed * (PRE_CONDITION_PERCENT / 100.0)))
    pre_window_start = bis_ts - int(PRE_CONDITION_WINDOW_H * 3600)
    finite_prices = [_slot_price_ct(s) for s in all_slots]
    finite_prices = [p for p in finite_prices if p is not None]
    cheapest_price_ct = min(finite_prices) if finite_prices else None
    pre_price_limit_ct = (
        cheapest_price_ct + PRE_CONDITION_MAX_PRICE_PREMIUM_CT
        if cheapest_price_ct is not None else None
    )
    pre_cond_candidates = []
    for slot in all_slots:
        if slot['ts'] < pre_window_start:
            continue
        slot_price = _slot_price_ct(slot)
        if pre_price_limit_ct is not None and slot_price is not None and slot_price > pre_price_limit_ct:
            continue
        pre_cond_candidates.append(slot)
    # Naechste an Abfahrtszeit bevorzugen (groesster Timestamp)
    pre_cond_candidates.sort(key=lambda x: -x['ts'])
    phase2_selected = pre_cond_candidates[:pre_cond_needed]
    phase2_ts_set   = {s['ts'] for s in phase2_selected}

    # --- Phase 1: Bulk-Laden (guenstigste Slots nach JIT-Score) -------------
    phase1_needed     = total_slots_needed - len(phase2_selected)
    phase1_candidates = [s for s in all_slots if s['ts'] not in phase2_ts_set]
    phase1_candidates.sort(key=lambda x: x['ai_score'])  # kleinster Score = bevorzugt
    phase1_selected   = phase1_candidates[:phase1_needed]

    # --- Kombinieren (chronologisch) ----------------------------------------
    selected_slots = sorted(phase1_selected + phase2_selected, key=lambda x: x['ts'])

    if not selected_slots:
        return []

    # AI-Score ist interne Groesse -> nicht im JSON speichern
    for s in selected_slots:
        s.pop('ai_score', None)
        s['wb_id'] = wb_id

    current_ts = int(now.timestamp())
    selected_slots, running_preserved = _merge_running_schedule_hysteresis(
        old_slots_for_continuity,
        selected_slots,
        all_slots,
        current_ts,
        total_slots_needed,
        wb_id,
    )
    if running_preserved:
        logger.info(
            "[Scheduler] WB%d: Laufendes Ladefenster bleibt erhalten - "
            "Plan-Aenderung ueberlappt den aktiven Slot.",
            wb_id,
        )

    try:
        if os.path.exists(schedule_file):
            old_slots = _read_schedule_file(schedule_file)
            if old_slots == selected_slots:
                return selected_slots
    except Exception:
        pass

    if target_unit == "kwh":
        logger.info(
            f"[Scheduler] WB{wb_id}: Neuer Plan: {len(selected_slots)} Slots "
            f"({len(phase1_selected)} Bulk + {len(phase2_selected)} Pre-Cond.), "
            f"Fenster: {wbvon}-{wbbis}, Ziel: {target_kwh:.1f} kWh ({wbhour}h)"
        )
    else:
        _soc = current_soc if current_soc is not None else 0.0
        logger.info(
            f"[Scheduler] WB{wb_id}: Neuer Plan: {len(selected_slots)} Slots "
            f"({len(phase1_selected)} Bulk + {len(phase2_selected)} Pre-Cond.), "
            f"Fenster: {wbvon}-{wbbis}, "
            f"SoC: {_soc:.0f}%->{target_soc:.0f}% ({wbhour}h)"
        )

    _write_schedule_file(schedule_file, selected_slots, wb_id=wb_id)
    return selected_slots


# ---------------------------------------------------------------------------
# Status: Ist aktuell ein Ladefenster aktiv?
# ---------------------------------------------------------------------------
def get_planned_charging_status(wb_id=None):
    """
    Prueft ob der aktuelle Zeitstempel in einem geplanten Ladefenster liegt.

    Prioritaet:
    1. native_wallbox_schedule.json (bei nativer/Python-Steuerung)
    2. e3dc.wallbox.out (Legacy C++ Plan, nur wenn kein nativer Typ konfiguriert)
    """
    current_ts  = int(time.time())
    NATIVE_TYPES = {'openwb', 'openwb_pro', 'go-e', 'e3dc', 'e3dc_auto', 'e3dc_efy', 'e3dc_easy_connect', 'e3dc_multi', 'e3dc_multi_connect', 'e3dc_multi_connect_ii', 'dummy'}

    try:
        cfg       = get_config()
        is_native = str(cfg.get("wb_native_enable", "0")).strip().lower() in ("1", "true")
        native_type_key = "wb_native_type2" if int(wb_id or 1) == 2 else "wb_native_type"
        wb_type   = str(cfg.get(native_type_key, cfg.get("wb_native_type", ""))).strip().lower()
    except Exception:
        is_native = False
        wb_type   = ""

    def _native_schedule_files():
        files = []
        if wb_id is not None:
            files.append(os.path.join(RAMDISK_DIR, f"native_wallbox_schedule_wb{int(wb_id)}.json"))
        files.append(os.path.join(RAMDISK_DIR, "native_wallbox_schedule.json"))
        return files

    def _slot_matches_wb(slot):
        if wb_id is None:
            return True
        if "wb_id" not in slot:
            # Alte Plaene ohne WB-ID sind Legacy und gelten fuer WB1.
            return int(wb_id) == 1
        try:
            return int(slot.get("wb_id", 0) or 0) == int(wb_id)
        except Exception:
            return False

    # --- Python Schedule (native Typen: alleinige Kontrolle) ----------------
    if is_native and wb_type in NATIVE_TYPES:
        for native_file in _native_schedule_files():
            if not os.path.exists(native_file):
                continue
            try:
                with open(native_file, 'r') as f:
                    slots = json.load(f)
                for slot in slots:
                    if not _slot_matches_wb(slot):
                        continue
                    slot_ts = int(slot.get('ts', 0))
                    if slot_ts <= current_ts < (slot_ts + 900):
                        return True
            except Exception:
                pass
        return False

    # --- Legacy: Eba-M C++ Plan (e3dc.wallbox.out) --------------------------
    out_file = os.path.join(INSTALL_DIR, "e3dc.wallbox.out")
    if os.path.exists(out_file):
        try:
            with open(out_file, 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        slot_ts = int(parts[1])
                        mode    = int(parts[2])
                        if wb_id is None and slot_ts <= current_ts < (slot_ts + 900) and mode > 0:
                            return True
        except Exception:
            pass
        return False

    # --- Fallback: Python Schedule auch fuer non-native ---------------------
    for native_file in _native_schedule_files():
        if not os.path.exists(native_file):
            continue
        try:
            with open(native_file, 'r') as f:
                slots = json.load(f)
            for slot in slots:
                if not _slot_matches_wb(slot):
                    continue
                slot_ts = int(slot.get('ts', 0))
                if slot_ts <= current_ts < (slot_ts + 900):
                    return True
        except Exception:
            pass

    return False

# Trusted WebUI candidate mode
# ---------------------------------------------------------------------------
def _candidate_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_private_directory(path):
    raw = os.path.abspath(str(path or ""))
    if not raw or os.path.islink(raw):
        raise ValueError("candidate_directory_invalid")
    resolved = os.path.realpath(raw)
    if raw != resolved:
        raise ValueError("candidate_directory_alias")
    info = os.stat(resolved)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("candidate_directory_untrusted")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("candidate_directory_not_private")
    parent = os.path.dirname(resolved)
    parent_info = os.stat(parent)
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid():
        raise ValueError("candidate_parent_untrusted")
    if stat.S_IMODE(parent_info.st_mode) & 0o077:
        raise ValueError("candidate_parent_not_private")
    return resolved


def _candidate_read_private_json(path, *, max_bytes=_MAX_CANDIDATE_JSON_BYTES):
    if os.path.islink(path):
        raise ValueError("candidate_file_symlink")
    before = os.stat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o077
        or before.st_size < 2
        or before.st_size > max_bytes
    ):
        raise ValueError("candidate_file_untrusted")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if signature != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise ValueError("candidate_file_replaced")
        payload = b""
        while len(payload) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(fd)
        if signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("candidate_file_changed")
    finally:
        os.close(fd)
    if len(payload) > max_bytes:
        raise ValueError("candidate_file_too_large")
    try:
        return json.loads(payload.decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError("candidate_json_invalid") from exc


def _candidate_atomic_json(path, payload):
    directory = os.path.dirname(path)
    name = ".planner-result-%d-%d.tmp" % (os.getpid(), time.time_ns())
    tmp = os.path.join(directory, name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("candidate_result_short_write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _candidate_flat_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("candidate_config_not_object")
    nested = raw.get("config")
    flat = dict(nested) if isinstance(nested, dict) else {}
    for key, value in raw.items():
        if key != "config":
            flat[key] = value
    return flat


def _candidate_finite(value, key, minimum, maximum):
    try:
        number = float(str(value).strip().replace(",", "."))
    except Exception as exc:
        raise ValueError("candidate_config_invalid_%s" % key) from exc
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ValueError("candidate_config_invalid_%s" % key)


def _validate_candidate_config(raw):
    encoded = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    if len(encoded) > _MAX_CANDIDATE_JSON_BYTES:
        raise ValueError("candidate_config_too_large")
    flat = _candidate_flat_config(raw)
    if len(flat) > 4096:
        raise ValueError("candidate_config_too_many_keys")
    for key, value in flat.items():
        if not isinstance(key, str) or not key or len(key) > 128:
            raise ValueError("candidate_config_key_invalid")
        if any(ord(char) < 32 for char in key):
            raise ValueError("candidate_config_key_control")
        if isinstance(value, str) and (len(value) > 65536 or "\x00" in value):
            raise ValueError("candidate_config_value_invalid")

    for wb_id in (1, 2):
        for key in (f"wb{wb_id}_mode",):
            if key in flat:
                _candidate_finite(flat[key], key, 0, 20)
        for key in (
            f"wb{wb_id}_locked",
            f"wb{wb_id}_smart_wbhour_enable",
            f"wb{wb_id}_native_eco",
            f"wb{wb_id}_sofort",
            f"wb{wb_id}_manual_pause",
        ):
            if key in flat and str(flat[key]).strip().lower() not in (
                "0", "1", "true", "false", "yes", "no", "on", "off",
            ):
                raise ValueError("candidate_config_invalid_%s" % key)
        for key in (f"wb{wb_id}_plan_hours", f"wb{wb_id}_wbhour"):
            if key in flat:
                _candidate_finite(flat[key], key, 0, 99)
        for key in (f"wb{wb_id}_target_soc", f"wb{wb_id}_max_soc_si", f"wb{wb_id}_current_soc"):
            if key in flat and str(flat[key]).strip() != "":
                _candidate_finite(flat[key], key, 0, 100)
        for key in (f"wb{wb_id}_capacity", f"wb{wb_id}_target_kwh"):
            if key in flat and str(flat[key]).strip() != "":
                _candidate_finite(flat[key], key, 0, 500)
        key = f"wb{wb_id}_charge_power"
        if key in flat and str(flat[key]).strip() != "":
            _candidate_finite(flat[key], key, 0.1, 100)
        key = f"wb{wb_id}_target_unit"
        if key in flat and str(flat[key]).strip().lower() not in ("soc", "kwh"):
            raise ValueError("candidate_config_invalid_%s" % key)
        for key in (f"wb{wb_id}_wbvon", f"wb{wb_id}_wbbis", f"wb{wb_id}_battery_departure_time"):
            if key not in flat:
                continue
            value = str(flat[key]).strip().lower()
            if value in ("now", "jetzt") and key.endswith("_wbvon"):
                continue
            try:
                hour, minute = [int(part) for part in value.split(":", 1)]
            except Exception as exc:
                raise ValueError("candidate_config_invalid_%s" % key) from exc
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                raise ValueError("candidate_config_invalid_%s" % key)

    for key in ("wbhour", "Wbhour"):
        if key in flat:
            _candidate_finite(flat[key], key.lower(), 0, 99)
    for key in ("wbminsoc", "car_target_soc", "car_max_soc_si"):
        if key in flat and str(flat[key]).strip() != "":
            _candidate_finite(flat[key], key, 0, 100)
    for key in ("car_capacity", "car_target_kwh"):
        if key in flat and str(flat[key]).strip() != "":
            _candidate_finite(flat[key], key, 0, 500)
    if "car_charge_power" in flat and str(flat["car_charge_power"]).strip() != "":
        _candidate_finite(flat["car_charge_power"], "car_charge_power", 0.1, 100)
    if "dvcarlimit" in flat and str(flat["dvcarlimit"]).strip() != "":
        _candidate_finite(flat["dvcarlimit"], "dvcarlimit", 0, 500)
    return flat


def _candidate_int(value, default=0):
    try:
        return int(float(str(value).strip().replace(",", ".")))
    except Exception:
        return default


def _candidate_manual_plan_required(config, wb_id):
    mode_value = config.get(f"wb{wb_id}_mode")
    if mode_value is not None and str(mode_value).strip() != "" and _candidate_int(mode_value, 0) == 0:
        return False
    legacy_hours = config.get("wbhour", config.get("Wbhour", 0)) if wb_id == 1 else 0
    hours = _candidate_int(
        config.get(f"wb{wb_id}_plan_hours", config.get(f"wb{wb_id}_wbhour", legacy_hours)),
        0,
    )
    legacy_sofort = config.get("wb_sofort", "0") if wb_id == 1 else "0"
    sofort = str(config.get(f"wb{wb_id}_sofort", legacy_sofort)).strip().lower() in ("1", "true", "yes")
    return hours > 0 or sofort


def _validate_candidate_plan(path, wb_id=None):
    data = _candidate_read_private_json(path)
    if not isinstance(data, list) or len(data) > 10000:
        raise ValueError("candidate_plan_invalid")
    previous = None
    seen = set()
    has_live_slot = not data
    now_ts = int(time.time())
    normalized = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("candidate_plan_entry_invalid")
        try:
            ts = int(entry["ts"])
            price = float(entry["price_ct"])
            market_price_raw = entry["market_price_ct"]
            market_price = None if market_price_raw is None else float(market_price_raw)
            entry_wb = int(entry["wb_id"])
        except Exception as exc:
            raise ValueError("candidate_plan_entry_incomplete") from exc
        if not math.isfinite(price) or (market_price is not None and not math.isfinite(market_price)):
            raise ValueError("candidate_plan_price_invalid")
        if market_price is None and str(entry.get("price_source", "")) not in (
            "configured_tariff",
            "runtime_tariff",
        ):
            raise ValueError("candidate_plan_market_price_missing_without_tariff")
        if entry_wb not in (1, 2) or (wb_id is not None and entry_wb != int(wb_id)):
            raise ValueError("candidate_plan_wallbox_invalid")
        if str(entry.get("mode", "")) not in ("auto", "manual"):
            raise ValueError("candidate_plan_mode_invalid")
        order = (ts, entry_wb)
        if previous is not None and order < previous:
            raise ValueError("candidate_plan_unsorted")
        if order in seen:
            raise ValueError("candidate_plan_duplicate")
        if ts < 0 or ts > now_ts + 14 * 86400:
            raise ValueError("candidate_plan_timestamp_invalid")
        if ts + 900 > now_ts:
            has_live_slot = True
        previous = order
        seen.add(order)
        normalized.append(entry)
    if data and not has_live_slot:
        raise ValueError("candidate_plan_stale")
    return normalized


def run_candidate_directory(candidate_dir):
    """Erzeugt und prüft einen Plan vollständig in einem privaten Transaktionslauf."""
    global RAMDISK_DIR, V4_CONFIG_FILE, CONFIG_FILE, INSTALL_DIR, _CANDIDATE_MODE

    directory = _candidate_private_directory(candidate_dir)
    request_path = os.path.join(directory, _CANDIDATE_REQUEST_FILE)
    config_path = os.path.join(directory, _CANDIDATE_CONFIG_FILE)
    result_path = os.path.join(directory, _CANDIDATE_RESULT_FILE)
    request = _candidate_read_private_json(request_path, max_bytes=65536)
    if not isinstance(request, dict) or request.get("schema") != _CANDIDATE_SCHEMA:
        raise ValueError("candidate_request_schema_invalid")
    operation = str(request.get("operation", ""))
    if operation not in ("plan", "clear", "preserve"):
        raise ValueError("candidate_operation_invalid")
    raw_config = _candidate_read_private_json(config_path)
    config = _validate_candidate_config(raw_config)
    required = request.get("require_plan", [])
    if not isinstance(required, list) or any(int(value) not in (1, 2) for value in required):
        raise ValueError("candidate_required_plan_invalid")
    required = sorted(set(int(value) for value in required))
    derived_required = [wb_id for wb_id in (1, 2) if _candidate_manual_plan_required(config, wb_id)]
    if required != derived_required:
        raise ValueError("candidate_required_plan_mismatch")

    previous_globals = (RAMDISK_DIR, V4_CONFIG_FILE, CONFIG_FILE, INSTALL_DIR, _CANDIDATE_MODE)
    try:
        RAMDISK_DIR = directory
        V4_CONFIG_FILE = config_path
        CONFIG_FILE = config_path
        INSTALL_DIR = directory
        _CANDIDATE_MODE = True

        if operation == "clear":
            for filename in _CANDIDATE_PLAN_FILES:
                path = os.path.join(directory, filename)
                if os.path.exists(path):
                    os.remove(path)
        elif operation == "plan":
            if required and not _uses_recurring_tariff_axis(config):
                epex_path = os.path.join(directory, "epex_daten.json")
                if not os.path.exists(epex_path):
                    raise ValueError("candidate_market_data_missing")
                epex = _candidate_read_private_json(epex_path)
                if not isinstance(epex, list) or not epex:
                    raise ValueError("candidate_market_data_missing")
            # Die Kandidatenkonfiguration wird nach allen kopierten Kontinuitätseingaben geschrieben.
            # Erneutes Berühren verhindert, dass ein stale kopierter Zeitplan den Freshness-Check gewinnt.
            now_ns = time.time_ns()
            os.utime(config_path, ns=(now_ns, now_ns))
            generate_native_charging_schedule(config)
        # preserve prüft ausschließlich und lässt kopierte Pläne byteidentisch.

        final_raw_config = _candidate_read_private_json(config_path)
        _validate_candidate_config(final_raw_config)
        plans = {}
        per_wb = {}
        for filename in _CANDIDATE_PLAN_FILES:
            path = os.path.join(directory, filename)
            if not os.path.exists(path):
                continue
            wb_id = 1 if filename.endswith("wb1.json") else 2 if filename.endswith("wb2.json") else None
            validated = _validate_candidate_plan(path, wb_id=wb_id)
            plans[filename] = {
                "sha256": _candidate_sha256(path),
                "entries": len(validated),
            }
            if wb_id is not None:
                per_wb[wb_id] = validated

        for wb_id in required:
            filename = f"native_wallbox_schedule_wb{wb_id}.json"
            if filename not in plans or plans[filename]["entries"] <= 0:
                raise ValueError("candidate_required_plan_empty_wb%d" % wb_id)

        combined_path = os.path.join(directory, "native_wallbox_schedule.json")
        expected_combined = sorted(
            per_wb.get(1, []) + per_wb.get(2, []),
            key=lambda entry: (int(entry.get("ts", 0)), int(entry.get("wb_id", 1))),
        )
        if expected_combined:
            if not os.path.exists(combined_path):
                raise ValueError("candidate_combined_plan_missing")
            actual_combined = _validate_candidate_plan(combined_path)
            if actual_combined != expected_combined:
                raise ValueError("candidate_combined_plan_mismatch")
        elif os.path.exists(combined_path):
            actual_combined = _validate_candidate_plan(combined_path)
            if actual_combined:
                raise ValueError("candidate_combined_plan_unexpected")

        result = {
            "schema": _CANDIDATE_RESULT_SCHEMA,
            "success": True,
            "operation": operation,
            "config_sha256": _candidate_sha256(config_path),
            "plans": plans,
            "generated_at": int(time.time()),
        }
        _candidate_atomic_json(result_path, result)
        return result
    finally:
        RAMDISK_DIR, V4_CONFIG_FILE, CONFIG_FILE, INSTALL_DIR, _CANDIDATE_MODE = previous_globals


def _candidate_cli(argv=None):
    parser = argparse.ArgumentParser(description="Validate a private Wallbox planning transaction")
    parser.add_argument("--candidate-dir", required=True)
    args = parser.parse_args(argv)
    result_path = os.path.join(os.path.abspath(args.candidate_dir), _CANDIDATE_RESULT_FILE)
    try:
        run_candidate_directory(args.candidate_dir)
        return 0
    except Exception as exc:
        try:
            directory = _candidate_private_directory(args.candidate_dir)
            result_path = os.path.join(directory, _CANDIDATE_RESULT_FILE)
            _candidate_atomic_json(result_path, {
                "schema": _CANDIDATE_RESULT_SCHEMA,
                "success": False,
                "error": str(exc)[:256] or "candidate_failed",
                "generated_at": int(time.time()),
            })
        except Exception:
            pass
        logger.error("[Scheduler] Kandidatenplanung fehlgeschlagen: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(_candidate_cli())

