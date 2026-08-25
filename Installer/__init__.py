"""
E3DC-Control Installer Package

Ein modulares Installer-System für E3DC-Control mit dynamischer Menüregistrierung.
"""

__version__ = "5.4.4c"




__author__ = "A9x"

# Paket-Initialisierung
__all__ = [
    "core",
    "utils",
    "permissions",
    "system",
    "backup",
    "update",
    "rollback",
    "diagrammphp",
    "config_wizard",
    "create_config",
    "strompreis_wizard",
    "ramdisk",
    "uninstall",
    "install_all",
    "install_docker",
    "uninstall_docker",
    "self_update",
    "install_luxtronik",
    "install_ha",
    "install_notifier",
    "install_mqtt_hub",
    "openwb_mqtt",
    "install_local_mqtt",
    "install_bluelink",
    "install_watchdog",
    "venv_tools",
    "change_venv",
    "data_models",
    "service_setup",
    "service_catalog",
    "service_load_snapshot",
    "web_installer",
    "emergency_mode",
    "install_matter",
    "Heat",             # Zentrale Wärme-Policy ohne Hardwarewrites
    "heizstab_manager",  # Heizstab/Shelly Manager (wp_type=2/3)
    "stiebel",           # Stiebel ISG Live-Dienst (read-only)
    "dimplex",           # Dimplex WPM Touch / NWPM Live-Dienst
    "e3dc_live",         # RSCP Live-Dienst (Python-nativ)
]
