"""
E3DC-Control Wallbox Manager - Konfiguration, Pfade & Hilfsfunktionen.
Alle Pfad-Konstanten, Logging-Setup, get_config(), read_live_data(),
write_status(), read_current_epex_price().
"""
import os
import sys
import json
import time
import logging
from logging.handlers import RotatingFileHandler

# Sicherstellen dass das Installer-Verzeichnis im Pfad liegt (fuer rscp_client etc.)
_INSTALLER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _INSTALLER_DIR not in sys.path:
    sys.path.insert(0, _INSTALLER_DIR)
_REPO_ROOT = os.path.dirname(_INSTALLER_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from quiet_logging import install_quiet_info_filter
except ModuleNotFoundError:
    from Installer.quiet_logging import install_quiet_info_filter
try:
    from data_models import normalize_live_data_dict
except ModuleNotFoundError:
    from Installer.data_models import normalize_live_data_dict
try:
    from json_cache import atomic_write_on_change, file_signature, read_json_cached
except ModuleNotFoundError:
    from Installer.json_cache import atomic_write_on_change, file_signature, read_json_cached
try:
    from tariff_schedule import (
        configured_billing_price_for_timestamp,
        parse_special_tariff_schedule,
    )
except ModuleNotFoundError:
    from Installer.tariff_schedule import (
        configured_billing_price_for_timestamp,
        parse_special_tariff_schedule,
    )
from Installer.utils import get_paths

# ---------------------------------------------------------------------------
# Pfade (dynamisch, funktioniert auf Bare-Metal und in Docker)
# ---------------------------------------------------------------------------
_PATHS = get_paths()
INSTALL_DIR = _PATHS["install_path"]

CONFIG_FILE = os.path.join(INSTALL_DIR, "e3dc.config.txt")
V4_CONFIG_FILE = "/var/www/html/data/e3dc_v4.json"

# Docker /data Verzeichnis hat Vorrang
if os.path.exists("/var/www/html/data/e3dc.config.txt"):
    CONFIG_FILE = "/var/www/html/data/e3dc.config.txt"
elif os.path.exists("/var/www/html/e3dc_paths.json"):
    try:
        with open("/var/www/html/e3dc_paths.json", "r") as _f:
            _pdata = json.load(_f)
            _cand = os.path.join(_pdata.get("install_path", ""), "e3dc.config.txt")
            if os.path.exists(_cand):
                CONFIG_FILE = _cand
    except Exception:
        pass

RAMDISK_DIR = "/var/www/html/ramdisk"
if not os.path.exists(RAMDISK_DIR):
    RAMDISK_DIR = os.path.join(INSTALL_DIR, "ramdisk")
    os.makedirs(RAMDISK_DIR, exist_ok=True)

LIVE_DATA_FILE_PY  = os.path.join(RAMDISK_DIR, "live_data_py.json")  # V4 Native Python
STATUS_OUTPUT_FILE = os.path.join(RAMDISK_DIR, "wallbox_native.json")

LOG_DIR = "/var/www/html/logs"
if not os.path.exists(LOG_DIR):
    LOG_DIR = os.path.join(INSTALL_DIR, "logs")
    if not os.path.exists(LOG_DIR):
        LOG_DIR = RAMDISK_DIR

# ---------------------------------------------------------------------------
# Logger (einmalig aufsetzen; Submodule holen sich Kinder-Logger)
# ---------------------------------------------------------------------------
def _configure_wallbox_logging():
    """Ergänzt die Wallbox-Handler auch bei bereits konfiguriertem Root-Logger.

    ``logging.basicConfig`` ist wirkungslos, sobald der Dienststarter oder ein
    zuvor importiertes Modul bereits einen Handler angelegt hat. Das führte zu
    einem stabil laufenden Manager, aber dauerhaft leerer Web-Diagnosedatei.
    """

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    log_path = os.path.abspath(os.path.join(LOG_DIR, "wallbox_manager.log"))
    has_file_handler = any(
        isinstance(handler, RotatingFileHandler)
        and os.path.abspath(getattr(handler, "baseFilename", "")) == log_path
        for handler in root_logger.handlers
    )
    if not has_file_handler:
        try:
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=2 * 1024 * 1024,
                backupCount=2,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except OSError:
            # Journal/STDERR bleibt der sichere Diagnosepfad, wenn das
            # Dateisystem den optionalen Web-Loghandler nicht zulässt.
            pass
    has_console_handler = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in root_logger.handlers
    )
    if not has_console_handler:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)


_configure_wallbox_logging()
logger = logging.getLogger("WallboxManager")
install_quiet_info_filter(
    logger,
    min_interval_s=300,
    normalize_numbers=True,
    always_keywords=(
        "not-aus",
        "fehler",
        "fehlgeschlagen",
        "timeout",
        "dienst",
        "sigterm",
        "strukturaenderung",
        "parameteraenderung",
        "stecker gezogen",
        "netzladefenster beendet",
    ),
)

_CONFIG_CACHE = {"signature": None, "data": {}}


# ---------------------------------------------------------------------------
# Konfiguration lesen
# ---------------------------------------------------------------------------
def _safe_float(val, default):
    """float()-Konvertierung mit Leerstring-Schutz.
    Wenn val ein leerer String ist (z.B. durch leeres UI-Feld), wird default zurueckgegeben."""
    try:
        s = str(val).strip().replace(',', '.')
        return float(s) if s != '' else default
    except (ValueError, TypeError):
        return default


def _parse_special_tariff_schedule(raw):
    """Kompatibilitätswrapper für die neutrale Tarifauflösung."""
    return parse_special_tariff_schedule(raw)


def _configured_billing_price_now(config, now_ts=None):
    """Kompatibilitätswrapper für die neutrale Tarifauflösung."""
    return configured_billing_price_for_timestamp(config, now_ts=now_ts)


def _load_config_uncached():
    """Liest e3dc.config.txt + e3dc_v4.json und gibt ein flaches Dict zurueck."""
    conf = {}

    # 1. Basis-Konfiguration (txt)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('#') or line.startswith('//') or '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    if '//' in v:
                        v = v.split('//')[0]
                    if '#' in v:
                        v = v.split('#')[0]
                    conf[k.strip().lower()] = v.strip()
        except Exception as e:
            logger.error(f"Fehler beim Lesen der txt Config: {e}")

    # 2. V4-Erweiterungen (json) - UI-Einstellungen wie wb_native_enable
    v4_candidates = [
        "/var/www/html/data/e3dc_v4.json",
        os.path.join(INSTALL_DIR, "data", "e3dc_v4.json"),
        os.path.join(INSTALL_DIR, "e3dc_v4.json"),
    ]
    for cand in v4_candidates:
        if os.path.exists(cand):
            try:
                with open(cand, 'r') as f:
                    v4_data = json.load(f)
                if isinstance(v4_data, dict):
                    def _clean(val):
                        s = str(val)
                        if '//' in s: s = s.split('//')[0]
                        if '#' in s: s = s.split('#')[0]
                        return s.strip()

                    # PHP Speichert manchmal keys verschachtelt unter {"config": {"key": "val"}}
                    for k, v in v4_data.items():
                        if isinstance(v, dict):
                            for sub_k, sub_v in v.items():
                                conf[sub_k.lower()] = _clean(sub_v)
                        else:
                            conf[k.lower()] = _clean(v)
            except Exception as e:
                logger.error(f"Fehler beim Lesen der v4 JSON ({cand}): {e}")
            break

    return conf


def get_config():
    """Lädt die Wallbox-Konfiguration nur nach einer Dateiveränderung neu."""

    candidates = [
        CONFIG_FILE,
        "/var/www/html/data/e3dc_v4.json",
        os.path.join(INSTALL_DIR, "data", "e3dc_v4.json"),
        os.path.join(INSTALL_DIR, "e3dc_v4.json"),
    ]
    signature = tuple((path, file_signature(path)) for path in candidates)
    if _CONFIG_CACHE.get("signature") == signature:
        return dict(_CONFIG_CACHE.get("data") or {})
    data = _load_config_uncached()
    _CONFIG_CACHE["signature"] = signature
    _CONFIG_CACHE["data"] = dict(data)
    return dict(data)


# ---------------------------------------------------------------------------
# Live-Daten lesen
# ---------------------------------------------------------------------------
def live_data_age_s(data=None):
    """Alter der Live-Daten in Sekunden.

    Primaer gilt der vom e3dc-live Dienst geschriebene ``_ts``. Falls aeltere
    Installationen dieses Feld noch nicht liefern, ist die Datei-MTime der
    Fallback. Fehlt beides, wird ein sehr grosser Wert geliefert.
    """
    now = time.time()
    try:
        if isinstance(data, dict):
            ts = float(data.get("_ts", 0) or 0)
            if ts > 0:
                return max(0.0, now - ts)
        if os.path.exists(LIVE_DATA_FILE_PY):
            return max(0.0, now - os.path.getmtime(LIVE_DATA_FILE_PY))
    except Exception:
        pass
    return 999999.0


def read_live_data(max_age_s=None):
    """Liest live_data_py.json und normalisiert Feldnamen fuer Rueckwaertskompatibilitaet."""
    data = None

    if os.path.exists(LIVE_DATA_FILE_PY):
        loaded, meta = read_json_cached(LIVE_DATA_FILE_PY, with_meta=True)
        if meta.get("valid") and isinstance(loaded, dict):
            data = loaded

    if data and max_age_s is not None:
        age_s = live_data_age_s(data)
        if age_s > float(max_age_s):
            return None
        data["_live_age_s"] = round(age_s, 1)

    if data:
        data = normalize_live_data_dict(data)

    return data


# ---------------------------------------------------------------------------
# Status schreiben
# ---------------------------------------------------------------------------
def _native_wb_charging_like(state):
    """True, wenn der Status nach aktivem Laden aussieht."""
    try:
        status_text = str(state.get("status_msg", "")).lower()
        status_norm = status_text.replace("ä", "ae")
        terminal_hint = str(state.get("operator_hint_code", "")).strip().lower() in (
            "vehicle_charge_done",
            "battery_departure_done",
        )
        terminal_status = any(token in status_norm for token in (
            "ladung beendet",
            "beendet",
            "kein fahrzeug",
            "wartet mindestleistung",
            "warte auf sonne",
            "idle",
        ))
        if terminal_hint or terminal_status:
            return False
        set_amp = float(state.get("set_amp", 0) or 0)
        cap_amp = float(state.get("cap_amp", 0) or 0)
        power_w = abs(float(state.get("total_power_w", 0) or 0))
        offered_only_status = any(token in status_norm for token in (
            "startfreigabe",
            "freigegeben",
            "start wartet",
            "wartet mindestleistung",
            "warte auf sonne",
        ))
        if offered_only_status:
            return bool(state.get("charging_active")) or power_w > 500
        active_status = any(token in status_norm for token in (
            "laedt",
            "laed",
            "lädt",
            "lade ",
            "lade mit",
            "lade parallel",
        ))
        return (
            bool(state.get("charging_active"))
            or power_w > 500
            or (active_status and (set_amp > 0 or cap_amp > 0))
        )
    except Exception:
        return False


def _sanitize_native_wb_power(state_dict):
    """Harmonisiert Gesamtleistung mit den pro-Wallbox-Messwerten."""
    try:
        details = state_dict.get("wb_details")
        if not isinstance(details, list) or not details:
            return state_dict

        measured_total = 0.0
        charging_any = False
        connected_any = False
        for detail in details:
            if not isinstance(detail, dict):
                continue
            connected_any = connected_any or bool(detail.get("plug", False))
            charging = bool(detail.get("charging", False))
            power_w = abs(float(detail.get("power_w", 0.0) or 0.0))
            if bool(detail.get("manager_stop_pending", False)):
                detail["power_w"] = 0
                detail["charging"] = False
                detail["charge_power_w"] = 0
                charging = False
                power_w = 0.0
                continue
            phase_data_present = any(k in detail for k in (
                "phase_power_l1_w",
                "phase_power_l2_w",
                "phase_power_l3_w",
                "phase_power_sum_w",
            ))
            if phase_data_present:
                phase_sum_w = abs(float(detail.get("phase_power_sum_w", 0.0) or 0.0))
                if bool(detail.get("phase_power_verified", False)) and phase_sum_w > 50.0:
                    power_w = phase_sum_w
                    detail["power_w"] = round(power_w, 1)
                else:
                    power_w = 0.0
                    detail["power_w"] = 0
                    detail["charging"] = False
                    charging = False
            if charging:
                charging_any = True
                measured_total += power_w

        state_dict["connected"] = bool(connected_any or state_dict.get("connected", False))
        state_dict["charging_active"] = bool(charging_any)
        if charging_any:
            state_dict["total_power_w"] = round(measured_total, 1)
        else:
            state_dict["total_power_w"] = 0
    except Exception:
        pass
    return state_dict


def _smooth_native_wb_status(state_dict):
    """Haelt kurze RSCP-/HTTP-Aussetzer aus dem UI heraus.

    Die Regelung arbeitet weiterhin mit den Live-Werten. Nur die Anzeige-Datei
    bekommt fuer wenige Sekunden den letzten plausiblen Ladezustand, wenn ein
    einzelner Zyklus harte 0W-/Max-Spruenge oder fehlende A-Werte liefert.
    """
    try:
        now = time.time()
        if not os.path.exists(STATUS_OUTPUT_FILE):
            return state_dict
        if now - os.path.getmtime(STATUS_OUTPUT_FILE) > 20:
            return state_dict
        with open(STATUS_OUTPUT_FILE, "r", encoding="utf-8") as f:
            prev = json.load(f)
        if not isinstance(prev, dict):
            return state_dict

        prev_active = _native_wb_charging_like(prev)
        cur_active = _native_wb_charging_like(state_dict)
        if not prev_active:
            return state_dict

        prev_power = abs(float(prev.get("total_power_w", 0) or 0))
        cur_power = abs(float(state_dict.get("total_power_w", 0) or 0))
        prev_ts = int(prev.get("ts", 0) or 0)
        if prev_power <= 500 or int(now) - prev_ts > 20:
            return state_dict

        held = False

        # Kurzer Messausfall waehrend die Wallbox weiter als aktiv gemeldet wird.
        if cur_active and cur_power <= 50:
            state_dict["total_power_w"] = prev_power
            held = True

        # Fehlende A-/Cap-/Phasenwerte auffuellen, solange aktiv geladen wird.
        if cur_active:
            for key in ("set_amp", "cap_amp", "detected_phases", "fuzzy_factor"):
                if (state_dict.get(key) in (None, "", 0, "0")) and prev.get(key) not in (None, "", 0, "0"):
                    state_dict[key] = prev.get(key)
                    held = True
            status_now = str(state_dict.get("status_msg", "")).replace(" ", "").lower()
            if (not state_dict.get("status_msg") or "0a" in status_now) and prev.get("status_msg"):
                state_dict["status_msg"] = prev.get("status_msg")
                held = True

        # Phantom-Maxwerte gegen gesetzten Strom verifizieren.
        phases = int(float(state_dict.get("detected_phases", prev.get("detected_phases", 3)) or 3))
        phases = max(1, min(3, phases))
        amp_limit = max(
            float(state_dict.get("set_amp", 0) or 0),
            float(state_dict.get("cap_amp", 0) or 0),
        )
        expected_w = amp_limit * 230.0 * phases
        if cur_active and cur_power > 1000:
            if (expected_w > 0 and cur_power > expected_w * 1.45) or (expected_w <= 0 and cur_power > 18000):
                state_dict["total_power_w"] = prev_power
                held = True

        if held:
            state_dict["ui_hold"] = True
            state_dict["ui_hold_ts"] = int(now)
    except Exception:
        pass
    return state_dict


def write_status(state_dict):
    """Schreibt den aktuellen Manager-Status in die Ramdisk."""
    try:
        state_dict = _smooth_native_wb_status(_sanitize_native_wb_power(dict(state_dict)))
        state_dict["ts"] = int(time.time())
        atomic_write_on_change(
            STATUS_OUTPUT_FILE,
            state_dict,
            force_interval_s=10.0,
            noise_keys={"ts", "ui_hold_ts", "_live_age_s"},
        )
    except Exception as e:
        logger.error(f"Konnte Status nicht schreiben: {e}")


# ---------------------------------------------------------------------------
# Nativer PV-Lade-Score (ersetzt Eba-M RQ/ML Abhaengigkeit)
# ---------------------------------------------------------------------------
def compute_charge_score(battery_soc, battery_power, grid_power, config):
    """
    Berechnet einen Lade-Score (0.0 - 1.0) und einen empfohlenen Battery-Mindest-SoC
    basierend auf:
      - PV-Prognose bis Sonnenuntergang (pv_forecast.json)
      - Aktuellem Netz- und Batterie-Fluss
      - Ziel: Hausakku kurz vor Sonnenuntergang auf 90-100%
      - Konfiguriertes Ziel-SoC aus Config (wb_bat_target_soc, default 90)

    Rueckgabe: dict mit:
      'score'        (0.0=kein Laden, 1.0=voll laden)
      'target_soc'   (dynamisch berechneter Mindest-SoC fuer jetzt)
      'reason'       (kurze Begruendung als String)
      'remaining_pv_kwh'  (geschaetzte Rest-PV-Energie bis Sonnenuntergang)
      'bat_cap_kwh'  (verbleibende freie Kapazitaet)
    """
    import datetime
    import math

    result = {
        'score': 0.5,
        'target_soc': _safe_float(config.get('wbminsoc', 70), 70.0),
        'reason': 'Keine Prognose',
        'remaining_pv_kwh': 0.0,
        'bat_cap_kwh': 0.0,
    }

    # Lese Ziel-SoC fuer den Abend aus Config (neuer Key wb_bat_target_soc, Fallback 90%)
    bat_target_soc   = _safe_float(config.get('wb_bat_target_soc', 90.0), 90.0)
    bat_capacity_kwh = _safe_float(config.get('bat_capacity', 26.0), 26.0)

    now = datetime.datetime.now()
    now_ts_ms = now.timestamp() * 1000.0

    # --- Verbleibende PV-Energie bis Sonnenuntergang aus Prognose lesen ---
    remaining_pv_kwh = 0.0
    forecast_loaded = False
    forecast_path = os.path.join(RAMDISK_DIR, "pv_forecast.json")
    if os.path.exists(forecast_path):
        try:
            with open(forecast_path, 'r') as f:
                forecast = json.load(f)
            if isinstance(forecast, list):
                for slot in forecast:
                    slot_start = slot.get('start_timestamp', 0)
                    slot_end   = slot.get('end_timestamp', 0)
                    # Nur zukuenftige Slots, Max bis 22 Uhr (Sonnenuntergang)
                    sunset_cutoff = now.replace(hour=22, minute=0, second=0).timestamp() * 1000.0
                    if slot_start >= now_ts_ms and slot_start < sunset_cutoff:
                        # predicted_kwh ist tatsaechlich Durchschnittsleistung in kW fuer 15-Min-Slot
                        slot_kwh = float(slot.get('predicted_kwh', 0)) * 0.25  # kW * 0.25h = kWh
                        remaining_pv_kwh += slot_kwh
                forecast_loaded = True
        except Exception:
            pass

    result['remaining_pv_kwh'] = round(remaining_pv_kwh, 2)

    # --- Wie viel Kapazitaet muss noch aufgefuellt werden? ---
    bat_needed_kwh = max(0.0, (bat_target_soc - battery_soc) / 100.0 * bat_capacity_kwh)
    result['bat_cap_kwh'] = round(bat_needed_kwh, 2)

    # Aktueller echter PV-Anteil: Netto-Einspeissung + Batterie-Ladung = echte PV-Leistung
    # grid_power < 0: Einspeisung ins Netz (PV-Ueberschuss)
    # battery_power > 0: Batterie LAEDT (PV-Ueberschuss geht in Batterie) - Vorzeichen korrekt!
    net_surplus_now_w = max(0, -grid_power) + max(0, battery_power)

    if not forecast_loaded:
        # Kein Forecast: Regelung wie bisher (keine Score-Aenderung)
        result['reason'] = 'Kein Forecast verfuegbar'
        result['score'] = 0.5
        return result

    # --- Stunden bis Sonnenuntergang ---
    sunset_h = 22.0  # Konservative Annahme, realer Sunset kann frueher sein
    hours_left = max(0.1, sunset_h - now.hour - now.minute / 60.0)

    # --- Score-Berechnung ---
    # Wenn prognostizierte Rest-PV > Batterie-Bedarf: Alles gut, Wallbox kann frei laden
    # Wenn Rest-PV < Batterie-Bedarf: Batterie muss priorisiert werden, Wallbox gedrosselt

    if bat_needed_kwh <= 0.5:
        # Ziel-SoC schon nahezu erreicht! Volle Freigabe fuer die Wallbox.
        score = 1.0
        target_soc = battery_soc  # Kein hoeher-Mindest noetig
        reason = f"Bat-Ziel {bat_target_soc:.0f}% weitgehend erreicht ({battery_soc:.0f}%)"
        # bat_at_target NUR setzen wenn gerade tatsaechlich PV-Surplus da ist (Tagstueber)!
        # Nachts wuerde das sonst die Batterie-Umleitungs-Logik im Wallbox-Manager triggern
        # und das Auto ohne PV aus dem Netz laden.
        if net_surplus_now_w > 500 or remaining_pv_kwh > 0.5:
            result['bat_at_target'] = True
    elif remaining_pv_kwh >= bat_needed_kwh * 1.3:
        # Prognose hat genug Reserve (+30%) -> Wallbox darf laden, Puffer ist da
        score = 0.9
        # Minimaler Akku-Mindest-SoC: Aktueller SoC oder konfigurierter Wert (der niedrigere reicht)
        target_soc = max(_safe_float(config.get('wbminsoc', 70), 70.0), battery_soc - 10)
        reason = f"PV-Prognose ({remaining_pv_kwh:.1f} kWh) > Bedarf ({bat_needed_kwh:.1f} kWh) +Puffer"
    elif remaining_pv_kwh >= bat_needed_kwh * 0.9:
        # Knapp: Prognose reicht gerade so. Wallbox mit reduzierter Prio
        score = 0.6
        # Zwischenwert: SoC-Mindest dynamisch hochsetzen
        fraction_done = 1.0 - (bat_needed_kwh / (bat_capacity_kwh * bat_target_soc / 100.0))
        _wbminsoc = _safe_float(config.get('wbminsoc', 70), 70.0)
        target_soc = _wbminsoc + fraction_done * (bat_target_soc - _wbminsoc)
        reason = f"PV knapp ({remaining_pv_kwh:.1f} kWh vs {bat_needed_kwh:.1f} kWh benoetigt)"
    else:
        # Prognose reicht NICHT: Batterie muss prioritaet haben!
        score = 0.2
        # Setze Mindest-SoC dynamisch: Wie weit muessen wir jetzt schon sein, um 90% bis Sonnenuntergang zu schaffen?
        # Einfache lineare Interpolation: Je weniger PV, desto hoeher der Mindest-SoC jetzt.
        linear_target = bat_target_soc - (remaining_pv_kwh / bat_capacity_kwh * 100.0)
        target_soc = max(_safe_float(config.get('wbminsoc', 70), 70.0), min(bat_target_soc, linear_target))
        reason = f"PV-Defizit: {remaining_pv_kwh:.1f} kWh Prognose < {bat_needed_kwh:.1f} kWh benoetigt"

    # Netzeinspeisung ueberschreibt Score nach oben: Wenn gerade massiv eingespeist wird,
    # ist genug da fuer beides (Batterie + Wallbox)
    if net_surplus_now_w > 3000:
        score = max(score, 0.85)
        reason += f" | Einspeissung {net_surplus_now_w:.0f}W aktiv"

    # --- Morgen-Prognose: Wenn genug PV fuer morgen prognostiziert, ist Bat-Vollladung heute nicht noetig ---
    # (Optional: verhindert "erzwungenes Volladen" am Abend vor sonnigem Tag)
    tomorrow_ok = False
    tomorrow_kwh_needed = bat_capacity_kwh * bat_target_soc / 100.0  # Grobe Naehe
    try:
        if isinstance(forecast, list) and forecast_loaded:
            tomorrow_start = now.replace(hour=6, minute=0, second=0).timestamp() * 1000.0 + 86400000
            tomorrow_end   = tomorrow_start + 86400000
            tomorrow_pv = sum(
                float(s.get('predicted_kwh', 0)) * 0.25
                for s in forecast
                if tomorrow_start <= s.get('start_timestamp', 0) < tomorrow_end
            )
            tomorrow_ok = tomorrow_pv >= (bat_capacity_kwh * 0.5)  # Mind. 50% Kapazitaet prognostiziert
            if tomorrow_ok and not result.get('bat_at_target'):
                result['tomorrow_pv_kwh'] = round(tomorrow_pv, 1)
    except Exception:
        pass

    result['score'] = round(score, 2)
    result['target_soc'] = round(target_soc, 1)
    result['reason'] = reason
    result['tomorrow_ok'] = tomorrow_ok
    return result


# ---------------------------------------------------------------------------
# Aktuellen EPEX-Preis lesen
# ---------------------------------------------------------------------------
def read_current_epex_price(config):
    """Gibt den aktuellen Brutto-Strompreis in ct/kWh zurueck oder None."""
    try:
        eco_file = os.path.join(RAMDISK_DIR, "eco_score.json")
        now_ts = time.time()
        now_ms = now_ts * 1000.0
        if os.path.exists(eco_file):
            try:
                with open(eco_file, "r", encoding="utf-8") as f:
                    eco_data = json.load(f)
                if isinstance(eco_data, dict):
                    eco_rows = eco_data.get("scores") or eco_data.get("eco_scores") or eco_data.get("timeline") or eco_data.get("data") or []
                else:
                    eco_rows = eco_data
                for entry in eco_rows if isinstance(eco_rows, list) else []:
                    if not isinstance(entry, dict) or entry.get("billing_price") is None:
                        continue
                    start_ms = float(entry.get("start_timestamp", 0))
                    end_ms = float(entry.get("end_timestamp", 0))
                    if start_ms <= now_ms < end_ms:
                        return float(entry.get("billing_price"))
            except Exception:
                pass

        epex_file = os.path.join(RAMDISK_DIR, "epex_daten.json")
        if not os.path.exists(epex_file):
            return _configured_billing_price_now(config, now_ts=now_ts)
        with open(epex_file, "r") as f:
            data = json.load(f)

        awmwst        = _safe_float(config.get("awmwst", 19), 19.0)
        awnebenkosten = _safe_float(config.get("awnebenkosten", 15.915), 15.915)

        for entry in data:
            if entry["start_timestamp"] <= now_ms < entry["end_timestamp"]:
                if entry.get("billing_price_ct") is not None:
                    return float(entry.get("billing_price_ct"))
                if entry.get("marketprice") is not None:
                    return (entry["marketprice"] / 10.0) * (1.0 + (awmwst / 100.0)) + awnebenkosten
    except Exception:
        pass
    return _configured_billing_price_now(config)
