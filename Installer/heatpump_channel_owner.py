"""Reiner Auftrags- und Rücknahmeautomat für jeweils einen SHI-Kanal.

Dieses Modul führt keine Ein-/Ausgabe aus. Vor einem positiven Write muss der
Aufrufer den von ``advance`` gelieferten Checkpoint dauerhaft sichern. Genau
ein serialisierter Aufrufer darf Aktionen ausführen und Transport melden.
Ein SHI-Readback belegt eine Parametereinstellung, keinen Verdichterlauf.
"""

import copy
import math
from datetime import date


SCHEMA = "heatpump_channel_checkpoint_v1"
CHANNELS = ("hz", "ww")
OWNERS = ("pv", "price", "predump", "manual", "tariff")
STATES = ("frei", "vorbereitet", "gesendet", "eigen_aktiv",
          "ruecknahme_offen", "ruecknahme_gesendet", "fremd", "aufgegeben")
RETRY_S = 60.0
MAX_ATTEMPTS = 3
READBACK_AGE_S = 45.0
START_CONFIRM_TIMEOUT_S = 180.0
REVOKE_TIMEOUT_S = 900.0


def _number(value, *, minimum=0.0):
    return (type(value) in (int, float) and math.isfinite(value)
            and value >= minimum)


def raw_mode(value):
    """Nur eindeutige Rohmodi; Normalisierungsersatzwerte gelten nie als Beleg."""
    if type(value) in (int, float) and value in (0, 1, 2):
        return int(value)
    if type(value) is str:
        return {"auto": 0, "sollwert": 1, "setpoint": 1,
                "offset": 2}.get(value.strip().casefold())
    return None


def _channel():
    return {
        "state": "frei", "owner": None, "request_id": None,
        "revision": None, "target_c": None, "prepared_ts": None,
        "sent_ts": None, "confirmed_ts": None, "withdraw_requested_ts": None,
        "withdraw_ack_ts": None, "foreign_ts": None, "abandoned_ts": None,
        "last_sample_ts": None, "attempts": 0, "next_attempt_ts": 0.0,
        "stop_until": 0.0, "automatic_enabled": True,
        "clock_fault": False, "possible_effect": False,
        "transport_uncertain": False, "protection_latched": False,
        "generation": 0, "inflight": None, "diagnostic": None,
        "stop_reason": None, "baseline_target_c": None, "pre_boost_target_c": None,
        "start_attempts": 0, "start_attempt_ts": None, "alarm": False,
        "protection_counted": False,
    }


def new_checkpoint(now_s):
    """Leerer bekannter Anfang; fehlende/kaputte Dateien sind kein solcher Anfang."""
    if not _number(now_s, minimum=0.000001):
        raise ValueError("invalid_time")
    return {"schema": SCHEMA, "updated_ts": float(now_s),
            "channels": {name: _channel() for name in CHANNELS},
            "pv_day_protection": {"day": None, "counts": {},
                                  "last_event": None, "blocked": False}}


def _identity(desired):
    return (isinstance(desired, dict) and desired.get("owner") in OWNERS
            and isinstance(desired.get("request_id"), str)
            and 0 < len(desired["request_id"]) <= 160
            and type(desired.get("revision")) is int and desired["revision"] >= 0
            and _number(desired.get("target_c")) and desired["target_c"] <= 100)


def _action_valid(action, channel):
    if not isinstance(action, dict) or set(action) != {
            "id", "channel", "kind", "mode", "target_c"}:
        return False
    return (action["channel"] == channel and type(action["id"]) is int
            and action["id"] > 0 and type(action["mode"]) is int
            and ((action["kind"] == "withdraw"
                  and ((channel == "hz" and action["mode"] == 0
                        and action["target_c"] is None)
                       or (channel == "ww" and action["mode"] == 1
                           and _number(action["target_c"]) and action["target_c"] <= 100)))
                 or (action["kind"] == "start" and action["mode"] == 1
                     and _number(action["target_c"]) and action["target_c"] <= 100)))


def validate_checkpoint(value):
    """Strenger Struktur-/Kausalitätscheck, ohne einen fremden Zustand zu heilen."""
    if (not isinstance(value, dict) or set(value) not in (
            {"schema", "updated_ts", "channels"},
            {"schema", "updated_ts", "channels", "pv_day_protection"})
            or value.get("schema") != SCHEMA
            or not _number(value.get("updated_ts"), minimum=0.000001)
            or not isinstance(value.get("channels"), dict)
            or set(value["channels"]) != set(CHANNELS)):
        return {}
    # v1-Dateien vor der Erweiterung bleiben lesbar; unbekannte Felder oder
    # widersprüchliche Werte werden weiterhin abgewiesen.
    value = copy.deepcopy(value)
    value.setdefault("pv_day_protection", {"day": None, "counts": {},
                                           "last_event": None, "blocked": False})
    daily = value["pv_day_protection"]
    if (not isinstance(daily, dict) or set(daily) != {"day", "counts", "last_event", "blocked"}
            or type(daily["blocked"]) is not bool or not isinstance(daily["counts"], dict)
            or (daily["last_event"] is not None and (type(daily["last_event"]) is not str
                                                     or len(daily["last_event"]) > 512))):
        return {}
    if daily["day"] is not None:
        try:
            if type(daily["day"]) is not str or date.fromisoformat(daily["day"]).isoformat() != daily["day"]:
                return {}
        except (TypeError, ValueError):
            return {}
    elif daily["counts"] or daily["blocked"] or daily["last_event"] is not None:
        return {}
    if any(type(reason) is not str or not 0 < len(reason) <= 160
           or type(count) is not int or not 0 <= count <= 1000000
           for reason, count in daily["counts"].items()):
        return {}
    if daily["blocked"] != any(count >= 2 for count in daily["counts"].values()):
        return {}
    additions = {"baseline_target_c": None, "pre_boost_target_c": None, "start_attempts": 0,
                 "start_attempt_ts": None, "alarm": False, "protection_counted": False}
    for channel, state in value["channels"].items():
        if isinstance(state, dict):
            # Frühere Dateien enthielten den flüchtigen Ferienanker. Nur einen
            # gültigen Altwert verwerfen; nach Neustart beginnt die Entprellung neu.
            holiday = state.pop("holiday_since_ts", None)
            if holiday is not None and (not _number(holiday, minimum=0.000001)
                                        or holiday > value["updated_ts"]):
                return {}
            legacy = "baseline_target_c" not in state
            # Alte WW-Modus-0-Rücknahmen sind kein Beleg für den jetzt
            # verlangten Timer-Grundzustand; sie werden begrenzt neu geprüft.
            if legacy and channel == "ww" and state.get("state") in (
                    "ruecknahme_offen", "ruecknahme_gesendet"):
                action = state.get("inflight")
                if isinstance(action, dict) and action.get("kind") == "withdraw":
                    if (set(action) != {"id", "channel", "kind", "mode", "target_c"}
                            or action["channel"] != "ww"
                            or type(action["id"]) is not int or action["id"] <= 0
                            or action["id"] != state.get("generation")
                            or type(action["mode"]) is not int or action["mode"] != 0
                            or action["target_c"] is not None):
                        return {}
                    state["inflight"] = None
                state["state"] = "ruecknahme_offen"
                state["withdraw_ack_ts"] = None
                state["attempts"] = 0
                state["next_attempt_ts"] = 0.0
                state["withdraw_requested_ts"] = value["updated_ts"]
            for key, default in additions.items():
                state.setdefault(key, default)
    timestamps = ("start_attempt_ts", "prepared_ts", "sent_ts", "confirmed_ts", "withdraw_requested_ts",
                  "withdraw_ack_ts", "foreign_ts", "abandoned_ts", "last_sample_ts")
    for name, state in value["channels"].items():
        if not isinstance(state, dict) or set(state) != set(_channel()):
            return {}
        if state["state"] not in STATES:
            return {}
        if any(type(state[key]) is not bool for key in (
                "automatic_enabled", "clock_fault", "possible_effect",
                "transport_uncertain", "protection_latched", "alarm", "protection_counted")):
            return {}
        for key in timestamps:
            if state[key] is not None and (not _number(state[key], minimum=0.000001)
                                           or state[key] > value["updated_ts"]):
                return {}
        if (state["baseline_target_c"] is not None and (
                not _number(state["baseline_target_c"]) or state["baseline_target_c"] > 100)):
            return {}
        if (state["pre_boost_target_c"] is not None and (
                not _number(state["pre_boost_target_c"]) or state["pre_boost_target_c"] > 100)):
            return {}
        if (type(state["start_attempts"]) is not int
                or not 0 <= state["start_attempts"] <= MAX_ATTEMPTS):
            return {}
        if (type(state["attempts"]) is not int or not 0 <= state["attempts"] <= MAX_ATTEMPTS
                or type(state["generation"]) is not int or state["generation"] < 0
                or not _number(state["next_attempt_ts"])
                or not _number(state["stop_until"])):
            return {}
        for key in ("diagnostic", "stop_reason"):
            if state[key] is not None and (type(state[key]) is not str or len(state[key]) > 160):
                return {}
        identity_present = any(state[key] is not None for key in (
            "owner", "request_id", "revision", "target_c"))
        if identity_present and not _identity(state):
            return {}
        if state["possible_effect"] and (not identity_present or state["prepared_ts"] is None):
            return {}
        if state["state"] in ("frei", "fremd") and state["possible_effect"]:
            return {}
        if state["state"] in ("vorbereitet", "gesendet", "eigen_aktiv") and not state["possible_effect"]:
            return {}
        if state["start_attempt_ts"] is not None and (state["prepared_ts"] is None
                or state["start_attempt_ts"] < state["prepared_ts"]):
            return {}
        if state["start_attempts"] > 0 and state["start_attempt_ts"] is None:
            return {}
        if state["sent_ts"] is not None and (state["prepared_ts"] is None
                                             or state["sent_ts"] < state["prepared_ts"]):
            return {}
        if state["confirmed_ts"] is not None and (state["sent_ts"] is None
                                                  or state["confirmed_ts"] <= state["sent_ts"]):
            return {}
        if state["state"] == "eigen_aktiv" and state["confirmed_ts"] is None:
            return {}
        if state["state"] == "fremd" and state["foreign_ts"] is None:
            return {}
        if state["state"] == "aufgegeben" and state["abandoned_ts"] is None:
            return {}
        if state["withdraw_ack_ts"] is not None and (
                state["withdraw_requested_ts"] is None
                or state["withdraw_ack_ts"] < state["withdraw_requested_ts"]
                or state["attempts"] == 0):
            return {}
        if state["state"] == "ruecknahme_gesendet" and state["withdraw_ack_ts"] is None:
            return {}
        if state["state"] in ("ruecknahme_offen", "ruecknahme_gesendet") and state["withdraw_requested_ts"] is None:
            return {}
        action = state["inflight"]
        if action is not None:
            if not _action_valid(action, name) or action["id"] != state["generation"]:
                return {}
            if action["kind"] == "start" and (state["state"] != "vorbereitet"
                                               or action["target_c"] != state["target_c"]):
                return {}
            if action["kind"] == "withdraw" and state["state"] not in (
                    "ruecknahme_offen", "ruecknahme_gesendet"):
                return {}
    return copy.deepcopy(value)


def _copy(checkpoint, now_s):
    result = validate_checkpoint(checkpoint)
    if not result or not _number(now_s, minimum=0.000001):
        raise ValueError("invalid_checkpoint_or_time")
    if now_s < result["updated_ts"]:
        for state in result["channels"].values():
            state["clock_fault"] = True
            state["diagnostic"] = "clock_went_backwards"
    else:
        result["updated_ts"] = float(now_s)
    return result


def _clear_effect(state, diagnostic):
    state.update({"state": "frei", "possible_effect": False, "inflight": None,
                  "diagnostic": diagnostic, "alarm": False})
    if diagnostic != "start_not_transmitted":
        # Ein bestätigter Abschluss beendet die Rücknahme-Wiederholungsfrist.
        # Für neue Starts gilt ausschließlich die eigene Wiedereinschaltsperre.
        state["next_attempt_ts"] = 0.0


def suspend_ineffective_withdrawal(checkpoint, channel, *, now_s, holiday_since_ts=None):
    """Beendet eine wirkungslose Rücknahme erst nach stabil beobachteten Ferien.

    Bis zum Ablauf eines Retry-Abstands bleiben Budget und Frist unverändert.
    Ferien unterdrücken IO sofort; ein einzelner Ferienimpuls beendet keinen
    fehlgeschlagenen Auftrag. Quittungen bleiben auch beim Abschluss erhalten.
    """
    if channel not in CHANNELS:
        raise ValueError("invalid_channel")
    result = _copy(checkpoint, now_s)
    state = result["channels"][channel]
    if not state["possible_effect"] and state["state"] in (
            "ruecknahme_offen", "ruecknahme_gesendet"):
        if (not state["clock_fault"] and _number(holiday_since_ts, minimum=0.000001)
                and now_s - holiday_since_ts >= RETRY_S):
            _clear_effect(state, "wp_holiday_mode")
    return result


def _foreign(state, sample_ts, reason):
    state.update({"state": "fremd", "possible_effect": False, "inflight": None,
                  "foreign_ts": sample_ts, "diagnostic": reason})


def _baseline_matches(state, channel, mode, target):
    if channel == "hz":
        return mode == 0
    return (mode == 1 and _number(target) and state["baseline_target_c"] is not None
            and abs(target - state["baseline_target_c"]) <= 0.1)


def _observe(state, observation, now_s, channel):
    if not isinstance(observation, dict) or observation.get("valid") is not True:
        return None
    sample = observation.get("sample_ts")
    mode = raw_mode(observation.get("raw_mode"))
    if (mode is None or not _number(sample, minimum=0.000001)
            or not 0 <= now_s - sample <= READBACK_AGE_S
            or (state["last_sample_ts"] is not None and sample <= state["last_sample_ts"])):
        return None
    state["last_sample_ts"] = sample
    target = observation.get("target_c")
    baseline = _baseline_matches(state, channel, mode, target)
    if state["state"] in ("ruecknahme_offen", "ruecknahme_gesendet"):
        # HZ: ausschließlich Modus prüfen. WW: Modus und Timer-Untergrenze.
        if (baseline and state["withdraw_ack_ts"] is not None
                and sample > state["withdraw_ack_ts"]):
            _clear_effect(state, "withdrawal_readback_confirmed")
        else:
            state["diagnostic"] = "withdrawal_readback_mismatch"
        return mode
    if state["state"] in ("fremd", "aufgegeben"):
        barrier = state["foreign_ts"] if state["state"] == "fremd" else state["abandoned_ts"]
        if (baseline or (mode == 0 and not state["alarm"])) and sample > barrier:
            _clear_effect(state, "external_auto_observed" if mode == 0 else "baseline_readback_confirmed")
        return mode
    anchor = state["start_attempt_ts"] or state["sent_ts"] or state["prepared_ts"]
    if not state["possible_effect"] or anchor is None or sample <= anchor:
        if state["state"] == "frei" and mode in (1, 2) and not baseline:
            _foreign(state, sample, "unexpected_setting_observed")
        return mode
    if (mode == 1 and _number(target) and abs(target - state["target_c"]) <= 0.1
            and (state["sent_ts"] is not None or state["start_attempt_ts"] is not None)
            and state["state"] in ("vorbereitet", "gesendet", "eigen_aktiv")):
        # Ein passender späterer Registerwert bestätigt auch einen zuvor
        # unsicheren Teilwrite. Eine bloße Absicht tut dies ausdrücklich nicht.
        if state["sent_ts"] is None:
            state["sent_ts"] = anchor
        state["state"] = "eigen_aktiv"
        state["transport_uncertain"] = False
        state["diagnostic"] = None
        if state["confirmed_ts"] is None:
            state["confirmed_ts"] = sample
    elif state["state"] in ("vorbereitet", "gesendet", "eigen_aktiv"):
        # Das EMS ist der einzige SHI-Schreiber. Ein alter Restwert begründet
        # keinen Fremdbesitz und blockiert weder Retry noch Rücknahme.
        state["diagnostic"] = "start_readback_mismatch"
    return mode


def _reserve(state, channel, kind):
    state["generation"] += 1
    withdrawal = kind == "withdraw"
    action = {"id": state["generation"], "channel": channel, "kind": kind,
              "mode": 0 if withdrawal and channel == "hz" else 1,
              "target_c": (None if channel == "hz" else state["baseline_target_c"])
              if withdrawal else state["target_c"]}
    state["inflight"] = action
    return copy.deepcopy(action)


def _abandon(state, now_s, reason):
    state.update({"state": "aufgegeben", "inflight": None,
                  "abandoned_ts": now_s, "diagnostic": reason,
                  "alarm": state["alarm"] or reason == "withdrawal_unresolved"})


def advance(checkpoint, channel, *, now_s, observation=None, desired=None,
            stop_reason=None, stop_allowed=True, automatic_enabled=True,
            protection=False, user_hold_s=0, signal_hold_s=600, manual_owned=False,
            baseline_target_c=None, release_on_baseline_readback=False):
    """Verarbeitet einen Kanal und liefert höchstens eine ausführbare Aktion.

    ``baseline_target_c`` ist die konfigurierte WW-Timer-Untergrenze.
    Rücknahmen benötigen keinen Besitzbeleg; externe SHI-Schreiber werden
    nicht unterstützt. ``desired`` enthält Besitzer, Auftragskennung, Revision, Temperatur sowie
    ``valid=True`` und ``grant=True``. Kein Wunsch ist ein Versandbeleg. ``None``
    beendet einen Auftrag unter Beachtung von ``stop_allowed``. Ein expliziter
    ``stop_reason`` oder Nutzer-Aus darf auch unbelegte Einstellungen zurücknehmen.
    ``stop_allowed`` kommt aus Mindestlaufzeit/Taktschutz der vorhandenen Policy;
    Nutzer-Aus und harter Schutz übergehen nur die Rücknahmeverzögerung.
    ``manual_owned`` ist ein bereits unabhängig vom SHI-Connect erhobener
    Nutzerbesitz (z. B. WW-Sofort), kein bloßer neuer Startwunsch.
    """
    if channel not in CHANNELS:
        raise ValueError("invalid_channel")
    if (any(type(value) is not bool for value in (
            stop_allowed, automatic_enabled, protection, manual_owned, release_on_baseline_readback))
            or not _number(user_hold_s) or not _number(signal_hold_s)
            or (baseline_target_c is not None and (not _number(baseline_target_c)
                                                   or baseline_target_c > 100))
            or (stop_reason is not None and (type(stop_reason) is not str
                                             or not 0 < len(stop_reason) <= 160))):
        raise ValueError("invalid_policy_input")
    result = _copy(checkpoint, now_s)
    state = result["channels"][channel]
    if state["diagnostic"] == "wp_holiday_mode":
        state["diagnostic"] = None
    if baseline_target_c is not None:
        state["baseline_target_c"] = float(baseline_target_c)
    # Bei rückwärts laufender Uhr keine neue Freigabe und keinen Zeitbeleg
    # annehmen. Eigene Wirkung darf dennoch einmal geordnet zurückgenommen
    # werden; die letzte bekannte Zeit hält Retry und Sperren konservativ an.
    now_s = max(now_s, result["updated_ts"])
    was_enabled = state["automatic_enabled"]
    state["automatic_enabled"] = automatic_enabled
    if was_enabled and not automatic_enabled:
        state["stop_until"] = max(state["stop_until"], now_s + user_hold_s)
    if (protection and state["possible_effect"] and not state["protection_latched"]
            and state["withdraw_ack_ts"] is None):
        state["protection_latched"] = True
        state["stop_until"] = max(state["stop_until"],
                                  now_s + max(600.0, signal_hold_s, user_hold_s))
    mode = None if state["clock_fault"] else _observe(state, observation, now_s, channel)
    if manual_owned and automatic_enabled and not protection and not stop_reason:
        _foreign(state, now_s, "manual_owner_observed")
        return result, None
    wanted = (_identity(desired) and desired.get("valid") is True
              and desired.get("grant") is True)
    # Ein bestätigter Tarifauftrag geht ohne neue Hardwarekante an PV über.
    if (wanted and state["owner"] == "tariff" and desired["owner"] == "pv"
            and state["state"] == "eigen_aktiv" and not protection and automatic_enabled
            and state["withdraw_requested_ts"] is None and not stop_reason
            and state["target_c"] == desired["target_c"]):
        state.update({key: desired[key] for key in ("owner", "request_id", "revision")})
    same = bool(wanted and all(state[key] == desired[key] for key in (
        "owner", "request_id", "revision", "target_c")))
    stopping = bool(not automatic_enabled or protection or stop_reason or not same)
    reason = ("user_off" if not automatic_enabled else
              (stop_reason or ("protection" if protection else "request_ended")))
    start_failed = bool((state["state"] in ("vorbereitet", "gesendet")
                         and (now_s - state["prepared_ts"] >= START_CONFIRM_TIMEOUT_S
                              or (state["start_attempts"] >= MAX_ATTEMPTS
                                  and now_s >= state["next_attempt_ts"])))
                        or (state["state"] == "aufgegeben" and state["possible_effect"]
                            and not state["alarm"]))
    if start_failed:
        stopping, reason = True, "start_unconfirmed"
    explicit_stop = bool(not automatic_enabled or protection or stop_reason)
    off_edge = was_enabled and not automatic_enabled
    # Eine bereits bestätigte WW-Grundstellung braucht auch bei Nutzer-Aus
    # keinen identischen Schreibbefehl. Ein eigener Boost wird weiter quittiert.
    baseline_observed = bool(channel == "ww" and mode is not None and _baseline_matches(
        state, channel, mode, (observation or {}).get("target_c")))
    return_target_observed = bool(baseline_observed or (
        channel == "ww" and mode == 0 and state["baseline_target_c"] is not None
        and _number((observation or {}).get("target_c"))
        and abs(observation["target_c"] - state["baseline_target_c"]) <= 0.1))
    if (release_on_baseline_readback and return_target_observed and state["possible_effect"]
            and stopping and (stop_allowed or not automatic_enabled or protection or start_failed)):
        # Im Ferienmodus genügt die frische Rücklesung des Rückkehrziels.
        # Keine identische Schreibung und keine erfundene Transportquittung.
        _clear_effect(state, "boost_return_readback_confirmed")
        state["stop_reason"] = reason
        return result, None
    withdrawal_needed = bool(state["possible_effect"] or (explicit_stop and not baseline_observed and (
        off_edge or state["state"] != "frei" or state["withdraw_ack_ts"] is None
        or (mode is not None and not _baseline_matches(
            state, channel, mode, (observation or {}).get("target_c"))))))
    exhausted = state["state"] == "aufgegeben" and state["alarm"] and not off_edge
    if (withdrawal_needed and not exhausted and state["state"] not in (
            "ruecknahme_offen", "ruecknahme_gesendet")
            and stopping and (stop_allowed or not automatic_enabled or protection or start_failed)):
        if channel == "ww" and state["baseline_target_c"] is None:
            # Ohne konfigurierte Untergrenze niemals einen Temperaturwert erfinden.
            _abandon(state, now_s, "withdrawal_baseline_missing")
            state["alarm"] = True
            return result, None
        hold = max(600.0, signal_hold_s, user_hold_s) if protection else user_hold_s
        state.update({"state": "ruecknahme_offen", "withdraw_requested_ts": now_s,
                      "withdraw_ack_ts": None, "attempts": 0,
                      "next_attempt_ts": now_s, "inflight": None,
                      "stop_reason": reason,
                      "stop_until": max(state["stop_until"], now_s + hold)})
    if state["state"] in ("ruecknahme_offen", "ruecknahme_gesendet"):
        if (now_s - state["withdraw_requested_ts"] >= REVOKE_TIMEOUT_S
                or (state["attempts"] >= MAX_ATTEMPTS and now_s >= state["next_attempt_ts"])):
            _abandon(state, now_s, "withdrawal_unresolved")
        elif state["inflight"] is None and now_s >= state["next_attempt_ts"]:
            return result, _reserve(state, channel, "withdraw")
        return result, None
    if (state["state"] == "gesendet" and wanted and same and not stopping
            and state["inflight"] is None and mode is not None
            and now_s >= state["next_attempt_ts"]
            and state["start_attempts"] < MAX_ATTEMPTS):
        state["state"] = "vorbereitet"
        return result, _reserve(state, channel, "start")
    baseline_ready = _baseline_matches(state, channel, mode, (observation or {}).get("target_c"))
    if (state["state"] == "frei" and wanted and automatic_enabled and not protection
            and not (same and state["stop_reason"] == "start_unconfirmed")
            and not state["clock_fault"] and stop_reason is None
            and now_s >= max(state["stop_until"], state["next_attempt_ts"]) and (mode == 0 or baseline_ready)):
        generation = state["generation"]
        stop_until = state["stop_until"]
        sample_ts = state["last_sample_ts"]
        baseline_target = state["baseline_target_c"]
        state.update(_channel())
        state.update({key: desired[key] for key in ("owner", "request_id", "revision", "target_c")})
        state.update({"state": "vorbereitet", "prepared_ts": now_s,
                      "possible_effect": True, "transport_uncertain": True,
                      "generation": generation,
                      "last_sample_ts": sample_ts, "stop_until": stop_until,
                      "baseline_target_c": baseline_target})
        return result, _reserve(state, channel, "start")
    return result, None


def note_transport(checkpoint, action, *, now_s, outcome):
    """Meldet nur die echte Ausgabe; Cachetreffer sind ausdrücklich kein ACK.

    ``write_failed`` kann einen Teilwrite enthalten und zählt als Versuch.
    ``connect_failed`` und ``cache_hit`` verbrauchen kein Write-Budget, werden
    aber gedrosselt. Der erste erfolgreiche Rücknahmeanker bleibt fest.
    """
    if outcome not in ("written", "write_failed", "connect_failed", "cache_hit"):
        raise ValueError("invalid_transport_outcome")
    result = _copy(checkpoint, now_s)
    if not isinstance(action, dict) or action.get("channel") not in CHANNELS:
        raise ValueError("invalid_action")
    state = result["channels"][action["channel"]]
    if not _action_valid(action, action["channel"]) or state["inflight"] != action:
        return result  # Doppelte oder verspätete Note gehört nicht mehr zum Auftrag.
    now_s = max(now_s, result["updated_ts"])
    state["inflight"] = None
    state["next_attempt_ts"] = now_s + RETRY_S
    if action["kind"] == "start":
        if outcome in ("written", "write_failed"):
            state["start_attempts"] += 1
            state["start_attempt_ts"] = now_s
        if outcome == "connect_failed" and state["start_attempts"] == 0:
            _clear_effect(state, "start_not_transmitted")
        else:
            state["state"] = "gesendet"
            if outcome == "written" and state["sent_ts"] is None:
                state["sent_ts"] = now_s
            state["transport_uncertain"] = outcome != "written"
            state["diagnostic"] = None if outcome == "written" else "start_transport_uncertain"
    else:
        if outcome in ("written", "write_failed"):
            state["attempts"] += 1
        if outcome == "written":
            if state["withdraw_ack_ts"] is None:
                state["withdraw_ack_ts"] = now_s
            state["state"] = "ruecknahme_gesendet"
        else:
            state["diagnostic"] = "withdrawal_" + outcome
    return result


def resume_checkpoint(checkpoint, *, now_s):
    """Übernimmt einen Neustart ohne positiven Replay oder fiktiven ACK.

    Ein vor IO gesicherter, unquittierter Versuch könnte ausgeführt worden sein.
    Ein solcher Rücknahmeversuch wird vorsichtig gegen das Budget gerechnet.
    """
    result = _copy(checkpoint, now_s)
    now_s = max(now_s, result["updated_ts"])
    for state in result["channels"].values():
        # Nach Neustart nur Statusproben der neuen Prozessgeneration annehmen.
        # So reicht ein sparsamer Persistenz-Heartbeat auch bei neueren
        # ausschließlich im RAM gesehenen Kanalframes.
        state["last_sample_ts"] = max(state["last_sample_ts"] or 0.0,
                                      result["updated_ts"])
        action = state["inflight"]
        if action is None:
            continue
        if (action["kind"] == "withdraw" and state["owner"] == "pv"
                and state["possible_effect"] and not state["protection_counted"]
                and state["stop_reason"] in (
                    "hardware_fault", "heat_source_limit", "electrical_profile_exceeded")):
            # Der vor IO gesicherte Entzug könnte vor dem Absturz gesendet
            # worden sein. Vorsichtig mitzählen, ausdrücklich ohne Write-ACK.
            daily = result["pv_day_protection"]
            if daily["day"] is None:
                daily["day"] = date.fromtimestamp(now_s).isoformat()
            reason = state["stop_reason"]
            daily["last_event"] = "uncertain:%s:%s:%s" % (
                state["request_id"], state["revision"], reason)
            daily["counts"][reason] = daily["counts"].get(reason, 0) + 1
            daily["blocked"] = any(count >= 2 for count in daily["counts"].values())
            for peer in result["channels"].values():
                if (peer["owner"] == "pv" and peer["possible_effect"]
                        and (peer["request_id"], peer["revision"])
                        == (state["request_id"], state["revision"])):
                    peer["protection_counted"] = True
        state["inflight"] = None
        state["next_attempt_ts"] = max(state["next_attempt_ts"], now_s + RETRY_S)
        if action["kind"] == "start":
            state["start_attempts"] = min(MAX_ATTEMPTS, state["start_attempts"] + 1)
            state["start_attempt_ts"] = now_s
            state["state"] = "gesendet"
            state["transport_uncertain"] = True
            state["diagnostic"] = "start_transport_uncertain_after_restart"
        else:
            state["attempts"] = min(MAX_ATTEMPTS, state["attempts"] + 1)
            state["diagnostic"] = "withdrawal_transport_uncertain_after_restart"
    return result


def migrate_legacy(command, *, now_s, observation=None):
    """Übernimmt konkrete alte PV-Intents; unbekannte Historie bleibt gesperrt.

    Ein behaupteter Altabschluss ist kein Schreib-ACK. Bei frischem Auto darf
    er als bereits extern beendeter Altauftrag gelten; aktive konkrete Ziele
    bleiben dagegen rücknehmbar. Preis-/Pre-Dump-Flags ohne Kanalziel werden
    nicht zu Besitzrechten. ``observation`` ist optional je Kanal strukturiert.
    """
    result = new_checkpoint(now_s)
    valid = (isinstance(command, dict)
             and command.get("schema") == "heatpump_pv_command_state_v1"
             and isinstance(command.get("request_id"), str)
             and 0 < len(command["request_id"]) <= 160
             and type(command.get("revision")) is int and command["revision"] >= 0
             and _number(command.get("prepared_ts"), minimum=0.000001)
             and command["prepared_ts"] <= now_s
             and isinstance(command.get("channels"), dict)
             and bool(command["channels"])
             and set(command["channels"]).issubset(CHANNELS))
    if valid:
        issued = command.get("issued_ts")
        valid = (issued is None or (_number(issued, minimum=0.000001)
                                   and command["prepared_ts"] <= issued <= now_s))
    if valid:
        acknowledged = command.get("acknowledged_channels", {})
        valid = isinstance(acknowledged, dict) and set(acknowledged).issubset(CHANNELS)
    if valid:
        for flag in ("confirmed", "readback_confirmed", "withdrawal_requested", "withdrawal_confirmed"):
            if flag in command and type(command[flag]) is not bool:
                valid = False
    if valid:
        for channel in command["channels"].values():
            if (not isinstance(channel, dict) or channel.get("active") is not True
                    or not _number(channel.get("target_c")) or channel["target_c"] > 100):
                valid = False
    if not valid:
        for state in result["channels"].values():
            _abandon(state, now_s, "legacy_ownership_unknown")
        return result
    for name, old in command["channels"].items():
        state = result["channels"][name]
        state.update({"state": "gesendet", "owner": "pv",
                      "request_id": command["request_id"], "revision": command["revision"],
                      "target_c": old["target_c"], "prepared_ts": command["prepared_ts"],
                      "possible_effect": True, "transport_uncertain": True,
                      "diagnostic": "legacy_intent_migrated"})
        ack = (command.get("acknowledged_channels") or {}).get(name)
        if (issued is not None and isinstance(ack, dict) and ack.get("active") is True
                and _number(ack.get("target_c"))
                and abs(ack["target_c"] - old["target_c"]) <= 0.1):
            state["sent_ts"] = issued
            state["transport_uncertain"] = False
        if command.get("withdrawal_requested") is True:
            state.update({"state": "ruecknahme_offen", "withdraw_requested_ts": now_s,
                          "stop_reason": "legacy_withdrawal", "next_attempt_ts": now_s})
        mode = _observe(state, (observation or {}).get(name), now_s, name)
        if command.get("withdrawal_confirmed") is True:
            if mode == 0 and state["last_sample_ts"] > (issued or command["prepared_ts"]):
                _clear_effect(state, "legacy_closed_auto_observed")
            elif state["state"] != "fremd":
                # Ein gleicher Sollwert nach behauptetem Altabschluss könnte
                # bereits einem Folgeauftrag gehören. Das alte Recht wird
                # deshalb weder wiederbelebt noch als sicher erledigt versteckt.
                _abandon(state, now_s, "legacy_closed_ownership_ambiguous")
    return result
