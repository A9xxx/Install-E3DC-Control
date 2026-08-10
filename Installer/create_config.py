import hashlib
import io
import os
import pwd

from .core import register_command
from .installer_config import (
    get_home_dir,
    get_install_path,
    get_user_ids,
    get_www_data_gid,
)
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning
from .secure_file_transaction import (
    atomic_write_bound_file,
    exclusive_transaction_lock,
    read_bound_regular_file,
    restore_bound_file,
    snapshot_bound_file,
    snapshots_match,
)

config_logger = get_or_create_logger("config")
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_CONFIG_LOCK = "e3dc-product-config.lock"


def _resolve_config_authority(*, install_path=None, install_user=None):
    """Bindet Wizard und Ziel an Benutzer, passwd-Home und laufenden Release-Root."""

    bootstrap_user = str(os.environ.get("E3DC_BOOTSTRAP_USER") or "").strip()
    user = str(install_user or bootstrap_user).strip()
    if not user or user in {"root", "www-data"}:
        raise RuntimeError("Ein normaler Installationsbenutzer ist nicht gebunden")
    if bootstrap_user and bootstrap_user != user:
        raise RuntimeError(
            "Expliziter Installationsbenutzer widerspricht dem Bootstrap-Nutzer"
        )
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise RuntimeError("Der gebundene Installationsbenutzer existiert nicht") from exc

    home_dir = get_home_dir(user)
    if home_dir != str(account.pw_dir or "").strip():
        raise RuntimeError("Das passwd-Home des Installationsbenutzers ist nicht eindeutig")

    module_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    requested_root = str(install_path or get_install_path(user) or "").strip()
    if (
        not os.path.isabs(requested_root)
        or os.path.abspath(requested_root) != requested_root
        or os.path.realpath(requested_root) != requested_root
        or requested_root != module_root
        or os.path.realpath(module_root) != module_root
    ):
        raise RuntimeError(
            "Konfigurationsziel entspricht nicht dem laufenden Produktroot"
        )

    uid, _ = get_user_ids(user)
    gid = get_www_data_gid()
    return {
        "install_user": user,
        "home_dir": home_dir,
        "install_path": requested_root,
        "config_path": os.path.join(requested_root, "e3dc.config.txt"),
        "uid": int(uid),
        "gid": int(gid),
    }


def _project_config_payload(payload, authority, *, source_snapshot=None):
    """Projiziert exakt diese Bytes mit Readback und semantischem Rollback."""

    if not isinstance(payload, bytes) or len(payload) > _MAX_CONFIG_BYTES:
        raise RuntimeError("Konfigurationsbytes liegen außerhalb des Größenvertrags")
    target = str(authority["config_path"])
    uid = int(authority["uid"])
    gid = int(authority["gid"])
    payload_sha = hashlib.sha256(payload).hexdigest()

    with exclusive_transaction_lock(_CONFIG_LOCK):
        if (
            source_snapshot is not None
            and str(source_snapshot.get("path") or "") == target
        ):
            preimage = source_snapshot
        else:
            preimage = snapshot_bound_file(
                target,
                allow_missing=True,
                max_bytes=_MAX_CONFIG_BYTES,
            )
        committed = None
        try:
            committed = atomic_write_bound_file(
                target,
                payload,
                uid=uid,
                gid=gid,
                mode=0o640,
                expected_snapshot=preimage,
                max_existing_bytes=_MAX_CONFIG_BYTES,
            )
            readback = read_bound_regular_file(
                target,
                expected_uid=uid,
                expected_gid=gid,
                max_bytes=_MAX_CONFIG_BYTES,
            )
            if (
                not snapshots_match(readback, committed, exact_metadata=True)
                or readback.get("sha256") != payload_sha
                or readback.get("payload") != payload
                or readback.get("mode") != 0o640
            ):
                raise RuntimeError("Konfigurations-Readback weicht vom Commit ab")
            return readback
        except Exception as exc:
            try:
                current = snapshot_bound_file(
                    target,
                    allow_missing=True,
                    max_bytes=_MAX_CONFIG_BYTES,
                )
            except Exception:
                raise RuntimeError(
                    "Konfigurationsrollback ist wegen nicht bindbarem Zieldrift gesperrt"
                ) from exc

            if snapshots_match(current, preimage, exact_metadata=True):
                raise
            ours = bool(
                current.get("exists")
                and current.get("kind") == "regular"
                and current.get("sha256") == payload_sha
                and current.get("uid") == uid
                and current.get("gid") == gid
                and current.get("mode") == 0o640
            )
            if committed is not None and snapshots_match(
                current,
                committed,
                exact_metadata=False,
            ):
                ours = True
            if not ours:
                raise RuntimeError(
                    "Konfigurationsrollback ist wegen Fremddrift gesperrt"
                ) from exc

            restored = restore_bound_file(
                preimage,
                expected_current=current,
                max_bytes=_MAX_CONFIG_BYTES,
            )
            if preimage.get("exists"):
                rollback_ok = bool(
                    restored.get("exists")
                    and restored.get("kind") == "regular"
                    and restored.get("sha256") == preimage.get("sha256")
                    and restored.get("uid") == preimage.get("uid")
                    and restored.get("gid") == preimage.get("gid")
                    and restored.get("mode") == preimage.get("mode")
                )
            else:
                rollback_ok = not restored.get("exists")
            if not rollback_ok:
                raise RuntimeError("Konfigurationsrollback blieb unvollständig") from exc
            raise


def ask(prompt, default=None, headless=False):
    """Fragt Benutzer mit Standardwert ab."""
    if headless:
        return default
    value = input(f"{prompt} [{default}]: ").strip()
    return value if value else default


def write_param(f, key, value, enabled=True):
    """Schreibt einen Parameter aktiv oder auskommentiert."""
    prefix = "" if enabled else "#"
    f.write(f"{prefix}{key} = {value}\n")


def copy_existing_config(*, install_path=None, install_user=None):
    """Kopiert eine vorhandene e3dc.config.txt in den Zielordner."""
    print("\n--- Vorhandene Konfiguration kopieren ---\n")
    config_logger.info("Versuche, eine vorhandene Konfiguration zu kopieren.")

    try:
        authority = _resolve_config_authority(
            install_path=install_path,
            install_user=install_user,
        )
    except Exception as exc:
        print(f"✗ Konfigurationskontext ist nicht vertrauenswürdig: {exc}")
        log_error("create_config", "Konfigurationskontext konnte nicht gebunden werden", exc)
        return False

    default_source = os.path.join(
        authority["home_dir"],
        "Install",
        "e3dc.config.txt",
    )
    source_path = ask("Pfad zur vorhandenen e3dc.config.txt", default_source)
    source_path = str(source_path or "").strip()
    if (
        not os.path.isabs(source_path)
        or os.path.normpath(source_path) != source_path
    ):
        print(f"✗ Der Quellpfad muss absolut und kanonisch sein: {source_path}")
        log_warning(
            "create_config",
            f"Nicht kanonischer Quellpfad für Konfigurationsdatei: {source_path}",
        )
        return False

    try:
        source_snapshot = read_bound_regular_file(
            source_path,
            expected_uid=authority["uid"],
            max_bytes=_MAX_CONFIG_BYTES,
        )
        if int(source_snapshot.get("mode") or 0) & 0o022:
            raise RuntimeError(
                "Konfigurationsquelle ist gruppen- oder weltbeschreibbar"
            )
        _project_config_payload(
            source_snapshot["payload"],
            authority,
            source_snapshot=source_snapshot,
        )
        print(
            f"✓ Datei sicher projiziert: {source_path} → "
            f"{authority['config_path']}"
        )
        config_logger.info(
            "Konfigurationsdatei sicher projiziert von %s nach %s",
            source_path,
            authority["config_path"],
        )

        print(f"\n✓ Konfiguration erfolgreich kopiert und installiert!\n")
        log_task_completed("Konfiguration erstellen", details="Vorhandene Konfiguration kopiert")
        return True

    except Exception as e:
        print(f"✗ Fehler beim Kopieren der Datei: {e}")
        log_error("create_config", f"Fehler beim Kopieren der Konfigurationsdatei: {e}", e)
        return False


def create_e3dc_config(headless=False, *, install_path=None, install_user=None):
    """Kompletter Config-Wizard mit allen Parametern und Defaults."""
    print("\n=== E3DC-Konfiguration erstellen ===\n")
    config_logger.info("Starte Konfigurations-Wizard.")

    try:
        authority = _resolve_config_authority(
            install_path=install_path,
            install_user=install_user,
        )
    except Exception as exc:
        print(f"✗ Konfigurationskontext ist nicht vertrauenswürdig: {exc}")
        log_error("create_config", "Konfigurationskontext konnte nicht gebunden werden", exc)
        return False

    # Prüfen ob vorhandene Config kopiert werden soll
    copy_existing = ask("Möchtest du eine vorhandene e3dc.config.txt kopieren? (j/n)", "n", headless)

    if copy_existing and copy_existing.lower() == "j":
        if copy_existing_config(
            install_path=authority["install_path"],
            install_user=authority["install_user"],
        ):
            return True
        else:
            print("\nFortfahren mit manuellem Wizard...\n")
            config_logger.warning("Kopieren der Konfiguration fehlgeschlagen, fahre mit manuellem Wizard fort.")

    cfg = {}

    # =========================================================
    # DIREKTVERMARKTUNG
    # =========================================================
    print("\n--- DIREKTVERMARKTUNG ---\n")
    dv_input = ask("Direktvermarktung aktiv? (j/n)", "n", headless)
    cfg["dv"] = "1" if dv_input.lower() == "j" else "0"

    if cfg["dv"] == "1":
        cfg["dvwbkwh"] = ask("Wallbox-Bedarf E-Auto (kWh)", "30", headless)
        cfg["dvmp"] = ask("Marktprämie (€/MWh)", "60", headless)
    else:
        cfg["dvwbkwh"] = "30"
        cfg["dvmp"] = "60"

    # =========================================================
    # GRUNDDATEN
    # =========================================================
    print("--- GRUNDDATEN ---\n")
    cfg["server_ip"] = ask("E3DC IP-Adresse", "", headless)
    cfg["server_port"] = ask("Port", "5033", headless)
    cfg["user"] = ask("Benutzername", "", headless)
    cfg["password"] = ask("Passwort", "", headless)
    cfg["aes"] = ask("AES-Passwort", "", headless)

    # =========================================================
    # LEISTUNGS- UND SPEICHERPARAMETER
    # =========================================================
    print("\n--- LEISTUNGS- UND SPEICHERPARAMETER ---\n")
    cfg["wrleistung"] = ask("Wechselrichterleistung (W)", "11700", headless)
    cfg["speichergroesse"] = ask("Speichergröße (kWh)", "15", headless)
    cfg["speicherEV"] = ask("Speicher Eigenverbrauch (W)", "80", headless)
    cfg["speicherETA"] = ask("Speicher Wirkungsgrad", "0.97", headless)
    cfg["einspeiselimit"] = ask("Einspeiselimit (kW)", "10.3", headless)
    cfg["unload"] = ask("Unload (%)", "65", headless)
    cfg["ladeschwelle"] = ask("Ladeschwelle (%)", "70", headless)
    cfg["ladeende"] = ask("Ladeende (%)", "85", headless)
    cfg["ladeende2"] = ask("Ladeende2 (%)", "91", headless)
    cfg["Ladeende2rampe"] = ask("Ladeende2-Rampe", "2", headless)
    cfg["maximumLadeleistung"] = ask("Maximale Ladeleistung (W)", "12500", headless)
    cfg["powerfaktor"] = ask("Powerfaktor", "1.75", headless)
    cfg["rb"] = ask("Regelbeginn", "7", headless)
    cfg["re"] = ask("Regelende", "12.5", headless)
    cfg["le"] = ask("Ladeende", "14.2", headless)

    # =========================================================
    # WALLBOX
    # =========================================================
    print("\n--- WALLBOX ---\n")
    wb_id = ask("E3DC Wallbox ID (0-n, oder -1 für keine)", "0", headless)
    cfg["wallbox"] = wb_id

    is_wb_configured = False
    try:
        is_wb_configured = int(cfg.get("wallbox", -1)) >= 0
    except (ValueError, TypeError):
        pass

    if is_wb_configured:
        cfg["wbmode"] = ask("WB Modus", "4", headless)
        cfg["wbminlade"] = ask("WB Mindestladeleistung (W)", "1200", headless)
        cfg["wbminSoC"] = ask("WB Mindest-SoC (%)", "85", headless)
        cfg["wbmaxladestrom"] = ask("WB Maximalstrom (A)", "32", headless)
        cfg["wbminladestrom"] = ask("WB Mindeststrom (A)", "6", headless)
        cfg["wbhour"] = ask("WB hour-Modus", "0", headless)
        cfg["Wbvon"] = ask("WB hour Startzeit", "22", headless)
        cfg["Wbbis"] = ask("WB hour Endzeit", "6", headless)
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
    print("\n--- WÄRMEPUMPE & HEIZSTAB ---\n")
    wp = ask("Wärmepumpe vorhanden? (j/n)", "n", headless)
    cfg["WP"] = (wp.lower() == "j")

    if cfg["WP"]:
        cfg["shellyem_ip"] = ask("Shelly EM IP", "", headless)
        cfg["WPHeizlast"] = ask("Heizlast (kW)", "18", headless)
        cfg["WPHeizgrenze"] = ask("Heizgrenze (°C)", "13", headless)
        cfg["WPLeistung"] = ask("Heizleistung (kW)", "20", headless)
        cfg["WPMin"] = ask("Min-Verbrauch (kW)", "0.5", headless)
        cfg["WPMax"] = ask("Max-Verbrauch (kW)", "4.7", headless)
    else:
        # Defaults setzen
        cfg["shellyem_ip"] = ""
        cfg["WPHeizlast"] = ""
        cfg["WPHeizgrenze"] = ""
        cfg["WPLeistung"] = ""
        cfg["WPMin"] = ""
        cfg["WPMax"] = ""

    hs = ask("Heizstab vorhanden? (j/n)", "n", headless)
    cfg["heizstab"] = "1" if hs.lower() == "j" else "0"
    if cfg["heizstab"] == "1":
        cfg["heizstab_ip"] = ask("Heizstab IP-Adresse", "", headless)
    else:
        cfg["heizstab_ip"] = ""

    # =========================================================
    # AWATTAR
    # =========================================================
    print("\n--- AWATTAR ---\n")
    aw = ask("Awattar aktiv? (j/n)", "j", headless)
    cfg["awattar"] = (aw.lower() == "j")

    if cfg["awattar"]:
        cfg["awmwst"] = ask("MwSt (%)", "19", headless)
        cfg["awnebenkosten"] = ask("Nebenkosten (ct)", "15.915", headless)
        cfg["awaufschlag"] = ask("Aufschlag (%)", "10", headless)
        cfg["awland"] = ask("Land (de/at/ch)", "de", headless)
        cfg["awreserve"] = ask("Reserve (%)", "20", headless)
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
    om = ask("OpenMeteo aktiv? (j/n)", "j", headless)
    cfg["openmeteo"] = (om.lower() == "j")

    if cfg["openmeteo"]:
        cfg["hoehe"] = ask("Breitengrad (°N)", "48.00000", headless)
        cfg["laenge"] = ask("Längengrad (°E)", "13.00000", headless)

        print("\n--- Forecast Parameter ---")
        cfg["forecast1"] = ask("Forecast1 (Neigung/Azimut/kWp)", "35/0/10.0", headless)

        # Forecast 2?
        f2 = ask("Forecast2 hinzufügen? (j/n)", "n", headless)
        cfg["forecast2_enabled"] = (f2.lower() == "j")
        if cfg["forecast2_enabled"]:
            cfg["forecast2"] = ask("Forecast2 (Neigung/Azimut/kWp)", "0/90/5.0", headless)
        else:
            cfg["forecast2"] = ""

        # Forecast 3?
        f3 = ask("Forecast3 hinzufügen? (j/n)", "n", headless)
        cfg["forecast3_enabled"] = (f3.lower() == "j")
        if cfg["forecast3_enabled"]:
            cfg["forecast3"] = ask("Forecast3 (Neigung/Azimut/kWp)", "0/-90/5.0", headless)
        else:
            cfg["forecast3"] = ""

        cfg["ForecastSoc"] = ask("Forecast SOC-Faktor", "1.2", headless)
        cfg["ForecastConsumption"] = ask("Forecast Verbrauchsfaktor", "1", headless)
        cfg["ForecastReserve"] = ask("Forecast Reserve (%)", "5", headless)
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
    # TELEGRAM
    # =========================================================
    print("\n--- TELEGRAM BENACHRICHTIGUNGEN ---\n")
    cfg["telegram_token"] = ask("Telegram Bot Token", "", headless)
    cfg["telegram_chat_id"] = ask("Telegram Chat ID", "", headless)
    cfg["telegram_stats_enable"] = "1" if ask("Tägliche Statistik (07:00 Uhr) senden? (j/n)", "n", headless).lower() == "j" else "0"
    cfg["telegram_weekly_enable"] = "1" if ask("Wöchentliche Statistik (Sonntag 20:00) senden? (j/n)", "j", headless).lower() == "j" else "0"

    # =========================================================
    # DATEI SCHREIBEN
    # =========================================================
    if write_e3dc_config(
        cfg,
        install_path=authority["install_path"],
        install_user=authority["install_user"],
    ) is not True:
        return False
    print(f"\n✓ Konfiguration gespeichert unter {authority['config_path']}\n")
    log_task_completed("Konfiguration erstellen", details="Manuell über Wizard erstellt")
    return True


def write_e3dc_config(cfg, *, install_path=None, install_user=None):
    """Rendert im Speicher und projiziert die Konfiguration transaktional."""

    try:
        authority = _resolve_config_authority(
            install_path=install_path,
            install_user=install_user,
        )
        with io.StringIO() as f:
            # Grunddaten
            write_param(f, "server_ip", cfg["server_ip"])
            write_param(f, "server_port", cfg["server_port"])
            write_param(f, "e3dc_user", cfg["user"])
            write_param(f, "e3dc_password", cfg["password"])
            write_param(f, "aes_password", cfg["aes"])

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

            is_wb_configured = False
            try:
                is_wb_configured = int(cfg.get("wallbox", -1)) >= 0
            except (ValueError, TypeError):
                pass

            # Wallbox
            f.write("\n# Wallbox Parameter\n")
            write_param(f, "wallbox", cfg["wallbox"])
            write_param(f, "wbmode", cfg.get("wbmode", ""), is_wb_configured)
            write_param(f, "wbminlade", cfg.get("wbminlade", ""), is_wb_configured)
            write_param(f, "wbminSoC", cfg.get("wbminSoC", ""), is_wb_configured)
            write_param(f, "wbmaxladestrom", cfg.get("wbmaxladestrom", ""), is_wb_configured)
            write_param(f, "wbminladestrom", cfg.get("wbminladestrom", ""), is_wb_configured)
            write_param(f, "wbhour", cfg.get("wbhour", ""), is_wb_configured)
            write_param(f, "Wbvon", cfg.get("Wbvon", ""), is_wb_configured)
            write_param(f, "Wbbis", cfg.get("Wbbis", ""), is_wb_configured)

            # Wärmepumpe
            f.write("\n# Wärmepumpe Parameter\n")
            write_param(f, "WP", str(cfg["WP"]).lower())
            write_param(f, "shellyem_ip", cfg.get("shellyem_ip", ""), cfg["WP"])
            write_param(f, "WPHeizlast", cfg.get("WPHeizlast", ""), cfg["WP"])
            write_param(f, "WPHeizgrenze", cfg.get("WPHeizgrenze", ""), cfg["WP"])
            write_param(f, "WPLeistung", cfg.get("WPLeistung", ""), cfg["WP"])
            write_param(f, "WPMin", cfg.get("WPMin", ""), cfg["WP"])
            write_param(f, "WPMax", cfg.get("WPMax", ""), cfg["WP"])

            write_param(f, "heizstab", cfg.get("heizstab", "0"))
            write_param(
                f,
                "heizstab_ip",
                cfg.get("heizstab_ip", ""),
                cfg.get("heizstab", "0") == "1",
            )

            # Direktvermarktung
            f.write("\n# Direktvermarktung\n")
            write_param(f, "dv", cfg.get("dv", "0"))
            write_param(
                f,
                "dvwbkwh",
                cfg.get("dvwbkwh", "30"),
                cfg.get("dv", "0") == "1",
            )
            write_param(
                f,
                "dvmp",
                cfg.get("dvmp", "60"),
                cfg.get("dv", "0") == "1",
            )

            # Awattar
            f.write("\n# Awattar Parameter\n")
            write_param(f, "awattar", str(cfg["awattar"]).lower())
            write_param(f, "awmwst", cfg.get("awmwst", ""), cfg["awattar"])
            write_param(f, "awnebenkosten", cfg.get("awnebenkosten", ""), cfg["awattar"])
            write_param(f, "awaufschlag", cfg.get("awaufschlag", ""), cfg["awattar"])
            write_param(f, "awland", cfg.get("awland", ""), cfg["awattar"])
            write_param(f, "awreserve", cfg.get("awreserve", ""), cfg["awattar"])

            # Telegram
            f.write("\n# Telegram Benachrichtigungen\n")
            write_param(f, "telegram_token", cfg.get("telegram_token", ""))
            write_param(f, "telegram_chat_id", cfg.get("telegram_chat_id", ""))
            write_param(
                f,
                "telegram_stats_enable",
                cfg.get("telegram_stats_enable", "0"),
            )
            write_param(
                f,
                "telegram_weekly_enable",
                cfg.get("telegram_weekly_enable", "1"),
            )

            # OpenMeteo + Forecast
            f.write("\n# OpenMeteo & Forecast Parameter\n")
            write_param(f, "openmeteo", str(cfg["openmeteo"]).lower())
            write_param(f, "hoehe", cfg.get("hoehe", ""), cfg["openmeteo"])
            write_param(f, "laenge", cfg.get("laenge", ""), cfg["openmeteo"])
            write_param(f, "forecast1", cfg.get("forecast1", ""), cfg["openmeteo"])
            write_param(
                f,
                "forecast2",
                cfg.get("forecast2", ""),
                cfg.get("forecast2_enabled", False),
            )
            write_param(
                f,
                "forecast3",
                cfg.get("forecast3", ""),
                cfg.get("forecast3_enabled", False),
            )
            write_param(f, "ForecastSoc", cfg.get("ForecastSoc", ""), cfg["openmeteo"])
            write_param(
                f,
                "ForecastConsumption",
                cfg.get("ForecastConsumption", ""),
                cfg["openmeteo"],
            )
            write_param(
                f,
                "ForecastReserve",
                cfg.get("ForecastReserve", ""),
                cfg["openmeteo"],
            )
            payload = f.getvalue().encode("utf-8")

        _project_config_payload(payload, authority)
        config_logger.info(
            "Konfigurationsdatei sicher geschrieben: %s",
            authority["config_path"],
        )
        return True
    except Exception as e:
        print(f"✗ Fehler beim Schreiben der Konfiguration: {e}")
        log_error(
            "create_config",
            f"Fehler beim Schreiben der Konfigurationsdatei: {e}",
            e,
        )
        return False


register_command("7", "E3DC-Konfiguration erstellen", create_e3dc_config, sort_order=70)
