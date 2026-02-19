"""
Zentrales Logging-Management für Installation und Fehlerbehandlung.
Verwaltet mehrere Log-Streams: install.log, permissions.log, error.log
"""

import logging
import os
from datetime import datetime

# Installation Session-Tracking
_session_stats = {
    "menu_actions": [],
    "errors": [],
    "completed_tasks": [],
    "skipped_tasks": [],
    "warnings": []
}


def _fix_log_ownership(log_file):
    """Hilfsfunktion um Log-Datei auf korrekten Benutzer zu setzen."""
    try:
        # Late import um Circular Imports zu vermeiden
        from .installer_config import get_install_user, get_user_ids
        install_user = get_install_user()
        uid, gid = get_user_ids(install_user)
        os.chown(log_file, uid, gid)
        os.chmod(log_file, 0o664)  # rw-rw-r--
    except Exception:
        # Bei Fehler weitermachen (z.B. nicht als root)
        pass


def get_or_create_logger(name, log_file=None):
    """
    Erstellt oder holt Logger mit separater Datei und Formatter.
    
    Args:
        name: Logger-Name (z.B. "permissions", "backup", "install")
        log_file: Optional, absoluter Pfad zur Log-Datei
    
    Returns:
        Logger-Instanz mit propagate=False (keine Doppel-Logs!)
    """
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            handler = logging.FileHandler(log_file, encoding='utf-8')
            formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            
            # Setze Ownership auf Install-User
            _fix_log_ownership(log_file)
        
        logger.setLevel(logging.DEBUG)
        logger.propagate = False  # Verhindert Doppel-Logs!
    
    return logger


def setup_installation_loggers():
    """Initialisiert alle spezialisierten Logger für Installation."""
    log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    # Setze Ownership des log_dir auf Install-User
    try:
        from .installer_config import get_install_user, get_user_ids
        install_user = get_install_user()
        uid, gid = get_user_ids(install_user)
        os.chown(log_dir, uid, gid)
        os.chmod(log_dir, 0o775)  # rwxrwxr-x
    except Exception:
        pass  # Bei Fehlern weitermachen
    
    loggers = {
        "install": get_or_create_logger("install", os.path.join(log_dir, "install.log")),
        "permissions": get_or_create_logger("permissions", os.path.join(log_dir, "permissions.log")),
        "backup": get_or_create_logger("backup", os.path.join(log_dir, "backup.log")),
        "error": get_or_create_logger("error", os.path.join(log_dir, "error.log")),
    }
    
    return loggers


def log_menu_action(menu_choice, menu_name):
    """Loggt User-Menü-Auswahl zu install.log und Memory."""
    global _session_stats
    timestamp = datetime.now().strftime("%H:%M:%S")
    action = f"[{timestamp}] {menu_choice}: {menu_name}"
    _session_stats["menu_actions"].append(action)
    
    logger = logging.getLogger("install")
    logger.info(f"→ Benutzer wählte: {menu_name} ({menu_choice})")


def log_task_completed(task_name, details=""):
    """Loggt erfolgreiche Task-Ausführung."""
    global _session_stats
    _session_stats["completed_tasks"].append(f"{task_name}: {details}" if details else task_name)
    
    logger = logging.getLogger("install")
    logger.info(f"✓ Task abgeschlossen: {task_name}" + (f" - {details}" if details else ""))


def log_task_skipped(task_name, reason=""):
    """Loggt übersprungene Task."""
    global _session_stats
    _session_stats["skipped_tasks"].append(f"{task_name}: {reason}" if reason else task_name)
    
    logger = logging.getLogger("install")
    logger.warning(f"⊘ Task übersprungen: {task_name}" + (f" ({reason})" if reason else ""))


def log_error(module_name, error_msg, exception=None):
    """Loggt Fehler zu error.log UND install.log."""
    global _session_stats
    _session_stats["errors"].append(f"{module_name}: {error_msg}")
    
    error_logger = logging.getLogger("error")
    install_logger = logging.getLogger("install")
    
    if exception:
        error_logger.error(f"[{module_name}] {error_msg}", exc_info=exception)
        install_logger.error(f"✗ FEHLER in {module_name}: {error_msg} (siehe error.log)")
    else:
        error_logger.error(f"[{module_name}] {error_msg}")
        install_logger.error(f"✗ FEHLER in {module_name}: {error_msg}")


def log_warning(module_name, warning_msg):
    """Loggt Warnung."""
    global _session_stats
    _session_stats["warnings"].append(f"{module_name}: {warning_msg}")
    
    logger = logging.getLogger("install")
    logger.warning(f"⚠ [{module_name}] {warning_msg}")


def print_installation_summary():
    """Gibt detaillierte Installation-Summary und loggt sie."""
    logger = logging.getLogger("install")
    
    summary = "\n" + "="*60 + "\n"
    summary += "INSTALLATION-ZUSAMMENFASSUNG\n"
    summary += "="*60 + "\n\n"
    
    # Menu-Aktionen
    if _session_stats["completed_tasks"]:
        summary += f"✓ Abgeschlossene Tasks ({len(_session_stats['completed_tasks'])}):\n"
        for task in _session_stats["completed_tasks"]:
            summary += f"  • {task}\n"
        summary += "\n"
    
    if _session_stats["skipped_tasks"]:
        summary += f"⊘ Übersprungene Tasks ({len(_session_stats['skipped_tasks'])}):\n"
        for task in _session_stats["skipped_tasks"]:
            summary += f"  • {task}\n"
        summary += "\n"
    
    if _session_stats["warnings"]:
        summary += f"⚠ Warnungen ({len(_session_stats['warnings'])}):\n"
        for warn in _session_stats["warnings"]:
            summary += f"  • {warn}\n"
        summary += "\n"
    
    if _session_stats["errors"]:
        summary += f"✗ Fehler ({len(_session_stats['errors'])}):\n"
        for error in _session_stats["errors"]:
            summary += f"  • {error}\n"
        summary += "\n"
    
    summary += "="*60 + "\n"
    summary += f"Session beendet: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    summary += "="*60 + "\n"
    
    # Print + Log
    print(summary)
    logger.info("\n" + summary)
    
    return _session_stats


def get_session_stats():
    """Gibt aktuelle Session-Statistiken zurück."""
    return _session_stats.copy()


def reset_session_stats():
    """Setzt Session-Statistiken zurück."""
    global _session_stats
    _session_stats = {
        "menu_actions": [],
        "errors": [],
        "completed_tasks": [],
        "skipped_tasks": [],
        "warnings": []
    }
