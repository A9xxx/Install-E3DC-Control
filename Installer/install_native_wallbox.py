from .core import register_command
from .utils import _create_service_file
from .logging_manager import log_task_completed, log_error

SERVICE_NAME = "e3dc-wallbox-manager"
SCRIPT_NAME = "wallbox_manager.py"


def setup_wallbox_service(headless=False, *, start_service=True):
    """Richtet den Systemd Service für den nativen Python Wallbox Manager ein."""
    print("\n=== Native Wallbox Regelung Setup ===\n")
    print("Dieser Schritt installiert den Hintergrunddienst für Drittanbieter Wallboxen (Go-E, etc.).")
    print("Hinweis: Vergiss nicht, wb_native_enable=1 in der E3DC-Config (Web UI) zu setzen!\n")

    print(f"→ Erstelle Systemd Service '{SERVICE_NAME}' über den kanonischen Unit-Helper…")
    try:
        result = _create_service_file(
            SERVICE_NAME,
            "E3DC Native Wallbox Manager",
            SCRIPT_NAME,
            "python3",
            restart_sec=15,
            start_service=bool(start_service),
            require_venv=True,
        )
        if result is not True:
            log_error(
                "wallbox_setup",
                "Kanonische Wallbox-Service-Installation meldet einen Fehler.",
            )
            return False
        log_task_completed("Native Wallbox Manager Service eingerichtet")
        return True
    except Exception as e:
        print(f"✗ Fehler beim Installieren des Services: {e}")
        log_error("wallbox_setup", f"Fehler bei Service Installation: {e}")
        return False
    finally:
        if not headless:
            input("\nDrücke Enter um ins Hauptmenü zurückzukehren...")

register_command("42", "Native Wallbox Manager (z.B. Go-E Charger)", setup_wallbox_service, sort_order=42)
