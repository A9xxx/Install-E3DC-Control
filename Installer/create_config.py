import os
import shutil

from .core import register_command
from .installer_config import get_install_path, get_home_dir, get_user_ids, get_www_data_gid
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_PATH = get_install_path()
CONFIG_FILE = os.path.join(INSTALL_PATH, "e3dc.config.txt")
config_logger = get_or_create_logger("config")


def ask(prompt, default=None):
    """Fragt Benutzer mit Standardwert ab."""
    value = input(f"{prompt} [{default}]: ").strip()
    return value if value else default


def write_param(f, key, value, enabled=True):
    """Schreibt einen Parameter aktiv oder auskommentiert."""
    prefix = "" if enabled else "#"
    f.write(f"{prefix}{key} = {value}\n")


def copy_existing_config():
    """Kopiert eine vorhandene e3dc.config.txt in den Zielordner."""
    print("\n--- Vorhandene Konfiguration kopieren ---\n")
    config_logger.info("Versuche, eine vorhandene Konfiguration zu kopieren.")
    
    default_source = os.path.join(get_home_dir(), "Install", "e3dc.config.txt")
    source_path = ask("Pfad zur vorhandenen e3dc.config.txt", default_source)
    
    if not os.path.exists(source_path):
        print(f"✗ Datei nicht gefunden: {source_path}")
        log_warning("create_config", f"Zu kopierende Konfigurationsdatei nicht gefunden: {source_path}")
        return False
    
    if not os.path.isfile(source_path):
        print(f"✗ Kein gültiger Dateipfad: {source_path}")
        log_warning("create_config", f"Ungültiger Pfad für Konfigurationsdatei angegeben: {source_path}")
        return False
    
    try:
        # Zielverzeichnis erstellen falls nicht vorhanden
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        
        # Datei kopieren
        shutil.copy2(source_path, CONFIG_FILE)
        print(f"✓ Datei kopiert: {source_path} → {CONFIG_FILE}")
        config_logger.info(f"Konfigurationsdatei kopiert von {source_path} nach {CONFIG_FILE}")
        
        # Berechtigungen setzen
        try:
            uid, _ = get_user_ids()
            os.chown(CONFIG_FILE, uid, get_www_data_gid())
            os.chmod(CONFIG_FILE, 0o664)  # rw-rw-r--
            print(f"✓ Berechtigungen gesetzt (664, Besitzer: UID {uid})")
            config_logger.info(f"Berechtigungen für {CONFIG_FILE} gesetzt.")
        except Exception as e:
            print(f"⚠ Warnung: Berechtigungen konnten nicht vollständig gesetzt werden: {e}")
            log_warning("create_config", f"Berechtigungen für kopierte Konfigurationsdatei konnten nicht gesetzt werden: {e}")
        
        print(f"\n✓ Konfiguration erfolgreich kopiert und installiert!\n")
        log_task_completed("Konfiguration erstellen", details="Vorhandene Konfiguration kopiert")
        return True
        
    except Exception as e:
        print(f"✗ Fehler beim Kopieren der Datei: {e}")
        log_error("create_config", f"Fehler beim Kopieren der Konfigurationsdatei: {e}", e)
        return False


def create_e3dc_config():
    """Kompletter Config-Wizard mit allen Parametern und Defaults."""
    print("\n=== E3DC-Konfiguration erstellen ===\n")
    config_logger.info("Starte Konfigurations-Wizard.")
    
    # Prüfen ob vorhandene Config kopiert werden soll
    copy_existing = ask("Möchtest du eine vorhandene e3dc.config.txt kopieren? (j/n)", "n")
    
    if copy_existing and copy_existing.lower() == "j":
        if copy_existing_config():
            return  # Erfolgreich kopiert, Wizard beenden
        else:
            print("\nFortfahren mit manuellem Wizard...\n")
            config_logger.warning("Kopieren der Konfiguration fehlgeschlagen, fahre mit manuellem Wizard fort.")
    
    cfg = {}

    # =========================================================
    # GRUNDDATEN
    # =========================================================
    print("--- GRUNDDATEN ---\n")
    cfg["server_ip"] = ask("E3DC IP-Adresse", "192.168.178.2")
    cfg["server_port"] = ask("Port", "5033")
    cfg["user"] = ask("Benutzername", "local.user")
    cfg["password"] = ask("Passwort", "1234")
    cfg["aes"] = ask("AES-Passwort", "1234")
    cfg["stop"] = ask("Stop-Flag", "0")

    # =========================================================
    # LEISTUNGS- UND SPEICHERPARAMETER
    # =========================================================
    print("\n--- LEISTUNGS- UND SPEICHERPARAMETER ---\n")
    cfg["wrleistung"] = ask("Wechselrichterleistung (W)", "11700")
    cfg["speichergroesse"] = ask("Speichergröße (kWh)", "35")
    cfg["speicherEV"] = ask("Speicher Eigenverbrauch (W)", "80")
    cfg["speicherETA"] = ask("Speicher Wirkungsgrad", "0.97")
    cfg["einspeiselimit"] = ask("Einspeiselimit (kW)", "10.3")
    cfg["unload"] = ask("Unload (%)", "65")
    cfg["ladeschwelle"] = ask("Ladeschwelle (%)", "70")
    cfg["ladeende"] = ask("Ladeende (%)", "85")
    cfg["ladeende2"] = ask("Ladeende2 (%)", "91")
    cfg["Ladeende2rampe"] = ask("Ladeende2-Rampe", "2")
    cfg["maximumLadeleistung"] = ask("Maximale Ladeleistung (W)", "12500")
    cfg["powerfaktor"] = ask("Powerfaktor", "1.75")
    cfg["rb"] = ask("Regelbeginn", "7")
    cfg["re"] = ask("Regelende", "12.5")
    cfg["le"] = ask("Ladeende", "14.2")

    # =========================================================
    # WALLBOX
    # =========================================================
    print("\n--- WALLBOX ---\n")
    wb = ask("Wallbox vorhanden? (j/n)", "j")
    cfg["wallbox"] = (wb.lower() == "j")

    if cfg["wallbox"]:
        cfg["wbmode"] = ask("WB Modus", "4")
        cfg["wbminlade"] = ask("WB Mindestladeleistung (W)", "1200")
        cfg["wbminSoC"] = ask("WB Mindest-SoC (%)", "85")
        cfg["wbmaxladestrom"] = ask("WB Maximalstrom (A)", "32")
        cfg["wbminladestrom"] = ask("WB Mindeststrom (A)", "6")
        cfg["wbhour"] = ask("WB hour-Modus", "0")
        cfg["Wbvon"] = ask("WB hour Startzeit", "22")
        cfg["Wbbis"] = ask("WB hour Endzeit", "6")
    else:
        # Defaults setzen, falls keine Wallbox
        cfg["wbmode"] = ""
        cfg["wbminlade"] = ""
        cfg["wbminSoC"] = ""
        cfg["wbmaxladestrom"] = ""
        cfg["wbminladestrom"] = ""
        cfg["wbhour"] = ""
        cfg["Wbvon"] = ""
        cfg["Wbbis"] = ""

    # =========================================================
    # WÄRMEPUMPE
    # =========================================================
    print("\n--- WÄRMEPUMPE ---\n")
    wp = ask("Wärmepumpe vorhanden? (j/n)", "n")
    cfg["WP"] = (wp.lower() == "j")

    if cfg["WP"]:
        cfg["shellyem_ip"] = ask("Shelly EM IP", "192.168.178.163")
        cfg["WPHeizlast"] = ask("Heizlast (kW)", "18")
        cfg["WPHeizgrenze"] = ask("Heizgrenze (°C)", "13")
        cfg["WPLeistung"] = ask("Heizleistung (kW)", "20")
        cfg["WPMin"] = ask("Min-Verbrauch (kW)", "0.5")
        cfg["WPMax"] = ask("Max-Verbrauch (kW)", "4.7")
    else:
        # Defaults setzen
        cfg["shellyem_ip"] = ""
        cfg["WPHeizlast"] = ""
        cfg["WPHeizgrenze"] = ""
        cfg["WPLeistung"] = ""
        cfg["WPMin"] = ""
        cfg["WPMax"] = ""

    # =========================================================
    # AWATTAR
    # =========================================================
    print("\n--- AWATTAR ---\n")
    aw = ask("Awattar aktiv? (j/n)", "j")
    cfg["awattar"] = (aw.lower() == "j")

    if cfg["awattar"]:
        cfg["awmwst"] = ask("MwSt (%)", "19")
        cfg["awnebenkosten"] = ask("Nebenkosten (ct)", "15.915")
        cfg["awaufschlag"] = ask("Aufschlag (%)", "10")
        cfg["awland"] = ask("Land (de/at/ch)", "de")
        cfg["awreserve"] = ask("Reserve (%)", "20")
    else:
        cfg["awmwst"] = ""
        cfg["awnebenkosten"] = ""
        cfg["awaufschlag"] = ""
        cfg["awland"] = ""
        cfg["awreserve"] = ""

    # =========================================================
    # OPENMETEO + FORECAST
    # =========================================================
    print("\n--- OPENMETEO & FORECAST ---\n")
    om = ask("OpenMeteo aktiv? (j/n)", "j")
    cfg["openmeteo"] = (om.lower() == "j")

    if cfg["openmeteo"]:
        cfg["hoehe"] = ask("Breitengrad (°N)", "48.00000")
        cfg["laenge"] = ask("Längengrad (°E)", "13.00000")

        print("\n--- Forecast Parameter ---")
        cfg["forecast1"] = ask("Forecast1 (Neigung/Azimut/kWp)", "30/0/15.4")

        # Forecast 2?
        f2 = ask("Forecast2 hinzufügen? (j/n)", "n")
        cfg["forecast2_enabled"] = (f2.lower() == "j")
        if cfg["forecast2_enabled"]:
            cfg["forecast2"] = ask("Forecast2 (Neigung/Azimut/kWp)", "0/90/5.0")
        else:
            cfg["forecast2"] = ""

        # Forecast 3?
        f3 = ask("Forecast3 hinzufügen? (j/n)", "n")
        cfg["forecast3_enabled"] = (f3.lower() == "j")
        if cfg["forecast3_enabled"]:
            cfg["forecast3"] = ask("Forecast3 (Neigung/Azimut/kWp)", "0/-90/5.0")
        else:
            cfg["forecast3"] = ""

        cfg["ForecastSoc"] = ask("Forecast SOC-Faktor", "1.2")
        cfg["ForecastConsumption"] = ask("Forecast Verbrauchsfaktor", "1")
        cfg["ForecastReserve"] = ask("Forecast Reserve (%)", "5")
    else:
        # Defaults setzen
        cfg["hoehe"] = ""
        cfg["laenge"] = ""
        cfg["forecast1"] = ""
        cfg["forecast2"] = ""
        cfg["forecast3"] = ""
        cfg["forecast2_enabled"] = False
        cfg["forecast3_enabled"] = False
        cfg["ForecastSoc"] = ""
        cfg["ForecastConsumption"] = ""
        cfg["ForecastReserve"] = ""

    # =========================================================
    # DATEI SCHREIBEN
    # =========================================================
    write_e3dc_config(cfg)
    print(f"\n✓ Konfiguration gespeichert unter {CONFIG_FILE}\n")
    log_task_completed("Konfiguration erstellen", details="Manuell über Wizard erstellt")


def write_e3dc_config(cfg):
    """Schreibt die Konfigurationsdatei."""
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        
        # Umask setzen für korrekte File-Berechtigungen
        old_umask = os.umask(0o002)
        try:
            with open(CONFIG_FILE, "w") as f:
                # Grunddaten
                write_param(f, "server_ip", cfg["server_ip"])
                write_param(f, "server_port", cfg["server_port"])
                write_param(f, "e3dc_user", cfg["user"])
                write_param(f, "e3dc_password", cfg["password"])
                write_param(f, "aes_password", cfg["aes"])
                write_param(f, "stop", cfg["stop"])

                f.write("\n# Leistungs- und Speicherparameter\n")
                write_param(f, "wrleistung", cfg["wrleistung"])
                write_param(f, "speichergroesse", cfg["speichergroesse"])
                write_param(f, "speicherEV", cfg["speicherEV"])
                write_param(f, "speicherETA", cfg["speicherETA"])
                write_param(f, "einspeiselimit", cfg["einspeiselimit"])
                write_param(f, "unload", cfg["unload"])
                write_param(f, "ladeschwelle", cfg["ladeschwelle"])
                write_param(f, "ladeende", cfg["ladeende"])
                write_param(f, "ladeende2", cfg["ladeende2"])
                write_param(f, "Ladeende2rampe", cfg["Ladeende2rampe"])
                write_param(f, "maximumLadeleistung", cfg["maximumLadeleistung"])
                write_param(f, "powerfaktor", cfg["powerfaktor"])
                write_param(f, "rb", cfg["rb"])
                write_param(f, "re", cfg["re"])
                write_param(f, "le", cfg["le"])

                # Wallbox
                f.write("\n# Wallbox Parameter\n")
                write_param(f, "wallbox", str(cfg["wallbox"]).lower())
                write_param(f, "wbmode", cfg.get("wbmode", ""), cfg["wallbox"])
                write_param(f, "wbminlade", cfg.get("wbminlade", ""), cfg["wallbox"])
                write_param(f, "wbminSoC", cfg.get("wbminSoC", ""), cfg["wallbox"])
                write_param(f, "wbmaxladestrom", cfg.get("wbmaxladestrom", ""), cfg["wallbox"])
                write_param(f, "wbminladestrom", cfg.get("wbminladestrom", ""), cfg["wallbox"])
                write_param(f, "wbhour", cfg.get("wbhour", ""), cfg["wallbox"])
                write_param(f, "Wbvon", cfg.get("Wbvon", ""), cfg["wallbox"])
                write_param(f, "Wbbis", cfg.get("Wbbis", ""), cfg["wallbox"])

                # Wärmepumpe
                f.write("\n# Wärmepumpe Parameter\n")
                write_param(f, "WP", str(cfg["WP"]).lower())
                write_param(f, "shellyem_ip", cfg.get("shellyem_ip", ""), cfg["WP"])
                write_param(f, "WPHeizlast", cfg.get("WPHeizlast", ""), cfg["WP"])
                write_param(f, "WPHeizgrenze", cfg.get("WPHeizgrenze", ""), cfg["WP"])
                write_param(f, "WPLeistung", cfg.get("WPLeistung", ""), cfg["WP"])
                write_param(f, "WPMin", cfg.get("WPMin", ""), cfg["WP"])
                write_param(f, "WPMax", cfg.get("WPMax", ""), cfg["WP"])

                # Awattar
                f.write("\n# Awattar Parameter\n")
                write_param(f, "awattar", str(cfg["awattar"]).lower())
                write_param(f, "awmwst", cfg.get("awmwst", ""), cfg["awattar"])
                write_param(f, "awnebenkosten", cfg.get("awnebenkosten", ""), cfg["awattar"])
                write_param(f, "awaufschlag", cfg.get("awaufschlag", ""), cfg["awattar"])
                write_param(f, "awland", cfg.get("awland", ""), cfg["awattar"])
                write_param(f, "awreserve", cfg.get("awreserve", ""), cfg["awattar"])

                # OpenMeteo + Forecast
                f.write("\n# OpenMeteo & Forecast Parameter\n")
                write_param(f, "openmeteo", str(cfg["openmeteo"]).lower())
                write_param(f, "hoehe", cfg.get("hoehe", ""), cfg["openmeteo"])
                write_param(f, "laenge", cfg.get("laenge", ""), cfg["openmeteo"])
                write_param(f, "forecast1", cfg.get("forecast1", ""), cfg["openmeteo"])
                write_param(f, "forecast2", cfg.get("forecast2", ""), cfg.get("forecast2_enabled", False))
                write_param(f, "forecast3", cfg.get("forecast3", ""), cfg.get("forecast3_enabled", False))
                write_param(f, "ForecastSoc", cfg.get("ForecastSoc", ""), cfg["openmeteo"])
                write_param(f, "ForecastConsumption", cfg.get("ForecastConsumption", ""), cfg["openmeteo"])
                write_param(f, "ForecastReserve", cfg.get("ForecastReserve", ""), cfg["openmeteo"])
        finally:
            os.umask(old_umask)
        
        config_logger.info(f"Konfigurationsdatei erfolgreich geschrieben: {CONFIG_FILE}")
        
        # Setze korrekten Owner und Berechtigungen
        try:
            uid, _ = get_user_ids()
            os.chown(CONFIG_FILE, uid, get_www_data_gid())
            os.chmod(CONFIG_FILE, 0o664)      # rw-rw-r-- damit PHP schreiben kann
            config_logger.info(f"Berechtigungen für {CONFIG_FILE} gesetzt.")
        except Exception as e:
            print(f"⚠ Warnung: Berechtigungen konnten nicht vollständig gesetzt werden: {e}")
            log_warning("create_config", f"Berechtigungen für {CONFIG_FILE} konnten nicht gesetzt werden: {e}")
            pass

        return True
    except Exception as e:
        print(f"✗ Fehler beim Schreiben der Konfiguration: {e}")
        log_error("create_config", f"Fehler beim Schreiben der Konfigurationsdatei: {e}", e)
        return False


register_command("7", "E3DC-Konfiguration erstellen", create_e3dc_config, sort_order=70)