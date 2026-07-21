import os
import json

from .core import register_command
from .config_secret_permissions import apply_config_secret_permissions
from .installer_config import get_install_path
from .logging_manager import get_or_create_logger, log_task_completed, log_error

INSTALL_PATH  = get_install_path()
V4_CONFIG     = '/var/www/html/data/e3dc_v4.json'
config_logger = get_or_create_logger('config')

# Nur die absolut notwendigen Keys fuer den ersten Start.
# Alles weitere wird komfortabel ueber die Web-UI konfiguriert!
WIZARD_KEYS = [
    # === PFLICHT: Ohne diese Keys startet gar nichts ===
    ('server_ip',      'E3DC IP-Adresse',              ''),
    ('server_port',    'RSCP-Port',                    '5033'),
    ('e3dc_user',      'E3DC Portal Benutzername',     ''),
    ('e3dc_password',  'E3DC Portal Passwort',         ''),
    ('aes_password',   'RSCP AES-Passwort (am Geraet vergeben)', ''),
    ('wurzelzaehler',  'Legacy-PM-Index fuer Phasendiagnose (0=auto)', '0'),
    ('wurzelzaehler_invertiert', 'Legacy-PM-Phasen invertiert (0/1)', '0'),

    # === PFLICHT: Standort fuer PV-Prognose ===
    ('hoehe',          'Breitengrad (Latitude, z.B. 48.604)', '48.60442'),
    ('laenge',         'Laengengrad (Longitude, z.B. 13.415)', '13.41513'),
    ('forecast1',      'PV-Anlage: Neigung/Azimuth/kWp (z.B. 40/-50/15.4)', '40/-50/15.4'),
    ('speichergroesse','Speichergroesse in kWh (Fallback)',   '15'),
    ('ems_budget_runtime_enable', 'EMS-Budget-Runtime (0=Shadow, 1=zentraler Budget-Executor)', '0'),

    # === EMPFOHLEN: Tarif fuer korrekte Kostenberechnung ===
    ('stromtarif_typ', 'Tarifmodell (static/tibber/awattar/octopus)', 'static'),
    ('strompreis_basis','Arbeitspreis in ct/kWh',       '25.0'),
    ('tariff_provider','Preisquelle (smard/entsoe/awattar/tibber)', 'smard'),

    # === OPTIONAL: Direktvermarktung Central Policy ===
    ('direct_marketing_settlement_basis', 'DV-Abrechnungsbasis (aktive Regelung: day_ahead_15min)', 'day_ahead_15min'),
    ('direct_marketing_profit_profile', 'DV-Profitprofil (standard/aggressive/expert)', 'standard'),
    ('direct_marketing_min_window_profit_eur', 'DV-Mindestgewinn je Verkaufsfenster in EUR', '0.25'),
    ('direct_marketing_min_export_energy_kwh', 'DV-Mindestenergie je Verkaufsfenster in kWh', '1.5'),
    ('direct_marketing_min_export_window_min', 'DV-Technisches Mindestfenster in Minuten', '15'),
    ('direct_marketing_preferred_export_plateau_min', 'DV-Bevorzugte Plateau-Dauer in Minuten', '60'),
    ('direct_marketing_price_plateau_tolerance_ct', 'DV-Preisplateau-Toleranz in ct/kWh', '0.75'),
    ('direct_marketing_max_daily_export_kwh', 'DV-Maximaler Batterieexport pro Tag in kWh (0=Zyklenlimit)', '0'),
    ('direct_marketing_deep_cycle_threshold_pct', 'DV-Tiefzyklus-Schwelle in Prozentpunkten', '20'),
    ('direct_marketing_deep_cycle_lcos_factor', 'DV-Tiefzyklus-LCOS-Faktor', '0.5'),
    ('direct_marketing_variable_fee_basis', 'DV-Basis der variablen Gebuehr (sell_revenue/eeg_compensation/manual)', 'sell_revenue'),
    ('direct_marketing_variable_fee_basis_ct_per_kwh', 'DV-Manuelle Gebuehrenbasis in ct/kWh', '0'),
    ('direct_marketing_service_vat_pct', 'DV-Umsatzsteuer auf Dienstleistungskosten in Prozent', '19'),
    ('direct_marketing_input_vat_recoverable', 'DV-Vorsteuer abziehbar (0/1)', '0'),
    ('direct_marketing_installed_kwp', 'DV-Abrechnungsleistung in kWp (0=PV-Prognose)', '0'),
    ('direct_marketing_balancing_cost_eur_per_kwp_month', 'DV-Ausgleichskosten-Abschlag in EUR/kWp/Monat', '0'),
    ('direct_marketing_balancing_cost_actual_eur_per_kwp_month', 'DV-Tatsaechliche Ausgleichskosten in EUR/kWp/Monat (leer=unbekannt)', ''),

    # === OPTIONAL: Direktvermarktung Zusatz-Wechselrichter-Shelly ===
    ('direct_marketing_aux_inverter_shelly_override', 'DV-Zusatz-WR Shelly-Steuerung (local/off oder central/on)', 'local'),
    ('direct_marketing_aux_inverter_shelly_ip', 'DV-Zusatz-WR Shelly-IP (leer = lokales Shelly-Skript bleibt Fallback)', ''),
    ('direct_marketing_aux_inverter_shelly_invert', 'DV-Zusatz-WR Shelly invertieren (1=NC-Schütz: Shelly an schaltet WR aus)', '0'),
    ('direct_marketing_aux_inverter_shelly_dynamic_unblock_enable', 'DV-Zusatz-WR bei hoher Last trotz Negativpreis freigeben', '0'),
    ('direct_marketing_aux_inverter_shelly_unblock_threshold_w', 'DV-Zusatz-WR Last-Freigabe ab Watt', '3000'),
]


def _normalize_numeric(value: str):
    """Normalisiert Komma-Dezimalzahl: '13,5' -> '13.5'."""
    if not value or ',' not in value:
        return value, False
    candidate = value.strip().replace(',', '.')
    try:
        float(candidate)
        return candidate, True
    except ValueError:
        return value, False


def _load_v4() -> dict:
    if not os.path.exists(V4_CONFIG):
        return {}
    try:
        with open(V4_CONFIG, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        config_logger.error(f'Lesefehler e3dc_v4.json: {e}')
        return {}


def _save_v4(data: dict) -> bool:
    tmp = V4_CONFIG + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(tmp, V4_CONFIG)
        apply_config_secret_permissions(V4_CONFIG, data=data)
        return True
    except Exception as e:
        config_logger.error(f'Schreibfehler e3dc_v4.json: {e}')
        return False


def config_wizard():
    """
    Minimalwizard fuer den ersten Start.
    Fragt nur die absolut notwendigen Keys ab.
    Alle weiteren Einstellungen bitte in der Web-UI vornehmen!
    """
    print('\n=== Ersteinrichtungs-Wizard (Minimal) ===')
    print('Nur die notwendigen Parameter fuer den ersten Start.')
    print('Alle weiteren Einstellungen danach bitte in der Web-UI!\n')
    config_logger.info('Starte Minimal Config-Wizard')

    config = _load_v4()
    if not config:
        print('[!] e3dc_v4.json nicht gefunden oder leer.')
        print(f'    Pfad: {V4_CONFIG}')
        print('    Bitte zuerst die Installation ausfuehren (Menuepunkt 1).')
        return

    print('Aktuelle Werte (Enter = unveraendert, "-" = loeschen):\n')

    updated = False
    for key, label, default in WIZARD_KEYS:
        current = config.get(key, default)
        display = f'[{current}]' if current else f'[Standard: {default}]'
        new_val = input(f'  {label}\n  {key} {display}: ').strip()

        if not new_val:
            # Wenn leer und Key noch nicht in Config: Default setzen
            if key not in config and default:
                config[key] = default
                print(f'  [OK] {key} = {default} (Default gesetzt)')
                updated = True
            continue

        if new_val == '-':
            config.pop(key, None)
            print(f'  [OK] {key} geloescht')
            updated = True
            continue

        normalized, changed = _normalize_numeric(new_val)
        if changed:
            print(f'  [i] Normalisiert: {new_val} -> {normalized}')
        config[key] = normalized
        print(f'  [OK] {key} = {normalized}')
        updated = True

    if updated:
        if _save_v4(config):
            print('\n[OK] Konfiguration gespeichert.\n')
            log_task_completed('Minimal-Konfiguration gespeichert')
        else:
            print('\n[!] Fehler beim Speichern der Konfiguration.\n')
            return

        print('Dienste neu starten damit Aenderungen sofort wirksam werden?')
        if input('Neu starten? (j/n) [j]: ').strip().lower() != 'n':
            from .utils import run_command
            print('[->] Starte Kerndienste neu...')
            for srv in ['e3dc-live', 'e3dc-storage-simulator', 'e3dc-epex-manager']:
                if os.path.exists(f'/etc/systemd/system/{srv}.service'):
                    res = run_command(f'sudo systemctl restart {srv}', timeout=15)
                    status = '[OK]' if res['success'] else '[!]'
                    print(f'  {status} {srv}')

        print('\n[->] Tipp: Alle weiteren Einstellungen bitte in der Web-UI konfigurieren!')
        print(f'          URL: http://<pi-ip>/config_editor.php\n')
    else:
        print('\n[->] Keine Aenderungen vorgenommen.\n')
        config_logger.info('Keine Aenderungen im Minimal Config-Wizard.')


register_command('8', 'Ersteinrichtung (Minimal-Wizard)', config_wizard, sort_order=80)
