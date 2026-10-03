"""Ein serialisierter Energy-Manager-Ausgang für beide Luxtronik-SHI-Kanäle.

Policy-Aufrufe liefern Absichten. Besitz, Rücknahme und Transportbelege werden
erst hier entschieden; Modus 0 schreibt niemals ein Temperaturregister.
"""

import copy
import math
import uuid
from datetime import datetime

try:
    from . import control_time
    from .Heat.price_boost import heating_boost_allowed, boost_outdoor_temperature
    from .heatpump_channel_owner import (
        CHANNELS, READBACK_AGE_S, advance, migrate_legacy, note_transport, raw_mode,
        resume_checkpoint, validate_checkpoint, suspend_ineffective_withdrawal,
    )
    from .heatpump_pv_state import (
        load_heatpump_channel_checkpoint, persist_heatpump_channel_checkpoint,
    )
except ImportError:
    import control_time
    from Heat.price_boost import heating_boost_allowed, boost_outdoor_temperature
    from heatpump_channel_owner import (
        CHANNELS, READBACK_AGE_S, advance, migrate_legacy, note_transport, raw_mode,
        resume_checkpoint, validate_checkpoint, suspend_ineffective_withdrawal,
    )
    from heatpump_pv_state import (
        load_heatpump_channel_checkpoint, persist_heatpump_channel_checkpoint,
    )


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


# Toleranz fehlender oder ungültiger E3DC-Livedaten, bevor ein laufender
# Auftrag zurückgenommen wird. Nutzeraufträge (WW-Sofort, Boost-Knopf, Timer)
# halten fünf Minuten, budgetgebundene Automatik (PV, Preis, Pre-Dump) so lange
# wie die Speicherzusage. Während der Lücke gibt es keinen neuen Start und
# keine Anhebung; Nutzer-Aus, Hardware-/Quellenschutz, Uhrstörung und eine
# Lücke der Wärmepumpen-Statusdaten wirken weiterhin sofort.
E3DC_GAP_USER_TOLERANCE_S = 300.0
E3DC_GAP_AUTO_TOLERANCE_S = 45.0


def e3dc_live_gap_update(previous, *, valid, clock_sample):
    """Verfolgt eine Lücke der E3DC-Livedaten ab dem ersten ungültigen Zyklus.

    Ein gültiger Zyklus beendet die Lücke. Ohne einen in diesem Prozess
    gesehenen gültigen Zyklus ist ihr Beginn unbekannt: ``elapsed_s`` ist dann
    ``None`` und gilt als überschrittene Toleranz. Ein Neustart des Energy
    Managers mitten in einer Lücke schenkt so keine Toleranz. Eine ungültige
    oder rückwärts laufende Uhr macht die Dauer ebenfalls unbekannt.
    """
    previous = previous if isinstance(previous, dict) else {}
    sample = clock_sample if isinstance(clock_sample, dict) else {}
    mono = number(sample.get("monotonic_ts"))
    wall = number(sample.get("wall_ts"))
    boot = sample.get("boot_id")
    if valid is True:
        return {"valid": True, "seen_valid": True, "since_mono": None,
                "since_ts": None, "boot_id": boot, "elapsed_s": 0.0}
    seen = previous.get("seen_valid") is True
    clock_ok = bool(sample.get("valid") is True and mono is not None)
    if previous.get("valid") is False:
        since = number(previous.get("since_mono"))
        since_ts = number(previous.get("since_ts"))
        if previous.get("boot_id") != boot:
            since = None
    else:
        since = mono if seen and clock_ok else None
        since_ts = wall
    elapsed = None
    if since is not None and clock_ok and mono >= since:
        elapsed = mono - since
    else:
        since = None
    return {"valid": False, "seen_valid": seen, "since_mono": since,
            "since_ts": since_ts, "boot_id": boot, "elapsed_s": elapsed}


def restart_anchor_ts(command_stop_ts, *, observation_valid, compressor_stop_ts):
    """Anker der Wiedereinschaltsperre für alle Startwege.

    Mit gültiger Verdichterbeobachtung zählt der gemessene Verdichterstillstand.
    Ist sie ungültig oder in diesem Prozess noch kein Stillstand gemessen, gilt
    konservativ der spätere Zeitpunkt aus Rücknahme-Quittung und letztem
    bekanntem Stillstand.
    """
    command = number(command_stop_ts) or 0.0
    measured = number(compressor_stop_ts) or 0.0
    if observation_valid is True and measured > 0.0:
        return measured
    return max(command, measured)


def restored_stop_ts(value, *, now_ts):
    """Zeitstempel eines Stillstands aus dem Restart-Zustand.

    Übernommen wird nur eine endliche Zahl größer 0. Ein Wert aus der Zukunft,
    etwa nach einem Uhrsprung, gilt als jetzt, damit eine Wiedereinschaltsperre
    nie verkürzt wird. Sonst 0.0 (unbekannt).
    """
    value = number(value)
    now = number(now_ts)
    if value is None or value <= 0.0 or now is None:
        return 0.0
    return min(value, now)


def restored_restart_anchor_ts(saved, *, now_ts):
    """Anker der Wiedereinschaltsperre nach einem Neustart, bis ein Stillstand gemessen ist.

    Ein gespeicherter Stillstand ist in diesem Prozess nicht selbst gemessen und
    zählt deshalb nur zusammen mit der Rücknahme-Quittung: maßgeblich ist der
    spätere Zeitpunkt. Lief der Verdichter beim Speichern (gemeldeter Lauf oder
    Start nach dem letzten Stillstand), lag der Stillstand frühestens beim
    Speicherzeitpunkt; ohne lesbaren Speicherzeitpunkt gilt jetzt. Die Sperre
    wird so gegenüber der Rücknahme-Quittung nie verkürzt.
    """
    saved = saved if isinstance(saved, dict) else {}
    now = number(now_ts)
    if now is None:
        return 0.0
    command = restored_stop_ts(saved.get("wp_last_pv_boost_stop_ts"), now_ts=now)
    stop = restored_stop_ts(saved.get("wp_compressor_last_stop_ts"), now_ts=now)
    start = restored_stop_ts(saved.get("wp_compressor_last_start_ts"), now_ts=now)
    anchor = max(command, stop)
    if saved.get("wp_compressor_running") is True or start > stop:
        try:
            saved_ts = restored_stop_ts(datetime.fromisoformat(str(saved.get("ts"))).timestamp(), now_ts=now)
        except (TypeError, ValueError, OverflowError, OSError):
            saved_ts = 0.0
        anchor = max(anchor, saved_ts or now)
    return anchor


def channel_observations(status):
    """Nur unverfälschte Kanalwerte; ein ungültiger Nachbarkanal sperrt nicht mit."""
    status = status if isinstance(status, dict) else {}
    valid = status.get("SHI_Source_valid") is True
    def target(name):
        value = number(status.get("%s_Setpoint_Raw" % name.upper()))
        return value if value is not None and 0 <= value <= 100 else None
    return {name: {
        "valid": valid,
        "sample_ts": number(status.get("SHI_Source_ts")),
        "raw_mode": status.get("SHI_%s_Mode_Raw" % name.upper()),
        "target_c": target(name),
    } for name in CHANNELS}


class LuxtronikChannelController:
    """Prozessgebundener Adapter; genau eine Instanz gehört zur EM-Hauptschleife."""

    def __init__(self, legacy_command=None, legacy_state=None, *, clock_sample=None, directory=None):
        self.directory = directory
        self.clock = copy.deepcopy(clock_sample or control_time.sample())
        self.now_s = max(0.000001, number(self.clock.get("wall_ts")) or 0.000001)
        loaded = load_heatpump_channel_checkpoint(directory=directory)
        self.load_status = loaded.get("status", "untrusted")
        checkpoint = validate_checkpoint(loaded.get("checkpoint"))
        if self.load_status == "valid" and checkpoint:
            # Eine Offline-Wanduhrdifferenz verkürzt keine Sperre. Verbleibende
            # Fristen werden nach Neustart konservativ erneut ab jetzt gehalten.
            saved_ts = checkpoint["updated_ts"]
            for state in checkpoint["channels"].values():
                for key in ("stop_until", "next_attempt_ts"):
                    state[key] = max(state[key], self.now_s + max(0.0, state[key] - saved_ts))
            checkpoint = resume_checkpoint(checkpoint, now_s=self.now_s)
        else:
            # Fehlend ist kein leerer Anfang. Konkrete Altintents migrieren;
            # unbekannte Flags/Dateien brauchen zunächst einen frischen Auto-Beleg.
            checkpoint = migrate_legacy(
                legacy_command if self.load_status == "missing" else None,
                now_s=self.now_s,
            )
            checkpoint = resume_checkpoint(checkpoint, now_s=self.now_s)
        self.checkpoint = checkpoint
        self.clock_fault = not bool(self.clock.get("valid"))
        self.requests = {}
        self.pending_offers = {}
        self.holiday_since_ts = {}
        self.last_result = {}
        self.last_observed_ww_target_c = None
        self.persisted = self._persist()

    def _persist(self):
        return bool(persist_heatpump_channel_checkpoint(
            self.checkpoint, directory=self.directory, now_s=self.now_s))

    def _time(self, current):
        elapsed = control_time.elapsed_contract(self.clock, current)
        delta = elapsed.get("elapsed_s", 0.0)
        wall_delta = elapsed.get("wall_delta_s")
        # Wanduhrsprünge sind keine abgelaufene Mindestlauf-/Wiedereinschaltzeit.
        if (not elapsed.get("known") or wall_delta is None
                or abs(wall_delta - delta) > 5.0):
            self.clock_fault = True
        if elapsed.get("known"):
            self.now_s += delta
        self.clock = copy.deepcopy(current)
        if self.clock_fault:
            for state in self.checkpoint["channels"].values():
                state["clock_fault"] = True
                state["diagnostic"] = "control_clock_discontinuity"
        return self.now_s

    def pv_command(self, previous=None):
        """Projiziert allein belegte Kanalzustände auf den bestehenden Speichervertrag."""
        previous = previous if isinstance(previous, dict) else {}
        channels = self.checkpoint["channels"]
        active = [value for value in channels.values()
                  if value["owner"] == "pv" and value["possible_effect"]]
        identity = active[0] if active else previous
        if not identity.get("request_id"):
            return copy.deepcopy(previous)
        matching = {name: state for name, state in channels.items()
                    if state["owner"] == "pv"
                    and state["request_id"] == identity.get("request_id")
                    and state["revision"] == identity.get("revision")}
        same_previous = bool(previous.get("request_id") == identity["request_id"]
                             and previous.get("revision") == identity.get("revision"))
        result = copy.deepcopy(previous) if same_previous else {}
        requested_channels = copy.deepcopy(result.get("channels") or {})
        requested_channels.update({name: {"active": True, "target_c": state["target_c"]}
                                   for name, state in matching.items()})
        if not requested_channels:
            return result
        # Ein haltbar jüngerer Eigenauftrag setzt zwingend die terminale
        # Rückgabe des vorigen voraus. So reaktiviert ein Absturz zwischen
        # neuem Kanalcheckpoint und altem PV-Export keine alten Besitzrechte.
        old_anchor = number(result.get("prepared_ts")) or number(result.get("issued_ts"))
        completion = {}
        for name in requested_channels:
            state = channels.get(name) or {}
            if name in matching:
                terminal = bool(not state["possible_effect"] and state["state"] in ("frei", "fremd"))
                completion[name] = {"state": state["state"], "terminal": terminal,
                                    "reason": state["diagnostic"],
                                    "withdraw_ack_ts": state["withdraw_ack_ts"]}
            else:
                newer_owner = bool(old_anchor is not None and state.get("prepared_ts") is not None
                                   and state["prepared_ts"] > old_anchor
                                   and (state.get("request_id"), state.get("revision"))
                                   != (identity.get("request_id"), identity.get("revision")))
                completion[name] = {"state": state.get("state"), "terminal": newer_owner,
                                    "reason": "subsequent_owned_request" if newer_owner else "old_request_unresolved",
                                    "withdraw_ack_ts": None}
        prepared = [state["prepared_ts"] for state in matching.values()]
        sent = [state["sent_ts"] for state in matching.values() if state["sent_ts"] is not None]
        if old_anchor is not None:
            prepared.append(old_anchor)
        old_sent = number(result.get("issued_ts"))
        if old_sent is not None:
            sent.append(old_sent)
        if not prepared:
            return result
        acknowledged = copy.deepcopy(result.get("acknowledged_channels") or {})
        acknowledged.update({name: {"active": True, "target_c": state["target_c"]}
                             for name, state in matching.items() if state["sent_ts"] is not None})
        result.update({
            "schema": "heatpump_pv_command_state_v1",
            "request_id": identity["request_id"], "revision": identity["revision"],
            "prepared_ts": min(prepared), "issued_ts": min(sent) if sent else None,
            "channels": requested_channels, "acknowledged_channels": acknowledged,
            "confirmed": bool(len(matching) == len(requested_channels)
                              and all(state["state"] == "eigen_aktiv" for state in matching.values())),
            "readback_confirmed": bool(result.get("readback_confirmed")
                                      or (len(matching) == len(requested_channels)
                                          and all(state["confirmed_ts"] is not None for state in matching.values()))),
            "withdrawal_requested": bool(result.get("withdrawal_requested")
                                        or any(state["withdraw_requested_ts"] is not None for state in matching.values())),
            "withdrawal_confirmed": all(value["terminal"] for value in completion.values()),
            "channel_completion": completion,
        })
        return result

    def _desired(self, owner, channel, target, *, identity=None, grant=True, purpose=""):
        target = number(target)
        if target is None or not 0 <= target <= 100:
            return None
        old = self.checkpoint["channels"][channel]
        if identity is None:
            key = (owner, channel, target, purpose)
            if (old["owner"] == owner and old["target_c"] == target and old["possible_effect"]
                    and (not purpose or str(old["request_id"]).startswith(purpose + ":"))):
                identity = old
            else:
                identity = self.requests.setdefault(key, {"request_id": (purpose + ":" if purpose else "") + uuid.uuid4().hex, "revision": 0})
        return {"owner": owner, "request_id": identity.get("request_id"),
                "revision": identity.get("revision", 0), "target_c": target,
                "valid": True, "grant": bool(grant)}

    def cycle(self, ctx, wp, *, clock_sample=None):
        now = self._time(clock_sample or control_time.sample())
        day = self.checkpoint["pv_day_protection"]
        today = datetime.fromtimestamp(now).date().isoformat()
        if day["day"] is None or today > day["day"]:
            day.update({"day": today, "counts": {}, "last_event": None, "blocked": False})
        observations = channel_observations(ctx.get("wp_status"))
        # Die bestehende Moduserkennung in main() wertet die gemeldeten
        # Heizungs-/WW-Modi aus; SHI-Modus 0 allein bedeutet keine Ferien.
        wp_holiday = ctx.get("wp_is_vacation") is True
        ww_observation = observations["ww"]
        sample_ts = number(ww_observation.get("sample_ts"))
        if (not self.checkpoint["channels"]["ww"]["possible_effect"]
                and ww_observation["valid"] and sample_ts is not None
                and 0 <= now - sample_ts <= 45.0
                and ww_observation["target_c"] is not None):
            self.last_observed_ww_target_c = ww_observation["target_c"]
        offers = list(getattr(wp, "channel_intents", []))
        desired = {name: None for name in CHANNELS}
        automatic = ctx.get("AUTO_MODE") == 1
        user_off = "manual_user_off" in (ctx.get("heatpump_positive_output_block_reasons") or [])
        pv_output = ctx.get("heatpump_pv_output") or {}
        pv_demand = ctx.get("heatpump_pv_contract") or {}
        pv_state = ctx.get("heatpump_pv_state") or {}
        manual = ctx.get("manual_boost_command") or {}
        manual_on = bool(manual.get("valid") and manual.get("action") == "on"
                         and manual.get("schema") == "manual_heatpump_command_v1")
        ww_state = ctx.get("manual_ww_sofort_state") or {}
        ww_pending = bool(ctx.get("manual_ww_requested_before_io"))
        ww_active = bool(ww_state.get("active") or ww_state.get("pending"))
        blocks = set(ctx.get("heatpump_positive_output_block_reasons") or [])
        hard = bool(ctx.get("heatpump_signal_typed_protection_stop")
                    or blocks.intersection({"manual_source_temperature_stop", "manual_low_soc_stop",
                                            "pre_control_independent_safety_stop", "independent_safety_stop"}))
        protection_reason = str(pv_output.get("protection_reason") or pv_demand.get("protection_reason") or "")
        # Eine abgelaufene E3DC-Datenlücke meldet der PV-Vertrag nur für seinen
        # eigenen Auftrag. Je Kanal gilt unten die Toleranz seines Besitzers.
        e3dc_scoped = bool(protection_reason == "invalid_control_data"
                           and pv_output.get("protection_scope") == "e3dc_live_gap")
        if protection_reason not in ("", "user_off") and not e3dc_scoped:
            hard = True
        gap = ctx.get("e3dc_live_gap") if isinstance(ctx.get("e3dc_live_gap"), dict) else {}
        e3dc_gap = gap.get("valid") is False
        gap_elapsed = number(gap.get("elapsed_s")) if e3dc_gap else None

        def gap_tolerance(state):
            owner_class = state["owner"] if state["possible_effect"] else None
            return E3DC_GAP_USER_TOLERANCE_S if owner_class == "manual" else E3DC_GAP_AUTO_TOLERANCE_S
        status_fresh = bool((ctx.get("wp_status") or {}).get("valid") is True
                            and (ctx.get("wp_status") or {}).get("source_fresh") is True)
        ww_baseline_readback_fresh = bool(
            status_fresh and ww_observation["valid"] and sample_ts is not None
            and 0 <= now - sample_ts <= READBACK_AGE_S
            and raw_mode(ww_observation["raw_mode"]) is not None
            and ww_observation["target_c"] is not None
            and (self.checkpoint["channels"]["ww"]["last_sample_ts"] is None
                 or sample_ts > self.checkpoint["channels"]["ww"]["last_sample_ts"]))
        if not status_fresh or self.clock_fault:
            hard = True
        # Die vorhandene Policy entscheidet, ob Angebote fachlich zulässig sind.
        # Der Automat entscheidet anschließend unabhängig für jeden Kanal über
        # Übergabe und Rücknahme. Ein Wunsch ist niemals Fremdbesitz.
        if pv_output.get("start") or pv_output.get("keep"):
            pv_identity = (pv_state.get("command") or {}) if pv_output.get("keep") else pv_demand
            for name in CHANNELS:
                offer = (pv_output.get("channels") or {}).get(name) or {}
                if offer.get("active"):
                    desired[name] = self._desired("pv", name, offer.get("target_c"), identity=pv_identity)
        # Bestehende Nicht-PV-Aufträge werden nur von ihrer aktuellen Policy
        # gehalten. Caches oder alte globale Boostflags erzeugen keinen Besitz.
        owner_live = {
            "price": bool(ctx.get("price_heatpump_start_requested") or ctx.get("pre_pause_active") or ctx.get("pv_pause_active")),
            "predump": bool(ctx.get("predump_heatpump_active")),
            "manual": manual_on or ww_active,
        }
        pv_live = bool((pv_output.get("start") or pv_output.get("keep")) and not pv_output.get("withdraw"))
        live_request = {}
        for name, state in self.checkpoint["channels"].items():
            current_request = owner_live.get(state["owner"])
            if state["owner"] == "manual":
                timer_owned = str(state["request_id"]).startswith("timer:")
                current_request = (bool(ctx.get("WW_TIMER_ENABLE"))
                                   and number(ctx.get("ww_timer_target_c")) == state["target_c"]
                                   if timer_owned else manual_on or (name == "ww" and ww_active))
            live_request[name] = bool(
                pv_live and ((pv_output.get("channels") or {}).get(name) or {}).get("active")
                if state["owner"] == "pv" else current_request)
            if state["possible_effect"] and state["owner"] != "pv" and current_request:
                if not str(state["request_id"]).startswith("timer:") or desired[name] is None:
                    desired[name] = self._desired(state["owner"], name, state["target_c"], identity=state)
        requested = {
            "price": bool(ctx.get("price_heatpump_start_requested") or ctx.get("pre_pause_active") or ctx.get("pv_pause_active")),
            "predump": bool(ctx.get("predump_heatpump_active")),
            "manual": bool(manual_on or ww_active),
            "timer": bool(ctx.get("WW_TIMER_ENABLE")),
        }
        for key, pending in list(self.pending_offers.items()):
            if not requested.get(pending["owner"]):
                del self.pending_offers[key]
        for offer in offers:
            key = (offer["owner"], offer["channel"])
            if offer["owner"] in requested:
                if offer["mode"] == 1:
                    self.pending_offers[key] = copy.deepcopy(offer)
                else:
                    self.pending_offers.pop(key, None)
            elif offer["owner"] == "release" and offer["mode"] == 0:
                for oldkey in list(self.pending_offers):
                    if oldkey[1] == offer["channel"] and oldkey[0] in ("price", "predump"):
                        del self.pending_offers[oldkey]
        # Sperrt der Boost-Mindest-SoC den Start, endet der manuelle Boost
        # regulär oder ist sein Auftrag abgelaufen, startet ein vorgemerktes
        # manuelles Angebot keinen Kanal, auf dem der Boost nicht schon läuft.
        if blocks.intersection({"manual_low_soc_start_blocked", "manual_low_soc_release",
                                "manual_command_expired"}):
            def manual_boost_running(channel):
                own = self.checkpoint["channels"][channel]
                return bool(own["possible_effect"] and own["owner"] == "manual"
                            and str(own["request_id"]).startswith("manual:"))
            for key in [key for key in self.pending_offers
                        if key[0] == "manual" and not manual_boost_running(key[1])]:
                del self.pending_offers[key]
            offers = [offer for offer in offers
                      if not (offer["owner"] == "manual" and offer["mode"] == 1
                              and not manual_boost_running(offer["channel"]))]
        offers = list(self.pending_offers.values()) + offers
        priority = {"pv": 0, "predump": 1, "price": 2, "manual": 3}
        def rank(value):
            return -1 if str(value.get("request_id")).startswith("timer:") else priority[value["owner"]]
        for offer in offers:
            name, source = offer["channel"], offer["owner"]
            owner = "manual" if source == "timer" else source
            if source == "release" and offer["mode"] == 0:
                if desired[name] and desired[name]["owner"] != "manual":
                    desired[name] = None
                continue
            if owner not in priority:
                continue
            # PV-Ausgaben stammen ausschließlich aus dem identitätsgebundenen
            # Grant. Die nachgelagerte Absicht darf keine neue UUID erfinden.
            if owner == "pv":
                if offer["mode"] == 0 and desired[name] and desired[name]["owner"] == "pv":
                    desired[name] = None
                continue
            # Der Komforttimer ist eine Grundstellung, kein positiver Auftrag.
            # Seine Absicht wird unten durch denselben Kanalausgang angewendet.
            if source == "timer":
                continue
            if offer["mode"] == 1:
                if desired[name] is None or priority[owner] >= rank(desired[name]):
                    desired[name] = self._desired(owner, name, offer["target_c"], purpose=offer.get("purpose") or source)
            elif desired[name] and ((source == "timer" and rank(desired[name]) < 0)
                                    or (source != "timer" and rank(desired[name]) <= priority[owner])):
                desired[name] = None
        tariff = ctx.get("heat_tariff_shift") or {}
        if tariff.get("commands_allowed") is True and tariff.get("target") in CHANNELS:
            target = tariff["target"]
            # Geschützte und höher priorisierte Aufträge behalten ihren Kanal.
            others = [s for s in self.checkpoint["channels"].values()
                      if s["possible_effect"] and s["owner"] != "tariff"]
            pending_peer = any(v["possible_effect"] for k, v in self.checkpoint["channels"].items() if k != target)
            if not others and not pending_peer and not any(desired.values()):
                desired[target] = self._desired("tariff", target, tariff.get("target_c"), identity=tariff)
        # Die Außentemperatur begrenzt HZ-Boosts, nicht explizite Pausen.
        # Der Zweck bleibt im Auftrag auch über Rücklesung und Neustart erhalten.
        if (desired['hz'] and desired['hz']['owner'] == 'price'
                and not str(desired['hz']['request_id']).startswith('price_pause:')
                and str((ctx.get('current_config') or {}).get('price_boost_enable', 0)).lower() in ('1', 'true', 'on')):
            outside = boost_outdoor_temperature((ctx.get('wp_data') or {}).get('Aussentemp_Mittel'))
            heating_limit = number(ctx.get('HEIZGRENZE_TEMP'))
            own = self.checkpoint['channels']['hz']
            running = (own['owner'] == 'price' and own['possible_effect']
                       and not str(own['request_id']).startswith('price_pause:'))
            if (outside is None or heating_limit is None or outside > heating_limit
                    or not heating_boost_allowed(ctx.get('current_config') or {}, outside, running=running)):
                desired['hz'] = None
        # WW-Sofort hat Vorrang vor HZ; sein bloßes Dateiflag ist jedoch keine
        # Vollmacht zur Übernahme eines fremden SHI-Sollwerts.
        if manual_on and not blocks.intersection({"manual_command_expired", "manual_source_temperature_stop", "manual_low_soc_stop",
                                                  "manual_low_soc_start_blocked", "manual_low_soc_release"}):
            if ctx.get("wp_write_allowed") and not any(offer["owner"] == "manual" for offer in offers):
                if number(ctx.get("at_mittel")) is not None and number(ctx.get("HEIZGRENZE_TEMP")) is not None:
                    summer = ctx["at_mittel"] > ctx["HEIZGRENZE_TEMP"]
                    if not summer:
                        desired["hz"] = self._desired("manual", "hz", ctx.get("CONF_HZ"), purpose="manual")
                    desired["ww"] = self._desired("manual", "ww", ctx.get("CONF_WWS") if summer else ctx.get("CONF_WWW"), purpose="manual")
        if ww_pending or ww_active:
            desired["hz"] = None
            if ww_state.get("active") and ctx.get("wp_write_allowed"):
                desired["ww"] = self._desired("manual", "ww", ww_state.get("target_c"), purpose="ww_immediate")
        # Der manuelle Gesamtboost nutzt die bestehende Softwarehysterese
        # auch zwischen den gedrosselten Policy-Aufrufen. WW-Sofort und der
        # explizite Komforttimer besitzen jeweils ihre eigene Thermopolitik.
        data = ctx.get("wp_data") or {}
        for name in CHANNELS:
            value = desired[name]
            if not value or value["owner"] != "manual" or not str(value["request_id"]).startswith("manual:"):
                continue
            actual = number(data.get("Ruecklauf_Ist" if name == "hz" else "Warmwasser_Ist"))
            if actual is not None:
                own = self.checkpoint["channels"][name]
                running = own["possible_effect"] and own["owner"] == "manual"
                if name == "ww":
                    running = running or (ctx.get("luxtronik_ww_runtime_contract") or {}).get("ww_running") is True
                if actual >= value["target_c"] or (not running and actual >= value["target_c"] - (2.0 if name == "hz" else 8.0)):
                    desired[name] = None
        if day["blocked"]:
            for name in CHANNELS:
                if desired[name] and desired[name]["owner"] == "pv":
                    desired[name] = None
        if user_off or not automatic or hard or wp_holiday:
            desired = {name: None for name in CHANNELS}
        if pv_output.get("withdraw"):
            for name in CHANNELS:
                if desired[name] and desired[name]["owner"] == "pv":
                    desired[name] = None
        restart_hold = max(0.0, number(ctx.get("WP_RESTART_BLOCK_MIN")) or 0.0) * 60.0
        minimum_run = max(0.0, number(ctx.get("WP_MIN_RUNTIME_MIN")) or 0.0) * 60.0
        last_start = number(ctx.get("wp_last_pv_boost_start_ts")) or 0.0
        physical_hold = bool(ctx.get("WP_TAKT_PROTECT") and last_start > 0
                             and now - last_start < minimum_run)
        signal_hold = bool((ctx.get("heatpump_positive_signal_window") or {}).get("minimum_signal_hold_active"))
        return_target = number(self.checkpoint["channels"]["ww"].get("pre_boost_target_c"))
        return_source = "pre_boost_readback"
        if return_target is None or not 0 <= return_target <= 100:
            return_target = number(ctx.get("WW_ECO"))
            return_source = "ww_eco_fallback"
        if return_target is None or not 0 <= return_target <= 100:
            return_target, return_source = None, "none"
        actions = []
        hold = {}
        gap_withdraw_at = {}
        ww_baseline_written_in_gap = False
        for name in CHANNELS:
            state = self.checkpoint["channels"][name]
            if (wp_holiday and not state["possible_effect"] and state["state"] in (
                    "ruecknahme_offen", "ruecknahme_gesendet")):
                self.holiday_since_ts.setdefault(name, max(now, self.checkpoint["updated_ts"]))
            else:
                self.holiday_since_ts.pop(name, None)
            if (wp_holiday or e3dc_gap) and not state["possible_effect"]:
                # Ferien lassen wirkungslose Kanäle unangetastet. In einer
                # E3DC-Lücke darf nur die WW-Grundstellung mit frischer
                # WP-Rücklesung weiter über den normalen Schreibschutz laufen.
                if wp_holiday:
                    self.checkpoint = suspend_ineffective_withdrawal(
                        self.checkpoint, name, now_s=now,
                        holiday_since_ts=self.holiday_since_ts.get(name))
                    self.persisted = self._persist()
                if not wp_holiday and desired[name] is not None:
                    # Ein neuer Auftrag wartet sichtbar auf gültige E3DC-Daten.
                    desired[name] = None
                    hold[name] = {"reason": "e3dc_live_gap", "until_ts": None}
                if wp_holiday:
                    continue
                if name == "ww":
                    if not ww_baseline_readback_fresh or self.clock_fault:
                        continue
                elif state["state"] not in ("ruecknahme_offen", "ruecknahme_gesendet"):
                    continue
            # Eine E3DC-Datenlücke nimmt einen laufenden Kanal erst nach der
            # Toleranz seines Besitzers zurück; eine unbekannte Dauer zählt als
            # abgelaufen. Ein Kanal ohne mögliche eigene Wirkung hat nichts
            # zurückzunehmen. Während der Lücke gibt es keinen neuen Start und
            # keine Anhebung.
            gap_expired = bool(e3dc_gap and state["possible_effect"]
                               and (gap_elapsed is None or gap_elapsed >= gap_tolerance(state)))
            # Speicherbelegung allein erhält den bestehenden Mindestlaufzeitschutz.
            # Schutzvetos und die eigene Datenlückentoleranz bleiben vorrangig.
            tariff_hard = bool(state["owner"] == "tariff" and (
                tariff.get("mode") != "active" or tariff.get("storage_safety_veto") is True
                or any(reason.endswith("_missing_or_blocked")
                for reason in tariff.get("blockers", [])
                if reason != "restart_free_missing_or_blocked"
                and not (reason == "storage_free_missing_or_blocked" and not e3dc_gap)
                and not (e3dc_gap and reason in (
                    "data_fresh_missing_or_blocked", "storage_free_missing_or_blocked")))))
            channel_hard = bool(hard or gap_expired or tariff_hard)
            if channel_hard:
                desired[name] = None
            elif e3dc_gap and desired[name] is not None:
                same_running = bool(state["possible_effect"] and all(
                    state[key] == desired[name][key] for key in ("owner", "request_id", "revision", "target_c")))
                if not same_running:
                    desired[name] = (self._desired(state["owner"], name, state["target_c"], identity=state)
                                     if state["possible_effect"] and live_request.get(name) else None)
                    hold[name] = {"reason": "e3dc_live_gap", "until_ts": None}
            if e3dc_gap and not channel_hard and state["possible_effect"]:
                gap_withdraw_at[name] = now + max(0.0, gap_tolerance(state) - (gap_elapsed or 0.0))
            # Das vor jedem Connect gelesene Nutzerflag schützt eine bereits
            # belegte, außerhalb dieses Automaten liegende WW-Einstellung.
            observed_mode = raw_mode(observations[name]["raw_mode"])
            user_channel_pending = bool(name == "ww" and ww_pending and state["owner"] != "manual")
            if (user_channel_pending and automatic and not user_off and not channel_hard
                    and (not observations[name]["valid"] or observed_mode is None)):
                actions.append({"channel": "ww", "outcome": "user_request_waits_for_ownership_readback"})
                continue
            external_manual_evidence = bool(
                ww_state.get("active") and ww_state.get("readback_confirmed")
                and (number(ww_state.get("started_ts")) or 0.0) > (state["prepared_ts"] or 0.0))
            manual_owned = bool(user_channel_pending and observed_mode in (1, 2)
                                and external_manual_evidence)
            own_signal_hold = bool(state["sent_ts"] is not None and now - state["sent_ts"] < 600.0)
            stop_allowed = not (physical_hold or signal_hold or own_signal_hold
                                or bool(ctx.get("heatpump_signal_manufacturer_cycle_hold"))
                                or bool(pv_output.get("hold_required")))
            if (ctx.get("heatpump_channel_stop_allowed") or {}).get(name) is False:
                stop_allowed = False
            if desired[name] and not state["possible_effect"]:
                # Die Wiedereinschaltsperre zählt ab dem gemessenen
                # Verdichterstillstand, für alle Startwege gleich.
                anchor = restart_anchor_ts(
                    ctx.get("wp_last_pv_boost_stop_ts"),
                    observation_valid=ctx.get("wp_compressor_observation_valid"),
                    compressor_stop_ts=ctx.get("wp_compressor_last_stop_ts"))
                takt_until = anchor + restart_hold
                takt_wait = bool(ctx.get("WP_TAKT_PROTECT") and anchor > 0 and now < takt_until)
                waits = [(takt_until, "restart_block")] if takt_wait else []
                if state["stop_until"] > now:
                    waits.append((state["stop_until"], "stop_until"))
                if waits:
                    until, reason = max(waits)
                    hold[name] = {"reason": reason, "until_ts": until}
                if (not ctx.get("wp_write_allowed") or takt_wait or self.clock_fault):
                    desired[name] = None
            # Der WW-Grundwert gilt wie ein eingebauter Timer unabhängig vom
            # Boost. Nur dessen eigene, physisch belegte Laufzeit schützt eine
            # Rücknahme; ein bloßes Signal oder ein alter Startzeitpunkt nicht.
            ww_pv_owned = bool(name == "ww" and (state["owner"] in ("pv", "tariff") or wp_holiday)
                               and state["possible_effect"])
            boost_left_s = 0.0
            # Die Laufzeit entscheidet erst bei entfallener Voraussetzung
            # über die Rücknahme, nicht während eines weiter gültigen Angebots.
            if ww_pv_owned and (channel_hard or desired[name] is None
                    or any(desired[name][key] != state[key] for key in ("owner", "target_c"))):
                sent = number(state.get("sent_ts"))
                started = number(ctx.get("wp_compressor_last_start_ts"))
                operating = (ctx.get("wp_data") or {}).get(
                    "Betriebsart", (ctx.get("wp_status") or {}).get("Betriebsart"))
                # Betriebsart 3 ist die normalisierte EVU-/Fremdsperre.
                grid_power = number(ctx.get("grid"))
                hardware_block = bool(
                    operating == 3 or self.clock_fault or not status_fresh or gap_expired
                    or ctx.get("heatpump_signal_typed_protection_stop")
                    or protection_reason not in ("", "user_off", "emergency_reserve")
                    or blocks.intersection({"manual_source_temperature_stop"})
                    or (grid_power is not None and grid_power > 2500.0)
                    or (grid_power is None and blocks.intersection({
                        "pre_control_independent_safety_stop", "independent_safety_stop"})))
                if (not hardware_block and not (state["owner"] == "tariff" and (channel_hard or user_off or not automatic)) and ctx.get("WP_TAKT_PROTECT")
                        and ctx.get("wp_compressor_observation_valid") is True
                        and ctx.get("wp_compressor_running_now") is True
                        and sent is not None and started is not None
                        and 0 < started <= now and sent <= now):
                    boost_left_s = max(0.0, minimum_run - (now - max(sent, started)))
                stop_allowed = boost_left_s <= 0.0
                if not stop_allowed:
                    # Nur den bereits eigenen WW-Auftrag halten. HZ und neue
                    # Starts behalten sämtliche bisherigen Schutzschranken.
                    pv_transfer = bool(state["owner"] == "tariff" and desired[name]
                                       and desired[name]["owner"] == "pv"
                                       and desired[name]["target_c"] == state["target_c"])
                    if not pv_transfer:
                        desired[name] = self._desired(state["owner"], name, state["target_c"], identity=state)
                    channel_hard = False
                    hold[name] = {"reason": "boost_min_runtime_hold",
                                  "until_ts": now + boost_left_s,
                                  "remaining_s": boost_left_s}
            baseline = number(ctx.get("WW_ECO")) if name == "ww" else None
            if name == "ww" and ctx.get("WW_TIMER_ENABLE"):
                baseline = number(ctx.get("ww_timer_target_c"))
            timer_offer = self.pending_offers.get(("timer", "ww")) if name == "ww" else None
            observed_target = number(observations[name].get("target_c"))
            timer_idle = bool(
                name == "ww" and desired[name] is None and not state["possible_effect"]
                and state["state"] in ("frei", "fremd", "aufgegeben") and not state["alarm"]
                and not ww_pending and not ww_active and not any(owner_live.values())
                and (ctx.get("wp_write_allowed") or user_off or not automatic or channel_hard)
                and (state["withdraw_ack_ts"] is None or now - state["withdraw_ack_ts"] >= 15.0)
                and not self.clock_fault and observations[name]["valid"]
                and observed_mode in (0, 1) and observed_target is not None
                and baseline is not None
                and (observed_mode != 1 or abs(observed_target - baseline) > 0.5))
            if (name == "ww" and desired[name] and not state["possible_effect"]
                    and observed_mode == 1 and observed_target is not None
                    and state["baseline_target_c"] is not None
                    and abs(observed_target - state["baseline_target_c"]) <= 0.1):
                # Ein Fensterwechsel darf die bisher bestätigte Grundstellung
                # nicht in Fremdbesitz verwandeln und den Booststart sperren.
                baseline = state["baseline_target_c"]
            if wp_holiday and name == "ww":
                # Der Boostwert selbst ist kein Rückkehrziel. Der vor dem
                # Versand gesicherte Wert bleibt über Neustarts erhalten.
                baseline = return_target
                if baseline is None:
                    hold[name] = {"reason": "ww_boost_return_target_missing", "until_ts": None}
                    continue
            old = copy.deepcopy(self.checkpoint)
            self.checkpoint, action = advance(
                self.checkpoint, name, now_s=now, observation=observations[name],
                desired=desired[name], stop_allowed=bool(stop_allowed),
                automatic_enabled=bool((automatic and not user_off) or boost_left_s > 0), protection=bool(channel_hard),
                user_hold_s=0.0 if timer_idle else restart_hold, signal_hold_s=600.0,
                manual_owned=bool(manual_owned and automatic and not user_off and not channel_hard),
                baseline_target_c=baseline,
                release_on_baseline_readback=bool(wp_holiday and name == "ww"),
                stop_reason=(None if boost_left_s > 0 else "user_off" if user_off else (protection_reason or "hard_protection") if hard else
                             "invalid_control_data" if channel_hard else
                             "timer_target" if timer_idle else
                             "policy_release" if desired[name] is None and any(
                                 offer["channel"] == name and offer["mode"] == 0
                                 for offer in offers) else None),
            )
            if (name == "ww" and action is not None and action["kind"] == "start"
                    and not old["channels"][name]["possible_effect"]):
                # Zusammen mit dem Startintent haltbar sichern, bevor IO läuft.
                self.checkpoint["channels"][name]["pre_boost_target_c"] = self.last_observed_ww_target_c
            self.persisted = self._persist()
            if action is None:
                continue
            if (action["kind"] == "start" or timer_idle) and not self.persisted:
                # Kein IO, also weder Transportnote noch behauptete mögliche
                # neue Wirkung. Ein Folgelauf muss den Intent erneut sichern.
                self.checkpoint = old
                actions.append({"action": action, "outcome": "intent_not_durable"})
                continue
            outcome = wp.dispatch_channel_action(action)
            if (e3dc_gap and name == "ww" and not old["channels"][name]["possible_effect"]
                    and action["kind"] == "withdraw" and outcome == "written"):
                ww_baseline_written_in_gap = True
            if (name == "ww" and outcome == "written" and timer_offer
                    and action["mode"] == 1 and action["target_c"] == timer_offer["target_c"]):
                # Nach erfolgreichem Anwenden entscheidet wieder die bestehende
                # Mismatch-/Heartbeat-Logik über eine neue Timerabsicht.
                self.pending_offers.pop(("timer", "ww"), None)
            completed_clock = getattr(wp, "last_channel_transport_sample", None)
            if isinstance(completed_clock, dict):
                now = self._time(completed_clock)
            self.checkpoint = note_transport(self.checkpoint, action, now_s=now, outcome=outcome)
            # Ein Entzug zählt kanalübergreifend einmal pro PV-Auftrag/Ursache.
            # Verbindungsfehler und Wiederholungen sind keine neue Schutzkante.
            prior = old["channels"][name]
            withdrawal_reason = self.checkpoint["channels"][name]["stop_reason"]
            if (action["kind"] == "withdraw" and outcome in ("written", "write_failed")
                    and prior["owner"] == "pv" and prior["possible_effect"]
                    and not prior["protection_counted"]
                    and withdrawal_reason in ("hardware_fault", "heat_source_limit", "electrical_profile_exceeded")):
                day = self.checkpoint["pv_day_protection"]
                day["last_event"] = "%s:%s:%s" % (prior["request_id"], prior["revision"], withdrawal_reason)
                day["counts"][withdrawal_reason] = day["counts"].get(withdrawal_reason, 0) + 1
                day["blocked"] = any(count >= 2 for count in day["counts"].values())
                for peer in self.checkpoint["channels"].values():
                    if (peer["owner"] == "pv" and peer["possible_effect"]
                            and (peer["request_id"], peer["revision"]) == (prior["request_id"], prior["revision"])):
                        peer["protection_counted"] = True
            self.persisted = self._persist()
            actions.append({"action": action, "outcome": outcome})
        wp.channel_intents = []
        # Diagnose: Grund und Ende einer Zurückhaltung je Kanal sowie der
        # Rücknahmezeitpunkt laufender Aufträge während einer E3DC-Datenlücke.
        self.last_result = {"checkpoint": copy.deepcopy(self.checkpoint), "actions": actions,
                            "desired": desired, "checkpoint_durable": self.persisted,
                            "load_status": self.load_status, "clock_fault": self.clock_fault,
                            "hold": hold,
                            "ww_boost_return_target_c": return_target,
                            "ww_boost_return_source": return_source,
                            "ww_baseline_suppressed_reason": ("wp_holiday_mode" if wp_holiday else
                                "e3dc_live_gap" if e3dc_gap and not ww_baseline_readback_fresh else None),
                            "ww_baseline_written_in_gap": ww_baseline_written_in_gap,
                            "ww_baseline_target_c": (number(ctx.get("ww_timer_target_c"))
                                if ctx.get("WW_TIMER_ENABLE") else number(ctx.get("WW_ECO"))),
                            "ww_boost_active": bool(self.checkpoint["channels"]["ww"]["owner"] == "pv"
                                and self.checkpoint["channels"]["ww"]["possible_effect"]),
                            "e3dc_gap": {"active": e3dc_gap, "elapsed_s": gap_elapsed,
                                         "withdraw_at_ts": gap_withdraw_at}}
        return copy.deepcopy(self.last_result)
