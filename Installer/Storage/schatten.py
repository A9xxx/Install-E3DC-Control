"""Neue Speicherregelung im Vergleichsbetrieb: reine Auswertung ohne Ausgang.

Nur Datenwerte passieren diese Grenze. Der Dienst hält Geräte, Uhren und Dateien.
Stundenzeilen verwenden gemeinsame Spalten, damit vierzehn Tage klein bleiben.
"""
from dataclasses import dataclass, field, replace
from copy import deepcopy
import json
from math import isfinite, ceil
from . import kern as k, kern_projektion as p, kern_budget as b
from .kern_uebergabe import auftauen


SCHEMA = "speicherkern_schatten_v1"
AGGREGAT_SCHEMA = "speicherkern_schatten_aggregate_v1"
VERSION = 2
ZUSTAND_BYTES = 20000
AGGREGAT_BYTES = 100000
FUNKTIONEN = ("Pre-Dump", "Unwetter", "Wärme-Tarif", "Lastspitzen",
              "Direktvermarktung", "Preis-Boost", "Vorhaltung")
MODI = ("FREI", "KURVE", "HALTEN", "NETZLADEN", "SONDER", "ENTLADEN", "LADEN")
KLASSEN = ("gleich", "A0", "A1", "A2", "A3", "A4", "U")
# Die drei hinteren Spalten kamen ohne Versionssprung dazu; ältere Zeilen werden beim Laden aufgefüllt.
STUNDEN_SPALTEN = ("beginn", "tag", "zyklen", "vergleichbar", "klassen", "codes",
    "abweichung_laengste_s", "a3_a4", "abdeckung", "laufzeit_ms", "betriebsfaelle",
    "verdeckt", "fehlstellen", "diagnose")
ALT_SPALTEN = 11
# Ausgelassene Zyklen; jeder Grund ist ein fester Code.
AUSGELASSEN = ("zeitbudget", "schattenfehler", "gesperrt") + tuple("produktpfad_ohne_vergleich:" + name
    for name in ("beobachtung", "live_fehlt", "handover", "fallback", "ausgang", "konfiguration", "besitzer"))
# Feste Fachcodes; freie Texte und interne Kerncodes werden nicht archiviert. Die U-Ursachen
# stehen hinten, damit die Indizes älterer Archivzeilen gültig bleiben.
ALT_CODES = 19
CODES = ("GLEICH", "MODUS", "DAUER", "LEISTUNG", "FLATTERN", "BUDGETFREIGABE",
    "SCHUTZ_FEHLT", "STRENGER", "RESERVE", "ANSCHLUSS", "AUS", "INSEL", "LADESP",
    "RESERVE_BUDGET", "DATEN", "NETZRAHMEN_UNBEKANNT", "SCHREIBWEG", "A0", "U",
    "eingang_ungebunden", "neustart") + tuple("nicht_abgedeckt:" + name
    for name in FUNKTIONEN + ("Sonderfunktion",)) + ("produkt_sonder",) + AUSGELASSEN
# Schrankenbefunde, die eine vorrangige Klasse (U, A4) verdeckt; gezählt, ohne die Klasse zu ändern.
SCHRANKEN = ("RESERVE", "NETZRAHMEN_UNBEKANNT", "ANSCHLUSS", "AUS", "INSEL", "LADESP", "RESERVE_BUDGET", "DATEN")
# Fehlende Fachfunktionen eines Zyklus, auch mehrere zugleich. Die Reihenfolge der Codes ist fest (Archivindex,
# nur hinten ergänzen); die Zuordnung über den Anfang des Kernhinweises prüft spezielle Anfänge zuerst.
FEHLSTELLEN = ("DIREKTVERMARKTUNG", "LASTSPITZEN", "VORAB_NETZFALLBACK", "VORAB_PFLICHTFELD", "UNWETTER_PLAN",
    "UNWETTER_AUFTRAG", "VORHALT_AUFNAHME", "VORHALT_KAPAZITAET", "HAND_ENTLADEN", "HAND_OHNE_ZIEL",
    "HAND_UNBEKANNT", "MARKT_HALTEN", "MARKT_LADEN", "PREIS_BOOST", "SONSTIGE",
    "DV_EXPORT", "DV_PV_SPEICHER", "DV_KURVENFORMUNG", "DV_PHASE5", "DV_AUFNAHME", "DV_LADEBLOCK_FENSTER",
    "MARKT_AUFNAHME", "PLATZSCHAFFEN", "KURVE_HORIZONT", "GEPLANTE_LAST")
FEHLSTELLEN_PRAEFIXE = (
    ("Direktvermarktung: Export", "DV_EXPORT"), ("Direktvermarktung: PV-Speicher", "DV_PV_SPEICHER"),
    ("Direktvermarktung: Kurvenformung", "DV_KURVENFORMUNG"), ("Direktvermarktung: Phase 5", "DV_PHASE5"),
    ("Direktvermarktung: Aufnahme", "DV_AUFNAHME"), ("Direktvermarktung: Ladeblock", "DV_LADEBLOCK_FENSTER"),
    ("Direktvermarktung", "DIREKTVERMARKTUNG"), ("Lastspitzen", "LASTSPITZEN"),
    ("Pre-Dump-Netzfallback", "VORAB_NETZFALLBACK"), ("Pre-Dump: Pflichtfeld", "VORAB_PFLICHTFELD"),
    ("Unwetter:", "UNWETTER_AUFTRAG"),
    ("Vorhaltung: Schattenaufnahme", "VORHALT_AUFNAHME"), ("Vorhaltung: Kapazität", "VORHALT_KAPAZITAET"),
    ("manuelles Entladen ohne Ziel", "HAND_OHNE_ZIEL"), ("manuelles Entladen", "HAND_ENTLADEN"),
    ("manueller Auftrag ohne Ziel", "HAND_OHNE_ZIEL"),
    ("unbekannter Handauftrag", "HAND_UNBEKANNT"), ("Markthalten ohne", "MARKT_HALTEN"),
    ("Marktladen ohne", "MARKT_LADEN"), ("Markt: Aufnahme", "MARKT_AUFNAHME"), ("Preis-Boost", "PREIS_BOOST"),
    ("Platzschaffen", "PLATZSCHAFFEN"), ("Kurve: gleitender Prognosehorizont", "KURVE_HORIZONT"),
    ("Geplante Last", "GEPLANTE_LAST"))
# Welche Fehlstellen eine angeforderte Funktion in diesem Zyklus offen lassen. Jede Funktion ist abgedeckt, solange
# der Kern für sie keine ihrer Fehlstellen meldet; andere Fehlstellen heißen Sonderfunktion.
FUNKTION_FEHLSTELLEN = {"Pre-Dump": ("VORAB_NETZFALLBACK", "VORAB_PFLICHTFELD"), "Unwetter": ("UNWETTER_AUFTRAG",),
    "Wärme-Tarif": (), "Lastspitzen": ("LASTSPITZEN",), "Direktvermarktung": ("DIREKTVERMARKTUNG", "DV_EXPORT",
    "DV_PV_SPEICHER", "DV_KURVENFORMUNG", "DV_PHASE5", "DV_AUFNAHME", "DV_LADEBLOCK_FENSTER"),
    "Preis-Boost": ("PREIS_BOOST",), "Vorhaltung": ("VORHALT_AUFNAHME", "VORHALT_KAPAZITAET")}
# Unabhängige Nebenprojektionen (Verbraucherbudget, Wärme, Unwetter) gegen das Produkt: nur Zähler, keine Klasse.
DIAGNOSEN = ("BUDGET_WALLBOX", "BUDGET_WAERMEPUMPE", "BUDGET_HEIZSTAB", "WAERME_FREIGABE",
    "WAERME_AKTIV", "UNWETTER_WIRKSAM", "NEBENPROJEKTION_FEHLER", "SMARTCHARGE")
KERN_FELDER = ("reserve_aktiv", "reserve_pct", "erholung_s", "erholung_proben",
    "letzte_messzeit_s", "letzte_zeit_s", "letzte_monoton_s", "zeitbasis", "kandidat",
    "kandidat_s", "kandidat_wh", "vorhalt_konto", "vorhalt_zeitbasis", "netzauftrag_konto",
    "netzauftrag_rest_wh", "netzauftrag_rate_w", "netzauftrag_ziel_wh", "vorab_latch",
    "vorab_warte_seit_s", "vorab_netz_aktiv", "entlade_konto", "entlade_wh", "entlade_tag", "entlade_tag_wh",
    "hand_konto", "hand_anker_soc", "hand_anker_s", "hand_fertig", "entlade_slot", "entlade_slot_wh",
    "platz_zuletzt_s", "pv_laden_seit_s")
# Später ergänzte Kernfelder mit ihrem Startwert; ältere Zustände ohne sie bleiben ladbar.
KERN_FELDER_NEU = dict(vorab_warte_seit_s=None, vorab_netz_aktiv=False, entlade_konto=None, entlade_wh=0.0,
    entlade_tag=None, entlade_tag_wh=0.0, hand_konto=None, hand_anker_soc=None, hand_anker_s=None, hand_fertig=False,
    entlade_slot=None, entlade_slot_wh=0.0, platz_zuletzt_s=None, pv_laden_seit_s=None)
PROJEKTION_FELDER = ("modus", "letzte_grid_zeit_s", "letzter_grid_wert_w", "max_laden_w",
    "max_entladen_w", "readback_bestaetigt")
GRENZEN_FELDER = ("max_charge_w", "max_discharge_w", "discharge_start_w", "limits_used", "bounded_zero_readback_max_w")
SOLL_FELDER = ("modus", "ladegrenze_w", "entladegrenze_w", "grenzen_frei", "grid_soll_w", "entlade_soll_w",
    "lade_soll_w")


def zahl(v):
    return type(v) in (int, float) and isfinite(v)


@dataclass
class Schatten:
    kern_zustand: k.Zustand = field(default_factory=k.Zustand)
    projektionszustand: p.Projektionszustand | None = None
    quellen_vorzustand: dict = field(default_factory=dict)
    gedaechtnis: dict = field(default_factory=dict)
    stunden: list = field(default_factory=list)
    heute: dict | None = None
    letzter_zyklus: dict = field(default_factory=dict)
    fehler: dict = field(default_factory=lambda: {"count": 0, "code": None})
    funktionsstatus: dict = field(default_factory=dict)
    gesperrt: str | None = None
    ts: float = 0
    neustart: bool = True
    gespeichert_s: float | None = None


def _form(v, tiefe=0):
    """Größe, Breite und Tiefe werden vor jeder Rekonstruktion begrenzt."""
    if tiefe > 8:
        raise ValueError("ZUSTANDSFORM")
    if isinstance(v, dict):
        return len(v) <= 180 and all(type(key) is str and len(key) <= 80 and _form(x, tiefe+1) for key, x in v.items())
    if isinstance(v, list):
        return len(v) <= 336 and all(_form(x, tiefe+1) for x in v)
    return v is None or type(v) is bool or zahl(v) or type(v) is str and len(v) <= 10000


def zustand_laden(daten, aggregate=None, *, jetzt, boot_id):
    """Versionierte Wiederaufnahme; Prozessproben werden immer frisch berechnet."""
    try:
        if (not isinstance(daten, dict) or not _form(daten)
                or daten.get("schema") != SCHEMA or daten.get("version") != VERSION
                or not zahl(daten.get("ts")) or not 0 <= jetzt-daten["ts"] < 7200
                or daten.get("boot_id") != boot_id):
            return Schatten()
        raw = daten["kern_zustand"]
        values = {key: raw[key] if key in raw or key not in KERN_FELDER_NEU else KERN_FELDER_NEU[key]
            for key in KERN_FELDER}
        if any(type(v) not in (str, bool, int, float, type(None)) for v in values.values()):
            return Schatten()
        values.update(soll=k.Stellgroesse(**raw["soll"]) if raw.get("soll") else None,
            reserve_anteile=tuple(k.Reserveanteil(*row) for row in raw["reserve_anteile"]),
            reserve_ausloeser=tuple(raw["reserve_ausloeser"]), haltezone=raw.get("haltezone"))
        projection = daten.get("projektionszustand")
        if projection:
            projection = p.Projektionszustand(**{key: projection[key] for key in PROJEKTION_FELDER},
                grenzen=p.Grenzen(**projection["grenzen"]) if projection.get("grenzen") else None)
        z = Schatten(kern_zustand=k.Zustand(**values), projektionszustand=projection,
            quellen_vorzustand={key: daten.get("quellen_vorzustand", {}).get(key)
                for key in ("previous_parallel_state", "previous_parallel_val", "zeit_s")},
            ts=daten["ts"], gespeichert_s=daten["ts"], neustart=False)
        h = _auffuellen(deepcopy(daten.get("stunde")))
        day = _auffuellen(deepcopy(daten.get("heute_intern")))
        if h is not None:
            # Fehlende, fremde oder falsch geformte Stundenzähler werden verworfen.
            if not _statistik_gueltig(h) or not _statistik_gueltig(day):
                return Schatten()
            z.gedaechtnis = {"stunde": h}
            z.heute = day
        if isinstance(daten.get("budget_zustand"), dict):
            z.gedaechtnis["vergleich"] = {"nach_neustart": False, "budget": deepcopy(daten["budget_zustand"])}
        if isinstance(daten.get("ausfall_zustand"), dict):
            z.gedaechtnis.setdefault("vergleich", {"nach_neustart": False})["ausfall"] = deepcopy(daten["ausfall_zustand"])
        if isinstance(daten.get("spitze_zustand"), dict):
            z.gedaechtnis.setdefault("vergleich", {"nach_neustart": False})["spitze"] = deepcopy(daten["spitze_zustand"])
        if (isinstance(aggregate, dict) and aggregate.get("schema") == AGGREGAT_SCHEMA
                and aggregate.get("version") == VERSION and _form(aggregate)):
            rows = aggregate.get("stunden", [])
            # Zeilen vor den drei hinteren Spalten erhalten leere Zähler, sonst gingen vierzehn Tage verloren.
            rows = [row + [{}, {}, {}] if type(row) is list and len(row) == ALT_SPALTEN else row
                for row in rows] if type(rows) is list else rows
            if type(rows) is list and len(rows) <= 336 and all(type(row) is list
                    and len(row) == len(STUNDEN_SPALTEN) and _statistik_gueltig(dict(zip(STUNDEN_SPALTEN, row)), zeile=True)
                    for row in rows):
                z.stunden = [row for row in rows if jetzt-14*86400 <= row[0] < (h or {}).get("beginn", jetzt)]
        return z
    except (ValueError, TypeError, KeyError, IndexError):
        return Schatten()


def produkt_normalisieren(ergebnis_felder, transaktion, power_settings):
    e, tx, ps = map(auftauen, (ergebnis_felder, transaktion, power_settings))
    state = str(e.get("state") or "")
    auto = e.get("auto_limit") or {}
    # Bestätigte Grenzen haben Vorrang vor der Absicht. Kein Fehlwert wird 0.
    grenzen = ps.get("readback") if ps.get("confirmed") is True else ps.get("requested")
    grenzen = grenzen if isinstance(grenzen, dict) else {}
    charge, discharge = grenzen.get("max_charge_w"), grenzen.get("max_discharge_w")
    mode = e.get("mode")
    grid = entlade = lade = None
    for name in ("set_power", "set_power_auto"):
        step = (tx.get("substeps") or {}).get(name) or {}
        if step.get("issued") is True or step.get("retained") is True:
            mode = step.get("mode", mode)
            wert = step.get("value_w", step.get("value", step.get("val")))
            if mode == 4:
                grid = wert
            elif mode == 2:
                entlade = wert
            elif mode == 3:
                lade = wert
    modus = "SONDER"
    if mode == 4:
        modus = "NETZLADEN"
    elif mode == 2:
        modus = "ENTLADEN"
    elif mode == 3:
        modus = "LADEN"
    elif mode == 0:
        modus = ("HALTEN" if discharge == 0 and not auto.get("release") else
            "KURVE" if auto.get("enabled") and not auto.get("release") else "FREI")
    # Notstrom-Reservezustände des Produkts heißen `ep_reserve_*` (Regelfall `ep_reserve_discharge_hold`) oder
    # `reserve_hold*`. Hält der E3DC die Reserve selbst, gibt das Produkt die Entladung frei und schützt nicht selbst.
    reserve = (state.startswith(("ep_reserve_floor", "reserve_hold")) or state.startswith("ep_reserve_")
        and e.get("ep_reserve_mode") != "device_self_held")
    schutz = ("RESERVE" if reserve else
        "INSEL" if state.startswith("emergency_power") else
        "DATEN" if state.startswith(("live_plausibility", "live_stale", "no_live")) else
        "AUS" if state in ("disabled", "observation", "storage_regulation_off") else
        "UNTERDRUECKT" if e.get("suppress_rscp_output") else None)
    budget = e.get("budget") or {}
    return dict(modus=modus, schutz=schutz, ladegrenze_w=charge if zahl(charge) else None,
        entladegrenze_w=discharge if zahl(discharge) else None,
        grid_soll_w=grid if zahl(grid) else None, entlade_soll_w=entlade if zahl(entlade) else None,
        lade_soll_w=lade if zahl(lade) else None,
        budget_pv_w=budget.get("budget_pv_w"), budget_battery_w=budget.get("budget_battery_w"),
        freigabe=budget.get("battery_support_authorized"))


def kern_modus(soll):
    """Moduslabel des Kerns nach seinen wirksamen Grenzen, gebildet wie beim Produkt.

    FREI mit gebundenen Grenzen und gesperrter Entladung ist physikalisch ein Halt (Regelfall: Notstromreserve,
    Kern `reserve_aktiv` mit Soll FREI und Entladegrenze 0 W); das Produkt nennt dieselben Grenzen HALTEN."""
    if soll.modus == "FREI" and soll.grenzen_frei is False and soll.entladegrenze_w == 0:
        return "HALTEN"
    return soll.modus


def _abweichung(a, b, *, budget=False):
    if not zahl(a) or not zahl(b):
        return None
    d = abs(a - b)
    grenze = 300 if budget else 50 if min(abs(a), abs(b)) <= 50 else max(100, .02 * max(abs(a), abs(b)))
    return d if d > grenze else 0


def umkehrung(vorher, werte, gesendet):
    """Zählt Richtungswechsel und A→B→A nur an tatsächlich betrachteten Ausgängen."""
    neu = deepcopy(vorher or {})
    n = 0
    if not gesendet:
        return 0, neu
    for key in ("ladegrenze_w", "entladegrenze_w", "grid_soll_w", "entlade_soll_w", "lade_soll_w"):
        old, value = neu.get(key), werte.get(key)
        if zahl(old) and zahl(value) and abs(value - old) > 50:
            richtung = 1 if value > old else -1
            if neu.get(key + "_richtung", richtung) != richtung:
                n += 1
            neu[key + "_richtung"] = richtung
        if zahl(value):
            neu[key] = value
    mode = werte.get("modus")
    if mode and mode != neu.get("modus"):
        if mode == neu.get("vorletzter_modus"):
            n += 1
        neu["vorletzter_modus"], neu["modus"] = neu.get("modus"), mode
    return n, neu


def a0_regel(u, e, r, produkt, differenzen, g):
    """Ausschließlich beschlossene Regeln mit messbarem, engem Merkmal."""
    raw, live, plan, cfg = map(auftauen, (u.ergebnis, u.eingaenge.live, u.eingaenge.plan, u.eingaenge.cfg))
    auto, backup = raw.get("auto_limit") or {}, raw.get("backup_hold_control") or {}
    state, mode, soll = str(raw.get("state") or ""), produkt["modus"], r.soll
    d = {key for key, value in differenzen.items() if value}
    charge = differenzen.get("ladegrenze_w") or 0
    dis = differenzen.get("entladegrenze_w") or 0
    budget = max(differenzen.get("budget_pv_w") or 0, differenzen.get("budget_battery_w") or 0)
    modusdiff = mode != kern_modus(soll)
    nur_laden = bool(d) and d <= {"ladegrenze_w"} and not modusdiff
    if nur_laden and mode == "KURVE" and e.kurve and e.kurve.zielzeit_s > e.jetzt_s:
        rate = abs(e.kurve.ziel_pct - (e.soc.wert or 0)) * e.kurve.kapazitaet_wh * 36 / (e.kurve.zielzeit_s - e.jetzt_s)
        if charge <= rate * k.OPTIMIERUNGSTAKT_S / max(k.OPTIMIERUNGSTAKT_S, e.kurve.zielzeit_s - e.jetzt_s):
            return "A0_RASTER_30S"
    opt = r.folgezustand.optimierung
    if opt and not modusdiff and d and not r.schreiben and g.get("kernprobe"):
        # Produkt liegt innerhalb der letzten beiden fälligen Kernproben.
        if all(zahl(produkt.get(key)) and zahl(g["kernprobe"].get(key))
                and min(g["kernprobe"][key], getattr(soll, key)) <= produkt[key]
                <= max(g["kernprobe"][key], getattr(soll, key))
                for key in d if key in ("ladegrenze_w", "entladegrenze_w", "grid_soll_w")) and d <= {"ladegrenze_w", "entladegrenze_w", "grid_soll_w"}:
            return "A0_TAKT_30S"
    if g.get("anschluss_seit") is not None and e.jetzt_s - g["anschluss_seit"] <= 40 and soll.modus == "NETZLADEN":
        return "A0_ANSCHLUSS_BIS_TAKT"
    age = e.jetzt_s - e.messzeit_s if zahl(e.messzeit_s) else None
    if r.grundcode == "DATENFEHLER" and age is not None and 10 < age <= 30 and produkt["schutz"] != "DATEN":
        return "A0_LIVEFRIST_10S"
    if (backup.get("locked") is True and not r.folgezustand.reserve_aktiv
            and (soll.entladegrenze_w or 0) > 0 and not backup.get("takeover_pending")):
        return "A0_VORHALT_HYSTERESE"
    if soll.modus == "HALTEN" and any(v.netzladen_gewollt for v in e.verbraucher) and mode in ("SONDER", "FREI", "KURVE"):
        return "A0_NETZFENSTER_HALTEN"
    box = [v for v in e.verbraucher if v.art.startswith("wallbox_")]
    kleine_box = sum(v.leistung.wert for v in box if zahl(v.leistung.wert) and 0 < v.leistung.wert <= 500)
    if not modusdiff and d and d <= {"ladegrenze_w", "entladegrenze_w", "budget_pv_w", "budget_battery_w"} and kleine_box > 0 and max(
            value or 0 for value in differenzen.values()) <= kleine_box + 300:
        return "A0_LADEBELEG_500W"
    reclaim = (raw.get("budget") or {}).get("priority_running_reclaimable_w")
    if not modusdiff and budget and d <= {"budget_pv_w", "budget_battery_w"}:
        if zahl(reclaim) and abs(budget - reclaim) <= 1:
            return "A0_KEINE_RUECKBUCHUNG"
        if any(v.art in ("waermepumpe", "heizstab") and v.laeuft for v in e.verbraucher) or (raw.get("budget") or {}).get("start_requested") is True:
            return "A0_EIN_RAHMEN"
    quelle = e.auftrag.quelle if e.auftrag else ""
    storm = "Unwetter" in quelle
    ziel = e.auftrag.ziel_pct if e.auftrag else None
    if storm and not r.folgezustand.reserve_aktiv and soll.modus == "HALTEN" and r.grundcode == "ZIEL_ERREICHT" and mode in ("FREI", "KURVE"):
        return "A0_UNWETTER_HALTEN_BIS_ENDE"
    if not r.folgezustand.reserve_aktiv and soll.modus == "HALTEN" and r.grundcode.startswith("NETZRAHMEN_") and produkt["entladegrenze_w"] == 0:
        return "A0_NETZLADEN_OHNE_RAHMEN_HAELT"
    if soll.modus == "NETZLADEN" and r.folgezustand.reserve_aktiv and state.startswith("reserve_hold"):
        return "A0_RESERVE_NUR_ENTLADUNG"
    if storm and modusdiff and zahl(ziel) and zahl(e.soc.wert) and ziel - .5 <= e.soc.wert <= ziel:
        return "A0_KEINE_ZIELHYSTERESE_UNWETTER"
    if soll.modus == "HALTEN" and state.endswith("_wait") and zahl(r.netzladerahmen_w) and 0 <= r.netzladerahmen_w < 300:
        return "A0_UNTERGRENZE_PROJEKTION"
    waerme = [v for v in e.verbraucher if v.art == "waermepumpe" and v.netzgedeckt]
    if waerme and not modusdiff and d == {"entladegrenze_w"}:
        if dis <= 200:
            return "A0_KEINE_SCHREIBBREMSE_WAERME"
        if dis <= 300 + sum(v.leistung.wert for v in box if zahl(v.leistung.wert) and v.im_haus):
            return "A0_DECKEL_HAUS_OHNE_WP"
    if waerme and not modusdiff and not d and soll.entladegrenze_w == 0 and (raw.get("heat_tariff_shift") or {}).get("safety_free") is False:
        return "A0_WP_START_UNABHAENGIG_BESITZER"
    meta = plan.get("target_curve_meta") or {}
    vorab_ziel = plan.get("predump_curve_soc", meta.get("predump_curve_soc"))
    vorab = state.startswith(("predump", "pre_discharge")) or e.auftrag and e.auftrag.modus == "VORAB"
    if vorab and zahl(vorab_ziel) and zahl(e.soc.wert):
        if g.get("nach_neustart") and vorab_ziel < e.soc.wert <= vorab_ziel + 1:
            return "A0_VORAB_NEUSTART_ZONE"
        if modusdiff and vorab_ziel <= e.soc.wert <= vorab_ziel + 1:
            return "A0_VORAB_EINE_HYSTERESE"
        if modusdiff and e.kurve and e.soc.wert <= e.kurve.boden_pct + .05:
            return "A0_VORAB_KURVENVORRANG"
        rate = r.budget.vorab_rate_w
        if not modusdiff and budget and zahl(rate) and zahl(e.max_entladen.wert) and abs(budget - (e.max_entladen.wert - rate)) <= 1:
            return "A0_VORAB_RATE_BEGRENZT"
        pre = raw.get("pre_discharge") or {}
        # Gilt nur, solange der Kern keinen eigenen Netzrückfall rechnet; sonst zeigt der Vergleich die Abweichung.
        if (isinstance(pre, dict) and zahl(pre.get("discharge_w")) and zahl(rate) and pre["discharge_w"] > rate
                and (e.netz.wert or 0) > 0 and r.grundcode != "PLAN_VORAB_NETZ"):
            return "A0_VORAB_KEINE_NETZANHEBUNG"
        if not modusdiff and not d and e.auftrag and e.auftrag.beginn_s <= e.jetzt_s and state == "predump_bev_wait":
            return "A0_VORAB_BEV_START"
    return None


def fehlstellen(e):
    """Alle fehlenden Fachfunktionen eines Zyklus als feste Codes, nicht nur die erste."""
    codes = []
    for hinweis in str(e.sonderfunktion or "").split("; "):
        if hinweis:
            codes.append(next((code for praefix, code in FEHLSTELLEN_PRAEFIXE if hinweis.startswith(praefix)), "SONSTIGE"))
    return sorted(set(codes), key=FEHLSTELLEN.index)


def funktion_offen(name, fehlend):
    """Lässt der Kern eine angeforderte Funktion in diesem Zyklus offen?"""
    return bool(set(FUNKTION_FEHLSTELLEN[name]) & set(fehlend))


def nebenprojektionen(u, e, r, vorzustand, *, jetzt, zeitprobe=None):
    """Vorhandene Budget-, Wärme- und Unwetterprojektionen, wirkungslos und mit eigenem Vorzustand.

    Kein Produktfeld fließt in die eigenen Werte (keine Übernahme in die Budgetprojektion); das Produkt ist
    nur Vergleichsreferenz. Abweichungen sind Diagnosezähler, keine Vergleichsklasse. Wärme-Nachlauf und
    Startangebote laufen auf der gemeinsamen monotonen Wachuhr (zeitprobe des Dienstes)."""
    vorher = vorzustand if isinstance(vorzustand, dict) else {}
    try:
        # Die vor der Entscheidung gebundene Zeitprobe des Dienstes (wall, monotonic, Boot); nur ohne sie frisch.
        probe = zeitprobe if isinstance(zeitprobe, dict) else (auftauen(u.eingaenge.zeitprobe)
            or b.control_time.sample(wall_ts=jetzt))
        cfg = auftauen(u.eingaenge.verbraucher).get("cfg") or {}
        reihenfolge = b.cp.priority_order_from_config(cfg)
        schluessel = b.cp.priority_order_key(reihenfolge)
        # C13: dieselbe monotone Nachlaufuhr wie der Produkt-Zuteiler (gemeinsame Funktion, eigener Vorzustand).
        nachlauf = b.cp.consumer_priority_runon_guard(vorher.get("vertrag"), previous_key=str(vorher.get("reihenfolge")
            or ""), configured_key=schluessel, duration_s=b.cp.priority_runon_s_from_config(cfg), current_sample=probe)
        intent = auftauen(u.eingaenge.wb_intent)
        phasen = auftauen(u.eingaenge.phasen)
        dauern, warte_s = b.lease_regeln(cfg)
        generation = str(intent.get("start_generation") or "")
        gebucht, wartend, leases = b.leases_pruefen(r.budget, vorher.get("leases"), probe, generation, warte_s)
        gesperrt = set(wartend)
        if phasen.get("aktiv") is True and not any(a.verbraucher.laeuft for a in r.budget.verbraucher
                if b._art(a.verbraucher) == "heatpump"):
            gesperrt.add("heatpump")      # Wie im Produkt: kein neuer WP-Start während einer Phasenumschaltung.
        reserviert = dict(gebucht)
        for name, w in (b.reservierungen(phasen) or {}).items():
            reserviert[name] = max(reserviert.get(name, 0), w)
        zuteilung = b.zuteilen(r.budget, order=reihenfolge, now_s=jetzt, previous_contract=vorher.get("vertrag"),
            wp_runon_s=b.cp.priority_runon_s_from_config(cfg), wp_runon_active=nachlauf["active"],
            reservations_w=reserviert or None, wartend=frozenset(gesperrt),
            gehalten=frozenset(name for name, w in reserviert.items() if w > 0))
        leases = b.leases_anbieten(r.budget, zuteilung, leases, probe, generation, dauern)
        budget = b.projektion(r.budget, zuteilung, e, jetzt, intent=intent, leases=leases, wartend=wartend,
            phasen=phasen)
        status = b.status_projektion(budget)
        waerme = p.waerme_vertrag(r, e, jetzt)
        unwetter = p.unwetter_diagnose(r, e, u.eingaenge)
    except (ValueError, TypeError, KeyError, AttributeError, ZeroDivisionError):
        return dict(diagnose=["NEBENPROJEKTION_FEHLER"]), vorher
    raw = auftauen(u.ergebnis)
    eigen = status["budget"]["consumer_allocations"]
    produkt = (raw.get("budget") or {}).get("consumer_allocations")
    diagnose = []
    if isinstance(produkt, dict):
        for name, code in (("wallbox", "BUDGET_WALLBOX"), ("heatpump", "BUDGET_WAERMEPUMPE"), ("heater", "BUDGET_HEIZSTAB")):
            if _abweichung(produkt.get(name), eigen.get(name), budget=True):
                diagnose.append(code)
    heat = raw.get("heat_tariff_shift")
    if isinstance(heat, dict):
        for key, code in (("safety_free", "WAERME_FREIGABE"), ("active", "WAERME_AKTIV")):
            if type(heat.get(key)) is bool and heat[key] != waerme[key]:
                diagnose.append(code)
    if unwetter["angefordert"] and unwetter["wirksam"] != str(raw.get("state") or "").startswith("storm_guard"):
        diagnose.append("UNWETTER_WIRKSAM")
    neu = dict(reihenfolge=schluessel, leases=leases, vertrag=dict(active=dict(zuteilung["contract"].get("active") or {}),
        consumer_priority_runon_guard=nachlauf["guard"], consumer_priority_runon_guard_active=nachlauf["active"]))
    return dict(diagnose=diagnose, budget_w={key: eigen.get(key) for key in ("wallbox", "heatpump", "heater")},
        betriebsrecht=dict(budget["consumer_operating_rights"]), nachlauf=nachlauf["active"],
        startangebote=sorted(name for name, lease in leases.items() if lease.get("art") == b.LEASE_ANGEBOT),
        budget_gueltig=budget["data_valid"], waerme_frei=waerme["safety_free"], waerme_aktiv=waerme["active"],
        unwetter_wirksam=unwetter["wirksam"]), neu


def gedaechtnis_fortschreiben(z, eingaenge):
    """C09, L01: Ausfall- und Intervallgedächtnis sind der Kontext der eigenen Aufnahme dieses Zyklus. Der Dienst
    schreibt sie direkt nach dem Binden fort, auch wenn der Vergleich dieses Zyklus entfällt (Zeitbudget, Fehler)."""
    g = z.gedaechtnis.setdefault("vergleich", {"nach_neustart": z.neustart})
    ausfall = auftauen(eingaenge.ausfall)
    if ausfall and ausfall.get("aufnahmefehler") is not True:
        g["ausfall"] = ausfall
    kontext = auftauen(eingaenge.spitze).get("kontext")
    if isinstance(kontext, dict):
        g["spitze"] = kontext


def vergleichen(u, e, r, projektion, gedaechtnis, funktionsstatus, *, neustart=False, schreibweg=False):
    produkt = produkt_normalisieren(u.ergebnis, u.rscp_request_transaction, u.power_settings)
    limits = projektion.power_settings
    # Beide Seiten tragen ihr Moduslabel nach den wirksamen Grenzen.
    kern = kern_modus(r.soll)
    hypothetisch = dict(modus=kern,
        ladegrenze_w=limits.max_charge_w if limits else None,
        entladegrenze_w=limits.max_discharge_w if limits else None,
        grid_soll_w=projektion.value_w if projektion.mode == 4 else None,
        entlade_soll_w=projektion.value_w if projektion.mode == 2 else None,
        lade_soll_w=projektion.value_w if projektion.mode == 3 else None,
        budget_pv_w=r.budget.pv_w.wert_w, budget_battery_w=r.budget.battery_w.wert_w)
    paar = produkt["modus"] + "×" + kern
    d = {key: _abweichung(produkt[key], hypothetisch[key], budget=key.startswith("budget"))
         for key in ("ladegrenze_w", "entladegrenze_w", "grid_soll_w", "entlade_soll_w", "lade_soll_w",
                     "budget_pv_w", "budget_battery_w")}
    g = deepcopy(gedaechtnis)
    tx = auftauen(u.rscp_request_transaction)
    steps = (tx.get("substeps") or {}).values()
    real_sets = sum(row.get("issued") is True for row in steps if isinstance(row, dict))
    hyp_sets = sum(step.wuerde_senden for step in projektion.substeps)
    real_kehr, g["real"] = umkehrung(g.get("real"), produkt, real_sets or (auftauen(u.power_settings).get("confirmed") is True))
    hyp_kehr, g["hyp"] = umkehrung(g.get("hyp"), hypothetisch, hyp_sets)
    g["real_n"] = g.get("real_n", 0) + real_kehr
    g["hyp_n"] = g.get("hyp_n", 0) + hyp_kehr
    modusdiff = produkt["modus"] != kern
    if {produkt["modus"], kern} <= {"FREI", "KURVE"} and not any(d.values()):
        modusdiff = False
    abweichend = modusdiff or any(d.values())
    # Eine Episode gehört zum Betriebsfall, unabhängig vom numerischen Differenzfeld.
    seit = g.get("abweichung_seit") if g.get("abweichung_signatur") == paar else None
    dauer = max(0, e.jetzt_s-seit) if zahl(seit) else 0
    anschluss = (r.soll.modus == "NETZLADEN" and zahl(r.netzladerahmen_w)
        and (r.soll.grid_soll_w or 0) > r.netzladerahmen_w)
    if not anschluss:
        g.pop("anschluss_seit", None)
    elif g.get("anschluss_seit") is None:
        g["anschluss_seit"] = e.jetzt_s
    klasse, code = "gleich", "GLEICH"
    angefordert = [name for name in FUNKTIONEN if (funktionsstatus.get(name) or {}).get("angefordert") is True]
    # Das Etikett nennt eine angeforderte Funktion, die der Kern in diesem Zyklus offen lässt (etwa Direktvermarktung
    # neben Pre-Dump). Deckt er alle angeforderten ab und meldet trotzdem eine Fehlstelle, heißt sie Sonderfunktion.
    fehlend = fehlstellen(e)
    nicht_abgedeckt = next((name for name in angefordert if funktion_offen(name, fehlend)),
        None if r.abgedeckt or fehlend else next(iter(angefordert), None))
    schutz_soll = (r.folgezustand.reserve_aktiv or r.grundcode in
        ("REGELUNG_AUS", "INSEL", "DATENFEHLER", "GERAETEGRENZE_UNBEKANNT"))
    lade = r.soll.ladegrenze_w or 0
    entlade = r.soll.entladegrenze_w or 0
    grid = r.soll.grid_soll_w or 0
    akku = r.budget.battery_w.wert_w
    reserve = r.folgezustand.reserve_pct
    schranke = ("RESERVE" if entlade > 0 and (r.folgezustand.reserve_aktiv or produkt["schutz"] == "RESERVE") else
        "NETZRAHMEN_UNBEKANNT" if r.soll.modus == "NETZLADEN" and not zahl(r.netzladerahmen_w) else
        "ANSCHLUSS" if anschluss and e.jetzt_s - g["anschluss_seit"] > 40 else
        "AUS" if e.regelung_an is False and (grid > 0 or not r.soll.grenzen_frei and (lade > 0 or entlade > 0)) else
        "INSEL" if e.notstrom.wert in (1, 4) and (grid > 0 or not r.soll.grenzen_frei and (lade > 0 or entlade > 0)) else
        "LADESP" if e.auftrag and e.auftrag.modus == "LADESP" and e.auftrag.beginn_s <= e.jetzt_s < e.auftrag.ende_s and lade > 0 else
        "RESERVE_BUDGET" if zahl(akku) and akku > 0 and zahl(e.soc.wert) and zahl(reserve) and e.soc.wert <= reserve else
        "DATEN" if r.grundcode == "DATENFEHLER" and hyp_sets and not (
            r.folgezustand.reserve_aktiv and entlade == 0 and grid == 0) else None)
    # Reihenfolge ist der Vertrag: A4, U, A3, A0, A2, A1, gleich.
    if schreibweg:
        klasse, code = "A4", "SCHREIBWEG"
    elif not e.zeitbasis:
        klasse, code = "U", "eingang_ungebunden"
    elif neustart:
        klasse, code = "U", "neustart"
    elif nicht_abgedeckt or not r.abgedeckt:
        klasse, code = "U", "nicht_abgedeckt:" + (nicht_abgedeckt or "Sonderfunktion")
    elif produkt["modus"] == "SONDER" and not any(v.netzladen_gewollt for v in e.verbraucher):
        klasse, code = "U", "produkt_sonder"
    elif schranke:
        klasse, code = "A3", schranke
    else:
        raw = auftauen(u.ergebnis)
        vertragsabweichung = (not abweichend and r.soll.entladegrenze_w == 0
            and (raw.get("heat_tariff_shift") or {}).get("safety_free") is False
            and any(v.art == "waermepumpe" and v.netzgedeckt for v in e.verbraucher)
            or not abweichend and raw.get("state") == "predump_bev_wait"
            and e.auftrag is not None and e.auftrag.modus == "VORAB")
        a0 = a0_regel(u, e, r, produkt, d, g) if abweichend or vertragsabweichung else None
        if g.get("nach_neustart") and produkt["schutz"] == "RESERVE" and not r.folgezustand.reserve_aktiv and not r.schreiben:
            a0 = "A0_NEUSTART_LATCH"
        if a0:
            klasse, code = "A0", a0
        elif modusdiff or max((value or 0 for value in d.values()), default=0) >= 1000 or abweichend and dauer > 300:
            klasse, code = "A2", "MODUS" if modusdiff else "DAUER" if dauer > 300 else "LEISTUNG"
        elif hyp_kehr and g["hyp_n"] > g["real_n"] + 2:
            # Nur der Zyklus, in dem der Kern über die Produktumkehrungen der Stunde hinaus umkehrt
            # (Zähler je Stunde im Speicherdienst), nicht jeder weitere Zyklus der Stunde.
            klasse, code = "A2", "FLATTERN"
        elif type(produkt["freigabe"]) is bool and produkt["freigabe"] != (r.budget.battery_w.gueltig and (akku or 0) > 0):
            klasse, code = "A2", "BUDGETFREIGABE"
        elif produkt["schutz"] and not schutz_soll:
            klasse, code = "A2", "SCHUTZ_FEHLT"
        elif abweichend or schutz_soll and not produkt["schutz"]:
            klasse, code = "A1", "STRENGER" if schutz_soll else "LEISTUNG"
    if klasse in ("A1", "A2"):
        if seit is None:
            seit = e.jetzt_s
        g.update(abweichung_seit=seit, abweichung_signatur=paar)
        dauer = max(0, e.jetzt_s-seit)
    else:
        g.pop("abweichung_seit", None)
        g.pop("abweichung_signatur", None)
        dauer = 0
    opt = r.folgezustand.optimierung
    if opt and opt.monoton_s != g.get("faelligkeit"):
        g.update(kernprobe={key: getattr(r.soll, key) for key in ("ladegrenze_w", "entladegrenze_w", "grid_soll_w")},
            faelligkeit=opt.monoton_s, nach_neustart=False)
    neben, g["budget"] = nebenprojektionen(u, e, r, g.get("budget"), jetzt=e.jetzt_s)
    revision = u.eingaenge.plan_revision
    return dict(klasse=klasse, code=code, moduspaar=paar,
        dauer_s=dauer, anschluss_wirkzeit="UNGEMESSEN",
        # Ein Schrankenbefund bleibt sichtbar, auch wenn U oder A4 die Klasse bestimmen.
        verdeckt=schranke if schranke and klasse != "A3" else None,
        fehlstellen=fehlend, diagnose=neben.pop("diagnose"), neben=neben,
        # Beide Seiten lesen dieselbe vor der Entscheidung gebundene Planrevision.
        plan_revision=revision[:80] if isinstance(revision, str) else None), g


LAUFZEIT_GRENZEN = tuple(range(101)) + tuple(range(110, 501, 10)) + tuple(1000*2**i for i in range(11))


def _klasse(value):
    return str(next((grenze for grenze in LAUFZEIT_GRENZEN if value <= grenze), LAUFZEIT_GRENZEN[-1]))


def _histogramm(hist, value):
    if zahl(value):
        # Endlich viele Klassen: 1 ms bis 100 ms, 10 ms bis 500 ms, danach gröber.
        key = _klasse(value)
        hist[key] = hist.get(key, 0) + 1
        hist["max"] = max(hist.get("max", 0), value)


def _quantile(hist):
    total = sum(v for key, v in hist.items() if key != "max")
    if not total:
        return [None, None, None, None]
    count = 0
    for value in sorted((key for key in hist if key != "max"), key=int):
        count += hist[value]
        if count >= ceil(total * .95):
            return [None, round(min(int(value), hist["max"]), 3), None, round(hist["max"], 3)]


def laufzeit_ergaenzen(z, laufzeit_ms):
    vorher = z.letzter_zyklus.get("laufzeit_ms", 0)
    for h in (z.gedaechtnis.get("stunde"), z.heute):
        if h:
            hist = h["laufzeit_ms"]
            key = _klasse(vorher)
            if hist.get(key, 0) > 0:
                hist[key] -= 1
                if not hist[key]:
                    del hist[key]
            _histogramm(hist, laufzeit_ms)
    z.letzter_zyklus.update(laufzeit_ms=round(laufzeit_ms, 3), laufzeit_block_ms=round(laufzeit_ms, 3))


def _neu_stunde(beginn, tag):
    return dict(beginn=beginn, tag=tag, zyklen=0, vergleichbar=0, klassen=[0]*7, codes=[0]*len(CODES),
        abweichung_laengste_s=0, a3_a4=0, laufzeit_ms={},
        abdeckung=[[0, 0] for _ in FUNKTIONEN], betriebsfaelle={},
        verdeckt=[0]*len(SCHRANKEN), fehlstellen=[0]*len(FEHLSTELLEN), diagnose=[0]*len(DIAGNOSEN))


# Frühere Längen der nur hinten ergänzten Zählervektoren; andere Längen bleiben fremd.
FRUEHERE_LAENGEN = dict(codes=(ALT_CODES,))


def _auffuellen(h):
    """Laufende Stundenzähler früherer Stände: fehlende Zähler und hinten ergänzte Codes mit 0 auffüllen."""
    if not isinstance(h, dict) or not isinstance(h.get("codes"), list):
        return h
    if len(h["codes"]) == ALT_CODES:
        for key, n in (("verdeckt", len(SCHRANKEN)), ("fehlstellen", len(FEHLSTELLEN)), ("diagnose", len(DIAGNOSEN))):
            h.setdefault(key, [0]*n)
    for key, laengen in FRUEHERE_LAENGEN.items():
        n = dict(codes=len(CODES))[key]
        if isinstance(h.get(key), list) and len(h[key]) in laengen:
            h[key] = h[key] + [0]*(n-len(h[key]))
    return h


def _vektor(v, n):
    return isinstance(v, list) and len(v) == n and all(zahl(x) and x >= 0 for x in v)


def _statistik_gueltig(h, zeile=False):
    if not isinstance(h, dict) or set(h) != set(STUNDEN_SPALTEN):
        return False
    if not all(zahl(h[key]) and h[key] >= 0 for key in ("beginn", "tag", "zyklen", "vergleichbar", "abweichung_laengste_s", "a3_a4")):
        return False
    # Dieselben endlichen Vektoren, im Archiv nur ohne Nullzeilen und Histogramm.
    codes, coverage, runtime = h["codes"], h["abdeckung"], h["laufzeit_ms"]
    zaehler = {key: h[key] for key in ("verdeckt", "fehlstellen", "diagnose")}
    laengen = dict(verdeckt=len(SCHRANKEN), fehlstellen=len(FEHLSTELLEN), diagnose=len(DIAGNOSEN))
    if zeile:
        if not isinstance(codes, dict) or not isinstance(coverage, dict) or not isinstance(runtime, list) or len(runtime) != 2:
            return False
        # Dünne Zähler: nur bekannte Indizes, sonst ist die Zeile fremd.
        if not all(isinstance(v, dict) and set(v) <= {str(i) for i in range(laengen[key])} for key, v in zaehler.items()):
            return False
        zaehler = {key: [v.get(str(i), 0) for i in range(laengen[key])] for key, v in zaehler.items()}
        codes = [codes.get(str(i), 0) for i in range(len(CODES))]
        coverage = [coverage.get(str(i), [0, 0]) for i in range(len(FUNKTIONEN))]
        runtime = {str(i): v for i, v in enumerate(runtime) if v is not None}
    return (all(_vektor(v, laengen[key]) for key, v in zaehler.items())
        and _vektor(h["klassen"], 7) and _vektor(codes, len(CODES))
        and isinstance(coverage, list) and len(coverage) == len(FUNKTIONEN) and all(_vektor(v, 2) for v in coverage)
        and isinstance(runtime, dict) and len(runtime) <= 180 and all((key == "max" or key.isdigit()) and zahl(v) and v >= 0 for key, v in runtime.items())
        and isinstance(h["betriebsfaelle"], dict) and all(key in {a+"×"+b for a in MODI for b in MODI} and _vektor(v, 4) for key, v in h["betriebsfaelle"].items()))


def _zeile(h):
    return [h["beginn"], h["tag"], h["zyklen"], h["vergleichbar"], list(h["klassen"]),
        {str(i): n for i, n in enumerate(h["codes"]) if n}, round(h["abweichung_laengste_s"], 3), h["a3_a4"],
        {str(i): row for i, row in enumerate(h["abdeckung"]) if any(row)},
        _quantile(h["laufzeit_ms"])[1::2], deepcopy(h["betriebsfaelle"])] + [
        {str(i): n for i, n in enumerate(h[key]) if n} for key in ("verdeckt", "fehlstellen", "diagnose")]


def fortschreiben(z, vergleich, *, jetzt, stunde, tag, laufzeit_ms, e=None, r=None,
                  projektionszustand=None, quellen=None, gedaechtnis=None, funktionsstatus=None):
    # Abgeschlossene Stunden bleiben unverändert und werden nur am Wechsel rotiert.
    neu = replace(z, gedaechtnis=deepcopy(z.gedaechtnis), heute=deepcopy(z.heute))
    neu.ts = jetzt
    neu.gedaechtnis.setdefault("vergleich", {"nach_neustart": neu.neustart})
    h = neu.gedaechtnis.get("stunde")
    if h is None or h["beginn"] != stunde:
        neu.stunden = list(z.stunden)
        if h:
            neu.stunden.append(_zeile(h))
        h = _neu_stunde(stunde, tag)
        neu.gedaechtnis["stunde"] = h
        neu.stunden = [row for row in neu.stunden[-336:] if row[0] >= jetzt-14*86400]
    if neu.heute is None or neu.heute["tag"] != tag:
        neu.heute = _neu_stunde(stunde, tag)
    if gedaechtnis is not None:
        neu.gedaechtnis["vergleich"] = gedaechtnis
    klasse, pair = vergleich["klasse"], vergleich.get("moduspaar")
    code = vergleich["code"]
    code = code if code in CODES else "A0" if klasse == "A0" else "U"
    status = funktionsstatus or neu.funktionsstatus
    for counter in (h, neu.heute):
        counter["zyklen"] += 1
        counter["vergleichbar"] += klasse != "U"
        counter["klassen"][KLASSEN.index(klasse)] += 1
        counter["codes"][CODES.index(code)] += 1
        counter["a3_a4"] += klasse in ("A3", "A4")
        dauer = vergleich.get("dauer_s", 0) if klasse in ("A1", "A2") else 0
        counter["abweichung_laengste_s"] = max(counter["abweichung_laengste_s"], dauer)
        _histogramm(counter["laufzeit_ms"], laufzeit_ms)
        if pair:
            counts = counter["betriebsfaelle"].setdefault(pair, [0, 0, 0, 0])
            counts[0] += klasse != "U"
            counts[1] += klasse == "A2"
            counts[2] = max(counts[2], dauer)
            counts[3] += klasse in ("A3", "A4")
        for i, name in enumerate(FUNKTIONEN):
            requested = (status.get(name) or {}).get("angefordert") is True
            counter["abdeckung"][i][0] += requested
            counter["abdeckung"][i][1] += requested and r is not None and r.abgedeckt
        # Verdeckte Schranke, Fehlstellen und Nebenprojektionen zählen neben der Klasse, nie statt ihr.
        if vergleich.get("verdeckt") in SCHRANKEN:
            counter["verdeckt"][SCHRANKEN.index(vergleich["verdeckt"])] += 1
        for key, namen in (("fehlstellen", FEHLSTELLEN), ("diagnose", DIAGNOSEN)):
            for name in set(vergleich.get(key) or ()):
                if name in namen:
                    counter[key][namen.index(name)] += 1
    if e is not None and r is not None:
        neu.kern_zustand, neu.projektionszustand = r.folgezustand, projektionszustand
        neu.quellen_vorzustand = dict(quellen or {})
        neu.neustart = False
        neu.fehler = {"count": 0, "code": None}
    if klasse == "A4":
        neu.gesperrt = "SCHREIBWEG"
    neu.funktionsstatus, neu.ts = dict(status), jetzt
    neu.letzter_zyklus = dict(vergleich, laufzeit_ms=round(laufzeit_ms, 3))
    return neu


def ausgelassen(z, grund, *, jetzt, stunde, tag, laufzeit_ms=0):
    if grund not in AUSGELASSEN:
        raise ValueError("GRUND_UNBEKANNT")
    neu = fortschreiben(z, dict(klasse="U", code=grund, moduspaar=None),
        jetzt=jetzt, stunde=stunde, tag=tag, laufzeit_ms=laufzeit_ms)
    # Keine Integration über eine ausgelassene Probe und keine alte Flatterepisode.
    neu.gedaechtnis["vergleich"].pop("abweichung_seit", None)
    neu.gedaechtnis["vergleich"].pop("abweichung_signatur", None)
    return neu


def aggregat_projektion(z):
    rows = list(z.stunden)
    if z.gedaechtnis.get("stunde"):
        rows.append(_zeile(z.gedaechtnis["stunde"]))
    rows = [row for row in rows[-336:] if row[0] >= z.ts-14*86400]
    # Die tatsächliche Schreiberkodierung bestimmt das Budget vor dem Schreiben.
    while True:
        faelle = {}
        for row in rows:
            for pair, v in row[STUNDEN_SPALTEN.index("betriebsfaelle")].items():
                old = faelle.setdefault(pair, [0, 0, 0, 0])
                faelle[pair] = [old[0]+v[0], old[1]+v[1], max(old[2], v[2]), old[3]+v[3]]
        out = dict(schema=AGGREGAT_SCHEMA, version=VERSION, ts=z.ts,
            stunden_spalten=list(STUNDEN_SPALTEN), klassen_spalten=list(KLASSEN),
            codes_spalten=list(CODES), abdeckung_spalten=list(FUNKTIONEN),
            verdeckt_spalten=list(SCHRANKEN), fehlstellen_spalten=list(FEHLSTELLEN),
            diagnose_spalten=list(DIAGNOSEN), stunden=rows,
            zeitraum=[rows[0][0], rows[-1][0]+3600] if rows else None,
            betriebsfaelle={pair: dict(a2_anteil_pct=round(100*v[1]/v[0], 5) if v[0] else None,
                a2_laengste_s=round(v[2], 3), a3_a4=v[3]) for pair, v in faelle.items()})
        if len(json.dumps(out, ensure_ascii=False, allow_nan=False).encode()) <= AGGREGAT_BYTES:
            return out
        rows = rows[1:]


def ramdisk_projektion(z, *, boot_id):
    # Nur explizite Felder der Version 2. Optimierungsprobe und Vergleichsuhr
    # entstehen nach einem Prozessneustart neu; Reserve und Konten bleiben erhalten.
    kern = {name: getattr(z.kern_zustand, name) for name in KERN_FELDER}
    kern.update(soll={name: getattr(z.kern_zustand.soll, name) for name in SOLL_FELDER} if z.kern_zustand.soll else None,
        reserve_anteile=[[v.quelle, v.pct] for v in z.kern_zustand.reserve_anteile],
        reserve_ausloeser=list(z.kern_zustand.reserve_ausloeser),
        haltezone={key: z.kern_zustand.haltezone.get(key) for key in ("band_inside", "ts")}
            if z.kern_zustand.haltezone is not None else None)
    projection = {name: getattr(z.projektionszustand, name) for name in PROJEKTION_FELDER} if z.projektionszustand else None
    if projection is not None:
        projection["grenzen"] = {name: getattr(z.projektionszustand.grenzen, name) for name in GRENZEN_FELDER} if z.projektionszustand.grenzen else None
    day = deepcopy(z.heute)
    if day:
        day["laufzeit_ms"] = _quantile(day["laufzeit_ms"])
    return dict(schema=SCHEMA, version=VERSION, ts=z.ts, boot_id=boot_id,
        kern_zustand=kern, projektionszustand=projection,
        quellen_vorzustand={key: z.quellen_vorzustand.get(key)
            for key in ("previous_parallel_state", "previous_parallel_val", "zeit_s")},
        stunde=z.gedaechtnis.get("stunde"), heute_intern=z.heute,
        budget_zustand=(z.gedaechtnis.get("vergleich") or {}).get("budget"),
        ausfall_zustand=(z.gedaechtnis.get("vergleich") or {}).get("ausfall"),
        spitze_zustand=(z.gedaechtnis.get("vergleich") or {}).get("spitze"),
        vergleich_letzter_zyklus=z.letzter_zyklus, fehler=z.fehler,
        gesperrt=z.gesperrt, funktionsstatus=z.funktionsstatus, heute=day)
