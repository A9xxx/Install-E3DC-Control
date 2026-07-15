"""
Sichere Task-Ausführungs-Wrapper mit automatischer Fehler-Logging.
Wird um Menu-Befehle gewickelt um Crashes zu tracken.
"""

import traceback
from .logging_manager import log_error, log_task_completed, log_task_skipped, log_warning


def safe_execute_task(task_name, task_function, *args, **kwargs):
    """
    Führt eine Task mit Fehlerbehandlung und Logging aus.
    
    Args:
        task_name: Name der Task (z.B. "Rechte prüfen & korrigieren")
        task_function: Funktion die ausgeführt werden soll
        *args, **kwargs: Argumente für task_function
    
    Returns:
        True wenn erfolgreich, False bei Fehler
    """
    try:
        print(f"\n■ Starte: {task_name}…")
        
        # Führe Task aus
        result = task_function(*args, **kwargs)
        
        # Log Erfolg
        log_task_completed(task_name)
        print(f"✓ {task_name} abgeschlossen.\n")
        
        return True
        
    except KeyboardInterrupt:
        log_task_skipped(task_name, reason="Benutzer hat abgebrochen (Ctrl+C)")
        print(f"\n✗ {task_name} vom Benutzer abgebrochen.\n")
        return False
        
    except Exception as e:
        error_msg = str(e)
        log_error(module_name=task_name, error_msg=error_msg, exception=e)
        print(f"\n✗ FEHLER in {task_name}:")
        print(f"  {error_msg}")
        print(f"  Stack-Trace siehe error.log\n")
        return False


def safe_menu_action(menu_choice, menu_name, menu_function, *args, **kwargs):
    """
    Wrapper für Menü-Aktionen mit Logging der Benutzer-Auswahl.
    
    Args:
        menu_choice: Menünummer (z.B. "2")
        menu_name: Menüname (z.B. "Rechte prüfen & korrigieren")
        menu_function: Auszuführende Funktion
        *args, **kwargs: Argumente
    """
    from .logging_manager import log_menu_action
    
    # Log Menü-Auswahl
    log_menu_action(menu_choice, menu_name)
    
    # Führe sichere Ausführung durch
    return safe_execute_task(menu_name, menu_function, *args, **kwargs)


def wrap_menu_handlers(menu_registry):
    """
    Wickelt alle registrierten Menü-Handler mit safe_menu_action ein.
    
    Args:
        menu_registry: Dict von {nummer: (name, function, sort_order)}
    
    Usage in install_all.py:
        menu = {}
        register_command("1", "Installation", install_system)
        ...
        wrapped_menu = wrap_menu_handlers(menu)
        # Jetzt alle Commands mit Error-Handling!
    """
    wrapped = {}
    for key, (name, func, sort_order) in menu_registry.items():
        wrapped[key] = (name, lambda f=func, k=key, n=name: safe_menu_action(k, n, f), sort_order)
    return wrapped
