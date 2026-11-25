"""Hypothetischer heutiger Befehlsvertrag; kein Transport und keine Quittung.

Das Gedächtnis enthält ausschließlich hypothetische Vorgaben und Sendezeiten.
Bestätigter Readback wird bei der Initialisierung getrennt übernommen.
"""
from dataclasses import dataclass, replace
from math import isfinite
from .befehl_werte import (MODE_AUTO, MODE_IDLE, MODE_DISCH, MODE_CHRG, MODE_GRID,
                           EMS_POWER_SETTINGS_NONZERO_MIN_W)
from .kern_uebergabe import auftauen


@dataclass(frozen=True)
class Grenzen:
    max_charge_w: int
    max_discharge_w: int
    discharge_start_w: int = 0
    limits_used: bool = True
    bounded_zero_readback_max_w: int = 0


@dataclass(frozen=True)
class Teilvorgabe:
    kind: str
    target: Grenzen | tuple
    wuerde_senden: bool


@dataclass(frozen=True)
class Projektion:
    contract_version: int
    path: str
    mode: int
    value_w: int
    power_settings: Grenzen | None
    substeps: tuple[Teilvorgabe, ...]
    grund: str
    # Reale Evidenz bleibt ausdrücklich unbekannt, kein vorgetäuschtes Senden.
    attempted: bool = False
    issued: bool = False
    acknowledged: bool | None = None
    confirmed: bool = False
    hypothetical: bool = True
    rest_wh: float | None = None
    gueltig_bis_s: float | None = None
    ausgabe_dauer_s: float | None = None


@dataclass(frozen=True)
class Projektionszustand:
    modus: int = MODE_AUTO
    letzte_grid_zeit_s: float | None = None
    letzter_grid_wert_w: int | None = None
    grenzen: Grenzen | None = None
    max_laden_w: int | None = None
    max_entladen_w: int | None = None
    readback_bestaetigt: bool = False


def _watt(v):
    return max(0, int(round(v))) if type(v) in (float, int) and isfinite(v) and v >= 0 else None


def projektionsstart(uebergabe, eingang, vorher=None):
    # G5 bindet diesen Kontext je Zyklus. Das hypothetische Sendegedächtnis
    # bleibt dabei erhalten; Freigaben dürfen keine alte Gerätekappe nutzen.
    if vorher is not None:
        return replace(vorher, max_laden_w=_watt(eingang.max_laden.wert),
                       max_entladen_w=_watt(eingang.max_entladen.wert))
    diag = auftauen(uebergabe.power_settings)
    rb = diag.get("readback") or {}
    bestaetigt = diag.get("confirmed") is True
    grenzen = None
    if (bestaetigt and all(type(rb.get(key)) is int and rb[key] >= 0
            for key in ("max_charge_w", "max_discharge_w", "discharge_start_w"))
            and type(rb.get("limits_used")) is bool):
        grenzen = Grenzen(rb["max_charge_w"], rb["max_discharge_w"], rb["discharge_start_w"], rb["limits_used"])
    transaction = auftauen(uebergabe.rscp_request_transaction)
    mode = transaction.get("current_mode", MODE_AUTO)
    # Der heutige Manager legt current_mode nicht oben in der Transaktion
    # ab. Ein gesendeter oder belegter gehaltener SET_POWER-Unterbeleg bindet
    # den zuletzt angeforderten Modus, ohne eine Gerätewirkung zu behaupten.
    for name in ("set_power", "set_power_auto"):
        step = (transaction.get("substeps") or {}).get(name) or {}
        if step.get("issued") is True or step.get("retained") is True:
            mode = step.get("mode", mode)
    if mode not in (MODE_AUTO, MODE_IDLE, MODE_DISCH, MODE_CHRG, MODE_GRID):
        mode = MODE_AUTO
    return Projektionszustand(modus=mode, grenzen=grenzen,
        max_laden_w=_watt(eingang.max_laden.wert), max_entladen_w=_watt(eingang.max_entladen.wert),
        readback_bestaetigt=grenzen is not None)


def projizieren(stellgroesse, vorherige_projektion, jetzt):
    """(Soll, hypothetisches Gedächtnis, Zeit) → (Vorschlag, Folgezustand).

    Kein Parameter schreiben: unveränderte Kernvorgaben brauchen weiterhin
    die GRID-Auffrischung in jedem aktiven Dienstzyklus.
    """
    z = vorherige_projektion
    if type(jetzt) not in (float, int) or not isfinite(jetzt):
        raise ValueError("Projektionszeit fehlt")
    # Restmenge, Frist und Ausgabedauer eines endlichen Netzauftrags werden nur
    # transportiert; die Kappe rechnet allein der Kern (eine Stelle, kein zweites Mal).
    if stellgroesse.modus == "NETZLADEN" and stellgroesse.grid_soll_w is not None:
        wert = _watt(stellgroesse.grid_soll_w)
        if wert is None:
            return Projektion(3, "unbekannt", MODE_AUTO, 0, None, (), "GRID-Soll ungültig"), z
        neu = replace(z, modus=MODE_GRID,
            letzte_grid_zeit_s=jetzt,
            letzter_grid_wert_w=wert)
        return Projektion(3, "mode:GRID", MODE_GRID, wert, None,
            (Teilvorgabe("set_power", (MODE_GRID, wert), True),), "GRID, Auffrischung je aktivem Zyklus",
            rest_wh=stellgroesse.grid_rest_wh, gueltig_bis_s=stellgroesse.grid_gueltig_bis_s,
            ausgabe_dauer_s=stellgroesse.grid_dauer_s), neu
    # Endliche Entladung: derselbe SET_POWER-Vertrag mit DISCH, aufgefrischt je aktivem Zyklus wie GRID.
    if stellgroesse.modus == "ENTLADEN" and getattr(stellgroesse, "entlade_soll_w", None) is not None:
        wert = _watt(stellgroesse.entlade_soll_w)
        if wert is None:
            return Projektion(3, "unbekannt", MODE_AUTO, 0, None, (), "DISCH-Soll ungültig"), z
        neu = replace(z, modus=MODE_DISCH, letzte_grid_zeit_s=None, letzter_grid_wert_w=None)
        return Projektion(3, "mode:DISCH", MODE_DISCH, wert, None,
            (Teilvorgabe("set_power", (MODE_DISCH, wert), True),), "DISCH, Auffrischung je aktivem Zyklus"), neu
    # Quellengebundenes PV-Laden: derselbe SET_POWER-Vertrag mit CHRG, aufgefrischt je aktivem Zyklus.
    if stellgroesse.modus == "LADEN" and getattr(stellgroesse, "lade_soll_w", None) is not None:
        wert = _watt(stellgroesse.lade_soll_w)
        if wert is None:
            return Projektion(3, "unbekannt", MODE_AUTO, 0, None, (), "CHRG-Soll ungültig"), z
        neu = replace(z, modus=MODE_CHRG, letzte_grid_zeit_s=None, letzter_grid_wert_w=None)
        return Projektion(3, "mode:CHRG", MODE_CHRG, wert, None,
            (Teilvorgabe("set_power", (MODE_CHRG, wert), True),), "CHRG, Auffrischung je aktivem Zyklus"), neu
    charge, discharge = _watt(stellgroesse.ladegrenze_w), _watt(stellgroesse.entladegrenze_w)
    frei = stellgroesse.grenzen_frei
    if frei:
        if z.max_laden_w is None or z.max_entladen_w is None:
            return Projektion(3, "unbekannt", MODE_AUTO, 0, None, (), "Freigabegrenzen unbekannt"), z
        # Bestehender Freigabepfad: nicht unter 300 W, danach Gerätekappen.
        discharge = max(EMS_POWER_SETTINGS_NONZERO_MIN_W, z.max_entladen_w)
        charge = max(EMS_POWER_SETTINGS_NONZERO_MIN_W, z.max_laden_w, discharge)
        charge = min(charge, z.max_laden_w)
        discharge = min(discharge, z.max_entladen_w)
    else:
        # Datenfehler können nur eine schon aktive Entladesperre vorgeben.
        if charge is None:
            charge = z.grenzen.max_charge_w if z.grenzen is not None else z.max_laden_w
        if discharge is None:
            discharge = z.grenzen.max_discharge_w if z.grenzen is not None else z.max_entladen_w
    if charge is None or discharge is None:
        return Projektion(3, "unbekannt", MODE_AUTO, 0, None, (), "POWER_SETTINGS-Grenze unbekannt"), z
    target = Grenzen(charge, discharge, 0, not frei,
        EMS_POWER_SETTINGS_NONZERO_MIN_W if charge == 0 and not frei else 0)
    # Bestätigte bounded-zero-Readbacks sind Beleg einer Nullanforderung,
    # kein neuer positiver Ladeauftrag. Ohne Beleg bleibt der Zielwert 0.
    match = (z.grenzen is not None and z.grenzen.limits_used == target.limits_used
        and (abs(z.grenzen.max_charge_w - target.max_charge_w) < 50
             if charge != 0 else z.grenzen.max_charge_w == 0
             or 0 < z.grenzen.max_charge_w <= target.bounded_zero_readback_max_w)
        and abs(z.grenzen.max_discharge_w - discharge) < 50
        and abs(z.grenzen.discharge_start_w - target.discharge_start_w) < 50)
    steps = [Teilvorgabe("power_settings", target, not match)]
    if not frei and z.modus in (MODE_IDLE, MODE_DISCH, MODE_CHRG, MODE_GRID):
        steps.append(Teilvorgabe("set_power_auto", (MODE_AUTO, 0), True))
    # Freigabe beendet nur aktive Refreshes; AUTO 0 wäre ein Nullleistungsbefehl.
    neu = replace(z, modus=MODE_AUTO, grenzen=target, readback_bestaetigt=False,
                  letzte_grid_zeit_s=None, letzter_grid_wert_w=None)
    return Projektion(3, "auto_limit_release" if frei else "auto_limit_enabled",
        MODE_AUTO, max(EMS_POWER_SETTINGS_NONZERO_MIN_W, discharge), target,
        tuple(steps), "Grenzen frei" if frei else "POWER_SETTINGS"), neu


def unwetter_diagnose(ergebnis, eingang, aufnahme):
    status = auftauen(aufnahme.funktionsstatus).get("Unwetter") or {}
    a = eingang.auftrag
    wirksam = (ergebnis.abgedeckt and eingang.regelung_an is True
        and ergebnis.grundcode not in ("INSEL", "DATENFEHLER", "REGELUNG_AUS")
        and (a is not None and "Unwetter" in a.quelle
            and ergebnis.soll.modus in ("NETZLADEN", "HALTEN")
            or a is None and eingang.kurve is not None))
    return dict(konfiguriert=status.get("konfiguriert"),
        angefordert=status.get("angefordert"), wirksam=bool(status.get("angefordert") and wirksam),
        modus=ergebnis.soll.modus, grund=ergebnis.grundcode,
        abweichungen=["A0-S2-" + str(i) for i in range(1, 7)] if status.get("angefordert") else [])


def waerme_vertrag(ergebnis, eingang, jetzt):
    """Startfreigabe aus der Schutzliste; Stellwirkung aus der gültigen Absicht."""
    senke = next((v for v in eingang.verbraucher if v.art == "waermepumpe"
        and v.netzgedeckt and v.frist_s is not None and jetzt < v.frist_s
        and v.leistung.zahl(jetzt, 0, 1000000) is not None), None)
    # Der Kern kennzeichnet seine Abbrüche bereits als ungültiges Budget.
    veto = not ergebnis.budget.battery_w.gueltig
    # Reserve sperrt die Entladung, nicht die Netzversorgung der Wärmepumpe.
    frei = not veto and (not ergebnis.folgezustand.reserve_aktiv
        or ergebnis.soll.entladegrenze_w == 0)
    return dict(ts=jetzt, safety_free=bool(frei), safety_veto=veto,
        intent_revision=senke.intent_revision if senke else None,
        active=bool(frei and ergebnis.abgedeckt and senke is not None and senke.laeuft),
        hold_w=senke.leistung.wert if senke else None,
        discharge_cap_w=ergebnis.soll.entladegrenze_w,
        reason="netzgedeckt" if frei and senke is not None else ergebnis.grundcode,
        nicht_abgedeckt=["S4", "S5a", "S6"],
        abweichungen=["A0-S3-1", "A0-S3-2", "A0-S3-3"] if senke else [])
