#!/usr/bin/env python3
"""
probe_pm.py - RSCP-Probe für E3DC Leistungsmesser (PM)

Testet einen konfigurierten PM-Index per RSCP und prüft, ob der Zähler
einen Verbraucher (Wärmepumpe, P >= 0W) oder einen Erzeuger (Zusatz-WR, P < -50W) misst.
Gibt ein strukturiertes JSON-Ergebnis für CLI und WebUI aus.
"""
import sys
import os
import json
import argparse

# Pfad-Setup
_INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
if _INSTALLER_DIR not in sys.path:
    sys.path.insert(0, _INSTALLER_DIR)
_REPO_ROOT = os.path.dirname(_INSTALLER_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from Wallbox.config import get_config
from rscp_client import RscpConnection, RscpTag, find_tag, find_tag_value


def _container(tag, children):
    return {"tag": tag, "type": 0x0E, "value": children}


def _uint16(tag, val):
    return {"tag": tag, "type": 0x05, "value": int(val)}


def _nil(tag):
    return {"tag": tag, "type": 0x00, "value": None}


def probe_pm_index(idx: int, timeout_s: float = 5.0) -> dict:
    """Fragt PM-Index per RSCP ab und bewertet Plausibilität als Verbraucher."""
    cfg = get_config()
    ip = cfg.get("server_ip") or cfg.get("e3dc_ip")
    port = int(cfg.get("server_port") or cfg.get("e3dc_port") or 5033)
    user = cfg.get("e3dc_user")
    pw = cfg.get("e3dc_password") or cfg.get("e3dc_pass")
    rscp_pw = cfg.get("aes_password") or cfg.get("rscp_password")

    if not ip or not user or not rscp_pw:
        return {
            "success": False,
            "error": "missing_credentials",
            "message": "E3DC-Verbindungsdaten unvollständig (IP, Benutzer oder RSCP-Passwort fehlt).",
        }

    conn = None
    try:
        conn = RscpConnection(ip, port, rscp_pw)
        conn.connect()
        conn.authenticate(user, pw)

        req = _container(RscpTag.PM_REQ_DATA, [
            _uint16(RscpTag.PM_INDEX, idx),
            _nil(RscpTag.PM_REQ_POWER_L1),
            _nil(RscpTag.PM_REQ_POWER_L2),
            _nil(RscpTag.PM_REQ_POWER_L3),
        ])
        resp = conn.request([req])
        pm_data = find_tag(resp, RscpTag.PM_DATA)
        if not (pm_data and isinstance(pm_data.get('value'), list)):
            return {
                "success": False,
                "error": "not_found",
                "index": idx,
                "message": f"PM-Index {idx} ist im E3DC-Kraftwerk nicht vorhanden oder liefert keine Daten.",
            }

        pd = pm_data['value']
        p1 = find_tag_value(pd, RscpTag.PM_POWER_L1)
        p2 = find_tag_value(pd, RscpTag.PM_POWER_L2)
        p3 = find_tag_value(pd, RscpTag.PM_POWER_L3)

        if (
            p1 is None or p2 is None or p3 is None
            or isinstance(p1, bool) or isinstance(p2, bool) or isinstance(p3, bool)
            or not isinstance(p1, (int, float)) or not isinstance(p2, (int, float)) or not isinstance(p3, (int, float))
        ):
            return {
                "success": False,
                "error": "incomplete_phases",
                "index": idx,
                "message": f"PM-Index {idx} liefert keine vollständigen 3-Phasen-Messwerte (nicht angeschlossen oder Teilphase).",
            }

        p1_val = round(float(p1), 1)
        p2_val = round(float(p2), 1)
        p3_val = round(float(p3), 1)
        p_sum = round(p1_val + p2_val + p3_val, 1)

        is_producer = bool(p_sum < 0.0 or p1_val < -10.0 or p2_val < -10.0 or p3_val < -10.0)
        plausible_consumer = not is_producer

        if is_producer:
            msg = (
                f"Plausibilitätsprüfung fehlgeschlagen: PM-Index {idx} misst Einspeisung / Erzeugung "
                f"({p_sum:+.0f} W: L1={p1_val:+.0f}W, L2={p2_val:+.0f}W, L3={p3_val:+.0f}W). "
                "Das ist eine Erzeugungsanlage (Zusatz-Wechselrichter), keine Wärmepumpe!"
            )
        else:
            msg = (
                f"PM-Index {idx} misst aktuellen Leistungsbezug "
                f"({p_sum:+.0f} W: L1={p1_val:+.0f}W, L2={p2_val:+.0f}W, L3={p3_val:+.0f}W). "
                "Hinweis: Bei PV-Zusatzwechselrichtern kann nachts ein geringer Standby-Bezug anliegen; "
                "prüfe die Zuordnung daher auch bei Tag oder anhand des Schaltplans."
            )

        return {
            "success": True,
            "plausible_consumer": plausible_consumer,
            "is_producer": is_producer,
            "index": idx,
            "p1": p1_val,
            "p2": p2_val,
            "p3": p3_val,
            "p_sum": p_sum,
            "message": msg,
        }
    except Exception as exc:
        return {
            "success": False,
            "error": "rscp_error",
            "index": idx,
            "message": f"RSCP-Abfrage für PM-Index {idx} fehlgeschlagen: {exc}",
        }
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="E3DC PM-Index Plausibilitätsprüfung")
    parser.add_argument("--index", type=int, default=1, help="PM-Index (0..7)")
    parser.add_argument("--json", action="store_true", help="Ausgabe im JSON-Format")
    args = parser.parse_args()

    res = probe_pm_index(args.index)
    if args.json or not sys.stdout.isatty():
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(f"PM-Index {res.get('index')}: {res.get('message')}")


if __name__ == "__main__":
    main()
