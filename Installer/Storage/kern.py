"""Reiner Speicherkern: Werte hinein, ein Vorschlag und explizites Gedächtnis heraus.

Kein Geräteausgang. Positive Akkuleistung bedeutet Laden, positiver Netzsaldo
Bezug. Der Aufrufer normalisiert Quellen und liefert beide Uhren als Werte.
"""

from dataclasses import dataclass, replace
from math import isfinite
try:
    from Installer.Wallbox.decision import wbminsoc_hold_zone_contract
except ModuleNotFoundError:
    from Wallbox.decision import wbminsoc_hold_zone_contract


# Startwerte; keine Herstellerparameter. Herkunft steht in der Spezifikation.
LIVE_FRIST_S = 10.0
UEBERBRUECKUNG_S = 60.0             # Nur einschränkender Verbraucher-Deckel.
UHR_ABWEICHUNG_S = 2.0
GERAETE_FRIST_S = 1800.0
VORAB_ABSTAND_PP = 1.0              # Zielhysterese des Pre-Dump-Ziels.
RESERVE_ABSTAND_PP = 1.5
RESERVE_ERHOLUNG_S = 30.0
OPTIMIERUNGSTAKT_S = 30.0             # Annahme, keine gemessene Gerätezeit.
MODUS_ZEIT_S = 30.0
MODUS_ENERGIE_WH = 20.0
SCHREIBTOTBAND_W = 100.0
HAUS_AUFSCHLAG_W = 300.0
RESTLAST_VORZUSTAND_S = 15.0        # Halber Schritt nur aus dem eigenen, so frischen Vorzustand.
# Bestehender Ladebeleg des Speicherdienstes, auch für native Wallboxen.
LADEBELEG_W = 500.0
WALLBOX_QUELLENPRAEFIX = "wallbox,"
HAUS_MIN_W = -100.0
LEISTUNG_MAX_W = 1000000.0
DREIPHASEN_W_PRO_A = 690.0
SEKUNDEN_PRO_STUNDE = 3600.0


@dataclass(frozen=True)
class Messwert:
    wert: float | None = None
    gueltig: bool = False
    quelle: str = "unbekannt"
    zeit_s: float | None = None
    grund: str = "fehlend"

    def zahl(self, jetzt: float, unten: float, oben: float,
             frist: float = LIVE_FRIST_S) -> float | None:
        if (not _zahl(jetzt) or self.gueltig is not True or not isinstance(self.quelle, str) or not self.quelle
                or not _zahl(self.wert) or not _zahl(self.zeit_s)
                or not 0 <= jetzt - self.zeit_s <= frist
                or not unten <= self.wert <= oben):
            return None
        return float(self.wert)


@dataclass(frozen=True)
class Verbraucher:
    art: str                         # wallbox, heatpump, heater
    leistung: Messwert = Messwert()
    im_haus: bool = True
    laeuft: bool = False
    anfrage_w: float | None = None
    mindest_w: float = 0.0
    akku_freigabe: bool | None = None
    boden_pct: float | None = None
    akku_kappe_w: float | None = None
    klasse: str | None = None
    pv_only_w: float | None = None
    netzladen_gewollt: bool = False
    gesperrt: bool = False
    netzgedeckt: bool = False
    frist_s: float | None = None
    intent_revision: str | None = None
    vorab_angemeldet: bool = False   # Zusätzliche Quelle, normales Recht bleibt erhalten.

    def watt(self, jetzt: float) -> float | None:
        """Deckelmessung mit unverändertem Quellzeitstempel, keine Freigabe."""
        frist = (UEBERBRUECKUNG_S if self.im_haus and self.art in (
            "waermepumpe", "wallbox_extern") else LIVE_FRIST_S)
        return self.leistung.zahl(jetzt, 0, LEISTUNG_MAX_W, frist=frist)


@dataclass(frozen=True)
class HaltezonenEingang:
    eligible: bool = False
    mode: int | None = None
    hysterese_pct: float | None = None
    neustart_pct: float = 2.0


@dataclass(frozen=True)
class Kurve:
    boden_pct: float
    deckel_pct: float
    ziel_pct: float
    zielzeit_s: float
    gueltig_bis_s: float
    kapazitaet_wh: float
    quelle: str


@dataclass(frozen=True)
class Auftrag:
    modus: str                        # HALTEN, NETZLADEN, LADESP, VORAB (Verbraucherangebot)
    beginn_s: float
    ende_s: float
    quelle: str
    rate_w: float | None = None
    ziel_pct: float | None = None
    rest_wh: float | None = None       # Netzenergie; bei VORAB Restenergie bis zum Ziel
    konto: str | None = None
    unabhaengig: "Auftrag | None" = None
    energieziel_wh: float | None = None  # eigenes absolutes Ziel, auf Netzenergie bezogen


@dataclass(frozen=True)
class Vorhalt:
    status: str
    grund: str
    boden_pct: float | None = None
    boden_wh: float | None = None
    kapazitaet_wh: float | None = None
    energie_wh: float | None = None
    konto: str | None = None           # Versiegelter, unveränderlicher Rechenzustand
    zeitbasis: str = ""
    gueltig_bis_s: float | None = None
    kapazitaetsquelle: str = "unbekannt"
    abweichungen: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reserveanteil:
    quelle: str
    pct: float


@dataclass(frozen=True)
class Eingang:
    jetzt_s: float
    monoton_s: float
    zeitbasis: str
    messzeit_s: float | None
    soc: Messwert
    notstrom: Messwert               # 0/3 Netz, 1/4 Insel, 2 unbekannt
    pv: Messwert
    haus: Messwert
    netz: Messwert
    akku: Messwert
    max_laden: Messwert
    max_entladen: Messwert
    eigene_reserve: Messwert
    geraetereserve: Messwert
    anschluss_a: Messwert = Messwert()
    anschluss_abstand_a: float = 0.0
    regelung_an: bool | None = None
    rscp_fehler: bool | None = None          # ausschließlich echter RSCP-Fehler
    verbraucher: tuple[Verbraucher, ...] = ()
    haltezone: HaltezonenEingang = HaltezonenEingang()
    kurve: Kurve | None = None
    auftrag: Auftrag | None = None
    sonderfunktion: str | None = None
    # Fertiger Bedarf zur Einspeisebegrenzung, keine Rechnung aus Akku/Netz.
    einspeise_ladebedarf: Messwert = Messwert()
    # Optionaler bestehender DC-only-Quellenrahmen; None heißt nicht angefordert.
    quellen_ladekappe: Messwert | None = None
    vorhalt: Vorhalt | None = None
    # Hypothetischer Wirk-/Refreshvertrag; G6 bindet ihn am realen Gerät.
    ausgabe_dauer_s: float = 3.0
    harte_restlast_w: float | None = None


@dataclass(frozen=True)
class Stellgroesse:
    modus: str = "FREI"
    ladegrenze_w: float | None = None
    entladegrenze_w: float | None = None
    grenzen_frei: bool = True
    grid_soll_w: float | None = None
    # Transport für die Projektion: Restmenge, Frist und Ausgabedauer des
    # endlichen Netzauftrags; gekappt wird nur hier im Kern (_endlicher_auftrag).
    grid_rest_wh: float | None = None
    grid_gueltig_bis_s: float | None = None
    grid_dauer_s: float | None = None


@dataclass(frozen=True)
class Anteil:
    wert_w: float | None
    gueltig: bool
    grund: str


@dataclass(frozen=True)
class VerbraucherBudget:
    verbraucher: Verbraucher
    battery_w: Anteil
    normal_w: float | None = None    # Ausgangsrecht für die zusätzliche VORAB-Quelle


@dataclass(frozen=True)
class Budget:
    pv_w: Anteil
    battery_w: Anteil
    house_base_w: float | None = None
    akkuanspruch_w: float | None = None
    rahmen_w: float | None = None
    verbraucher: tuple[VerbraucherBudget, ...] = ()
    haltezone: dict | None = None     # Aktueller Vertrag, kein Vorzustand.
    vorab_rate_w: float | None = None
    vorab_boden_pct: float | None = None
    house_base_quelle: str = "unbekannt"


@dataclass(frozen=True)
class Optimierung:
    monoton_s: float
    vorgabe: tuple
    budget: Budget
    rate_w: float
    kandidat: str
    kurvenkappe_w: float
    grid_w: float | None
    netzrahmen_w: float | None


@dataclass(frozen=True)
class Zustand:
    soll: Stellgroesse | None = None
    reserve_aktiv: bool = False
    reserve_pct: float | None = None
    erholung_s: float = 0.0
    erholung_proben: int = 0
    letzte_messzeit_s: float | None = None
    letzte_zeit_s: float | None = None
    letzte_monoton_s: float | None = None
    zeitbasis: str = ""
    kandidat: str | None = None
    kandidat_s: float = 0.0
    kandidat_wh: float = 0.0
    # Letzte einzelne Probe als Ergebnisse, ohne Mittel oder Filter.
    optimierung: Optimierung | None = None
    reserve_anteile: tuple[Reserveanteil, ...] = ()
    reserve_ausloeser: tuple[str, ...] = ()
    vorhalt_konto: str | None = None
    vorhalt_zeitbasis: str = ""
    abweichungen: tuple[str, ...] = ()
    netzauftrag_konto: str | None = None
    netzauftrag_rest_wh: float | None = None
    netzauftrag_rate_w: float = 0.0
    netzauftrag_ziel_wh: float | None = None
    haltezone: dict | None = None
    vorab_latch: float | None = None
    # Nachgeführte Restlast des letzten gültigen Entladedeckels; kein Haltewert.
    restlast_w: float | None = None
    # Zuordnung der Verbraucher (Art, herausgerechnet belegt, Rechtsgrund), zu der diese Restlast gehört.
    restlast_zuordnung: tuple = ()


@dataclass(frozen=True)
class Ergebnis:
    soll: Stellgroesse
    folgezustand: Zustand
    budget: Budget
    schreiben: bool                 # hypothetische Änderung, keine Quittung
    abgedeckt: bool
    grundcode: str
    satz: str
    netzladerahmen_w: float | None = None


def _zahl(wert: object) -> bool:
    return type(wert) in (float, int) and isfinite(wert)


def _schrittzeit(e: Eingang, z: Zustand) -> float:
    if (e.zeitbasis != z.zeitbasis or not _zahl(z.letzte_zeit_s)
            or not _zahl(z.letzte_monoton_s) or not _zahl(e.monoton_s)
            or not _zahl(e.jetzt_s)):
        return 0.0
    dt = e.monoton_s - z.letzte_monoton_s
    if not 0 < dt <= LIVE_FRIST_S or abs(e.jetzt_s - z.letzte_zeit_s - dt) > UHR_ABWEICHUNG_S:
        return 0.0
    return dt


def _abschluss(e: Eingang, z: Zustand, neu: Zustand, soll: Stellgroesse,
               budget: Budget, code: str, satz: str, *, abgedeckt: bool = True,
               rahmen: float | None = None) -> Ergebnis:
    neu = replace(neu, soll=soll, letzte_zeit_s=e.jetzt_s,
                  letzte_monoton_s=e.monoton_s, zeitbasis=e.zeitbasis)
    abgedeckt = abgedeckt and not bool(e.sonderfunktion)
    if _auftrag_gueltig(e.auftrag, e.jetzt_s) and e.auftrag.modus not in ("HALTEN", "LADESP", "NETZLADEN", "VORAB"):
        abgedeckt = False
    return Ergebnis(soll, neu, budget, soll != z.soll, abgedeckt, code, satz, rahmen)


def _unbekannt(grund: str) -> Budget:
    return Budget(Anteil(None, False, grund), Anteil(None, False, grund))


def waerme_hauslast(v) -> bool:
    """Wärmepumpe und Heizstab ohne eigenen Akkuvertrag sind Hauslast.

    Ihre Leistung bleibt im Hausmesspunkt des Verbraucherbudgets und zählt
    dort genau einmal; einen zweiten Budgetanteil erhalten sie nicht. Pause,
    Netzdeckung, ein eigener Vertrag und ein angemeldeter Pre-Dump behalten
    ihren bestehenden Weg als Verbraucher.
    """
    return (v.art in ("waermepumpe", "heizstab") and v.akku_freigabe is None
            and not v.gesperrt and not v.netzgedeckt and not v.vorab_angemeldet)


def _hausbasis(e: Eingang) -> float | None:
    haus = e.haus.zahl(e.jetzt_s, HAUS_MIN_W, LEISTUNG_MAX_W)
    if haus is None:
        return None
    enthalten = 0.0
    for v in e.verbraucher:
        w = v.leistung.zahl(e.jetzt_s, 0, LEISTUNG_MAX_W)
        if v.im_haus and v.laeuft and w is not None and not waerme_hauslast(v):
            enthalten += w
    return max(0.0, haus - enthalten)


def _restlast_nachfuehren(wert: float, e: Eingang, z: Zustand, zuordnung: tuple) -> float:
    """Halber Schritt aus dem eigenen frischen Vorzustand, sonst der Messwert.

    Der Hauswert enthält die Akkuleistung einen Takt verspätet: Jede Änderung
    der Entladegrenze erscheint einen Takt lang mit umgekehrtem Vorzeichen als
    Hauslast. Der volle Schritt pendelt deshalb im Zweitakt; der halbe klingt
    ab und folgt echten Laständerungen in wenigen Takten. Keine Zeithaltung:
    Ohne gültigen Vortakt (Datenlücke, Pause, Uhrsprung) gilt der Messwert,
    ebenso nach jedem Wechsel der Zuordnung (Lauf, Messung, Recht, Sperre,
    Netzdeckung eines Verbrauchers): Rechte wirken im selben Takt.
    """
    if (not _zahl(z.restlast_w) or z.restlast_zuordnung != zuordnung or e.zeitbasis != z.zeitbasis
            or not _zahl(z.letzte_zeit_s) or not _zahl(z.letzte_monoton_s)):
        return wert
    dt = e.monoton_s - z.letzte_monoton_s
    if not 0 < dt <= RESTLAST_VORZUSTAND_S or abs(e.jetzt_s - z.letzte_zeit_s - dt) > UHR_ABWEICHUNG_S:
        return wert
    return z.restlast_w + 0.5 * (wert - z.restlast_w)


def _ladebelegt(v, jetzt):
    # Bestätigte netzgedeckte Wärme braucht nur eine positive Messung;
    # der allgemeine Ladebeleg bleibt für alle anderen Verbraucher.
    schwelle = 0 if v.art == "waermepumpe" and v.netzgedeckt else LADEBELEG_W
    return v.laeuft and (v.watt(jetzt) or 0) > schwelle


def _als_hauslast(anteil) -> bool:
    """Wärmepumpe und Heizstab ohne eigenen Akkuvertrag sind Hauslast: Der Akku trägt sie mit dem Haus."""
    return (anteil.verbraucher.art in ("waermepumpe", "heizstab")
            and anteil.battery_w.grund == "AKKU_NICHT_FREIGEGEBEN:kein_vertrag")


def _akkuanteil(v, e, soc, reserve, gesperrt, modus, kurve_ok, entlade_max, hausbedarf, zone):
    grund = ("gesperrt" if v.gesperrt else "reserve" if gesperrt or reserve is None else
        "netzgedeckt" if v.netzgedeckt else
        "netzladen_gewollt" if v.netzladen_gewollt else
        "auftrag" if modus in ("HALTEN", "NETZLADEN") else
        "wbminsoc_hold_zone" if v.art.startswith("wallbox_") and zone["active"] else
        "kein_vertrag" if v.akku_freigabe is None else
        "boden_unbekannt" if not _zahl(v.boden_pct) or not 0 <= v.boden_pct <= 100 else
        "boden" if soc <= max(reserve, v.boden_pct) else
        "keine_freigabe" if v.akku_freigabe is not True else
        "klasse" if v.klasse in ("pv_only", "floor_closed") else
        "kurve" if kurve_ok and soc <= e.kurve.deckel_pct and v.klasse != "floor_contingent" else
        "hausbedarf_unbekannt" if hausbedarf is None else "")
    kappe = max(0.0, v.akku_kappe_w) if _zahl(v.akku_kappe_w) else entlade_max
    wert = (min(max(0.0, entlade_max - hausbedarf), kappe) if not grund else
        None if grund in ("kein_vertrag", "hausbedarf_unbekannt", "boden_unbekannt") else 0.0)
    return VerbraucherBudget(v, Anteil(wert, wert is not None,
        "AKKU_NICHT_FREIGEGEBEN:" + grund if grund else "AKKU_FREIGEGEBEN"))


def _vorab_boden(reserve, ziel):
    """Der zusätzliche Auftrag und die Haltezone lesen denselben gültigen Boden."""
    return max(reserve, ziel) if (_zahl(reserve) and 0 <= reserve <= 100
        and _zahl(ziel) and 0 <= ziel <= 100) else None


def _vorab_anteil(v, e, soc, reserve, gesperrt, modus, kurve_ok,
                  entlade_max, hausbedarf, zone, aktiv, rate):
    normal = _akkuanteil(v, e, soc, reserve, gesperrt, modus, kurve_ok,
        entlade_max, hausbedarf, zone)
    if not aktiv:
        return normal
    normal = replace(normal, normal_w=normal.battery_w.wert_w)
    if not v.vorab_angemeldet:
        return normal
    anfrage = (max(v.anfrage_w, rate) if v.art.startswith("wallbox_")
        and _zahl(v.anfrage_w) else v.anfrage_w)
    normal = replace(normal, verbraucher=replace(v, anfrage_w=anfrage))
    vorab = replace(v, akku_freigabe=True, boden_pct=_vorab_boden(reserve, e.auftrag.ziel_pct),
        akku_kappe_w=rate, klasse="Pre-Dump", anfrage_w=anfrage)
    zusatz = _akkuanteil(vorab, e, soc, reserve, gesperrt, modus,
        False, entlade_max, hausbedarf, zone)
    # F1: Null Zusatzanteil entzieht niemals ein bestehendes Akkurecht.
    if (zusatz.battery_w.wert_w or 0) <= (normal.battery_w.wert_w or 0):
        return normal
    return replace(zusatz, normal_w=normal.battery_w.wert_w,
        battery_w=replace(zusatz.battery_w, grund="Pre-Dump"))


def _kurve_gueltig(k: Kurve | None, jetzt: float) -> bool:
    return bool(isinstance(k, Kurve) and isinstance(k.quelle, str) and k.quelle and all(_zahl(v) for v in (
        k.boden_pct, k.deckel_pct, k.ziel_pct, k.zielzeit_s,
        k.gueltig_bis_s, k.kapazitaet_wh))
        and 0 <= k.boden_pct <= k.deckel_pct <= 100
        and 0 <= k.ziel_pct <= 100 and k.kapazitaet_wh > 0
        and jetzt <= k.gueltig_bis_s and jetzt < k.zielzeit_s)


def _auftrag_gueltig(a: Auftrag | None, jetzt: float) -> bool:
    return bool(isinstance(a, Auftrag) and isinstance(a.quelle, str) and a.quelle and _zahl(a.beginn_s)
                and _zahl(a.ende_s) and a.beginn_s <= jetzt < a.ende_s)


def _rohbilanz(e: Eingang) -> Eingang:
    """Der Hauswert der Bilanz verwendet dieselben rohen Quellen genau einmal."""
    if e.haus.quelle != "Bilanz":
        return e
    pv = e.pv.zahl(e.jetzt_s, 0, LEISTUNG_MAX_W)
    netz = e.netz.zahl(e.jetzt_s, -LEISTUNG_MAX_W, LEISTUNG_MAX_W)
    akku = e.akku.zahl(e.jetzt_s, -LEISTUNG_MAX_W, LEISTUNG_MAX_W)
    nativ = tuple(p.leistung.zahl(e.jetzt_s, 0, LEISTUNG_MAX_W)
                  for p in e.verbraucher if p.art == "wallbox_nativ" and not p.im_haus)
    haus = (replace(e.haus, wert=max(0.0, pv + netz - akku - sum(nativ)))
            if None not in (pv, netz, akku) and None not in nativ else
            replace(e.haus, wert=None, gueltig=False, grund="BILANZQUELLE_UNGUELTIG"))
    return replace(e, haus=haus)


def _reserve(e, z, neu, soc, fehler):
    """Ein Maximum, ein Latch mit Herkunft, ein energiebasierter Entladerahmen."""
    dt = _schrittzeit(e, z)
    eigene = e.eigene_reserve.zahl(e.jetzt_s, 0, 100, GERAETE_FRIST_S)
    geraet = e.geraetereserve.zahl(e.jetzt_s, 0, 100, GERAETE_FRIST_S)
    alt = {a.quelle: a.pct for a in z.reserve_anteile}
    # Ein altes Zweikomponentengedächtnis enthält ausschließlich Basisreserve.
    if not alt and z.reserve_pct is not None:
        alt = dict(eigene=z.reserve_pct, geraet=z.reserve_pct)
    altbasis = max((wert for name, wert in alt.items() if name != 'vorhalt'), default=None)
    basis = {name: wert if wert is not None else altbasis
             for name, wert in (("eigene", eigene), ("geraet", geraet))}
    anteile = {name: wert for name, wert in basis.items() if wert is not None}
    v = e.vorhalt
    angefordert = (e.regelung_an is True and e.notstrom.zahl(e.jetzt_s, 0, 4) not in (1, 4)
                   and v is not None and v.status in ("aktiv", "Prognosefallback", "Schattenfehler")
                   and v.grund != "backup_disabled")
    vorhalt = (v.boden_pct if angefordert
               and _zahl(v.boden_pct) and _zahl(v.gueltig_bis_s)
               and e.jetzt_s <= v.gueltig_bis_s else None)
    # K4: Erst die schon um regelbare Verbraucher und Akkuladung bereinigte
    # harte Restlast erlaubt Rücknahme des Vorhalts. Die Basis bleibt bindend.
    unbekannt = angefordert and vorhalt is None
    entlastung = vorhalt is not None and _zahl(e.harte_restlast_w) and e.harte_restlast_w > 0
    if vorhalt is not None and not entlastung:
        anteile['vorhalt'] = vorhalt
    reserve = max(anteile.values()) if anteile else None
    nur_geraet = eigene == 0.0 and geraet is not None and reserve == geraet and not (
        vorhalt is not None and not entlastung and vorhalt > geraet)
    ausloeser = set(z.reserve_ausloeser)
    if z.reserve_aktiv and not ausloeser:
        ausloeser = {name for name, wert in alt.items() if wert == z.reserve_pct}
    ausloeser.intersection_update(anteile)
    if nur_geraet:
        ausloeser.difference_update(('eigene', 'geraet'))
    if not fehler and soc is not None:
        ausloeser.update(name for name, wert in anteile.items() if wert > 0 and soc <= wert
                        and not (nur_geraet and name in ('eigene', 'geraet')))
    aktiv = bool(ausloeser)
    erholung, proben = 0.0, 0
    if (not fehler and aktiv and reserve is not None and soc > reserve
            and soc >= min(100.0, reserve + RESERVE_ABSTAND_PP)
            and (eigene is not None or geraet is not None or vorhalt is not None)):
        # Bewegliche Böden verdienen dieselbe kontinuierliche Erholung, solange
        # jede Probe den jeweils aktuellen Energieabstand tatsächlich belegt.
        if (dt > 0 and _zahl(z.letzte_messzeit_s) and _zahl(z.erholung_s)
                and type(z.erholung_proben) is int
                and 0 <= z.erholung_s <= max(0, z.erholung_proben - 1) * LIVE_FRIST_S
                and 0 <= e.messzeit_s - z.letzte_messzeit_s <= LIVE_FRIST_S):
            if e.messzeit_s == z.letzte_messzeit_s:
                erholung, proben = z.erholung_s, z.erholung_proben
            else:
                erholung = z.erholung_s + (min(dt, e.messzeit_s-z.letzte_messzeit_s)
                                           if z.erholung_proben else 0.0)
                proben = z.erholung_proben + 1
        else:
            proben = 1
        if erholung >= RESERVE_ERHOLUNG_S:
            aktiv, ausloeser = False, set()
    neu = replace(neu, reserve_aktiv=aktiv, reserve_pct=reserve,
        reserve_anteile=tuple(Reserveanteil(name, wert) for name, wert in sorted(anteile.items())),
        reserve_ausloeser=tuple(sorted(ausloeser)), erholung_s=erholung,
        erholung_proben=proben, letzte_messzeit_s=e.messzeit_s if not fehler else None,
        vorhalt_konto=v.konto if v else z.vorhalt_konto,
        vorhalt_zeitbasis=v.zeitbasis if v else z.vorhalt_zeitbasis,
        abweichungen=v.abweichungen if v else ())
    entladen = e.max_entladen.zahl(e.jetzt_s, 0, LEISTUNG_MAX_W, GERAETE_FRIST_S)
    rahmen = 0.0 if unbekannt or aktiv or reserve is None or eigene == 0 and geraet is None else entladen
    if (rahmen is not None and v and vorhalt is not None and _zahl(v.kapazitaet_wh)
            and _zahl(v.energie_wh) and _zahl(e.ausgabe_dauer_s) and e.ausgabe_dauer_s > 0):
        # Eine ganze SoC-Stufe trägt die Quantisierung; die Reichweite trägt
        # die mögliche Ausgabehaltung. Kein weiterer Vorhalteausgang.
        reichweite = max(35.0, LIVE_FRIST_S + e.ausgabe_dauer_s)
        safe_wh = max(0.0, v.energie_wh - reserve*v.kapazitaet_wh/100 - v.kapazitaet_wh/100)
        rahmen = min(rahmen, safe_wh*SEKUNDEN_PRO_STUNDE/reichweite)
        if entlastung:
            rahmen = min(rahmen, e.harte_restlast_w)
    return neu, reserve, nur_geraet, rahmen, entlastung or unbekannt


def _endlicher_auftrag(e, z, neu, soc):
    a = e.auftrag
    rest_alt = z.netzauftrag_rest_wh
    if _zahl(rest_alt):
        vergangen = max(0.0, e.monoton_s-z.letzte_monoton_s) if (
            e.zeitbasis == z.zeitbasis and _zahl(z.letzte_monoton_s) and _zahl(e.monoton_s)) else 0.0
        rest_alt = max(0.0, rest_alt-z.netzauftrag_rate_w*vergangen/3600)
    neu = replace(neu, netzauftrag_rest_wh=rest_alt, netzauftrag_rate_w=0.0)
    if not _auftrag_gueltig(a, e.jetzt_s):
        return a.unabhaengig if a and a.rest_wh is not None else a, neu, None
    if a.rest_wh is None:
        return a, neu, None
    rest = a.rest_wh
    if z.netzauftrag_konto == a.konto and _zahl(rest_alt):
        # Tatsächlich gehaltene Ausgabezeit, auch bei noch nicht fälliger
        # Optimierung. Replan und Duplikatprobe erneuern das Konto nicht.
        # Eine tatsächlich höhere Zielpflicht darf nur ihre zusätzliche Energie
        # nachfordern. Gleiches Replan-Ziel oder Zurück-/Wiederhochsetzen geben
        # bereits kontierte Wh nicht noch einmal frei.
        mehrbedarf = max(0.0, a.energieziel_wh-z.netzauftrag_ziel_wh) if (
            _zahl(a.energieziel_wh) and _zahl(z.netzauftrag_ziel_wh)) else 0.0
        rest = min(rest, rest_alt+mehrbedarf)
    ziel = max(a.energieziel_wh, z.netzauftrag_ziel_wh) if (
        z.netzauftrag_konto == a.konto and _zahl(a.energieziel_wh)
        and _zahl(z.netzauftrag_ziel_wh)) else a.energieziel_wh
    neu = replace(neu, netzauftrag_konto=a.konto, netzauftrag_rest_wh=rest,
                  netzauftrag_rate_w=0.0, netzauftrag_ziel_wh=ziel)
    dauer = e.ausgabe_dauer_s
    rate = min(a.rate_w, rest*3600/dauer) if (_zahl(rest) and _zahl(dauer) and dauer > 0
        and _zahl(a.rate_w)) else 0.0
    if rate < 300 or (_zahl(a.ziel_pct) and _zahl(soc) and soc >= a.ziel_pct):
        return a.unabhaengig, neu, None
    if _auftrag_gueltig(a.unabhaengig, e.jetzt_s):
        b = a.unabhaengig
        if b.modus == 'NETZLADEN' and _zahl(b.rate_w) and _zahl(b.ziel_pct):
            rate = max(rate, b.rate_w)
    return replace(a, rate_w=rate, rest_wh=rest), neu, rate


def schritt(eingang: Eingang, vorzustand: Zustand) -> Ergebnis:
    """Entscheidet ohne I/O; der Vorzustand wird niemals verändert."""
    e, z = eingang, vorzustand
    neu = replace(z, kandidat=None, kandidat_s=0.0, kandidat_wh=0.0, restlast_w=None, restlast_zuordnung=())
    notstrom = e.notstrom.zahl(e.jetzt_s, 0, 4)
    soc = e.soc.zahl(e.jetzt_s, 0, 100)
    # Schutzliste 2: genau die vier vereinbarten Gesamtauslöser.
    fehler = (not _zahl(e.messzeit_s) or not _zahl(e.jetzt_s)
              or not 0 <= e.jetzt_s - e.messzeit_s <= LIVE_FRIST_S
              or not _zahl(e.monoton_s) or not isinstance(e.zeitbasis, str) or not e.zeitbasis
              or e.regelung_an is not True or e.rscp_fehler is not False or soc is None or notstrom not in (0, 2, 3))
    neu, reserve, nur_geraet, entlade_max, reserve_vorgabe = _reserve(e, z, neu, soc, fehler)
    if e.auftrag is not None and e.auftrag.modus == "VORAB":
        a, endliche_rate = e.auftrag, None
        neu = replace(neu, netzauftrag_rate_w=0.0)
    else:
        a, neu, endliche_rate = _endlicher_auftrag(e, z, neu, soc)
    # Schutzliste 1: Nutzer und echte Insel haben Vorrang vor Datenfehlern.
    if e.regelung_an is False or notstrom in (1, 4):
        code = "REGELUNG_AUS" if e.regelung_an is False else "INSEL"
        neu = replace(neu, optimierung=None, erholung_s=0, erholung_proben=0, letzte_messzeit_s=None,
                      netzauftrag_konto=None, netzauftrag_rest_wh=None, netzauftrag_rate_w=0.0,
                      netzauftrag_ziel_wh=None)
        return _abschluss(e, z, neu, Stellgroesse(), _unbekannt(code), code,
                           "Die Regelung gibt die Grenzen einmalig an das Gerät zurück.")
    if fehler:
        soll = (Stellgroesse(entladegrenze_w=0.0, grenzen_frei=False)
                if neu.reserve_aktiv else Stellgroesse())
        neu = replace(neu, optimierung=None, erholung_s=0, erholung_proben=0, letzte_messzeit_s=None)
        return _abschluss(e, z, neu, soll, _unbekannt("DATENFEHLER"), "DATENFEHLER",
                           "Die Messdaten fehlen; nur eine bereits aktive Entladesperre bleibt bestehen.")

    dt = _schrittzeit(e, z)
    aktiv = neu.reserve_aktiv
    eigene = e.eigene_reserve.zahl(e.jetzt_s, 0, 100, GERAETE_FRIST_S)
    geraet = e.geraetereserve.zahl(e.jetzt_s, 0, 100, GERAETE_FRIST_S)
    laden = e.max_laden.zahl(e.jetzt_s, 0, LEISTUNG_MAX_W, GERAETE_FRIST_S)
    entladen = e.max_entladen.zahl(e.jetzt_s, 0, LEISTUNG_MAX_W, GERAETE_FRIST_S)
    # Fehlende Gerätegrenzen werden nicht als gemessene Null ausgegeben.
    if laden is None or entladen is None:
        soll = Stellgroesse(entladegrenze_w=0.0, grenzen_frei=False) if aktiv else Stellgroesse()
        return _abschluss(e, z, replace(neu, optimierung=None), soll, _unbekannt("GERAETEGRENZE_UNBEKANNT"),
                           "GERAETEGRENZE_UNBEKANNT", "Ohne belegte Gerätegrenzen gibt es keine neue Optimierung.")
    e = _rohbilanz(e)
    kurve_ok = _kurve_gueltig(e.kurve, e.jetzt_s)
    # Laufender Rest ist kein neuer Optimierungsauftrag.
    vorgabe_auftrag = (a.modus, a.quelle, a.ende_s, a.ziel_pct) if (
        a is not None and a.modus == "VORAB") else (a.modus, a.konto, a.ende_s, a.unabhaengig) if (
        a is not None and a.rest_wh is not None) else a
    vorgabe = (e.kurve if kurve_ok else None,
               vorgabe_auftrag if _auftrag_gueltig(a, e.jetzt_s) else None)
    o = z.optimierung
    # Die Frischeverlängerung desselben Wallbox-HALTEN ist kein neuer Auftrag.
    vergleich = vorgabe
    if (o is not None and isinstance(vorgabe[1], Auftrag)
            and isinstance(o.vorgabe[1], Auftrag) and a.modus == "HALTEN"
            and a.quelle.startswith(WALLBOX_QUELLENPRAEFIX)):
        vergleich = (vorgabe[0], replace(a, ende_s=o.vorgabe[1].ende_s))
    # Andere Vorgabenbeginne/-enden bleiben unmittelbare Anweisungen.
    faellig = (o is None or e.zeitbasis != z.zeitbasis or not _zahl(o.monoton_s)
               or not 0 <= e.monoton_s - o.monoton_s < OPTIMIERUNGSTAKT_S
               or vergleich != o.vorgabe)
    vorab = (_auftrag_gueltig(a, e.jetzt_s) and a.modus == "VORAB"
        and _zahl(a.ziel_pct) and 0 <= a.ziel_pct <= 100
        and _zahl(a.rate_w) and 300 <= a.rate_w <= entladen)
    if vorab:
        neu = replace(neu, vorab_latch=z.vorab_latch if not faellig or
            o is not None and e.zeitbasis == z.zeitbasis and o.vorgabe[1] == vorgabe_auftrag else
            a.ziel_pct if (o is None or e.zeitbasis != z.zeitbasis)
                and soc < a.ziel_pct + VORAB_ABSTAND_PP else None)
        if soc <= a.ziel_pct:
            neu = replace(neu, vorab_latch=a.ziel_pct)
        elif soc >= a.ziel_pct + VORAB_ABSTAND_PP:
            neu = replace(neu, vorab_latch=None)
        faellig = faellig or neu.vorab_latch != z.vorab_latch
    # Anschlussrahmen nur an einer Fälligkeit; der kurze Takt prüft
    # weiter die Daten, reagiert aber nicht auf eigene Leistungssprünge.
    netz = e.netz.zahl(e.jetzt_s, -LEISTUNG_MAX_W, LEISTUNG_MAX_W)
    akku = e.akku.zahl(e.jetzt_s, -LEISTUNG_MAX_W, LEISTUNG_MAX_W)
    anschluss = e.anschluss_a.zahl(e.jetzt_s, 0, 1000, GERAETE_FRIST_S)
    rahmen = None
    if (netz is not None and akku is not None and anschluss is not None
            and _zahl(e.anschluss_abstand_a) and e.anschluss_abstand_a >= 0):
        rahmen = (max(0.0, (anschluss - e.anschluss_abstand_a) * DREIPHASEN_W_PRO_A
                      - max(0.0, max(0.0, netz) - akku)) if faellig else o.netzrahmen_w)
    if (e.sonderfunktion or (_auftrag_gueltig(e.auftrag, e.jetzt_s)
                             and e.auftrag.modus not in ("HALTEN", "LADESP", "NETZLADEN", "VORAB"))):
        soll = Stellgroesse(ladegrenze_w=laden, entladegrenze_w=entlade_max, grenzen_frei=False)
        return _abschluss(e, z, replace(neu, optimierung=None), soll, _unbekannt("NICHT_ABGEDECKT"),
                           "NICHT_ABGEDECKT", "Eine Sonderfunktion wird von diesem Kern noch nicht abgedeckt.",
                           abgedeckt=False, rahmen=rahmen)

    haus = _hausbasis(e)
    pv = e.pv.zahl(e.jetzt_s, 0, LEISTUNG_MAX_W)
    rate = 0.0
    modus = "FREI"
    code = "FREI"
    if kurve_ok:
        k = e.kurve
        rate = (0.0 if soc >= k.deckel_pct else
                min(laden, max(0.0, (k.ziel_pct - soc) / 100.0 * k.kapazitaet_wh
                               * SEKUNDEN_PRO_STUNDE / (k.zielzeit_s - e.jetzt_s))))
        # Unter dem Korridor frei aufholen, ab dem Boden verzögert laden.
        modus = "FREI" if soc < k.boden_pct else "KURVE"
        if not faellig:
            rate, modus = o.rate_w, o.kandidat
        code = "KURVE" if modus == "KURVE" else "KURVE_AUFHOLEN"
        vorher = z.soll.modus if z.soll else None
        if vorher in ("FREI", "KURVE") and modus != vorher:
            zeit = z.kandidat_s + dt if z.kandidat == modus and dt else 0.0
            energie = (z.kandidat_wh if z.kandidat == modus and dt else 0.0)
            angebot = (max(0.0, pv - haus) if faellig and pv is not None and haus is not None
                       else ((o.budget.pv_w.wert_w or 0.0) + (o.budget.akkuanspruch_w or 0.0)
                             if (o.budget.pv_w.wert_w or 0.0) > 0 else 0.0)
                       if not faellig else 0.0)
            energie += abs(min(laden, angebot) - min(rate, angebot)) * dt / SEKUNDEN_PRO_STUNDE
            neu = replace(neu, kandidat=modus, kandidat_s=zeit, kandidat_wh=energie)
            if zeit < MODUS_ZEIT_S or energie < MODUS_ENERGIE_WH:
                modus, code = vorher, "KORRIDOR_HYSTERESE"
    kandidat = ("FREI" if soc < e.kurve.boden_pct else "KURVE") if kurve_ok and faellig else (
        o.kandidat if not faellig else "FREI")
    ladeseite = modus
    if _auftrag_gueltig(a, e.jetzt_s):
        if a.modus == "HALTEN":
            modus, code = "HALTEN", "PLAN_HALTEN"
        elif a.modus == "LADESP":
            modus, rate, code = "KURVE", 0.0, "NUTZER_LADESP"
        elif a.modus == "VORAB" and vorab:
            if neu.vorab_latch is not None:
                code = "ZIEL_ERREICHT"
            elif not kurve_ok or soc >= e.kurve.boden_pct:
                rate = a.rate_w if faellig else o.rate_w
                code = "PLAN_VORAB"
            else:
                code = "KURVE_AUFHOLEN"
        elif (a.modus == "NETZLADEN" and _zahl(a.rate_w) and a.rate_w > 0
              and _zahl(a.ziel_pct) and 0 <= a.ziel_pct <= 100):
            if soc < a.ziel_pct and rahmen is not None and rahmen > 0:
                modus, code = "NETZLADEN", "PLAN_NETZLADEN"
            elif soc >= a.ziel_pct:
                # Ziel bis zum Auftragsende erhalten: derselbe HALTEN-Modus,
                # kein Ziel-Latch, kein neuer Timer und keine Ladesperre.
                modus, code = "HALTEN", "ZIEL_ERREICHT"
            else:
                modus, code = "HALTEN", "NETZRAHMEN_UNBEKANNT" if rahmen is None else "NETZRAHMEN_NULL"

    bedarf = e.einspeise_ladebedarf.zahl(e.jetzt_s, 0, LEISTUNG_MAX_W)
    kurvenkappe = min(laden, max(rate, bedarf or 0.0)) if faellig else min(laden, o.kurvenkappe_w)
    ladegrenze = (kurvenkappe
                 if modus == "KURVE" or modus == "HALTEN" and ladeseite == "KURVE" else laden)
    if kurve_ok and soc >= e.kurve.deckel_pct:
        ladegrenze = min(ladegrenze, bedarf or 0.0)
    if code in ("NUTZER_LADESP", "PLAN_VORAB"):
        ladegrenze = 0.0
    gridrate = (min(a.rate_w, laden) if _auftrag_gueltig(a, e.jetzt_s)
                and a.modus == "NETZLADEN" and _zahl(a.rate_w) and a.rate_w > 0 else None)
    if not faellig:
        gridrate = min(laden, o.grid_w) if o.grid_w is not None else None
    if endliche_rate is not None:
        gridrate = min(laden, endliche_rate)
    grid = min(gridrate, rahmen) if modus == "NETZLADEN" and gridrate is not None else None
    # Einspeisedruck öffnet nur innerhalb des erlaubten Quellenrahmens; nur
    # tatsächliches Netzladen ist kein PV-Ladepfad und bleibt davon frei.
    if e.quellen_ladekappe is not None and modus != "NETZLADEN":
        quellenkappe = e.quellen_ladekappe.zahl(e.jetzt_s, 0, LEISTUNG_MAX_W)
        ladegrenze = min(ladegrenze, quellenkappe if quellenkappe is not None else 0.0)
    # Genau ein Anspruch; niemals Akku-Istleistung oder Wallboxlast als Rate.
    anspruch = (grid if modus == "NETZLADEN" else rate if kurve_ok else 0.0)
    if code in ("NUTZER_LADESP", "PLAN_VORAB"):
        anspruch = 0.0
    pv_anteil = (Anteil(None, False, "PV_ODER_HAUS_UNGUELTIG") if pv is None or haus is None
                 else Anteil(max(0.0, pv - haus - anspruch), True, "PV_NACH_HAUS_UND_ANSPRUCH"))
    hausbedarf = (max(0.0, haus - pv) + HAUS_AUFSCHLAG_W if haus is not None and pv is not None else None)
    # Ein Aufruf: Der Produktvertrag liefert Band und PV-Rahmen.
    boxen = [v for v in e.verbraucher if v.art.startswith("wallbox_")]
    boxwerte = [v.leistung.zahl(e.jetzt_s, 0, LEISTUNG_MAX_W) for v in boxen]
    box_w = sum(w for w in boxwerte if w is not None)
    boeden = [(_vorab_boden(reserve, a.ziel_pct)
        if code == "PLAN_VORAB" and v.vorab_angemeldet else v.boden_pct) for v in boxen]
    boeden = [b for b in boeden if _zahl(b)]
    h = e.haltezone
    zone = wbminsoc_hold_zone_contract(eligible=h.eligible and not aktiv and not nur_geraet
        and modus not in ("HALTEN", "NETZLADEN") and haus is not None and pv is not None
        and not any(v.gesperrt for v in boxen), mode=h.mode, soc=soc,
        floor_soc=(None if code == "PLAN_VORAB" and reserve is None else
            max(boeden) if boeden else None),
        soc_hysteresis_pct=h.hysterese_pct, restart_above_pct=h.neustart_pct,
        wallbox_w=box_w, wallbox_measurement_valid=bool(boxwerte and None not in boxwerte),
        residual_w=(pv - haus - sum(v.leistung.zahl(e.jetzt_s, 0, LEISTUNG_MAX_W) or 0
            for v in e.verbraucher if v.laeuft and not waerme_hauslast(v))
            if pv is not None and haus is not None else 0),
        previous=z.haltezone, now_s=e.jetzt_s)
    neu = replace(neu, haltezone={key: zone[key] for key in ("band_inside", "ts")})
    if zone["active"]:
        # Produktregel: Im Band keine vorrangige Kurvenladung.
        anspruch = 0.0
        pv_anteil = Anteil(max(0.0, pv - haus), True, "PV_HALTEZONE")
    elif not faellig and pv is not None and haus is not None:
        pv_anteil = o.budget.pv_w
        anspruch = o.budget.akkuanspruch_w
    anteile = tuple(_vorab_anteil(v, e, soc, reserve, aktiv or nur_geraet,
        modus, kurve_ok, entlade_max, hausbedarf, zone, code == "PLAN_VORAB", rate)
        for v in e.verbraucher)
    gruppen, normalgruppen = {}, {}
    for anteil in anteile:
        art = "wallbox" if anteil.verbraucher.art.startswith("wallbox_") else anteil.verbraucher.art
        gruppen[art] = gruppen.get(art, 0.0) + max(0.0, anteil.battery_w.wert_w or 0.0)
        normalgruppen[art] = normalgruppen.get(art, 0.0) + max(0.0, anteil.normal_w or 0.0)
    batterie = Anteil(min(max(0.0, entlade_max - (hausbedarf or 0.0)),
        max(gruppen.values(), default=0.0)), True, "INDIVIDUELLE_AKKUVERTRAEGE")
    if code == "PLAN_VORAB":
        # Ein zusätzlicher Topf, auch bei mehreren angemeldeten Boxen.
        # F1: Der bestehende normale Topf wird dadurch nicht verkleinert.
        normal_w = min(max(0.0, entlade_max - (hausbedarf or 0.0)),
            max(normalgruppen.values(), default=0.0))
        batterie = replace(batterie, wert_w=max(normal_w, min(rate, batterie.wert_w)))
    # Ein aktueller Deckel: Erlaubte externe Last bleibt im Hausmesspunkt.
    # Nur gemessene, nicht getragene Anteile verlassen ihn genau einmal.
    # Solange ein gemessener Verbraucher nicht voll getragen wird, bindet der
    # Deckel die Akkuleistung; nur dann folgt die gemessene Restlast in halben
    # Schritten (kein Zweitaktpendeln). Rechte gelten immer ungeglättet; nach
    # einem Wechsel der Zuordnung gilt der Messwert.
    haus_roh = e.haus.zahl(e.jetzt_s, HAUS_MIN_W, LEISTUNG_MAX_W)
    deckel = None
    gemessen = haus_roh is not None and pv is not None
    # Eine Einteilung je gemessenem Verbraucher, auch ohne gültigen Haus- oder
    # PV-Wert: getragen (Akkurecht), offen (Rest) und eingeschränkt (kein volles
    # Akkurecht; eine Box in Haltezone oder am Boden ist getragen, aber eingeschränkt).
    restlast, rechte, offen, eingeschraenkt = (haus_roh - pv if gemessen else 0.0), 0.0, 0.0, False
    for anteil in anteile:
        v = anteil.verbraucher
        if not _ladebelegt(v, e.jetzt_s) or _als_hauslast(anteil):
            continue
        w = v.watt(e.jetzt_s)
        eingeschraenkt = eingeschraenkt or (anteil.battery_w.wert_w or 0.0) < w
        getragen = (w if v.art.startswith("wallbox_") and anteil.battery_w.grund in (
            "AKKU_NICHT_FREIGEGEBEN:wbminsoc_hold_zone",
            "AKKU_NICHT_FREIGEGEBEN:boden") else
            min(w, max(0.0, anteil.battery_w.wert_w or 0.0)))
        rechte += getragen
        offen += w - getragen
        if v.im_haus:
            restlast -= w
    if gemessen:
        # Hauslast-Verbraucher bleiben im Hausmesspunkt; ihre Belegung gehört nicht zur Zuordnung.
        zuordnung = tuple((a.verbraucher.art, _ladebelegt(a.verbraucher, e.jetzt_s) and not _als_hauslast(a),
                           a.battery_w.grund) for a in anteile)
        if offen > 0:
            restlast = _restlast_nachfuehren(restlast, e, z, zuordnung)
        neu = replace(neu, restlast_w=restlast, restlast_zuordnung=zuordnung)
        deckel = max(0.0, restlast + rechte) + HAUS_AUFSCHLAG_W
    entladedeckel = min(entladen, deckel) if deckel is not None else None
    entladegrenze = min(entlade_max, entladedeckel) if entladedeckel is not None else 0.0
    # Ohne auszuschließende Istlast bleibt der reservebegrenzte Rahmen.
    # Nach der Überbrückung gilt ohne Messung „Haus inkl. Verbraucher“.
    if not eingeschraenkt:
        entladegrenze = entlade_max
    if modus == "HALTEN":
        entladegrenze = 0.0

    # Ein alleiniger belegter Geräteboden hat keine zweite eigene Nullgrenze.
    # Andere Vorgaben (HALTEN, Kurve, Ladesperre) bleiben unabhängig wirksam.
    if (nur_geraet and modus == "FREI" and not kurve_ok and not reserve_vorgabe
            and not any(v.netzgedeckt and _ladebelegt(v, e.jetzt_s)
                for v in e.verbraucher)):
        soll = Stellgroesse()
    else:
        soll = Stellgroesse(modus, ladegrenze, entladegrenze, False, grid)
    # S5: ein Totband, gegen den letzten ausgegebenen Sollsatz. Harte Kappen
    # und Nullgrenzen wirken sofort; kein Transport-/Besitzergedächtnis.
    if z.soll and soll.modus == z.soll.modus and not z.soll.grenzen_frei:
        werte = {}
        for feld, maximum in (("ladegrenze_w", laden), ("entladegrenze_w", entlade_max)):
            alt, wert = getattr(z.soll, feld), getattr(soll, feld)
            if (alt is not None and wert is not None and 0 < wert and alt <= maximum
                    and abs(wert - alt) < SCHREIBTOTBAND_W):
                werte[feld] = alt
        soll = replace(soll, **werte)
        if (grid is not None and z.soll.grid_soll_w is not None
                and z.soll.grid_soll_w <= min(laden, rahmen, endliche_rate if endliche_rate is not None else laden)
                and abs(grid - z.soll.grid_soll_w) < SCHREIBTOTBAND_W):
            soll = replace(soll, grid_soll_w=z.soll.grid_soll_w)
    if a is not None and a.rest_wh is not None and grid is not None:
        if grid < 300:
            soll = replace(soll, modus="FREI", grid_soll_w=None)
        else:
            soll = replace(soll, grid_rest_wh=a.rest_wh, grid_gueltig_bis_s=a.ende_s,
                           grid_dauer_s=e.ausgabe_dauer_s)
            neu = replace(neu, netzauftrag_rate_w=soll.grid_soll_w)
    # Unterschiedliche Namen bei identischer Hardwarewirkung sind kein Wechsel.
    if (z.soll and {soll.modus, z.soll.modus} == {"FREI", "KURVE"}
            and soll.ladegrenze_w == z.soll.ladegrenze_w
            and soll.entladegrenze_w == z.soll.entladegrenze_w):
        soll = replace(soll, modus=z.soll.modus)
    # Kappe des gehaltenen PV-Anteils gegen die aktuelle Hardware zwischen Takten.
    rahmen_w = (min(pv_anteil.wert_w + batterie.wert_w, pv + entlade_max)
        if pv_anteil.gueltig else None)
    budget = Budget(pv_anteil, batterie,
        o.budget.house_base_w if not faellig and not zone["active"] and haus is not None and pv is not None else haus,
        anspruch, rahmen_w, anteile, zone,
        house_base_quelle=("gehaltene_optimierung:" if not faellig and not zone["active"]
            and haus is not None and pv is not None else "aktuelle_verbraucherbereinigung:") + e.haus.quelle)
    if vorab and not aktiv:
        budget = replace(budget, vorab_rate_w=rate if code == "PLAN_VORAB" else None,
            vorab_boden_pct=_vorab_boden(reserve, a.ziel_pct))
    if faellig:
        neu = replace(neu, optimierung=Optimierung(e.monoton_s, vorgabe, budget, rate,
            kandidat, kurvenkappe, gridrate, rahmen))
    if deckel is None:
        code = "HAUS_UNGUELTIG:" + e.haus.grund if haus_roh is None else "PV_UNGUELTIG:" + e.pv.grund
        budget = replace(budget, battery_w=Anteil(None, False, code))
    if aktiv:
        code = "RESERVE_ENTLADUNG_GESPERRT"
    return _abschluss(e, z, neu, soll, budget, code,
                       f"{soll.modus}: Grund {code}.",
                       rahmen=rahmen)
